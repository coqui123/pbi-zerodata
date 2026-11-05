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
           "synth": {**{k: info.get(k) for k in ("seed", "rows", "built", "tables")},
                     "profile": info.get("profile", "standard")},
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
    p0, p1 = s0.get("profile", "standard"), info.get("profile", "standard")
    if p0 != p1:
        comparable = False
        drift["profile"] = (f"rebuilt with the {p1} data profile (saved with {p0}): results are not comparable; "
                            f"rebuild with{' --edge' if p0 == 'edge' else 'out --edge'} or save again")
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


# ---------------------------------------------------------------- synth check: properties with counterexamples

CHECK_KINDS = {"executes", "nonblank", "nonempty", "equal", "differs", "filter_independent", "additive", "unique",
               "no_orphans", "subset", "no_blank_keys", "sign", "compiles"}
MAX_EXAMPLES = 10


def _spec_checks(spec) -> list[dict]:
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
            raise PbzError("BAD_REQUEST", 'pass {"checks": [...]} as JSON or a .json file')
    checks = data.get("checks") if isinstance(data, dict) else None
    if not isinstance(checks, list) or not checks:
        raise PbzError("BAD_REQUEST", 'the spec needs {"checks": [{"id", "kind", ...}]} (the pbz check format)')
    return [c for c in checks if isinstance(c, dict)]


def _calc(expr: str, where: list) -> str:
    return expr if not where else f"CALCULATE({expr}, {', '.join(where)})"


def _calctable(expr: str, where: list) -> str:
    return expr if not where else f"CALCULATETABLE({expr}, {', '.join(where)})"


def _column(model, ref) -> tuple[str, str, str]:
    """('T'[C] DAX reference, data type, 'T' table reference) for a column reference."""
    m = _REF.match(ref or "") if isinstance(ref, str) else None
    t = (m.group(1) or m.group(2) or "").replace("''", "'").strip() if m else ""
    col = model.column(t, m.group(3)) if m and t else None
    if col is None:
        raise PbzError("NOT_FOUND", f"column {ref}: use 'Table'[Column]")
    return (f"{lexers.dax_table_name(col.table)}[{col.name.replace(']', ']]')}]", (col.data_type or "").lower(),
            lexers.dax_table_name(col.table))


def _mismatch(n: int) -> str:
    return "NONE" if n == 0 else "ONE" if n == 1 else "FEW" if n <= 5 else "MANY"


def _same(a, b, tol: float) -> bool:
    """BLANK equals BLANK only; numbers within a relative tolerance; others by value (as the engine)."""
    if a is None or b is None:
        return a is None and b is None
    if _number(a) and _number(b):
        a, b = float(a), float(b)
        return a == b or abs(a - b) <= tol * max(abs(a), abs(b))
    return a == b


def _one(ws, project, dax: str) -> tuple[list | None, str | None]:
    r = _query(ws, project, dax)
    if r.get("error"):
        return None, r["error"]
    return (list(r["rows"][0]) if r["rows"] else []) + [None, None], None  # padded: ROW always answers one row


def _equal(ws, project, model, left, right, by, where, tol) -> dict:
    if not by:
        row, err = _one(ws, project, f"EVALUATE ROW(\"l\", {_calc(left, where)}, \"r\", {_calc(right, where)})")
        if err:
            return {"status": "ERROR", "class": err}
        ok = _same(row[0], row[1], tol)
        return {"status": "PASS" if ok else "FAIL", **({} if ok else {"counterexample": {"left": row[0], "right": row[1]}})}
    cols = [_column(model, b)[0] for b in by]
    r = _query(ws, project, "EVALUATE " + _calctable(
        f"SUMMARIZECOLUMNS({', '.join(cols)}, \"__pbz_l\", {left}, \"__pbz_r\", {right})", where))
    if r.get("error"):
        return {"status": "ERROR", "class": r["error"]}
    rows = r["rows"]
    if not rows:
        return {"status": "EMPTY", "mismatch": "NONE"}
    n = len(by)
    bad = [x for x in rows if not _same(x[n], x[n + 1], tol)]
    out = {"status": "PASS" if not bad else "FAIL", "mismatch": _mismatch(len(bad)), "groups_compared": len(rows)}
    if bad:
        out["failing_groups"] = len(bad)
        out["counterexample"] = [{"group": dict(zip(by, x[:n])), "left": x[n], "right": x[n + 1]}
                                 for x in sorted(bad, key=lambda x: json.dumps(x, default=str))[:MAX_EXAMPLES]]
    if r.get("truncated"):
        out["note"] = f"only the first {MAX_ROWS} groups were compared"
    return out


def _offenders(ws, project, count_dax: str, examples_dax: str) -> dict:
    row, err = _one(ws, project, count_dax)
    if err:
        return {"status": "ERROR", "class": err}
    n, o = (row + [None, None])[:2]
    if not n:
        return {"status": "EMPTY"}
    if not o:
        return {"status": "PASS", "rows": n}
    r = _query(ws, project, examples_dax)
    out = {"status": "FAIL", "rows": n, "offenders": o}
    if not r.get("error"):
        out["counterexample"] = [x[0] if len(x) == 1 else x for x in r["rows"][:MAX_EXAMPLES]]
    return out


