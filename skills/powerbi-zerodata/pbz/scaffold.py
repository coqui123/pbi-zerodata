"""`pbz new <kind>`: write complete, Desktop-shaped TMDL for the structures agents most often hand-write
wrongly: measure table, date table, calculation group, field parameter. Mirror only (then diff / push).
Every object gets a fresh lineageTag; table files are added and referenced from model.tmdl."""
from __future__ import annotations

import os
import re
import uuid

from . import lexers, tmdl
from .common import PbzError, commit_bytes, read_text, write_text
from .tokens import TOKEN_RE, apply_edits

TI_ITEMS = {  # calculation item name -> DAX template ({date} = the date column), optional format string
    "Current": ("SELECTEDMEASURE()", None),
    "MTD": ("CALCULATE(SELECTEDMEASURE(), DATESMTD({date}))", None),
    "QTD": ("CALCULATE(SELECTEDMEASURE(), DATESQTD({date}))", None),
    "YTD": ("CALCULATE(SELECTEDMEASURE(), DATESYTD({date}))", None),
    "PY": ("CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR({date}))", None),
    "PY YTD": ("CALCULATE(SELECTEDMEASURE(), DATESYTD(SAMEPERIODLASTYEAR({date})))", None),
    "YoY": ("\n\t\t\t\tVAR cur = SELECTEDMEASURE()\n\t\t\t\tVAR py = CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR({date}))\n"
            "\t\t\t\tRETURN IF(NOT ISBLANK(cur) && NOT ISBLANK(py), cur - py)", None),
    "YoY %": ("\n\t\t\t\tVAR cur = SELECTEDMEASURE()\n\t\t\t\tVAR py = CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR({date}))\n"
              "\t\t\t\tRETURN DIVIDE(cur - py, py)", '"0.0%"'),
}


def _tag() -> str:
    return str(uuid.uuid4())


def _file_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def _q(name: str) -> str:
    return lexers.tmdl_name(name)


def _model(project):
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    return mdef, tmdl.load_model(mdef)


def _check_new_table(model, name: str) -> None:
    if not name.strip() or name != name.strip() or re.search(r"[\r\n\t]", name):
        raise PbzError("BAD_REQUEST", "table names cannot be blank, padded or contain control characters")
    if model.table(name) or any(e.lower() == name.lower() for e in model.expressions):
        raise PbzError("NAME_TAKEN", name)


def _literal_guard(*texts: str) -> None:
    for t in texts:
        if TOKEN_RE.search(t or ""):
            raise PbzError("BAD_REQUEST", "mirror tokens cannot be used as names or labels")


def _plan_table(mdef: str, model, name: str, text: str, model_flags: list[str] = ()) -> list[tuple[str, str]]:
    """New table file and its model.tmdl ref, as (path, text). Does not write."""
    rel = f"tables/{_file_name(name)}.tmdl"
    path = os.path.join(mdef, *rel.split("/"))
    if os.path.exists(path):
        raise PbzError("NAME_TAKEN", f"file {rel} exists")
    eol = "\r\n" if "\r\n" in (model.docs.get("model.tmdl").text if "model.tmdl" in model.docs else "") else "\n"
    planned = [(path, text.replace("\n", eol))]
    mt = model.docs.get("model.tmdl")
    if mt is None:
        raise PbzError("NO_MIRROR", "model.tmdl is missing; run `pbz pull` first")
    refs = list(re.finditer(r"(?m)^ref table .*$", mt.text))
    line = f"ref table {_q(name)}"
    if refs:
        at = refs[-1].end()
        edits = [(at, at, eol + line)]
    else:
        edits = [(len(mt.text), len(mt.text), ("" if mt.text.endswith(("\n", "\r")) else eol) + eol + line + eol)]
    if model.model_node is not None:
        for flag in model_flags:
            edits += tmdl.prop_edit(mt, model.model_node, flag, None)
    planned.append((os.path.join(mdef, "model.tmdl"), apply_edits(mt.text, sorted(edits))))
    return planned


