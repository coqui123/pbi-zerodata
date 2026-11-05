"""`pbz synth test`: measure regression tests on the synthetic model.

Golden results come from the synthetic copy (`pbz synth build`, `pbz synth open`): fake rows, so the
values are stored and shown. Every query goes through `pbz synth query`, which needs the synthetic
folder and its own Desktop instance, and the engine's `synth-query` verb answers only for a model
carrying the HMAC marker of its own lineage tags with every partition calculated: a real model can
never answer. Tests live in <ws>/.pbz/synth-tests/<name>.json.

- save <name>: run a spec ({"queries": [{"id", "dax", "keys"?}]}) and/or a measure shortcut
  (measures, optionally grouped by columns) and keep the results, the synth build settings (seed,
  rows, tables) and a hash of the mirror model.
- run <name>: rerun and compare per query (SAME, DIFF with the changed rows and cells, ERROR:<class>),
  floats within a relative tolerance. Drift is reported apart: a synthetic copy rebuilt with another
  seed or row count makes results not comparable; a mirror model changed since the save is expected
  after a refactor; a synthetic copy older than the mirror model still runs the old DAX.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re

from . import lexers, synth, tmdl
from .common import PbzError, read_text, write_text

DIR = (".pbz", "synth-tests")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$")
MAX_ROWS = 500  # the engine caps synth-query results here
SHOW = 20
QUERY = synth.query  # (ws, project, dax, max_rows) -> {"columns", "rows", "truncated"}; tests replace it
ABORT = {"ENGINE_UNAVAILABLE", "UNSAFE"}  # environment problems: stop instead of recording an error
_REF = re.compile(r"^\s*(?:'((?:[^']|'')+)'|([^\[\]']*?))?\s*\[(.+)\]\s*$")


# ---------------------------------------------------------------- storage

def _dir(ws) -> str:
    return os.path.join(ws.root, *DIR)


def _path(ws, name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise PbzError("BAD_REQUEST", "a test name uses letters, digits, spaces, _ . - (up to 80)")
    return os.path.join(_dir(ws), name + ".json")


def _load(ws, name: str) -> dict:
    path = _path(ws, name)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        raise PbzError("NOT_FOUND", f"synth test {name} (`pbz synth test list`)")
    except ValueError:
        raise PbzError("BAD_REQUEST", f"synth test {name} is not valid JSON")


def _model_files(project) -> list[str]:
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    return [p for p in tmdl.tmdl_files(mdef)
            if not os.path.relpath(p, mdef).replace("\\", "/").lower().startswith("cultures/")]


def model_hash(project) -> str:
    """Hash of the mirror model definition (translations excluded: they do not change results)."""
    mdef = os.path.join(project.mirror_model, "definition")
    h = hashlib.sha256()
    for p in _model_files(project):
        h.update(os.path.relpath(p, mdef).replace("\\", "/").encode("utf-8") + b"\0")
        h.update(read_text(p)[0].replace("\r\n", "\n").encode("utf-8") + b"\0")
    return h.hexdigest()[:16]


def _stale(project, info: dict) -> bool:
    """Was the mirror model edited after the synthetic copy was built?"""
    try:
        built = dt.datetime.fromisoformat(str(info.get("built"))).timestamp()
    except ValueError:
        return False
    return any(os.path.getmtime(p) > built + 1 for p in _model_files(project))


# ---------------------------------------------------------------- queries

def _dax_ok(dax) -> bool:
    text = re.sub(r"/\*.*?\*/|//[^\n]*|--[^\n]*", "", dax if isinstance(dax, str) else "", flags=re.S).strip()
    return bool(re.match(r"(?i)^(define|evaluate)\b", text))


def _spec_queries(spec) -> list[dict]:
    if spec is None:
        return []
    data = spec
    if isinstance(spec, str):
        s = spec.strip()
        if s.startswith("{"):
            try:
                data = json.loads(s)
            except ValueError:
                raise PbzError("BAD_REQUEST", "the spec is not valid JSON")
        elif os.path.isfile(s):
            try:
                with open(s, encoding="utf-8-sig") as f:
                    data = json.load(f)
            except ValueError:
                raise PbzError("BAD_REQUEST", "the spec file is not valid JSON")
        else:
            raise PbzError("BAD_REQUEST", "the spec is a JSON object or a .json file")
    queries = data.get("queries") if isinstance(data, dict) else None
    if not isinstance(queries, list) or not queries:
        raise PbzError("BAD_REQUEST", 'the spec needs {"queries": [{"id": ..., "dax": "EVALUATE ..."}]}')
    out = []
    for q in queries:
        if not isinstance(q, dict) or not isinstance(q.get("id"), str) or not q["id"].strip():
            raise PbzError("BAD_REQUEST", "every query needs an id")
        if not _dax_ok(q.get("dax")):
            raise PbzError("BAD_REQUEST", f"{q['id']}: dax must start with DEFINE or EVALUATE")
        rec = {"id": q["id"].strip(), "dax": q["dax"]}
        if isinstance(q.get("keys"), (int, list)) and not isinstance(q.get("keys"), bool):
            rec["keys"] = q["keys"]
        out.append(rec)
    return out


def measure_query(project, measures: list[str], by: list[str] | None = None) -> dict:
    """A query for measures by name, optionally grouped by columns (leading key columns)."""
    model = tmdl.load_model(os.path.join(project.mirror_model, "definition"))
    names = []
    for ref in measures:
        m = _REF.match(ref)
        mm = model.measure(m.group(3) if m else ref.strip())
        if mm is None:
            raise PbzError("NOT_FOUND", f"measure {ref}")
        names.append(mm.name)
    cols = []
    for ref in by or []:
        m = _REF.match(ref)
        t = (m.group(1) or m.group(2) or "").replace("''", "'").strip() if m else ""
        col = model.column(t, m.group(3)) if m and t else None
        if col is None:
            raise PbzError("NOT_FOUND", f"column {ref}: use 'Table'[Column]")
        cols.append(f"{lexers.dax_table_name(col.table)}[{col.name.replace(']', ']]')}]")
    pairs = [f"{lexers.dax_string(n)}, [{n.replace(']', ']]')}]" for n in names]
    if cols:
        dax = "EVALUATE\nSUMMARIZECOLUMNS(\n    " + ",\n    ".join(cols + pairs) + "\n)\nORDER BY " + ", ".join(cols)
    else:
        dax = "EVALUATE ROW(" + ", ".join(pairs) + ")"
    label = ", ".join(f"[{n}]" for n in names) + (" by " + ", ".join(by) if by else "")
    return {"id": label[:120], "dax": dax, "keys": len(cols)}


def _query(ws, project, dax: str) -> dict:
    try:
        r = QUERY(ws, project, dax, MAX_ROWS)
    except PbzError as e:
        if e.code in ABORT:
            raise
        return {"error": e.code}
    return {"columns": list(r.get("columns") or []), "rows": [list(x) for x in r.get("rows") or []],
            "truncated": bool(r.get("truncated"))}


# ---------------------------------------------------------------- comparison

def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _eq(a, b, tol: float) -> bool:
    if _number(a) and _number(b):
        return math.isclose(float(a), float(b), rel_tol=tol, abs_tol=1e-12)
    return a == b


def _key_index(keys, columns: list, rows: list) -> list[int]:
    """Key columns: a count of leading columns, a list of column names, or (default) every column
    whose golden values are all text, dates or blanks."""
    if isinstance(keys, int):
        return list(range(min(keys, len(columns))))
    if isinstance(keys, list):
        return [columns.index(k) for k in keys if k in columns]
    return [i for i in range(len(columns))
            if rows and all(r[i] is None or isinstance(r[i], str) or isinstance(r[i], bool) for r in rows)
            and len(columns) > 1]


def compare(gold: dict, now: dict, tol: float = 1e-9) -> dict:
    if now.get("error"):
        if gold.get("error") == now["error"]:
            return {"result": "SAME", "note": f"still ERROR:{now['error']}"}
        return {"result": f"ERROR:{now['error']}"}
    if gold.get("error"):
        return {"result": "DIFF", "note": f"was ERROR:{gold['error']}, now returns {len(now['rows'])} row(s)"}
    gc, nc = gold.get("columns", []), now.get("columns", [])
    if gc != nc:
        return {"result": "DIFF", "columns": {"was": gc, "now": nc}}
    g_rows, n_rows = gold.get("rows", []), now.get("rows", [])
    kidx = _key_index(gold.get("keys"), gc, g_rows)
    vidx = [i for i in range(len(gc)) if i not in kidx]
    missing, added, cells = [], [], []

    def kv(row):
        return json.dumps([row[i] for i in kidx], ensure_ascii=False, default=str)

    gm, nm = {kv(r): r for r in g_rows}, {kv(r): r for r in n_rows}
    if kidx and len(gm) == len(g_rows) and len(nm) == len(n_rows):
        missing = [json.loads(k) for k in gm if k not in nm]
        added = [json.loads(k) for k in nm if k not in gm]
        pairs = [(json.loads(k), gm[k], nm[k]) for k in gm if k in nm]
    else:  # no usable key: compare the sorted rows position by position
        gs = sorted(g_rows, key=lambda r: json.dumps(r, default=str))
        ns = sorted(n_rows, key=lambda r: json.dumps(r, default=str))
        pairs = [(i, a, b) for i, (a, b) in enumerate(zip(gs, ns))]
        if len(gs) > len(ns):
            missing = [{"row": r} for r in gs[len(ns):]]
        elif len(ns) > len(gs):
            added = [{"row": r} for r in ns[len(gs):]]
        vidx = list(range(len(gc)))
    for key, a, b in pairs:
        for i in vidx:
            if not _eq(a[i], b[i], tol):
                cells.append({"key": key, "column": gc[i], "was": a[i], "now": b[i]})
    if not (missing or added or cells):
        return {"result": "SAME"}
    out = {"result": "DIFF", "rows": {"golden": len(g_rows), "now": len(n_rows)}}
    if cells:
        out["changed_cells"] = len(cells)
        out["cells"] = cells[:SHOW]
    if missing:
        out["missing_rows"] = missing[:SHOW]
    if added:
        out["added_rows"] = added[:SHOW]
    return out


# ---------------------------------------------------------------- verbs

def save(ws, project, name: str, spec=None, measures: list[str] | None = None, by: list[str] | None = None,
         replace: bool = False) -> dict:
    path = _path(ws, name)
    if os.path.exists(path) and not replace:
        raise PbzError("NAME_TAKEN", f"synth test {name} exists: pass --replace or pick another name")
    queries = _spec_queries(spec)
    if measures:
        queries.append(measure_query(project, measures, by))
    elif by:
        raise PbzError("BAD_REQUEST", "--by groups measures: pass --measure too")
    if not queries:
        raise PbzError("BAD_REQUEST", "pass --spec and/or --measure")
    ids = [q["id"] for q in queries]
    if len(set(ids)) != len(ids):
        raise PbzError("BAD_REQUEST", "query ids must be unique")
    info = synth.build_info(ws, project)  # UNSAFE unless the synthetic copy exists
    stale = _stale(project, info)
    results = []
    for q in queries:
        r = _query(ws, project, q["dax"])
        if r.get("truncated"):
            raise PbzError("TOO_MANY_ROWS", f"{q['id']}: more than {MAX_ROWS} rows; group by fewer columns or use TOPN")
        rec = dict(q, **r)
        rec.pop("truncated", None)
        if isinstance(rec.get("keys"), list) and rec.get("columns"):
            missing = [k for k in rec["keys"] if k not in rec["columns"]]
            if missing:
                raise PbzError("BAD_REQUEST", f"{q['id']}: key columns not in the result: {', '.join(missing)}")
        results.append(rec)
    doc = {"name": name, "saved": dt.datetime.now().isoformat(timespec="seconds"),
           "synth": {k: info.get(k) for k in ("seed", "rows", "built", "tables")},
           "model_hash": model_hash(project), "queries": results}
    write_text(path, json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    out = {"verb": "synth-test", "action": "save", "name": name, "data": "synthetic",
           "queries": [{"id": r["id"], **({"error": r["error"]} if r.get("error") else
                                          {"rows": len(r["rows"]), "columns": len(r["columns"])})} for r in results],
           "file": os.path.relpath(path, ws.root).replace("\\", "/"),
           "next": f"after a refactor: pbz synth build, pbz synth open, pbz synth test run {name}"}
    if stale:
        out["warning"] = ("the synthetic copy is older than the mirror model, so these results come from the old "
                          "DAX: rebuild it (`pbz synth build`, `pbz synth open`) and save again with --replace")
    return out


def run(ws, project, name: str, tol: float = 1e-9) -> dict:
    doc = _load(ws, name)
    info = synth.build_info(ws, project)
    drift, comparable = {}, True
    s0 = doc.get("synth") or {}
    if (s0.get("seed"), s0.get("rows")) != (info.get("seed"), info.get("rows")):
        comparable = False
        drift["synth"] = (f"rebuilt with seed {info.get('seed')} and {info.get('rows')} rows (saved with seed "
                          f"{s0.get('seed')} and {s0.get('rows')} rows): results are not comparable; rebuild with "
                          f"`pbz synth build --seed {s0.get('seed')} --rows {s0.get('rows')}` or save again")
    if doc.get("model_hash") != model_hash(project):
        drift["model"] = "the mirror model changed since the save: expected after a refactor, review each DIFF"
    if _stale(project, info):
        drift["synth_stale"] = ("the synthetic copy is older than the mirror model and still runs the old DAX: "
                                "rebuild and reopen it (`pbz synth build`, `pbz synth open`)")
    results = []
    for q in doc.get("queries", []):
        cmp = compare(q, _query(ws, project, q["dax"]), tol)
        results.append({"id": q["id"], **cmp})
    counts: dict[str, int] = {}
    for r in results:
        k = r["result"] if not r["result"].startswith("ERROR") else "ERROR"
        counts[k] = counts.get(k, 0) + 1
    verdict = ("NOT_COMPARABLE" if not comparable else "SAME" if counts.get("SAME", 0) == len(results)
               else "DIFF" if counts.get("DIFF") else "ERROR")
    out = {"verb": "synth-test", "action": "run", "name": name, "result": verdict, "summary": counts,
           "tolerance": tol, "queries": results, "data": "synthetic"}
    if drift:
        out["drift"] = drift
    return out


def list_(ws) -> dict:
    tests = []
    d = _dir(ws)
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, f), encoding="utf-8") as h:
                doc = json.load(h)
        except (OSError, ValueError):
            tests.append({"name": f[:-5], "error": "unreadable"})
            continue
        s = doc.get("synth") or {}
        tests.append({"name": doc.get("name", f[:-5]), "saved": doc.get("saved"), "queries": len(doc.get("queries", [])),
                      "seed": s.get("seed"), "rows": s.get("rows")})
    return {"verb": "synth-test", "action": "list", "tests": tests}


def remove(ws, name: str) -> dict:
    path = _path(ws, name)
    if not os.path.exists(path):
        raise PbzError("NOT_FOUND", f"synth test {name}")
    os.remove(path)
    return {"verb": "synth-test", "action": "remove", "name": name, "result": "REMOVED"}
