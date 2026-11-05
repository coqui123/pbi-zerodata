#!/usr/bin/env python3
"""Tests for the engine verb `memory` and pbz/memory.py (stdlib + jsonschema, no pytest).

    python tests\\test_memory.py                        # offline: wrapper logic, closed bands, engine envelopes
    python tests\\test_memory.py --live --ws <folder>   # also the live verb on a copy of the Canary fixture that is
                                                        # already open in Power BI Desktop; run it with the same
                                                        # PBZ_PRIVATE_ROOT / PBZ_SYNTH_ROOT as the pbz session there

The live part queries only the Desktop instance that holds <folder>\\Canary.pbip, and only after `connect` shows the
fixture's tables. It asserts that every band comes from the closed set, that no output holds a JSON number or a string
outside the closed vocabulary (bands, enums, model object names), and that no canary listed in tests\\fixture\\CANARIES.md
appears in engine stdout/stderr, the wrapper result or the saved .pbz/memory.json. This file contains no canary literal.
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import jsonschema

S = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(S))
FIXTURE = S / "tests" / "fixture"
EXE = S / "engine" / "bin" / "pbz-engine.exe"
SCHEMA = json.loads((S / "engine" / "contract.schema.json").read_text("utf-8"))
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)

# canary lists, scan patterns and the fixture table set live in the engine test
_spec = importlib.util.spec_from_file_location("pbz_test_engine", S / "tests" / "test_engine.py")
TE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(TE)

from pbz import desktop, engine as engine_mod, memory, tmdl  # noqa: E402
from pbz.common import PbzError, Project, Workspace  # noqa: E402

SIZE = ("<1 MiB", "1-10 MiB", "10-100 MiB", "100-1000 MiB", ">=1000 MiB")
SHARE = ("<1%", "1-5%", "5-20%", ">=20%")
ENUMS = {"FULL", "PARTIAL", "OFF", "DONE", "memory"}
PRED_KEYS = {"time_part", "fractional", "long_text"}
PRED_OF_TYPE = {"dateTime": "time_part", "double": "fractional", "decimal": "fractional", "string": "long_text"}

RESULTS: list[tuple[str, bool]] = []
OUTPUTS: list[tuple[str, str]] = []  # agent-visible text, scanned for canaries at the end


def expect(name: str, cond, detail=None) -> bool:
    ok = bool(cond)
    RESULTS.append((name, ok))
    line = ("PASS " if ok else "FAIL ") + name
    if not ok and detail is not None:
        try:
            line += " :: " + TE.redact(json.dumps(detail, ensure_ascii=False, default=str))[:700]
        except Exception:
            line += " :: (detail not printable)"
    print(line, flush=True)
    return ok


def engine(req: dict | None, label: str, raw: bytes | None = None) -> dict | None:
    payload = raw if raw is not None else json.dumps(req, ensure_ascii=False).encode("utf-8")
    p = subprocess.run([str(EXE), "memory"], input=payload, capture_output=True, timeout=900)
    OUTPUTS.append((label, p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")))
    resp = None
    try:
        resp = json.loads(p.stdout.decode("utf-8"))
    except ValueError:
        pass
    errors = [] if resp is None else [e.message[:120] for e in VALIDATOR.iter_errors(resp)][:3]
    expect(f"[{label}] envelope: exit 0, no stderr, schema-valid",
           p.returncode == 0 and p.stderr == b"" and resp is not None and not errors,
           {"exit": p.returncode, "stderr_bytes": len(p.stderr), "schema": errors, "resp": resp})
    return resp


# --------------------------------------------------------------------------- closed-vocabulary helpers


def walk(node, path: str = ""):
    yield path, node
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f"{path}/{i}")


def numbers(node) -> list[str]:
    return [p for p, v in walk(node) if isinstance(v, (int, float)) and not isinstance(v, bool)]


def bad_bands(node) -> list[str]:
    out = []
    for p, v in walk(node):
        if (p.endswith("/size_band") and v not in SIZE) or (p.endswith("/share_band") and v not in SHARE):
            out.append(p)
    return out


def stray_strings(node, allowed: set[str]) -> list[str]:
    return [p for p, v in walk(node) if isinstance(v, str) and v not in allowed]


def name_sorted(items: list[dict]) -> bool:
    names = [i.get("name", "") for i in items]
    return names == sorted(names, key=lambda n: (n.lower(), n))


def all_sorted(res: dict) -> bool:
    return (name_sorted(res.get("tables", [])) and name_sorted(res.get("relationships", []))
            and all(name_sorted(t.get("columns", [])) and name_sorted(t.get("hierarchies", [])) for t in res.get("tables", [])))


# --------------------------------------------------------------------------- offline


FAKE = {
    "ok": True, "verb": "memory",
    "model": {"size_band": "10-100 MiB", "bytes": 4242},
    "tables": [
        {"name": "Zeta", "size_band": "1-10 MiB", "share_band": "5-20%", "rows": 99,
         "columns": [{"name": "b", "size_band": "<1 MiB", "share_band": "<1%", "dictionary_heavy": True, "fractional": False, "raw": 5},
                     {"name": "a", "size_band": "2 MiB", "share_band": "<1%", "dictionary_heavy": "yes", "time_part": True}],
         "hierarchies": [{"name": "H", "size_band": "<1 MiB", "share_band": "<1%"}]},
        {"name": "alpha", "size_band": "<1 MiB", "share_band": "<1%", "columns": [], "hierarchies": []},
        {"name": 7, "columns": []},
    ],
    "relationships": [{"name": "r1", "from": "Zeta[b]", "to": "alpha[x]", "size_band": "<1 MiB", "segments": 3}],
    "other": {"size_band": "<1 MiB", "share_band": "<1%"},
    "auto_datetime": {"size_band": "<1 MiB"},
    "coverage": "MOSTLY", "predicates": "DONE", "debug": "raw text",
}

FAKE_CLOSED = {
    "model": {"size_band": "10-100 MiB"},
    "tables": [
        {"name": "alpha", "size_band": "<1 MiB", "share_band": "<1%", "columns": [], "hierarchies": []},
        {"name": "Zeta", "size_band": "1-10 MiB", "share_band": "5-20%",
         "columns": [{"name": "a", "share_band": "<1%", "time_part": True},
                     {"name": "b", "size_band": "<1 MiB", "share_band": "<1%", "dictionary_heavy": True, "fractional": False}],
         "hierarchies": [{"name": "H", "size_band": "<1 MiB"}]},
    ],
    "relationships": [{"name": "r1", "from": "Zeta[b]", "to": "alpha[x]", "size_band": "<1 MiB"}],
    "other": {"size_band": "<1 MiB", "share_band": "<1%"},
    "coverage": "PARTIAL", "predicates": "DONE",
}


def fixture_copy(tmp: Path, name: str) -> Workspace:
    ws_dir = tmp / name
    shutil.copytree(FIXTURE, ws_dir, ignore=shutil.ignore_patterns(".pbi", "pbz-workspace", ".pbz", "CANARIES.md"))
    ws = Workspace(str(ws_dir))
    ws.private = str(tmp / ("private-" + name) / ws.id)  # never the caller's private store
    return ws


def write_policy(ws: Workspace, pol: dict) -> None:
    os.makedirs(ws.pbz_dir, exist_ok=True)
    with open(ws.policy_path, "w", encoding="utf-8") as f:
        json.dump(pol, f)


def probes_used(ws: Workspace) -> int:
    try:
        with open(os.path.join(ws.private, "state.json"), encoding="utf-8") as f:
            return int(json.load(f).get("probes", 0))
    except (OSError, ValueError):
        return 0


def test_offline(tmp: Path) -> None:
    defs = SCHEMA["$defs"]
    expect("bands: wrapper, contract schema and the fixed policy lists agree",
           memory.SIZE_BANDS == SIZE == tuple(defs["sizeBand"]["enum"])
           and memory.SHARE_BANDS == SHARE == tuple(defs["storageShareBand"]["enum"]))
    expect("schema: memory is a oneOf branch and a known verb",
           {"$ref": "#/$defs/memory"} in SCHEMA["oneOf"] and "memory" in defs["verb"]["enum"])
    expect("sanity: this test, the wrapper and the engine source hold no canary literal",
           all(TE.canary_hits(p.read_text("utf-8"), TE.FILE_PATTERNS) == 0
               for p in (Path(__file__), S / "pbz" / "memory.py", S / "engine" / "Memory.cs")))

    # engine envelopes without Desktop
    pd = str(tmp / "pd")
    r = engine({"private_dir": pd, "port": 1}, "closed-port")
    expect("engine: closed port -> ENGINE_UNAVAILABLE, class and log id only",
           r and r.get("ok") is False and r.get("class") == "ENGINE_UNAVAILABLE" and set(r) <= {"ok", "verb", "class", "log_id"}, r)
    r = engine({"private_dir": pd}, "no-port")
    expect("engine: missing port -> BAD_REQUEST", r and r.get("class") == "BAD_REQUEST", r)
    r = engine({"private_dir": pd, "port": 1, "predicates": "yes"}, "bad-predicates")
    expect("engine: non-boolean predicates -> BAD_REQUEST (before connecting)", r and r.get("class") == "BAD_REQUEST", r)
    r = engine({"private_dir": pd, "port": 1, "disclosure": "loose"}, "bad-disclosure")
    expect("engine: unknown disclosure -> BAD_REQUEST", r and r.get("class") == "BAD_REQUEST", r)

    # closed(): only names, fixed bands, booleans and enums survive; lists sorted by name
    got = memory.closed(copy.deepcopy(FAKE))
    expect("closed(): drops numbers, unknown keys, bad bands and non-boolean flags; sorts by name", got == FAKE_CLOSED, got)
    expect("closed(): result holds no number", not numbers(got), numbers(got))
    expect("closed(): garbage input -> empty closed shape",
           memory.closed(None) == {"tables": [], "relationships": [], "coverage": "PARTIAL"})

    # wrapper: a fixture copy that no Desktop holds -> ENGINE_UNAVAILABLE, nothing saved, nothing charged
    ws = fixture_copy(tmp, "ws")
    project = ws.project()
    try:
        memory.run(ws, project)
        expect("wrapper: Desktop not open -> PbzError ENGINE_UNAVAILABLE", False, {"raised": None})
    except PbzError as e:
        expect("wrapper: Desktop not open -> PbzError ENGINE_UNAVAILABLE", e.code == "ENGINE_UNAVAILABLE", {"code": e.code})
    saved_path = Path(ws.pbz_dir) / "memory.json"
    expect("wrapper: nothing saved and no probe budget charged when Desktop is not open",
           not saved_path.exists() and probes_used(ws) == 0)

    real_port, real_call, real_exe = desktop.require_port, memory.engine_call, engine_mod.ENGINE_EXE
    calls: list[tuple[str, dict]] = []

    def fake_call(verb, req, timeout=120):
        calls.append((verb, req))
        return copy.deepcopy(FAKE)

    try:
        desktop.require_port = lambda project: 1
        # engine missing -> ENGINE_UNAVAILABLE
        engine_mod.ENGINE_EXE = S / "engine" / "bin" / "missing-engine.exe"
        try:
            memory.run(ws, project)
            expect("wrapper: engine missing -> PbzError ENGINE_UNAVAILABLE", False, {"raised": None})
        except PbzError as e:
            expect("wrapper: engine missing -> PbzError ENGINE_UNAVAILABLE", e.code == "ENGINE_UNAVAILABLE", {"code": e.code})
        engine_mod.ENGINE_EXE = real_exe
        expect("wrapper: a failed engine call saves nothing", not saved_path.exists())

        memory.engine_call = fake_call
        out = memory.run(ws, project)
        expect("wrapper run: verb, project, closed body, saved path",
               out == {"verb": "memory", "project": project.name, **FAKE_CLOSED, "saved": ".pbz/memory.json"}, out)
        req = calls[-1][1]
        expect("wrapper run: request carries private_dir, port, shape tier and predicates",
               calls[-1][0] == "memory" and req.get("private_dir") == ws.private and req.get("port") == 1
               and req.get("disclosure") == "shape" and req.get("predicates") is True, req)
        doc = json.loads(saved_path.read_text("utf-8"))
        expect("saved file: closed body plus saved_at, mirror_rev and disclosure; no numbers",
               {k: doc[k] for k in FAKE_CLOSED if k in doc} == FAKE_CLOSED
               and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", doc.get("saved_at", ""))
               and re.fullmatch(r"m[0-9a-f]{12}", doc.get("mirror_rev") or "") and doc.get("disclosure") == "shape"
               and not numbers(doc), doc)
        loaded = memory.load(ws, project)
        expect("load: fresh result is not stale", loaded and loaded.get("stale") is False
               and {k: loaded[k] for k in FAKE_CLOSED} == FAKE_CLOSED, loaded)
        mdef = Path(project.mirror_model) / "definition"
        mdef.mkdir(parents=True, exist_ok=True)
        (mdef / "model.tmdl").write_text("model Model\n", "utf-8")
        loaded = memory.load(ws, project)
        expect("load: a mirror edit after the run makes it stale", loaded and loaded.get("stale") is True, loaded)
        expect("load: another project's file -> None", memory.load(ws, Project(ws, "Other")) is None)

        memory.run(ws, project, predicates=False)
        expect("wrapper: predicates=False reaches the engine", calls[-1][1].get("predicates") is False, calls[-1][1])
        expect("load: a new run is current again", (memory.load(ws, project) or {}).get("stale") is False)
        write_policy(ws, {"disclosure": "strict"})
        memory.run(ws, project)
        expect("wrapper: strict policy -> strict request", calls[-1][1].get("disclosure") == "strict", calls[-1][1])
        write_policy(ws, {"disclosure": "loose"})
        memory.run(ws, project)
        expect("wrapper: malformed disclosure fails closed (strict)", calls[-1][1].get("disclosure") == "strict", calls[-1][1])
        saved_path.write_text("{not json", "utf-8")
        expect("load: corrupt file -> None", memory.load(ws, project) is None)

        # budget: each run is one probe; a spent budget answers BUDGET without calling the engine
        ws2 = fixture_copy(tmp, "ws2")
        write_policy(ws2, {"budgets": {"probes_per_day": 1}})
        n = len(calls)
        first = memory.run(ws2, ws2.project())
        second = memory.run(ws2, ws2.project())
        expect("budget: one run per probe unit, then BUDGET with no engine call",
               first.get("saved") and second.get("status") == "BUDGET" and len(calls) == n + 1 and probes_used(ws2) == 1,
               {"second": second, "calls": len(calls) - n})
    finally:
        desktop.require_port, memory.engine_call, engine_mod.ENGINE_EXE = real_port, real_call, real_exe


# --------------------------------------------------------------------------- live


def model_names(ws: Workspace, project: Project) -> tuple[tmdl.Model, set[str]]:
    """Object names of the fixture copy (from the mirror when present): the only strings memory may print."""
    mirror_def = Path(project.mirror_model) / "definition"
    model = tmdl.load_model(str(mirror_def if mirror_def.is_dir() else Path(project.model_dir) / "definition"))
    names = set(model.tables)
    for t in model.tables.values():
        names |= set(t.columns) | set(t.hierarchies)
        names |= {f"{t.name}[{c}]" for c in t.columns}
    names |= {r.name for r in model.relationships}
    return model, names


def test_live(ws_dir: Path, tmp: Path) -> None:
    ws = Workspace(str(ws_dir))
    project = ws.project()
    if not expect("live: the workspace is a copy of the Canary fixture", project.name == "Canary"):
        return
    inst = desktop.find_instance(project)
    port = desktop.engine_port(int(inst["pid"])) if inst and inst.get("pid") else None
    if not expect("live: a Desktop instance holds this copy and its engine port is known", port, inst):
        return
    model, allowed = model_names(ws, project)
    pd = str(tmp / "live-pd")
    p = subprocess.run([str(EXE), "connect"], input=json.dumps({"private_dir": pd, "port": port}).encode("utf-8"),
                       capture_output=True, timeout=300)
    OUTPUTS.append(("connect", p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")))
    conn = json.loads(p.stdout.decode("utf-8") or "{}")
    live_tables = set(conn.get("tables") or [])
    if not expect("live safety gate: TMSCHEMA tables are the fixture's (plus tables of this copy's model files)",
                  conn.get("ok") and TE.FIXTURE_TABLES <= live_tables <= set(model.tables), conn):
        return
    allowed |= set(SIZE) | set(SHARE) | ENUMS

    r = engine({"private_dir": pd, "port": port}, "memory") or {}
    tables = {t["name"]: t for t in r.get("tables", [])}
    cols = {(t["name"], c["name"]): c for t in r.get("tables", []) for c in t.get("columns", [])}
    expect("memory: ok, coverage FULL, predicates DONE, model band present",
           r.get("ok") and r.get("coverage") == "FULL" and r.get("predicates") == "DONE" and r.get("model", {}).get("size_band") in SIZE, r)
    expect("memory: every band is from the closed set", not bad_bands(r), bad_bands(r))
    expect("memory: no JSON number anywhere", not numbers(r), numbers(r))
    expect("memory: every string is a band, an enum or a model object name", not stray_strings(r, allowed),
           stray_strings(r, allowed))
    expect("memory: tables, columns, hierarchies and relationships are sorted by name", all_sorted(r))
    expect("memory: tables are exactly the user tables; each lists every column of the model",
           set(tables) == live_tables and all(set(c["name"] for c in tables[t]["columns"]) == set(model.tables[t].columns)
                                              for t in tables), sorted(tables))
    expect("memory: every table and column carries size and share bands; every column a dictionary_heavy boolean",
           all({"size_band", "share_band"} <= set(t) for t in tables.values())
           and all({"size_band", "share_band"} <= set(c) and isinstance(c.get("dictionary_heavy"), bool) for c in cols.values()))
    hier = {(t, h) for t, mt in model.tables.items() for h in mt.hierarchies}
    expect("memory: user hierarchies of the model are listed with a size band",
           hier == {(t["name"], h["name"]) for t in r.get("tables", []) for h in t.get("hierarchies", [])}
           and all("size_band" in h for t in r.get("tables", []) for h in t.get("hierarchies", [])),
           sorted(hier))
    rels = {(x.get("from"), x.get("to")) for x in r.get("relationships", [])}
    want = {(f"{m.from_table}[{m.from_column}]", f"{m.to_table}[{m.to_column}]") for m in model.relationships}
    expect("memory: every model relationship is listed with from / to and a size band",
           want and want == rels and all("size_band" in x for x in r.get("relationships", [])), sorted(rels))
    # predicates: the fixture is small, so every eligible column of an imported table has its boolean
    eligible = {(t, c): PRED_OF_TYPE.get(col.data_type) for t, mt in model.tables.items() for c, col in mt.columns.items()}
    missing = [k for k, key in eligible.items() if key and k in cols and key not in cols[k]]
    extra = [k for k, key in eligible.items() if k in cols and (set(cols[k]) & PRED_KEYS) - ({key} if key else set())]
    expect("predicates: every date/time, double/decimal and text column has exactly its own predicate; others none",
           not missing and not extra, {"missing": missing, "extra": extra})
    expect("predicates: planted decimals -> Hours[Hours] fractional; dates at midnight -> no time_part; short text",
           cols.get(("Hours", "Hours"), {}).get("fractional") is True
           and all(cols.get(k, {}).get("time_part") is False for k in (("Hours", "Date"), ("DimDate", "Date"), ("Project", "PlannedEnd")))
           and all(cols.get(k, {}).get("long_text") is False for k in (("Person", "Name"), ("Project", "Status"), ("ExceptionType", "Label"))),
           {k: cols.get(k) for k in (("Hours", "Hours"), ("Hours", "Date"), ("Person", "Name"))})

    r = engine({"private_dir": pd, "port": port, "disclosure": "strict"}, "memory-strict") or {}
    band_keys = [p for p, _ in walk(r) if p.endswith("_band") or p in ("/model", "/other", "/auto_datetime")]
    expect("memory strict: no band anywhere; names, dictionary_heavy, predicates and coverage stay",
           r.get("ok") and not band_keys and r.get("coverage") == "FULL" and r.get("predicates") == "DONE"
           and all(isinstance(c.get("dictionary_heavy"), bool) for t in r.get("tables", []) for c in t.get("columns", [])),
           band_keys)
    r = engine({"private_dir": pd, "port": port, "predicates": False}, "memory-nopred") or {}
    expect("memory predicates=false: predicates OFF and no predicate key",
           r.get("ok") and r.get("predicates") == "OFF"
           and not any(set(c) & PRED_KEYS for t in r.get("tables", []) for c in t.get("columns", [])), r.get("predicates"))

    # the wrapper on the same instance (charges one probe in this workspace's private store)
    try:
        out = memory.run(ws, project)
    except PbzError as e:
        expect("wrapper live: run succeeds", False, {"code": e.code})
        return
    OUTPUTS.append(("wrapper", json.dumps(out, ensure_ascii=False)))
    body = {k: v for k, v in out.items() if k not in ("verb", "project", "saved")}
    expect("wrapper live: verb, saved path, coverage FULL, closed bands, no numbers, no stray strings",
           out.get("verb") == "memory" and out.get("saved") == ".pbz/memory.json" and out.get("coverage") == "FULL"
           and not bad_bands(out) and not numbers(out) and not stray_strings(body, allowed),
           {"bands": bad_bands(out), "numbers": numbers(out), "stray": stray_strings(body, allowed)})
    saved = Path(ws.pbz_dir) / "memory.json"
    text = saved.read_text("utf-8") if saved.is_file() else ""
    OUTPUTS.append(("saved", text))
    doc = json.loads(text or "{}")
    meta = {k: doc.pop(k, None) for k in ("verb", "project", "saved_at", "mirror_rev", "disclosure")}
    expect("saved .pbz/memory.json: closed body plus timestamp and mirror revision",
           meta["verb"] == "memory" and meta["project"] == project.name and meta["disclosure"] in ("shape", "strict")
           and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", meta["saved_at"] or "")
           and re.fullmatch(r"m[0-9a-f]{12}", meta["mirror_rev"] or "")
           and not bad_bands(doc) and not numbers(doc) and not stray_strings(doc, allowed), meta)
    loaded = memory.load(ws, project)
    expect("load live: the fresh result is not stale", loaded and loaded.get("stale") is False, (loaded or {}).get("stale"))

    # the engine's private store for the direct calls: only DPAPI log lines
    bad = []
    for f in Path(pd).rglob("*"):
        if not f.is_file():
            continue
        if f.name == "engine.log":
            if not all(re.fullmatch(r"e-\d{8}-\d{4,}\t[A-Za-z0-9+/=]+", ln) for ln in f.read_text("utf-8").splitlines()):
                bad.append("engine.log format")
        elif TE.canary_hits(f.read_bytes().decode("latin-1"), TE.OUT_PATTERNS):
            bad.append(f.name)
    expect("live private_dir: nothing but DPAPI log lines", not bad, bad)


# --------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="also run the live verb (needs --ws)")
    ap.add_argument("--ws", help="workspace folder of a Canary fixture copy that is open in Power BI Desktop")
    args = ap.parse_args()
    if not EXE.is_file():
        print("FAIL engine not built: run dotnet publish (see engine/README.md)")
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="pbz-memory-test-"))
    try:
        test_offline(tmp)
        if args.live:
            if not args.ws or not (Path(args.ws) / "Canary.pbip").is_file():
                expect("live: --ws points at a folder holding Canary.pbip", False, {"ws": args.ws})
            else:
                test_live(Path(args.ws).resolve(), tmp)
        hits = [label for label, text in OUTPUTS if TE.canary_hits(text, TE.OUT_PATTERNS)]
        expect(f"canary scan: {len(OUTPUTS)} outputs (engine stdout/stderr, wrapper result, saved file) are clean",
               not hits, {"labels": hits})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
