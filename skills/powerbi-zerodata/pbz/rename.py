"""Structured rename of a measure or column across the mirror's TMDL and PBIR files."""
from __future__ import annotations

import json
import os
import re

from . import lexers, pbir, tmdl
from .common import PbzError, commit_bytes, read_text
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
    elif kind == "table":
        return rename_table(ws, project, model, old, new)
    else:
        raise PbzError("UNSUPPORTED", "rename supports measure, column and table")
    ext = _external_users(ws, project, kind, home, old)

    changed = []
    ops = []
    # ---- TMDL (planned; nothing is written until PBIR and cultures are planned too)
    for rel, doc in model.docs.items():
        text = doc.text
        edits = []
        pkind = {"measure": "perspectiveMeasure", "column": "perspectiveColumn"}[kind]
        for node in doc.nodes:
            if node.kind == kind and node.name.lower() == old.lower() and node.parent and node.parent.name == home:
                e = _decl_edit(text, node, kind, new)
                if e:
                    edits.append(e)
            elif node.kind == pkind and node.name.lower() == old.lower() and node.parent is not None \
                    and node.parent.name.lower() == home.lower():  # perspective membership follows the rename
                e = _decl_edit(text, node, pkind, new)
                if e:
                    edits.append(e)
        for reg in doc.regions:
            if reg.kind != "dax":
                continue
            n = reg.node
            while n is not None and n.kind != "table":
                n = n.parent
            edits += dax_ref_edits(text, reg.start, reg.end, kind, home, old, new, n.name if n else None)
        if kind == "column":
            for m_ in re.finditer(r"(?m)^(\t*(?:sortByColumn|column)\s*:\s*)('(?:[^']|'')*'|\S+)\s*$", text):
                if tmdl.unquote(m_.group(2)).lower() == old.lower() and rel == model.tables[home].file:
                    edits.append((m_.start(2), m_.end(2), lexers.tmdl_name(new)))
            for m_ in re.finditer(r"(?m)^(\t*(?:fromColumn|toColumn)\s*:\s*)(.+?)\s*$", text):
                parts = tmdl.split_dotted(m_.group(2))
                if len(parts) == 2 and parts[0].lower() == home.lower() and parts[1].lower() == old.lower():
                    edits.append((m_.start(2), m_.end(2), f"{lexers.tmdl_name(parts[0])}.{lexers.tmdl_name(new)}"))
        if edits:
            path = os.path.join(mdef, *rel.split("/"))
            _t, bom = read_text(path)
            ops.append(("write", path, apply_edits(text, edits), bom))
            changed.append(f"{project.name}.SemanticModel/definition/{rel}")
    from .move import plan_pbir
    for path, text, bom, rel in plan_pbir(
            ws, project.mirror_report,
            lambda root, _text: (pbir_ref_edits(root, model, kind, home, old, new), False)):
        ops.append(("write", path, text, bom))
        changed.append(rel)
    from .governance import culture_rename_texts
    for rel, text in culture_rename_texts(mdef, kind, home, old, new).items():
        path = os.path.join(mdef, *rel.split("/"))
        _t, bom = read_text(path)
        ops.append(("write", path, text, bom))
        changed.append(f"{project.name}.SemanticModel/definition/{rel}")
    commit_bytes(ops)
    return {"verb": "rename", "kind": kind, "table": home, "from": old, "to": new, "files": changed,
            "next": "pbz diff, then pbz push", **ext}


def dax_ref_edits(text: str, start: int, end: int, kind: str, home: str, old: str, new: str,
                  cur: str | None = None) -> list[tuple[int, int, str]]:
    """Edits renaming every reference to measure / column `old` of table `home` inside text[start:end] (one
    DAX expression) to `new`: 'home'[old] and home[old], and bare [old] (for a column only when `cur`, the
    table that holds the expression, is `home`). Offsets are in `text`."""
    edits = []
    sig = lexers.significant(lexers.lex_dax(text[start:end], start))
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
    return edits


def pbir_ref_edits(root, model, kind: str, home: str, old: str, new: str) -> list[tuple[int, int, str]]:
    """Edits renaming measure / column `old` of table `home` in one parsed PBIR document: every field
    reference's Property, and queryRef / nativeQueryRef / metadata strings."""
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
    return edits


