"""Structured rename of a measure or column across the mirror's TMDL and PBIR files."""
from __future__ import annotations

import json
import ntpath
import os
import re
import unicodedata

from . import deps, lexers, mirror, pbir, tmdl
from .common import PbzError, commit_bytes, refuse_below
from .tokens import apply_edits


def _fold(name: str) -> str:
    """The one case-insensitive equivalence for names in this module: lower(),
    with the Greek final sigma normalized to the medial form. lower() alone
    splits a name ending in capital sigma from the same name written with a
    medial sigma before a dot, while casefold() would additionally merge
    distinct names such as Maße and Masse."""
    return name.lower().replace("ς", "σ")


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
        m = next((mm for mm in model.all_measures() if _fold(mm.name) == _fold(old)), None)
        if not m:
            raise PbzError("NOT_FOUND", f"measure {old}")
        if _fold(new) != _fold(old) and (any(_fold(mm.name) == _fold(new) for mm in model.all_measures())
                                         or any(_fold(c.name) == _fold(new)
                                                for c in model.all_columns())):
            raise PbzError("NAME_TAKEN", new)
        if any(_fold(c.name) == _fold(old) for c in model.all_columns()):
            raise PbzError("AMBIGUOUS", f"a column is also named {old}; rename it in Desktop")
        home = m.table
        old = m.name
        if _fold(new) != _fold(old) and _fold(f"{home}.{new}") in _extension_qualified(project.mirror_report, home):
            raise PbzError("NAME_TAKEN", new)
        ext_names = {_fold(mn) for _e, ms in _extension_entities(project.mirror_report) for mn in ms}
        if _fold(new) != _fold(old) and _fold(new) in ext_names:
            raise PbzError("NAME_TAKEN", new)
    elif kind == "column":
        hits = [c for c in model.all_columns() if _fold(c.name) == _fold(old) and (not table or _fold(c.table) == _fold(table))]
        if len(hits) != 1:
            raise PbzError("AMBIGUOUS" if hits else "NOT_FOUND", f"column {old}; pass --table")
        home = hits[0].table
        old = hits[0].name
        if _fold(new) != _fold(old) and (any(_fold(c.table) == _fold(home) and _fold(c.name) == _fold(new)
                                              for c in model.all_columns())
                                         or any(_fold(mm.name) == _fold(new) for mm in model.all_measures())):
            raise PbzError("NAME_TAKEN", new)
        if _fold(new) != _fold(old) and _fold(f"{home}.{new}") in _extension_qualified(project.mirror_report, home):
            raise PbzError("NAME_TAKEN", new)
        ext_names = {_fold(mn) for _e, ms in _extension_entities(project.mirror_report) for mn in ms}
        if _fold(new) != _fold(old) and _fold(new) in ext_names:
            raise PbzError("NAME_TAKEN", new)
    elif kind == "table":
        return rename_table(ws, project, model, old, new)
    else:
        raise PbzError("UNSUPPORTED", "rename supports measure, column and table")
    if _fold(new) != _fold(old) and (
            _other_table_owns(
                model, home, f"{home}.{new}",
                _extension_owned_others(project.mirror_report, home),
                _extension_owned(project.mirror_report, home),
                extra_tables=_extension_tables(project.mirror_report))
            or _other_table_owns(
                model, home, new,
                _extension_owned_others(project.mirror_report, home),
                _extension_owned(project.mirror_report, home),
                extensions_any=True,
                extra_tables=_extension_tables(project.mirror_report))):
        raise PbzError("NAME_TAKEN", f"{home}.{new}")
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
            if node.kind == kind and _fold(node.name) == _fold(old) and node.parent and node.parent.name == home:
                e = _decl_edit(text, node, kind, new)
                if e:
                    edits.append(e)
            elif node.kind in ((pkind, "columnPermission") if kind == "column" else (pkind,)) and _fold(node.name) == _fold(old) and node.parent is not None \
                    and _fold(node.parent.name) == _fold(home) and node.parent.kind in ("perspectiveTable", "tablePermission"):
                # Perspective membership and object-level security follow the rename.
                e = _decl_edit(text, node, node.kind, new)
                if e:
                    edits.append(e)
        for reg in doc.regions:
            if reg.kind != "dax":
                continue
            bindings = deps.dax_region(text, reg, res).bindings
            edits += _binding_edits(bindings, kind, home, old, new, text, model=model)
        if kind == "column":
            for m_ in re.finditer(r"(?m)^(\t*(?:sortByColumn|column)\s*:\s*)('(?:[^']|'')*'|\S+)\s*$", text):
                if _fold(tmdl.unquote(m_.group(2))) == _fold(old) and rel == model.tables[home].file:
                    edits.append((m_.start(2), m_.end(2), lexers.tmdl_name(new)))
            for m_ in re.finditer(r"(?m)^(\t*(?:fromColumn|toColumn|baseColumn)\s*:\s*)(.+?)\s*$", text):
                parts = tmdl.split_dotted(m_.group(2))
                if len(parts) == 2 and _fold(parts[0]) == _fold(home) and _fold(parts[1]) == _fold(old):
                    edits.append((m_.start(2), m_.end(2), f"{lexers.tmdl_name(parts[0])}.{lexers.tmdl_name(new)}"))
        if edits:
            path = os.path.join(mdef, *rel.split("/"))
            _t, bom = mirror.read_mirror_text(path, root=ws.mirror)
            ops.append(("write", path, apply_edits(text, edits), bom))
            changed.append(f"{project.name}.SemanticModel/definition/{rel}")
    from .move import plan_pbir
    ext_owned = _extension_owned_others(project.mirror_report, home)
    ext_ext = _extension_owned(project.mirror_report, home)
    ext_tables = _extension_tables(project.mirror_report)
    claims = _selector_claims(ws, project, model, kind, home, old)
    for path, text, bom, rel in plan_pbir(
            ws, project.mirror_report,
            lambda root, _text: (pbir_ref_edits(root, model, kind, home, old, new,
                                                extra_owned=ext_owned,
                                                extra_extending=ext_ext,
                                                extra_tables=ext_tables,
                                                selector_claims=claims,
                                                ext_bare=_extension_bare_names(project.mirror_report)), False)):
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


