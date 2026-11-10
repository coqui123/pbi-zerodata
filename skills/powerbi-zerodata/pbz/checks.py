"""Data-blind assertions, probes and snapshots through pbz-engine, with budgets and constant limits."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import sys

from . import desktop, dpapi, lexers, policy as policy_mod, tmdl, tokens
from .classify import Classifier
from .common import PbzError, engine_call

ENGINE_KINDS = {"executes", "nonblank", "nonempty", "equal", "additive", "unique", "no_orphans",
                "no_blank_keys", "sign", "subset"}
ALL_KINDS = ENGINE_KINDS | {"differs", "filter_independent", "compiles", "rls", "perf"}
EXPR_FIELDS = ("expr", "left", "right", "column", "from", "to", "of")
RLS_EXPECT = ("EMPTY", "NON_EMPTY", "LESS", "EQUAL", "OLS_INACCESSIBLE")
_RLS_TEXT = ("query", "role", "user", "column", "table")
_CHECK_STATUS = {"PASS", "FAIL", "EMPTY", "UNSUPPORTED", "ERROR"}

CHECK_REQUIRED = {
    **{k: ("expr",) for k in ("executes", "nonblank", "nonempty", "sign", "compiles")},
    **{k: ("left", "right") for k in ("equal", "differs")},
    **{k: ("column",) for k in ("unique", "no_blank_keys")},
    "additive": ("expr", "by"), "no_orphans": ("from", "to"),
    "subset": ("column", "of"), "filter_independent": ("expr", "column"),
}


def check_schema() -> dict:
    """Public, dependency-free schema for check files. `subset.of` names a column.

    `rls` and `perf` are separate branches. role, query, user and baseline are not expression fields.
    """
    branches = [{"type": "object", "required": ["kind", *required], "properties": {
        "kind": {"const": kind}, "id": {"type": "string", "minLength": 1},
        **{k: {"type": "string", "minLength": 1} for k in EXPR_FIELDS},
        "table": {"type": "string", "minLength": 1},
        "by": {"type": "array", "items": {"type": "string", "minLength": 1},
               **({"minItems": 1} if kind == "additive" else {})},
        "where": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "tol": {"type": "number", "minimum": 0},
        "expect": {"type": "array", "items": {"enum": ["NEG", "ZERO", "POS", "BLANK"]}},
    }} for kind, required in CHECK_REQUIRED.items()]
    branches.append({"type": "object", "required": ["kind", "expect"], "properties": {
        "kind": {"const": "rls"}, "id": {"type": "string", "minLength": 1},
        "expect": {"enum": list(RLS_EXPECT)},
        **{k: {"type": "string", "minLength": 1} for k in _RLS_TEXT},
    }})
    branches.append({"type": "object", "required": ["kind", "query"], "properties": {
        "kind": {"const": "perf"}, "id": {"type": "string", "minLength": 1},
        "query": {"type": "string", "minLength": 1}, "expect": {"const": "NOT_SLOWER"},
    }})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
            "required": ["checks"], "properties": {"checks": {"type": "array", "items": {"oneOf": branches}}}}


def _human_terminal() -> bool:
    """Local copy of the CLI human gate. Do not import pbz.cli (that imports this module)."""
    return sys.stdin.isatty() and sys.stdout.isatty() and not any(
        os.environ.get(name) for name in ("CLAUDECODE", "CODEX_SANDBOX", "CODEX_MANAGED_BY_NPM"))


def _rls_synthetic(ws, project, pol: dict) -> bool:
    """True only for a verified pbz-synth tree (policy, synth.json, and the lineage HMAC).

    A policy flag or an annotation named PBZ_SYNTHETIC is not enough: either can be written
    onto a real model. `project` and `pol` stay in the signature for callers.
    """
    del project, pol
    if ws is None or not getattr(ws, "root", None):
        return False
    from . import synth
    return synth._verified_owned(ws, ws.root)


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _perf_check(ws, project, raw: dict, cid: str) -> dict:
    """perf.expect spends the probe budget. This does not also spend a check."""
    from . import perf
    query = raw.get("query") if isinstance(raw.get("query"), str) else ""
    try:
        got = perf.expect(ws, project, query)
    except PbzError as err:
        return {"id": cid, "status": "ERROR", "class": _class_of(err.code, _PY_CLASS) or "OTHER"}
    status = got.get("status") if got.get("status") in _CHECK_STATUS else "ERROR"
    verdict_ = got.get("verdict") if got.get("verdict") in ("FASTER", "SAME", "SLOWER", "NOISY", "UNAVAILABLE") else "UNAVAILABLE"
    return {"id": cid, "status": status, "verdict": verdict_}


_ERROR_CLASS = frozenset({
    "SYNTAX", "UNKNOWN_OBJECT", "TYPE_MISMATCH", "CONVERSION", "CIRCULAR", "DUPLICATE_KEY",
    "TIMEOUT", "ENGINE_UNAVAILABLE", "BAD_REQUEST", "OTHER",
})
_PY_CLASS = _ERROR_CLASS | frozenset({"HUMAN_ONLY", "ENGINE_MISMATCH", "REFUSED", "UNSAFE", "NO_MIRROR", "NOT_FOUND"})
_LOG_ID = re.compile(r"^e-[0-9]{8}-[0-9]{4,}$")
_COUNT_BAND = frozenset({"0", "1-9", "10-99", "100-999", "1k-9k", "10k+"})
_MISMATCH = frozenset({"NONE", "ONE", "FEW", "MANY"})
_SIGN = frozenset({"NEG", "ZERO", "POS", "BLANK"})


def _class_of(value, allowed: frozenset) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _rls_clean(row: dict) -> dict:
    status = row.get("status") if row.get("status") in _CHECK_STATUS else "ERROR"
    out = {"id": row.get("id"), "status": status}
    klass = _class_of(row.get("class"), _ERROR_CLASS)
    if klass is not None:
        out["class"] = klass
    elif status == "ERROR":
        out["class"] = "OTHER"
    return out


def _closed_engine_row(row: dict, allowed_ids: set[str]) -> dict | None:
    """Engine check rows: submitted ids and the contract fields only."""
    ident = row.get("id")
    if not isinstance(ident, str) or ident not in allowed_ids:
        return None
    status = row.get("status") if row.get("status") in _CHECK_STATUS else "ERROR"
    out = {"id": ident, "status": status}
    klass = _class_of(row.get("class"), _ERROR_CLASS)
    if klass is not None:
        out["class"] = klass
    elif status == "ERROR":
        out["class"] = "OTHER"
    log_id = row.get("log_id")
    if isinstance(log_id, str) and _LOG_ID.fullmatch(log_id):
        out["log_id"] = log_id
    if row.get("mismatch") in _MISMATCH:
        out["mismatch"] = row["mismatch"]
    if row.get("offenders_bucket") in _COUNT_BAND:
        out["offenders_bucket"] = row["offenders_bucket"]
    if row.get("sign") in _SIGN:
        out["sign"] = row["sign"]
    if row.get("note") == "differs":
        out["note"] = "differs"
    groups = row.get("groups")
    if isinstance(groups, list) and groups:
        out["groups"] = groups
    return out


def validate_spec(spec) -> dict:
    """Check request shape before opening private stores or charging disclosure budget.

    Diagnostics disclose field paths and closed messages, never submitted expressions/values.
    Runtime DAX/object validity remains the engine's responsibility.
    """
    issues = []

    def issue(index, field, message):
        issues.append({"id": f"c{index + 1}", "path": f"checks[{index}].{field}",
                       "message": message})

    if not isinstance(spec, dict) or not isinstance(spec.get("checks"), list):
        return {"valid": False, "findings": [{"id": "spec", "path": "checks",
                                              "message": "checks must be an array"}]}
    seen = set()
    for index, check in enumerate(spec["checks"]):
        if not isinstance(check, dict):
            issue(index, "kind", "each check must be an object")
            continue
        ident = check.get("id", f"c{index + 1}")
        if not isinstance(ident, str) or not ident.strip():
            issue(index, "id", "id must be a nonempty string")
        elif ident in seen:
            issue(index, "id", "check ids must be unique")
        else:
            seen.add(ident)
        kind = check.get("kind")
        if kind == "rls":
            if check.get("expect") not in RLS_EXPECT:
                issue(index, "expect", "rls expect is EMPTY, NON_EMPTY, LESS, EQUAL or OLS_INACCESSIBLE")
            for field in _RLS_TEXT:
                if field in check and not _nonempty(check.get(field)):
                    issue(index, field, "field must be a nonempty string")
            continue
        if kind == "perf":
            if "expect" in check and check.get("expect") != "NOT_SLOWER":
                issue(index, "expect", "perf expect is NOT_SLOWER")
            if not _nonempty(check.get("query")):
                issue(index, "query", "required field must be a nonempty string")
            continue
        if not isinstance(kind, str) or kind not in CHECK_REQUIRED:
            issue(index, "kind", "unknown check kind")
            continue
        for field in CHECK_REQUIRED[kind]:
            value = check.get(field)
            if field == "by":
                if not isinstance(value, list) or not value:
                    issue(index, field, "additive requires a nonempty grouping list")
            elif not isinstance(value, str) or not value.strip():
                issue(index, field, "required field must be a nonempty string")
        for field in (*EXPR_FIELDS, "table"):
            if field in check and (not isinstance(check[field], str) or not check[field].strip()):
                issue(index, field, "field must be a nonempty string")
        for field in ("where", "by"):
            if field in check and (not isinstance(check[field], list) or
                                   any(not isinstance(x, str) or not x.strip() for x in check[field])):
                issue(index, field, "field must be an array of nonempty strings")
        if "tol" in check and (isinstance(check["tol"], bool) or not isinstance(check["tol"], (int, float)) or
                               (isinstance(check["tol"], float) and not math.isfinite(check["tol"])) or check["tol"] < 0):
            issue(index, "tol", "tolerance must be a finite nonnegative number")
        if "expect" in check and (kind != "sign" or not isinstance(check["expect"], list) or
                                  any(x not in ("NEG", "ZERO", "POS", "BLANK") for x in check["expect"])):
            issue(index, "expect", "sign expectations must use closed sign categories")
    return {"valid": not issues, "findings": issues}


def _state_path(ws) -> str:
    return os.path.join(ws.private, "state.json")


LEDGER_DAYS = 30
DAILY = {"checks": ("session_checks", 300), "probes": ("probes_per_day", 100),
         "snapshot_queries": ("snapshot_queries_per_day", 200), "smoke": ("smoke_runs_per_day", 50)}


def _load_state(ws) -> dict:
    """Daily counters (reset each day) plus a persistent ledger: distinct constants per target fingerprint
    for LEDGER_DAYS and hashes of literals the agent introduced through pushes."""
    today = dt.date.today().isoformat()
    try:
        with open(_state_path(ws), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    ledger = st.get("ledger") or {"constants": {}, "authored": []}
    for th, hs in (st.get("constants") or {}).items():  # migrate the old daily constants
        if st.get("day") == today:
            ledger["constants"].setdefault(th, []).extend({"h": h, "day": today} for h in hs)
    if st.get("day") != today:
        st = {"day": today, "per_target": {}}
    st.pop("constants", None)
    for k in DAILY:
        st.setdefault(k, 0)
    cutoff = (dt.date.today() - dt.timedelta(days=LEDGER_DAYS)).isoformat()
    ledger["constants"] = {th: [e for e in es if e.get("day", today) >= cutoff]
                           for th, es in ledger["constants"].items()}
    ledger["constants"] = {th: es for th, es in ledger["constants"].items() if es}
    st["ledger"] = ledger
    return st


def spend(ws, kind: str, n: int = 1) -> bool:
    """Charge n units of a daily disclosure budget (checks, probes, snapshot_queries, smoke). False when spent."""
    from . import policy as policy_mod
    budgets = policy_mod.load(ws).get("budgets", {})
    st = _load_state(ws)
    key, default = DAILY[kind]
    if st[kind] + n > int(budgets.get(key, default)):
        return False
    st[kind] += n
    _save_state(ws, st)
    return True


def record_authored(ws, before: dict, after: dict) -> int:
    """Remember (as hashes) the literals a push introduced into DAX / M, so later checks that reach them
    through measure definitions count them as constants. before/after: {rel: sanitized text}."""
    def lits(text: str, rel: str) -> set[str]:
        out = set()
        doc = tmdl.parse(rel, text)
        for reg in doc.regions:
            if reg.kind not in ("dax", "m"):
                continue
            for t in (lexers.lex_dax if reg.kind == "dax" else lexers.lex_m)(text[reg.start:reg.end]):
                if t.kind == "str" and t.value and not tokens.TOKEN_RE.search(t.value):
                    out.add(t.value)
                elif t.kind == "num" and t.text not in ("0", "1"):
                    out.add(t.text)
        return out

    new = set()
    for rel, text in after.items():
        if rel.lower().endswith(".tmdl") and text is not None:
            new |= lits(text, rel) - (lits(before[rel], rel) if before.get(rel) else set())
    if not new:
        return 0
    st = _load_state(ws)
    authored = set(st["ledger"]["authored"]) | {_h(x) for x in new}
    st["ledger"]["authored"] = sorted(authored)[-5000:]
    _save_state(ws, st)
    return len(new)


def _reached_literals(model, res, expr: str, home: str) -> set[str]:
    """Literals inside the definitions of every measure the expression reaches (transitively)."""
    from . import deps
    out, seen, todo = set(), set(), [(expr, home)]
    while todo:
        text, tab = todo.pop()
        for t in lexers.significant(lexers.lex_dax(text)):
            if t.kind == "str" and t.value and not tokens.TOKEN_RE.search(t.value):
                out.add(t.value)
            elif t.kind == "num":
                out.add(t.text)
        for ref in deps.dax(text, tab, res).refs:
            if ref.kind == "measure" and (ref.table, ref.name) not in seen:
                seen.add((ref.table, ref.name))
                m = model.table(ref.table).measures.get(ref.name) if model.table(ref.table) else None
                if m is not None:
                    todo.append((m.expression, m.table))
    return out


def _save_state(ws, st: dict) -> None:
    os.makedirs(ws.private, exist_ok=True)
    with open(_state_path(ws), "w", encoding="utf-8") as f:
        json.dump(st, f)


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def model_literals(project) -> set[str]:
    """Literals visible in the mirror (category labels, constants): free to use in checks."""
    out: set[str] = set()
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        return out
    model = tmdl.load_model(mdef)
    for doc in model.docs.values():
        for reg in doc.regions:
            if reg.kind not in ("dax", "m"):
                continue
            seg = doc.text[reg.start:reg.end]
            for t in (lexers.lex_dax(seg) if reg.kind == "dax" else lexers.lex_m(seg)):
                if t.kind == "str" and not tokens.TOKEN_RE.search(t.value):
                    out.add(t.value)
                elif t.kind == "num":
                    out.add(t.text)
    return out


def _constants(expr: str) -> set[str]:
    out = set()
    for t in lexers.significant(lexers.lex_dax(expr)):
        if t.kind == "str" and not tokens.TOKEN_RE.fullmatch(t.value or ""):
            out.add(t.value)
        elif t.kind == "num" and t.text not in ("0", "1"):
            out.add(t.text)
    return out


def _canon_col(ref: str) -> str:
    """'Table'[Col] / Table[Col] -> Table[Col] label."""
    m = re.match(r"^\s*'((?:[^']|'')+)'\s*\[(.+)\]\s*$", ref) or re.match(r"^\s*([^\[\]']+?)\s*\[(.+)\]\s*$", ref)
    return f"{m.group(1).replace(chr(39) * 2, chr(39))}[{m.group(2)}]" if m else ref.strip()


def _rewrite(c: dict) -> tuple[dict | None, str | None]:
    kind = c.get("kind")
    if kind == "differs":
        return {**c, "kind": "equal", "_negate": True}, None
    if kind == "filter_independent":
        col = c.get("column", "")
        expr = c.get("expr", "")
        return {"id": c["id"], "kind": "equal", "left": expr, "right": f"CALCULATE({expr}, REMOVEFILTERS({col}))",
                "by": c.get("by", []), "where": c.get("where", [])}, None
    return c, None


def run_checks(ws, project, spec: dict) -> dict:
    shape = validate_spec(spec)
    if not shape["valid"]:
        return {"verb": "check", "project": project.name, "spec_valid": False,
                "results": [{"id": f["id"], "status": "ERROR", "class": "BAD_REQUEST",
                             "field": f["path"], "note": f["message"]} for f in shape["findings"]]}
    pol = policy_mod.load(ws)
    synthetic = _rls_synthetic(ws, project, pol)
    perf_done = []
    for index, raw in enumerate(spec.get("checks", [])):
        if isinstance(raw, dict) and raw.get("kind") == "perf":
            cid = str(raw.get("id") or f"c{index + 1}")
            perf_done.append((index, _perf_check(ws, project, raw, cid)))
    budgets = pol.get("budgets", {})
    strict = pol.get("disclosure") == "strict"
    tm = tokens.TokenMap(ws.private)
    st = _load_state(ws)
    known = model_literals(project)
    real_model = tmdl.load_model(os.path.join(project.model_dir, "definition"))
    clf = Classifier(real_model, ws.private, pol)
    from . import deps
    mdef = os.path.join(project.mirror_model, "definition")
    mmodel = tmdl.load_model(mdef) if os.path.isdir(mdef) else tmdl.Model()
    mres = deps.Resolver(mmodel)
    authored = set(st["ledger"]["authored"])
    results, to_engine, compiles_items, negate = [], [], [], set()
    rls_ids = set()
    for index, raw in enumerate(spec.get("checks", [])):
        cid = str(raw.get("id") or f"c{index + 1}")
        kind = raw.get("kind")
        if kind == "perf":
            continue
        if kind == "rls":
            if not synthetic and not _human_terminal():
                results.append({"id": cid, "status": "ERROR", "class": "HUMAN_ONLY"})
                continue
            query = raw.get("query") if isinstance(raw.get("query"), str) else None
            if query is not None:
                _q, unknown = tokens.detokenize_dax(query, tm)
                if unknown:
                    results.append({"id": cid, "status": "ERROR", "class": "BAD_REQUEST",
                                    "note": "unknown or opaque token"})
                    continue
                query = _q
            th = _h("rls")
            if st["checks"] >= int(budgets.get("session_checks", 300)) or \
                    st["per_target"].get(th, 0) >= int(budgets.get("per_target", 60)):
                results.append({"id": cid, "status": "BUDGET",
                                "note": "daily check budget reached; the user can reset it"})
                continue
            st["checks"] += 1
            st["per_target"][th] = st["per_target"].get(th, 0) + 1
            payload = {"id": cid, "kind": "rls", "expect": raw.get("expect")}
            for field in _RLS_TEXT:
                if field == "query":
                    if query is not None:
                        payload["query"] = query
                    continue
                if isinstance(raw.get(field), str):
                    payload[field] = raw[field]
            rls_ids.add(cid)
            to_engine.append(payload)
            continue
        if kind not in ALL_KINDS:
            results.append({"id": cid, "status": "ERROR", "class": "BAD_REQUEST", "note": "unknown kind"})
            continue
        if kind == "between":
            results.append({"id": cid, "status": "UNSUPPORTED", "note": "between is not offered"})
            continue
        c, _ = _rewrite({**raw, "id": cid})
        target = re.sub(r"\s+", " ", str(c.get("left") or c.get("expr") or c.get("column") or c.get("from") or ""))
        th = _h(target)
        consts = set()
        for f in EXPR_FIELDS:
            if isinstance(c.get(f), str):
                consts |= _constants(c[f])
        for w in c.get("where", []) or []:
            if isinstance(w, str):
                consts |= _constants(w)
        consts = {x for x in consts if x not in known}
        # literals the agent pushed into measures this target reaches count as constants too: editing a
        # measure body must not turn a fixed check into a fresh guessing channel
        for f in EXPR_FIELDS:
            if isinstance(c.get(f), str):
                consts |= {x for x in _reached_literals(mmodel, mres, c[f], next(iter(mmodel.tables), ""))
                           if _h(x) in authored}
        ledger = st["ledger"]["constants"].setdefault(th, [])
        seen_consts = {e["h"] for e in ledger}
        if len(seen_consts | {_h(x) for x in consts}) > int(budgets.get("constants_per_target", 3)):
            results.append({"id": cid, "status": "BUDGET",
                            "note": f"too many distinct constants against this target in {LEDGER_DAYS} days"})
            continue
        if st["checks"] >= int(budgets.get("session_checks", 300)) or \
                st["per_target"].get(th, 0) >= int(budgets.get("per_target", 60)):
            results.append({"id": cid, "status": "BUDGET", "note": "daily check budget reached; the user can reset it"})
            continue
        bad = []
        out = {}
        for k, v in c.items():
            if k.startswith("_"):
                continue
            if isinstance(v, str) and k in EXPR_FIELDS:
                v, unknown = tokens.detokenize_dax(v, tm)
                bad += unknown
            elif k in ("where", "by") and isinstance(v, list):
                vv = []
                for item in v:
                    if isinstance(item, str):
                        item, unknown = tokens.detokenize_dax(item, tm)
                        bad += unknown
                    vv.append(item)
                v = vv
            out[k] = v
        if bad:
            results.append({"id": cid, "status": "ERROR", "class": "BAD_REQUEST", "note": "unknown or opaque token"})
            continue
        st["checks"] += 1
        st["per_target"][th] = st["per_target"].get(th, 0) + 1
        ledger += [{"h": h, "day": st["day"]} for h in sorted({_h(x) for x in consts} - seen_consts)]
        if c.get("_negate"):
            negate.add(cid)
        if out["kind"] == "compiles":
            compiles_items.append({"id": cid, "table": out.get("table") or next(iter(real_model.tables), ""),
                                   "expression": out.get("expr", "")})
        else:
            to_engine.append(out)
    _save_state(ws, st)
    port = desktop.require_port(project) if (to_engine or compiles_items) else None
    reveal = []
    for c in to_engine:
        for b in c.get("by", []) or []:
            lab = _canon_col(b)
            t, _, col = lab.partition("[")
            col = col[:-1]
            if clf.column_class(t, col) in ("category", "date") or lab in pol.get("reveal_columns", []):
                reveal.append(b)
    if to_engine:
        res = engine_call("check", {"private_dir": ws.private, "port": port, "disclosure": pol.get("disclosure", "shape"),
                                    "reveal": sorted(set(reveal)), "checks": to_engine}, timeout=600)
        if not res.get("ok"):
            raise PbzError(_class_of(res.get("class"), _PY_CLASS) or "ENGINE_UNAVAILABLE")
        key = dpapi.load_key(ws.private)
        allowed_ids = {str(c.get("id")) for c in to_engine if c.get("id")}
        for r in res.get("checks", res.get("results", [])):
            r = dict(r)
            if r.get("id") in negate and r.get("status") in ("PASS", "FAIL"):
                r["status"] = "FAIL" if r["status"] == "PASS" else "PASS"
                r["note"] = "differs"
            groups = []
            for g in r.get("groups", []) or []:
                ng = {}
                for col, v in g.items():
                    if isinstance(v, dict) and v.get("blank"):
                        ng[col] = "(blank)"
                    elif isinstance(v, dict) and "reveal" in v:
                        ng[col] = v["reveal"]
                    elif isinstance(v, dict) and "hmac" in v:
                        lab = _canon_col(col)
                        ng[col] = tm.find_by_hmac(key, lab, v["hmac"]) or tm.opaque(lab, v["hmac"])
                if ng:
                    groups.append(ng)
            if groups and not strict:
                r["groups"] = groups
            else:
                r.pop("groups", None)
            if strict:
                for k in list(r):
                    if k.endswith("_bucket"):
                        r.pop(k)
            if str(r.get("id")) in rls_ids:
                r = _rls_clean(r)
            else:
                r = _closed_engine_row(r, allowed_ids)
            if r is not None:
                results.append(r)
    if compiles_items:
        res = engine_call("compiles", {"private_dir": ws.private, "port": port, "items": compiles_items}, timeout=300)
        if not res.get("ok"):
            raise PbzError(_class_of(res.get("class"), _PY_CLASS) or "ENGINE_UNAVAILABLE")
        from .validate import retokenizer
        scrub = retokenizer(tm)
        for it in res.get("items", []):
            it = dict(it)
            if "message" in it:
                it["message"] = scrub(it["message"])
            results.append(it)
    tm.save()
    results.extend(row for _index, row in perf_done)
    order = {str(c.get("id") or f"c{i + 1}"): i for i, c in enumerate(spec.get("checks", []))}
    results.sort(key=lambda r: order.get(str(r.get("id")), 1e9))
    return {"verb": "check", "project": project.name, "results": results,
            "budget": {"used_today": st["checks"], "limit": int(budgets.get("session_checks", 300))}}


def health(ws, project) -> dict:
    port = desktop.require_port(project)
    res = engine_call("health", {"private_dir": ws.private, "port": port}, timeout=300)
    if not res.get("ok"):
        raise PbzError(res.get("class", "ENGINE_UNAVAILABLE"))
    from .validate import retokenizer
    scrub = retokenizer(tokens.TokenMap(ws.private))
    for e in res.get("errors", []):
        if "message" in e:
            e["message"] = scrub(e["message"])
    res["verb"] = "health"
    return res


def probe(ws, project, table: str, columns: list[str] | None) -> dict:
    pol = policy_mod.load(ws)
    if not spend(ws, "probes"):
        return {"verb": "probe", "status": "BUDGET", "note": "daily probe budget reached; the user can reset it"}
    port = desktop.require_port(project)
    req = {"private_dir": ws.private, "port": port, "table": table, "disclosure": pol.get("disclosure", "shape")}
    if columns:
        req["columns"] = columns
    res = engine_call("probe", req, timeout=300)
    if not res.get("ok"):
        raise PbzError(res.get("class", "ENGINE_UNAVAILABLE"))
    res["verb"] = "probe"
    return res


def refresh(ws, project, tables: list[str] | None) -> dict:
    port = desktop.require_port(project)
    req = {"private_dir": ws.private, "port": port}
    if tables:
        req["tables"] = tables
    res = engine_call("refresh", req, timeout=3600)
    out = {"verb": "refresh", "project": project.name, "status": res.get("status", "ERROR" if not res.get("ok") else "PASS")}
    if not res.get("ok"):
        out["class"] = res.get("class", "OTHER")
    return out


def index(ws, project) -> dict:
    pol = policy_mod.load(ws)
    port = desktop.require_port(project)
    res = engine_call("index", {"private_dir": ws.private, "port": port, "category_max": int(pol.get("category_max", 50)),
                                "value_cap": 50000, "disclosure": pol.get("disclosure", "shape")}, timeout=1800)
    if not res.get("ok"):
        raise PbzError(res.get("class", "ENGINE_UNAVAILABLE"))
    # keep an agent-safe summary (no values) for `pbz describe`
    with open(os.path.join(ws.private, "index-summary.json"), "w", encoding="utf-8") as f:
        json.dump(res, f)
    moved = harden_index(ws, pol)
    cols = [c for t in res.get("tables", []) for c in t.get("columns", [])]
    return {"verb": "index", "project": project.name, "tables": len(res.get("tables", [])), "columns": len(cols),
            "category_columns": sum(1 for c in cols if c.get("class") == "category") - moved,
            "personal_or_id_columns_hashed": moved,
            "hint": "run `pbz pull --force` (or --rebase) to reclassify the mirror with the new index"}


def harden_index(ws, pol: dict) -> int:
    """Low-cardinality columns with personal or ID names are sensitive whatever their size: move their
    values out of categories.bin into HMAC sets (whole values + name words) so free text can be checked."""
    from .classify import is_id_column, is_pii_column
    idx_path = os.path.join(ws.private, "index.json")
    cat_path = os.path.join(ws.private, "categories.bin")
    try:
        with open(idx_path, encoding="utf-8") as f:
            idx = json.load(f)
    except (OSError, ValueError):
        return 0
    raw = dpapi.read_protected(cat_path)
    cats = json.loads(raw.decode("utf-8")) if raw else {}
    key = dpapi.load_key(ws.private)
    forced = {c.lower() for c in pol.get("sensitive_columns", [])}
    moved = 0
    for lab in list(cats):
        if "[" not in lab or not lab.endswith("]"):
            continue
        col = lab[lab.index("[") + 1:-1]
        if not (is_pii_column(col) or is_id_column(col) or lab.lower() in forced):
            continue
        vals = [v for v in cats.pop(lab) if v not in (None, "")]
        info = idx.setdefault("columns", {}).setdefault(lab, {})
        info["hmacs"] = sorted(set(info.get("hmacs", [])) | {dpapi.hmac_hex(key, v) for v in vals})
        if is_pii_column(col):
            words = set(info.get("word_hmacs", []))
            for v in vals:
                for w in str(v).split():
                    core = dpapi.word_core(w)
                    if sum(ch.isalpha() for ch in core) >= 3:
                        words.add(dpapi.hmac_hex(key, core))
            info["word_hmacs"] = sorted(words)
        info["class"] = "high"
        moved += 1
    if moved:
        tmp = idx_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(idx, f)
        os.replace(tmp, idx_path)
        dpapi.write_protected(cat_path, json.dumps(cats, ensure_ascii=False).encode("utf-8"))
    return moved


def snapshot(ws, project, action: str, name: str, spec: dict | None) -> dict:
    port = desktop.require_port(project)
    tm = tokens.TokenMap(ws.private)
    queries = []
    for q in (spec or {}).get("queries", []):
        dax, unknown = tokens.detokenize_dax(q.get("dax", ""), tm)
        if unknown:
            raise PbzError("BAD_REQUEST", "unknown token in query " + str(q.get("id")))
        queries.append({"id": q.get("id"), "dax": dax, "keys": q.get("keys", [])})
    verb = "snapshot-save" if action == "save" else "snapshot-compare"
    if not spend(ws, "snapshot_queries", max(1, len(queries))):
        return {"verb": verb, "status": "BUDGET", "note": "daily snapshot query budget reached; the user can reset it"}
    res = engine_call(verb, {"private_dir": ws.private, "port": port, "name": name, "queries": queries}, timeout=1800)
    if not res.get("ok"):
        raise PbzError(res.get("class", "ENGINE_UNAVAILABLE"))
    if action == "save":
        os.makedirs(os.path.join(ws.private, "snapshots"), exist_ok=True)
        with open(os.path.join(ws.private, "snapshots", f"{name}.spec.json"), "w", encoding="utf-8") as f:
            json.dump(spec or {}, f)
    res["verb"] = verb
    return res


def saved_spec(ws, name: str) -> dict | None:
    try:
        with open(os.path.join(ws.private, "snapshots", f"{name}.spec.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