def _external_users(ws, project, kind: str, table: str, name: str) -> dict:
    """Registered external consumers bind by name: a rename breaks them (warn, never block)."""
    from .consumers import users_of
    try:
        ids = users_of(ws, project, kind, table, name)
    except (PbzError, OSError, ValueError):
        return {}
    if not ids:
        return {}
    return {"warnings": [{"code": "USED_EXTERNALLY", "consumers": ids,
                          "message": "registered external consumers use this name and break after the rename: "
                                     "update them too (or keep the old name)"}]}


# ---------------------------------------------------------------- tables

IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _dax_table(new: str, quoted: bool) -> str:
    return lexers.dax_table_name(new) if quoted or not IDENT_RE.fullmatch(new) else new


def _m_ident(new: str) -> str:
    return new if IDENT_RE.fullmatch(new) else '#"' + new.replace('"', '""') + '"'


def translated_names(project) -> set[str]:
    """Lower-case names of objects with translations in real culture files the mirror lacks."""
    from .unused import translated
    keys = translated(project)
    return {k[2] for k in keys} | {k[1] for k in keys if k[1]}


def rename_table(ws, project, model, old: str, new: str) -> dict:
    t = model.table(old)
    if t is None:
        raise PbzError("NOT_FOUND", f"table {old}")
    old = t.name
    if model.table(new) and new.lower() != old.lower():
        raise PbzError("NAME_TAKEN", new)
    if not new.strip() or new != new.strip() or any(c in new for c in "\r\n\t"):
        raise PbzError("BAD_REQUEST", "table names cannot be blank, padded or contain control characters")
    if any(n == old.lower() for n in translated_names(project)):
        raise PbzError("UNSUPPORTED", "cultures not in this mirror: run pbz pull")
    if any(e.lower() == old.lower() for e in model.expressions):
        raise PbzError("AMBIGUOUS", f"a shared query is also named {old}")
    ext = _external_users(ws, project, "table", "", old)
    mdef = os.path.join(project.mirror_model, "definition")
    _refuse_file_collision(mdef, t.file, new)
    low = old.lower()
    changed, renamed_file = [], None
    ops = []
    for rel, doc in model.docs.items():
        text = doc.text
        edits = []
        for node in doc.nodes:
            if node.kind == "table" and node.parent is None and node.name.lower() == low:
                e = _decl_edit(text, node, "table", new)
                if e:
                    edits.append(e)
            elif node.kind == "partition" and node.parent is not None and node.parent.name.lower() == low \
                    and node.name.lower() == low:
                e = _decl_edit(text, node, "partition", new)
                if e:
                    edits.append(e)
            elif node.kind in ("perspectiveTable", "tablePermission") and node.name.lower() == low:
                e = _decl_edit(text, node, node.kind, new)
                if e:
                    edits.append(e)
        for reg in doc.regions:
            seg = text[reg.start:reg.end]
            if reg.kind == "dax":
                sig = lexers.significant(lexers.lex_dax(seg, reg.start))
                for i, tok in enumerate(sig):
                    nxt = sig[i + 1] if i + 1 < len(sig) else None
                    if tok.kind == "table" and tok.value.lower() == low:
                        edits.append((tok.start, tok.end, _dax_table(new, True)))
                    elif tok.kind == "ident" and tok.text.lower() == low and not (nxt is not None and nxt.text == "("):
                        edits.append((tok.start, tok.end, _dax_table(new, False)))
            elif reg.kind == "m":
                sig = lexers.significant(lexers.lex_m(seg, reg.start))
                steps = {(x.value or x.text) for k, x in enumerate(sig[:-1])
                         if x.kind in ("ident", "qident") and sig[k + 1].text == "="}
                for i, tok in enumerate(sig):
                    name = tok.value if tok.kind == "qident" else (tok.text if tok.kind == "ident" else None)
                    prev = sig[i - 1] if i else None
                    if name == old and name not in steps and not (prev is not None and prev.text == "." and prev.end == tok.start):
                        edits.append((tok.start, tok.end, _m_ident(new)))
            elif reg.kind == "text" and reg.node.kind == "annotation" and reg.node.name == "PBI_QueryOrder":
                try:
                    names = json.loads(seg)
                except ValueError:
                    names = None
                if isinstance(names, list) and old in names:
                    edits.append((reg.start, reg.end, json.dumps([new if n == old else n for n in names],
                                                                 separators=(",", ":"), ensure_ascii=False)))
        for m_ in re.finditer(r"(?m)^(\t*(?:fromColumn|toColumn|baseColumn|baseTable|defaultHierarchy)\s*:\s*)(.+?)\s*$", text):
            parts = tmdl.split_dotted(m_.group(2))
            if parts and parts[0].lower() == low:
                edits.append((m_.start(2), m_.end(2), ".".join([lexers.tmdl_name(new)] + [lexers.tmdl_name(p) for p in parts[1:]])))
        if rel == "model.tmdl":
            for m_ in re.finditer(r"(?m)^ref table ('(?:[^']|'')*'|\S+)[ \t]*$", text):
                if tmdl.unquote(m_.group(1)).lower() == low:
                    edits.append((m_.start(1), m_.end(1), lexers.tmdl_name(new)))
        if not edits and rel != t.file:
            continue
        new_text = apply_edits(text, edits) if edits else text
        path = os.path.join(mdef, *rel.split("/"))
        _t, bom = read_text(path)
        if rel == t.file and os.path.basename(rel)[:-5].lower() == low:
            target = "/".join(rel.split("/")[:-1] + [_safe_file(new) + ".tmdl"])
            if target != rel:
                ops.append(("write", os.path.join(mdef, *target.split("/")), new_text, bom))
                ops.append(("delete", path))
                renamed_file = (rel, target)
                changed.append(f"{project.name}.SemanticModel/definition/{target}")
                continue
        if edits:
            ops.append(("write", path, new_text, bom))
            changed.append(f"{project.name}.SemanticModel/definition/{rel}")

    def _table_pbir(root, _text):
        edits = []

        def visit(node, key=""):
            if isinstance(node, pbir.JObj):
                ents = node.get("entities") if key == "" else None
                for k, v in node.pairs:
                    if k.value == "Entity" and isinstance(v, pbir.JStr) and v.value.lower() == low:
                        edits.append((v.start, v.end, json.dumps(new, ensure_ascii=False)))
                    visit(v, k.value)
                if isinstance(ents, pbir.JArr):
                    for ent in ents.items:
                        nm = ent.get("name") if isinstance(ent, pbir.JObj) else None
                        if isinstance(nm, pbir.JStr) and nm.value.lower() == low:
                            edits.append((nm.start, nm.end, json.dumps(new, ensure_ascii=False)))
            elif isinstance(node, pbir.JArr):
                for v in node.items:
                    visit(v, key)

        visit(root)
        for key, node in pbir.string_nodes(root):
            if key in ("queryRef", "metadata", "queryName") and node.value.lower().startswith(low + "."):
                edits.append((node.start, node.end, json.dumps(new + node.value[len(old):], ensure_ascii=False)))
        return edits, False

    from .move import plan_pbir
    for path, text, bom, rel in plan_pbir(ws, project.mirror_report, _table_pbir):
        ops.append(("write", path, text, bom))
        changed.append(rel)
    from .governance import culture_rename_texts
    for rel, text in culture_rename_texts(mdef, "table", "", old, new).items():
        path = os.path.join(mdef, *rel.split("/"))
        _t, bom = read_text(path)
        ops.append(("write", path, text, bom))
        changed.append(f"{project.name}.SemanticModel/definition/{rel}")
    commit_bytes(ops)
    out = {"verb": "rename", "kind": "table", "from": old, "to": new, "files": sorted(set(changed)),
           "next": "pbz diff, pbz validate, then pbz push", **ext}
    if renamed_file:
        out["file_renamed"] = {"from": renamed_file[0], "to": renamed_file[1]}
    return out


def _safe_file(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def _refuse_file_collision(mdef: str, table_file: str, new: str) -> None:
    """Refuse a new table name whose sanitized file name is already another table file."""
    src = os.path.join(mdef, *table_file.split("/"))
    dest = os.path.join(mdef, *table_file.split("/")[:-1], _safe_file(new) + ".tmdl")
    if os.path.normcase(os.path.abspath(dest)) == os.path.normcase(os.path.abspath(src)):
        return
    if os.path.lexists(dest):
        raise PbzError("NAME_TAKEN", new)
