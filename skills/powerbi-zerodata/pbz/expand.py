"""DEFINE block for a measure, its dependency closure and the UDFs it calls.

The text is the sanitized mirror. It is not executed against the real model.
"""
from __future__ import annotations

import os

from . import deps, tmdl
from .common import PbzError


def _measure(model, target: str):
    name = target.strip()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    found = model.measure(name)
    if found is None:
        raise PbzError("NOT_FOUND", "measure not found")
    return found


def run(ws, project, target: str) -> dict:
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    model = tmdl.load_model(mdef)
    root = _measure(model, target)
    res = deps.Resolver(model)
    order, seen, functions = [], set(), []
    fn_seen = set()

    def walk(name: str):
        key = name.lower()
        if key in seen:
            return
        found = model.measure(name)
        if found is None:
            return
        seen.add(key)
        info = deps.dax(found.expression, found.table, res)
        for ref in info.refs:
            if ref.kind == "measure":
                walk(ref.name)
            elif ref.kind == "function" and ref.name.lower() not in fn_seen:
                fn_seen.add(ref.name.lower())
                functions.append(ref.name)
        order.append(found)

    walk(root.name)
    lines = ["DEFINE"]
    for measure in order:
        expr = " ".join(measure.expression.split())
        lines.append(f"MEASURE '{measure.table}'[{measure.name}] = {expr}")
    for name in functions:
        node = model.functions.get(name)
        body = ""
        if node is not None:
            doc = model.docs.get(node.file)
            if doc is not None:
                reg = next((r for r in doc.regions if r.node is node and r.kind == "dax"), None)
                if reg is not None:
                    body = " ".join(doc.text[reg.start:reg.end].split())
        lines.append(f"FUNCTION {name} = {body}".rstrip())
    return {"verb": "expand", "project": project.name, "measure": root.name,
            "measures": [m.name for m in order], "functions": functions,
            "dax": "\n".join(lines)}
