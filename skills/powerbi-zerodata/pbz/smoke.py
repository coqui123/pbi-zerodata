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


def col_dax(table: str, col: str) -> str:
    return f"{dax_table_name(table)}[{col.replace(']', ']]')}]"


def _lit_dax(raw: str, tm) -> str | None:
    if tokens.TOKEN_RE.fullmatch(raw or ""):
        rec = tm.get(raw)
        if not rec or rec.get("kind") != "lit":
            return None
        v, vtype = rec.get("value"), rec.get("vtype")
    else:
        v, vtype = pbir.decode_literal(raw)
    if vtype == "string":
        return dax_string(str(v))
    if vtype in ("int", "double", "number"):
        return str(v)
    if vtype == "bool":
        return "TRUE()" if v else "FALSE()"
    if vtype == "datetime" and isinstance(v, str):
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", v)
        return f"DATE({int(m.group(1))}, {int(m.group(2))}, {int(m.group(3))})" if m else None
    return None


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
                lits = []
                for row in vals.items:
                    cell = row.items[0] if isinstance(row, pbir.JArr) and row.items else None
                    lit = cell.get("Literal") if isinstance(cell, pbir.JObj) else None
                    raw = lit.get("Value").value if isinstance(lit, pbir.JObj) and isinstance(lit.get("Value"), pbir.JStr) else None
                    d = _lit_dax(raw, tm) if raw is not None else None
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
                d = _lit_dax(raw, tm) if raw is not None else None
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
    if isinstance(qs, pbir.JObj):
        for _role, rnode in qs.pairs:
            projs = rnode.get("projections") if isinstance(rnode, pbir.JObj) else None
            for p in projs.items if isinstance(projs, pbir.JArr) else []:
                fld = p.get("field") if isinstance(p, pbir.JObj) else None
                ref = pbir.field_of(fld, {}, model)
                if not ref or not ref.table:
                    continue
                if ref.kind == "measure":
                    measures.append(f"[{ref.name.replace(']', ']]')}]")
                elif ref.kind == "aggregation":
                    measures.append(f"{AGG.get(ref.function, 'COUNTA')}({col_dax(ref.table, ref.name)})")
                elif ref.kind in ("column", "hierarchy") and ref.name:
                    c = col_dax(ref.table, ref.name)
                    if c not in groups:
                        groups.append(c)
    if not groups and not measures:
        return None, "UNSUPPORTED"
    fl, partial = _filters(visual_root.get("filterConfig"), model, tm)
    pf, ppartial = _filters(page_root.get("filterConfig") if isinstance(page_root, pbir.JObj) else None, model, tm)
    filters = fl + pf
    note = "filters partial" if (partial or ppartial) else ""
    named = [f'"m{i}", {m}' for i, m in enumerate(measures)]
    if groups:
        args = ", ".join(groups + filters + named)
        return f"EVALUATE TOPN(1, SUMMARIZECOLUMNS({args}))", note
    row = f'ROW({", ".join(named)})'
    inner = f"CALCULATETABLE({row}, {', '.join(filters)})" if filters else row
    cond = " || ".join(f"NOT ISBLANK([m{i}])" for i in range(len(measures)))
    return f"EVALUATE FILTER({inner}, {cond})", note


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
                row["status"] = note
            else:
                items.append({"id": f"{pid}/{vid}", "dax": dax})
                if note:
                    row["note"] = note
            rows.append(row)
    if items:
        res = engine_call("smoke", {"private_dir": ws.private, "port": port, "visuals": items}, timeout=1800)
        if not res.get("ok"):
            raise PbzError(res.get("class", "ENGINE_UNAVAILABLE"))
        by_id = {r.get("id"): r for r in res.get("visuals", [])}
        for row in rows:
            if "status" in row:
                continue
            r = by_id.get(f"{row['_pid']}/{row['visual']}") or {}
            row["status"] = r.get("status", "ERROR")
            if r.get("class"):
                row["class"] = r["class"]
    counts = {}
    for row in rows:
        row.pop("_pid", None)
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"verb": "smoke", "project": project.name, "summary": counts, "visuals": rows}
