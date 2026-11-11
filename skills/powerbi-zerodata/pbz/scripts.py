"""Export and apply a measure script.

The file is JSON under the workspace: version, then measures with a table, a name and an
expression. Applying it is one commit. New text that looks like a token is refused unless that
same token is already in the measure.
"""
from __future__ import annotations

import json
import os

from . import tmdl
from .common import PbzError, commit_bytes, read_text, write_text, _workspace_root
from .tokens import TOKEN_RE


def _script_expression(expression: str) -> str:
    """The expression text export writes. Apply uses this same strip."""
    return expression.strip()


def _measures(project):
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    model = tmdl.load_model(mdef)
    rows = []
    for table in model.tables.values():
        for measure in table.measures.values():
            rows.append({"table": table.name, "name": measure.name,
                         "expression": _script_expression(measure.expression)})
    return model, rows


def _shape(doc, text: str, changed: set[tuple[str, str]]) -> tuple:
    nodes = []
    for node in doc.nodes:
        parent = None if node.parent is None else (node.parent.kind, node.parent.name, node.parent.indent)
        nodes.append((node.kind, node.name, node.indent, parent, tuple(sorted(node.props.items()))))
    regions = []
    for reg in doc.regions:
        parent_name = reg.node.parent.name if reg.node.parent is not None else None
        if reg.node.kind == "measure" and reg.prop == "expression" and (parent_name, reg.node.name) in changed:
            continue
        parent = None if reg.node.parent is None else (reg.node.parent.kind, reg.node.parent.name)
        regions.append((reg.kind, reg.prop, reg.node.kind, reg.node.name, parent, text[reg.start:reg.end]))
    return tuple(nodes), tuple(regions)


def _expressions_only(rel: str, before: str, after: str, expected: dict[tuple[str, str], str]) -> bool:
    """True when the splice is the same document with only those measure expressions changed."""
    try:
        old = tmdl.parse(rel, before)
        new = tmdl.parse(rel, after)
    except Exception:
        return False
    changed = set(expected)
    if _shape(old, before, changed) != _shape(new, after, changed):
        return False
    for (table, name), expr in expected.items():
        hits = [node for node in new.nodes
                if node.kind == "measure" and node.name == name and node.parent is not None
                and node.parent.kind == "table" and node.parent.name == table]
        if len(hits) != 1:
            return False
        regs = [reg for reg in new.regions if reg.node is hits[0] and reg.prop == "expression"]
        if len(regs) != 1 or after[regs[0].start:regs[0].end] != expr:
            return False
    return True


def export(ws, project, path: str) -> dict:
    _model, rows = _measures(project)
    doc = {"version": 1, "measures": rows}
    write_text(path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n", root=_workspace_root(ws))
    return {"verb": "script", "action": "export", "measures": len(rows), "file": os.path.basename(path)}


def apply(ws, project, path: str) -> dict:
    try:
        doc = json.loads(open(path, encoding="utf-8").read())
    except (OSError, UnicodeError, ValueError) as err:
        raise PbzError("BAD_REQUEST", "script file could not be read") from err
    if not isinstance(doc, dict) or doc.get("version") != 1 or not isinstance(doc.get("measures"), list):
        raise PbzError("BAD_REQUEST", "script version must be 1")
    model, _rows = _measures(project)
    by_file: dict[str, list] = {}
    changed = []
    seen = set()
    for item in doc["measures"]:
        if not isinstance(item, dict):
            raise PbzError("BAD_REQUEST", "each measure needs table, name and expression")
        table, name, expr = item.get("table"), item.get("name"), item.get("expression")
        if not all(isinstance(x, str) and x.strip() for x in (table, name, expr)):
            raise PbzError("BAD_REQUEST", "each measure needs table, name and expression")
        found = model.measure(name)
        if found is None or found.table.lower() != table.lower() or found.node is None:
            raise PbzError("NOT_FOUND", "measure not found")
        existing = set(TOKEN_RE.findall(found.expression))
        for token in TOKEN_RE.findall(expr):
            if token not in existing:
                raise PbzError("REFUSED", "script introduced a token that is not already in the measure")
        doc_ = model.docs[found.node.file]
        reg = next((r for r in doc_.regions if r.node is found.node and r.kind == "dax"), None)
        if reg is None:
            raise PbzError("BAD_REQUEST", "measure has no expression")
        key = (found.node.file, reg.start, reg.end)
        if key in seen:
            raise PbzError("BAD_REQUEST", "duplicate measure in script")
        seen.add(key)
        if _script_expression(expr) == _script_expression(found.expression):
            continue
        by_file.setdefault(found.node.file, [])
        by_file[found.node.file].append((reg.start, reg.end, expr, found.table, found.name))
        changed.append(name)
    ops = []
    for rel, edits in by_file.items():
        before = model.docs[rel].text
        text = before
        for start, end, expr, _table, _name in sorted(edits, key=lambda item: item[0], reverse=True):
            text = text[:start] + expr + text[end:]
        expected = {(table, name): expr for _start, _end, expr, table, name in edits}
        if not _expressions_only(rel, before, text, expected):
            raise PbzError("REFUSED", "script did not keep the measure")
        path_ = os.path.join(project.mirror_model, "definition", *rel.split("/"))
        _old, bom = read_text(path_)
        ops.append(("write", path_, text, bom))
    if ops:
        commit_bytes(ops, root=ws.root)
    return {"verb": "script", "action": "apply", "measures": changed, "result": "CHANGED" if changed else "NOTHING_TO_CHANGE"}
