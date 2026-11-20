"""Structured rename of a measure or column across the mirror's TMDL and PBIR files."""
from __future__ import annotations

import json
import ntpath
import os
import re

from . import deps, lexers, mirror, pbir, tmdl
from .common import PbzError, commit_bytes
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
    model = tmdl.load_model(mdef, root=os.path.dirname(project.mirror_model))
    if kind == "measure":
        m = model.measure(old)
        if not m:
            raise PbzError("NOT_FOUND", f"measure {old}")
        if (model.measure(new) or model.column(m.table, new)) and new.lower() != old.lower():
            raise PbzError("NAME_TAKEN", new)
        if any(c.name.lower() == old.lower() for c in model.all_columns()):
            raise PbzError("AMBIGUOUS", f"a column is also named {old}; rename it in Desktop")
        home = m.table
    elif kind == "column":
        hits = [c for c in model.all_columns() if c.name.lower() == old.lower() and (not table or c.table.lower() == table.lower())]
        if len(hits) != 1:
            raise PbzError("AMBIGUOUS" if hits else "NOT_FOUND", f"column {old}; pass --table")
        home = hits[0].table
        if (model.column(home, new) or model.measure(new)) and new.lower() != old.lower():
            raise PbzError("NAME_TAKEN", new)
    elif kind == "table":
        return rename_table(ws, project, model, old, new)
    else:
        raise PbzError("UNSUPPORTED", "rename supports measure, column and table")
    ext = _external_users(ws, project, kind, home, old)

    res = deps.Resolver(model)
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
            elif node.kind in ((pkind, "columnPermission") if kind == "column" else (pkind,)) and node.name.lower() == old.lower() and node.parent is not None \
                    and node.parent.name.lower() == home.lower() and node.parent.kind in ("perspectiveTable", "tablePermission"):
                # Perspective membership and object-level security follow the rename.
                e = _decl_edit(text, node, node.kind, new)
                if e:
                    edits.append(e)
        for reg in doc.regions:
            if reg.kind != "dax":
                continue
            bindings = deps.dax_region(text, reg, res).bindings
            edits += _binding_edits(bindings, kind, home, old, new, text)
        if kind == "column":
            for m_ in re.finditer(r"(?m)^(\t*(?:sortByColumn|column)\s*:\s*)('(?:[^']|'')*'|\S+)\s*$", text):
                if tmdl.unquote(m_.group(2)).lower() == old.lower() and rel == model.tables[home].file:
                    edits.append((m_.start(2), m_.end(2), lexers.tmdl_name(new)))
            for m_ in re.finditer(r"(?m)^(\t*(?:fromColumn|toColumn|baseColumn)\s*:\s*)(.+?)\s*$", text):
                parts = tmdl.split_dotted(m_.group(2))
                if len(parts) == 2 and parts[0].lower() == home.lower() and parts[1].lower() == old.lower():
                    edits.append((m_.start(2), m_.end(2), f"{lexers.tmdl_name(parts[0])}.{lexers.tmdl_name(new)}"))
        if edits:
            path = os.path.join(mdef, *rel.split("/"))
            _t, bom = mirror.read_mirror_text(path, root=ws.mirror)
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
        _t, bom = mirror.read_mirror_text(path, root=ws.mirror)
        ops.append(("write", path, text, bom))
        changed.append(f"{project.name}.SemanticModel/definition/{rel}")
    _verify_plan(ws, project, model, mdef, ops, kind, home, old, new)
    commit_bytes(ops, root=ws.mirror)
    return {"verb": "rename", "kind": kind, "table": home, "from": old, "to": new, "files": changed,
            "next": "pbz diff, then pbz push", **ext}


