#!/usr/bin/env python3
"""End-to-end tests for pbz-engine against the synthetic Canary fixture (stdlib + jsonschema, no pytest).

Run from anywhere:
    python tests\\test_engine.py            # opens the fixture in Power BI Desktop if it is not open
    python tests\\test_engine.py --offline  # only the verbs that need no Desktop
    python tests\\test_engine.py --close    # close the fixture's Desktop window at the end
    python tests\\test_engine.py --keep     # keep the temporary private_dir for inspection

Safety: the only Desktop instance ever queried is the one whose command line or bridge status shows
tests\\fixture\\Canary.pbip AND whose TMSCHEMA table names equal the fixture's. Nothing here prints values:
failures print the check name and a canary-redacted response. All canary literals live in
tests\\fixture\\CANARIES.md; this file contains none.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac as hmac_mod
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import zlib
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import jsonschema

S = Path(__file__).resolve().parent.parent
FIXTURE = S / "tests" / "fixture"
PBIP = FIXTURE / "Canary.pbip"
MODEL_DEF = FIXTURE / "Canary.SemanticModel" / "definition"
EXE = S / "engine" / "bin" / "pbz-engine.exe"
BRIDGE = S / "tools" / "node_modules" / ".bin" / "powerbi-desktop.cmd"
SCHEMA = json.loads((S / "engine" / "contract.schema.json").read_text("utf-8"))
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)
FIXTURE_TABLES = {"Person", "Project", "Hours", "DimDate", "ExceptionType", "Notes"}
BLANK = "∅blank"

# --------------------------------------------------------------------------- canaries


def load_canaries() -> dict[str, list[str]]:
    text = (FIXTURE / "CANARIES.md").read_text("utf-8")
    blocks: dict[str, list[str]] = {}
    for m in re.finditer(r"```(canary-[a-z-]+)\r?\n(.*?)```", text, re.S):
        blocks[m.group(1)] = [ln.strip() for ln in m.group(2).splitlines() if ln.strip()]
    return blocks


CAN = load_canaries()


def pick(block: str, pattern: str) -> list[str]:
    rx = re.compile(pattern)
    return sorted(s for s in CAN[block] if rx.search(s))


def req_canary(tag: str) -> str:
    found = pick("canary-request-strings", re.escape(tag))
    assert found, tag
    return found[0]


PERSONS = pick("canary-strings", r"^[A-Z]+-PERSON-\w+$")
INT_CANARIES = sorted(int(n) for n in CAN["canary-numbers"] if re.fullmatch(r"\d{6}", n))
DERIVED = CAN["canary-derived-numbers"]


def scan_patterns(for_files: bool) -> list[re.Pattern]:
    pats: list[re.Pattern] = []
    for s in CAN["canary-strings"] + CAN["canary-blob-strings"] + CAN["canary-request-strings"] + CAN["canary-stems"]:
        pats.append(re.compile(re.escape(s), re.I))
    pats.append(re.compile(r"(?<![0-9a-fA-F])(?:7f3a[1-49]|5b9d[12])(?![0-9a-fA-F])"))
    for n in CAN["canary-numbers"]:
        core = n.lstrip("-")
        if for_files and re.fullmatch(r"\d+", core) and int(core) == INT_CANARIES[2]:
            continue  # bare value shared with tests/test_mirror.py synthetic data (see CANARIES.md)
        tail = r"(?![0-9A-Za-z])" if re.fullmatch(r"\d+", core) else r"(?![0-9])"
        pats.append(re.compile(r"(?<![0-9A-Za-z.])" + re.escape(core) + tail))
    for n in DERIVED:
        ip = n.split(".")[0]
        pats.append(re.compile(r"(?<![0-9A-Za-z.])" + re.escape(ip) + r"(?![0-9])"))
        pats.append(re.compile(re.escape(ip[:-3] + "," + ip[-3:])))
    return pats


OUT_PATTERNS = scan_patterns(for_files=False)
FILE_PATTERNS = scan_patterns(for_files=True)


def redact(text: str) -> str:
    for p in OUT_PATTERNS:
        text = p.sub("<<REDACTED>>", text)
    return text


def canary_hits(text: str, patterns: list[re.Pattern]) -> int:
    return sum(1 for p in patterns if p.search(text))


# --------------------------------------------------------------------------- results

RESULTS: list[tuple[str, bool]] = []


def expect(name: str, cond: bool, detail=None) -> bool:
    ok = bool(cond)
    RESULTS.append((name, ok))
    line = ("PASS " if ok else "FAIL ") + name
    if not ok and detail is not None:
        try:
            line += " :: " + redact(json.dumps(detail, ensure_ascii=False, default=str))[:700]
        except Exception:
            line += " :: (detail not printable)"
    print(line, flush=True)
    return ok


# --------------------------------------------------------------------------- engine calls

OUTPUTS: list[tuple[str, bytes, bytes]] = []


def engine(verb: str, req: dict | None, label: str, raw: bytes | None = None, timeout: int = 300) -> dict | None:
    payload = raw if raw is not None else json.dumps(req, ensure_ascii=False).encode("utf-8")
    p = subprocess.run([str(EXE), verb], input=payload, capture_output=True, timeout=timeout)
    OUTPUTS.append((label, p.stdout, p.stderr))
    resp = None
    try:
        resp = json.loads(p.stdout.decode("utf-8"))
    except Exception:
        pass
    schema_errors = [] if resp is None else [e.message[:120] for e in VALIDATOR.iter_errors(resp)][:3]
    expect(f"[{label}] envelope: exit 0, no stderr, schema-valid",
           p.returncode == 0 and p.stderr == b"" and resp is not None and not schema_errors,
           {"exit": p.returncode, "stderr_bytes": len(p.stderr), "schema": schema_errors, "resp": resp})
    return resp


def by_id(items: list[dict] | None) -> dict[str, dict]:
    return {i.get("id"): i for i in (items or [])}


# --------------------------------------------------------------------------- DPAPI + HMAC (Python side reference)


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.POINTER(DATA_BLOB),
                                        ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(DATA_BLOB)]
_crypt32.CryptUnprotectData.restype = wt.BOOL
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_kernel32.LocalFree.restype = ctypes.c_void_p
CRYPTPROTECT_UI_FORBIDDEN = 0x1


def dpapi_unprotect(blob: bytes) -> bytes:
    buf = ctypes.create_string_buffer(blob, len(blob))
    inp = DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = DATA_BLOB()
    if not _crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


# .NET char.IsWhiteSpace set (String.Trim): Python's str.strip() also strips U+001C..U+001F, .NET does not.
DOTNET_WS = "\t\n\v\f\r \x85\xa0 " + "".join(chr(c) for c in range(0x2000, 0x200B)) + "    　"


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFC", s).strip(DOTNET_WS)
    # .NET ToLowerInvariant uses simple (1:1) case mapping
    return "".join("i" if ch == "İ" else (ch.lower() if len(ch.lower()) == 1 else ch) for ch in s)


def dotnet_r(x: float) -> str:
    """double.ToString("R", InvariantCulture) on .NET 10: shortest round-trip digits, 'G' layout with
    nMaxDigits 17, i.e. scientific only when the value is >= 1e17 or < 1e-4 (checked against the runtime)."""
    if x != x:
        return "NaN"
    if x in (float("inf"), float("-inf")):
        return "Infinity" if x > 0 else "-Infinity"
    if x == 0:
        return "-0" if str(x).startswith("-") else "0"
    sign, digits, exp = Decimal(repr(x)).as_tuple()
    ds = "".join(map(str, digits)).lstrip("0")
    e = int(exp)
    while len(ds) > 1 and ds.endswith("0"):
        ds = ds[:-1]
        e += 1
    scale = len(ds) + e  # decimal point position relative to the first significant digit
    neg = "-" if sign else ""
    if scale > 17 or scale < -3:
        mant = ds[0] + ("." + ds[1:] if len(ds) > 1 else "")
        k = scale - 1
        return f"{neg}{mant}E{'+' if k >= 0 else '-'}{abs(k):02d}"
    if scale <= 0:
        return f"{neg}0.{'0' * (-scale)}{ds}"
    if scale >= len(ds):
        return f"{neg}{ds}{'0' * (scale - len(ds))}"
    return f"{neg}{ds[:scale]}.{ds[scale:]}"


def norm_number(v: float) -> str:
    r = float(f"{v:.12g}")
    if r == 0:
        r = 0.0
    return dotnet_r(r)


def norm_value(v) -> str:
    if v is None:
        return BLANK
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return norm_number(float(v))
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%dT%H:%M:%S")
    return norm_text(str(v))


def hmac_hex(key: bytes, v) -> str:
    return hmac_mod.new(key, norm_value(v).encode("utf-8"), hashlib.sha256).hexdigest()


def words(value: str) -> list[str]:
    """v1.1 word_hmacs words: whitespace split, non-letters stripped at both ends, >= 3 letters."""
    out = []
    for raw in value.split():
        a, b = 0, len(raw) - 1
        while a <= b and not raw[a].isalpha():
            a += 1
        while b >= a and not raw[b].isalpha():
            b -= 1
        if a > b:
            continue
        w = raw[a:b + 1]
        if sum(ch.isalpha() for ch in w) >= 3:
            out.append(w)
    return out


# --------------------------------------------------------------------------- Desktop discovery (fixture only)


def powershell_json(command: str):
    cmd = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command
    p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
                       capture_output=True, timeout=120)
    out = p.stdout.decode("utf-8", "replace").strip()
    if not out:
        return []
    data = json.loads(out)
    return data if isinstance(data, list) else [data]


def desktop_processes() -> list[dict]:
    return powershell_json(
        "Get-CimInstance Win32_Process -Filter \"Name='PBIDesktop.exe' OR Name='msmdsrv.exe'\" | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress")


def bridge_fixture_pids() -> set[int]:
    try:
        p = subprocess.run([str(BRIDGE), "status"], capture_output=True, timeout=120)
        data = json.loads(p.stdout.decode("utf-8", "replace"))
    except Exception:
        return set()
    target = os.path.normcase(str(PBIP))
    return {int(i["pid"]) for i in data.get("instances", [])
            if i.get("currentFilePath") and os.path.normcase(i["currentFilePath"]) == target}


def find_fixture_instance() -> tuple[int, int] | None:
    """(PBIDesktop pid, msmdsrv port) for the Desktop that has the fixture open, else None."""
    procs = desktop_processes()
    target = os.path.normcase(str(PBIP))
    pids = {int(p["ProcessId"]) for p in procs
            if (p.get("Name") or "").lower() == "pbidesktop.exe" and target in os.path.normcase(p.get("CommandLine") or "")}
    pids |= bridge_fixture_pids()
    for pid in sorted(pids):
        for m in procs:
            if (m.get("Name") or "").lower() != "msmdsrv.exe" or int(m.get("ParentProcessId") or 0) != pid:
                continue
            mm = re.search(r'-s\s+"([^"]+)"', m.get("CommandLine") or "")
            if not mm:
                continue
            port_file = Path(mm.group(1)) / "msmdsrv.port.txt"
            if not port_file.is_file():
                continue
            raw = port_file.read_bytes()
            text = raw.decode("utf-16-le") if b"\x00" in raw else raw.decode("ascii", "replace")
            digits = re.sub(r"\D", "", text)
            if digits:
                return pid, int(digits)
    return None


def close_desktop(pid: int, wait_s: int = 90) -> str:
    """Graceful close of the fixture instance only: Process.CloseMainWindow, then wait. No UI automation:
    if Desktop shows a save prompt, the person at the machine answers it (the fixture is synthetic)."""
    script = ("$p = Get-Process -Id %d -ErrorAction SilentlyContinue; if (-not $p) { 'gone'; exit 0 }; "
              "[void]$p.CloseMainWindow(); if ($p.WaitForExit(%d)) { 'closed' } else { 'still open (prompt?)' }") % (int(pid), wait_s * 1000)
    p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, timeout=wait_s + 60)
    lines = p.stdout.decode("utf-8", "replace").strip().splitlines()
    return lines[-1] if lines else "unknown"

# --------------------------------------------------------------------------- tests


class Env:
    pd: Path
    port: int = 0
    pid: int = 0
    opened_here: bool = False
    log_ids: dict[str, str] = {}

    def req(self, **kw) -> dict:
        d = {"private_dir": str(self.pd)}
        if self.port:
            d["port"] = self.port
        d.update(kw)
        return d


def test_offline(env: Env, tmp: Path) -> None:
    r = engine("ping", None, "ping-empty", raw=b"")
    expect("ping without private_dir", r and r.get("ok") is True and r.get("version") == "1", r)
    r = engine("ping", {"private_dir": str(env.pd)}, "ping")
    expect("ping creates private_dir/logs/snapshots", r and r["ok"] and (env.pd / "logs").is_dir() and (env.pd / "snapshots").is_dir(), r)
    r = engine("no-such-verb", {"private_dir": str(env.pd)}, "unknown-verb")
    expect("unknown verb -> BAD_REQUEST + log_id", r and r["ok"] is False and r["class"] == "BAD_REQUEST" and "log_id" in r, r)
    r = engine("health", None, "bad-json", raw=b"{not json")
    expect("invalid JSON -> BAD_REQUEST", r and r["ok"] is False and r["class"] == "BAD_REQUEST", r)
    r = engine("health", {"port": 1}, "no-private-dir")
    expect("missing private_dir -> BAD_REQUEST without log_id", r and r["class"] == "BAD_REQUEST" and "log_id" not in r, r)
    r = engine("health", {"private_dir": "relative\\dir", "port": 1}, "relative-private-dir")
    expect("relative private_dir -> BAD_REQUEST", r and r["class"] == "BAD_REQUEST", r)
    r = engine("probe", {"private_dir": str(env.pd), "port": 1, "table": "Hours", "disclosure": "loose"}, "bad-disclosure")
    expect("bad disclosure -> BAD_REQUEST", r and r["class"] == "BAD_REQUEST", r)
    r = engine("connect", {"private_dir": str(env.pd)}, "no-port")
    expect("missing port -> BAD_REQUEST", r and r["class"] == "BAD_REQUEST", r)
    r = engine("connect", {"private_dir": str(env.pd), "port": 1}, "closed-port")
    expect("closed port -> ENGINE_UNAVAILABLE", r and r["ok"] is False and r["class"] == "ENGINE_UNAVAILABLE", r)

    # tmdl-validate: the fixture itself
    r = engine("tmdl-validate", {"private_dir": str(env.pd), "folder": str(MODEL_DEF)}, "tmdl-fixture")
    expect("tmdl-validate fixture: valid, no errors/missing/orphans",
           r and r["ok"] and r["valid"] is True and r["errors"] == [] and r["missing_refs"] == [] and r["orphan_files"] == [], r)

    # tmdl-validate: broken copies (outside S; deleted at the end)
    def variant(name: str, rel: str, old: str, new: str) -> Path:
        d = tmp / name
        shutil.copytree(MODEL_DEF, d)
        f = d / rel
        text = f.read_text("utf-8")
        assert old in text, (name, rel)
        f.write_text(text.replace(old, new, 1), "utf-8")
        return d

    bad, strc, ind, col = req_canary("-TMDLBAD-"), req_canary("-TMDLSTR-"), req_canary("-TMDLIND-"), req_canary("-COL-")
    d = variant("syn1", "tables/Person.tmdl", "sourceColumn: [Email]", f'{bad} === "{strc}"\n\t\tsourceColumn: [Email]')
    r = engine("tmdl-validate", {"private_dir": str(env.pd), "folder": str(d)}, "tmdl-syntax")
    e = (r or {}).get("errors") or [{}]
    expect("tmdl syntax error: SYNTAX in tables/Person.tmdl with a line number",
           r and r["valid"] is False and e[0].get("class") == "SYNTAX" and e[0].get("file") == "tables/Person.tmdl" and isinstance(e[0].get("line"), int), r)
    d = variant("syn2", "tables/Project.tmdl", "\tcolumn Title", f"   column '{ind}'")
    r = engine("tmdl-validate", {"private_dir": str(env.pd), "folder": str(d)}, "tmdl-indent")
    e = (r or {}).get("errors") or [{}]
    expect("tmdl indentation error with quoted canary line: SYNTAX, file Project", r and e[0].get("class") == "SYNTAX" and e[0].get("file") == "tables/Project.tmdl", r)
    d = variant("syn3", "tables/Hours.tmdl", "dataType: double", "dataType: nonsense")
    r = engine("tmdl-validate", {"private_dir": str(env.pd), "folder": str(d)}, "tmdl-value")
    e = (r or {}).get("errors") or [{}]
    expect("tmdl bad property value: SYNTAX, file Hours", r and e[0].get("class") == "SYNTAX" and e[0].get("file") == "tables/Hours.tmdl", r)
    d = variant("ref1", "relationships.tmdl", "toColumn: Person.Name", f"toColumn: Person.'{col}'")
    r = engine("tmdl-validate", {"private_dir": str(env.pd), "folder": str(d)}, "tmdl-reference")
    e = (r or {}).get("errors") or [{}]
    expect("tmdl broken relationship: UNKNOWN_OBJECT in relationships.tmdl",
           r and r["valid"] is False and e[0].get("class") == "UNKNOWN_OBJECT" and e[0].get("file") == "relationships.tmdl", r)
    d = variant("ghost", "model.tmdl", "ref table Notes", "ref table Notes\nref table Ghost")
    r = engine("tmdl-validate", {"private_dir": str(env.pd), "folder": str(d)}, "tmdl-missing-ref")
    expect("tmdl missing ref: missing_refs = tables/Ghost.tmdl, invalid", r and r["valid"] is False and r["missing_refs"] == ["tables/Ghost.tmdl"], r)
    d = variant("orph", "model.tmdl", "ref table Notes", "")
    r = engine("tmdl-validate", {"private_dir": str(env.pd), "folder": str(d)}, "tmdl-orphan")
    expect("tmdl orphan: orphan_files = tables/Notes.tmdl, still valid", r and r["valid"] is True and r["orphan_files"] == ["tables/Notes.tmdl"], r)
    r = engine("tmdl-validate", {"private_dir": str(env.pd), "folder": str(tmp / "nope")}, "tmdl-no-folder")
    expect("tmdl missing folder -> BAD_REQUEST", r and r["class"] == "BAD_REQUEST", r)

    # pbz/engine.py wrapper
    spec = importlib.util.spec_from_file_location("pbz_engine_wrapper", S / "pbz" / "engine.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    r = mod.call("ping", {"private_dir": str(env.pd)})
    expect("engine.py call(ping) passes the response through", r == {"ok": True, "verb": "ping", "version": "1"}, r)
    saved = mod.ENGINE_EXE
    try:
        mod.ENGINE_EXE = S / "engine" / "bin" / "missing-engine.exe"
        r = mod.call("health", {"private_dir": str(env.pd), "port": 1})
        expect("engine.py: missing exe -> ENGINE_UNAVAILABLE", r == {"ok": False, "verb": "health", "class": "ENGINE_UNAVAILABLE"}, r)
        mod.ENGINE_EXE = Path(sys.executable)
        r = mod.call("-V", {"private_dir": str(env.pd)})
        expect("engine.py: unparseable output -> ENGINE_UNAVAILABLE", r == {"ok": False, "verb": "-V", "class": "ENGINE_UNAVAILABLE"}, r)
    finally:
        mod.ENGINE_EXE = saved


def ensure_desktop(env: Env) -> bool:
    inst = find_fixture_instance()
    if inst is None:
        print("INFO fixture not open in Desktop: opening it", flush=True)
        os.startfile(str(PBIP))  # noqa: S606 (file association -> Power BI Desktop)
        env.opened_here = True
        deadline = time.time() + 300
        while time.time() < deadline and inst is None:
            time.sleep(3)
            inst = find_fixture_instance()
    if inst is None:
        expect("fixture Desktop instance found", False, {"reason": "no PBIDesktop with the fixture open"})
        return False
    env.pid, env.port = inst
    deadline = time.time() + 300
    r = None
    while time.time() < deadline:
        r = engine("connect", env.req(), "connect-wait")
        if r and r.get("ok") and set(r.get("tables", [])) == FIXTURE_TABLES:
            break
        time.sleep(3)
    ok = expect("connect: TMSCHEMA tables are exactly the fixture's (safety gate)",
                r and r.get("ok") and set(r.get("tables", [])) == FIXTURE_TABLES and int(r.get("compat_level", 0)) >= 1500, r)
    return bool(ok)


def test_live(env: Env, key_holder: dict) -> None:
    # v1.1: the engine never refreshes M partitions (Desktop refuses external mashup refreshes)
    r = engine("refresh", env.req(), "refresh-calculate")
    expect("refresh (database calculate, no data refresh) -> PASS", r and r.get("ok") and r.get("status") == "PASS", r)
    r = engine("refresh", env.req(tables=["Hours", "DimDate"]), "refresh-calculated-tables")
    expect("refresh of calculated tables (DAX recalc) -> PASS", r and r.get("ok") and r.get("status") == "PASS", r)
    r = engine("refresh", env.req(tables=["Notes"]), "refresh-m-table")
    expect("refresh of a table with an M partition -> UNSUPPORTED, not attempted", r and r.get("ok") and r.get("status") == "UNSUPPORTED", r)
    r = engine("refresh", env.req(tables=["Hours", "Notes"]), "refresh-mixed")
    expect("refresh of a list containing an M table -> UNSUPPORTED", r and r.get("ok") and r.get("status") == "UNSUPPORTED", r)
    r = engine("refresh", env.req(tables=["NoSuchTable"]), "refresh-unknown")
    expect("refresh unknown table -> UNKNOWN_OBJECT", r and r["ok"] is False and r["class"] == "UNKNOWN_OBJECT", r)

    # health
    r = engine("health", env.req(), "health")
    errs = (r or {}).get("errors", [])
    expect("health: object counts", r and r.get("objects") == {"tables": 6, "columns": 17, "measures": 4, "relationships": 3}, r)
    expect("health: exactly the planted broken measure, UNKNOWN_OBJECT, scrubbed message",
           len(errs) == 1 and errs[0].get("kind") == "measure" and errs[0].get("table") == "Hours"
           and errs[0].get("name") == "Broken Measure" and errs[0].get("class") == "UNKNOWN_OBJECT"
           and "«q»" in errs[0].get("message", ""), r)
    expect("health: only the M table may be unprocessed (calculated tables load on open)",
           r and set(r.get("unprocessed_tables", [])) <= {"Notes"}, r)

    # index
    r = engine("index", env.req(category_max=3), "index")
    cls = {(t["table"], c["column"]): c for t in (r or {}).get("tables", []) for c in t["columns"]}
    want = {("Person", "Name"): "high", ("Person", "Email"): "category", ("Project", "Title"): "high",
            ("Project", "Status"): "category", ("Hours", "Person"): "high", ("ExceptionType", "Code"): "category",
            ("ExceptionType", "Label"): "category"}
    expect("index: text column classes (category_max=3)", all(cls.get(k, {}).get("class") == v for k, v in want.items()), r)
    expect("index: non-text columns carry no class", all("class" not in cls[k] for k in cls if cls[k]["type"] != "string"), r)
    tb = {t["table"]: t for t in (r or {}).get("tables", [])}
    expect("index: row buckets", tb.get("Hours", {}).get("rows_bucket") == "10-99" and tb.get("DimDate", {}).get("rows_bucket") == "100-999", r)
    expect("index: blank share bucket and date span",
           cls.get(("Hours", "Person"), {}).get("blank_bucket") == "1-10%"
           and cls.get(("Project", "PlannedEnd"), {}).get("year_min") == 2025 and cls.get(("Project", "PlannedEnd"), {}).get("year_max") == 2031, r)
    key = dpapi_unprotect((env.pd / "key.bin").read_bytes())
    key_holder["key"] = key
    expect("key.bin: DPAPI-protected 32-byte key readable from Python", len(key) == 32)
    idx = json.loads((env.pd / "index.json").read_text("utf-8"))
    cols = idx.get("columns", {})
    name_h = set(cols.get("Person[Name]", {}).get("hmacs", []))
    expect("index.json: Person[Name] holds 4 HMACs, Python HMAC of a planted name matches",
           len(name_h) == 4 and hmac_hex(key, PERSONS[0]) in name_h and hmac_hex(key, PERSONS[3]) in name_h)
    expect("index.json: numeric/date columns have no hmacs", all("hmacs" not in v for k, v in cols.items() if v.get("type") != "string"))
    wh = set(cols.get("Person[Name]", {}).get("word_hmacs", []))
    expect("index.json v1.1: word_hmacs on name-like columns (Person[Name], Hours[Person]) only",
           wh and "word_hmacs" in cols.get("Hours[Person]", {}) and "word_hmacs" not in cols.get("Project[Title]", {})
           and "word_hmacs" not in cols.get("Person[Email]", {}))
    expect("index.json v1.1: word HMAC of a stripped planted word matches", all(hmac_hex(key, w) in wh for w in words(PERSONS[1])))
    cats = json.loads(dpapi_unprotect((env.pd / "categories.bin").read_bytes()).decode("utf-8"))
    expect("categories.bin: DPAPI JSON with category values (high-cardinality columns absent)",
           cats.get("Project[Status]") == ["Active", "Closed", "On Hold"] and len(cats.get("ExceptionType[Code]", [])) == 3
           and "Person[Name]" not in cats)
    r = engine("index", env.req(category_max=3, disclosure="strict"), "index-strict")
    flat = [c for t in (r or {}).get("tables", []) for c in t["columns"]]
    expect("index strict: no buckets or years", r and r.get("ok") and all(not ({"card_bucket", "blank_bucket", "year_min", "year_max"} & set(c)) for c in flat)
           and all("rows_bucket" not in t for t in r["tables"]), r)

    # probe
    r = engine("probe", env.req(table="Hours"), "probe-hours")
    pc = {c["column"]: c for c in (r or {}).get("columns", [])}
    expect("probe Hours: rows bucket, blanks, negatives, future dates",
           r and r.get("rows_bucket") == "10-99" and pc["Person"]["has_blanks"] is True and pc["Hours"]["has_negatives"] is True
           and pc["ProjectID"]["has_negatives"] is False and pc["Date"]["has_future_dates"] is False
           and pc["Date"].get("year_min") == 2025 and pc["Date"].get("year_max") == 2026, r)
    r = engine("probe", env.req(table="Project", columns=["PlannedEnd", "Status"], disclosure="strict"), "probe-strict")
    pc = {c["column"]: c for c in (r or {}).get("columns", [])}
    expect("probe strict: booleans only (future date planted)",
           r and pc.get("PlannedEnd", {}).get("has_future_dates") is True and "rows_bucket" not in r
           and all(not ({"blank_bucket", "card_bucket", "year_min", "year_max"} & set(c)) for c in pc.values()), r)
    r = engine("probe", env.req(table="NoSuchTable"), "probe-unknown")
    expect("probe unknown table -> UNKNOWN_OBJECT", r and r["ok"] is False and r["class"] == "UNKNOWN_OBJECT", r)

    # compiles
    items = [
        {"id": "ok", "table": "Hours", "expression": "[Total Hours] * 2"},
        {"id": "unknown-col", "table": "Hours", "expression": f"SUM(Hours[{req_canary('-COL-')}])"},
        {"id": "syntax", "table": "Hours", "expression": f"SUM(Hours[Hours] + \"{req_canary('-SYN-')}\""},
        {"id": "naked", "table": "Hours", "expression": "Hours[Hours] + 1"},
        {"id": "error-fn", "table": "Hours", "expression": f"ERROR(\"{req_canary('-ERRFN-')}\")"},
        {"id": "conversion", "table": "Hours", "expression": f"VALUE(\"{req_canary('-CONV-')}\")"},
        {"id": "unknown-func", "table": "Hours", "expression": "NOSUCHFUNCTION(1)"},
        {"id": "bad-table", "table": "NoSuchTable", "expression": "1"},
        {"id": "inject", "table": "Hours", "expression": "1\nEVALUATE Person"},
    ]
    r = engine("compiles", env.req(items=items), "compiles")
    ci = by_id((r or {}).get("items"))
    expect("compiles: valid expression PASS", ci.get("ok", {}).get("status") == "PASS", r)
    expect("compiles: unknown column -> UNKNOWN_OBJECT with scrubbed message",
           ci.get("unknown-col", {}).get("class") == "UNKNOWN_OBJECT" and "«q»" in ci["unknown-col"].get("message", ""), r)
    expect("compiles: syntax error -> SYNTAX", ci.get("syntax", {}).get("class") == "SYNTAX", r)
    expect("compiles: naked column -> ERROR (bind-time)", ci.get("naked", {}).get("status") == "ERROR", r)
    expect("compiles: ERROR() and conversion are not evaluated (PASS)",
           ci.get("error-fn", {}).get("status") == "PASS" and ci.get("conversion", {}).get("status") == "PASS", r)
    expect("compiles: unknown function / table -> UNKNOWN_OBJECT",
           ci.get("unknown-func", {}).get("class") == "UNKNOWN_OBJECT" and ci.get("bad-table", {}).get("class") == "UNKNOWN_OBJECT", r)
    expect("compiles: EVALUATE injection -> BAD_REQUEST without message",
           ci.get("inject", {}).get("class") == "BAD_REQUEST" and "message" not in ci.get("inject", {}), r)

    # check (shape tier)
    closed_pid = INT_CANARIES[3]  # a Closed project id (normalization check for numbers)
    checks = [
        {"id": "exec-ok", "kind": "executes", "expr": "[Total Hours]"},
        {"id": "exec-errfn", "kind": "executes", "expr": f"ERROR(\"{req_canary('-ERRFN-')}\")"},
        {"id": "exec-conv", "kind": "executes", "expr": f"VALUE(\"{req_canary('-CONV-')}\") + 1"},
        {"id": "exec-unk", "kind": "executes", "expr": f"SUM(Hours[{req_canary('-COL-')}])"},
        {"id": "nb-ok", "kind": "nonblank", "expr": "[Total Hours]"},
        {"id": "nb-fail", "kind": "nonblank", "expr": "CALCULATE([Total Hours], Project[Status] = \"Nope\")"},
        {"id": "nb-where", "kind": "nonblank", "expr": "[Total Hours]", "where": ["Project[Status] = \"Active\""]},
        {"id": "ne-ok", "kind": "nonempty", "expr": "VALUES(Person[Name])"},
        {"id": "ne-fail", "kind": "nonempty", "expr": "FILTER(Person, FALSE())"},
        {"id": "eq-ok", "kind": "equal", "left": "[Total Hours]", "right": "[Active Hours] + CALCULATE([Total Hours], Project[Status] <> \"Active\")"},
        {"id": "eq-status", "kind": "equal", "left": "[Total Hours]", "right": "[Active Hours]", "by": ["Project[Status]"]},
        {"id": "eq-status-q", "kind": "equal", "left": "[Total Hours]", "right": "[Active Hours]", "by": ["'Project'[Status]"]},
        {"id": "eq-person", "kind": "equal", "left": "[Total Hours]", "right": "[Canary Person Hours]", "by": ["'Person'[Name]"]},
        {"id": "eq-pid", "kind": "equal", "left": "[Total Hours]", "right": "[Active Hours]", "by": ["Hours[ProjectID]"]},
        {"id": "eq-date", "kind": "equal", "left": "[Total Hours]", "right": "[Active Hours]", "by": ["Hours[Date]"]},
        {"id": "eq-year", "kind": "equal", "left": "[Total Hours]", "right": "[Total Hours] * 1", "by": ["DimDate[Year]"]},
        {"id": "add-status", "kind": "additive", "expr": "[Total Hours]", "by": ["Project[Status]"]},
        {"id": "add-year", "kind": "additive", "expr": "[Total Hours]", "by": ["DimDate[Year]"]},
        {"id": "add-fail", "kind": "additive", "expr": "DISTINCTCOUNT(Hours[Person])", "by": ["Project[Status]"]},
        {"id": "uq-pid", "kind": "unique", "column": "Project[ProjectID]"},
        {"id": "uq-email", "kind": "unique", "column": "Person[Email]"},
        {"id": "uq-name", "kind": "unique", "column": "'Person'[Name]"},
        {"id": "orph-proj", "kind": "no_orphans", "from": "Hours[ProjectID]", "to": "Project[ProjectID]"},
        {"id": "orph-person", "kind": "no_orphans", "from": "Hours[Person]", "to": "Person[Name]"},
        {"id": "orph-date", "kind": "no_orphans", "from": "Hours[Date]", "to": "DimDate[Date]"},
        {"id": "nbk-person", "kind": "no_blank_keys", "column": "Hours[Person]"},
        {"id": "nbk-pid", "kind": "no_blank_keys", "column": "Hours[ProjectID]"},
        {"id": "nbk-dim", "kind": "no_blank_keys", "column": "Project[ProjectID]"},
        {"id": "sign-pos", "kind": "sign", "expr": "[Total Hours]"},
        {"id": "sign-neg", "kind": "sign", "expr": "CALCULATE(SUM(Hours[Hours]), Hours[Hours] < 0)", "expect": ["NEG"]},
        {"id": "sign-blank", "kind": "sign", "expr": "CALCULATE([Total Hours], Project[Status] = \"Nope\")"},
        {"id": "sign-zero", "kind": "sign", "expr": "[Total Hours] - [Total Hours]", "expect": "POS"},
        {"id": "sub-pid", "kind": "subset", "column": "Hours[ProjectID]", "of": "Project[ProjectID]"},
        {"id": "sub-person", "kind": "subset", "column": "Hours[Person]", "of": "Person[Name]"},
        {"id": "unsupported", "kind": "between", "expr": "[Total Hours]"},
        {"id": "inject", "kind": "executes", "expr": "1)\nEVALUATE Person\n//"},
        {"id": "bad-col", "kind": "unique", "column": "Person[NoSuchColumn]"},
        {"id": "info-fn", "kind": "nonblank", "expr": "COUNTROWS(INFO.STORAGETABLES())"},
    ]
    r = engine("check", env.req(reveal=["Project[Status]", "DimDate[Year]"], checks=checks), "check")
    c = by_id((r or {}).get("checks"))
    st = {k: v.get("status") for k, v in c.items()}
    expect("check executes: PASS / ERROR OTHER (ERROR()) / CONVERSION / UNKNOWN_OBJECT, class only",
           st.get("exec-ok") == "PASS" and c.get("exec-errfn", {}).get("class") == "OTHER"
           and c.get("exec-conv", {}).get("class") == "CONVERSION" and c.get("exec-unk", {}).get("class") == "UNKNOWN_OBJECT"
           and all("message" not in c.get(k, {}) for k in ("exec-errfn", "exec-conv", "exec-unk")), r)
    env.log_ids["errfn"] = c.get("exec-errfn", {}).get("log_id", "")
    expect("check nonblank / nonempty", (st.get("nb-ok"), st.get("nb-fail"), st.get("nb-where"), st.get("ne-ok"), st.get("ne-fail"))
           == ("PASS", "FAIL", "PASS", "PASS", "FAIL"), r)
    expect("check equal ungrouped identity PASS", st.get("eq-ok") == "PASS", r)
    g = c.get("eq-status", {})
    expect("check equal by revealed category: FAIL FEW, groups Closed/On Hold/blank",
           g.get("status") == "FAIL" and g.get("mismatch") == "FEW"
           and sorted(json.dumps(x, sort_keys=True) for x in g.get("groups", [])) == sorted(json.dumps(x, sort_keys=True) for x in [
               {"Project[Status]": {"reveal": "Closed"}}, {"Project[Status]": {"reveal": "On Hold"}}, {"Project[Status]": {"blank": True}}]), r)
    gq = c.get("eq-status-q", {})
    expect("check v1.1: group keyed by the exact by-string, not revealed unless that exact string is in reveal",
           gq.get("mismatch") == "FEW" and all(set(x) == {"'Project'[Status]"} for x in gq.get("groups", []))
           and not any("reveal" in v for x in gq.get("groups", []) for v in x.values()), r)
    key = key_holder["key"]
    gp = c.get("eq-person", {})
    hm = {v.get("hmac") for x in gp.get("groups", []) for v in x.values()}
    expect("check equal by hidden text key: FAIL FEW, HMAC keys match Python HMAC, blank key flagged",
           gp.get("mismatch") == "FEW" and hmac_hex(key, PERSONS[1]) in hm and hmac_hex(key, PERSONS[0]) not in hm
           and any(v.get("blank") for x in gp.get("groups", []) for v in x.values()), r)
    hm = {v.get("hmac") for x in c.get("eq-pid", {}).get("groups", []) for v in x.values()}
    expect("check equal by hidden integer key: Python number normalization matches", hmac_hex(key, closed_pid) in hm, r)
    hm = {v.get("hmac") for x in c.get("eq-date", {}).get("groups", []) for v in x.values()}
    expect("check equal by hidden date key: Python date normalization matches", hmac_hex(key, datetime(2025, 7, 2)) in hm, r)
    expect("check equal by year PASS with mismatch NONE", st.get("eq-year") == "PASS" and c.get("eq-year", {}).get("mismatch") == "NONE", r)
    expect("check additive PASS, PASS, FAIL", (st.get("add-status"), st.get("add-year"), st.get("add-fail")) == ("PASS", "PASS", "FAIL"), r)
    expect("check unique: ids PASS, planted duplicate email FAIL, names PASS (+ offenders buckets)",
           (st.get("uq-pid"), st.get("uq-email"), st.get("uq-name")) == ("PASS", "FAIL", "PASS")
           and c.get("uq-email", {}).get("offenders_bucket") == "1-9" and c.get("uq-pid", {}).get("offenders_bucket") == "0", r)
    expect("check no_orphans: planted orphans FAIL; blank FK ignored; dates PASS",
           (st.get("orph-proj"), st.get("orph-person"), st.get("orph-date")) == ("FAIL", "PASS", "PASS"), r)
    expect("check no_blank_keys: planted blank FAIL; ids PASS; dimension blank row not counted",
           (st.get("nbk-person"), st.get("nbk-pid"), st.get("nbk-dim")) == ("FAIL", "PASS", "PASS"), r)
    expect("check sign: POS / NEG / BLANK / ZERO vs expect POS -> FAIL",
           (c.get("sign-pos", {}).get("sign"), c.get("sign-neg", {}).get("sign"), c.get("sign-blank", {}).get("sign"), c.get("sign-zero", {}).get("sign"))
           == ("POS", "NEG", "BLANK", "ZERO") and st.get("sign-neg") == "PASS" and st.get("sign-zero") == "FAIL", r)
    expect("check subset FAIL / PASS", (st.get("sub-pid"), st.get("sub-person")) == ("FAIL", "PASS"), r)
    expect("check unknown kind UNSUPPORTED, injection BAD_REQUEST, unknown column UNKNOWN_OBJECT, INFO.* refused",
           st.get("unsupported") == "UNSUPPORTED" and c.get("inject", {}).get("class") == "BAD_REQUEST"
           and c.get("bad-col", {}).get("class") == "UNKNOWN_OBJECT" and c.get("info-fn", {}).get("class") == "BAD_REQUEST", r)

    r = engine("check", env.req(disclosure="strict", reveal=["Project[Status]"], checks=[
        {"id": "eq-status", "kind": "equal", "left": "[Total Hours]", "right": "[Active Hours]", "by": ["Project[Status]"]},
        {"id": "uq-email", "kind": "unique", "column": "Person[Email]"}]), "check-strict")
    c = by_id((r or {}).get("checks"))
    expect("check strict: mismatch kept, no groups, no offenders bucket",
           c.get("eq-status", {}).get("mismatch") == "FEW" and "groups" not in c.get("eq-status", {})
           and c.get("uq-email", {}).get("status") == "FAIL" and "offenders_bucket" not in c.get("uq-email", {}), r)

    # snapshots
    q1 = {"id": "q1", "dax": "EVALUATE SUMMARIZECOLUMNS(DimDate[Year], \"m\", [Total Hours])", "keys": ["DimDate[Year]"]}
    q1b = {"id": "q1", "dax": "EVALUATE SUMMARIZECOLUMNS(DimDate[Year], \"m\", [Active Hours])", "keys": ["DimDate[Year]"]}
    q2 = {"id": "q2", "dax": "EVALUATE ROW(\"t\", [Total Hours])"}
    q3 = {"id": "q3", "dax": "EVALUATE SUMMARIZECOLUMNS(Project[Status], \"m\", [Total Hours], \"a\", [Active Hours])", "keys": ["Project[Status]"]}
    q3b = {"id": "q3", "dax": "EVALUATE SUMMARIZECOLUMNS(Project[Status], \"m\", [Total Hours], \"a\", [Total Hours])", "keys": ["Project[Status]"]}
    qerr = {"id": "qerr", "dax": f"EVALUATE ROW(\"e\", ERROR(\"{req_canary('-SNAPERR-')}\"))"}
    r = engine("snapshot-save", env.req(name="t1", queries=[q1, q2, q3, qerr]), "snapshot-save")
    s = {q["id"]: q for q in (r or {}).get("queries", [])}
    expect("snapshot-save: SAVED x3, failing query ERROR with class only",
           [s.get(k, {}).get("result") for k in ("q1", "q2", "q3")] == ["SAVED"] * 3 and s.get("qerr", {}).get("result") == "ERROR"
           and s.get("qerr", {}).get("class") == "OTHER", r)
    snap = json.loads((env.pd / "snapshots" / "t1.json").read_text("utf-8"))
    expect("snapshot file: HMAC rows + refreshed stamp, no raw cells",
           all(re.fullmatch(r"[0-9a-f]{64}", k) for k in snap["queries"]["q1"]["rows"]) and "refreshed" in snap
           and all(re.fullmatch(r"[0-9a-f]{64}", k) for k in snap["queries"]["q2"]["multiset"]))
    r = engine("snapshot-compare", env.req(name="t1", queries=[q1, q2, q3]), "snapshot-same")
    expect("snapshot-compare unchanged: SAME x3", [q.get("result") for q in (r or {}).get("queries", [])] == ["SAME"] * 3, r)
    r = engine("snapshot-compare", env.req(name="t1", queries=[q1b, q3b, {"id": "q9", "dax": "EVALUATE ROW(\"x\", 1)"}]), "snapshot-diff")
    s = {q["id"]: q for q in (r or {}).get("queries", [])}
    expect("snapshot-compare changed: DIFF(columns, mismatch), NEW, MISSING",
           s.get("q1") == {"id": "q1", "result": "DIFF", "columns": ["m"], "mismatch": "FEW"}
           and s.get("q3") == {"id": "q3", "result": "DIFF", "columns": ["a"], "mismatch": "FEW"}
           and s.get("q9", {}).get("result") == "NEW" and s.get("q2", {}).get("result") == "MISSING", r)
    r = engine("snapshot-compare", env.req(name="never-saved", queries=[q1]), "snapshot-new")
    expect("snapshot-compare without baseline: NEW", [q.get("result") for q in (r or {}).get("queries", [])] == ["NEW"], r)
    r = engine("snapshot-save", env.req(name="../escape", queries=[q1]), "snapshot-bad-name")
    expect("snapshot bad name -> BAD_REQUEST", r and r["ok"] is False and r["class"] == "BAD_REQUEST", r)
    r = engine("snapshot-save", env.req(name="t2", queries=[{"id": "tmsl", "dax": "{\"refresh\": {\"type\": \"full\", \"objects\": []}}"}]), "snapshot-tmsl")
    expect("snapshot query that is not DAX (TMSL) -> BAD_REQUEST", r and r["ok"] is False and r["class"] == "BAD_REQUEST", r)
    engine("refresh", env.req(tables=["DimDate"]), "refresh-for-stale")
    r = engine("snapshot-compare", env.req(name="t1", queries=[q1, q1b | {"id": "q1b"}]), "snapshot-stale")
    s = {q["id"]: q for q in (r or {}).get("queries", [])}
    expect("snapshot-compare after refresh: unchanged query stays SAME, unknown id is NEW",
           s.get("q1", {}).get("result") == "SAME" and s.get("q1b", {}).get("result") == "NEW", r)
    r = engine("snapshot-compare", env.req(name="t1", queries=[q1b]), "snapshot-stale-diff")
    s = {q["id"]: q for q in (r or {}).get("queries", [])}
    expect("snapshot-compare after refresh with a changed query: STALE_DATA", s.get("q1", {}).get("result") == "STALE_DATA", r)

    # smoke
    r = engine("smoke", env.req(visuals=[
        {"id": "page/card", "dax": "EVALUATE TOPN(1, SUMMARIZECOLUMNS(\"Total Hours\", [Total Hours]))"},
        {"id": "page/empty", "dax": "EVALUATE TOPN(1, FILTER(VALUES(Person[Name]), FALSE()))"},
        {"id": "page/error", "dax": f"EVALUATE ROW(\"e\", ERROR(\"{req_canary('-SMOKE-')}\"))"},
        {"id": "page/dmv", "dax": "SELECT * FROM $SYSTEM.DISCOVER_STORAGE_TABLES"},
        {"id": "page/unsupported"}]), "smoke")
    v = by_id((r or {}).get("visuals"))
    expect("smoke: OK / EMPTY / ERROR(class) / UNSUPPORTED; storage DMV text refused",
           (v.get("page/card", {}).get("status"), v.get("page/empty", {}).get("status"), v.get("page/error", {}).get("status"),
            v.get("page/unsupported", {}).get("status")) == ("OK", "EMPTY", "ERROR", "UNSUPPORTED")
           and v["page/error"].get("class") == "OTHER" and v.get("page/dmv", {}).get("class") == "BAD_REQUEST", r)

    # the raw text of a forced error exists only inside the DPAPI log
    log_id = env.log_ids.get("errfn", "")
    found = False
    for line in (env.pd / "logs" / "engine.log").read_text("utf-8").splitlines():
        lid, _, payload = line.partition("\t")
        if lid == log_id:
            found = req_canary("-ERRFN-") in dpapi_unprotect(base64.b64decode(payload)).decode("utf-8")
    expect("engine.log: forced ERROR() text is present only inside the DPAPI payload", found)


def test_norm_reference() -> None:
    """Python number normalization vs strings produced by the .NET 10 runtime (G12 then "R")."""
    golden = [(1e15, "1000000000000000"), (123456789012345.0, "123456789012000"), (1e-5, "1E-05"), (0.0001, "0.0001"),
              (1.5e-05, "1.5E-05"), (0.1 + 0.2, "0.3"), (1.0 / 3, "0.333333333333"), (-0.0, "0"),
              (12345678901234567.0, "12345678901200000"), (2.5e20, "2.5E+20"), (100.0, "100"), (1e14, "100000000000000"),
              (999999999999.5, "1000000000000"), (0.000123456789012345, "0.000123456789012"), (1e16, "10000000000000000"),
              (1e17, "1E+17"), (1.5e17, "1.5E+17"), (9.99e16, "99900000000000000"), (1.2345e18, "1.2345E+18"),
              (123456789012e6, "1.23456789012E+17"), (5e-4, "0.0005"), (5e-5, "5E-05")]
    bad = [i for i, (v, want) in enumerate(golden) if norm_number(v) != want]
    expect("python number normalization matches .NET G12 + R golden strings", not bad, {"index": bad})
    expect("python text/date/blank/bool normalization",
           norm_value("  ÉCOLE ") == "école" and norm_value(datetime(2025, 7, 2)) == "2025-07-02T00:00:00"
           and norm_value(None) == BLANK and norm_value(True) == "true" and norm_value(918) == "918")


def test_fixture_sanity() -> None:
    sources = [p for p in FIXTURE.rglob("*") if p.is_file() and p.suffix.lower() in (".tmdl", ".json", ".pbism", ".pbir", ".pbip") and ".pbi" not in p.parts]
    text = "\n".join(p.read_text("utf-8", "replace") for p in sources)
    missing = [i for i, s in enumerate(CAN["canary-strings"]) if s not in text]
    expect("sanity: every planted canary string exists in the fixture sources", not missing, {"missing_index": missing})
    missing = [i for i, n in enumerate(CAN["canary-numbers"]) if not re.search(r"(?<![0-9.])" + re.escape(n) + r"(?![0-9])", text)]
    expect("sanity: every planted canary number exists in the fixture sources", not missing, {"missing_index": missing})
    notes = (MODEL_DEF / "tables" / "Notes.tmdl").read_text("utf-8")
    blob = re.search(r'Binary\.FromText\("([^"]+)"', notes).group(1)
    rows = json.loads(zlib.decompress(base64.b64decode(blob), -15).decode("utf-8"))
    expect("sanity: Enter Data blob decodes to the blob canaries", sorted(r[0] for r in rows) == sorted(CAN["canary-blob-strings"])
           and not any(s in text for s in CAN["canary-blob-strings"]))
    expect("sanity: test code holds no canary literal", canary_hits(Path(__file__).read_text("utf-8"), FILE_PATTERNS) == 0)


def scan_all(env: Env) -> None:
    hits = [label for label, out, err in OUTPUTS if canary_hits(out.decode("utf-8", "replace"), OUT_PATTERNS) or err]
    expect(f"canary scan: {len(OUTPUTS)} engine stdout/stderr captures are clean", not hits, {"labels": hits[:10]})

    def file_texts(p: Path) -> list[str]:
        b = p.read_bytes()
        return [b.decode("latin-1"), b.decode("utf-16-le", "ignore")]

    bad = []
    count = 0
    fixture_root = os.path.normcase(str(FIXTURE))
    for root, _dirs, files in os.walk(S):
        if os.path.normcase(root).startswith(fixture_root):
            continue
        for f in files:
            p = Path(root) / f
            count += 1
            try:
                if any(canary_hits(t, FILE_PATTERNS) for t in file_texts(p)):
                    bad.append(str(p.relative_to(S)))
            except OSError:
                pass
    expect(f"canary scan: {count} files under the skill folder (fixture sources excluded) are clean", not bad, {"files": bad[:10]})

    bad = []
    blobs = 0
    for p in env.pd.rglob("*"):
        if not p.is_file():
            continue
        if p.name in ("key.bin", "categories.bin"):
            dpapi_unprotect(p.read_bytes())  # must be a DPAPI blob
            blobs += 1
            continue
        if p.name == "engine.log":
            for line in p.read_text("utf-8").splitlines():
                if not re.fullmatch(r"e-\d{8}-\d{4,}\t[A-Za-z0-9+/=]+", line):
                    bad.append("engine.log format")
                    break
                dpapi_unprotect(base64.b64decode(line.split("\t", 1)[1]))
            blobs += 1
            continue
        if any(canary_hits(t, OUT_PATTERNS) for t in file_texts(p)):
            bad.append(str(p.relative_to(env.pd)))
    expect(f"canary scan: private_dir clean outside {blobs} DPAPI blobs", not bad, {"files": bad[:10]})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="skip verbs that need Power BI Desktop")
    ap.add_argument("--close", action="store_true", help="close the fixture's Desktop window at the end")
    ap.add_argument("--keep", action="store_true", help="keep the temporary private_dir")
    args = ap.parse_args()

    if not EXE.is_file():
        print("FAIL engine not built: run dotnet publish (see engine/README.md)")
        return 2
    base = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "pbz"
    base.mkdir(parents=True, exist_ok=True)
    env = Env()
    env.pd = Path(tempfile.mkdtemp(prefix="test-engine-", dir=base))
    tmp = Path(tempfile.mkdtemp(prefix="pbz-tmdl-"))
    key_holder: dict = {}
    try:
        test_fixture_sanity()
        test_norm_reference()
        test_offline(env, tmp)
        if not args.offline and ensure_desktop(env):
            test_live(env, key_holder)
        shutil.rmtree(tmp, ignore_errors=True)
        scan_all(env)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if not args.keep:
            shutil.rmtree(env.pd, ignore_errors=True)
        if env.pid and (args.close or env.opened_here):
            print("INFO closing fixture Desktop:", close_desktop(env.pid), flush=True)
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