def _selector_claims(ws, project, model, kind, home, old):
    """Cross-file ownership claims on queryRef texts: for each folded
    queryRef string, whether the projections carrying it belong to the
    rename target ("target"), to another field or an ambiguous binding
    ("other"), or both. A formatting selector in any file links to a
    projection by this text alone, so an orphan selector may follow the
    rename only when no other field's projection claims its text."""
    claims = {}
    if kind not in ("column", "measure"):
        return claims

    def kind_of(r):
        if r.kind == "aggregation" or (r.kind == "hierarchy" and r.variation):
            return "column"
        return r.kind

    for path in pbir.report_files(project.mirror_report):
        try:
            text, _bom = mirror.read_mirror_text(path, root=ws.mirror)
            root = pbir.parse_json(text)
        except Exception:
            continue
        fields = pbir.walk_fields_with_path(root, model)
        for site, (node, field) in _report_string_sites(root, model, fields).items():
            if site[-1] != "queryRef" or field is None:
                continue
            if field is not _AMBIGUOUS and kind_of(field) == kind \
                    and _fold(field.table) == _fold(home) and _fold(field.name) == _fold(old):
                claims.setdefault(_fold(node.value), set()).add("target")
            else:
                claims.setdefault(_fold(node.value), set()).add("other")
    return claims


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
        if k != kind or _fold(name) != _fold(old) or (kind != "table" and _fold(table) != _fold(home)):
            continue
        if kind == "measure" and len(site) == 4 and site[1] == "dax":
            qualifier = before.get((site[0], "qualifier", *site[2:]))
            if qualifier and _fold(qualifier[2]) != _fold(home):
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
    home, old = _canonical_target(model, kind, home, old)
    claims = _selector_claims(ws, project, model, kind, home, old)

    def _kind_of(r):
        if r.kind == "aggregation" or (r.kind == "hierarchy" and r.variation):
            return "column"
        return r.kind

    # Independent ownership inventory: a dotted path may name several objects.
    paths, bare_names = {}, {}

    def add_owner(path, owner, bare=None):
        paths.setdefault(_fold(path), set()).add(owner)
        if bare is not None:
            bare_names.setdefault(_fold(bare), set()).add(owner)

    for table in model.tables.values():
        for k, pool in (("column", table.columns), ("measure", table.measures)):
            for obj in pool.values():
                add_owner(f"{table.name}.{obj.name}", (k, _fold(table.name), _fold(obj.name)), obj.name)
        for hierarchy, levels in table.hierarchies.items():
            for level in levels:
                add_owner(f"{table.name}.{hierarchy}.{level}",
                          ("hierarchy", _fold(table.name), _fold(hierarchy), _fold(level)))
    entities = _extension_entities(project.mirror_report)
    for entity, measures in entities:
        for measure in measures:
            add_owner(f"{entity}.{measure}", ("extension", _fold(entity), _fold(measure)), measure)
    table_names = [t.name for t in model.tables.values()] + [e for e, _ms in entities]
    anchor = old if kind == "table" else home
    base_old, base_new = f"{home}.{old}", f"{home}.{new}"

    def target_owner(owner):
        return owner[1] == _fold(old) if kind == "table" else owner == (kind, _fold(home), _fold(old))

    def competing(text, *, bare=False, unanchored=False):
        owners = set(paths.get(_fold(text), ()))
        for path, tags in paths.items():
            rest = _rest_after(text, path)
            if rest is not None and _path_extension(rest, anchor, table_names):
                owners.update(o for o in tags if o[1] != _fold(anchor)
                              and (bare or o[1].startswith(_fold(anchor) + ".")))
        if unanchored:
            owners.update(bare_names.get(_fold(text), ()))
        return any(not target_owner(o) for o in owners)

    def head_after(text, table):
        for i, ch in enumerate(text):
            if ch == "." and _fold(text[:i]) == _fold(table):
                return new + text[i:]
        return text

    def path_form(text, base, replacement, extensions=False):
        if _fold(text) == _fold(base):
            return replacement
        rest = _rest_after(text, _fold(base))
        if extensions and rest is not None and _path_extension(rest, anchor, table_names):
            return replacement + "." + rest
        return None

    global_owned_by_text = {}
    for path in pbir.report_files(project.mirror_report):
        try:
            scan_text, _sbom = mirror.read_mirror_text(path, root=ws.mirror)
            scan_root = pbir.parse_json(scan_text)
        except Exception:
            continue
        scan_fields = pbir.walk_fields_with_path(scan_root, model)
        for site, (node, field) in _report_string_sites(scan_root, model, scan_fields).items():
            if site[-1] == "queryRef" and field is not None and field is not _AMBIGUOUS:
                global_owned_by_text.setdefault(_fold(node.value), set()).add(
                    (field.kind, _fold(field.table), _fold(field.name), _fold(field.hierarchy),
                     _fold(field.level), field.function, field.variation, _fold(node.value)))
    global_finals = {}
    global_originals = {}
    global_selector_finals = []
    global_link_finals = []
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
        orig_sites = _report_string_sites(original_root, model, original)
        anchored = _doc_anchored(original_root, kind, home, old)

        def expectation(key, value, field):
            if (key == "queryName" and kind != "table") or field is _AMBIGUOUS:
                return value
            if field is not None:
                target = (_fold(field.table) == _fold(old)) if kind == "table" else (
                    _kind_of(field) == kind and _fold(field.table) == _fold(home)
                    and _fold(field.name) == _fold(old))
                if not target:
                    return value
                base = f"{field.table}.{field.name}"
                if kind == "table":
                    if field.kind == "hierarchy" and field.hierarchy and not field.variation:
                        base = f"{field.table}.{field.hierarchy}.{field.level}"
                    wrapped = _wrapped_inner(value)
                    for text, wrapper in [(value, None)] + ([(wrapped[1], wrapped[0])] if wrapped else []):
                        if path_form(text, base, base, True) is not None:
                            out = head_after(text, field.table)
                            return f"{wrapper}({out})" if wrapper else out
                    return value
                if key == "nativeQueryRef" and _fold(value) == _fold(field.name):
                    return new
                wrapped = _wrapped_call(value) if kind == "column" else None
                for text, wrapper in [(value, None)] + ([(wrapped[1], wrapped[0])] if wrapped else []):
                    out = path_form(text, base, base_new, bool(field.variation))
                    if out is None and _fold(text) == _fold(base_old):
                        out = base_new
                    if out is not None:
                        return f"{wrapper}({out})" if wrapper else out
                return value

            # Literal ownership takes precedence over a possible wrapped reading.
            bare = key == "nativeQueryRef"
            literal_bare = bare and (kind == "table" or _fold(value) == _fold(old))
            if competing(value, bare=literal_bare,
                         unanchored=bare and (kind == "table" or not anchored)):
                return value
            if kind == "table":
                def table_form(text):
                    if competing(text):
                        return None
                    if any(any(target_owner(o) for o in tags)
                           and path_form(text, base, base, True) is not None
                           for base, tags in paths.items()):
                        return head_after(text, old)
                    return None
                out = table_form(value)
                if out is not None:
                    return out
                wrapped = _wrapped_inner(value)
                if wrapped:
                    out = table_form(wrapped[1])
                    if out is not None:
                        return f"{wrapped[0]}({out})"
            else:
                if _fold(value) == _fold(base_old):
                    return base_new
                if bare and _fold(value) == _fold(old):
                    return new
                wrapped = _wrapped_call(value) if kind == "column" else None
                if wrapped and _fold(wrapped[1]) == _fold(base_old) and not competing(wrapped[1]):
                    return f"{wrapped[0]}({base_new})"
                if key == "queryRef" and not _literal_other_owner(model, value, home, old):
                    out = path_form(value, base_old, base_new, True)
                    if out is not None:
                        return out
            return value

        expected_strings, links, projection_owners = {}, {}, {}
        for site, (node, field) in orig_sites.items():
            expected = expectation(site[-1], node.value, field)
            expected_strings[site] = expected
            if site[-1] == "queryRef" and field is not None and field is not _AMBIGUOUS:
                projection_owners.setdefault(_fold(node.value), set()).add((
                    field.kind, _fold(field.table), _fold(field.name), _fold(field.hierarchy),
                    _fold(field.level), field.function, field.variation))
            if site[-1] == "queryRef":
                links.setdefault(node.value, set()).add(expected)
            if kind == "column" and _fold(new) != _fold(old) and field is None \
                    and site[-1] == "nativeQueryRef" and _fold(node.value) == _fold(old) \
                    and expected != node.value and competing(expected, bare=True, unanchored=True):
                raise PbzError("REFUSED", "a report string would lose its owner; nothing renamed")
            # An unchanged bare destination can lose its unique original owner.
            if kind == "column" and _fold(new) != _fold(old) and field is None \
                    and site[-1] == "nativeQueryRef" and _fold(node.value) == _fold(new) \
                    and len(bare_names.get(_fold(new), ())) == 1 \
                    and not target_owner(next(iter(bare_names[_fold(new)]))):
                raise PbzError("NAME_TAKEN", "a bare report string would lose its owner; nothing renamed")
            # An unchanged bare string can also be captured by the new
            # qualified path: it uniquely named another field (here a
            # column whose bare name contains a dot), and after the
            # rename the same spelling is the target's qualified path.
            if kind == "column" and _fold(new) != _fold(old) and field is None \
                    and site[-1] == "nativeQueryRef" and _fold(node.value) == _fold(base_new) \
                    and expected == node.value:
                owners = bare_names.get(_fold(node.value), set()) | paths.get(_fold(node.value), set())
                if len(owners) == 1 and not target_owner(next(iter(owners))):
                    raise PbzError("NAME_TAKEN", "a bare report string would lose its owner; nothing renamed")
        # A rewritten queryRef must not land on the expected text of a
        # queryRef that answers to a different owner, or selectors naming
        # that string would no longer identify one projection.
        owned_by_text = {}
        for site, (node, field) in orig_sites.items():
            if site[-1] == "queryRef" and field is not None and field is not _AMBIGUOUS:
                owned_by_text.setdefault(_fold(node.value), set()).add(
                    (field.kind, _fold(field.table), _fold(field.name), _fold(field.hierarchy),
                     _fold(field.level), field.function, field.variation, _fold(node.value)))
        finals_by_text = {}
        for site, (node, field) in orig_sites.items():
            if site[-1] != "queryRef":
                continue
            if field is not None and field is not _AMBIGUOUS:
                identity = (field.kind, _fold(field.table), _fold(field.name), _fold(field.hierarchy),
                            _fold(field.level), field.function, field.variation, _fold(node.value))
            elif field is _AMBIGUOUS:
                identity = ("ambiguous", site)
            else:
                owners = owned_by_text.get(_fold(node.value), set())
                identity = next(iter(owners)) if len(owners) == 1 else ("unowned", _fold(node.value))
            finals_by_text.setdefault(_fold(expected_strings[site]), []).append(
                (identity, expected_strings[site] != node.value))
        for group in finals_by_text.values():
            if any(changed for _identity, changed in group) and len({i for i, _c in group}) > 1:
                raise PbzError("REFUSED", "a report string would become ambiguous; nothing renamed")
        for site, (node, field) in orig_sites.items():
            if site[-1] != "queryRef":
                continue
            if field is not None and field is not _AMBIGUOUS:
                gidentity = (field.kind, _fold(field.table), _fold(field.name), _fold(field.hierarchy),
                             _fold(field.level), field.function, field.variation, _fold(node.value))
            elif field is _AMBIGUOUS:
                gidentity = ("ambiguous", _fold(node.value))
            else:
                owners = global_owned_by_text.get(_fold(node.value), set())
                gidentity = next(iter(owners)) if len(owners) == 1 else ("unowned", _fold(node.value))
            global_finals.setdefault(_fold(expected_strings[site]), []).append(
                (gidentity, expected_strings[site] != node.value))
            global_originals.setdefault(_fold(node.value), set()).add(_fold(expected_strings[site]))
        for site, (node, field) in orig_sites.items():
            expected = expected_strings[site]
            if kind != "table" and site[-2:] == ("selector", "metadata") and field is None:
                claim = claims.get(_fold(node.value), set())
                if "other" in claim:
                    expected = node.value
                candidates = set(links.get(node.value, ()))
                if expected != node.value:
                    candidates.add(expected)
                if "other" not in claim and expected == node.value \
                        and not any(v != node.value for v in candidates) \
                        and not competing(node.value) \
                        and not _literal_other_owner(model, node.value, home, old):
                    followed = path_form(node.value, base_old, base_new, True)
                    if followed is not None and followed != node.value:
                        if competing(followed):
                            raise PbzError("REFUSED", "a report string selector cannot be resolved uniquely; nothing renamed")
                        expected = followed
                        candidates.add(expected)
                if "target" in claim and "other" in claim \
                        and (expected != node.value or any(v != node.value for v in candidates)):
                    raise PbzError("REFUSED", "a report string selector cannot be resolved uniquely; nothing renamed")
                if "other" not in claim and any(v != node.value for v in candidates):
                    if len(candidates) != 1 or len(projection_owners.get(_fold(node.value), ())) > 1:
                        raise PbzError("REFUSED", "a report string selector cannot be resolved uniquely; nothing renamed")
                    expected = next(iter(candidates))
                    carrier_ids = {_raw_identity(f) for s2, (n2, f) in orig_sites.items()
                                   if s2[-1] == "queryRef" and f is not None and f is not _AMBIGUOUS
                                   and _fold(n2.value) == _fold(node.value)}
                    if any(other.value != node.value and expected_strings[s] == expected
                           and not (_fold(other.value) == _fold(node.value) and expected_strings[s] != other.value)
                           and not (_owner is not None and _owner is not _AMBIGUOUS
                                    and _raw_identity(_owner) in carrier_ids)
                           for s, (other, _owner) in orig_sites.items() if s[-1] == "queryRef"):
                        raise PbzError("REFUSED", "a report string selector cannot be resolved uniquely; nothing renamed")
            if site[-2:] == ("selector", "metadata"):
                global_selector_finals.append((_fold(node.value), _fold(expected), expected != node.value))
            if site[-1] == "queryRef" and field is None:
                global_link_finals.append((_fold(node.value), _fold(expected)))
            if after_strings.get(site) != expected:
                raise PbzError("REFUSED", "a report string did not follow the rename; nothing renamed")

        for ref, site in original:
            k = _kind_of(ref)
            target = (_fold(ref.table) == _fold(old)) if kind == "table" else (
                k == kind and _fold(ref.table) == _fold(home) and _fold(ref.name) == _fold(old))
            if not target:
                continue
            after_ref = following.get(site)
            table_name = new if kind == "table" else ref.table
            object_name = ref.name if kind == "table" else new
            if after_ref is not None and ref.kind == "hierarchy" and after_ref.kind == "hierarchy" \
                    and after_ref.table == table_name and after_ref.hierarchy == ref.hierarchy \
                    and after_ref.level == ref.level \
                    and (_fold(ref.name) == _fold(ref.hierarchy)
                         or _fold(after_ref.name) == _fold(after_ref.hierarchy)):
                # The backing column was resolved by exact table lookup on
                # one side only (a case-variant entity defeats it), so the
                # name is not comparable; the hierarchy path identity is.
                continue
            if (after_ref is None or _kind_of(after_ref) != k
                    or after_ref.table != table_name or after_ref.name != object_name):
                raise PbzError("REFUSED", "a report binding did not follow the rename; nothing renamed")

    # The per-file landing check cannot see across files: a rewritten
    # queryRef must not land on the planned text of a queryRef answering
    # to a different field in ANY report file, or a selector naming that
    # string would no longer identify one projection in the report.
    for group in global_finals.values():
        if any(changed for _identity, changed in group) and len({i for i, _c in group}) > 1:
            raise PbzError("REFUSED", "a report string would become ambiguous; nothing renamed")
    # A selector rewritten by the rename must not land on the unchanged
    # queryRef text of a projection owned by another field in any file:
    # it would end up naming that projection instead of its own.
    for _original_text, final_text, changed in global_selector_finals:
        if not changed:
            continue
        carrier_ids = {i[:7] for i in global_owned_by_text.get(_original_text, ())}
        for identity, qchanged in global_finals.get(final_text, []):
            if qchanged:
                continue
            if not (len(identity) == 8 and identity[:7] in carrier_ids):
                raise PbzError("REFUSED", "a report string would become ambiguous; nothing renamed")
    # A reference string must end at the one planned text of the
    # projections that carried its original text: if any of them kept the
    # text (a path-like queryRef the field itself does not rewrite) or
    # they landed on more than one text, the string cannot keep pointing
    # at all of them. This covers selectors and fieldless references
    # alike (for example a bookmark's explorationState queryRef); a
    # projection's own queryRef and nativeQueryRef are identifiers and
    # labels of their own field, not references, and are exempt.
    for original_text, final_text in global_link_finals:
        finals = global_originals.get(original_text, ())
        if finals and finals != {final_text}:
            raise PbzError("REFUSED", "a report string selector cannot be resolved uniquely; nothing renamed")
    for original_text, final_text, _changed in global_selector_finals:
        finals = global_originals.get(original_text, ())
        if finals and finals != {final_text}:
            raise PbzError("REFUSED", "a report string selector cannot be resolved uniquely; nothing renamed")


