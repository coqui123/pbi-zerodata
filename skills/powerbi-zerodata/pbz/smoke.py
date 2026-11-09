"""Per-visual smoke test: build an approximate query from each visual's bindings and filters."""
from __future__ import annotations

import datetime as dt
import json
import os
import re

from . import desktop, pbir, tmdl, tokens
from .common import PbzError, engine_call, read_definition_text
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
    """DATE, or DATE+TIME when a clock is present. Second value: precision was dropped (fraction/offset)."""
    m = _DT.fullmatch(v.strip())
    if not m:
        return None, False
    y, mo, d = int(m.group("y")), int(m.group("mo")), int(m.group("d"))
    offset = bool(re.search(r"(?:Z|[+-]\d{2}:\d{2})$", v.strip()))
    if m.group("h") is None:
        if not _real_when(y, mo, d):
            return None, True
        return f"DATE({y}, {mo}, {d})", offset
    h, mi, s = int(m.group("h")), int(m.group("mi")), int(m.group("s") or 0)
    if not _real_when(y, mo, d, h, mi, s):
        return None, True
    frac = m.group("f")
    dropped = bool(frac and int(frac) != 0) or offset
    if h == mi == s == 0 and not dropped:
        return f"DATE({y}, {mo}, {d})", False
    # DATETIME() is not a DAX constructor; DATE + TIME is.
    return f"(DATE({y}, {mo}, {d}) + TIME({h}, {mi}, {s}))", dropped


def _real_when(y, mo, d, h=None, mi=None, s=None) -> bool:
    try:
        if h is None:
            dt.date(y, mo, d)
        else:
            dt.datetime(y, mo, d, h, mi, s)
    except ValueError:
        return False
    return True


def _lit_dax(raw: str, tm) -> tuple[str | None, bool]:
    """(dax, precision_lost). precision_lost when fraction/offset is dropped from a datetime literal."""
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


_UNSUPPORTED_FILTER_TYPES = {"TopN", "RelativeDate", "RelativeTime", "AdvancedRelativeDate"}


def _cell_dax(cell, tm) -> tuple[str | None, bool]:
    lit = cell.get("Literal") if isinstance(cell, pbir.JObj) else None
    raw = lit.get("Value").value if isinstance(lit, pbir.JObj) and isinstance(lit.get("Value"), pbir.JStr) else None
    if raw is None:
        return None, False
    return _lit_dax(raw, tm)


def _column_ref(node, aliases, model):
    ref = pbir.field_of(node, aliases, model)
    if not ref or ref.kind not in ("column", "hierarchy") or not ref.table or not ref.name:
        return None
    return ref


def _in_filter(inn, aliases, model, tm) -> tuple[str | None, bool]:
    """One TREATAS. Multi-column rows stay paired; a broken row is omitted entirely."""
    if not isinstance(inn, pbir.JObj) or "Table" in inn:
        return None, True
    exprs = inn.get("Expressions")
    cols = [_column_ref(e, aliases, model) for e in (exprs.items if isinstance(exprs, pbir.JArr) else [])]
    vals = inn.get("Values")
    if not cols or any(c is None for c in cols) or not isinstance(vals, pbir.JArr) or not vals.items:
        return None, True
    rows, partial = [], False
    for row in vals.items:
        cells = row.items if isinstance(row, pbir.JArr) else None
        if cells is None or len(cells) != len(cols):
            return None, True
        lits = []
        for cell in cells:
            d, trunc = _cell_dax(cell, tm)
            partial = partial or trunc
            if d is None:
                return None, True
            lits.append(d)
        rows.append(lits)
    targets = ", ".join(col_dax(c.table, c.name) for c in cols)
    if len(cols) == 1:
        body = ", ".join(r[0] for r in rows)
    else:
        body = ", ".join("(" + ", ".join(r) + ")" for r in rows)
    return f"TREATAS({{{body}}}, {targets})", partial


def _cmp_filter(cmp_, aliases, model, tm) -> tuple[str | None, bool]:
    if not isinstance(cmp_, pbir.JObj):
        return None, True
    ref = _column_ref(cmp_.get("Left"), aliases, model)
    right = cmp_.get("Right")
    kind = cmp_.get("ComparisonKind")
    op = CMP.get(kind.value if isinstance(kind, pbir.JLit) else None)
    d, trunc = _cell_dax(right, tm)
    if not ref or not op or d is None:
        return None, True
    c = col_dax(ref.table, ref.name)
    return f"KEEPFILTERS(FILTER(ALL({c}), {c} {op} {d}))", trunc


def _condition(cond, aliases, model, tm) -> tuple[list[str], bool]:
    """Supported Comparison / In conditions, including a straightforward AND of those."""
    if not isinstance(cond, pbir.JObj):
        return [], True
    if "And" in cond:
        node = cond.get("And")
        if not isinstance(node, pbir.JObj):
            return [], True
        left, lp = _condition(node.get("Left"), aliases, model, tm)
        right, rp = _condition(node.get("Right"), aliases, model, tm)
        return left + right, lp or rp
    if "In" in cond:
        expr, partial = _in_filter(cond.get("In"), aliases, model, tm)
        return ([expr] if expr else []), partial
    if "Comparison" in cond:
        expr, partial = _cmp_filter(cond.get("Comparison"), aliases, model, tm)
        return ([expr] if expr else []), partial
    return [], True


