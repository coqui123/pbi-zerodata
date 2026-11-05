"""`pbz refresh-policy set|remove|show`: configure incremental refresh on an import table in the mirror.
Adds the RangeStart / RangeEnd DateTime parameters, a folding-friendly filter step (>= start, < end) to
the table's query, and the refreshPolicy block (store / refresh windows, optional change detection).
Partitions are created by the service on publish; nothing here can prove refresh or folding."""
from __future__ import annotations

import os
import re
import uuid

from . import lexers, mlineage, tmdl
from .common import PbzError, read_text, write_text
from .tokens import apply_edits

UNITS = ("day", "month", "quarter", "year")
FILTER_STEP = "pbz Incremental Filter"


def _model(project):
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    return mdef, tmdl.load_model(mdef)


def _write(mdef: str, rel: str, text: str) -> None:
    path = os.path.join(mdef, *rel.split("/"))
    bom = read_text(path)[1] if os.path.exists(path) else b""
    write_text(path, text, bom)


def _m_name(s: str) -> str:
    return s if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s) else '#"' + s.replace('"', '""') + '"'


def _field(s: str) -> str:
    return "[" + s.replace("]", "]]") + "]" if re.fullmatch(r"[^\[\]#\"=,]+", s) else '[#"' + s.replace('"', '""') + '"]'


def _partition(model, t):
    parts = [p for p in t.partition_nodes if (p.decl_value.strip().lower() or "m") == "m"]
    if len(parts) != 1:
        raise PbzError("UNSUPPORTED", "incremental refresh needs a table with exactly one Power Query (M) partition")
    p = parts[0]
    reg = next((r for r in model.docs[p.file].regions if r.node is p and r.kind == "m"), None)
    if reg is None:
        raise PbzError("UNSUPPORTED", "the partition has no M expression")
    return p, reg


def _params(mdef: str, model) -> list[str]:
    """Create RangeStart / RangeEnd DateTime parameters when missing. Returns created names."""
    have = {e.lower() for e in model.expressions}
    made = []
    rel = "expressions.tmdl"
    path = os.path.join(mdef, rel)
    text = read_text(path)[0] if os.path.exists(path) else ""
    eol = "\r\n" if "\r\n" in text else "\n"
    for name, (y, mo) in (("RangeStart", (2024, 1)), ("RangeEnd", (2024, 2))):
        if name.lower() in have:
            continue
        block = (f"expression {name} = #datetime({y}, {mo}, 1, 0, 0, 0) meta [IsParameterQuery=true, Type=\"DateTime\", "
                 f"IsParameterQueryRequired=true]{eol}\tlineageTag: {uuid.uuid4()}{eol}{eol}\tannotation PBI_ResultType = DateTime{eol}")
        text = (text.rstrip("\r\n") + eol + eol if text.strip() else "") + block
        made.append(name)
    if made:
        _write(mdef, rel, text)
    return made