def _report_strings(node, path=()):
    """Report reference values retain their full, index-bearing JSON paths."""
    for site, value in _report_string_nodes(node, path):
        yield site, value.value


def _report_string_nodes(node, path=()):
    """Report reference nodes with paths for both verification and span edits."""
    if isinstance(node, pbir.JObj):
        for key, value in node.pairs:
            here = path + (key.value,)
            if key.value in ("queryRef", "nativeQueryRef", "metadata", "queryName") and isinstance(value, pbir.JStr):
                yield here, value
            yield from _report_string_nodes(value, here)
    elif isinstance(node, pbir.JArr):
        for i, value in enumerate(node.items):
            yield from _report_string_nodes(value, path + (i,))


def _report_string_sites(root, model, fields=None):
    """Map each string site to its node and longest containing resolved field."""
    if fields is None:
        fields = pbir.walk_fields_with_path(root, model)
    owners = {site: ref for ref, site in fields}
    siblings = {}
    for ref, site in fields:
        siblings.setdefault(site[:-1], {})[id(ref)] = (ref, site)

    def sibling_field(prefix, site):
        # The fields held directly by an ancestor, counting only those
        # in the string's own array elements: a field whose path
        # diverges from the string's at an array index belongs to a
        # sibling element and is never inherited. Several counting
        # fields make the container ambiguous.
        entries = siblings.get(prefix)
        if not entries:
            return None
        refs = {}
        for ref, fsite in entries.values():
            if all(not isinstance(site[i], int) or fsite[i] == site[i]
                   for i in range(min(len(fsite), len(site)))):
                refs[id(ref)] = ref
        if len(refs) == 1:
            return next(iter(refs.values()))
        return _AMBIGUOUS if refs else None

    sites = {}
    for site, node in _report_string_nodes(root):
        # Resolve ownership nearest-first across both maps: at each
        # ancestor, a field whose object contains the string (owners)
        # decides, else the fields held directly by that ancestor
        # (siblings, ambiguous for several) decide, and the search
        # stops at whichever comes first, so a nearer ambiguous
        # container fails closed before a farther unique field can
        # claim the string, and a unique nearer field is not poisoned
        # by a farther ambiguous container.
        field = None
        for length in range(len(site) - 1, -1, -1):
            field = owners.get(site[:length])
            if field is not None:
                break
            field = sibling_field(site[:length], site)
            if field is not None:
                break
        sites[site] = (node, field)
    return sites


_AMBIGUOUS = object()


def _find_table(model, name):
    """The model table whose name folds equal to name, or None. The model's
    own lookups use lower(), which splits the Greek sigma spellings."""
    if model is None:
        return None
    for t in model.tables.values():
        if _fold(t.name) == _fold(name):
            return t
    return None


def _canonical_target(model, kind, home, old):
    """The model-resolved (table, name) for a rename target, in canonical spelling."""
    if model is None:
        return home, old
    table = _find_table(model, home)
    if table is None:
        return home, old
    if kind == "table":
        return table.name, old
    pool = table.columns if kind == "column" else table.measures
    for obj in pool.values():
        if _fold(obj.name) == _fold(old):
            return table.name, obj.name
    return table.name, old


def _wrapper_head(value):
    """(wrapper, index of '(') when value starts with a Name( head, else None.

    The name scan is fold-consistent: a character continues the name
    when it is alphanumeric, an underscore, or a combining mark,
    since folding a precomposed letter can yield a combining mark
    (İ folds to i plus a combining dot), and two spellings equal
    under _fold must parse the same way."""
    if not value or not (value[0] == "_" or value[0].isalpha()):
        return None
    i = 1
    while i < len(value):
        c = value[i]
        if c == "_" or c.isalnum() or unicodedata.category(c).startswith("M"):
            i += 1
        else:
            break
    if i < len(value) and value[i] == "(":
        return value[:i], i
    return None


def _wrapped_inner(value):
    """(wrapper, inner) when value is Name(...) with the opening
    parenthesis closing at the final character: two calls in a row
    are not one wrapper."""
    head = _wrapper_head(value)
    if not head or not value.endswith(")"):
        return None
    wrapper, start = head
    depth = 0
    for i in range(start, len(value)):
        if value[i] == "(":
            depth += 1
        elif value[i] == ")":
            depth -= 1
            if depth == 0 and i != len(value) - 1:
                return None
    if depth != 0:
        return None
    return wrapper, value[start + 1:-1]


def _wrapped_call(value):
    """(wrapper, inner) when value is one Name(inner) call with a nonempty inner."""
    got = _wrapped_inner(value)
    if not got or not got[1]:
        return None
    return got


def _table_qualified(model, old):
    """(table, {qualified object paths}) for a table rename, or (None, empty set)."""
    if model is None:
        return None, set()
    table = _find_table(model, old)
    if table is None:
        return None, set()
    qualified = {_fold(f"{table.name}.{obj.name}")
                 for obj in list(table.columns.values()) + list(table.measures.values())}
    for hname, levels in table.hierarchies.items():
        for level in levels:
            qualified.add(_fold(f"{table.name}.{hname}.{level}"))
    return table, qualified


_WORD_OPERATORS = {"and", "in", "not", "or"}


def _path_extension(rest, old, tables=()):
    """True when rest is only dotted name segments extending a qualified path
    (hierarchy levels, variations), never an expression fragment. An allowlist,
    so a new operator character cannot reopen a partial rewrite; a word
    operator or a fresh reference to any known table also disqualifies the
    remainder, since either means the string holds more than one reference."""
    if not rest:
        return False
    names = {_fold(old)} | {_fold(t) for t in tables}
    folded = _fold(rest)

    def _name_char(ch):
        return ch.isalnum() or ch == "_" or unicodedata.category(ch).startswith("M")

    for n in names:
        idx = folded.find(n + ".")
        while idx != -1:
            # A fresh reference starts only where the preceding character is
            # not a name character, under the same rule the segment check
            # below applies (combining marks are part of a name).
            if idx == 0 or not _name_char(folded[idx - 1]):
                return False
            idx = folded.find(n + ".", idx + 1)
    for seg in rest.split("."):
        # Classify the folded segment, and count combining marks as name
        # characters: spellings that fold to the same string (a precomposed
        # letter and its decomposed form) must classify identically.
        seg = _fold(seg.strip())
        if not seg or not all(ch.isalnum() or unicodedata.category(ch).startswith("M")
                              or ch in "_ " for ch in seg):
            return False
        if any(tok in _WORD_OPERATORS for tok in seg.split()):
            return False
    return True


def _rest_after(text, prefix_lower):
    """The remainder of text after a dotted prefix matched case-insensitively,
    sliced at the value's own boundary dot, or None when the prefix does not
    match. Length-based slicing is unsafe: case folding can change the code
    point count of the written segment."""
    for i, ch in enumerate(text):
        if ch == "." and _fold(text[:i]) == prefix_lower:
            return text[i + 1:]
    return None


def _prefix_swap(value, base, new_base):
    """Replace a leading dotted base path in value with new_base, slicing at
    the value's own boundary: the written base can differ in length from the
    canonical spelling when case folding changes code points."""
    for i, ch in enumerate(value):
        if ch == "." and _fold(value[:i]) == _fold(base):
            return new_base + value[i:]
    if _fold(value) == _fold(base):
        return new_base
    return new_base + value[len(base):]


def _head_swap(value, table, new):
    """Replace the leading table segment of a dotted value with new, slicing
    at the value's own boundary dot: the written segment can differ in length
    from the canonical name when case folding changes code points."""
    return _prefix_swap(value, table, new)


