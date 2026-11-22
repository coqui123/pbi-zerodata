"""Structured rename of a measure or column across the mirror's TMDL and PBIR files."""
from __future__ import annotations

import json
import ntpath
import os
import re

from . import deps, lexers, mirror, pbir, tmdl
from .common import PbzError, commit_bytes
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
        m = model.measure(old)
        if not m:
            raise PbzError("NOT_FOUND", f"measure {old}")
        if _fold(new) != _fold(old) and (any(_fold(mm.name) == _fold(new) for mm in model.all_measures())
                                         or any(_fold(c.name) == _fold(new) and c.table == m.table
                                                for c in model.all_columns())):
            raise PbzError("NAME_TAKEN", new)
        if any(_fold(c.name) == _fold(old) for c in model.all_columns()):
            raise PbzError("AMBIGUOUS", f"a column is also named {old}; rename it in Desktop")
        home = m.table
    elif kind == "column":
        hits = [c for c in model.all_columns() if _fold(c.name) == _fold(old) and (not table or _fold(c.table) == _fold(table))]
        if len(hits) != 1:
            raise PbzError("AMBIGUOUS" if hits else "NOT_FOUND", f"column {old}; pass --table")
        home = hits[0].table
        if _fold(new) != _fold(old) and (any(_fold(c.name) == _fold(new) and c.table == home
                                              for c in model.all_columns())
                                         or any(_fold(mm.name) == _fold(new) for mm in model.all_measures())):
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
            edits += _binding_edits(bindings, kind, home, old, new, text)
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
    for path, text, bom, rel in plan_pbir(
            ws, project.mirror_report,
            lambda root, _text: (pbir_ref_edits(root, model, kind, home, old, new,
                                                extra_owned=ext_owned,
                                                extra_extending=ext_ext,
                                                extra_tables=ext_tables), False)):
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
    ext_q = _extension_qualified(project.mirror_report, old) if kind == "table" else set()
    ext_owned = _extension_owned_others(project.mirror_report, home) if kind != "table" \
        else _extension_owned_others(project.mirror_report, old)
    ext_ext = _extension_owned(project.mirror_report, home) if kind != "table" \
        else _extension_owned(project.mirror_report, old)
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
        home, old = _canonical_target(model, kind, home, old)
        for site, (node, field) in _report_string_sites(original_root, model, original).items():
            expected = _report_string_rename(site[-1], node.value, kind, home, old, new, field, model,
                                             extra_qualified=ext_q, extra_owned=ext_owned,
                                             extra_extending=ext_ext,
                                             extra_tables=_extension_tables(project.mirror_report))
            if after_strings.get(site) != expected:
                raise PbzError("REFUSED", "a report string did not follow the rename; nothing renamed")
        for ref, site in original:
            k = "column" if ref.kind == "aggregation" else ref.kind
            target = (_fold(ref.table) == _fold(old)) if kind == "table" else (
                k == kind and _fold(ref.table) == _fold(home) and _fold(ref.name) == _fold(old))
            if not target:
                continue
            after_ref = following.get(site)
            table_name = new if kind == "table" else ref.table
            object_name = ref.name if kind == "table" else new
            if (after_ref is None or ("column" if after_ref.kind == "aggregation" else after_ref.kind) != k
                    or after_ref.table != table_name or after_ref.name != object_name):
                raise PbzError("REFUSED", "a report binding did not follow the rename; nothing renamed")


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
        parent = site[:-1]
        siblings[parent] = ref if parent not in siblings else _AMBIGUOUS
    sites = {}
    for site, node in _report_string_nodes(root):
        field = None
        for length in range(len(site) - 1, -1, -1):
            field = owners.get(site[:length])
            if field is not None:
                break
        if field is None:
            # Inherit the field of the containing element: ascend only within
            # the innermost array element on the path, and let the nearest
            # ancestor decide. siblings stores _AMBIGUOUS for a parent holding
            # several fields, so an ambiguous container still fails closed,
            # while a unique nearer field is not poisoned by a farther
            # ambiguous container and a sibling element's field is never
            # inherited.
            floor = 0
            for i, part in enumerate(site):
                if isinstance(part, int):
                    floor = i + 1
            for length in range(len(site) - 1, floor - 1, -1):
                field = siblings.get(site[:length])
                if field is not None:
                    break
        sites[site] = (node, field)
    return sites