def _filters(filter_config, model, tm) -> tuple[list[str], bool]:
    """DAX filter args for simple In / Comparison conditions; second value: something was skipped."""
    out, partial = [], False
    if filter_config is None:
        return out, partial
    if not isinstance(filter_config, pbir.JObj):
        return out, True
    if "filters" not in filter_config:
        return out, partial
    flist = filter_config.get("filters")
    if not isinstance(flist, pbir.JArr):
        return out, True
    for f in flist.items:
        if not isinstance(f, pbir.JObj):
            partial = True
            continue
        kind = f.get("type")
        if isinstance(kind, pbir.JStr) and kind.value in _UNSUPPORTED_FILTER_TYPES:
            partial = True
            continue
        if "filter" not in f:
            continue
        flt = f.get("filter")
        if not isinstance(flt, pbir.JObj):
            partial = True
            continue
        aliases = {}
        frm = flt.get("From")
        for e in frm.items if isinstance(frm, pbir.JArr) else []:
            if isinstance(e, pbir.JObj) and isinstance(e.get("Name"), pbir.JStr) and isinstance(e.get("Entity"), pbir.JStr):
                aliases[e.get("Name").value] = e.get("Entity").value
        if "Where" not in flt:
            continue
        where = flt.get("Where")
        if not isinstance(where, pbir.JArr):
            partial = True
            continue
        for w in where.items:
            produced, skipped = _condition(w.get("Condition") if isinstance(w, pbir.JObj) else None, aliases, model, tm)
            partial = partial or skipped
            out.extend(produced)
    return out, partial


def _interactions(root) -> bool:
    if not isinstance(root, pbir.JObj):
        return False
    vi = root.get("visualInteractions")
    return isinstance(vi, pbir.JArr) and bool(vi.items)


# Default cross-filter from these siblings is never in the visual's own filterConfig.
SLICER_TYPES = {"slicer", "advancedSlicerVisual", "slicerFilter", "fieldParameter"}


def _mirror_rel(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace("\\", "/")


def _read_pbir(path: str, rel: str):
    try:
        text = read_definition_text(path)[0]
    except PbzError:
        raise
    except (OSError, UnicodeError) as e:
        raise PbzError("INVALID", rel) from e
    try:
        return pbir.parse_json(text)
    except (pbir.JsonError, ValueError, IndexError) as e:
        raise PbzError("INVALID", rel) from e


def _page_has_slicers(page_dir: str) -> bool:
    vdir = os.path.join(page_dir, "visuals")
    if not os.path.isdir(vdir):
        return False
    mirror = page_dir
    for _ in range(4):
        mirror = os.path.dirname(mirror)
    for vid in os.listdir(vdir):
        path = os.path.join(vdir, vid, "visual.json")
        if not os.path.isfile(path):
            continue
        root = _read_pbir(path, _mirror_rel(mirror, path))
        v = root.get("visual") if isinstance(root, pbir.JObj) else None
        vt = v.get("visualType") if isinstance(v, pbir.JObj) else None
        if isinstance(vt, pbir.JStr) and vt.value in SLICER_TYPES:
            return True
    return False


def build_query(visual_root, page_root, model, tm, report_root=None, page_has_slicers=False) -> tuple[str | None, str]:
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
    filters, partial = [], False
    for root in (visual_root, page_root, report_root):
        cfg = root.get("filterConfig") if isinstance(root, pbir.JObj) else None
        part, skipped_part = _filters(cfg, model, tm)
        filters.extend(part)
        partial = partial or skipped_part
    if _interactions(page_root) or _interactions(report_root) or page_has_slicers:
        partial = True
    coverage = "PARTIAL" if (skipped or partial) else "FULL"
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
    pages_dir = os.path.join(project.mirror_report, "definition", "pages")
    if not os.path.isdir(pages_dir):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    mirror = os.path.dirname(project.mirror_report)
    report_path = os.path.join(project.mirror_report, "definition", "report.json")
    report_root = _read_pbir(report_path, _mirror_rel(mirror, report_path)) if os.path.isfile(report_path) else None
    loaded = []
    matched = page is None
    for pid in sorted(os.listdir(pages_dir)):
        pdir = os.path.join(pages_dir, pid)
        pjson = os.path.join(pdir, "page.json")
        if not os.path.isfile(pjson):
            continue
        proot = _read_pbir(pjson, _mirror_rel(mirror, pjson))
        pname = proot.get("displayName").value if isinstance(proot, pbir.JObj) and isinstance(proot.get("displayName"), pbir.JStr) else pid
        if not page or page in (pid, pname):
            matched = True
        has_slicers = _page_has_slicers(pdir)
        visuals = []
        vdir = os.path.join(pdir, "visuals")
        for vid in sorted(os.listdir(vdir)) if os.path.isdir(vdir) else []:
            vjson = os.path.join(vdir, vid, "visual.json")
            if not os.path.isfile(vjson):
                continue
            visuals.append((vid, _read_pbir(vjson, _mirror_rel(mirror, vjson))))
        loaded.append((pid, pname, proot, has_slicers, visuals))
    if page and not matched:
        raise PbzError("NOT_FOUND", f"page {page}")
    if not spend(ws, "smoke"):
        return {"verb": "smoke", "status": "BUDGET", "note": "daily smoke budget reached; the user can reset it"}
    port = desktop.require_port(project)
    tm = tokens.TokenMap(ws.private)
    mdef = os.path.join(project.mirror_model, "definition")
    model = tmdl.load_model(mdef)
    items, rows = [], []
    for pid, pname, proot, has_slicers, visuals in loaded:
        if page and page not in (pid, pname):
            continue
        for vid, vroot in visuals:
            v = vroot.get("visual")
            vt = v.get("visualType").value if isinstance(v, pbir.JObj) and isinstance(v.get("visualType"), pbir.JStr) else "?"
            # Slicer visuals themselves are not cross-filtered by sibling slicers into their own query.
            dax, note = build_query(vroot, proot, model, tm, report_root,
                                    page_has_slicers=has_slicers and vt not in SLICER_TYPES)
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