def _inventory(model):
    """Stable binding sites, including names that no longer resolve after an edit.

    Node/region ordinals survive a name-only edit; retaining unresolved sites is
    essential: a stale name must not disappear from the post-edit check.
    """
    out = {}
    res = deps.Resolver(model)
    for rel, doc in model.docs.items():
        for i, node in enumerate(doc.nodes):
            parent = node.parent
            if node.kind in ("column", "measure", "perspectiveColumn", "perspectiveMeasure", "columnPermission") and parent:
                kind = "measure" if node.kind in ("measure", "perspectiveMeasure") else "column"
                out[rel, "node", i] = (kind, parent.name, node.name)
            elif node.kind in ("table", "perspectiveTable", "tablePermission"):
                out[rel, "node", i] = ("table", "", node.name)
        for i, reg in enumerate(doc.regions):
            if reg.kind == "m":
                mres = deps.Resolver(model)
                mres.expressions.update({name: name for name in model.tables})
                for j, binding in enumerate(deps.m_bindings(doc.text[reg.start:reg.end], mres)):
                    out[rel, "m", i, j] = ("table", "", binding.ref.name)
            if reg.kind == "text" and reg.node.kind == "annotation" and reg.node.name == "PBI_QueryOrder":
                try:
                    names = json.loads(doc.text[reg.start:reg.end])
                except ValueError:
                    names = None
                if isinstance(names, list):
                    for j, name in enumerate(names):
                        out[rel, "query-order", i, j] = ("table", "", name)
            if reg.kind != "dax":
                continue
            for j, binding in enumerate(deps.dax_region(doc.text, reg, res).bindings):
                ref = binding.ref
                out[rel, "dax", i, j] = (ref.kind, "" if ref.kind == "table" else ref.table, ref.name)
                if binding.qualifier:
                    a, b = binding.qualifier
                    tok = lexers.significant(lexers.lex_dax(doc.text[a:b]))[0]
                    out[rel, "qualifier", i, j] = ("table", "", tok.value or tok.text)
        for i, line in enumerate(doc.text.splitlines()):
            ref = re.match(r"^ref table (.+?)\s*$", line)
            if ref:
                out[rel, "ref", i] = ("table", "", tmdl.unquote(ref[1]))
            prop = re.match(r"^\t*(fromColumn|toColumn|baseColumn|baseTable|defaultHierarchy|sortByColumn|column)\s*:\s*(.+?)\s*$", line)
            if not prop:
                continue
            parts = tmdl.split_dotted(prop[2])
            if prop[1] in ("sortByColumn", "column"):
                tables = [t.name for t in model.tables.values() if t.file == rel]
                if len(tables) == 1:
                    out[rel, "prop", i] = ("column", tables[0], tmdl.unquote(prop[2]))
            elif prop[1] in ("baseTable", "defaultHierarchy") and parts:
                out[rel, "prop-table", i] = ("table", "", parts[0])
            elif len(parts) == 2:
                out[rel, "prop", i] = ("column", parts[0], parts[1])
                out[rel, "prop-table", i] = ("table", "", parts[0])
    return out


def _verify_plan(ws, project, model, mdef, ops, kind, home, old, new):
    before = _inventory(model)
    texts = {rel: doc.text for rel, doc in model.docs.items()}
    moved = {}
    for op in ops:
        rel = os.path.relpath(op[1], mdef).replace("\\", "/")
        if rel.startswith("../"):
            continue
        if op[0] == "delete":
            texts.pop(rel, None)
            if kind == "table":
                moved[rel] = "/".join(rel.split("/")[:-1] + [_safe_file(new) + ".tmdl"])
        else:
            texts[rel] = op[2]
    planned = tmdl.load_model(mdef, texts=texts)
    after = _inventory(planned)
    for site, ref in before.items():
        k, table, name = ref
        if k != kind or name.lower() != old.lower() or (kind != "table" and table.lower() != home.lower()):
            continue
        if kind == "measure" and len(site) == 4 and site[1] == "dax":
            qualifier = before.get((site[0], "qualifier", *site[2:]))
            if qualifier and qualifier[2].lower() != home.lower():
                continue
        dest = (moved.get(site[0], site[0]), *site[1:])
        expected = (kind, table, new)
        if after.get(dest) != expected:
            raise PbzError("REFUSED", "a binding did not follow the rename; nothing renamed")
        if kind == "column" and planned.column(home, new) is None:
            raise PbzError("REFUSED", "renamed binding has no target; nothing renamed")
        if kind == "measure" and planned.measure(new) is None:
            raise PbzError("REFUSED", "renamed binding has no target; nothing renamed")
        if kind == "table" and planned.table(new) is None:
            raise PbzError("REFUSED", "renamed binding has no target; nothing renamed")

    # Inventory report field sites before editing too, even when a missed rewrite
    # produced no write operation for the containing file.
    writes = {op[1]: op[2] for op in ops if op[0] == "write"}
    for path in pbir.report_files(project.mirror_report):
        text, _bom = mirror.read_mirror_text(path, root=ws.mirror)
        try:
            original_root = pbir.parse_json(text)
            following_root = pbir.parse_json(writes.get(path, text))
        except PbzError:
            raise
        except Exception as exc:
            raise PbzError("REFUSED", "cannot parse report while verifying rename; nothing renamed") from exc
        original = pbir.walk_fields_with_path(original_root, model)
        following = dict((site, ref) for ref, site in pbir.walk_fields_with_path(following_root, planned))
        after_strings = dict(_report_strings(following_root))
        for site, value in _report_strings(original_root):
            expected = _report_string_rename(site[-1], value, kind, home, old, new)
            if after_strings.get(site) != expected:
                raise PbzError("REFUSED", "a report string did not follow the rename; nothing renamed")
        for ref, site in original:
            k = "column" if ref.kind == "aggregation" else ref.kind
            target = (ref.table.lower() == old.lower()) if kind == "table" else (
                k == kind and ref.table.lower() == home.lower() and ref.name.lower() == old.lower())
            if not target:
                continue
            after_ref = following.get(site)
            table_name = new if kind == "table" else ref.table
            object_name = ref.name if kind == "table" else new
            if (after_ref is None or ("column" if after_ref.kind == "aggregation" else after_ref.kind) != k
                    or after_ref.table != table_name or after_ref.name != object_name):
                raise PbzError("REFUSED", "a report binding did not follow the rename; nothing renamed")