def _check_one(ws, project, model, c: dict) -> dict:
    kind = c.get("kind")
    where = [synth.fake_dax_tokens(model, w) for w in c.get("where") or [] if isinstance(w, str)]
    tol = float(c.get("tol", 1e-9))
    f = {k: synth.fake_dax_tokens(model, c[k]) for k in ("expr", "left", "right") if isinstance(c.get(k), str)}
    by = [b for b in c.get("by") or [] if isinstance(b, str)]

    def need(*keys):
        for k in keys:
            if not isinstance(c.get(k), str) or not c[k].strip():
                raise PbzError("BAD_REQUEST", f"{kind} needs {k}")

    if kind == "executes":
        need("expr")
        row, err = _one(ws, project, f"EVALUATE ROW(\"v\", {_calc(f['expr'], where)})")
        return {"status": "ERROR", "class": err} if err else {"status": "PASS", "value": row[0]}
    if kind == "nonblank":
        need("expr")
        row, err = _one(ws, project, f"EVALUATE ROW(\"v\", {_calc(f['expr'], where)})")
        if err:
            return {"status": "ERROR", "class": err}
        out = {"status": "PASS" if row[0] is not None else "FAIL", "value": row[0]}
        if by and row[0] is not None:  # where is it blank below the total? (shown, not judged)
            cols = [_column(model, b)[0] for b in by]
            r = _query(ws, project, "EVALUATE " + _calctable(
                f"FILTER(ADDCOLUMNS(SUMMARIZECOLUMNS({', '.join(cols)}), \"__pbz_v\", CALCULATE({f['expr']})), "
                f"ISBLANK([__pbz_v]))", where))
            if not r.get("error") and r["rows"]:
                out["blank_groups"] = [dict(zip(by, x[:len(by)])) for x in r["rows"][:MAX_EXAMPLES]]
        return out
    if kind == "nonempty":
        need("expr")
        row, err = _one(ws, project, f"EVALUATE ROW(\"n\", COUNTROWS({_calctable(f['expr'], where)}))")
        if err:
            return {"status": "ERROR", "class": err}
        return {"status": "PASS" if row[0] else "FAIL", "rows": row[0] or 0}
    if kind in ("equal", "differs"):
        need("left", "right")
        out = _equal(ws, project, model, f["left"], f["right"], by, where, tol)
        if kind == "differs" and out["status"] in ("PASS", "FAIL"):
            out["status"] = "FAIL" if out["status"] == "PASS" else "PASS"
            if "counterexample" in out:  # where they differ is the evidence for a PASS
                out["differences"] = out.pop("counterexample")
            out["note"] = "differs: PASS when at least one value differs"
        elif kind == "equal" and out["status"] == "FAIL" and len(by) > 1:
            fails_at = []
            for b in by:  # shrink: the coarsest single-column grain that already breaks
                sub = _equal(ws, project, model, f["left"], f["right"], [b], where, tol)
                if sub["status"] == "FAIL":
                    fails_at.append(b)
            out["fails_at_single_column"] = fails_at
        return out
    if kind == "filter_independent":
        need("expr", "column")
        col = _column(model, c["column"])[0]
        return _equal(ws, project, model, f["expr"], f"CALCULATE({f['expr']}, REMOVEFILTERS({col}))", by, where, tol)
    if kind == "additive":
        need("expr")
        if not by:
            raise PbzError("BAD_REQUEST", "additive needs by")
        cols = [_column(model, b)[0] for b in by]
        row, err = _one(ws, project, f"EVALUATE ROW(\"t\", {_calc(f['expr'], where)})")
        if err:
            return {"status": "ERROR", "class": err}
        r = _query(ws, project, "EVALUATE " + _calctable(f"SUMMARIZECOLUMNS({', '.join(cols)}, \"__pbz_v\", {f['expr']})", where))
        if r.get("error"):
            return {"status": "ERROR", "class": r["error"]}
        vals = [x[len(by)] for x in r["rows"] if x[len(by)] is not None]
        if row[0] is None and not vals:
            return {"status": "EMPTY"}
        if any(not _number(v) for v in vals + ([row[0]] if row[0] is not None else [])):
            return {"status": "ERROR", "class": "TYPE_MISMATCH"}
        total, s = float(row[0] or 0), float(sum(vals))
        ok = _same(total, s, tol)
        out = {"status": "PASS" if ok else "FAIL", "total": row[0], "sum_of_groups": s, "groups": len(r["rows"])}
        if not ok:
            out["counterexample"] = [{"group": dict(zip(by, x[:len(by)])), "value": x[len(by)]}
                                     for x in r["rows"][:MAX_EXAMPLES]]
        return out
    if kind == "unique":
        need("column")
        col, _d, t = _column(model, c["column"])
        dup = f"FILTER(ADDCOLUMNS(DISTINCT({col}), \"__pbz_n\", CALCULATE(COUNTROWS({t}))), [__pbz_n] > 1)"
        return _offenders(ws, project,
                          f"EVALUATE ROW(\"n\", {_calc(f'COUNTROWS({t})', where)}, \"o\", {_calc(f'COUNTROWS({dup})', where)})",
                          f"EVALUATE TOPN({MAX_EXAMPLES}, {_calctable(dup, where)}, [__pbz_n], DESC)")
    if kind in ("no_orphans", "subset"):
        a_key, b_key = ("column", "of") if kind == "subset" else ("from", "to")
        need(a_key, b_key)
        a, da, _ta = _column(model, c[a_key])
        b = _column(model, c[b_key])[0]
        fa = f"FILTER(DISTINCT({a}), {a} <> \"\")" if da == "string" else f"FILTER(DISTINCT({a}), NOT ISBLANK({a}))"
        orphans = f"EXCEPT({fa}, DISTINCT({b}))"
        return _offenders(ws, project,
                          f"EVALUATE ROW(\"n\", {_calc(f'COUNTROWS({fa})', where)}, \"o\", {_calc(f'COUNTROWS({orphans})', where)})",
                          f"EVALUATE TOPN({MAX_EXAMPLES}, {_calctable(orphans, where)})")
    if kind == "no_blank_keys":
        need("column")
        col, dtp, t = _column(model, c["column"])
        pred = f"{col} = \"\"" if dtp == "string" else f"ISBLANK({col})"
        out = _offenders(ws, project,
                         f"EVALUATE ROW(\"n\", {_calc(f'COUNTROWS({t})', where)}, \"o\", {_calc(f'CALCULATE(COUNTROWS({t}), {pred})', where)})",
                         f"EVALUATE TOPN(5, {_calctable(f'CALCULATETABLE({t}, {pred})', where)})")
        if "counterexample" in out:
            out["counterexample"] = {"blank_rows": out.get("offenders"), "example_rows": out["counterexample"]}
        return out
    if kind == "sign":
        need("expr")
        row, err = _one(ws, project, f"EVALUATE ROW(\"v\", {_calc(f['expr'], where)})")
        if err:
            return {"status": "ERROR", "class": err}
        v = row[0]
        if v is not None and not _number(v):
            return {"status": "ERROR", "class": "TYPE_MISMATCH"}
        sign = "BLANK" if v is None else "NEG" if v < 0 else "ZERO" if v == 0 else "POS"
        expect = c.get("expect") or []
        expect = [expect] if isinstance(expect, str) else expect
        if any(e not in ("NEG", "ZERO", "POS", "BLANK") for e in expect):
            raise PbzError("BAD_REQUEST", "expect must be NEG, ZERO, POS or BLANK")
        return {"status": "PASS" if not expect or sign in expect else "FAIL", "sign": sign, "value": v}
    if kind == "compiles":
        need("expr")
        table = c.get("table") or next((t.name for t in model.tables.values() if not t.private), "")
        dax = (f"DEFINE MEASURE {lexers.dax_table_name(table)}[__pbz_c] = {f['expr']}\n"
               f"EVALUATE ROW(\"v\", [__pbz_c])")
        row, err = _one(ws, project, dax)
        return {"status": "ERROR", "class": err} if err else {"status": "PASS", "value": row[0]}
    raise PbzError("BAD_REQUEST", f"unknown kind {kind}")


