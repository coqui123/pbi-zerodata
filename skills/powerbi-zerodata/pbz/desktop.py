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
        paths = [str(v) for k, v in inst.items() if isinstance(v, str) and re.search(r"(?i)path|file", k)]
        out.append({"pid": int(pid) if pid else None, "paths": paths,
                    "unsaved": bool(inst.get("hasUnsavedChanges") or inst.get("unsaved") or inst.get("isDirty"))})
    return out


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p.strip().strip('"')))


def find_instance(project: Project) -> dict | None:
    target = _norm(project.pbip)
    for inst in bridge_status():
        if any(_norm(p) == target for p in inst["paths"]):
            return {"pid": inst["pid"], "via": "bridge", "unsaved": inst["unsaved"]}
    for proc in processes():
        if target in os.path.normcase(proc["cmd"]):
            return {"pid": proc["pid"], "via": "process", "unsaved": None}
    return None


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


def status(project: Project) -> dict:
    inst = find_instance(project)
    if not inst:
        return {"verb": "desktop", "project": project.name, "open": False}
    port = engine_port(inst["pid"]) if inst.get("pid") else None
    return {"verb": "desktop", "project": project.name, "open": True, "pid": inst["pid"], "via": inst["via"],
            "unsaved_changes": inst.get("unsaved"), "engine": "READY" if port else "NOT_READY"}


def open_(project: Project, wait: int = 240) -> dict:
    if find_instance(project):
        return {**status(project), "action": "already_open"}
    # the bridge's `open` cannot find the Store build of Desktop; the file association can
    os.startfile(project.pbip)
    deadline = time.time() + wait
    while time.time() < deadline:
        inst = find_instance(project)
        port = engine_port(inst["pid"]) if inst and inst.get("pid") else None
        if port:
            res = engine_call("connect", {"private_dir": project.ws.private, "port": port})
            if res.get("ok") or res.get("class") == "ENGINE_UNAVAILABLE":
                return {**status(project), "action": "opened"}
        time.sleep(5)
    return {**status(project), "action": "timeout"}


def close(project: Project, wait: int = 60) -> dict:
    inst = find_instance(project)
    if not inst:
        return {"verb": "desktop", "project": project.name, "open": False, "action": "not_open"}
    pid = int(inst["pid"])
    _ps(f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; if ($p) {{ [void]$p.CloseMainWindow() }}")
    deadline = time.time() + wait
    while time.time() < deadline:
        alive = _ps(f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ 'alive' }}").strip()
        if not alive:
            return {"verb": "desktop", "project": project.name, "open": False, "action": "closed"}
        time.sleep(2)
    return {"verb": "desktop", "project": project.name, "open": True, "action": "WAITING_ON_DIALOG",
            "hint": "Desktop did not close; a Save prompt is probably open. Ask the user to answer it."}


def reload(project: Project) -> str:
    inst = find_instance(project)
    if not inst or not os.path.exists(BRIDGE):
        return "NOT_AVAILABLE"
    p = subprocess.run([BRIDGE, "reload", "--pid", str(inst["pid"])], capture_output=True, timeout=180)
    try:
        data = json.loads(p.stdout.decode("utf-8", errors="replace") or "{}")
    except ValueError:
        data = {}
    ok = p.returncode == 0 and not (isinstance(data, dict) and data.get("error"))
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
    if expected and live and not live <= expected | {t for t in live if t.startswith(("LocalDateTable_", "DateTableTemplate_"))}:
        raise PbzError("ENGINE_MISMATCH", "the open model's tables differ from this project; refusing to query it")
    return port
