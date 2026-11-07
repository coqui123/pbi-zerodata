"""Power BI Desktop control without screenshots: find, open, close, reload, engine port."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time

from .common import TOOLS_BIN, PbzError, Project, engine_call

BRIDGE = os.path.join(TOOLS_BIN, "powerbi-desktop.cmd")


def _ps(script: str, timeout: int = 60) -> str:
    """Run a PowerShell snippet: PowerShell 7 when installed, else the Windows PowerShell every Windows has."""
    for exe in ("pwsh", "powershell"):
        try:
            p = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script],
                               capture_output=True, timeout=timeout)
        except FileNotFoundError:
            continue
        return p.stdout.decode("utf-8", errors="replace")
    return ""


def _json_list(text: str) -> list:
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    return data if isinstance(data, list) else [data]


def processes() -> list[dict]:
    """PBIDesktop.exe processes with their command lines (file paths are metadata)."""
    out = _ps("Get-CimInstance Win32_Process -Filter \"Name='PBIDesktop.exe'\" | "
              "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress")
    return [{"pid": int(p["ProcessId"]), "cmd": p.get("CommandLine") or ""} for p in _json_list(out)]


def _dirty_state(inst: dict) -> bool | None:
    """Retain unknown; a missing bridge capability is never proof of clean state."""
    values = [inst[k] for k in ("hasUnsavedChanges", "unsaved", "isDirty") if isinstance(inst.get(k), bool)]
    return True if True in values else False if values else None


def bridge_status() -> list[dict]:
    if not os.path.exists(BRIDGE):
        return []
    try:
        p = subprocess.run([BRIDGE, "status"], capture_output=True, timeout=60)
        data = json.loads(p.stdout.decode("utf-8", errors="replace") or "{}")
    except (ValueError, subprocess.SubprocessError, OSError):
        return []
    insts = data.get("instances") if isinstance(data, dict) else None
    out = []
    for inst in insts or []:
        if not isinstance(inst, dict):
            continue
        pid = inst.get("pid") or inst.get("processId")
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        paths = [str(v) for k, v in inst.items() if isinstance(v, str) and re.search(r"(?i)path|file", k)]
        out.append({"pid": int(pid) if pid else None, "paths": paths,
                    "unsaved": _dirty_state(inst)})
    return out


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p.strip().strip('"')))


def find_instance(project: Project) -> dict | None:
    target = _norm(project.pbip)
    matches: dict[int, dict] = {}
    for inst in bridge_status():
        if any(_norm(p) == target for p in inst["paths"]):
            matches[inst["pid"]] = {"pid": inst["pid"], "via": "bridge", "unsaved": inst["unsaved"]}
    for proc in processes():
        args = [quoted or bare for quoted, bare in re.findall(r'"([^"]*)"|(\S+)', proc["cmd"])]
        if any(_norm(arg) == target for arg in args):
            matches.setdefault(proc["pid"], {"pid": proc["pid"], "via": "process", "unsaved": None})
    if len(matches) > 1:
        raise PbzError("AMBIGUOUS_DESKTOP", "multiple Desktop instances match this project; close the extra instance")
    return next(iter(matches.values()), None)


def engine_port(pid: int) -> int | None:
    """Port of the msmdsrv.exe started by this PBIDesktop process."""
    out = _ps("Get-CimInstance Win32_Process -Filter \"Name='msmdsrv.exe'\" | "
              "Select-Object ProcessId, ParentProcessId, CommandLine | ConvertTo-Json -Compress")
    for m in _json_list(out):
        if int(m.get("ParentProcessId") or 0) != pid:
            continue
        cmd = m.get("CommandLine") or ""
        mm = re.search(r'-s\s+"([^"]+)"', cmd) or re.search(r"-s\s+(\S+)", cmd)
        if mm:
            pf = os.path.join(mm.group(1), "msmdsrv.port.txt")
            try:
                with open(pf, "rb") as f:
                    raw = f.read()
                txt = raw.decode("utf-16-le" if raw[1:2] == b"\x00" else "utf-8", errors="ignore")
                digits = re.sub(r"\D", "", txt)
                if digits:
                    return int(digits)
            except OSError:
                pass
        ports = _ps(f"Get-NetTCPConnection -OwningProcess {int(m['ProcessId'])} -State Listen -ErrorAction "
                    "SilentlyContinue | Select-Object -ExpandProperty LocalPort")
        nums = [int(x) for x in ports.split() if x.isdigit()]
        if nums:
            return min(nums)
    return None


def _instance_status(project: Project, inst: dict | None, ready: bool = False) -> dict:
    if not inst:
        return {"verb": "desktop", "project": project.name, "open": False, "engine": "NOT_READY"}
    return {"verb": "desktop", "project": project.name, "open": True, "pid": inst.get("pid"),
            "via": inst.get("via"), "unsaved_changes": inst.get("unsaved"),
            "engine": "READY" if ready else "NOT_READY"}


def _ready(project: Project, inst: dict | None) -> bool:
    port = engine_port(inst["pid"]) if inst and inst.get("pid") else None
    if not port:
        return False
    res = engine_call("connect", {"private_dir": project.ws.private, "port": port}, timeout=30)
    return res.get("ok") is True


def status(project: Project) -> dict:
    inst = find_instance(project)
    return _instance_status(project, inst, _ready(project, inst))


def open_(project: Project, wait: int = 240) -> dict:
    inst = find_instance(project)
    already_open = inst is not None
    if not already_open:
        # File association supports the Store build as well as the desktop build.
        os.startfile(project.pbip)
    deadline = time.monotonic() + max(0, wait)
    while True:
        inst = find_instance(project)
        if _ready(project, inst):
            return {**_instance_status(project, inst, True), "result": "READY",
                    "action": "already_open" if already_open else "opened"}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {**_instance_status(project, inst), "result": "TIMEOUT", "action": "timeout"}
        time.sleep(min(2, remaining))


def close(project: Project, wait: int = 60) -> dict:
    inst = find_instance(project)
    if not inst:
        return {"verb": "desktop", "project": project.name, "open": False,
                "action": "not_open", "result": "NOT_OPEN"}
    # A close request cannot save or dismiss a modal. Never force-kill Desktop.
    pid = int(inst["pid"])
    _ps(f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; if ($p) {{ [void]$p.CloseMainWindow() }}")
    deadline = time.monotonic() + max(0, wait)
    while True:
        alive = _ps(f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ 'alive' }}").strip()
        if not alive:
            return {"verb": "desktop", "project": project.name, "open": False,
                    "action": "closed", "result": "CLOSED"}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"verb": "desktop", "project": project.name, "open": True,
                    "action": "WAITING_ON_DIALOG", "result": "WAITING_ON_DIALOG", "reason": "UNKNOWN",
                    "hint": "Desktop did not close. Ask the user to inspect pending dialogs; no dialog contents were read."}
        time.sleep(min(2, remaining))


def reload(project: Project) -> str:
    """Legacy string contract, with fail-closed dirty-state checks at the operation boundary."""
    inst = find_instance(project)
    if not inst or not os.path.exists(BRIDGE):
        return "NOT_AVAILABLE"
    if inst.get("unsaved") is True:
        return "DESKTOP_DIRTY"
    if inst.get("unsaved") is not False:
        return "DESKTOP_STATE_UNKNOWN"
    try:
        p = subprocess.run([BRIDGE, "reload", "--pid", str(inst["pid"])], capture_output=True, timeout=180)
        data = json.loads(p.stdout.decode("utf-8", errors="replace") or "{}")
    except (ValueError, subprocess.SubprocessError, OSError):
        return "RELOAD_FAILED"
    ok = p.returncode == 0 and isinstance(data, dict) and data.get("ok") is not False and not data.get("error")
    return "RELOADED" if ok else "RELOAD_FAILED"


def require_port(project: Project) -> int:
    """Engine port of the Desktop instance holding this project, cross-checked against its tables."""
    inst = find_instance(project)
    if not inst or not inst.get("pid"):
        raise PbzError("ENGINE_UNAVAILABLE", "open the project in Desktop first (`pbz desktop open`)")
    port = engine_port(int(inst["pid"]))
    if not port:
        raise PbzError("ENGINE_UNAVAILABLE", "Desktop is open but its engine port was not found yet")
    res = engine_call("connect", {"private_dir": project.ws.private, "port": port}, timeout=120)
    if not res.get("ok"):
        raise PbzError(res.get("class", "ENGINE_UNAVAILABLE"), "engine did not accept the connection")
    from . import tmdl
    expected = set(tmdl.load_model(os.path.join(project.model_dir, "definition")).tables)
    live = set(res.get("tables", []))
    live_user = {t for t in live if not t.startswith(("LocalDateTable_", "DateTableTemplate_"))}
    expected_user = {t for t in expected if not t.startswith(("LocalDateTable_", "DateTableTemplate_"))}
    if not expected_user or live_user != expected_user:
        raise PbzError("ENGINE_MISMATCH", "the open model's tables differ from this project; refusing to query it")
    return port