_AMBIGUOUS = object()


def _canonical_target(model, kind, home, old):
    """The model-resolved (table, name) for a rename target, in canonical spelling."""
    if model is None:
        return home, old
    table = model.table(home)
    if table is None:
        return home, old
    if kind == "table":
        return table.name, old
    pool = table.columns if kind == "column" else table.measures
    for obj in pool.values():
        if _fold(obj.name) == _fold(old):
            return table.name, obj.name
    return table.name, old


def _wrapped_inner(value):
    """(wrapper, inner) when value is Name(...) with the final character closing it."""
    m = re.match(r"([A-Za-z_][\w]*)\(", value)
    if not m or not value.endswith(")"):
        return None
    return m.group(1), value[m.end():-1]


def _table_qualified(model, old):
    """(table, {qualified object paths}) for a table rename, or (None, empty set)."""
    if model is None:
        return None, set()
    table = model.table(old)
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
    pattern = rf"(?<![\w])(?:{'|'.join(re.escape(n) for n in sorted(names, key=len, reverse=True))})\."
    if re.search(pattern, _fold(rest)):
        return False
    for seg in rest.split("."):
        seg = seg.strip()
        if not seg or not all(ch.isalnum() or ch in "_ " for ch in seg):
            return False
        if any(tok in _WORD_OPERATORS for tok in _fold(seg).split()):
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


def _head_swap(value, table, new):
    """Replace the leading table segment of a dotted value with new, slicing
    at the value's own boundary dot: the written segment can differ in length
    from the canonical name when case folding changes code points."""
    for i, ch in enumerate(value):
        if ch == "." and _fold(value[:i]) == _fold(table):
            return new + value[i:]
    return new + value[len(table):]


def _other_table_owns(model, home, value, extra_owned=(), extra_extending=(),
                      extensions_any=False, extra_tables=()):
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
    low = _fold(value)
    tables = tuple(t.name for t in model.tables.values()) if model is not None else ()
    tables = tables + tuple(extra_tables)
    if low in {_fold(p) for p in extra_owned}:
        return True
    for p in extra_extending:
        pl = _fold(p)
        if low == pl:
            return True
        rest = _rest_after(value, pl)
        if rest is not None and _path_extension(rest, home, tables):
            return True
    if extensions_any:
        for p in extra_owned:
            rest = _rest_after(value, _fold(p))
            if rest is not None and _path_extension(rest, home, tables):
                return True
    if model is None:
        return False
    for t in model.tables.values():
        if _fold(t.name) == _fold(home):
            continue
        paths = {_fold(f"{t.name}.{o.name}")
                 for o in list(t.columns.values()) + list(t.measures.values())}
        for hname, levels in t.hierarchies.items():
            paths |= {_fold(f"{t.name}.{hname}.{lvl}") for lvl in levels}
        if low in paths:
            return True
        if extensions_any or _fold(t.name).startswith(_fold(home) + "."):
            for q in paths:
                rest = _rest_after(value, q)
                if rest is not None and _path_extension(rest, home, tables):
                    return True
    return False


_BLOCKED = object()


def _table_rewrite(value, old, new, qualified, table=None, model=None, other_owned=(),
                   other_extending=(), other_tables=()):
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

    out = _rewrite(value)
    if out is _BLOCKED:
        return value
    if out is not None:
        return out
    wrapped = _wrapped_inner(value)
    if wrapped:
        inner = _rewrite(wrapped[1])
        if inner is not None and inner is not _BLOCKED:
            return f"{wrapped[0]}({inner})"
    return value