def _other_table_owns(model, home, value, extra_owned=(), extra_extending=(),
                      extensions_any=False, extra_tables=(), head=None, dest_tables=None,
                      bare_fields=False, ext_bare=()):
    """True when an unbound string is owned by another table: exactly a
    qualified path of any other table (Sales.Region.Amount beside the Sales
    column 'Region.Amount'), or an owned extension of a longer-named
    table's path. The string is ambiguous and the other table's reading
    wins, so it stays unchanged. extra_owned carries the report-extension
    measure paths of any other entity; extra_extending only those whose
    entity name extends home, since extension ownership is decided by the
    owning entity, not by the flattened path. extensions_any extends the
    extension test to every other table, for values with no home anchor
    (a bare nativeQueryRef)."""
    if head is None:
        head = home
    low = _fold(value)
    if dest_tables is not None:
        tables = tuple(dest_tables)
    else:
        tables = tuple(t.name for t in model.tables.values()) if model is not None else ()
    tables = tables + tuple(extra_tables)
    if low in {_fold(p) for p in extra_owned}:
        return True
    for p in extra_extending:
        pl = _fold(p)
        if low == pl:
            return True
        rest = _rest_after(value, pl)
        if rest is not None and _path_extension(rest, head, tables):
            return True
    if extensions_any:
        for p in extra_owned:
            rest = _rest_after(value, _fold(p))
            if rest is not None and _path_extension(rest, head, tables):
                return True
    if model is None:
        return False
    if bare_fields:
        # A bare value with no anchor to its home anywhere in its
        # document (a bare nativeQueryRef) is owned by any other table
        # with a field of that whole name, not only by a qualified path:
        # model fields of any other table, and report-extension measures
        # of any entity (their true names arrive in ext_bare, since the
        # flattened extra_owned paths cannot be split back reliably).
        # The value is the whole field name by construction at these
        # call sites, dots included.
        for t in model.tables.values():
            if _fold(t.name) == _fold(home):
                continue
            if any(_fold(o.name) == low
                   for o in list(t.columns.values()) + list(t.measures.values())):
                return True
        if low in ext_bare:
            return True
    for t in model.tables.values():
        if _fold(t.name) == _fold(home):
            continue
        paths = {_fold(f"{t.name}.{o.name}")
                 for o in list(t.columns.values()) + list(t.measures.values())}
        for hname, levels in t.hierarchies.items():
            paths |= {_fold(f"{t.name}.{hname}.{lvl}") for lvl in levels}
        if low in paths:
            return True
        if extensions_any or _fold(t.name).startswith(_fold(head) + "."):
            for q in paths:
                rest = _rest_after(value, q)
                if rest is not None and _path_extension(rest, head, tables):
                    return True
    return False


_BLOCKED = object()


def _table_rewrite_flagged(value, old, new, qualified, table=None, model=None, other_owned=(),
                           other_extending=(), other_tables=(), key=None, ext_bare=()):
    """Rewrite one table reference in a report string, or return the value unchanged.

    A value rewrites when it is exactly a qualified object path of the old
    table, or a longer operator-free path extending one (hierarchy levels,
    variations). A string owned by a longer-named table, exactly or by
    extension, never rewrites: the longer table's reading wins."""
    dots = table.name.count(".") if table is not None else old.count(".")

    def _swap(text):
        at = -1
        for _ in range(dots + 1):
            at = text.index(".", at + 1)
        return new + text[at:]

    _tables = [t.name for t in model.tables.values()] if model is not None else []
    _tables = _tables + list(other_tables)
    other_exact = {_fold(p) for p in other_owned}
    other_ext = {_fold(p) for p in other_extending}
    if model is not None:
        for other in model.tables.values():
            if _fold(other.name) == _fold(old):
                continue
            other_exact |= _table_qualified(model, other.name)[1]
            if _fold(other.name).startswith(_fold(old) + "."):
                other_ext |= _table_qualified(model, other.name)[1]

    def _extends(text, qset):
        for q in qset:
            rest = _rest_after(text, q)
            if rest is not None and _path_extension(rest, old, _tables):
                return True
        return False

    def _rewrite(text):
        if _fold(text) in other_exact:
            return _BLOCKED
        if _fold(text) in qualified or _extends(text, qualified):
            if _extends(text, other_ext):
                return _BLOCKED
            return _swap(text)
        return None

    if key == "nativeQueryRef" and model is not None \
            and _other_table_owns(model, old, value, other_owned, other_extending,
                                  extensions_any=True, extra_tables=other_tables,
                                  bare_fields=True, ext_bare=ext_bare):
        # The literal string names another table's field: that
        # reading is probed before every other, so neither the raw
        # extension acceptance nor the wrapper retry runs on it.
        return value, False
    out = _rewrite(value)
    if out is _BLOCKED:
        return value, False
    if out is not None:
        return out, False
    wrapped = _wrapped_inner(value)
    if wrapped:
        inner = _rewrite(wrapped[1])
        if inner is not None and inner is not _BLOCKED:
            return f"{wrapped[0]}({inner})", True
    return value, False


def _table_rewrite(value, old, new, qualified, table=None, model=None, other_owned=(),
                   other_extending=(), other_tables=()):
    return _table_rewrite_flagged(value, old, new, qualified, table, model, other_owned,
                                  other_extending, other_tables)[0]


def _single_wrapped(value):
    """(wrapper, inner) when value is exactly one Name(...) call, else None."""
    head = _wrapper_head(value)
    if not head:
        return None
    wrapper, start = head
    depth = 0
    for i in range(start, len(value)):
        if value[i] == "(":
            depth += 1
        elif value[i] == ")":
            depth -= 1
            if depth == 0:
                if i != len(value) - 1:
                    return None
                return wrapper, value[start + 1:i]
    return None


def _extension_entities(report_dir):
    """(entity name, [measure names]) pairs from reportExtensions.json."""
    path = os.path.join(report_dir, "definition", "reportExtensions.json")
    # Validate the ancestor chain against the mirror root before any
    # early return: a link on the report directory or on definition
    # refuses even when the inventory file itself is absent, so an
    # escaping path is never mistaken for a genuinely absent one.
    refuse_below(path, os.path.dirname(report_dir))
    parent = os.path.dirname(path)
    # An existing ancestor that is not a directory is a broken path,
    # never an absent inventory. Check it directly: on some platforms
    # lstat of a path through a regular file reports not-found, which
    # the absent() probe below would mistake for a missing component.
    for anc in (report_dir, parent):
        if os.path.lexists(anc) and not os.path.isdir(anc):
            raise PbzError("UNSAFE_PATH", anc)

    def absent(p):
        # Genuinely absent means the lookup fails only because a
        # component does not exist; a non-directory component or any
        # other failure is a broken path, not an absent one, and the
        # guarded read below refuses it.
        try:
            os.lstat(p)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False

    if absent(report_dir) or absent(parent):
        return []
    if absent(path) and os.path.isdir(parent):
        return []
    # The inventory is mirror content: load it through the mirror read
    # guard, so a path escaping the mirror (a symlink to an external
    # file) refuses instead of being read. Only a genuinely absent
    # file is an empty inventory: a broken path or an unparseable
    # file refuses rather than reporting that nothing is owned.
    text, _bom = mirror.read_mirror_text(path, root=os.path.dirname(report_dir))
    try:
        root = pbir.parse_json(text)
    except Exception:
        raise PbzError("REFUSED", "definition/reportExtensions.json") from None
    if not isinstance(root, pbir.JObj):
        raise PbzError("REFUSED", "definition/reportExtensions.json")
    ents = root.get("entities")
    if ents is not None and not isinstance(ents, pbir.JArr):
        # A structurally unreadable inventory (entities present but
        # not an array) refuses rather than reporting that nothing
        # is owned.
        raise PbzError("REFUSED", "definition/reportExtensions.json")
    out = []
    if isinstance(ents, pbir.JArr):
        for ent in ents.items:
            if not isinstance(ent, pbir.JObj):
                continue
            ms = ent.get("measures")
            if ms is not None and not isinstance(ms, pbir.JArr):
                # A present, non-array measures container is a
                # structurally unreadable inventory, like a non-array
                # entities value: refuse rather than report it empty.
                # The check runs before the name skip, so an unnamed
                # entity cannot hide a malformed container.
                raise PbzError("REFUSED", "definition/reportExtensions.json")
            nm = ent.get("name")
            if not isinstance(nm, pbir.JStr):
                continue
            measures = []
            if isinstance(ms, pbir.JArr):
                for m in ms.items:
                    mn = m.get("name") if isinstance(m, pbir.JObj) else None
                    if isinstance(mn, pbir.JStr):
                        measures.append(mn.value)
            out.append((nm.value, measures))
    return out


def _extension_qualified(report_dir, old):
    """Qualified paths of the report-extension measures defined for a table.

    reportExtensions.json can attach measures to a model table inside the
    report. They are not model objects, but reference strings naming them
    belong to the table and must follow its rename like model measures do."""
    return {_fold(f"{name}.{m}")
            for name, measures in _extension_entities(report_dir)
            if _fold(name) == _fold(old) for m in measures}


def _extension_owned(report_dir, home):
    """Qualified paths of report-extension measures on tables whose dotted
    name extends home: strings the longer-table reading owns."""
    return {_fold(f"{name}.{m}")
            for name, measures in _extension_entities(report_dir)
            if _fold(name).startswith(_fold(home) + ".") for m in measures}


def _extension_bare_names(report_dir):
    """Folded names of every report-extension measure on every entity:
    the bare-name ownership universe for an unanchored bare reference,
    which the flattened qualified paths cannot be split back into."""
    return {_fold(m) for _name, measures in _extension_entities(report_dir)
            for m in measures}


def _extension_owned_dest(report_dir, old, new):
    """Qualified paths of report-extension measures on entities extending
    the destination name, excluding the renamed entity itself: ownership
    in the destination namespace, where the renamed table's own extension
    paths are not another table's."""
    return {_fold(f"{name}.{m}")
            for name, measures in _extension_entities(report_dir)
            if _fold(name).startswith(_fold(new) + ".") and _fold(name) != _fold(old)
            for m in measures}


def _extension_owned_others(report_dir, home):
    """Qualified paths of report-extension measures on any entity other
    than home: strings another table's reading owns."""
    return {_fold(f"{name}.{m}")
            for name, measures in _extension_entities(report_dir)
            if _fold(name) != _fold(home) for m in measures}


def _extension_tables(report_dir):
    """Entity names defined in reportExtensions.json, including report-only
    entities the model does not know: a fresh reference to one must stop a
    partial rewrite like a model table reference does."""
    return [name for name, _measures in _extension_entities(report_dir)]


def _doc_anchored(root, kind, home, old):
    """True when the document itself anchors bare references to the rename
    target: some string in it is the qualified home.old path. A bare
    nativeQueryRef in an anchored document denotes the home field even
    when another table has a field of the same name."""
    if kind == "table":
        return True
    target = _fold(f"{home}.{old}")

    def walk(node):
        # Every string in the document counts, under any key: a semantic
        # query stores the qualified path in a Select entry's Name, not
        # only in the reference keys.
        if isinstance(node, pbir.JStr):
            return _fold(node.value) == target
        if isinstance(node, pbir.JObj):
            return any(walk(v) for _k, v in node.pairs)
        if isinstance(node, pbir.JArr):
            return any(walk(v) for v in node.items)
        return False

    return walk(root)


def _raw_identity(field):
    """A projection's identity without its identifier text: kind, table,
    name, hierarchy, level, function, and variation, folded where textual.
    An aggregation of a column is a different identity from the column."""
    return (field.kind, _fold(field.table), _fold(field.name), _fold(field.hierarchy),
            _fold(field.level), field.function, field.variation)


