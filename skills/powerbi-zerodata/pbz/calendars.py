"""Author a calendar or a KPI in the mirror, then read the block back with the TMDL parser.

Offline tests stop at the parser. `tmdl-validate` remains available when Desktop is open;
this module does not require it.
"""
from __future__ import annotations

import os

from . import lexers, tmdl
from .common import PbzError, commit_bytes, read_text


def _model(project):
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    return mdef, tmdl.load_model(mdef)


def _insert(mdef, doc, node, block: str, root: str):
    _s, end = tmdl.block_span(doc, node)
    eol = "\r\n" if "\r\n" in doc.text else "\n"
    if end > 0 and doc.text[end - 1] != "\n":
        block = eol + block
    text = doc.text[:end] + block + ("" if block.endswith(eol) else eol) + doc.text[end:]
    path = os.path.join(mdef, *doc.path.split("/"))
    _old, bom = read_text(path)
    commit_bytes([("write", path, text, bom)], root=root)


def _named_calendar(table_node, name: str):
    want = name.lower()
    return next((node for node in table_node.children
                 if node.kind == "calendar" and node.name.lower() == want), None)


def _definition_path(mdef: str, doc) -> str:
    return os.path.join(mdef, *doc.path.split("/"))


def add_calendar(ws, project, table: str, name: str, column: str, unit: str = "Day") -> dict:
    units = {"Day", "Month", "Quarter", "Year", "Week"}
    if unit not in units:
        raise PbzError("BAD_REQUEST", "unit must be Day, Month, Quarter, Year or Week")
    mdef, model = _model(project)
    found = next((t for t in model.tables.values() if t.name.lower() == table.lower()), None)
    if found is None or found.node is None:
        raise PbzError("NOT_FOUND", "table not found")
    if _named_calendar(found.node, name) is not None:
        raise PbzError("BAD_REQUEST", "calendar already exists")
    ind = "\t" * (found.node.indent + 1)
    block = (f"{ind}calendar {lexers.tmdl_name(name)}\n"
             f"{ind}\tcalendarColumnGroup\n"
             f"{ind}\t\tcolumn: {column}\n"
             f"{ind}\t\tunit: {unit}\n")
    doc = model.docs[found.node.file]
    path = _definition_path(mdef, doc)
    with open(path, "rb") as handle:
        previous = handle.read()
    _insert(mdef, doc, found.node, block, ws.root)
    node = None
    try:
        again = tmdl.load_model(mdef)
        found_again = next((t for t in again.tables.values() if t.name.lower() == found.name.lower()), None)
        if found_again is not None and found_again.node is not None:
            node = _named_calendar(found_again.node, name)
    except Exception:
        node = None
    if node is None:
        commit_bytes([("write_bytes", path, previous)], root=ws.root)
        raise PbzError("BAD_REQUEST", "calendar block did not parse")
    return {"verb": "calendar", "action": "add", "table": found.name, "name": node.name, "unit": unit}


def set_kpi(ws, project, measure: str, target: str | None, status: str | None, trend: str | None) -> dict:
    if not any((target, status, trend)):
        raise PbzError("BAD_REQUEST", "pass --target, --status or --trend")
    mdef, model = _model(project)
    name = measure.strip()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    found = model.measure(name)
    if found is None or found.node is None:
        raise PbzError("NOT_FOUND", "measure not found")
    if any(c.kind == "kpi" for c in (found.node.children or [])):
        raise PbzError("BAD_REQUEST", "kpi already exists")
    ind = "\t" * (found.node.indent + 1)
    lines = [f"{ind}kpi"]
    if target:
        lines.append(f"{ind}\ttargetExpression = {target}")
    if status:
        lines.append(f"{ind}\tstatusExpression = {status}")
    if trend:
        lines.append(f"{ind}\ttrendExpression = {trend}")
    doc = model.docs[found.node.file]
    _insert(mdef, doc, found.node, "\n".join(lines) + "\n", ws.root)
    again = tmdl.load_model(mdef)
    node = next((n for n in again.docs[found.node.file].nodes
                 if n.kind == "kpi" and n.parent is not None and n.parent.name.lower() == found.name.lower()), None)
    if node is None:
        raise PbzError("BAD_REQUEST", "kpi block did not parse")
    return {"verb": "kpi", "action": "set", "measure": found.name,
            "set": [p for p, v in (("target", target), ("status", status), ("trend", trend)) if v]}
