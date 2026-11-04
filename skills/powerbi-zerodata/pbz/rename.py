"""Structured rename of a measure or column across the mirror's TMDL and PBIR files."""
from __future__ import annotations

import json
import os
import re

from . import lexers, pbir, tmdl
from .common import PbzError, read_text, write_text
from .tokens import apply_edits


def _bracket(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _decl_edit(text: str, node, kind: str, new: str):
    lines = text.splitlines(keepends=True)
    off = sum(len(l) for l in lines[:node.line])
    line = lines[node.line]
    m = re.match(rf"^(\t*{kind}\s+)('(?:[^']|'')*'|[^\s=]+)", line)
    if not m:
        return None
    return (off + m.start(2), off + m.end(2), lexers.tmdl_name(new))


def rename(ws, project, kind: str, old: str, new: str, table: str | None = None) -> dict:
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    model = tmdl.load_model(mdef)
    if kind == "measure":
        m = model.measure(old)
        if not m:
            raise PbzError("NOT_FOUND", f"measure {old}")
        if model.measure(new) or model.column(m.table, new):
            raise PbzError("NAME_TAKEN", new)
        if any(c.name.lower() == old.lower() for c in model.all_columns()):
            raise PbzError("AMBIGUOUS", f"a column is also named {old}; rename it in Desktop")
        home = m.table
    elif kind == "column":
        hits = [c for c in model.all_columns() if c.name.lower() == old.lower() and (not table or c.table.lower() == table.lower())]
        if len(hits) != 1:
            raise PbzError("AMBIGUOUS" if hits else "NOT_FOUND", f"column {old}; pass --table")
        home = hits[0].table
        if model.column(home, new) or model.measure(new):
            raise PbzError("NAME_TAKEN", new)
    else:
        raise PbzError("UNSUPPORTED", "rename supports measure and column (rename tables in Desktop)")

    changed = []
    # ---- TMDL
    for rel, doc in model.docs.items():
        text = doc.text
        edits = []
        for node in doc.nodes:
            if node.kind == kind and node.name.lower() == old.lower() and node.parent and node.parent.name == home:
                e = _decl_edit(text, node, kind, new)
                if e:
                    edits.append(e)
        for reg in doc.regions:
            if reg.kind != "dax":
                continue
            cur = None
            n = reg.node
            while n is not None and n.kind != "table":
                n = n.parent
            cur = n.name if n else None
            sig = lexers.significant(lexers.lex_dax(text[reg.start:reg.end], reg.start))
            for i, t in enumerate(sig):
                if t.kind != "bracket" or t.value.lower() != old.lower():
                    continue
                prev = sig[i - 1] if i else None
                qualified = prev is not None and prev.end == t.start and prev.kind in ("table", "ident")
                qtable = prev.value if qualified else None
                if kind == "measure" and (not qualified or qtable.lower() == home.lower()):
                    edits.append((t.start, t.end, _bracket(new)))
                elif kind == "column" and ((qualified and qtable.lower() == home.lower()) or
                                           (not qualified and cur and cur.lower() == home.lower())):
                    edits.append((t.start, t.end, _bracket(new)))
        if kind == "column":
            for m_ in re.finditer(r"(?m)^(\t*(?:sortByColumn|column)\s*:\s*)('(?:[^']|'')*'|\S+)\s*$", text):
                if tmdl.unquote(m_.group(2)).lower() == old.lower() and rel == model.tables[home].file:
                    edits.append((m_.start(2), m_.end(2), lexers.tmdl_name(new)))
            for m_ in re.finditer(r"(?m)^(\t*(?:fromColumn|toColumn)\s*:\s*)(.+?)\s*$", text):
                parts = tmdl.split_dotted(m_.group(2))
                if len(parts) == 2 and parts[0].lower() == home.lower() and parts[1].lower() == old.lower():
                    edits.append((m_.start(2), m_.end(2), f"{lexers.tmdl_name(parts[0])}.{lexers.tmdl_name(new)}"))
        if edits:
            new_text = apply_edits(text, edits)
            path = os.path.join(mdef, *rel.split("/"))
            _t, bom = read_text(path)
            write_text(path, new_text, bom)
            changed.append(f"{project.name}.SemanticModel/definition/{rel}")
    # ---- PBIR
    rdir = project.mirror_report
    for path in pbir.report_files(rdir):
        text, bom = read_text(path)
        try:
            root = pbir.parse_json(text)
        except pbir.JsonError:
            continue
        edits = []
        want = "measure" if kind == "measure" else "column"
        for ref in pbir.walk_fields(root, model):
            k = "column" if ref.kind == "aggregation" else ref.kind
            if k == want and ref.name.lower() == old.lower() and ref.table.lower() == home.lower() and ref.prop_node:
                edits.append((ref.prop_node.start, ref.prop_node.end, json.dumps(new, ensure_ascii=False)))
        for key, node in pbir.string_nodes(root):
            if key in ("queryRef", "nativeQueryRef", "metadata") and old.lower() in node.value.lower():
                v = node.value
                nv = re.sub(rf"(?i)(?<![\w]){re.escape(home)}\.{re.escape(old)}(?![\w])", f"{home}.{new}", v)
                if key == "nativeQueryRef" and v.lower() == old.lower():
                    nv = new
                if nv != v:
                    edits.append((node.start, node.end, json.dumps(nv, ensure_ascii=False)))
        if edits:
            write_text(path, apply_edits(text, edits), bom)
            changed.append(os.path.relpath(path, ws.mirror).replace("\\", "/"))
    return {"verb": "rename", "kind": kind, "table": home, "from": old, "to": new, "files": changed,
            "next": "pbz diff, then pbz push"}