def _literal_other_owner(model, value, home, old):
    """True when another field's qualified path owns the string at least
    as specifically as the target's path: the string equals that path or
    extends it with dotted segments, and the path is no shorter than the
    target's own. Such a string denotes that field (or its extensions),
    whatever reading as an extension of the target it also admits."""
    folded = _fold(value)
    base = _fold(f"{home}.{old}")
    for c in list(model.all_columns()) + list(model.all_measures()):
        if _fold(c.table) == _fold(home) and _fold(c.name) == _fold(old):
            continue
        path = _fold(f"{c.table}.{c.name}")
        if len(path) >= len(base) and (folded == path or folded.startswith(path + ".")):
            return True
    return False


def _report_string_rename(key, value, kind, home, old, new, field=None, model=None,
                          extra_qualified=(), extra_owned=(), extra_extending=(),
                          extra_tables=(), dest_owned=None, dest_extending=None,
                          anchored=True, ext_bare=()):
    nv, wrapped_used = _report_string_rename_inner(key, value, kind, home, old, new, field, model,
                                                   extra_qualified, extra_owned, extra_extending,
                                                   extra_tables, anchored, ext_bare)
    if nv != value and model is not None:
        # The emitted string, exactly as produced (extensions retained from
        # the source string, wrapper text included), must not be owned by
        # another table in the destination namespace; a wrapped emission is
        # also checked on its inner path. Destination ownership uses its own
        # sets: the source namespace's sets do not decide it.
        anchor = old if kind == "table" else home
        head = new if kind == "table" else home
        bare = key == "nativeQueryRef" and _fold(value) == _fold(old)
        owned = extra_owned if dest_owned is None else dest_owned
        extending = extra_extending if dest_extending is None else dest_extending
        dest_tables = None
        dest_extra = extra_tables
        if kind == "table":
            # Suffixes are classified against the post-rename table names:
            # the old name no longer exists in the destination namespace,
            # in the model or in the report-extension entities.
            dest_tables = [new if _fold(t.name) == _fold(old) else t.name
                           for t in model.tables.values()]
            dest_extra = [new if _fold(e) == _fold(old) else e for e in extra_tables]
        produced = [nv]
        if wrapped_used:
            # An inner path is checked only when the rewrite actually used
            # the wrapped reading, never when a literal output merely
            # parses as a wrapper. The emission must still parse as the
            # same wrapper: a destination that breaks the wrapper (a
            # parenthesis in a table name) would leave the reference
            # unrecognizable to every later rename.
            wrapped = _wrapped_inner(nv)
            source = _wrapped_inner(value)
            if not wrapped or source is None or _fold(wrapped[0]) != _fold(source[0]):
                raise PbzError("NAME_TAKEN", nv)
            produced.append(wrapped[1])
        for text in produced:
            if _other_table_owns(model, anchor, text, owned, extending,
                                 extensions_any=bare, extra_tables=dest_extra, head=head,
                                 dest_tables=dest_tables):
                raise PbzError("NAME_TAKEN", nv)
        if kind == "table":
            # The emitted path must stay recognizable as its own field's
            # path in the destination namespace: a retained suffix that is
            # not a pure extension there (it names the new table as a fresh
            # reference) would strand the string on the next rename.
            bases = []
            for t in model.tables.values():
                tname = new if _fold(t.name) == _fold(old) else t.name
                bases += [f"{tname}.{o.name}"
                          for o in list(t.columns.values()) + list(t.measures.values())]
                bases += [f"{tname}.{h}.{lvl}" for h, levels in t.hierarchies.items()
                          for lvl in levels]
            for q in extra_qualified:
                if _fold(q).startswith(_fold(old) + "."):
                    bases.append(new + q[len(_fold(old)):])
            names = list(dest_tables or ()) + list(dest_extra or ())
            # Recognizability is judged on the path the rewrite produced:
            # the inner path for a wrapped emission, the string itself
            # otherwise. The wrapper text is not a path.
            recog = produced[1:] if wrapped_used else produced[:1]
            own_base = None
            if field is not None and field is not _AMBIGUOUS:
                if field.kind == "hierarchy" and field.hierarchy and not field.variation:
                    own_base = f"{new}.{field.hierarchy}.{field.level}"
                else:
                    own_base = f"{new}.{field.name}"
            for text in recog:
                if own_base is not None:
                    rest = _rest_after(text, _fold(own_base))
                    if _fold(text) != _fold(own_base) and rest is None:
                        continue
                    cands = [own_base]
                else:
                    cands = [b for b in bases
                             if _fold(text) == _fold(b) or _rest_after(text, _fold(b)) is not None]
                    if not cands:
                        continue

                    def _consumed(b):
                        # The candidate consuming the most of the emitted
                        # path wins: folding can change code point counts,
                        # so raw string length does not measure the match.
                        if _fold(text) == _fold(b):
                            return -1
                        r = _rest_after(text, _fold(b))
                        return len(_fold(r)) if r is not None else 1 << 30

                    cands = [min(cands, key=_consumed)]
                rest = _rest_after(text, _fold(cands[0]))
                if rest is not None and not _path_extension(rest, new, names):
                    raise PbzError("AMBIGUOUS",
                                   f"the renamed path {nv} would no longer be recognized; nothing renamed")
    return nv


def _report_string_rename_inner(key, value, kind, home, old, new, field=None, model=None,
                                extra_qualified=(), extra_owned=(), extra_extending=(),
                                extra_tables=(), anchored=True, ext_bare=()):
    if kind == "table":
        if field is _AMBIGUOUS:
            return value, False
        if key in ("queryRef", "metadata", "queryName", "nativeQueryRef"):
            if field is not None:
                # The resolved field settles ownership of a string naming its
                # own qualified path (exact or pure extension, raw or wrapped):
                # the string follows the field's table rename, and no other
                # table's, even when a longer table name shares the prefix.
                # The literal value is tested before its wrapped reading, and
                # a hierarchy field's path names the hierarchy and level, not
                # the level's backing column.
                if field.kind == "hierarchy" and field.hierarchy and not field.variation:
                    base = f"{field.table}.{field.hierarchy}.{field.level}"
                else:
                    base = f"{field.table}.{field.name}"
                wrapped = _wrapped_inner(value)
                probes = [(value, None)] + ([(wrapped[1], wrapped[0])] if wrapped else [])
                for probe, wrap_name in probes:
                    _rest = _rest_after(probe, _fold(base))
                    if _fold(probe) == _fold(base) \
                            or (_rest is not None
                                and _path_extension(_rest, old,
                                                    ([t.name for t in model.tables.values()]
                                                     if model is not None else []) + list(extra_tables))):
                        if _fold(field.table) != _fold(old):
                            return value, False
                        swapped = _head_swap(probe, field.table, new)
                        return (f"{wrap_name}({swapped})" if wrap_name else swapped), wrap_name is not None
                # A resolved binding owns the string even when its text does
                # not match the field's own path: never fall through to the
                # unbound rewrite and desync it from its field.
                return value, False
            _tbl, qualified = _table_qualified(model, old)
            if _tbl is not None:
                return _table_rewrite_flagged(value, old, new, qualified | set(extra_qualified), _tbl, model,
                                      other_owned=extra_owned, other_extending=extra_extending,
                                      other_tables=extra_tables, key=key, ext_bare=ext_bare)
            if _fold(value).startswith(_fold(old) + "."):
                return _head_swap(value, old, new), False
            wrapped = _single_wrapped(value)
            _rest = _rest_after(wrapped[1], _fold(old)) if wrapped else None
            if wrapped and _rest is not None \
                    and _path_extension(_rest, old,
                                        ([t.name for t in model.tables.values()]
                                         if model is not None else []) + list(extra_tables)):
                return f"{wrapped[0]}({_head_swap(wrapped[1], old, new)})", True
        return value, False
    if key == "queryName":
        return value, False
    if field is _AMBIGUOUS:
        return value, False
    if field is not None:
        if kind == "column" and field.variation \
                and _fold(field.table) == _fold(home) and _fold(field.name) == _fold(old):
            # An auto date/time variation field names its base column: a
            # string naming that column, or a pure extension of its path
            # (the variation and hierarchy segments), follows the rename,
            # raw or wrapped, as does a bare nativeQueryRef. The remainder
            # check tests table names, not the column's own name.
            base = f"{field.table}.{field.name}"
            if _fold(value) == _fold(base):
                return f"{home}.{new}", False
            if key == "nativeQueryRef" and _fold(value) == _fold(field.name):
                return new, False

            def _follow(text):
                if _fold(text) == _fold(base):
                    return f"{home}.{new}"
                rest = _rest_after(text, _fold(base))
                if rest is not None and _path_extension(
                        rest, home,
                        ([t.name for t in model.tables.values()] if model is not None else [])
                        + list(extra_tables)):
                    return _prefix_swap(text, base, f"{home}.{new}")
                return None

            out = _follow(value)
            if out is not None:
                return out, False
            wrapped = _wrapped_call(value)
            if wrapped:
                inner = _follow(wrapped[1])
                if inner is not None:
                    return f"{wrapped[0]}({inner})", True
            return value, False
        field_kind = "column" if field.kind == "aggregation" else field.kind
        if field_kind != kind or _fold(field.table) != _fold(home) or _fold(field.name) != _fold(old):
            return value, False
        if _fold(value) in (_fold(f"{field.table}.{field.name}"), _fold(f"{home}.{old}")):
            return f"{home}.{new}", False
        if key == "nativeQueryRef" and _fold(value) == _fold(field.name):
            return new, False
        if field_kind == "column":
            wrapped = _wrapped_call(value)
            if wrapped and _fold(wrapped[1]) in (
                    _fold(f"{field.table}.{field.name}"), _fold(f"{home}.{old}")):
                return f"{wrapped[0]}({home}.{new})", True
        return value, False
    if key in ("queryRef", "metadata", "nativeQueryRef") and _fold(value) == _fold(f"{home}.{old}") \
            and not _other_table_owns(model, home, value, extra_owned, extra_extending,
                                      extra_tables=extra_tables):
        return f"{home}.{new}", False
    if key == "nativeQueryRef" and _fold(value) == _fold(old) \
            and not _other_table_owns(model, home, value, extra_owned, extra_extending,
                                      extensions_any=True, extra_tables=extra_tables,
                                      bare_fields=not anchored, ext_bare=ext_bare):
        return new, False
    if kind == "column" and key in ("queryRef", "metadata", "nativeQueryRef") \
            and _other_table_owns(model, home, value, extra_owned, extra_extending,
                                  extra_tables=extra_tables):
        return value, False
    if kind == "column" and key in ("queryRef", "metadata", "nativeQueryRef"):
        # A bare nativeQueryRef can name another table's field by its
        # whole literal name, wrapper-shaped text included: that
        # ownership blocks the wrapper retry just as it blocks the
        # bare rewrite.
        wrapped = _wrapped_call(value)
        if wrapped and _fold(wrapped[1]) == _fold(f"{home}.{old}") \
                and not _other_table_owns(model, home, wrapped[1], extra_owned, extra_extending,
                                          extra_tables=extra_tables) \
                and not (key == "nativeQueryRef"
                         and _other_table_owns(model, home, value, extra_owned, extra_extending,
                                               extensions_any=True, extra_tables=extra_tables,
                                               bare_fields=not anchored, ext_bare=ext_bare)):
            return f"{wrapped[0]}({home}.{new})", True
    if key == "queryRef" and kind in ("column", "measure"):
        # An unowned queryRef that is a pure path extension of the target
        # (for example a variation path in a bookmark file) has the target
        # as its only possible owner, so it follows like the owned form,
        # unless the full string is literally another field's own path.
        rest = _rest_after(value, _fold(f"{home}.{old}"))
        if rest is not None and _path_extension(
                rest, home,
                ([t.name for t in model.tables.values()] if model is not None else [])
                + list(extra_tables)) \
                and not (model is not None and _literal_other_owner(model, value, home, old)):
            return _prefix_swap(value, f"{home}.{old}", f"{home}.{new}"), False
    return value, False


