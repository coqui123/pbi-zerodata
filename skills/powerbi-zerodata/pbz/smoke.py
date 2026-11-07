"""Per-visual smoke test: build an approximate query from each visual's bindings and filters."""
from __future__ import annotations

import json
import os
import re

from . import desktop, pbir, tmdl, tokens
from .common import PbzError, engine_call, read_text
from .lexers import dax_string, dax_table_name

NON_DATA = {"textbox", "image", "shape", "basicShape", "actionButton", "pageNavigator", "bookmarkNavigator",
            "groupButton", "iframe"}
AGG = {0: "SUM", 1: "AVERAGE", 2: "COUNTA", 3: "MIN", 4: "MAX", 5: "DISTINCTCOUNT", 6: "MEDIAN",
       7: "STDEV.P", 8: "VAR.P"}
CMP = {0: "=", 1: ">", 2: ">=", 3: "<", 4: "<="}
ENGINE_STATUS = {"OK", "EMPTY", "ERROR"}
_DT = re.compile(r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})"
                 r"(?:[T ](?P<h>\d{2}):(?P<mi>\d{2})(?::(?P<s>\d{2})(?:\.(?P<f>\d+))?)?)?"
                 r"(?:Z|[+-]\d{2}:\d{2})?")


def col_dax(table: str, col: str) -> str:
    return f"{dax_table_name(table)}[{col.replace(']', ']]')}]"


def _date_dax(v: str) -> tuple[str | None, bool]:
    """DATE(...) for a date or midnight datetime. Second value: a clock time was dropped."""
    m = _DT.fullmatch(v.strip())
    if not m:
        return None, False
    dax = f"DATE({int(m.group('y'))}, {int(m.group('mo'))}, {int(m.group('d'))})"
    if m.group("h") is None:
        return dax, False
    frac = m.group("f")
    midnight = int(m.group("h")) == int(m.group("mi")) == int(m.group("s") or 0) == 0 and not (frac and int(frac) != 0)
    return dax, not midnight


def _lit_dax(raw: str, tm) -> tuple[str | None, bool]:
    """(dax, time_truncated). time_truncated is true when a datetime clock time became DATE(...)."""
    if tokens.TOKEN_RE.fullmatch(raw or ""):
        rec = tm.get(raw)
        if not rec or rec.get("kind") != "lit":
            return None, False
        v, vtype = rec.get("value"), rec.get("vtype")
    else:
        v, vtype = pbir.decode_literal(raw)
    if vtype == "string":
        return dax_string(str(v)), False
    if vtype in ("int", "double", "number"):
        return str(v), False
    if vtype == "bool":
        return ("TRUE()" if v else "FALSE()"), False
    if vtype == "datetime" and isinstance(v, str):
        return _date_dax(v)
    return None, False