def _report_strings(node, path=()):
    """Standalone query references retain their full, index-bearing JSON paths."""
    if isinstance(node, pbir.JObj):
        for key, value in node.pairs:
            here = path + (key.value,)
            if key.value in ("queryRef", "nativeQueryRef", "metadata", "queryName") and isinstance(value, pbir.JStr):
                yield here, value.value
            yield from _report_strings(value, here)
    elif isinstance(node, pbir.JArr):
        for i, value in enumerate(node.items):
            yield from _report_strings(value, path + (i,))


def _report_string_rename(key, value, kind, home, old, new):
    if kind == "table":
        if key in ("queryRef", "metadata", "queryName") and value.lower().startswith(old.lower() + "."):
            return new + value[len(old):]
        return value
    if key == "queryName":
        return value
    rewritten = re.sub(rf"(?i)(?<![\w]){re.escape(home)}\.{re.escape(old)}(?![\w])", f"{home}.{new}", value)
    if key == "nativeQueryRef" and value.lower() == old.lower():
        return new
    return rewritten


def _binding_edits(bindings, kind, home, old, new, text):
    edits = []
    for binding in bindings:
        ref = binding.ref
        if ref.name.lower() != old.lower():
            continue
        if binding.uncertain:
            raise PbzError("AMBIGUOUS", f"cannot safely bind a reference to {old}; nothing renamed")
        if kind == "measure" and binding.qualifier is not None:
            a, b = binding.qualifier
            tok = lexers.significant(lexers.lex_dax(text[a:b]))[0]
            if (tok.value or tok.text).lower() != home.lower():
                continue
        if ref.kind == kind and ref.table.lower() == home.lower():
            edits.append((binding.start, binding.end, _bracket(new)))
    return edits


def dax_ref_edits(text: str, start: int, end: int, kind: str, home: str, old: str, new: str,
                  cur: str | None = None, *, res, row_home: bool = False) -> list[tuple[int, int, str]]:
    """Rename exactly the occurrences bound to the target by the shared resolver."""
    bindings = deps.dax_bindings(text[start:end], cur or "", res, row_home, base=start).bindings
    return _binding_edits(bindings, kind, home, old, new, text)


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
            nv = _report_string_rename(key, v, kind, home, old, new)
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
    src = os.path.join(mdef, *t.file.split("/"))
    dest = os.path.join(mdef, *t.file.split("/")[:-1], _safe_file(new) + ".tmdl")
    # Apply Windows path identity on every OS, including sanitized name collisions.
    if old != new and ntpath.normcase(os.path.abspath(dest)) == ntpath.normcase(os.path.abspath(src)):
        raise PbzError("UNSUPPORTED", "table rename requires a distinct sanitized file path")
    _refuse_file_collision(mdef, t.file, new)
    res = deps.Resolver(model)
    # M partitions can reference table queries as well as shared expressions.
    mres = deps.Resolver(model)
    mres.expressions.update({name: name for name in model.tables})
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
                for binding in deps.dax_region(text, reg, res).bindings:
                    ref = binding.ref
                    if binding.uncertain and ref.table.lower() == low:
                        raise PbzError("AMBIGUOUS", f"cannot safely bind a reference to {old}")
                    if ref.kind == "table" and ref.name.lower() == low:
                        quoted = text[binding.start:binding.start + 1] == "'"
                        edits.append((binding.start, binding.end, _dax_table(new, quoted)))
                    elif binding.qualifier and ref.kind in {"column", "measure"}:
                        a, b = binding.qualifier
                        # A qualified measure can name any table; rename its actual qualifier.
                        tok = lexers.significant(lexers.lex_dax(text[a:b]))[0]
                        if (tok.value or tok.text).lower() == low:
                            edits.append((a, b, _dax_table(new, tok.kind == "table")))
            elif reg.kind == "m":
                for binding in deps.m_bindings(seg, mres, base=reg.start):
                    if binding.ref.name != old:
                        continue
                    if binding.uncertain:
                        raise PbzError("AMBIGUOUS", f"cannot safely bind an M reference to {old}")
                    if new in deps.m_defined_names(seg):
                        raise PbzError("AMBIGUOUS", f"M local {new} would capture a reference to {old}")
                    edits.append((binding.start, binding.end, _m_ident(new)))
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
            for m_ in re.finditer(r"(?m)^ref table ('(?:[^']|'')*'|[^\s]+)[ \t]*\r?$", text):
                if tmdl.unquote(m_.group(1)).lower() == low:
                    edits.append((m_.start(1), m_.end(1), lexers.tmdl_name(new)))
        if not edits and rel != t.file:
            continue
        new_text = apply_edits(text, edits) if edits else text
        path = os.path.join(mdef, *rel.split("/"))
        _t, bom = mirror.read_mirror_text(path, root=ws.mirror)
        if rel == t.file and os.path.basename(rel)[:-5].lower() == low and old != new:
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
        _t, bom = mirror.read_mirror_text(path, root=ws.mirror)
        ops.append(("write", path, text, bom))
        changed.append(f"{project.name}.SemanticModel/definition/{rel}")
    _verify_plan(ws, project, model, mdef, ops, "table", "", old, new)
    commit_bytes(ops, root=ws.mirror)
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