def _binding_edits(bindings, kind, home, old, new, text, *, model=None):
    edits = []
    if model is not None and kind == "column" and _fold(new) != _fold(old) \
            and any(_fold(c.table) != _fold(home) and _fold(c.name) == _fold(new)
                    for c in model.all_columns()):
        for binding in bindings:
            ref = binding.ref
            if binding.qualifier is None and ref.kind in ("column", "unresolved") \
                    and (_fold(ref.name) == _fold(new)
                         or (_fold(ref.name) == _fold(old) and _fold(ref.table) == _fold(home))):
                raise PbzError("NAME_TAKEN", f"a bare reference could be captured by {new}; nothing renamed")
    for binding in bindings:
        ref = binding.ref
        if _fold(ref.name) != _fold(old):
            continue
        if binding.uncertain:
            raise PbzError("AMBIGUOUS", f"cannot safely bind a reference to {old}; nothing renamed")
        if kind == "measure" and binding.qualifier is not None:
            a, b = binding.qualifier
            tok = lexers.significant(lexers.lex_dax(text[a:b]))[0]
            if _fold(tok.value or tok.text) != _fold(home):
                continue
        if ref.kind == kind and _fold(ref.table) == _fold(home):
            edits.append((binding.start, binding.end, _bracket(new)))
    return edits


def dax_ref_edits(text: str, start: int, end: int, kind: str, home: str, old: str, new: str,
                  cur: str | None = None, *, res, row_home: bool = False) -> list[tuple[int, int, str]]:
    """Rename exactly the occurrences bound to the target by the shared resolver."""
    bindings = deps.dax_bindings(text[start:end], cur or "", res, row_home, base=start).bindings
    return _binding_edits(bindings, kind, home, old, new, text, model=res.model)


def _pvs_property_node(node):
    """The PropertyVariationSource Property string node of a field object, or
    None: the node naming the base column of an auto date/time variation."""
    if not isinstance(node, pbir.JObj):
        return None
    agg = node.get("Aggregation")
    if isinstance(agg, pbir.JObj):
        return _pvs_property_node(agg.get("Expression"))
    grp = node.get("GroupRef")
    if isinstance(grp, pbir.JObj):
        expr = grp.get("Expression")
        pvs = expr.get("PropertyVariationSource") if isinstance(expr, pbir.JObj) else None
        prop = pvs.get("Property") if isinstance(pvs, pbir.JObj) else None
        return prop if isinstance(prop, pbir.JStr) else None
    for key in ("Column", "HierarchyLevel"):
        inner = node.get(key)
        if isinstance(inner, pbir.JObj):
            expr = inner.get("Expression")
            if key == "HierarchyLevel" and isinstance(expr, pbir.JObj):
                h = expr.get("Hierarchy")
                expr = h.get("Expression") if isinstance(h, pbir.JObj) else None
            pvs = expr.get("PropertyVariationSource") if isinstance(expr, pbir.JObj) else None
            prop = pvs.get("Property") if isinstance(pvs, pbir.JObj) else None
            return prop if isinstance(prop, pbir.JStr) else None
    return None


# The DAX reserved words, plus the bare enum values and type names
# that appear as standalone arguments (ASC, DESC, SKIP, DENSE, BLANKS,
# FIRST, LAST, DEFAULT, ABS, REL, the date interval units SECOND
# through YEAR, the CROSSFILTER directions, and the CONVERT types):
# an unquoted token with one of these texts is the keyword, never a
# table reference, since DAX itself requires such table names to be
# quoted.
_DAX_KEYWORDS = {_fold(w) for w in (
    "abs", "anchored", "and", "asc", "blanks", "boolean", "both", "break",
    "by",
    "column", "currency", "datetime", "day", "decimal", "default", "define",
    "densify", "dense", "desc", "double", "else", "endaligned", "evaluate",
    "extending", "false",
    "first", "group", "hour", "in", "index", "int64", "integer", "last",
    "logical", "matchby",
    "measure", "minute", "month", "mparameter", "none", "not", "oneway",
    "oneway_leftfiltersright",
    "oneway_rightfiltersleft", "or", "order", "precise", "quarter", "rel",
    "return", "row", "string", "variant",
    "second", "skip", "table", "text", "then", "total", "true", "var",
    "week", "window", "year")}


def _dax_string_edits(text, model, kind, home, old, new, cur, ext_measures=None):
    """Binding edits renaming the target inside one DAX expression string,
    under the same rules as the TMDL editors. The model-only resolver does
    not know report-extension measures, so a binding denoting one (a bare
    reference naming one, or a qualified reference to one on its entity)
    is never edited through it: a qualified reference to an extension
    measure on the renamed table still follows that table's qualifier."""
    ext_measures = ext_measures or {}
    ext_names = set().union(*ext_measures.values()) if ext_measures else set()
    if kind == "table":
        # The emitted table name must not capture a DAX variable defined in
        # the expression: the rewritten reference would bind the variable.
        toks = lexers.significant(lexers.lex_dax(text))
        for i, tok in enumerate(toks[:-1]):
            if tok.kind == "ident" and tok.text.upper() == "VAR" \
                    and toks[i + 1].kind == "ident" and _fold(toks[i + 1].text) == _fold(new):
                raise PbzError("AMBIGUOUS",
                               f"a variable named {new} is defined in a report expression; nothing renamed")
    res = deps.Resolver(model)
    if ext_names:
        # A bare reference bearing an extension measure's name can denote
        # that measure, so the resolver's unique-column fallback must not
        # fire for any such name: the binding surfaces as unresolved
        # instead of a firm column binding, and it cannot steer the
        # resolver's row-table inference. Row-context and home bindings
        # do not use that index.
        for key in [k for k in res.by_column if _fold(k) in ext_names]:
            res.by_column.pop(key, None)

    def _qualifier_entity(binding):
        if not binding.qualifier:
            return None
        a, b = binding.qualifier
        tok = lexers.significant(lexers.lex_dax(text[a:b]))[0]
        return tok.value or tok.text

    def _ext_bound(binding):
        ent = _qualifier_entity(binding)
        if ent is not None:
            return _fold(binding.ref.name) in ext_measures.get(_fold(ent), set())
        if binding.qualifier is None:
            # A spaced qualifier (Budget [Amount]) is still a qualified
            # reference to that entity's extension measure, although the
            # resolver leaves the binding bare. A DAX keyword in that
            # position (RETURN [Amount]) is not a qualifier.
            toks = lexers.significant(lexers.lex_dax(text[:binding.start]))
            if toks and toks[-1].kind in ("ident", "table") \
                    and _fold(toks[-1].value or toks[-1].text) not in _DAX_KEYWORDS:
                prev_name = toks[-1].value or toks[-1].text
                if _fold(binding.ref.name) in ext_measures.get(_fold(prev_name), set()):
                    return True
        if binding.ref.kind == "measure" and _fold(binding.ref.name) in ext_names:
            # A bare measure reference is ambiguous whenever a report-
            # extension measure bears the name: measures bind by name
            # alone, so the resolver's firm answer cannot distinguish
            # the model measure from the extension measure.
            return True
        if binding.ref.kind == "table" or not (binding.uncertain or binding.ref.kind == "unresolved"):
            # A firm binding (a table reference, or a row-context column)
            # is the resolver's answer and is honored; only an unresolved
            # or uncertain bare reference can be the extension measure.
            return False
        return _fold(binding.ref.name) in ext_names

    def _spurious_qualifier(binding):
        # An unquoted DAX keyword in qualifier position (RETURN [Amount]
        # with a table named Return in the model) is the keyword, never a
        # qualifier; a quoted name is a real qualifier.
        if not binding.qualifier:
            return False
        a, b = binding.qualifier
        tok = lexers.significant(lexers.lex_dax(text[a:b]))[0]
        return tok.kind == "ident" and _fold(tok.value or tok.text) in _DAX_KEYWORDS

    def _keyword_self(binding):
        # A binding whose own span is a bare DAX keyword token (RETURN in
        # "VAR x = 1 RETURN x" with a table named Return) is the keyword,
        # never a reference to that table.
        toks = lexers.significant(lexers.lex_dax(text[binding.start:binding.end]))
        return len(toks) == 1 and toks[0].kind == "ident" \
            and _fold(toks[0].value or toks[0].text) in _DAX_KEYWORDS

    mask_spans = []
    ext_refs = []
    if ext_measures:
        # A qualified reference to an entity's extension measure
        # (Budget [Limit]) is invisible to the model-only resolver as
        # such: it mis-reads the bracket in the current context, and
        # the mis-read can poison its row-table inference for later
        # references. Mask the whole reference lexically so the
        # resolver never sees it; a table rename of that entity still
        # rewrites the qualifier from the lexical span below.
        toks = lexers.significant(lexers.lex_dax(text))
        for i in range(len(toks) - 1):
            t0, t1 = toks[i], toks[i + 1]
            if t0.kind in ("ident", "table") and t1.kind == "bracket":
                ent = _fold(t0.value or t0.text)
                if ent in ext_measures \
                        and not (t0.kind == "ident" and ent in _DAX_KEYWORDS) \
                        and _fold(t1.value or t1.text) in ext_measures[ent]:
                    mask_spans.append((t0.start, t1.end))
                    ext_refs.append((t0.start, t0.end, ent, t0.kind == "table"))
    raw_bindings = deps.dax_bindings(text, cur, res).bindings
    mask_spans += [b.qualifier for b in raw_bindings if _spurious_qualifier(b)]
    mask_spans += [(b.start, b.end) for b in raw_bindings if _keyword_self(b)]
    for b in raw_bindings:
        if not b.uncertain:
            continue
        # A keyword touching the bracket (RETURN[Amount]) made the
        # resolver mark a sound row-context binding uncertain although
        # the keyword itself was never bound as a table.
        toks = lexers.significant(lexers.lex_dax(text[:b.start]))
        if toks and toks[-1].kind == "ident" and toks[-1].end == b.start \
                and _fold(toks[-1].value or toks[-1].text) in _DAX_KEYWORDS:
            mask_spans.append((toks[-1].start, toks[-1].end))
    if mask_spans:
        # A keyword the resolver mistook for a table (as a bracket's
        # qualifier, as a standalone binding, or as an iterator's row
        # table) poisons the bindings around it. Mask every such token
        # as a parenthesized number (same length, offsets preserved;
        # spaces would let the preceding name become a spaced
        # qualifier, and bare digits would glue to a neighboring
        # identifier and form a phantom token) and take the bindings
        # from a second pass over the masked text, where the keyword
        # can no longer be misread.
        chars = list(text)
        for a, z in mask_spans:
            n = z - a
            chars[a:z] = "(" + "1" * (n - 2) + ")" if n >= 2 else "1"
        bindings = deps.dax_bindings("".join(chars), cur, res).bindings
        # A masked span touching an identifier character glues into a
        # single phantom token (RETURN'B'[M] masks to RETURN111111);
        # a binding that partially overlaps a mask span is that glue,
        # never a real reference, so it is dropped.
        bindings = [b for b in bindings
                    if not any(b.start < z and b.end > a
                               and not (a <= b.start and b.end <= z)
                               for a, z in mask_spans)]
    else:
        bindings = raw_bindings
    if kind == "table":
        low = _fold(old)
        out = []
        for binding in bindings:
            ref = binding.ref
            if _ext_bound(binding):
                ent = _qualifier_entity(binding)
                if ent is not None and _fold(ent) == low:
                    a, b = binding.qualifier
                    tok = lexers.significant(lexers.lex_dax(text[a:b]))[0]
                    out.append((a, b, _dax_table(new, tok.kind == "table")))
                continue
            if binding.uncertain and _fold(ref.table) == low:
                raise PbzError("AMBIGUOUS", f"cannot safely bind a reference to {old}")
            if ref.kind == "table" and _fold(ref.name) == low:
                quoted = text[binding.start:binding.start + 1] == "'"
                out.append((binding.start, binding.end, _dax_table(new, quoted)))
            elif binding.qualifier and ref.kind in {"column", "measure"}:
                a, b = binding.qualifier
                tok = lexers.significant(lexers.lex_dax(text[a:b]))[0]
                if _fold(tok.value or tok.text) == low:
                    out.append((a, b, _dax_table(new, tok.kind == "table")))
        for a, b, ent, quoted in ext_refs:
            if ent == low:
                out.append((a, b, _dax_table(new, quoted)))
        return out
    kept = [b for b in bindings if not _ext_bound(b)]
    return _binding_edits(kept, kind, home, old, new, text, model=model)