def check(ws, project, spec) -> dict:
    """`pbz check` specs evaluated on the synthetic copy only, with counterexamples: the data are fake, so
    failing groups, both sides of a comparison, duplicate and orphan values are shown."""
    checks = _spec_checks(spec)
    info = synth.build_info(ws, project)  # UNSAFE unless the synthetic copy exists; no query before this
    model = tmdl.load_model(os.path.join(project.mirror_model, "definition"))
    results = []
    for i, c in enumerate(checks):
        cid = str(c.get("id") or f"c{i + 1}")
        kind = c.get("kind")
        if kind == "between":
            results.append({"id": cid, "kind": kind, "status": "UNSUPPORTED", "note": "between is not offered"})
            continue
        if kind not in CHECK_KINDS:
            results.append({"id": cid, "kind": kind, "status": "ERROR", "class": "BAD_REQUEST", "note": "unknown kind"})
            continue
        try:
            res = _check_one(ws, project, model, c)
        except PbzError as e:
            if e.code in ABORT:
                raise
            res = {"status": "ERROR", "class": e.code, **({"note": e.detail} if e.detail else {})}
        results.append({"id": cid, "kind": kind, **res})
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    out = {"verb": "synth-check", "project": project.name, "data": "synthetic", "profile": info.get("profile", "standard"),
           "summary": counts, "results": results}
    if _stale(project, info):
        out["drift"] = {"synth_stale": "the synthetic copy is older than the mirror model and still runs the old DAX: "
                                       "rebuild and reopen it (`pbz synth build`, `pbz synth open`)"}
    if info.get("profile", "standard") != "edge":
        out["hint"] = "pbz synth build --edge plants blanks, orphans, zero and negative values and boundary dates"
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