def _write_table(project, mdef: str, model, name: str, text: str, model_flags: list[str] = ()) -> list[str]:
    files = []
    for path, body in _plan_table(mdef, model, name, text, model_flags):
        rel = os.path.relpath(path, mdef).replace("\\", "/")
        if os.path.basename(path) == "model.tmdl":
            _t, bom = read_text(path)
            write_text(path, body, bom)
        else:
            write_text(path, body)
        files.append(f"{project.name}.SemanticModel/definition/{rel}")
    return files


def _column(name: str, dtype: str, source: str, *, hidden=False, fmt="", sort_by="", summarize="none",
            key=False, extra: str = "") -> str:
    lines = [f"\tcolumn {_q(name)}", f"\t\tdataType: {dtype}"]
    if key:
        lines.append("\t\tisKey")
    if hidden:
        lines.append("\t\tisHidden")
    if fmt:
        lines.append(f"\t\tformatString: {fmt}")
    lines += [f"\t\tlineageTag: {_tag()}", f"\t\tsummarizeBy: {summarize}", f"\t\tsourceColumn: {source}"]
    if sort_by:
        lines.append(f"\t\tsortByColumn: {_q(sort_by)}")
    body = "\n".join(lines) + "\n"
    if extra:
        body += "\n" + extra
    return body + "\n\t\tannotation SummarizationSetBy = Automatic\n\n"


def measure_table(ws, project, name: str) -> dict:
    mdef, model = _model(project)
    _check_new_table(model, name)
    _literal_guard(name)
    text = (f"table {_q(name)}\n\tlineageTag: {_tag()}\n\n"
            + _column("Value", "int64", "[Value]", hidden=True, fmt="0")
            + f"\tpartition {_q(name)} = calculated\n\t\tmode: import\n\t\tsource = ROW(\"Value\", 0)\n")
    files = _write_table(project, mdef, model, name, text)
    return {"verb": "new", "kind": "measure-table", "table": name, "files": files,
            "next": f"pbz move measure <name> --to {_q(name)} for each measure, then pbz diff / pbz push"}


def date_table(ws, project, name: str, start: int, end: int, relate: list[str] = ()) -> dict:
    mdef, model = _model(project)
    _check_new_table(model, name)
    if not (1900 <= start <= end <= 2200):
        raise PbzError("BAD_REQUEST", "--start and --end are years, start <= end")
    if any(t.node is not None and t.node.props.get("dataCategory", "").strip().lower() == "time"
           for t in model.tables.values() if not t.private):
        raise PbzError("AMBIGUOUS", "the model already has a marked date table")
    qt = lexers.dax_table_name(name)
    expr = ("\n\t\t\t\tADDCOLUMNS(\n"
            f"\t\t\t\t    CALENDAR(DATE({start}, 1, 1), DATE({end}, 12, 31)),\n"
            "\t\t\t\t    \"Year\", YEAR([Date]),\n"
            "\t\t\t\t    \"Quarter\", \"Q\" & QUARTER([Date]),\n"
            "\t\t\t\t    \"Month Number\", MONTH([Date]),\n"
            "\t\t\t\t    \"Month\", FORMAT([Date], \"MMM\"),\n"
            "\t\t\t\t    \"Year Month\", FORMAT([Date], \"YYYY-MM\"),\n"
            "\t\t\t\t    \"Weekday Number\", WEEKDAY([Date], 2),\n"
            "\t\t\t\t    \"Weekday\", FORMAT([Date], \"ddd\")\n"
            "\t\t\t\t)\n")
    text = (f"table {_q(name)}\n\tlineageTag: {_tag()}\n\tdataCategory: Time\n\n"
            + _column("Date", "dateTime", "[Date]", fmt="yyyy-mm-dd", key=True)
            + _column("Year", "int64", "[Year]", fmt="0")
            + _column("Quarter", "string", "[Quarter]")
            + _column("Month Number", "int64", "[Month Number]", hidden=True, fmt="0")
            + _column("Month", "string", "[Month]", sort_by="Month Number")
            + _column("Year Month", "string", "[Year Month]")
            + _column("Weekday Number", "int64", "[Weekday Number]", hidden=True, fmt="0")
            + _column("Weekday", "string", "[Weekday]", sort_by="Weekday Number")
            + f"\thierarchy Calendar\n\t\tlineageTag: {_tag()}\n\n"
            + "".join(f"\t\tlevel {_q(lv)}\n\t\t\tlineageTag: {_tag()}\n\t\t\tcolumn: {_q(lv)}\n\n"
                      for lv in ("Year", "Quarter", "Month", "Date"))
            + f"\tpartition {_q(name)} = calculated\n\t\tmode: import\n\t\tsource ={expr}")
    planned = _plan_table(mdef, model, name, text)
    texts = {rel: doc.text for rel, doc in model.docs.items()}
    for path, body in planned:
        texts[os.path.relpath(path, mdef).replace("\\", "/")] = body
    mem = tmdl.load_model(mdef, texts)
    rel_text = texts.get("relationships.tmdl", "")
    labels = []
    if relate:
        from . import relate as rel_mod
        for ref in relate:
            rel_text, info = rel_mod.plan_add(mem, rel_text, ref, f"{qt}[Date]")
            labels.append(info["relationship"])
    ops = []
    files = []
    for path, body in planned:
        rel = os.path.relpath(path, mdef).replace("\\", "/")
        files.append(f"{project.name}.SemanticModel/definition/{rel}")
        bom = read_text(path)[1] if os.path.isfile(path) else b""
        ops.append(("write", path, body, bom))
    if relate:
        rpath = os.path.join(mdef, "relationships.tmdl")
        bom = read_text(rpath)[1] if os.path.isfile(rpath) else b""
        ops.append(("write", rpath, rel_text, bom))
        files.append(f"{project.name}.SemanticModel/definition/relationships.tmdl")
    commit_bytes(ops)
    out = {"verb": "new", "kind": "date-table", "table": name, "files": files, "marked": True,
           "next": "pbz relationship add ... (or --relate), pbz diff, pbz push, then `pbz refresh` with Desktop open"}
    if labels:
        out["relationships"] = labels
    return out


