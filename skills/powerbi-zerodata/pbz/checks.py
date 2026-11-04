"""Data-blind assertions, probes and snapshots through pbz-engine, with budgets and constant limits."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re

from . import desktop, dpapi, lexers, policy as policy_mod, tmdl, tokens
from .classify import Classifier
from .common import PbzError, engine_call

ENGINE_KINDS = {"executes", "nonblank", "nonempty", "equal", "additive", "unique", "no_orphans",
                "no_blank_keys", "sign", "subset"}
ALL_KINDS = ENGINE_KINDS | {"differs", "filter_independent", "compiles"}
EXPR_FIELDS = ("expr", "left", "right", "column", "from", "to", "of")


def _state_path(ws) -> str:
    return os.path.join(ws.private, "state.json")


def _load_state(ws) -> dict:
    today = dt.date.today().isoformat()
    try:
        with open(_state_path(ws), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    if st.get("day") != today:
        st = {"day": today, "checks": 0, "per_target": {}, "constants": {}}
    return st


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
    pol = policy_mod.load(ws)
    budgets = pol.get("budgets", {})
    strict = pol.get("disclosure") == "strict"
    tm = tokens.TokenMap(ws.private)
    st = _load_state(ws)
    known = model_literals(project)
    real_model = tmdl.load_model(os.path.join(project.model_dir, "definition"))
    clf = Classifier(real_model, ws.private, pol)
    results, to_engine, compiles_items, negate = [], [], [], set()
    for raw in spec.get("checks", []):
        cid = str(raw.get("id") or f"c{len(results) + len(to_engine) + len(compiles_items) + 1}")
        kind = raw.get("kind")
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
        seen_consts = set(st["constants"].get(th, []))
        if len(seen_consts | {_h(x) for x in consts}) > int(budgets.get("constants_per_target", 3)):
            results.append({"id": cid, "status": "BUDGET", "note": "too many distinct constants against this target"})
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
        st["constants"][th] = sorted(seen_consts | {_h(x) for x in consts})
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
            raise PbzError(res.get("class", "ENGINE_UNAVAILABLE"))
        key = dpapi.load_key(ws.private)
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
                groups.append(ng)
            if groups and not strict:
                r["groups"] = groups
            else:
                r.pop("groups", None)
            if strict:
                for k in list(r):
                    if k.endswith("_bucket"):
                        r.pop(k)
            results.append(r)
    if compiles_items:
        res = engine_call("compiles", {"private_dir": ws.private, "port": port, "items": compiles_items}, timeout=300)
        if not res.get("ok"):
            raise PbzError(res.get("class", "ENGINE_UNAVAILABLE"))
        from .validate import retokenizer
        scrub = retokenizer(tm)
        for it in res.get("items", []):
            it = dict(it)
            if "message" in it:
                it["message"] = scrub(it["message"])
            results.append(it)
    tm.save()
    order = {str(c.get("id")): i for i, c in enumerate(spec.get("checks", []))}
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
