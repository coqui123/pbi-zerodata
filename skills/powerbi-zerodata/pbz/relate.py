"""`pbz relationship add|remove|set`: edit relationships.tmdl in the mirror with metadata guards: both
columns exist, types match, no duplicate, and no second active filter path between two tables (Desktop
rejects ambiguous models). Data checks (key uniqueness, orphans) stay with `pbz check`. Names only."""
from __future__ import annotations

import os
import re
import uuid

from . import lexers, tmdl
from .common import PbzError, commit_bytes, read_text
from .tokens import apply_edits

REF = (re.compile(r"^\s*'((?:[^']|'')+)'\s*\[(.+)\]\s*$"), re.compile(r"^\s*([^\[\]']+?)\s*\[(.+)\]\s*$"))


def _model(project):
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    for path in tmdl.tmdl_files(mdef):
        rel = os.path.relpath(path, project.ws.mirror).replace("\\", "/")
        try:
            read_text(path)
        except (OSError, UnicodeError):
            raise PbzError("REFUSED", rel) from None
    return mdef, tmdl.load_model(mdef)


def _col(model, ref: str):
    for rx in REF:
        m = rx.match(ref)
        if m:
            c = model.column(m.group(1).replace("''", "'"), m.group(2))
            if c is None:
                raise PbzError("NOT_FOUND", ref)
            return c
    raise PbzError("BAD_REQUEST", f"{ref}: use 'Table'[Column]")


def _endpoint(c) -> str:
    return f"{lexers.tmdl_name(c.table)}.{lexers.tmdl_name(c.name)}"


def _find(model, a, b):
    ends = {(a.table.lower(), a.name.lower()), (b.table.lower(), b.name.lower())}
    return [r for r in model.relationships
            if {(r.from_table.lower(), r.from_column.lower()), (r.to_table.lower(), r.to_column.lower())} == ends]


def _active_path(model, t1: str, t2: str, skip=None) -> bool:
    """Is t2 reachable from t1 over active relationships (either direction)?"""
    adj: dict[str, set] = {}
    for r in model.relationships:
        if r.active and r is not skip:
            adj.setdefault(r.from_table.lower(), set()).add(r.to_table.lower())
            adj.setdefault(r.to_table.lower(), set()).add(r.from_table.lower())
    seen, todo = {t1.lower()}, [t1.lower()]
    while todo:
        x = todo.pop()
        for y in adj.get(x, ()):
            if y == t2.lower():
                return True
            if y not in seen:
                seen.add(y)
                todo.append(y)
    return False


def _rel_path(mdef: str) -> tuple[str, str, bytes]:
    path = os.path.join(mdef, "relationships.tmdl")
    if os.path.exists(path):
        text, bom = read_text(path)
        return path, text, bom
    return path, "", b""


def plan_add(model, text: str, from_ref: str, to_ref: str, inactive: bool = False, both: bool = False,
             many_to_many: bool = False, force: bool = False) -> tuple[str, dict]:
    """Plan one relationship onto `text` and the in-memory model. Does not write."""
    a, b = _col(model, from_ref), _col(model, to_ref)
    if a.table.lower() == b.table.lower():
        raise PbzError("BAD_REQUEST", "both columns are in the same table")
    if _find(model, a, b):
        raise PbzError("DUPLICATE", "a relationship between these columns exists; use `pbz relationship set`")
    if a.data_type.lower() != b.data_type.lower() and not force:
        raise PbzError("TYPE_MISMATCH", f"{a.data_type} vs {b.data_type} (pass --force to add it anyway)")
    if not inactive and _active_path(model, a.table, b.table):
        raise PbzError("AMBIGUOUS_PATH", "the tables are already connected by active relationships; add it --inactive "
                                         "and activate it with USERELATIONSHIP")
    eol = "\r\n" if "\r\n" in text else "\n"
    name = str(uuid.uuid4())
    lines = [f"relationship {name}"]
    if inactive:
        lines.append("\tisActive: false")
    if both:
        lines.append("\tcrossFilteringBehavior: bothDirections")
    if many_to_many:
        lines.append("\ttoCardinality: many")
    lines += [f"\tfromColumn: {_endpoint(a)}", f"\ttoColumn: {_endpoint(b)}"]
    block = eol.join(lines) + eol
    new = (text.rstrip("\r\n") + eol + eol if text.strip() else "") + block
    model.relationships.append(tmdl.Relationship(
        name, a.table, a.name, b.table, b.name, both, not inactive, None))
    label = f"{a.table}[{a.name}] -> {b.table}[{b.name}]"
    return new, {"relationship": label, "name": name,
                 "checks": {"types_match": a.data_type.lower() == b.data_type.lower(), "active": not inactive,
                            "cross_filter": "both" if both else "single",
                            "cardinality": "many-many" if many_to_many else "many-one"},
                 "verify": f"pbz check: unique on {b.table}[{b.name}], no_orphans from {a.table}[{a.name}] (Desktop open)"}