def calc_group(ws, project, name: str, date_col: str, items: list[str], precedence: int | None = None) -> dict:
    mdef, model = _model(project)
    _check_new_table(model, name)
    m = re.match(r"^\s*'((?:[^']|'')+)'\s*\[(.+)\]\s*$", date_col) or re.match(r"^\s*([^\[\]']+?)\s*\[(.+)\]\s*$", date_col)
    if not m or not model.column(m.group(1).replace("''", "'"), m.group(2)):
        raise PbzError("NOT_FOUND", "--date must be an existing 'Table'[Column]")
    dt, dc = m.group(1).replace("''", "'"), m.group(2)
    date_ref = f"{lexers.dax_table_name(dt)}[{dc.replace(']', ']]')}]"
    bad = [i for i in items if i not in TI_ITEMS]
    if bad:
        raise PbzError("BAD_REQUEST", "items must be from: " + ", ".join(TI_ITEMS))
    used = {int(p.strip()) for t in model.tables.values() for c in (t.node.children if t.node else [])
            if c.kind == "calculationGroup" for p in [c.props.get("precedence", "0")] if p.strip().isdigit()}
    prec = precedence if precedence is not None else (max(used) + 1 if used else 1)
    if prec in used:
        raise PbzError("NAME_TAKEN", f"precedence {prec} is used by another calculation group")
    body = []
    for n, item in enumerate(items):
        expr, fmt = TI_ITEMS[item]
        dax = expr.format(date=date_ref)
        line = f"\t\tcalculationItem {_q(item)} =" + (dax if dax.startswith("\n") else " " + dax)
        body.append(line.rstrip() + f"\n\t\t\tordinal: {n}\n")
        if fmt:
            body.append(f"\t\t\tformatStringDefinition = {fmt}\n")
        body.append("\n")
    text = (f"table {_q(name)}\n\tlineageTag: {_tag()}\n\n\tcalculationGroup\n\t\tprecedence: {prec}\n\n" + "".join(body)
            + _column(name if name.lower() != "ordinal" else "Calculation", "string", "Name", sort_by="Ordinal")
            + _column("Ordinal", "int64", "Ordinal", hidden=True, summarize="sum", fmt="0")
            + f"\tpartition {_q(name)} = calculationGroup\n\t\tmode: import\n")
    flags = ["discourageImplicitMeasures"] if model.model_node is not None and not model.model_node.flag(
        "discourageImplicitMeasures") else []
    files = _write_table(project, mdef, model, name, text, model_flags=flags)
    return {"verb": "new", "kind": "calc-group", "table": name, "items": items, "precedence": prec, "files": files,
            **({"model_setting": "discourageImplicitMeasures turned on (calculation groups need explicit measures)"} if flags else {}),
            "next": "pbz diff, pbz push, then bind the calculation column to a slicer or matrix"}