def _single_wrapped(value):
    """(wrapper, inner) when value is exactly one Name(...) call, else None."""
    m = re.match(r"([A-Za-z_][\w]*)\(", value)
    if not m:
        return None
    depth = 0
    for i in range(m.end() - 1, len(value)):
        if value[i] == "(":
            depth += 1
        elif value[i] == ")":
            depth -= 1
            if depth == 0:
                if i != len(value) - 1:
                    return None
                return m.group(1), value[m.end():i]
    return None


def _extension_entities(report_dir):
    """(entity name, [measure names]) pairs from reportExtensions.json."""
    path = os.path.join(report_dir, "definition", "reportExtensions.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8-sig") as fh:
            root = pbir.parse_json(fh.read())
    except Exception:
        return []
    out = []
    ents = root.get("entities") if isinstance(root, pbir.JObj) else None
    if isinstance(ents, pbir.JArr):
        for ent in ents.items:
            if not isinstance(ent, pbir.JObj):
                continue
            nm = ent.get("name")
            if not isinstance(nm, pbir.JStr):
                continue
            measures = []
            ms = ent.get("measures")
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


def _report_string_rename(key, value, kind, home, old, new, field=None, model=None,
                          extra_qualified=(), extra_owned=(), extra_extending=(),
                          extra_tables=()):
    if kind == "table":
        if field is _AMBIGUOUS:
            return value
        if key in ("queryRef", "metadata", "queryName"):
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
                            return value
                        swapped = _head_swap(probe, field.table, new)
                        return f"{wrap_name}({swapped})" if wrap_name else swapped
                # A resolved binding owns the string even when its text does
                # not match the field's own path: never fall through to the
                # unbound rewrite and desync it from its field.
                return value
            _tbl, qualified = _table_qualified(model, old)
            if _tbl is not None:
                return _table_rewrite(value, old, new, qualified | set(extra_qualified), _tbl, model,
                                      other_owned=extra_owned, other_extending=extra_extending,
                                      other_tables=extra_tables)
            if _fold(value).startswith(_fold(old) + "."):
                return _head_swap(value, old, new)
            wrapped = _single_wrapped(value)
            _rest = _rest_after(wrapped[1], _fold(old)) if wrapped else None
            if wrapped and _rest is not None \
                    and _path_extension(_rest, old,
                                        ([t.name for t in model.tables.values()]
                                         if model is not None else []) + list(extra_tables)):
                return f"{wrapped[0]}({_head_swap(wrapped[1], old, new)})"
        return value
    if key == "queryName":
        return value
    if field is _AMBIGUOUS:
        return value
    if field is not None:
        field_kind = "column" if field.kind == "aggregation" else field.kind
        if field_kind != kind or _fold(field.table) != _fold(home) or _fold(field.name) != _fold(old):
            return value
        if _fold(value) in (_fold(f"{field.table}.{field.name}"), _fold(f"{home}.{old}")):
            return f"{home}.{new}"
        if key == "nativeQueryRef" and _fold(value) == _fold(field.name):
            return new
        if field_kind == "column":
            wrapped = re.fullmatch(r"([A-Za-z_][\w]*)\((.+)\)", value)
            if wrapped and _fold(wrapped.group(2)) in (
                    _fold(f"{field.table}.{field.name}"), _fold(f"{home}.{old}")):
                return f"{wrapped.group(1)}({home}.{new})"
        return value
    if key in ("queryRef", "metadata", "nativeQueryRef") and _fold(value) == _fold(f"{home}.{old}") \
            and not _other_table_owns(model, home, value, extra_owned, extra_extending,
                                      extra_tables=extra_tables):
        return f"{home}.{new}"
    if key == "nativeQueryRef" and _fold(value) == _fold(old) \
            and not _other_table_owns(model, home, value, extra_owned, extra_extending,
                                      extensions_any=True, extra_tables=extra_tables):
        return new
    if kind == "column" and key in ("queryRef", "metadata", "nativeQueryRef") \
            and _other_table_owns(model, home, value, extra_owned, extra_extending,
                                  extra_tables=extra_tables):
        return value
    if kind == "column" and key in ("queryRef", "metadata", "nativeQueryRef"):
        wrapped = re.fullmatch(r"([A-Za-z_][\w]*)\((.+)\)", value)
        if wrapped and _fold(wrapped.group(2)) == _fold(f"{home}.{old}") \
                and not _other_table_owns(model, home, wrapped.group(2), extra_owned, extra_extending,
                                          extra_tables=extra_tables):
            return f"{wrapped.group(1)}({home}.{new})"
    return value