def set_policy(ws, project, table: str, date_col: str, store: int, store_unit: str, refresh: int, refresh_unit: str,
               detect: str | None = None, complete: bool = False, hybrid: bool = False) -> dict:
    if store_unit not in UNITS or refresh_unit not in UNITS or store < 1 or refresh < 1:
        raise PbzError("BAD_REQUEST", "windows are positive numbers of day, month, quarter or year")
    mdef, model = _model(project)
    t = model.table(table)
    if t is None or t.node is None:
        raise PbzError("NOT_FOUND", f"table {table}")
    if any(c.kind == "refreshPolicy" for c in t.node.children):
        raise PbzError("NAME_TAKEN", "the table already has a refresh policy (pbz refresh-policy remove first)")
    c = model.column(t.name, date_col.strip("[]'"))
    if c is None or not c.source_column:
        raise PbzError("NOT_FOUND", f"import column {date_col} of {t.name}")
    if not c.data_type.lower().startswith("date"):
        raise PbzError("UNSUPPORTED", "the filter column must be dateTime (convert integer date keys in Power Query first)")
    dcol = model.column(t.name, detect.strip("[]'")) if detect else None
    if detect and (dcol is None or not dcol.source_column):
        raise PbzError("NOT_FOUND", f"import column {detect} of {t.name}")
    p, reg = _partition(model, t)
    doc = model.docs[p.file]
    src = doc.text[reg.start:reg.end]
    steps, result = mlineage.split_steps(src)
    if steps is None or result is None:
        raise PbzError("UNSUPPORTED", "the query is not a simple `let ... in <step>`")
    made = _params(mdef, model)
    # 1) filter step in the partition query (skip when the query already filters on RangeStart)
    if "RangeStart" in src and "RangeEnd" in src:
        new_src, filtered = src, "EXISTING"
    else:
        toks = lexers.significant(lexers.lex_m(src))
        in_tok = next(i for i in range(len(toks) - 1, -1, -1) if toks[i].kind == "ident" and toks[i].text == "in")
        last = toks[in_tok + 1]
        line_start = src.rfind("\n", 0, toks[1].start) + 1
        indent = re.match(r"[ \t]*", src[line_start:]).group(0)
        f = _field(c.source_column)
        step = (f",\n{indent}#\"{FILTER_STEP}\" = Table.SelectRows({last.text}, each {f} >= RangeStart and "
                f"{f} < RangeEnd)")
        prev_end = toks[in_tok - 1].end
        new_src = src[:prev_end] + step + src[prev_end:last.start] + f'#"{FILTER_STEP}"' + src[last.end:]
        filtered = "ADDED"
    # 2) the refreshPolicy block (source expression = the filtered query)
    eol = "\r\n" if "\r\n" in doc.text else "\n"
    expr_lines = [ln.strip("\t") for ln in new_src.splitlines() if ln.strip()]
    base = "\t" * 4
    lines = ["\trefreshPolicy", "\t\tpolicyType: basic", f"\t\trollingWindowGranularity: {store_unit}",
             f"\t\trollingWindowPeriods: {store}", f"\t\tincrementalGranularity: {refresh_unit}",
             f"\t\tincrementalPeriods: {refresh}"]
    if complete:
        lines.append("\t\tincrementalPeriodsOffset: -1")
    if hybrid:
        lines.append("\t\tmode: hybrid")
    lines.append("\t\tsourceExpression =")
    lines += [base + ln for ln in expr_lines]
    if dcol is not None:
        lines.append("\t\tpollingExpression =")
        lines += [base + ln for ln in (expr_lines[:-2] if expr_lines[-2].strip() == "in" else expr_lines)]
        poll_last = expr_lines[-1].strip()
        if lines[-1].strip().endswith(("in",)):
            lines.pop()
        lines[-1] = lines[-1].rstrip(",") + ","
        lines += [base + f'    #"pbz Change Detection" = List.Max({poll_last}{_field(dcol.source_column)})', base + "in",
                  base + '    #"pbz Change Detection"']
    block = eol.join(lines) + eol + eol
    kids = [ch for ch in t.node.children if ch.kind not in ("annotation", "changedProperty")]
    at = tmdl.block_span(doc, kids[0])[0] if kids else tmdl.block_span(doc, t.node)[1]
    edits = [(reg.start, reg.end, new_src), (at, at, block)] if filtered == "ADDED" else [(at, at, block)]
    _write(mdef, p.file, apply_edits(doc.text, sorted(edits)))
    files = [f"{project.name}.SemanticModel/definition/{p.file}"] + (
        [f"{project.name}.SemanticModel/definition/expressions.tmdl"] if made else [])
    return {"verb": "refresh-policy", "action": "set", "table": t.name, "status": "CONFIGURED_NOT_REFRESH_VERIFIED",
            "store": f"{store} {store_unit}(s)", "refresh": f"{refresh} {refresh_unit}(s)", "filter_step": filtered,
            "parameters_created": made, "change_detection": bool(dcol), "files": files,
            "next": "pbz diff, pbz push; partitions are created when the service refreshes after publish. Keep the "
                    "RangeStart/RangeEnd window small in Desktop. Folding is not verified offline."}


def remove_policy(ws, project, table: str) -> dict:
    mdef, model = _model(project)
    t = model.table(table)
    if t is None or t.node is None:
        raise PbzError("NOT_FOUND", f"table {table}")
    rp = next((ch for ch in t.node.children if ch.kind == "refreshPolicy"), None)
    if rp is None:
        raise PbzError("NOT_FOUND", f"{t.name} has no refresh policy")
    doc = model.docs[rp.file]
    _write(mdef, rp.file, apply_edits(doc.text, [tmdl.block_span(doc, rp) + ("",)]))
    return {"verb": "refresh-policy", "action": "removed", "table": t.name,
            "note": "the RangeStart filter step and the parameters stay; remove them in Power Query if unwanted",
            "next": "pbz diff, pbz push"}


def show(ws, project, table: str | None = None) -> dict:
    _mdef, model = _model(project)
    out = []
    for t in model.tables.values():
        if table and t.name.lower() != table.lower():
            continue
        rp = next((ch for ch in (t.node.children if t.node else []) if ch.kind == "refreshPolicy"), None)
        if rp is None:
            continue
        out.append({"table": t.name, **{k: rp.props[k].strip() for k in ("policyType", "rollingWindowGranularity",
                    "rollingWindowPeriods", "incrementalGranularity", "incrementalPeriods", "incrementalPeriodsOffset",
                    "mode") if k in rp.props},
                    "change_detection": any(r.prop == "pollingExpression" for r in model.docs[rp.file].regions if r.node is rp)})
    params = sorted(e for e in model.expressions if e.lower() in ("rangestart", "rangeend"))
    return {"verb": "refresh-policy", "action": "show", "policies": out, "parameters": params}