def _measure_expression_edits(root, model, kind, home, old, new):
    """Span edits for the DAX expression strings of report-extension
    measures. Only direct measure entries of the document's root
    entities array (the reportExtensions.json shape, the same
    traversal that renames the entity declarations) qualify; a name
    and expression object anywhere else is not a measure and is
    never edited."""
    edits = []
    entities = []
    if isinstance(root, pbir.JObj):
        ents = root.get("entities")
        if isinstance(ents, pbir.JArr):
            entities.extend(item for item in ents.items if isinstance(item, pbir.JObj))
    ext_measures = {}
    for ent in entities:
        nm = ent.get("name")
        measures = ent.get("measures")
        if isinstance(nm, pbir.JStr) and isinstance(measures, pbir.JArr):
            names = set()
            for m in measures.items:
                mn = m.get("name") if isinstance(m, pbir.JObj) else None
                if isinstance(mn, pbir.JStr):
                    names.add(_fold(mn.value))
            # Duplicate entries for one entity union their measure
            # sets; a later entry never replaces an earlier one.
            ext_measures.setdefault(_fold(nm.value), set()).update(names)
    for ent in entities:
        nm = ent.get("name")
        measures = ent.get("measures")
        if not (isinstance(nm, pbir.JStr) and isinstance(measures, pbir.JArr)):
            continue
        for m in measures.items:
            if not isinstance(m, pbir.JObj):
                continue
            mn = m.get("name")
            expr = m.get("expression")
            if isinstance(expr, pbir.JStr) and isinstance(mn, pbir.JStr):
                sub = _dax_string_edits(expr.value, model, kind, home, old, new, nm.value, ext_measures)
                if sub:
                    edits.append((expr.start, expr.end,
                                  json.dumps(apply_edits(expr.value, sub), ensure_ascii=False)))
            refs = m.get("references")
            if refs is not None and kind in ("measure", "table"):
                # Dependency records follow the expression: an object
                # with entity and name and no schema string denotes a
                # model measure (the consolidate merge rewrites the
                # same records); a schema-bearing record denotes an
                # extension measure and is left alone.
                stack = [refs]
                while stack:
                    node = stack.pop()
                    if isinstance(node, pbir.JArr):
                        stack.extend(node.items)
                    elif isinstance(node, pbir.JObj):
                        ent = node.get("entity")
                        name = node.get("name")
                        schema = node.get("schema")
                        if isinstance(ent, pbir.JStr) and isinstance(name, pbir.JStr) \
                                and not isinstance(schema, pbir.JStr):
                            if kind == "table" and _fold(ent.value) == _fold(old):
                                edits.append((ent.start, ent.end,
                                              json.dumps(new, ensure_ascii=False)))
                            elif kind == "measure" and _fold(ent.value) == _fold(home) \
                                    and _fold(name.value) == _fold(old):
                                edits.append((name.start, name.end,
                                              json.dumps(new, ensure_ascii=False)))
                        stack.extend(v for _k, v in node.pairs)
    return edits


def pbir_ref_edits(root, model, kind: str, home: str, old: str, new: str,
                   extra_owned=(), extra_extending=(), extra_tables=(),
                   selector_claims=None, ext_bare=()) -> list[tuple[int, int, str]]:
    """Edits renaming measure / column `old` of table `home` in one parsed PBIR document: every field
    reference's Property, and queryRef / nativeQueryRef / metadata strings."""
    edits = []
    want = "measure" if kind == "measure" else "column"
    fields = pbir.walk_fields_with_path(root, model)
    for ref, _site in fields:
        k = "column" if ref.kind == "aggregation" else ref.kind
        if k == want and _fold(ref.name) == _fold(old) and _fold(ref.table) == _fold(home) and ref.prop_node:
            edits.append((ref.prop_node.start, ref.prop_node.end, json.dumps(new, ensure_ascii=False)))
    if kind == "column":
        # A variation field carries no prop_node for its base column: the
        # base lives in the PropertyVariationSource Property, found by
        # walking the document with the same From-alias scoping the field
        # walker uses.
        def visit(node, aliases):
            if isinstance(node, pbir.JArr):
                for v in node.items:
                    visit(v, aliases)
                return
            if not isinstance(node, pbir.JObj):
                return
            frm = node.get("From")
            if isinstance(frm, pbir.JArr):
                aliases = dict(aliases)
                for e in frm.items:
                    if isinstance(e, pbir.JObj) and isinstance(e.get("Name"), pbir.JStr) \
                            and isinstance(e.get("Entity"), pbir.JStr):
                        aliases[e.get("Name").value] = e.get("Entity").value
            ref = pbir.field_of(node, aliases, model)
            if ref is not None and ref.variation and _fold(ref.table) == _fold(home) \
                    and _fold(ref.name) == _fold(old):
                prop = _pvs_property_node(node)
                if prop is not None:
                    edits.append((prop.start, prop.end, json.dumps(new, ensure_ascii=False)))
            for _k, v in node.pairs:
                visit(v, aliases)

        visit(root, {})
    home, old = _canonical_target(model, kind, home, old)
    anchored = _doc_anchored(root, kind, home, old)
    sites = _report_string_sites(root, model, fields)
    outputs = {}
    proven, projection_owners = {}, {}
    for site, (node, field) in sites.items():
        nv = _report_string_rename(site[-1], node.value, kind, home, old, new, field, model,
                                   extra_owned=extra_owned, extra_extending=extra_extending,
                                   extra_tables=extra_tables, anchored=anchored,
                                   ext_bare=ext_bare)
        if kind == "column" and _fold(new) != _fold(old) and field is None \
                and site[-1] == "nativeQueryRef" and _fold(node.value) == _fold(old) \
                and nv != node.value and any(_fold(c.table) != _fold(home) and _fold(c.name) == _fold(new)
                                            for c in model.all_columns()):
            raise PbzError("NAME_TAKEN", f"a bare report reference could be captured by {new}; nothing renamed")
        outputs[site] = nv
        if site[-1] == "queryRef" and field is not None and field is not _AMBIGUOUS:
            projection_owners.setdefault(_fold(node.value), set()).add((
                field.kind, _fold(field.table), _fold(field.name), _fold(field.hierarchy),
                _fold(field.level), field.function, field.variation))
        if site[-1] == "queryRef":
            # Only a queryRef is a projection identifier a selector can
            # link to; a nativeQueryRef label or a metadata string with
            # the same text must not supply the selector's candidate.
            proven.setdefault(node.value, set()).add(nv)
    finals = {}
    for site, (node, field) in sites.items():
        nv = outputs[site]
        if kind in ("column", "measure") and site[-2:] == ("selector", "metadata") and field is None:
            # A projection in any file carrying this text as its queryRef
            # claims the selector for itself: another field's claim keeps
            # the selector with that projection (its text does not move,
            # whatever the string's shape), a claim by both sides is
            # ambiguous, and only an unclaimed or target-claimed selector
            # follows the shape-based rewrites below.
            claim = (selector_claims or {}).get(_fold(node.value), set())
            if "other" in claim:
                nv = node.value
            elif nv == node.value and not any(v != node.value for v in proven.get(node.value, ())):
                # No proven projection in this file rewrites the selector,
                # but the string itself is a pure path extension of the
                # target (for example a variation path in a bookmark
                # file): the target is its only possible owner, so it
                # follows the rename instead of being left pointing at
                # the old path.
                rest = _rest_after(node.value, _fold(f"{home}.{old}"))
                if rest is not None and _path_extension(
                        rest, home,
                        ([t.name for t in model.tables.values()] if model is not None else [])
                        + list(extra_tables)) \
                        and not (model is not None
                                 and _literal_other_owner(model, node.value, home, old)):
                    followed = _prefix_swap(node.value, f"{home}.{old}", f"{home}.{new}")
                    if model is not None and _other_table_owns(
                            model, home, followed, extra_owned, extra_extending,
                            extra_tables=extra_tables):
                        raise PbzError("REFUSED", "a formatting selector cannot be resolved uniquely; nothing renamed")
                    nv = followed
            candidates = set(proven.get(node.value, ()))
            if nv != node.value:
                candidates.add(nv)
            if "target" in claim and "other" in claim \
                    and (nv != node.value or any(value != node.value for value in candidates)):
                raise PbzError("REFUSED", "a formatting selector cannot be resolved uniquely; nothing renamed")
            if "other" not in claim and any(value != node.value for value in candidates):
                if len(candidates) != 1 or len(projection_owners.get(_fold(node.value), ())) > 1:
                    raise PbzError("REFUSED", "a formatting selector cannot be resolved uniquely; nothing renamed")
                nv = next(iter(candidates))
                carrier_ids = {_raw_identity(f) for s2, (n2, f) in sites.items()
                               if s2[-1] == "queryRef" and f is not None and f is not _AMBIGUOUS
                               and _fold(n2.value) == _fold(node.value)}
                if any(other.value != node.value and outputs[s] == nv
                       and not (_fold(other.value) == _fold(node.value) and outputs[s] != other.value)
                       and not (_owner is not None and _owner is not _AMBIGUOUS
                                and _raw_identity(_owner) in carrier_ids)
                       for s, (other, _owner) in sites.items() if s[-1] == "queryRef"):
                    raise PbzError("REFUSED", "a formatting selector cannot be resolved uniquely; nothing renamed")
        finals[site] = nv
        if nv != node.value:
            edits.append((node.start, node.end, json.dumps(nv, ensure_ascii=False)))
    # A rewritten queryRef must not land on the final text of a queryRef
    # that answers to a different owner: two projections sharing one string
    # would leave any selector naming it pointing at either of them.
    owned_by_text = {}
    for site, (node, field) in sites.items():
        if site[-1] == "queryRef" and field is not None and field is not _AMBIGUOUS:
            owned_by_text.setdefault(_fold(node.value), set()).add(
                (field.kind, _fold(field.table), _fold(field.name), _fold(field.hierarchy),
                 _fold(field.level), field.function, field.variation, _fold(node.value)))
    finals_by_text = {}
    for site, (node, field) in sites.items():
        if site[-1] != "queryRef":
            continue
        if field is not None and field is not _AMBIGUOUS:
            identity = (field.kind, _fold(field.table), _fold(field.name), _fold(field.hierarchy),
                        _fold(field.level), field.function, field.variation, _fold(node.value))
        elif field is _AMBIGUOUS:
            identity = ("ambiguous", site)
        else:
            owners = owned_by_text.get(_fold(node.value), set())
            identity = next(iter(owners)) if len(owners) == 1 else ("unowned", _fold(node.value))
        finals_by_text.setdefault(_fold(finals[site]), []).append(
            (identity, finals[site] != node.value))
    for group in finals_by_text.values():
        if any(changed for _identity, changed in group) and len({i for i, _c in group}) > 1:
            raise PbzError("REFUSED", "the rename would make a report reference ambiguous; nothing renamed")
    edits.extend(_measure_expression_edits(root, model, kind, home, old, new))
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
    # A DAX keyword name is quoted even when the source was unquoted: the
    # bare token would be the keyword, not a table reference.
    return lexers.dax_table_name(new) if quoted or not IDENT_RE.fullmatch(new) \
        or _fold(new) in _DAX_KEYWORDS else new


