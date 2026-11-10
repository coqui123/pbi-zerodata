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


def _measures(project):
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    model = tmdl.load_model(mdef)
    rows = []
    for table in model.tables.values():
        for measure in table.measures.values():
            rows.append({"table": table.name, "name": measure.name, "expression": measure.expression.strip()})
    return model, rows


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
        by_file.setdefault(found.node.file, [])
        key = (found.node.file, reg.start, reg.end)
        if any(start == reg.start and end == reg.end for start, end, _expr in by_file[found.node.file]):
            raise PbzError("BAD_REQUEST", "duplicate measure in script")
        by_file[found.node.file].append((reg.start, reg.end, expr))
        changed.append(name)
    ops = []
    for rel, edits in by_file.items():
        text = model.docs[rel].text
        for start, end, expr in sorted(edits, reverse=True):
            text = text[:start] + expr + text[end:]
        path_ = os.path.join(project.mirror_model, "definition", *rel.split("/"))
        _old, bom = read_text(path_)
        ops.append(("write", path_, text, bom))
    if ops:
        commit_bytes(ops, root=ws.root)
    return {"verb": "script", "action": "apply", "measures": changed, "result": "CHANGED" if changed else "NOTHING_TO_CHANGE"}
