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


def _resolve_function(model, name: str):
    """Function node by DAX's case-insensitive name, or None."""
    if not name:
        return None
    found = model.functions.get(name)
    if found is not None:
        return found
    low = name.lower()
    for key, node in model.functions.items():
        if key.lower() == low or getattr(node, "name", "").lower() == low:
            return node
    return None


def _function_body(model, node) -> str:
    doc = model.docs.get(node.file)
    if doc is None:
        low = (node.file or "").lower()
        doc = next((item for path, item in model.docs.items() if path.lower() == low), None)
    if doc is None:
        return ""
    reg = next((r for r in doc.regions if r.node is node and r.kind == "dax"), None)
    if reg is None:
        return ""
    return doc.text[reg.start:reg.end]


def run(ws, project, target: str) -> dict:
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    model = tmdl.load_model(mdef)
    root = _measure(model, target)
    res = deps.Resolver(model)
    order, seen = [], set()
    functions: list[str] = []
    bodies: dict[str, str] = {}
    fn_seen: set[str] = set()

    def walk_function(name: str):
        key = name.lower()
        if key in fn_seen:
            return
        fn_seen.add(key)
        node = _resolve_function(model, name)
        if node is None:
            return
        raw = _function_body(model, node)
        body = " ".join(raw.split())
        if not body:
            return
        info = deps.dax(raw, "", res, function=True)
        for ref in info.refs:
            if ref.kind == "measure":
                walk(ref.name)
            elif ref.kind == "function":
                walk_function(ref.name)
        functions.append(node.name)
        bodies[node.name] = body

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
            elif ref.kind == "function":
                walk_function(ref.name)
        order.append(found)

    walk(root.name)
    lines = ["DEFINE"]
    for measure in order:
        expr = " ".join(measure.expression.split())
        lines.append(f"MEASURE '{measure.table}'[{measure.name}] = {expr}")
    for name in functions:
        lines.append(f"FUNCTION {name} = {bodies[name]}")
    return {"verb": "expand", "project": project.name, "measure": root.name,
            "measures": [m.name for m in order], "functions": functions,
            "dax": "\n".join(lines)}