def _filters(filter_config, model, tm) -> tuple[list[str], bool]:
    """DAX filter args for simple In / Comparison conditions; second value: something was skipped."""
    out, partial = [], False
    if not isinstance(filter_config, pbir.JObj):
        return out, partial
    flist = filter_config.get("filters")
    for f in flist.items if isinstance(flist, pbir.JArr) else []:
        if not isinstance(f, pbir.JObj):
            continue
        flt = f.get("filter")
        if not isinstance(flt, pbir.JObj):
            continue
        aliases = {}
        frm = flt.get("From")
        for e in frm.items if isinstance(frm, pbir.JArr) else []:
            if isinstance(e, pbir.JObj) and isinstance(e.get("Name"), pbir.JStr) and isinstance(e.get("Entity"), pbir.JStr):
                aliases[e.get("Name").value] = e.get("Entity").value
        where = flt.get("Where")
        for w in where.items if isinstance(where, pbir.JArr) else []:
            cond = w.get("Condition") if isinstance(w, pbir.JObj) else None
            if not isinstance(cond, pbir.JObj):
                continue
            if "In" in cond:
                inn = cond.get("In")
                exprs = inn.get("Expressions")
                cols = [pbir.field_of(e, aliases, model) for e in (exprs.items if isinstance(exprs, pbir.JArr) else [])]
                vals = inn.get("Values")
                if len(cols) != 1 or not cols[0] or cols[0].kind not in ("column", "hierarchy") or not isinstance(vals, pbir.JArr):
                    partial = True
                    continue
                if not vals.items:
                    partial = True
                    continue
                lits = []
                for row in vals.items:
                    cell = row.items[0] if isinstance(row, pbir.JArr) and row.items else None
                    lit = cell.get("Literal") if isinstance(cell, pbir.JObj) else None
                    raw = lit.get("Value").value if isinstance(lit, pbir.JObj) and isinstance(lit.get("Value"), pbir.JStr) else None
                    d, trunc = _lit_dax(raw, tm) if raw is not None else (None, False)
                    if trunc:
                        partial = True
                    if d is None:
                        partial = True
                        break
                    lits.append(d)
                else:
                    if lits:
                        out.append(f"TREATAS({{{', '.join(lits)}}}, {col_dax(cols[0].table, cols[0].name)})")
            elif "Comparison" in cond:
                cmp_ = cond.get("Comparison")
                f_ = pbir.field_of(cmp_.get("Left"), aliases, model)
                right = cmp_.get("Right")
                lit = right.get("Literal") if isinstance(right, pbir.JObj) else None
                raw = lit.get("Value").value if isinstance(lit, pbir.JObj) and isinstance(lit.get("Value"), pbir.JStr) else None
                kind = cmp_.get("ComparisonKind")
                op = CMP.get(kind.value if isinstance(kind, pbir.JLit) else None)
                d, trunc = _lit_dax(raw, tm) if raw is not None else (None, False)
                if trunc:
                    partial = True
                if not f_ or f_.kind not in ("column", "hierarchy") or not op or d is None:
                    partial = True
                    continue
                c = col_dax(f_.table, f_.name)
                out.append(f"KEEPFILTERS(FILTER(ALL({c}), {c} {op} {d}))")
            else:
                partial = True
    return out, partial


def build_query(visual_root, page_root, model, tm) -> tuple[str | None, str]:
    v = visual_root.get("visual") if isinstance(visual_root, pbir.JObj) else None
    if not isinstance(v, pbir.JObj):
        return None, "UNSUPPORTED"
    vt = v.get("visualType")
    vtype = vt.value if isinstance(vt, pbir.JStr) else ""
    if vtype in NON_DATA:
        return None, "N/A"
    q = v.get("query")
    qs = q.get("queryState") if isinstance(q, pbir.JObj) else None
    groups, measures = [], []
    skipped = False
    bad_agg = False
    if isinstance(qs, pbir.JObj):
        for _role, rnode in qs.pairs:
            projs = rnode.get("projections") if isinstance(rnode, pbir.JObj) else None
            for p in projs.items if isinstance(projs, pbir.JArr) else []:
                fld = p.get("field") if isinstance(p, pbir.JObj) else None
                ref = pbir.field_of(fld, {}, model)
                if not ref or not ref.table:
                    skipped = True
                    continue
                if ref.kind == "measure":
                    measures.append(f"[{ref.name.replace(']', ']]')}]")
                elif ref.kind == "aggregation":
                    fn = AGG.get(ref.function)
                    if not fn:
                        bad_agg = True
                        continue
                    measures.append(f"{fn}({col_dax(ref.table, ref.name)})")
                elif ref.kind in ("column", "hierarchy") and ref.name:
                    c = col_dax(ref.table, ref.name)
                    if c not in groups:
                        groups.append(c)
    if bad_agg or (not groups and not measures):
        return None, "UNSUPPORTED"
    fl, partial = _filters(visual_root.get("filterConfig"), model, tm)
    pf, ppartial = _filters(page_root.get("filterConfig") if isinstance(page_root, pbir.JObj) else None, model, tm)
    filters = fl + pf
    coverage = "PARTIAL" if (skipped or partial or ppartial) else "FULL"
    named = [f'"m{i}", {m}' for i, m in enumerate(measures)]
    if groups:
        args = ", ".join(groups + filters + named)
        return f"EVALUATE TOPN(1, SUMMARIZECOLUMNS({args}))", coverage
    row = f'ROW({", ".join(named)})'
    inner = f"CALCULATETABLE({row}, {', '.join(filters)})" if filters else row
    cond = " || ".join(f"NOT ISBLANK([m{i}])" for i in range(len(measures)))
    return f"EVALUATE FILTER({inner}, {cond})", coverage


