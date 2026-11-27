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


from canary_scan import CAN, scan, scan_patterns

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


def _dpapi_libraries():
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.POINTER(DATA_BLOB),
                                            ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(DATA_BLOB)]
    _crypt32.CryptUnprotectData.restype = wt.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    return _crypt32, _kernel32


CRYPTPROTECT_UI_FORBIDDEN = 0x1


def dpapi_unprotect(blob: bytes) -> bytes:
    _crypt32, _kernel32 = _dpapi_libraries()
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


def _tree_manifest(root: Path) -> dict[str, str]:
    """Fingerprint every file in a source tree, including existing build outputs."""
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def _manifest_changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    changes = []
    for path in sorted(before.keys() | after.keys()):
        if path not in before:
            changes.append(f"added: {path}")
        elif path not in after:
            changes.append(f"removed: {path}")
        elif before[path] != after[path]:
            changes.append(f"content-changed: {path}")
    return sorted(changes)


def test_grouped_streaming(tmp: Path) -> None:
    """Offline .NET acceptance: execute the production aggregators with a lazy row source.

    Requires the Windows .NET SDK; no Desktop connection or pre-materialized fixture rows.
    """
    import xml.etree.ElementTree as ET
    helper = tmp / "grouped-streaming"
    helper.mkdir()
    artifacts = tmp / "grouped-streaming-artifacts"
    project = ET.Element("Project", Sdk="Microsoft.NET.Sdk")
    properties = ET.SubElement(project, "PropertyGroup")
    for key, value in {"OutputType": "Exe", "TargetFramework": "net10.0",
                       "RuntimeIdentifier": "win-x64", "ImplicitUsings": "enable", "Nullable": "enable"}.items():
        ET.SubElement(properties, key).text = value
    refs = ET.SubElement(project, "ItemGroup")
    ET.SubElement(refs, "ProjectReference", Include=str(S / "engine" / "PbzEngine.csproj"))
    ET.ElementTree(project).write(helper / "Grouped.csproj", encoding="unicode")
    (helper / "Program.cs").write_text(r'''
using System.Text.Json.Nodes;
using PbzEngine;
using System.Reflection;
static string Normalize(string query) => (string)typeof(Perf).GetMethod("NormalizeQuery", BindingFlags.NonPublic | BindingFlags.Static)!.Invoke(null, new object[] { query })!;
static void Assert(bool ok, string label) { if (!ok) throw new Exception(label); }
static JsonObject Result(Checks.EqualAggregate a) { var r = new JsonObject(); a.Write(r); return r; }
static IEnumerable<object?[]> Rows(int count, int bad) {
    // All failures arrive after the passing prefix, smallest serialized keys last.
    for (int i = 0; i < count; i++)
        yield return new object?[] { "g" + (count - i - 1).ToString("D8"), 1, i >= count - bad ? 0 : 1 };
}
// Exercise the production normalizer, including literal and comment extents.
foreach (var pair in new[] {
    ("EVALUATE {1+2}", "EVALUATE {1 + 2}", true),
    ("EVALUATE {1=1||2=3}", "EVALUATE {1=1 || 2=3}", true),
    ("EVALUATE {1 -- note\n+2}", "EVALUATE {1 -- note +2}", false),
    ("EVALUATE {1 // note\n+2}", "EVALUATE {1 // note +2}", false),
    ("EVALUATE {1 /* note */+2}", "EVALUATE {1 /* note */ + 2}", true),
    ("EVALUATE {1 /* note +2 */}", "EVALUATE {1 /* note */+2}", false),
    ("EVALUATE {\"a b\"}", "EVALUATE {\"ab\"}", false),
    ("EVALUATE {'a b'[x]}", "EVALUATE {'ab'[x]}", false)
}) Assert((Normalize(pair.Item1) == Normalize(pair.Item2)) == pair.Item3, "query normalization: " + pair.Item1);
// PR 4a: exact int64 and tol == 0 assertions on Checks.ValuesEqual
Assert(!Checks.ValuesEqual(9007199254740993L, 9007199254740992L, 0.0), "exact int64 tol=0 inequality");
Assert(Checks.ValuesEqual(9007199254740993L, 9007199254740993L, 0.0), "exact int64 tol=0 identity");
Assert(!Checks.ValuesEqual(long.MaxValue, long.MaxValue - 1, 0.0), "int64 max tol=0 inequality");
Assert(Checks.ValuesEqual(123, 123L, 0.0), "int vs long tol=0 identity");
Assert(!Checks.ValuesEqual(123, 124L, 0.0), "int vs long tol=0 inequality");
Assert(Checks.ValuesEqual(1L, 1.0, 0.0), "int vs float tol=0 identity");
Assert(!Checks.ValuesEqual(1L, 1.5, 0.0), "int vs float tol=0 fractional inequality");
Assert(!Checks.ValuesEqual(9007199254740993L, 9007199254740992.0, 0.0), "int vs float tol=0 precision inequality");
Assert(!Checks.ValuesEqual(9007199254740992.0, 9007199254740993L, 0.0), "float vs int tol=0 precision inequality");
Assert(Checks.ValuesEqual(9007199254740992L, 9007199254740992.0, 0.0), "int vs float tol=0 2^53 exact identity");
Assert(Checks.ValuesEqual(9007199254740992.0, 9007199254740992L, 0.0), "float vs int tol=0 2^53 exact identity");
Assert(!Checks.ValuesEqual(long.MaxValue, 9223372036854775808.0, 0.0), "long.MaxValue vs 2^63 double inequality");
Assert(!Checks.ValuesEqual(9223372036854775808.0, long.MaxValue, 0.0), "2^63 double vs long.MaxValue inequality");
Assert(Checks.ValuesEqual(long.MinValue, -9223372036854775808.0, 0.0), "long.MinValue vs -2^63 double identity");
Assert(Checks.ValuesEqual(-9223372036854775808.0, long.MinValue, 0.0), "-2^63 double vs long.MinValue identity");
Assert(Checks.ValuesEqual(9223372036854775808UL, 9223372036854775808.0, 0.0), "ulong 2^63 vs double identity");
Assert(Checks.ValuesEqual(9223372036854775808.0, 9223372036854775808UL, 0.0), "double vs ulong 2^63 identity");
Assert(!Checks.ValuesEqual(ulong.MaxValue, 18446744073709551616.0, 0.0), "ulong.MaxValue vs 2^64 double inequality");
Assert(!Checks.ValuesEqual(18446744073709551616.0, ulong.MaxValue, 0.0), "2^64 double vs ulong.MaxValue inequality");
Assert(Checks.ValuesEqual(ulong.MaxValue, ulong.MaxValue, 0.0), "ulong.MaxValue identity");
Assert(!Checks.ValuesEqual(ulong.MaxValue, ulong.MaxValue - 1, 0.0), "ulong.MaxValue inequality");
Assert(!Checks.ValuesEqual(double.PositiveInfinity, 1L, 0.0), "infinity vs long inequality");
Assert(!Checks.ValuesEqual(1L, double.PositiveInfinity, 0.0), "long vs infinity inequality");
Assert(!Checks.ValuesEqual(double.NaN, 1L, 0.0), "NaN vs long inequality");
Assert(!Checks.ValuesEqual(1L, double.NaN, 0.0), "long vs NaN inequality");
Assert(Checks.ValuesEqual(123.45m, 123.45m, 0.0), "decimal vs decimal identity");
Assert(!Checks.ValuesEqual(123.45m, 123.46m, 0.0), "decimal vs decimal inequality");
Assert(Checks.ValuesEqual(9007199254740993L, 9007199254740993m, 0.0), "long vs decimal 2^53 identity");
Assert(Checks.ValuesEqual(9007199254740993m, 9007199254740993L, 0.0), "decimal vs long 2^53 identity");
Assert(!Checks.ValuesEqual(9007199254740992L, 9007199254740993m, 0.0), "long vs decimal 2^53 inequality");
Assert(!Checks.ValuesEqual(9007199254740993m, 9007199254740992L, 0.0), "decimal vs long 2^53 inequality");
Assert(!Checks.ValuesEqual(9007199254740994L, 9007199254740993m, 0.0), "long vs decimal 2^53+1 inequality");
Assert(!Checks.ValuesEqual(9007199254740993m, 9007199254740994L, 0.0), "decimal vs long 2^53+1 inequality");
Assert(!Checks.ValuesEqual(9007199254740993m, 9007199254740992m, 0.0), "decimal vs decimal 2^53 inequality");
Assert(Checks.ValuesEqual(9007199254740993m, 9007199254740993m, 0.0), "decimal vs decimal 2^53 identity");
Assert(Checks.ValuesEqual(9007199254740993m, 9007199254740992.0, 0.0), "decimal vs double double-precision fallback identity");
Assert(Checks.ValuesEqual(9007199254740992.0, 9007199254740993m, 0.0), "double vs decimal double-precision fallback identity");
Assert(!Checks.ValuesEqual(1L, 1.000000000000000000001m, 0.0), "long vs decimal fractional inequality");
Assert(!Checks.ValuesEqual(1.000000000000000000001m, 1L, 0.0), "decimal vs long fractional inequality");
Assert(Checks.ValuesEqual(0.0, -0.0, 0.0), "signed zero float identity");
Assert(Checks.ValuesEqual(0L, -0.0, 0.0), "signed zero int vs float identity");
Assert(!Checks.ValuesEqual(double.NaN, double.NaN, 0.0), "NaN vs NaN inequality");
Assert(!Checks.ValuesEqual(double.NaN, 0.0, 0.0), "NaN vs zero inequality");
Assert(Checks.ValuesEqual(9007199254740993L, 9007199254740992L, 1e-9), "tol > 0 preserved for int64");
Assert(Checks.ValuesEqual(100.0, 100.01, 0.01), "tol > 0 preserved for float");
Assert(!Checks.ValuesEqual(100.0, 100.01, 0.00001), "tol > 0 tight tolerance");
// PR 4b: Checks.NoBlankKeysDax wraps blank predicate in KEEPFILTERS
string nbkDax = Checks.NoBlankKeysDax("'Hours'", "Hours[Person] = \"\"", new[] { "Hours[Hours] > 0" });
Assert(nbkDax.Contains("KEEPFILTERS(Hours[Person] = \"\")"), "no_blank_keys wraps blank predicate in KEEPFILTERS with where");
Assert(nbkDax == "EVALUATE ROW(\"n\", CALCULATE(COUNTROWS('Hours'), Hours[Hours] > 0), \"o\", CALCULATE(CALCULATE(COUNTROWS('Hours'), KEEPFILTERS(Hours[Person] = \"\")), Hours[Hours] > 0))", "no_blank_keys exact DAX with where");
string nbkDaxNoWhere = Checks.NoBlankKeysDax("'Hours'", "Hours[Person] = \"\"", Array.Empty<string>());
Assert(nbkDaxNoWhere == "EVALUATE ROW(\"n\", COUNTROWS('Hours'), \"o\", CALCULATE(COUNTROWS('Hours'), KEEPFILTERS(Hours[Person] = \"\")))", "no_blank_keys exact DAX without where");
string nbkIsBlank = Checks.NoBlankKeysDax("'Hours'", "ISBLANK(Hours[ProjectID])", new[] { "Hours[Hours] > 0" });
Assert(nbkIsBlank.Contains("KEEPFILTERS(ISBLANK(Hours[ProjectID]))"), "no_blank_keys wraps ISBLANK in KEEPFILTERS with where");
string nbkSameCol = Checks.NoBlankKeysDax("'Hours'", "Hours[Person] = \"\"", new[] { "Hours[Person] <> \"\"" });
Assert(nbkSameCol == "EVALUATE ROW(\"n\", CALCULATE(COUNTROWS('Hours'), Hours[Person] <> \"\"), \"o\", CALCULATE(CALCULATE(COUNTROWS('Hours'), KEEPFILTERS(Hours[Person] = \"\")), Hours[Person] <> \"\"))", "no_blank_keys exact DAX with same-column where");
Assert(Checks.SignStatus(null, "POS") == "PASS", "omitted sign expect passes");
Assert(Checks.SignStatus(new List<string>(), "POS") == "FAIL", "empty sign expect fails");
Assert(Checks.SignStatus(new List<string> { "NEG" }, "POS") == "FAIL", "unlisted sign expect fails");
Assert(Checks.SignStatus(new List<string> { "POS", "ZERO" }, "POS") == "PASS", "listed sign expect passes");
string directory = Path.Combine(Path.GetTempPath(), "pbz-grouped-" + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(directory);
try {
    var vault = Vault.Open(directory);
    var table = new TableMeta { Name = "T" };
    var columns = new List<ColumnMeta> { new() { Table = table, Name = "Key", DataType = 2 } };
    var by = new List<string> { "T[Key]" };
    var reveal = new HashSet<string> { "T[Key]" };
    var allow = new HashSet<string> { "T[Key]" };
    int loads = 0;
    var snapshot = new Checks.RequestSnapshot(() => { loads++; return null; });
    Func<object?[], JsonObject> Factory() => Checks.GroupFactory(snapshot, by, columns, reveal, allow, vault);
    Assert(loads == 0, "fresh holder has not loaded");
    var firstFactory = Factory();
    Assert(loads == 1, "first factory build loads snapshot");
    var secondFactory = Factory();
    Assert(loads == 1, "second factory build reuses snapshot");
    firstFactory(new object?[] { "first" });
    secondFactory(new object?[] { "second" });
    Assert(loads == 1, "one production holder load across two factories and group calls");
    snapshot.Invalidate();
    firstFactory(new object?[] { "after invalidation" });
    secondFactory(new object?[] { "after invalidation" });
    Assert(loads == 1, "pre-existing factories leave invalidated holder alone");
    var newFactory = Factory();
    Assert(loads == 2, "new factory build reloads invalidated snapshot");
    newFactory(new object?[] { "new snapshot" });
    Assert(loads == 2, "new factory group call does not reload");
    foreach (var test in new[] { (0, 0, "EMPTY", "NONE"), (23, 0, "PASS", "NONE"),
                                 (23, 1, "FAIL", "ONE"), (23, 4, "FAIL", "FEW"),
                                 (23, 20, "FAIL", "MANY"), (1000000, 20, "FAIL", "MANY") }) {
        var a = new Checks.EqualAggregate(1, 1e-9, Factory());
        foreach (var row in Rows(test.Item1, test.Item2)) a.Accept(row);
        var result = Result(a);
        Assert((string?)result["status"] == test.Item3 && (string?)result["mismatch"] == test.Item4, "expected verdict/bucket");
        Assert(a.RowsConsumed == test.Item1 && a.Failures == test.Item2, "actual consumed and failures");
        Assert(a.RetainedGroups == Math.Min(10, test.Item2) && a.PeakRetainedGroups <= 10, "bounded retained objects");
        if (test.Item2 == 0) Assert(result["groups"] == null, "no groups on pass/empty");
        else {
            var expected = new JsonArray();
            for (int i = 0; i < Math.Min(10, test.Item2); i++)
                expected.Add(new JsonObject { ["T[Key]"] = new JsonObject { ["reveal"] = "g" + i.ToString("D8") } });
            Assert(JsonNode.DeepEquals(result["groups"], expected), "independently expected exact sample");
        }
        Assert(loads == 2, "no further request snapshot loads across checks");
    }
    // Shuffling supplements independently expected results; duplicates must survive sampling.
    var input = Rows(23, 20).ToArray();
    var random = new Random(7);
    for (int iteration = 0; iteration < 4; iteration++) {
        random.Shuffle(input);
        var a = new Checks.EqualAggregate(1, 1e-9, Factory());
        foreach (var row in input) a.Accept(row);
        var expected = new JsonArray();
        for (int i = 0; i < 10; i++) expected.Add(new JsonObject { ["T[Key]"] = new JsonObject { ["reveal"] = "g" + i.ToString("D8") } });
        Assert((string?)Result(a)["status"] == "FAIL" && JsonNode.DeepEquals(Result(a)["groups"], expected), "shuffled exact sample");
    }
    var duplicate = new Checks.EqualAggregate(1, 1e-9, Factory());
    for (int i = 0; i < 15; i++) duplicate.Accept(new object?[] { "same", 1, 0 });
    Assert(((JsonArray)Result(duplicate)["groups"]!).Count == 10, "duplicate groups retained");
    var strict = new Checks.EqualAggregate(1, 1e-9, null);
    foreach (var row in Rows(1000000, 20)) strict.Accept(row);
    Assert(Result(strict).ToJsonString() == "{\"status\":\"FAIL\"}" && strict.RowsConsumed == 1000000 && strict.RetainedGroups == 0, "strict traverses without groups");
    // Permissions are snapshots per column, with exact category membership.
    var index = JsonNode.Parse("{\"columns\":{\"T[Key]\":{\"class\":\"category\"}},\"categories\":{\"T[Key]\":[\"known\"]}}")!.AsObject();
    int categoryLoads = 0;
    var categorySnapshot = new Checks.RequestSnapshot(() => { categoryLoads++; return index; });
    var category = Checks.GroupFactory(categorySnapshot, by, columns, reveal, new(), vault);
    Assert((string?)category(new object?[] { "known" })["T[Key]"]?["reveal"] == "known", "member reveals");
    index["categories"]!["T[Key]"] = new JsonArray("new");
    Assert(category(new object?[] { "new" })["T[Key]"]?["hmac"] != null, "new value hidden, cached permissions");
    var categorySecond = Checks.GroupFactory(categorySnapshot, by, columns, reveal, new(), vault);
    Assert(categorySecond(new object?[] { "new" })["T[Key]"]?["hmac"] != null, "permissions cached across factories");
    Assert(categoryLoads == 1, "one index load across category factories");
    var alias = Checks.GroupFactory(categorySnapshot, new() { "'T'[Key]" }, columns, reveal, new(), vault);
    Assert(alias(new object?[] { "known" })["'T'[Key]"]?["hmac"] != null, "cached column still requires exact reveal spelling");
    Assert(category(new object?[] { null })["T[Key]"]?["blank"]?.GetValue<bool>() == true, "blank group");
    categorySnapshot.Invalidate();
    var reloaded = Checks.GroupFactory(categorySnapshot, by, columns, reveal, new(), vault);
    Assert(categoryLoads == 2 && (string?)reloaded(new object?[] { "new" })["T[Key]"]?["reveal"] == "new",
        "invalidation reloads snapshot and permissions");
    var multiIndex = JsonNode.Parse("{\"columns\":{\"T[Key]\":{\"class\":\"category\"},\"T[Second]\":{\"class\":\"category\"}},\"categories\":{\"T[Key]\":[\"known\"],\"T[Second]\":[\"other\"]}}")!.AsObject();
    var multiSnapshot = new Checks.RequestSnapshot(() => multiIndex);
    var multiBy = new List<string> { "T[Key]", "T[Second]" };
    var multiColumns = new List<ColumnMeta> { columns[0], new() { Table = table, Name = "Second", DataType = 2 } };
    var multiFactory = Checks.GroupFactory(multiSnapshot, multiBy, multiColumns, new(multiBy), new(), vault);
    var multi = new Checks.EqualAggregate(2, 1e-9, multiFactory);
    multi.Accept(new object?[] { "known", "other", 1, 1 });
    multi.Accept(new object?[] { "known", "other", 1, 0 });
    multi.Accept(new object?[] { "known", null, 2, 0 });
    var multiExpected = new JsonArray(
        new JsonObject { ["T[Key]"] = new JsonObject { ["reveal"] = "known" }, ["T[Second]"] = new JsonObject { ["blank"] = true } },
        new JsonObject { ["T[Key]"] = new JsonObject { ["reveal"] = "known" }, ["T[Second]"] = new JsonObject { ["reveal"] = "other" } });
    Assert(multi.RowsConsumed == 3 && multi.Failures == 2 && JsonNode.DeepEquals(Result(multi)["groups"], multiExpected),
        "two grouping columns: exact permissions, offsets and groups");
    // Additive expectations use the actual input order (floating point is order sensitive).
    foreach (var test in new[] { (new double?[] {}, (object?)null, "EMPTY"),
                                 (new double?[] { null, null }, (object?)null, "EMPTY"),
                                 (new double?[] { 2, 3 }, (object?)5, "PASS"),
                                 (new double?[] { 2, 3 }, (object?)6, "FAIL"),
                                 (new double?[] { 1e16, 1, -1e16 }, (object?)0, "PASS"),
                                 (new double?[] { 1e16, -1e16, 1 }, (object?)1, "PASS") }) {
        var a = new Checks.AdditiveAggregate(0);
        foreach (var value in test.Item1) a.Accept(new object?[] { value });
        var result = new JsonObject(); a.Write(result, test.Item2, 1e-9);
        Assert((string?)result["status"] == test.Item3 && a.RowsConsumed == test.Item1.Length, "additive fixed-order expectation");
    }
    Console.WriteLine("grouped streaming acceptance passed");
} finally { Directory.Delete(directory, true); }
''', "utf-8")
    label = "offline grouped streaming: exact verdicts, samples, consumption, retention and index loads"
    # Global settings also apply to implicit restore and referenced projects. The engine
    # sets BaseOutputPath itself, so override it along with the SDK artifacts layout.
    source_manifests = {source: _tree_manifest(source) for source in (helper, S / "engine")}
    def expect_source_trees_unchanged(name: str) -> None:
        source_changes = {str(source): changes for source, before in source_manifests.items()
                          if (changes := _manifest_changes(before, _tree_manifest(source)))}
        expect(name, not source_changes, {"paths": source_changes})

    # Build the helper once, then execute the built apphost directly for both
    # acceptance passes; the second pass no longer exercises an incremental rebuild.
    try:
        proc = subprocess.run(["dotnet", "build", str(helper / "Grouped.csproj"),
                               "--artifacts-path", str(artifacts),
                               f"-p:BaseOutputPath={artifacts / 'bin'}/"],
                              capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        expect(label, False, {"reason": f"{type(exc).__name__}: {exc}"})
        return
    expect(f"{label} (build)", proc.returncode == 0,
           {"exit": proc.returncode, "stdout": proc.stdout.decode("utf-8", "replace"),
            "stderr": proc.stderr.decode("utf-8", "replace")})
    expect_source_trees_unchanged("grouped streaming build: source trees unchanged")
    executables = sorted(artifacts.rglob("Grouped.exe"))
    for run_number in (1, 2):
        try:
            if not executables:
                raise FileNotFoundError(f"Grouped.exe not found under {artifacts}")
            proc = subprocess.run([str(executables[0])], capture_output=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            expect(label, False, {"reason": f"{type(exc).__name__}: {exc}"})
            return
        expect(f"{label} (run {run_number})",
               proc.returncode == 0 and b"grouped streaming acceptance passed" in proc.stdout,
               {"exit": proc.returncode, "stdout": proc.stdout.decode("utf-8", "replace"),
                "stderr": proc.stderr.decode("utf-8", "replace")})
        expect_source_trees_unchanged(f"grouped streaming run {run_number}: source trees unchanged")


def test_offline(env: Env, tmp: Path) -> None:
    test_grouped_streaming(tmp)
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

    # perf / refresh / memory contract shapes (no Desktop)
    perf_ok = {
        "ok": True, "verb": "perf", "timing": "BANDED", "duration_band": "100_500MS",
        "fe_share": "LOW", "se_queries": "1_5", "dq_events": "0", "cache_hit": "NONE",
        "callback": False, "agg_rewrite": "NONE", "trace": "CAPTURED", "verdict": "SAME",
        "baseline": "SAVED",
        "plan": {"CallbackDataID": "0", "Spool_Iterator": "1", "CrossApply": "2_5", "LargeMaterialization": "0"},
    }
    perf_down = {"ok": True, "verb": "perf", "timing": "UNAVAILABLE", "trace": "UNAVAILABLE"}
    perf_raw = dict(perf_ok)
    perf_raw["milliseconds"] = 12
    perf_unbanded = {"ok": True, "verb": "perf", "timing": "BANDED", "trace": "CAPTURED"}
    expect("perf banded example validates", not list(VALIDATOR.iter_errors(perf_ok)))
    expect("perf unavailable example validates without a duration", not list(VALIDATOR.iter_errors(perf_down)))
    expect("perf rejects a raw timing field", list(VALIDATOR.iter_errors(perf_raw)))
    expect("perf BANDED requires duration_band", list(VALIDATOR.iter_errors(perf_unbanded)))
    refresh_old = {"ok": True, "verb": "refresh", "status": "PASS"}
    refresh_new = {"ok": True, "verb": "refresh", "status": "PASS",
                   "objects": [{"name": "Sales", "duration_band": "LT_100MS"}]}
    expect("refresh without objects still validates", not list(VALIDATOR.iter_errors(refresh_old)))
    expect("refresh duration objects validate", not list(VALIDATOR.iter_errors(refresh_new)))
    mem_old = {
        "ok": True, "verb": "memory", "tables": [{
            "name": "Sales", "columns": [{"name": "Id", "dictionary_heavy": False}], "hierarchies": [],
        }], "relationships": [], "coverage": "FULL", "predicates": "OFF",
    }
    mem_new = {
        "ok": True, "verb": "memory", "tables": [{
            "name": "Sales", "columns": [{
                "name": "Id", "dictionary_heavy": False, "encoding": "Hash", "segment_band": "1-9",
                "ri": "RI_OK", "ri_count": "0",
            }], "hierarchies": [],
        }], "relationships": [{"name": "SalesDate", "from": "Sales[Id]", "to": "Date[Id]",
                               "ri": "RI_VIOLATION", "ri_count": "1-9"}],
        "coverage": "FULL", "predicates": "OFF",
    }
    expect("memory column without the new fields still validates", not list(VALIDATOR.iter_errors(mem_old)))
    expect("memory column with encoding, segment_band and ri validates", not list(VALIDATOR.iter_errors(mem_new)))

    r = engine("perf", {"private_dir": str(env.pd), "port": 1, "dax": "SELECT 1", "runs": 1}, "perf-not-dax")
    expect("perf refuses a query that is not DEFINE or EVALUATE", r and r.get("ok") is False and r.get("class") == "BAD_REQUEST", r)
    r = engine("perf", {"private_dir": str(env.pd), "port": 1, "dax": "EVALUATE INFO.TABLES()", "runs": 1}, "perf-info")
    expect("perf refuses INFO", r and r.get("ok") is False and r.get("class") == "BAD_REQUEST", r)
    r = engine("perf", {"private_dir": str(env.pd), "port": 1, "dax": "EVALUATE EVALUATEANDLOG(1)", "runs": 1}, "perf-eal")
    expect("perf refuses EVALUATEANDLOG", r and r.get("ok") is False and r.get("class") == "BAD_REQUEST", r)
    r = engine("perf", {"private_dir": str(env.pd), "port": 1, "dax": "EVALUATE ROW(\"v\", 1)", "runs": 0}, "perf-runs")
    expect("perf runs must be from 1 to 20", r and r.get("class") == "BAD_REQUEST", r)
    r = engine("perf", {"private_dir": str(env.pd), "port": 1, "dax": "EVALUATE ROW(\"v\", 1)", "runs": 1, "action": "drop"}, "perf-action")
    expect("perf action must be save or compare", r and r.get("class") == "BAD_REQUEST", r)
    r = engine("perf", {"private_dir": str(env.pd), "port": 1, "dax": "EVALUATE ROW(\"v\", 1)", "runs": 1, "action": "save"}, "perf-baseline")
    expect("perf save requires a baseline name", r and r.get("class") == "BAD_REQUEST", r)
    r = engine("perf", {"private_dir": str(env.pd), "port": 1, "dax": "EVALUATE ROW(\"v\", 1)", "runs": 1}, "perf-closed")
    expect("perf on a closed port is ENGINE_UNAVAILABLE after the guard", r and r.get("ok") is False and r.get("class") == "ENGINE_UNAVAILABLE", r)

    # pbz/engine.py wrapper
    spec = importlib.util.spec_from_file_location("pbz_engine_wrapper", S / "pbz" / "engine.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    r = mod.call("ping", {"private_dir": str(env.pd)})
    expect("engine.py call(ping) returns ok, verb, and version", r == {"ok": True, "verb": "ping", "version": "1"}, r)
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


def publish_index(env: Env, summary: dict, project: str) -> None:
    """Use the production hardener and publisher for live reader tests."""
    sys.path.insert(0, str(S))
    from types import SimpleNamespace
    from pbz import checks, index_store
    checks.harden_index(SimpleNamespace(private=str(env.pd)), {})
    summary["tables"] = checks._rebuild_summary(str(env.pd), summary.get("tables", []))
    index_store.publish(str(env.pd), project, summary)


def test_live_index_binding(env: Env, tmp: Path) -> None:
    """Real A/B Desktop models sharing a private store; index A LAST, then check B."""
    global PBIP
    sys.path.insert(0, str(S))
    from pbz import dpapi, index_store
    original_pbip = PBIP
    b = Env()
    b.pd = env.pd
    directory = tmp / "index-project-b"
    shutil.copytree(FIXTURE, directory, ignore=shutil.ignore_patterns(".pbi"))
    table_file = directory / "Canary.SemanticModel" / "definition" / "tables" / "Project.tmdl"
    table = table_file.read_text("utf-8")
    # Keep Closed in both A and B: a raw reveal in B would pass a membership-only gate.
    # Add enough distinct Status values to cross the policy's default category_max.
    start = table.index("DATATABLE(")
    table = table[:start] + ('DATATABLE("ProjectID", INTEGER, "Title", STRING, "Status", STRING, '
        '"PlannedEnd", DATETIME, {' + ",".join(
            '{' + str(1000 + i) + ',"fixture","' + ("Closed" if i == 0 else f"Status-{i}")
            + '","2026-01-01"}' for i in range(60)) + '})\n')
    table_file.write_text(table, "utf-8")
    try:
        PBIP = directory / "Canary.pbip"
        if not expect("binding fixture Desktop is available", ensure_desktop(b)):
            return
        engine("refresh", b.req(), "binding-b-refresh")
        rb = engine("index", b.req(), "binding-index-b")
        publish_index(b, rb, "B")
        expect("binding B: Status exceeds category_max", any(
            t["table"] == "Project" and any(c["column"] == "Status" and c.get("class") == "high"
                                             for c in t["columns"]) for t in rb["tables"]), rb)
        ra = engine("index", env.req(), "binding-index-a-LAST")
        publish_index(env, ra, "A")

        def grouped(target, label, where=None):
            return engine("check", target.req(reveal=["Project[Status]"], checks=[{
                "id": "binding", "kind": "equal", "left": "1", "right": "0",
                "by": ["Project[Status]"], "where": where or ["NOT ISBLANK(Project[Status])"]}]), label)

        shared = ['Project[Status] = "Closed"']
        raw_b = grouped(b, "binding-check-b-shared", shared)
        groups = raw_b.get("checks", raw_b.get("results", []))[0].get("groups", [])
        expect("B never reveals even a member of A's list", groups and all(
            "hmac" in g["Project[Status]"] for g in groups), raw_b)
        raw_a = grouped(env, "binding-check-a-member", shared)
        groups = raw_a.get("checks", raw_a.get("results", []))[0].get("groups", [])
        expect("A genuine category member still reveals", groups == [
            {"Project[Status]": {"reveal": "Closed"}}], raw_a)
        # A complete, hash-valid generation still needs the live model stamp.
        manifest_path = env.pd / "index-current.json"
        stamped_manifest = manifest_path.read_bytes()
        manifest = json.loads(stamped_manifest)
        unstamped = "index-" + os.urandom(16).hex()
        unstamped_dir = env.pd / unstamped
        shutil.copytree(env.pd / manifest["generation"], unstamped_dir)
        index_path = unstamped_dir / "index.json"
        idx = json.loads(index_path.read_bytes())
        expect("binding source generation has a model stamp", bool(idx.get("model_stamp")))
        idx.pop("model_stamp", None)
        index_path.write_bytes(json.dumps(idx).encode("utf-8"))
        manifest["generation"] = unstamped
        manifest["sha256"] = {name: hashlib.sha256((unstamped_dir / name).read_bytes()).hexdigest()
                              for name in index_store.FILES}
        try:
            manifest_path.write_text(json.dumps(manifest), "utf-8")
            expect("unstamped generation is complete and hash-valid", index_store.load(str(env.pd), "A") is not None)
            unstamped_result = grouped(env, "binding-check-a-unstamped", shared)
            groups = unstamped_result.get("checks", unstamped_result.get("results", []))[0].get("groups", [])
            expect("missing model stamp prevents category reveal", groups and all(
                set(g["Project[Status]"]) == {"hmac"} for g in groups), unstamped_result)
        finally:
            manifest_path.write_bytes(stamped_manifest)

        many = grouped(b, "binding-check-b-max-ten")
        groups = many.get("checks", many.get("results", []))[0].get("groups", [])
        expect("failing grouped equal returns at most ten HMAC groups", len(groups) == 10 and all(
            "hmac" in g["Project[Status]"] for g in groups), many)

        # Change only the disposable B model through TOM. Index it at low cardinality,
        # then add a real row on the same database/port without reindexing.
        helper = tmp / "index-row-mutation"
        helper.mkdir()
        import xml.etree.ElementTree as ET
        project_xml = ET.Element("Project", Sdk="Microsoft.NET.Sdk")
        props = ET.SubElement(project_xml, "PropertyGroup")
        ET.SubElement(props, "OutputType").text = "Exe"
        ET.SubElement(props, "TargetFramework").text = "net10.0"
        refs = ET.SubElement(project_xml, "ItemGroup")
        for dll in EXE.parent.glob("Microsoft.AnalysisServices*.dll"):
            ref = ET.SubElement(refs, "Reference", Include=dll.stem)
            ET.SubElement(ref, "HintPath").text = str(dll)
        ET.ElementTree(project_xml).write(helper / "Mutation.csproj", encoding="unicode")
        (helper / "Program.cs").write_text(r"""
using System;
using System.IO;
using System.Linq;
using Microsoft.AnalysisServices.Tabular;
using var server = new Server();
server.Connect("Data Source=localhost:" + args[0] + ";");
if (server.Databases.Count != 1) throw new Exception("fixture database count");
var database = server.Databases[0];
if (database.ID != args[1]) throw new Exception("fixture identity mismatch");
if (!database.Model.Tables.Select(t => t.Name).ToHashSet().SetEquals(
    new[] { "Person", "Project", "Hours", "DimDate", "ExceptionType", "Notes" }))
    throw new Exception("fixture table mismatch");
var table = database.Model.Tables["Project"];
((CalculatedPartitionSource)table.Partitions[0].Source).Expression = File.ReadAllText(args[2]);
table.RequestRefresh(RefreshType.Full);
database.Model.SaveChanges();
""", "utf-8")
        connection = engine("connect", b.req(), "row-add-identity")
        database_id = connection["database_id"]

        def change_rows(statuses):
            expression = ('DATATABLE("ProjectID", INTEGER, "Title", STRING, "Status", STRING, '
                '"PlannedEnd", DATETIME, {' + ",".join(
                    '{' + str(1000 + i) + ',"fixture","' + status + '","2026-01-01"}'
                    for i, status in enumerate(statuses)) + '})')
            expression_file = helper / "partition.dax"
            expression_file.write_text(expression, "utf-8")
            proc = subprocess.run(["dotnet", "run", "--project", str(helper / "Mutation.csproj"),
                                   "--", str(b.port), database_id, str(expression_file)],
                                  capture_output=True, timeout=300)
            return expect("disposable B row mutation succeeds", proc.returncode == 0,
                          {"exit": proc.returncode, "stderr_bytes": len(proc.stderr)})

        if not change_rows(["Closed", "Active"]):
            return
        rb = engine("index", b.req(), "index-before-row-add")
        publish_index(b, rb, "B")
        control = grouped(b, "binding-b-before-row-add", shared)
        groups = control.get("checks", control.get("results", []))[0].get("groups", [])
        expect("B category member reveals before mutation", groups == [
            {"Project[Status]": {"reveal": "Closed"}}], control)
        if not change_rows(["Closed", "Active", "NewStatus"]):
            return
        stale = grouped(b, "binding-new-live-value", ['Project[Status] IN {"Closed", "NewStatus"}'])
        groups = stale.get("checks", stale.get("results", []))[0].get("groups", [])
        key = dpapi.load_key(str(env.pd), create=False)
        expect("refresh invalidates all grouped reveals without reindex", len(groups) == 2
            and all(set(g["Project[Status]"]) == {"hmac"} for g in groups)
            and {g["Project[Status]"]["hmac"] for g in groups}
                == {dpapi.hmac_hex(key, value) for value in ("Closed", "NewStatus")}, stale)
    finally:
        PBIP = original_pbip
        if b.pid and b.opened_here:
            print("INFO closing binding fixture Desktop:", close_desktop(b.pid), flush=True)


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()


def perf_workload_digest(query):
    # Match the production token boundaries, including exact strings/identifiers
    # and line/block comment extents. Comment whitespace is normalized only inside
    # the token, so a newline ending a line comment cannot swallow executable DAX.
    tokens = re.findall(
        r'//[^\r\n]*|--[^\r\n]*|/\*[\s\S]*?(?:\*/|$)|'
        r'"(?:[^"]|"")*"|\'(?:[^\']|\'\')*\'|\[(?:[^\]]|\]\])*\]|'
        r'[+\-*/^&=<>(),{}.:;!|]|[^\s"\'\[+*/^&=<>(),{}.:;!|\-]+', query)
    tokens = [re.sub(r"\s+", " ", token) if token.startswith(("//", "--", "/*"))
              else token for token in tokens]
    return digest("\x1f".join(tokens))


def perf_result_private(value, query, project):
    workload = perf_workload_digest(query)

    def private(node):
        if isinstance(node, str):
            return query not in node and project not in node and workload not in node
        if isinstance(node, dict):
            return all("fingerprint" not in key.lower() and "workload" not in key.lower()
                       and private(child) for key, child in node.items())
        if isinstance(node, list):
            return all(private(child) for child in node)
        return True

    return private(value)


def test_perf_baseline_workloads(env: Env) -> None:
    """Windows/Desktop acceptance: actual protected baseline save/read, never a mocked save."""
    query_a = 'EVALUATE ROW("workload", "a b", "probe", SUMX(GENERATESERIES(1, 1000000), SIN([Value])))'
    query_b = query_a.replace('"a b"', '"ab"')

    def call(project, check, query, action):
        result = engine("perf", env.req(project_identity=project, baseline=check,
                                        dax=query, runs=20, action=action), "perf-workload") or {}
        expect("perf workload identity stays private", perf_result_private(result, query, project), result)
        return result

    saved = call("project-one", "check:one", query_a, "save")
    expect("perf same-query control save succeeded", saved.get("baseline") == "SAVED", saved)
    control = call("project-one", "check:one", query_a, "compare")
    expect("perf same-query PASS control", control.get("verdict") in ("SAME", "FASTER")
           and control.get("ratio_verdict") == "WITHIN_LIMIT", control)
    mismatch = call("project-one", "check:one", query_b, "compare")
    expect("perf literal-only different query has no comparable baseline",
           mismatch.get("baseline") == "MISSING" and "verdict" not in mismatch, mismatch)
    for project, check, query in (("project-one", "check:two", query_b),
                                  ("project-two", "check:one", query_b),
                                  ("project-two", "check:two", query_a)):
        result = call(project, check, query, "save")
        expect("perf interleaved save succeeds", result.get("baseline") == "SAVED", result)
    for project, check, query in (("project-one", "check:one", query_a),
                                  ("project-one", "check:two", query_b),
                                  ("project-two", "check:one", query_b),
                                  ("project-two", "check:two", query_a)):
        result = call(project, check, query, "compare")
        expect("perf project/check baseline remains comparable", "verdict" in result, result)
        other = query_b if query == query_a else query_a
        result = call(project, check, other, "compare")
        expect("perf project/check isolation", result.get("baseline") == "MISSING" and "verdict" not in result, result)
    # Replace the matching file with a protected legacy body, to prove version and
    # workload checks reject it even if a legacy file reaches the new storage path.
    sys.path.insert(0, str(S))
    from pbz import dpapi
    workload = perf_workload_digest(query_a)
    key = digest("project-one\ncheck:one\n" + workload)
    path = env.pd / "perf" / (digest(key) + ".bin")
    expect("perf legacy test reaches saved baseline", path.exists())
    path.write_bytes(dpapi.protect(json.dumps({"median": 1, "spread": 0}).encode("utf-8")))
    result = call("project-one", "check:one", query_a, "compare")
    expect("perf legacy unbound baseline ignored", result.get("baseline") == "MISSING" and "verdict" not in result, result)


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
    def text_classes(resp):
        return {(t["table"], c["column"]): c.get("class")
                for t in (resp or {}).get("tables", []) for c in t["columns"] if c.get("type") == "string"}

    r_low = engine("index", env.req(category_max=1), "index-cut-low")
    r = engine("index", env.req(category_max=100000), "index")
    expect("index: request category_max does not move the class boundary",
           r_low and r and r.get("ok") and text_classes(r_low) == text_classes(r)
           and set(text_classes(r).values()) <= {"category", "high", "empty"}, r)
    cls = {(t["table"], c["column"]): c for t in (r or {}).get("tables", []) for c in t["columns"]}
    expect("index: non-text columns carry no class", all("class" not in cls[k] for k in cls if cls[k]["type"] != "string"), r)
    tb = {t["table"]: t for t in (r or {}).get("tables", [])}
    expect("index: row buckets", tb.get("Hours", {}).get("rows_bucket") == "10-99" and tb.get("DimDate", {}).get("rows_bucket") == "100-999", r)
    expect("index: blank share bucket and no exact years",
           cls.get(("Hours", "Person"), {}).get("blank_bucket") == "1-10%"
           and all("year_min" not in c and "year_max" not in c for c in cls.values()), r)
    key = dpapi_unprotect((env.pd / "key.bin").read_bytes())
    key_holder["key"] = key
    expect("key.bin: DPAPI-protected 32-byte key readable from Python", len(key) == 32)
    idx = json.loads((env.pd / "index.json").read_text("utf-8"))
    cols = idx.get("columns", {})
    name_entry = cols.get("Person[Name]", {})
    name_h = set(name_entry.get("hmacs", []))
    if name_entry.get("class") == "high":
        expect("index.json: Person[Name] holds 4 HMACs, Python HMAC of a planted name matches",
               len(name_h) == 4 and hmac_hex(key, PERSONS[0]) in name_h and hmac_hex(key, PERSONS[3]) in name_h)
    else:
        expect("index.json: a category name column has no value hmac list", "hmacs" not in name_entry)
    expect("index.json: numeric/date columns have no hmacs", all("hmacs" not in v for k, v in cols.items() if v.get("type") != "string"))
    wh = set(cols.get("Person[Name]", {}).get("word_hmacs", []))
    expect("index.json v1.1: word_hmacs on name-like columns (Person[Name], Hours[Person]) only",
           wh and "word_hmacs" in cols.get("Hours[Person]", {}) and "word_hmacs" not in cols.get("Project[Title]", {})
           and "word_hmacs" not in cols.get("Person[Email]", {}))
    expect("index.json v1.1: word HMAC of a stripped planted word matches", all(hmac_hex(key, w) in wh for w in words(PERSONS[1])))
    cats = json.loads(dpapi_unprotect((env.pd / "categories.bin").read_bytes()).decode("utf-8"))
    high_cols = [k for k, v in cols.items() if v.get("class") == "high"]
    expect("categories.bin: DPAPI JSON with category values (high-cardinality columns absent)",
           cats.get("Project[Status]") == ["Active", "Closed", "On Hold"] and len(cats.get("ExceptionType[Code]", [])) == 3
           and all(k not in cats for k in high_cols))
    r = engine("index", env.req(category_max=3, disclosure="strict"), "index-strict")
    flat = [c for t in (r or {}).get("tables", []) for c in t["columns"]]
    expect("index strict: no buckets or years", r and r.get("ok") and all(not ({"card_bucket", "blank_bucket", "year_min", "year_max"} & set(c)) for c in flat)
           and all("rows_bucket" not in t for t in r["tables"]), r)

    partial = engine("check", env.req(reveal=["Person[Name]"], checks=[{
        "id": "partial", "kind": "equal", "left": "1", "right": "0", "by": ["Person[Name]"],
        "where": ["NOT ISBLANK(Person[Name])"]}]), "index-unpublished-personal")
    groups = partial.get("checks", partial.get("results", []))[0].get("groups", [])
    expect("unpublished low-cardinality personal index is never accepted", groups and all(
        "hmac" in g["Person[Name]"] for g in groups), partial)
    publish_index(env, r, "Canary")

    # probe
    r = engine("probe", env.req(table="Hours"), "probe-hours")
    pc = {c["column"]: c for c in (r or {}).get("columns", [])}
    expect("probe Hours: rows bucket, blanks, negatives, future dates",
           r and r.get("rows_bucket") == "10-99" and pc["Person"]["has_blanks"] is True and pc["Hours"]["has_negatives"] is True
           and pc["ProjectID"]["has_negatives"] is False and pc["Date"]["has_future_dates"] is False
           and "year_min" not in pc["Date"] and "year_max" not in pc["Date"], r)
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
        # nbk-where tests that an outer where filter on the key column (Hours[Person] <> "")
        # is preserved by KEEPFILTERS and not clobbered by the blank predicate (Hours[Person] = "")
        {"id": "nbk-where", "kind": "no_blank_keys", "column": "Hours[Person]", "where": ["Hours[Person] <> \"\""]},
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
    expect("check no_blank_keys: planted blank FAIL; ids PASS; dimension blank row not counted; where PASS",
           (st.get("nbk-person"), st.get("nbk-pid"), st.get("nbk-dim"), st.get("nbk-where")) == ("FAIL", "PASS", "PASS", "PASS"), r)
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
    expect("check strict: no mismatch bucket, no groups, no offenders bucket",
           "mismatch" not in c.get("eq-status", {}) and "groups" not in c.get("eq-status", {})
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
    snap = json.loads((env.pd / "snapshots" / "results" / "t1.json").read_text("utf-8"))
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

    # PR A: a save where every query fails must not publish (real engine).
    qerr2 = {"id": "qerr2", "dax": f"EVALUATE ROW(\"e\", ERROR(\"{req_canary('-SNAPERR-')}\"))"}
    r = engine("snapshot-save", env.req(name="af", queries=[q1, q2]), "snapshot-af-seed")
    expect("snapshot af seed save: ok", bool((r or {}).get("ok")), r)
    s = {q["id"]: q for q in (r or {}).get("queries", [])}
    expect("snapshot af seed save: verdicts SAVED x2",
           s.get("q1", {}).get("result") == "SAVED" and s.get("q2", {}).get("result") == "SAVED", r)
    af_path = env.pd / "snapshots" / "results" / "af.json"
    expect("snapshot af seed save: baseline file exists", af_path.is_file(), r)
    af_bytes = af_path.read_bytes() if af_path.is_file() else None
    r = engine("snapshot-save", env.req(name="af", queries=[qerr, qerr2]), "snapshot-af-all-failed")
    s = {q["id"]: q for q in (r or {}).get("queries", [])}
    expect("snapshot all-failed save: verdicts ERROR x2",
           s.get("qerr", {}).get("result") == "ERROR" and s.get("qerr2", {}).get("result") == "ERROR", r)
    expect("snapshot all-failed save: baseline bytes unchanged",
           af_bytes is not None and af_path.is_file() and af_path.read_bytes() == af_bytes, r)
    r = engine("snapshot-save", env.req(name="af-never", queries=[qerr]), "snapshot-af-first")
    expect("snapshot first all-failed save: verdict ERROR",
           ((r or {}).get("queries") or [{}])[0].get("result") == "ERROR", r)
    expect("snapshot first all-failed save: no result file created",
           not (env.pd / "snapshots" / "results" / "af-never.json").exists(), r)
    qzero = {"id": "qzero", "dax": "EVALUATE TOPN(0, SUMMARIZECOLUMNS(DimDate[Year], \"m\", [Total Hours]))"}
    r = engine("snapshot-save", env.req(name="af-zero", queries=[qzero]), "snapshot-af-zero")
    expect("snapshot zero-row success: verdict SAVED",
           [q.get("result") for q in (r or {}).get("queries", [])] == ["SAVED"], r)
    expect("snapshot zero-row success: result file written",
           (env.pd / "snapshots" / "results" / "af-zero.json").is_file(), r)
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

    # snapshot namespace: monthly and monthly.spec do not collide in results/
    r_monthly = engine("snapshot-save", env.req(name="monthly", queries=[q1]), "snapshot-monthly")
    r_spec = engine("snapshot-save", env.req(name="monthly.spec", queries=[q2]), "snapshot-monthly-spec")
    expect("snapshot non-collision: both monthly and monthly.spec save successfully",
           bool((r_monthly or {}).get("ok") and (r_spec or {}).get("ok")))
    expect("snapshot results files: separate results/monthly.json and results/monthly.spec.json exist",
           (env.pd / "snapshots" / "results" / "monthly.json").is_file()
           and (env.pd / "snapshots" / "results" / "monthly.spec.json").is_file())
    r_cmp_monthly = engine("snapshot-compare", env.req(name="monthly", queries=[q1]), "compare-monthly")
    r_cmp_spec = engine("snapshot-compare", env.req(name="monthly.spec", queries=[q2]), "compare-monthly-spec")
    expect("snapshot compare non-collision: both compare independently as SAME",
           [q.get("result") for q in (r_cmp_monthly or {}).get("queries", [])] == ["SAME"]
           and [q.get("result") for q in (r_cmp_spec or {}).get("queries", [])] == ["SAME"])

    # snapshot legacy fallback: read-only fallback to private_dir/snapshots/<name>.json
    # 1. Save fresh baseline named 'legacy' with [q1, q2, q3]
    r_save_leg = engine("snapshot-save", env.req(name="legacy", queries=[q1, q2, q3]), "snapshot-save-legacy")
    expect("snapshot save fresh legacy baseline -> ok", bool((r_save_leg or {}).get("ok")))
    primary_leg_file = env.pd / "snapshots" / "results" / "legacy.json"
    legacy_file = env.pd / "snapshots" / "legacy.json"
    # 2. Move result file to snapshots/legacy.json (primary becomes absent)
    primary_bytes = primary_leg_file.read_bytes()
    legacy_file.write_bytes(primary_bytes)
    primary_leg_file.unlink()
    # 3. Compare without intervening refresh: should read from legacy fallback, leave legacy bytes untouched
    r_legacy = engine("snapshot-compare", env.req(name="legacy", queries=[q1, q2, q3]), "compare-legacy-fallback")
    leg_queries = (r_legacy or {}).get("queries", [])
    expect("snapshot legacy fallback: reads from snapshots/legacy.json when results/ missing",
           not primary_leg_file.exists()
           and legacy_file.read_bytes() == primary_bytes
           and [q.get("id") for q in leg_queries] == ["q1", "q2", "q3"]
           and [q.get("result") for q in leg_queries] == ["SAME", "SAME", "SAME"])

    # snapshot legacy fallback: does not misread a spec file as a result document
    spec_only_file = env.pd / "snapshots" / "unmigrated.spec.json"
    spec_only_file.write_text(json.dumps({"queries": [{"id": "q1", "dax": "EVALUATE {1}"}]}), encoding="utf-8")
    r_spec_only = engine("snapshot-compare", env.req(name="unmigrated.spec", queries=[q1]), "compare-spec-only")
    expect("snapshot legacy fallback: does not misread a spec file as a result document (reports NEW)",
           [q.get("result") for q in (r_spec_only or {}).get("queries", [])] == ["NEW"])

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

    # deps: engine dependency list, identity columns only, names limited to TMSCHEMA names
    r = engine("deps", env.req(), "deps")
    edges = {tuple(e) for e in (r or {}).get("edges", [])}
    expect("deps: measure -> measure and measure -> column edges from DISCOVER_CALC_DEPENDENCY",
           ("MEASURE", "Hours", "Active Hours", "MEASURE", "Hours", "Total Hours") in edges
           and any(e[0] == "MEASURE" and e[2] == "Total Hours" and e[3] in ("COLUMN", "CALC_COLUMN") and e[5] == "Hours"
                   for e in edges), r)
    expect("deps: every name is a model object name, \"\" or \"?\"",
           all(len(e) == 6 for e in edges) and not any("=" in x or "(" in x for e in edges for x in e), r)

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

    result = scan([S])
    expect(f"canary scan: {result['files']} files read", result['ok'], result['failures'][:10])

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


# Scanner fixtures run without a Windows engine; no existing engine checks are skipped.
import unittest
from unittest.mock import patch


class PerfPrivacyTests(unittest.TestCase):
    def test_workload_digest_spacing_and_comment_extents(self):
        for left, right, equal in (
            ("EVALUATE {1+2}", "EVALUATE {1 + 2}", True),
            ("EVALUATE {1=1||2=3}", "EVALUATE {1=1 || 2=3}", True),
            ("EVALUATE {1 -- note\n+2}", "EVALUATE {1 -- note +2}", False),
            ("EVALUATE {1 // note\n+2}", "EVALUATE {1 // note +2}", False),
            ("EVALUATE {1 /* note */+2}", "EVALUATE {1 /* note */ + 2}", True),
            ("EVALUATE {1 /* note +2 */}", "EVALUATE {1 /* note */+2}", False),
        ):
            with self.subTest(query=left):
                if equal:
                    self.assertEqual(perf_workload_digest(left), perf_workload_digest(right))
                else:
                    self.assertNotEqual(perf_workload_digest(left), perf_workload_digest(right))

    def test_nested_workload_digest_value_is_private(self):
        query = 'EVALUATE ROW("workload", "a b")'
        workload = perf_workload_digest(query)
        self.assertEqual(workload, digest("\x1f".join([
            "EVALUATE", "ROW", "(", '\"workload\"', ",", '\"a b\"', ")"])))
        self.assertFalse(perf_result_private(
            {"safe": [{"detail": "prefix " + workload + " suffix"}]}, query, "project-one"))

    def test_legitimate_results_stay_private(self):
        query = 'EVALUATE ROW("workload", "a b")'
        for result in ({"baseline": "SAVED"}, {"verdict": "SAME", "ratio_verdict": "WITHIN_LIMIT"},
                       {"baseline": "MISSING"}, {"detail": [{"status": "PASS", "count": 1}]}):
            self.assertTrue(perf_result_private(result, query, "project-one"))


class GroupedStreamingLauncherTests(unittest.TestCase):
    def test_source_tree_manifest_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "engine" / "bin" / "pbz-engine.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"original")
            before = _tree_manifest(root)
            self.assertEqual(set(before), {"engine/bin/pbz-engine.exe"})
            self.assertEqual(_manifest_changes(before, _tree_manifest(root)), [])

            added = executable.parent / "new.dll"
            added.write_bytes(b"new")
            self.assertEqual(_manifest_changes(before, _tree_manifest(root)),
                             ["added: engine/bin/new.dll"])
            before = _tree_manifest(root)
            executable.write_bytes(b"modified")  # Same size; content must be compared.
            self.assertEqual(_manifest_changes(before, _tree_manifest(root)),
                             ["content-changed: engine/bin/pbz-engine.exe"])
            before = _tree_manifest(root)
            added.unlink()
            self.assertEqual(_manifest_changes(before, _tree_manifest(root)),
                             ["removed: engine/bin/new.dll"])

            before = _tree_manifest(root)
            build_output = root / "engine" / "build" / "output.dll"
            build_output.parent.mkdir()
            build_output.write_bytes(b"output")
            executable.unlink()
            self.assertEqual(_manifest_changes(before, _tree_manifest(root)),
                             ["added: engine/build/output.dll", "removed: engine/bin/pbz-engine.exe"])

    def assert_launch_failure(self, error):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(subprocess, "run", side_effect=error) as run, \
             patch.object(sys.modules[__name__], "expect") as record:
            test_grouped_streaming(Path(directory))
            record.assert_called_once()
            name, ok, detail = record.call_args.args
            self.assertIn("offline grouped streaming", name)
            self.assertFalse(ok)
            self.assertIn(type(error).__name__, detail["reason"])
            self.assertIn(str(error), detail["reason"])
            command = run.call_args.args[0]
            self.assertEqual(command[:2], ["dotnet", "build"])
            artifacts = Path(directory) / "grouped-streaming-artifacts"
            self.assertEqual(command[command.index("--artifacts-path") + 1], str(artifacts))
            self.assertIn(f"-p:BaseOutputPath={artifacts / 'bin'}/", command)

    def test_missing_sdk_is_recorded(self):
        self.assert_launch_failure(FileNotFoundError("dotnet SDK missing"))

    def test_timeout_is_recorded(self):
        self.assert_launch_failure(subprocess.TimeoutExpired(["dotnet", "run"], 300))


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(S))
        from pbz import engine
        self.engine = engine

    def test_check_identity_and_hard_ratio_fail_closed(self):
        from pbz import common, perf
        with tempfile.TemporaryDirectory() as root:
            ws = common.Workspace(root)
            project = common.Project(ws, "One")
            with patch("pbz.perf.desktop.require_port", return_value=1), \
                 patch("pbz.perf.checks.spend", return_value=True), \
                 patch("pbz.perf.engine_call") as call:
                for verdict, ratio, status in (("NOISY", "WITHIN_LIMIT", "PASS"),
                                               ("NOISY", "OVER_LIMIT", "FAIL")):
                    call.return_value = {"ok": True, "verb": "perf", "timing": "BANDED", "trace": "CAPTURED",
                                         "verdict": verdict, "ratio_verdict": ratio}
                    self.assertEqual(perf.expect(ws, project, "EVALUATE {1}")["status"], status)

    def test_probe_requires_verb(self):
        request = {"table": "Sales", "columns": ["Amount"], "disclosure": "shape"}
        response = {"ok": True, "table": "Sales", "columns": []}
        for verb in (None, "health"):
            obj = {**response, **({"verb": verb} if verb else {})}
            self.assertEqual(self.engine._project("probe", obj, request)["class"], "ENGINE_UNAVAILABLE")
        self.assertTrue(self.engine._project("probe", {**response, "verb": "probe"}, request)["ok"])

    def test_failure_verb_must_match(self):
        self.assertEqual(self.engine._project("deps", {"ok": False, "verb": "health", "class": "TIMEOUT"}),
                         {"ok": False, "verb": "deps", "class": "ENGINE_UNAVAILABLE"})
        self.assertEqual(self.engine._project("deps", {"ok": False, "verb": "deps", "class": "TIMEOUT"})["class"], "TIMEOUT")

    def test_deps_positions(self):
        edge = ["MEASURE", "Sales", "Total", "COLUMN", "Sales", "Amount"]
        good = {"ok": True, "verb": "deps", "edges": [edge]}
        self.assertEqual(self.engine._project("deps", good), good)
        for i in range(6):
            wrong = edge[:]
            wrong[i] = 12
            self.assertEqual(self.engine._project("deps", {**good, "edges": [wrong]})["class"], "ENGINE_UNAVAILABLE")

    def test_unusable_contract(self):
        for error in (FileNotFoundError("missing"), PermissionError("unreadable")):
            with patch.object(Path, "read_text", side_effect=error):
                self.assertEqual(self.engine._project("deps", {"ok": True})["class"], "ENGINE_UNAVAILABLE")
        for schema in ({}, [], {"$defs": []}, {"$defs": {"bad": []}}):
            with self.subTest(schema=schema), patch.object(Path, "read_text", return_value=json.dumps(schema)):
                self.assertEqual(self.engine._project("deps", {"ok": True})["class"], "ENGINE_UNAVAILABLE")

    def test_non_object_group_key_union_fails_closed(self):
        contract = json.loads((S / "engine" / "contract.schema.json").read_text())
        response = {"ok": True, "verb": "check", "checks": [
            {"id": "fixture", "status": "FAIL", "groups": [{"Region": {"reveal": "public"}}]}]}
        self.assertEqual(self.engine._project("check", response), response)
        contract["$defs"]["groupKey"]["oneOf"] = [None]
        with patch.object(Path, "read_text", return_value=json.dumps(contract)):
            self.assertEqual(self.engine._project("check", response),
                             {"ok": False, "verb": "check", "class": "ENGINE_UNAVAILABLE"})


class CanaryScanTests(unittest.TestCase):
    def test_clean_positive_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'clean.txt').write_text('safe metadata')
            result = scan([tmp], excluded=[])
            self.assertTrue(result['ok'])
            self.assertEqual(result['files'], 1)

    def test_fixture_prefix_sibling_is_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp, 'fixture')
            sibling = Path(tmp, 'fixture-output')
            fixture.mkdir()
            sibling.mkdir()
            for folder in (fixture, sibling):
                (folder / 'value.txt').write_text(CAN['canary-strings'][0])
            result = scan([tmp], excluded=[fixture])
            self.assertFalse(result['ok'])
            self.assertTrue(result['completed'])
            self.assertEqual(len(result['failures']), 1)
            self.assertIn('fixture-output', result['failures'][0]['path'])

    def test_read_error_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'blocked').touch()
            def denied(path):
                raise OSError('injected')
            result = scan([tmp], read=denied)
            self.assertFalse(result['ok'])
            self.assertTrue(result['completed'])
            self.assertEqual(result['files'], 0)
            self.assertEqual(result['failures'][0]['reason'], 'read error')

    def test_missing_root_and_enumeration_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = scan([Path(tmp, 'absent')])
            self.assertFalse(missing['ok'])
            self.assertFalse(missing['completed'])
            def broken(root, onerror):
                onerror(OSError(5, 'injected', str(root)))
                return []
            result = scan([tmp], walk=broken)
            self.assertFalse(result['ok'])
            self.assertFalse(result['completed'])
            def raised(root, onerror):
                raise OSError('enumeration failed')
            self.assertFalse(scan([tmp], walk=raised)['completed'])

    def test_canary_fails_shard_without_engine_and_aggregate(self):
        import copy
        import ci_equivalence
        import run_offline
        from ci_manifest import MANIFEST, allocate
        entries = json.loads(MANIFEST.read_text())
        groups = allocate(entries, 2)
        identity = {k: 'fixture' for k in ('python', 'os', 'image', 'dotnet', 'dependencies',
                                         'nuget', 'source', 'project', 'payload')}
        base = dict(sha='fixture', identity=identity, ok=True, scan=dict(completed=True, ok=True))
        def rows(entries):
            return [dict(e, exit_code=0, skip_reasons=[], error=None, failure_reason=None) for e in entries]
        reference = dict(copy.deepcopy(base), shard='serial', executed=rows(entries))
        shards = [dict(copy.deepcopy(base), shard=i, executed=rows(group)) for i, group in enumerate(groups)]
        planted_index = next(i for i, group in enumerate(groups) if all(e['id'] != 'engine-offline' for e in group))
        with tempfile.TemporaryDirectory() as tmp, patch.object(run_offline, 'identity', return_value=identity), patch.object(run_offline.subprocess, 'check_output', return_value='fixture'):
            output = Path(tmp, 'output')
            output.mkdir()
            def child(argv, **kwargs):
                (output / 'leak.txt').write_text(CAN['canary-strings'][0])
                return subprocess.CompletedProcess(argv, 0)
            report = run_offline.execute(groups[planted_index], shard=planted_index, output=output,
                                         run=child, scanner=lambda roots: scan([output]))
            self.assertFalse(report['ok'])
            self.assertTrue(report['scan']['completed'])
            shards[planted_index] = report
            evidence = Path(tmp, 'reports')
            evidence.mkdir()
            for i, data in enumerate([reference, *shards]):
                (evidence / f'{i}.json').write_text(json.dumps(data))
            with patch.object(sys, 'argv', ['ci_equivalence.py', str(evidence)]), self.assertRaisesRegex(ValueError, 'incomplete or failed scan'):
                ci_equivalence.main()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="skip verbs that need Power BI Desktop")
    ap.add_argument("--close", action="store_true", help="close the fixture's Desktop window at the end")
    ap.add_argument("--keep", action="store_true", help="keep the temporary private_dir")
    args = ap.parse_args()

    scanner_tests = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromNames(["CanaryScanTests", "ProjectionTests", "GroupedStreamingLauncherTests", "PerfPrivacyTests"], module=sys.modules[__name__]))
    if not scanner_tests.wasSuccessful():
        return 1
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
            test_perf_baseline_workloads(env)
            test_live(env, key_holder)
            test_live_index_binding(env, tmp)
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
    if "--launcher-tests" in sys.argv:
        unittest.main(argv=[sys.argv[0]], defaultTest="GroupedStreamingLauncherTests")
    if "--projection-tests" in sys.argv:
        unittest.main(argv=[sys.argv[0]], defaultTest="ProjectionTests")
    if "--scan-tests" in sys.argv:
        unittest.main(argv=[sys.argv[0]], defaultTest="CanaryScanTests")
    sys.exit(main())