def field_parameter(ws, project, name: str, fields: list[str], labels: list[str] | None = None) -> dict:
    mdef, model = _model(project)
    _check_new_table(model, name)
    if not fields:
        raise PbzError("BAD_REQUEST", "--fields needs at least one [Measure] or 'Table'[Column]")
    labels = labels or []
    if labels and len(labels) != len(fields):
        raise PbzError("BAD_REQUEST", "--labels must match --fields one to one")
    _literal_guard(name, *labels)
    rows = []
    for i, f in enumerate(fields):
        m = re.match(r"^\s*'((?:[^']|'')+)'\s*\[(.+)\]\s*$", f) or re.match(r"^\s*([^\[\]']+?)\s*\[(.+)\]\s*$", f)
        if m:
            t, c = m.group(1).replace("''", "'"), m.group(2)
            if not (model.column(t, c) or (model.measure(c) and model.measure(c).table.lower() == t.lower())):
                raise PbzError("NOT_FOUND", f)
        else:
            m2 = re.match(r"^\s*\[(.+)\]\s*$", f)
            mm = model.measure(m2.group(1)) if m2 else None
            if mm is None:
                raise PbzError("NOT_FOUND", f)
            t, c = mm.table, mm.name
        label = labels[i] if labels else c
        ref = f"{lexers.dax_table_name(t)}[{c.replace(']', ']]')}]"
        rows.append(f"\t\t\t\t    ({lexers.dax_string(label)}, NAMEOF({ref}), {i})")
    meta = "\t\textendedProperty ParameterMetadata =\n\t\t\t\t{\n\t\t\t\t  \"version\": 3,\n\t\t\t\t  \"kind\": 2\n\t\t\t\t}\n"
    rel = f"\t\trelatedColumnDetails\n\t\t\tgroupByColumn: {_q(name + ' Fields')}\n"
    text = (f"table {_q(name)}\n\tlineageTag: {_tag()}\n\n"
            + _column(name, "string", "[Value1]", sort_by=f"{name} Order", extra=rel)
            + _column(f"{name} Fields", "string", "[Value2]", hidden=True, sort_by=f"{name} Order", extra=meta)
            + _column(f"{name} Order", "int64", "[Value3]", hidden=True, fmt="0", summarize="sum")
            + f"\tpartition {_q(name)} = calculated\n\t\tmode: import\n\t\tsource =\n\t\t\t\t{{\n"
            + ",\n".join(rows) + "\n\t\t\t\t}\n")
    files = _write_table(project, mdef, model, name, text)
    return {"verb": "new", "kind": "field-parameter", "table": name, "fields": len(fields), "files": files,
            "next": f"pbz diff, pbz push, then bind {_q(name)}[{name}] (the visible column) to a slicer and visuals"}


def run(ws, project, a) -> dict:
    if a.kind == "measure-table":
        return measure_table(ws, project, a.name)
    if a.kind == "date-table":
        if a.start is None or a.end is None:
            raise PbzError("BAD_REQUEST", "--start and --end years are required (no data-derived range)")
        return date_table(ws, project, a.name, a.start, a.end, a.relate or [])
    if a.kind == "calc-group":
        return calc_group(ws, project, a.name, a.date or "", a.items or ["Current", "YTD", "PY", "YoY", "YoY %"],
                          a.precedence)
    if a.kind == "field-parameter":
        return field_parameter(ws, project, a.name, a.fields or [], a.labels)
    raise PbzError("BAD_REQUEST", a.kind)