def run(ws, project, page: str | None = None) -> dict:
    from .checks import spend
    if not spend(ws, "smoke"):
        return {"verb": "smoke", "status": "BUDGET", "note": "daily smoke budget reached; the user can reset it"}
    port = desktop.require_port(project)
    tm = tokens.TokenMap(ws.private)
    mdef = os.path.join(project.mirror_model, "definition")
    model = tmdl.load_model(mdef)
    pages_dir = os.path.join(project.mirror_report, "definition", "pages")
    if not os.path.isdir(pages_dir):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    items, rows = [], []
    for pid in sorted(os.listdir(pages_dir)):
        pdir = os.path.join(pages_dir, pid)
        pjson = os.path.join(pdir, "page.json")
        if not os.path.isfile(pjson):
            continue
        proot = pbir.parse_json(read_text(pjson)[0])
        pname = proot.get("displayName").value if isinstance(proot.get("displayName"), pbir.JStr) else pid
        if page and page not in (pid, pname):
            continue
        vdir = os.path.join(pdir, "visuals")
        for vid in sorted(os.listdir(vdir)) if os.path.isdir(vdir) else []:
            vjson = os.path.join(vdir, vid, "visual.json")
            if not os.path.isfile(vjson):
                continue
            vroot = pbir.parse_json(read_text(vjson)[0])
            v = vroot.get("visual")
            vt = v.get("visualType").value if isinstance(v, pbir.JObj) and isinstance(v.get("visualType"), pbir.JStr) else "?"
            dax, note = build_query(vroot, proot, model, tm)
            row = {"page": pname, "visual": vid, "type": vt, "_pid": pid}
            if dax is None:
                row["status"] = note if note in ("N/A", "UNSUPPORTED") else "UNSUPPORTED"
                row["coverage"] = "NONE"
            else:
                items.append({"id": f"{pid}/{vid}", "dax": dax})
                row["coverage"] = note if note in ("FULL", "PARTIAL") else "PARTIAL"
            rows.append(row)
    ENGINE_CLASS = {"SYNTAX", "UNKNOWN_OBJECT", "TYPE_MISMATCH", "CONVERSION", "CIRCULAR",
                    "DUPLICATE_KEY", "TIMEOUT", "ENGINE_UNAVAILABLE", "BAD_REQUEST", "OTHER"}
    if items:
        res = engine_call("smoke", {"private_dir": ws.private, "port": port, "visuals": items}, timeout=1800)
        if not res.get("ok"):
            klass = res.get("class")
            if not isinstance(klass, str) or klass not in ENGINE_CLASS | {"ENGINE_UNAVAILABLE", "TIMEOUT", "NOT_READY"}:
                raise PbzError("PROTOCOL_ERROR", "engine smoke failure class is not in the closed vocabulary")
            raise PbzError(klass)
        raw_visuals = res.get("visuals")
        if not isinstance(raw_visuals, list):
            raise PbzError("PROTOCOL_ERROR", "engine smoke visuals must be a list")
        by_id: dict = {}
        dup_ids: set[str] = set()
        for r in raw_visuals:
            if not isinstance(r, dict) or not isinstance(r.get("id"), str):
                continue
            rid = r["id"]
            if rid in by_id:
                dup_ids.add(rid)
                continue
            by_id[rid] = r
        for row in rows:
            if "status" in row:
                continue
            key = f"{row['_pid']}/{row['visual']}"
            if key in dup_ids or key not in by_id:
                row["status"] = "PROTOCOL_ERROR"
                continue
            r = by_id[key]
            status = r.get("status")
            if not isinstance(status, str) or status not in ENGINE_STATUS:
                row["status"] = "PROTOCOL_ERROR"
                continue
            row["status"] = status
            klass = r.get("class")
            if klass is None:
                continue
            if isinstance(klass, str) and klass in ENGINE_CLASS:
                row["class"] = klass
            else:
                row["status"] = "PROTOCOL_ERROR"
    executed, not_executed = {}, {}
    for row in rows:
        row.pop("_pid", None)
        # PROTOCOL_ERROR is a protocol fault on an attempted execution, not a successful verdict.
        bucket = executed if row["status"] in ENGINE_STATUS else not_executed
        if row["status"] == "PROTOCOL_ERROR":
            bucket = not_executed
        bucket[row["status"]] = bucket.get(row["status"], 0) + 1
    from .outcomes import envelope
    out, _rc = envelope({"verb": "smoke", "project": project.name,
                         "summary": {"executed": executed, "not_executed": not_executed}, "visuals": rows})
    return out