def _mirror_rel(ws, path: str) -> str:
    return os.path.relpath(path, ws.mirror).replace("\\", "/")


def _apply(path: str, bom: bytes, body: str, dry_run: bool, out: dict, rel: str) -> dict:
    out["files"] = [rel]
    if dry_run:
        out["result"] = "DRY_RUN"
        return out
    commit_bytes([("write", path, body, bom)])
    return out


def add(ws, project, from_ref: str, to_ref: str, inactive: bool = False, both: bool = False,
        many_to_many: bool = False, force: bool = False, dry_run: bool = False) -> dict:
    mdef, model = _model(project)
    path, text, bom = _rel_path(mdef)
    new, info = plan_add(model, text, from_ref, to_ref, inactive, both, many_to_many, force)
    out = {"verb": "relationship", "action": "added", "relationship": info["relationship"], "name": info["name"],
           "checks": info["checks"], "verify": info["verify"], "next": "pbz diff, pbz push"}
    return _apply(path, bom, new, dry_run, out, _mirror_rel(ws, path))


def remove(ws, project, from_ref: str, to_ref: str, dry_run: bool = False) -> dict:
    mdef, model = _model(project)
    a, b = _col(model, from_ref), _col(model, to_ref)
    hits = _find(model, a, b)
    if not hits:
        raise PbzError("NOT_FOUND", "no relationship between these columns")
    r = hits[0]
    doc = model.docs[r.node.file]
    s, e = tmdl.block_span(doc, r.node)
    path = os.path.join(mdef, *r.node.file.split("/"))
    _t, bom = read_text(path)
    out = {"verb": "relationship", "action": "removed", "relationship": f"{a.table}[{a.name}] -> {b.table}[{b.name}]",
           "next": "pbz unused / pbz validate (measures using USERELATIONSHIP on it break), pbz diff, pbz push"}
    return _apply(path, bom, apply_edits(doc.text, [(s, e, "")]), dry_run, out, _mirror_rel(ws, path))


def set_(ws, project, from_ref: str, to_ref: str, active: bool | None = None, both: bool | None = None,
         dry_run: bool = False) -> dict:
    mdef, model = _model(project)
    a, b = _col(model, from_ref), _col(model, to_ref)
    hits = _find(model, a, b)
    if not hits:
        raise PbzError("NOT_FOUND", "no relationship between these columns")
    r = hits[0]
    if active and not r.active and _active_path(model, r.from_table, r.to_table, skip=r):
        raise PbzError("AMBIGUOUS_PATH", "activating it would create a second active path between the tables")
    doc = model.docs[r.node.file]
    s, e = tmdl.block_span(doc, r.node)
    block = doc.text[s:e]

    def drop(prop, blk):
        return re.sub(rf"(?m)^\t{prop}\s*:.*\r?\n", "", blk)

    edits_block = block
    if active is True:
        edits_block = drop("isActive", edits_block)
    if both is False:
        edits_block = drop("crossFilteringBehavior", edits_block)
    sub = tmdl.parse(doc.path, edits_block)
    node = next((n for n in sub.nodes if n.kind == "relationship"), None)
    eds = []
    if active is False and node is not None:
        eds += tmdl.prop_edit(sub, node, "isActive", "false")
    if both is True and node is not None:
        eds += tmdl.prop_edit(sub, node, "crossFilteringBehavior", "bothDirections")
    edits_block = apply_edits(edits_block, sorted(eds)) if eds else edits_block
    if edits_block == block:
        return {"verb": "relationship", "action": "unchanged",
                "relationship": f"{a.table}[{a.name}] -> {b.table}[{b.name}]", "files": []}
    path = os.path.join(mdef, *r.node.file.split("/"))
    _t, bom = read_text(path)
    out = {"verb": "relationship", "action": "updated", "relationship": f"{a.table}[{a.name}] -> {b.table}[{b.name}]",
           "active": active if active is not None else r.active,
           "cross_filter": ("both" if both else "single") if both is not None else ("both" if r.both else "single"),
           "next": "pbz diff, pbz push"}
    return _apply(path, bom, doc.text[:s] + edits_block + doc.text[e:], dry_run, out, _mirror_rel(ws, path))