def _binding_edits(bindings, kind, home, old, new, text):
    edits = []
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
    return _binding_edits(bindings, kind, home, old, new, text)


def pbir_ref_edits(root, model, kind: str, home: str, old: str, new: str,
                   extra_owned=(), extra_extending=(), extra_tables=()) -> list[tuple[int, int, str]]:
    """Edits renaming measure / column `old` of table `home` in one parsed PBIR document: every field
    reference's Property, and queryRef / nativeQueryRef / metadata strings."""
    edits = []
    want = "measure" if kind == "measure" else "column"
    fields = pbir.walk_fields_with_path(root, model)
    for ref, _site in fields:
        k = "column" if ref.kind == "aggregation" else ref.kind
        if k == want and _fold(ref.name) == _fold(old) and _fold(ref.table) == _fold(home) and ref.prop_node:
            edits.append((ref.prop_node.start, ref.prop_node.end, json.dumps(new, ensure_ascii=False)))
    home, old = _canonical_target(model, kind, home, old)
    for site, (node, field) in _report_string_sites(root, model, fields).items():
        nv = _report_string_rename(site[-1], node.value, kind, home, old, new, field, model,
                                   extra_owned=extra_owned, extra_extending=extra_extending,
                                   extra_tables=extra_tables)
        if nv != node.value:
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
    if _fold(new) != _fold(old) and any(_fold(t2.name) == _fold(new) for t2 in model.tables.values()):
        raise PbzError("NAME_TAKEN", new)
    if not new.strip() or new != new.strip() or any(c in new for c in "\r\n\t"):
        raise PbzError("BAD_REQUEST", "table names cannot be blank, padded or contain control characters")
    if any(_fold(n) == _fold(old) for n in translated_names(project)):
        raise PbzError("UNSUPPORTED", "cultures not in this mirror: run pbz pull")
    if any(_fold(e) == _fold(old) for e in model.expressions):
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
                if isinstance(names, list) and old in names:
                    edits.append((reg.start, reg.end, json.dumps([new if n == old else n for n in names],
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

        def visit(node, key=""):
            if isinstance(node, pbir.JObj):
                ents = node.get("entities") if key == "" else None
                for k, v in node.pairs:
                    if k.value == "Entity" and isinstance(v, pbir.JStr) and _fold(v.value) == low:
                        edits.append((v.start, v.end, json.dumps(new, ensure_ascii=False)))
                    visit(v, k.value)
                if isinstance(ents, pbir.JArr):
                    for ent in ents.items:
                        nm = ent.get("name") if isinstance(ent, pbir.JObj) else None
                        if isinstance(nm, pbir.JStr) and _fold(nm.value) == low:
                            edits.append((nm.start, nm.end, json.dumps(new, ensure_ascii=False)))
            elif isinstance(node, pbir.JArr):
                for v in node.items:
                    visit(v, key)

        visit(root)
        _tbl, _qualified = _table_qualified(model, old)
        if _tbl is not None:
            ext_q = _extension_qualified(project.mirror_report, old)
            ext_owned = _extension_owned_others(project.mirror_report, old)
            for site, (node, field) in _report_string_sites(root, model).items():
                if site[-1] in ("queryRef", "metadata", "queryName"):
                    nv = _report_string_rename(site[-1], node.value, "table", "", old, new,
                                               field, model, extra_qualified=ext_q,
                                               extra_owned=ext_owned,
                                               extra_extending=_extension_owned(project.mirror_report, old),
                                               extra_tables=_extension_tables(project.mirror_report))
                    if nv != node.value:
                        edits.append((node.start, node.end, json.dumps(nv, ensure_ascii=False)))
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