def _m_ident(new: str) -> str:
    return new if IDENT_RE.fullmatch(new) else '#"' + new.replace('"', '""') + '"'


def translated_names(project) -> set[str]:
    """Lower-case names of objects with translations in real culture files the mirror lacks."""
    from .unused import translated
    keys = translated(project)
    return {k[2] for k in keys} | {k[1] for k in keys if k[1]}


def rename_table(ws, project, model, old: str, new: str) -> dict:
    t = _find_table(model, old)
    if t is None:
        raise PbzError("NOT_FOUND", f"table {old}")
    old = t.name
    if _fold(new) != _fold(old) and (any(_fold(t2.name) == _fold(new) for t2 in model.tables.values())
                                     or any(_fold(nm) == _fold(new)
                                            for nm in _extension_tables(project.mirror_report))):
        raise PbzError("NAME_TAKEN", new)
    if not new.strip() or new != new.strip() or any(c in new for c in "\r\n\t"):
        raise PbzError("BAD_REQUEST", "table names cannot be blank, padded or contain control characters")
    if any(_fold(n) == _fold(old) for n in translated_names(project)):
        raise PbzError("UNSUPPORTED", "cultures not in this mirror: run pbz pull")
    if any(_fold(e) == _fold(old) for e in model.expressions):
        raise PbzError("AMBIGUOUS", f"a shared query is also named {old}")
    if _fold(new) != _fold(old):
        rests = [o.name for o in list(t.columns.values()) + list(t.measures.values())]
        rests += [f"{h}.{lvl}" for h, levels in t.hierarchies.items() for lvl in levels]
        rests += [mn for ent, ms in _extension_entities(project.mirror_report)
                  if _fold(ent) == _fold(old) for mn in ms]
        for rest in rests:
            if _other_table_owns(model, old, f"{new}.{rest}",
                                 _extension_owned_others(project.mirror_report, old),
                                 _extension_owned_dest(project.mirror_report, old, new),
                                 extra_tables=[new if _fold(e) == _fold(old) else e
                                               for e in _extension_tables(project.mirror_report)],
                                 head=new,
                                 dest_tables=[new if _fold(t2.name) == _fold(old) else t2.name
                                              for t2 in model.tables.values()]):
                raise PbzError("NAME_TAKEN", f"{new}.{rest}")
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
    low = _fold(old)
    changed, renamed_file = [], None
    ops = []
    for rel, doc in model.docs.items():
        text = doc.text
        edits = []
        for node in doc.nodes:
            if node.kind == "table" and node.parent is None and _fold(node.name) == low:
                e = _decl_edit(text, node, "table", new)
                if e:
                    edits.append(e)
            elif node.kind == "partition" and node.parent is not None and _fold(node.parent.name) == low \
                    and _fold(node.name) == low:
                e = _decl_edit(text, node, "partition", new)
                if e:
                    edits.append(e)
            elif node.kind in ("perspectiveTable", "tablePermission") and _fold(node.name) == low:
                e = _decl_edit(text, node, node.kind, new)
                if e:
                    edits.append(e)
        for reg in doc.regions:
            seg = text[reg.start:reg.end]
            if reg.kind == "dax":
                for binding in deps.dax_region(text, reg, res).bindings:
                    ref = binding.ref
                    if binding.uncertain and _fold(ref.table) == low:
                        raise PbzError("AMBIGUOUS", f"cannot safely bind a reference to {old}")
                    if ref.kind == "table" and _fold(ref.name) == low:
                        quoted = text[binding.start:binding.start + 1] == "'"
                        edits.append((binding.start, binding.end, _dax_table(new, quoted)))
                    elif binding.qualifier and ref.kind in {"column", "measure"}:
                        a, b = binding.qualifier
                        # A qualified measure can name any table; rename its actual qualifier.
                        tok = lexers.significant(lexers.lex_dax(text[a:b]))[0]
                        if _fold(tok.value or tok.text) == low:
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
                if isinstance(names, list) and any(isinstance(n, str) and _fold(n) == low for n in names):
                    edits.append((reg.start, reg.end, json.dumps(
                        [new if isinstance(n, str) and _fold(n) == low else n for n in names],
                        separators=(",", ":"), ensure_ascii=False)))
        for m_ in re.finditer(r"(?m)^(\t*(?:fromColumn|toColumn|baseColumn|baseTable|defaultHierarchy)\s*:\s*)(.+?)\s*$", text):
            parts = tmdl.split_dotted(m_.group(2))
            if parts and _fold(parts[0]) == low:
                edits.append((m_.start(2), m_.end(2), ".".join([lexers.tmdl_name(new)] + [lexers.tmdl_name(p) for p in parts[1:]])))
        if rel == "model.tmdl":
            for m_ in re.finditer(r"(?m)^ref table ('(?:[^']|'')*'|[^\s]+)[ \t]*\r?$", text):
                if _fold(tmdl.unquote(m_.group(1))) == low:
                    edits.append((m_.start(1), m_.end(1), lexers.tmdl_name(new)))
        if not edits and rel != t.file:
            continue
        new_text = apply_edits(text, edits) if edits else text
        path = os.path.join(mdef, *rel.split("/"))
        _t, bom = mirror.read_mirror_text(path, root=ws.mirror)
        if rel == t.file and _fold(os.path.basename(rel)[:-5]) == low and old != new:
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

        def visit(node):
            if isinstance(node, pbir.JObj):
                ents = node.get("entities") if node is root else None
                for k, v in node.pairs:
                    if k.value == "Entity" and isinstance(v, pbir.JStr) and _fold(v.value) == low:
                        edits.append((v.start, v.end, json.dumps(new, ensure_ascii=False)))
                    visit(v)
                if isinstance(ents, pbir.JArr):
                    for ent in ents.items:
                        nm = ent.get("name") if isinstance(ent, pbir.JObj) else None
                        if isinstance(nm, pbir.JStr) and _fold(nm.value) == low:
                            edits.append((nm.start, nm.end, json.dumps(new, ensure_ascii=False)))
            elif isinstance(node, pbir.JArr):
                for v in node.items:
                    visit(v)

        visit(root)
        _tbl, _qualified = _table_qualified(model, old)
        if _tbl is not None:
            ext_q = _extension_qualified(project.mirror_report, old)
            ext_owned = _extension_owned_others(project.mirror_report, old)
            for site, (node, field) in _report_string_sites(root, model).items():
                if site[-1] in ("queryRef", "metadata", "queryName", "nativeQueryRef"):
                    nv = _report_string_rename(site[-1], node.value, "table", "", old, new,
                                               field, model, extra_qualified=ext_q,
                                               extra_owned=ext_owned,
                                               extra_extending=_extension_owned(project.mirror_report, old),
                                               extra_tables=_extension_tables(project.mirror_report),
                                               dest_owned=ext_owned,
                                               dest_extending=_extension_owned_dest(project.mirror_report, old, new),
                                               ext_bare=_extension_bare_names(project.mirror_report))
                    if nv != node.value:
                        edits.append((node.start, node.end, json.dumps(nv, ensure_ascii=False)))
            edits.extend(_measure_expression_edits(root, model, "table", "", old, new))
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
