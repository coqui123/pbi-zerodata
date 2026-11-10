"""`pbz culture | translate | perspective | role`: edit translations, perspectives and security roles in
the mirror by object name (then diff / validate / push). Role members and translated strings that look
like personal names are tokenized again on the next pull. Output: names and counts only."""
from __future__ import annotations

import json
import os
import re

from . import deps, lexers, tmdl
from .common import PbzError, commit_bytes, read_text, write_text
from .tokens import TOKEN_RE, apply_edits

CODE_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
REF = (re.compile(r"^\s*'((?:[^']|'')+)'\s*\[(.+)\]\s*$"), re.compile(r"^\s*([^\[\]']+?)\s*\[(.+)\]\s*$"))


def _q(name: str) -> str:
    return lexers.tmdl_name(name)


def _file_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def _model(project):
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    return mdef, tmdl.load_model(mdef)


def _rel(project, rel: str) -> str:
    return f"{project.name}.SemanticModel/definition/{rel}"


def _write_op(mdef: str, rel: str, text: str) -> tuple:
    path = os.path.join(mdef, *rel.split("/"))
    bom = read_text(path)[1] if os.path.exists(path) else b""
    return ("write", path, text, bom)


def _commit(ops) -> None:
    ops = [op for op in ops if op]
    if ops:
        commit_bytes(ops)


def _commit_texts(mdef: str, planned: dict) -> None:
    _commit([_write_op(mdef, rel, text) for rel, text in planned.items()])


def _require_model_tmdl(mdef: str) -> None:
    if not os.path.isfile(os.path.join(mdef, "model.tmdl")):
        raise PbzError("NO_MIRROR", "model.tmdl is missing; run `pbz pull` first")


def _destination(mdef: str, folder: str, name: str) -> str:
    filename = _file_name(name)
    if not filename or filename in (".", ".."):
        raise PbzError("BAD_REQUEST", "that name is not a file name")
    rel = f"{folder}/{filename}.tmdl"
    if os.path.exists(os.path.join(mdef, *rel.split("/"))):
        raise PbzError("NAME_TAKEN", "that file name is already used")
    return rel


def _write(mdef: str, rel: str, text: str) -> None:
    path = os.path.join(mdef, *rel.split("/"))
    bom = read_text(path)[1] if os.path.exists(path) else b""
    write_text(path, text, bom)


def _eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _ref_line(mdef: str, kind: str, name: str, add: bool):
    """Plan `ref <kind> <name>` in model.tmdl. Does not write. None when the line is already right."""
    path = os.path.join(mdef, "model.tmdl")
    if not os.path.isfile(path):
        raise PbzError("NO_MIRROR", "model.tmdl is missing; run `pbz pull` first")
    text, bom = read_text(path)
    eol = _eol(text)
    pat = re.compile(rf"(?m)^ref {kind} ('(?:[^']|'')*'|\S+)[ \t]*(?:\r?\n|$)")
    hits = [m for m in pat.finditer(text) if tmdl.unquote(m.group(1)).lower() == name.lower()]
    if add and not hits:
        refs = list(re.finditer(r"(?m)^ref \w+ [^\r\n]*", text))
        line = f"ref {kind} {_q(name)}"
        new = text[:refs[-1].end()] + eol + line + text[refs[-1].end():] if refs else text.rstrip("\r\n") + eol + eol + line + eol
        return ("write", path, new, bom)
    if not add and hits:
        return ("write", path, apply_edits(text, [(m.start(), m.end(), "") for m in hits]), bom)
    return None


def culture_docs(mdef: str) -> dict:
    """{rel: TmdlDoc} of the mirror's culture files (load_model skips them)."""
    out = {}
    cdir = os.path.join(mdef, "cultures")
    for f in sorted(os.listdir(cdir)) if os.path.isdir(cdir) else []:
        if f.lower().endswith(".tmdl"):
            out[f"cultures/{f}"] = tmdl.parse(f"cultures/{f}", read_text(os.path.join(cdir, f))[0])
    return out


def _target(model, ref: str):
    """('table'|'column'|'measure'|'hierarchy', table, name) for a user reference."""
    for rx in REF:
        m = rx.match(ref)
        if m:
            t, n = m.group(1).replace("''", "'"), m.group(2)
            tab = model.table(t)
            if tab is None:
                raise PbzError("NOT_FOUND", f"table {t}")
            c = model.column(tab.name, n)
            if c:
                return "column", tab.name, c.name
            if n in tab.measures or any(x.lower() == n.lower() for x in tab.measures):
                return "measure", tab.name, next(x for x in tab.measures if x.lower() == n.lower())
            if any(h.lower() == n.lower() for h in tab.hierarchies):
                return "hierarchy", tab.name, next(h for h in tab.hierarchies if h.lower() == n.lower())
            raise PbzError("NOT_FOUND", ref)
    m = re.match(r"^\s*\[(.+)\]\s*$", ref)
    if m:
        mm = model.measure(m.group(1))
        if mm is None:
            raise PbzError("NOT_FOUND", ref)
        return "measure", mm.table, mm.name
    tab = model.table(ref.strip().strip("'"))
    if tab is None:
        raise PbzError("NOT_FOUND", ref)
    return "table", "", tab.name


def _child(node, kind: str, name: str):
    return next((c for c in node.children if c.kind == kind and c.name.lower() == name.lower()), None)


def _append_child(doc_text: str, doc, parent, line: str) -> str:
    """Insert `line` (already indented) as the last child of parent."""
    s, e = tmdl.block_span(doc, parent)
    eol = _eol(doc_text)
    block = doc_text[s:e]
    body = block.rstrip("\r\n")
    insert_at = s + len(body)
    return doc_text[:insert_at] + eol + line + doc_text[insert_at:] if doc_text[insert_at:] else \
        doc_text[:insert_at] + eol + line + eol


# ---------------------------------------------------------------- cultures


def cultures_mirrored(project) -> bool:
    """True when every real culture file is in the mirror (older mirrors skipped cultures)."""
    real = os.path.join(project.model_dir, "definition", "cultures")
    mine = os.path.join(project.mirror_model, "definition", "cultures")
    have = {f.lower() for f in os.listdir(mine)} if os.path.isdir(mine) else set()
    return all(f.lower() in have for f in (os.listdir(real) if os.path.isdir(real) else []) if f.lower().endswith(".tmdl"))


def _culture_objects(doc, kind: str, table: str, name: str):
    """Translation nodes of one object across a culture doc."""
    out = []
    for n in doc.nodes:
        if kind == "table" and n.kind == "table" and n.parent is not None and n.parent.kind == "model" \
                and n.name.lower() == name.lower():
            out.append(n)
        elif n.kind == kind and n.parent is not None and n.parent.kind == "table" and \
                n.parent.name.lower() == table.lower() and n.name.lower() == name.lower():
            out.append(n)
    return out


def culture_rename_texts(mdef: str, kind: str, table: str, old: str, new: str) -> dict[str, str]:
    """{rel: new text} renaming translation entries. Does not write."""
    out = {}
    for rel, doc in culture_docs(mdef).items():
        edits = []
        lines = doc.text.splitlines(keepends=True)
        for n in _culture_objects(doc, kind, table, old):
            off = sum(len(x) for x in lines[:n.line])
            m = re.match(rf"^(\t*{n.kind}\s+)('(?:[^']|'')*'|[^\s=]+)", lines[n.line])
            if m:
                edits.append((off + m.start(2), off + m.end(2), _q(new)))
        if edits:
            out[rel] = apply_edits(doc.text, edits)
    return out


def culture_edits_rename(mdef: str, kind: str, table: str, old: str, new: str) -> list[str]:
    """Rename an object's translation entries in every mirrored culture file. Returns changed rels."""
    planned = culture_rename_texts(mdef, kind, table, old, new)
    _commit_texts(mdef, planned)
    return list(planned)


def culture_edits_drop(mdef: str, objects: list[tuple]) -> dict[str, str]:
    """{rel: new text} with the translation blocks of removed (kind, table, name) objects cut."""
    out = {}
    for rel, doc in culture_docs(mdef).items():
        cuts = []
        for kind, table, name in objects:
            for n in _culture_objects(doc, kind, table, name):
                cuts.append(tmdl.block_span(doc, n) + ("",))
        if cuts:
            out[rel] = apply_edits(doc.text, sorted(set(cuts)))
    return out


def culture_move_texts(mdef: str, src: str, dst: str, measure: str) -> dict[str, str]:
    """{rel: new text} moving every translation hit of a measure. Does not write."""
    out = {}
    for rel, doc in culture_docs(mdef).items():
        hits = _culture_objects(doc, "measure", src, measure)
        if not hits:
            continue
        text = doc.text
        moved = False
        for _ in range(len(hits)):
            cur = tmdl.parse(rel, text) if moved else doc
            left = _culture_objects(cur, "measure", src, measure)
            if not left:
                break
            s, e = tmdl.block_span(cur, left[0])
            block = text[s:e].rstrip("\r\n")
            text = apply_edits(text, [(s, e, "")])
            doc2 = tmdl.parse(rel, text)
            model_node = left[0].parent.parent
            mnode = next((n for n in doc2.nodes if n.kind == "model" and n.name == model_node.name), None)
            tnode = _child(mnode, "table", dst) if mnode is not None else None
            if tnode is None and mnode is not None:
                text = _append_child(text, doc2, mnode, f"{chr(9) * 3}table {_q(dst)}")
                doc2 = tmdl.parse(rel, text)
                mnode = next(n for n in doc2.nodes if n.kind == "model" and n.name == model_node.name)
                tnode = _child(mnode, "table", dst)
            if tnode is None:
                break
            text = _append_child(text, doc2, tnode, block)
            moved = True
        if moved:
            out[rel] = text
    return out


def culture_edits_move(mdef: str, src: str, dst: str, measure: str) -> list[str]:
    """Move a measure's translations from table src to table dst in every culture file."""
    planned = culture_move_texts(mdef, src, dst, measure)
    _commit_texts(mdef, planned)
    return list(planned)


def perspective_move_texts(mdef: str, src: str, dst: str, measure: str, texts: dict | None = None) -> dict[str, str]:
    """{rel: new text} moving perspective membership. `texts` overrides disk (planned TMDL). Does not write."""
    if texts is None:
        pairs = [(r, d) for r, d in tmdl.load_model(mdef).docs.items() if r.startswith("perspectives/")]
    else:
        pairs = [(r, tmdl.parse(r, t)) for r, t in texts.items() if r.startswith("perspectives/")]
    out = {}
    for rel, doc in pairs:
        text = doc.text
        moved = False
        for p in (n for n in doc.nodes if n.kind == "perspective"):
            pt = _child(p, "perspectiveTable", src)
            pm = _child(pt, "perspectiveMeasure", measure) if pt is not None else None
            if pm is None:
                continue
            text = apply_edits(text, [tmdl.block_span(doc, pm) + ("",)])
            d2 = tmdl.parse(rel, text)
            p2 = next(n for n in d2.nodes if n.kind == "perspective" and n.name == p.name)
            if _child(p2, "perspectiveTable", dst) is None:
                text = _append_child(text, d2, p2, f"\tperspectiveTable {_q(dst)}")
                d2 = tmdl.parse(rel, text)
                p2 = next(n for n in d2.nodes if n.kind == "perspective" and n.name == p.name)
            text = _append_child(text, d2, _child(p2, "perspectiveTable", dst), f"\t\tperspectiveMeasure {_q(measure)}")
            moved = True
        if moved:
            out[rel] = text
    return out


def perspective_edits_move(mdef: str, src: str, dst: str, measure: str) -> list[str]:
    """Move a measure's perspective membership from perspectiveTable src to dst (created if missing)."""
    planned = perspective_move_texts(mdef, src, dst, measure)
    _commit_texts(mdef, planned)
    return list(planned)


def culture_add(ws, project, code: str) -> dict:
    if not CODE_RE.match(code):
        raise PbzError("BAD_REQUEST", "culture code like de-DE or fr-FR")
    mdef = os.path.join(project.mirror_model, "definition")
    _require_model_tmdl(mdef)
    mdef, model = _model(project)
    rel = f"cultures/{code}.tmdl"
    if os.path.exists(os.path.join(mdef, *rel.split("/"))):
        raise PbzError("NAME_TAKEN", code)
    mname = model.model_node.name if model.model_node is not None else "Model"
    _commit([_write_op(mdef, rel, f"cultureInfo {code}\n\n\ttranslations\n\n\t\tmodel {_q(mname)}\n"),
             _ref_line(mdef, "cultureInfo", code, True)])
    return {"verb": "culture", "action": "added", "culture": code, "files": [_rel(project, rel), _rel(project, "model.tmdl")],
            "next": f"pbz translate {code} <object> --caption ..., pbz culture missing {code}"}


def culture_remove(ws, project, code: str) -> dict:
    mdef = os.path.join(project.mirror_model, "definition")
    _require_model_tmdl(mdef)
    mdef, _model_ = _model(project)
    rel = f"cultures/{code}.tmdl"
    path = os.path.join(mdef, *rel.split("/"))
    if not os.path.exists(path):
        raise PbzError("NOT_FOUND", f"culture {code}")
    ref = _ref_line(mdef, "cultureInfo", code, False)
    files = [_rel(project, rel)]
    ops = [("delete", path)]
    if ref:
        ops.append(ref)
        files.append(_rel(project, "model.tmdl"))
    _commit(ops)
    return {"verb": "culture", "action": "removed", "culture": code, "files": files, "next": "pbz diff, pbz push"}


def _culture(mdef: str, code: str):
    rel = f"cultures/{code}.tmdl"
    path = os.path.join(mdef, *rel.split("/"))
    if not os.path.exists(path):
        raise PbzError("NOT_FOUND", f"culture {code} (pbz culture add {code})")
    return rel, path


def translate(ws, project, code: str, target: str, caption: str | None = None, description: str | None = None,
              folder: str | None = None) -> dict:
    if caption is None and description is None and folder is None:
        raise PbzError("BAD_REQUEST", "give --caption, --description or --folder")
    for s in (caption, description, folder):
        if s is not None and (TOKEN_RE.search(s) or "\n" in s or "\r" in s):
            raise PbzError("BAD_REQUEST", "translations are single-line plain text")
    mdef, model = _model(project)
    rel, path = _culture(mdef, code)
    kind, table, name = _target(model, target)
    mname = model.model_node.name if model.model_node is not None else "Model"
    text = read_text(path)[0]

    def parse():
        return tmdl.parse(rel, text)

    doc = parse()
    root = next((n for n in doc.nodes if n.kind == "cultureInfo"), None)
    if root is None:
        raise PbzError("BAD_REQUEST", f"culture {code} has no cultureInfo declaration")
    tr = _child(root, "translations", "")
    if tr is None:
        text = _append_child(text, doc, root, f"\ttranslations")
        doc = parse()
        root = next(n for n in doc.nodes if n.kind == "cultureInfo")
        tr = _child(root, "translations", "")
    mnode = _child(tr, "model", mname)
    if mnode is None:
        text = _append_child(text, doc, tr, f"\t\tmodel {_q(mname)}")
        doc = parse()
        tr = _child(next(n for n in doc.nodes if n.kind == "cultureInfo"), "translations", "")
        mnode = _child(tr, "model", mname)
    tname = name if kind == "table" else table
    tnode = _child(mnode, "table", tname)
    if tnode is None:
        text = _append_child(text, doc, mnode, f"\t\t\ttable {_q(tname)}")
        doc = parse()
        mnode = _child(_child(next(n for n in doc.nodes if n.kind == "cultureInfo"), "translations", ""), "model", mname)
        tnode = _child(mnode, "table", tname)
    obj = tnode
    if kind != "table":
        obj = _child(tnode, kind, name)
        if obj is None:
            text = _append_child(text, doc, tnode, f"\t\t\t\t{kind} {_q(name)}")
            doc = parse()
            mnode = _child(_child(next(n for n in doc.nodes if n.kind == "cultureInfo"), "translations", ""), "model", mname)
            obj = _child(_child(mnode, "table", tname), kind, name)
    edits = []
    for prop, val in (("caption", caption), ("description", description), ("displayFolder", folder)):
        if val is not None:
            edits += tmdl.prop_edit(doc, obj, prop, val)
    text = apply_edits(text, sorted(edits)) if edits else text
    _write(mdef, rel, text)
    label = name if kind == "table" else (f"{table}[{name}]" if kind == "column" else f"{table}.{name}" if kind == "hierarchy" else f"[{name}]")
    return {"verb": "translate", "culture": code, "object": label, "kind": kind,
            "set": [p for p, v in (("caption", caption), ("description", description), ("displayFolder", folder)) if v is not None],
            "files": [_rel(project, rel)], "next": "pbz diff, pbz validate, pbz push"}


_TRANSLATION_PROPS = ("caption", "description", "displayFolder")


def _local_json(path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise PbzError("BAD_REQUEST", "a local translations file is required")
    if re.match(r"(?i)^[a-z][a-z0-9+.-]*://", path.strip()):
        raise PbzError("REFUSED", "translations are a local file")
    if os.path.lexists(path) and not os.path.isfile(path):
        raise PbzError("REFUSED", "translations file must be a regular file")
    return path


def _translation_rows(mdef: str) -> list[dict]:
    rows = []
    for rel, doc in culture_docs(mdef).items():
        code = rel.split("/")[-1][:-5]
        for node in doc.nodes:
            if node.kind == "linguisticMetadata":
                continue
            if node.kind == "table" and node.parent is not None and node.parent.kind == "model":
                kind, table, obj = "table", node.name, node.name
            elif node.kind in ("column", "measure", "hierarchy") and node.parent is not None and node.parent.kind == "table":
                kind, table, obj = node.kind, node.parent.name, node.name
            else:
                continue
            for prop in _TRANSLATION_PROPS:
                if prop not in node.props:
                    continue
                value = node.props[prop].strip()
                if value:
                    rows.append({"culture": code, "objectType": kind, "table": table, "object": obj,
                                 "property": prop, "value": value})
    rows.sort(key=lambda r: (r["culture"].lower(), r["objectType"], r["table"].lower(), r["object"].lower(), r["property"]))
    return rows


def translation_export(ws, project, path: str) -> dict:
    """Flat JSON of culture captions. Counts only come back; the file holds the strings."""
    path = _local_json(path)
    mdef, _model_ = _model(project)
    rows = _translation_rows(mdef)
    write_text(path, json.dumps({"version": 1, "translations": rows}, indent=1, ensure_ascii=False) + "\n")
    return {"verb": "translate", "action": "export", "cultures": len({r["culture"] for r in rows}),
            "translations": len(rows)}


def _resolve_translation(model, kind: str, table: str, obj: str):
    if kind == "table":
        hit = model.table(obj or table)
        if hit is None:
            raise PbzError("NOT_FOUND", "table")
        return "table", "", hit.name
    host = model.table(table)
    if host is None:
        raise PbzError("NOT_FOUND", "table")
    if kind == "column":
        col = model.column(host.name, obj)
        if col is None:
            raise PbzError("NOT_FOUND", "column")
        return "column", host.name, col.name
    if kind == "measure":
        measure = next((item for item_name, item in host.measures.items() if item_name.lower() == obj.lower()), None)
        if measure is None:
            raise PbzError("NOT_FOUND", "measure")
        return "measure", host.name, measure.name
    if kind == "hierarchy":
        hier = next((item for item in host.hierarchies if item.lower() == obj.lower()), None)
        if hier is None:
            raise PbzError("NOT_FOUND", "hierarchy")
        return "hierarchy", host.name, hier
    raise PbzError("BAD_REQUEST", "objectType is table, column, measure or hierarchy")


def _apply_one(text: str, rel: str, model, code: str, kind: str, table: str, name: str, prop: str, value: str) -> str:
    """Same culture-tree edits as translate(), on one in-memory document."""
    mname = model.model_node.name if model.model_node is not None else "Model"

    def parse():
        return tmdl.parse(rel, text)

    doc = parse()
    root = next((n for n in doc.nodes if n.kind == "cultureInfo"), None)
    if root is None:
        raise PbzError("BAD_REQUEST", f"culture {code} has no cultureInfo declaration")
    tr = _child(root, "translations", "")
    if tr is None:
        text = _append_child(text, doc, root, "\ttranslations")
        doc = parse()
        root = next(n for n in doc.nodes if n.kind == "cultureInfo")
        tr = _child(root, "translations", "")
    mnode = _child(tr, "model", mname)
    if mnode is None:
        text = _append_child(text, doc, tr, f"\t\tmodel {_q(mname)}")
        doc = parse()
        tr = _child(next(n for n in doc.nodes if n.kind == "cultureInfo"), "translations", "")
        mnode = _child(tr, "model", mname)
    tname = name if kind == "table" else table
    tnode = _child(mnode, "table", tname)
    if tnode is None:
        text = _append_child(text, doc, mnode, f"\t\t\ttable {_q(tname)}")
        doc = parse()
        mnode = _child(_child(next(n for n in doc.nodes if n.kind == "cultureInfo"), "translations", ""), "model", mname)
        tnode = _child(mnode, "table", tname)
    obj = tnode
    if kind != "table":
        obj = _child(tnode, kind, name)
        if obj is None:
            text = _append_child(text, doc, tnode, f"\t\t\t\t{kind} {_q(name)}")
            doc = parse()
            mnode = _child(_child(next(n for n in doc.nodes if n.kind == "cultureInfo"), "translations", ""), "model", mname)
            obj = _child(_child(mnode, "table", tname), kind, name)
    edits = tmdl.prop_edit(doc, obj, prop, value)
    return apply_edits(text, sorted(edits)) if edits else text


def _add_ref_text(text: str, kind: str, name: str) -> str:
    eol = _eol(text)
    pat = re.compile(rf"(?m)^ref {kind} ('(?:[^']|'')*'|\S+)[ \t]*(?:\r?\n|$)")
    if any(tmdl.unquote(m.group(1)).lower() == name.lower() for m in pat.finditer(text)):
        return text
    refs = list(re.finditer(r"(?m)^ref \w+ [^\r\n]*", text))
    line = f"ref {kind} {_q(name)}"
    if refs:
        return text[:refs[-1].end()] + eol + line + text[refs[-1].end():]
    return text.rstrip("\r\n") + eol + eol + line + eol


def translation_import(ws, project, path: str) -> dict:
    """Write cultures/{code}.tmdl from flat JSON. Token-like values are refused. Counts only."""
    path = _local_json(path)
    try:
        data = json.loads(open(path, encoding="utf-8-sig").read())
    except (OSError, UnicodeError, ValueError) as err:
        raise PbzError("BAD_REQUEST", "translations file could not be read") from err
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("translations"), list):
        raise PbzError("BAD_REQUEST", "translations file needs version 1 and a translations array")
    mdef, model = _model(project)
    planned = []
    for row in data["translations"]:
        if not isinstance(row, dict):
            raise PbzError("BAD_REQUEST", "each translation is an object")
        code, kind, table, obj = row.get("culture"), row.get("objectType"), row.get("table"), row.get("object")
        prop, value = row.get("property"), row.get("value")
        if not isinstance(code, str) or not CODE_RE.match(code):
            raise PbzError("BAD_REQUEST", "culture code like de-DE or fr-FR")
        if kind not in ("table", "column", "measure", "hierarchy"):
            raise PbzError("BAD_REQUEST", "objectType is table, column, measure or hierarchy")
        if prop not in _TRANSLATION_PROPS:
            raise PbzError("BAD_REQUEST", "property is caption, description or displayFolder")
        if not isinstance(table, str) or not isinstance(obj, str) or not isinstance(value, str):
            raise PbzError("BAD_REQUEST", "translation fields must be strings")
        if TOKEN_RE.search(value) or "\n" in value or "\r" in value:
            raise PbzError("BAD_REQUEST", "translations are single-line plain text")
        planned.append((code, *_resolve_translation(model, kind, table, obj), prop, value))
    texts: dict[str, str] = {}
    cultures = {code for code, *_rest in planned}
    mname = model.model_node.name if model.model_node is not None else "Model"
    for code in sorted(cultures):
        rel = f"cultures/{code}.tmdl"
        full = os.path.join(mdef, *rel.split("/"))
        if os.path.exists(full):
            texts[rel] = read_text(full)[0]
        else:
            texts[rel] = f"cultureInfo {code}\n\n\ttranslations\n\n\t\tmodel {_q(mname)}\n"
    for code, kind, table, name, prop, value in planned:
        rel = f"cultures/{code}.tmdl"
        texts[rel] = _apply_one(texts[rel], rel, model, code, kind, table, name, prop, value)
    model_path = os.path.join(mdef, "model.tmdl")
    model_text, model_bom = read_text(model_path)
    new_model = model_text
    for code in sorted(cultures):
        new_model = _add_ref_text(new_model, "cultureInfo", code)
    ops = [_write_op(mdef, rel, text) for rel, text in texts.items()]
    if new_model != model_text:
        ops.append(("write", model_path, new_model, model_bom))
    _commit(ops)
    return {"verb": "translate", "action": "import", "cultures": len(cultures),
            "translations": len(planned), "files": len(ops)}


def _translated(doc) -> set[tuple]:
    out = set()
    for n in doc.nodes:
        if "caption" not in n.props:
            continue
        if n.kind == "table" and n.parent is not None and n.parent.kind == "model":
            out.add(("table", "", n.name.lower()))
        elif n.kind in ("column", "measure", "hierarchy") and n.parent is not None and n.parent.kind == "table":
            out.add((n.kind, n.parent.name.lower(), n.name.lower()))
    return out


def culture_list(ws, project, code: str | None = None) -> dict:
    mdef, model = _model(project)
    out = []
    for rel, doc in culture_docs(mdef).items():
        c = rel.split("/")[-1][:-5]
        if code and c.lower() != code.lower():
            continue
        out.append({"culture": c, "captions": len(_translated(doc)),
                    "linguistic_schema": any(n.kind == "linguisticMetadata" for n in doc.nodes)})
    if code and not out:
        raise PbzError("NOT_FOUND", f"culture {code}")
    return {"verb": "culture", "action": "list", "cultures": out}


def culture_missing(ws, project, code: str) -> dict:
    mdef, model = _model(project)
    rel, path = _culture(mdef, code)
    done = _translated(tmdl.parse(rel, read_text(path)[0]))
    missing = []
    for t in model.tables.values():
        if t.private or t.hidden:
            continue
        if ("table", "", t.name.lower()) not in done:
            missing.append(t.name)
        missing += [f"{t.name}[{c.name}]" for c in t.columns.values()
                    if not c.hidden and ("column", t.name.lower(), c.name.lower()) not in done]
        missing += [f"[{m.name}]" for m in t.measures.values()
                    if not (m.node and m.node.flag("isHidden")) and ("measure", t.name.lower(), m.name.lower()) not in done]
        missing += [f"{t.name}.{h}" for h in t.hierarchies if ("hierarchy", t.name.lower(), h.lower()) not in done]
    return {"verb": "culture", "action": "missing", "culture": code, "count": len(missing), "objects": missing[:300]}


# ---------------------------------------------------------------- perspectives


def _perspective(mdef: str, name: str):
    for rel, doc in ((r, d) for r, d in tmdl.load_model(mdef).docs.items() if r.startswith("perspectives/")):
        node = next((n for n in doc.nodes if n.kind == "perspective" and n.name.lower() == name.lower()), None)
        if node is not None:
            return rel, doc, node
    raise PbzError("NOT_FOUND", f"perspective {name}")


def perspective_add(ws, project, name: str) -> dict:
    mdef = os.path.join(project.mirror_model, "definition")
    _require_model_tmdl(mdef)
    mdef, model = _model(project)
    if TOKEN_RE.search(name) or not name.strip():
        raise PbzError("BAD_REQUEST", "a perspective needs a plain name")
    if any(p.lower() == name.lower() for p in model.perspectives):
        raise PbzError("NAME_TAKEN", name)
    rel = _destination(mdef, "perspectives", name)
    _commit([_write_op(mdef, rel, f"perspective {_q(name)}\n"), _ref_line(mdef, "perspective", name, True)])
    return {"verb": "perspective", "action": "added", "perspective": name,
            "files": [_rel(project, rel), _rel(project, "model.tmdl")],
            "next": f"pbz perspective include \"{name}\" <objects...>"}


def perspective_remove(ws, project, name: str) -> dict:
    mdef = os.path.join(project.mirror_model, "definition")
    _require_model_tmdl(mdef)
    mdef, _m = _model(project)
    rel, _doc, node = _perspective(mdef, name)
    ref = _ref_line(mdef, "perspective", node.name, False)
    files = [_rel(project, rel)]
    ops = [("delete", os.path.join(mdef, *rel.split("/")))]
    if ref:
        ops.append(ref)
        files.append(_rel(project, "model.tmdl"))
    _commit(ops)
    return {"verb": "perspective", "action": "removed", "perspective": node.name, "files": files,
            "next": "pbz diff, pbz push"}


PKIND = {"column": "perspectiveColumn", "measure": "perspectiveMeasure", "hierarchy": "perspectiveHierarchy"}


def perspective_edit(ws, project, name: str, targets: list[str], include: bool) -> dict:
    mdef, model = _model(project)
    if not targets:
        raise PbzError("BAD_REQUEST", "list the objects: Table, 'T'[Column], [Measure] or T.Hierarchy")
    rel, doc, pnode = _perspective(mdef, name)
    text = doc.text
    changed = []
    for ref in targets:
        if re.match(r"^[^\[\]]+\.[^\[\]]+$", ref) and not model.table(ref):
            t, h = ref.split(".", 1)
            kind, table, obj = ("hierarchy", model.table(t).name, h) if model.table(t) and h in model.table(t).hierarchies \
                else _target(model, ref)
        else:
            kind, table, obj = _target(model, ref)
        doc = tmdl.parse(rel, text)
        pnode = next(n for n in doc.nodes if n.kind == "perspective" and n.name.lower() == name.lower())
        tname = obj if kind == "table" else table
        pt = _child(pnode, "perspectiveTable", tname)
        if include:
            if pt is None:
                text = _append_child(text, doc, pnode, f"\tperspectiveTable {_q(tname)}")
                doc = tmdl.parse(rel, text)
                pnode = next(n for n in doc.nodes if n.kind == "perspective" and n.name.lower() == name.lower())
                pt = _child(pnode, "perspectiveTable", tname)
            members = []
            t = model.table(tname)
            if kind == "table":
                members = [("column", c) for c in t.columns if not t.columns[c].hidden] + \
                          [("measure", m) for m in t.measures] + [("hierarchy", h) for h in t.hierarchies]
            else:
                members = [(kind, obj)]
            for k, n in members:
                doc = tmdl.parse(rel, text)
                pnode = next(x for x in doc.nodes if x.kind == "perspective" and x.name.lower() == name.lower())
                pt = _child(pnode, "perspectiveTable", tname)
                if _child(pt, PKIND[k], n) is None:
                    text = _append_child(text, doc, pt, f"\t\t{PKIND[k]} {_q(n)}")
                    changed.append(f"+ {k} {tname}.{n}")
        else:
            if pt is None:
                continue
            if kind == "table":
                text = apply_edits(text, [tmdl.block_span(doc, pt) + ("",)])
                changed.append(f"- table {tname}")
            else:
                ch = _child(pt, PKIND[kind], obj)
                if ch is not None:
                    text = apply_edits(text, [tmdl.block_span(doc, ch) + ("",)])
                    changed.append(f"- {kind} {tname}.{obj}")
    if text != read_text(os.path.join(mdef, *rel.split("/")))[0]:
        _write(mdef, rel, text)
    return {"verb": "perspective", "action": "include" if include else "exclude", "perspective": name,
            "changes": changed, "files": [_rel(project, rel)] if changed else [], "next": "pbz diff, pbz push"}


# ---------------------------------------------------------------- roles


def _role(mdef: str, model, name: str):
    node = next((n for k, n in model.role_nodes.items() if k.lower() == name.lower()), None)
    if node is None:
        raise PbzError("NOT_FOUND", f"role {name}")
    return node.file, model.docs[node.file], node


def role_add(ws, project, name: str, permission: str = "read") -> dict:
    mdef = os.path.join(project.mirror_model, "definition")
    _require_model_tmdl(mdef)
    mdef, model = _model(project)
    if permission not in ("read", "readRefresh", "refresh", "administrator", "none"):
        raise PbzError("BAD_REQUEST", "permission: read, readRefresh, refresh, administrator or none")
    if TOKEN_RE.search(name) or not name.strip():
        raise PbzError("BAD_REQUEST", "a role needs a plain name")
    if any(r.lower() == name.lower() for r in model.role_nodes):
        raise PbzError("NAME_TAKEN", name)
    rel = _destination(mdef, "roles", name)
    _commit([_write_op(mdef, rel, f"role {_q(name)}\n\tmodelPermission: {permission}\n"),
             _ref_line(mdef, "role", name, True)])
    return {"verb": "role", "action": "added", "role": name,
            "files": [_rel(project, rel), _rel(project, "model.tmdl")],
            "next": f"pbz role filter \"{name}\" <table> \"<DAX>\", then pbz push; members are usually assigned in the service"}


def role_remove(ws, project, name: str) -> dict:
    mdef = os.path.join(project.mirror_model, "definition")
    _require_model_tmdl(mdef)
    mdef, model = _model(project)
    rel, _doc, node = _role(mdef, model, name)
    ref = _ref_line(mdef, "role", node.name, False)
    files = [_rel(project, rel)]
    ops = [("delete", os.path.join(mdef, *rel.split("/")))]
    if ref:
        ops.append(ref)
        files.append(_rel(project, "model.tmdl"))
    _commit(ops)
    return {"verb": "role", "action": "removed", "role": node.name, "files": files, "next": "pbz diff, pbz push"}


def role_filter(ws, project, name: str, table: str, dax: str | None) -> dict:
    mdef, model = _model(project)
    rel, doc, node = _role(mdef, model, name)
    t = model.table(table)
    if t is None:
        raise PbzError("NOT_FOUND", f"table {table}")
    tp = next((c for c in node.children if c.kind == "tablePermission" and c.name.lower() == t.name.lower()), None)
    text = doc.text
    eol = _eol(text)
    if dax is None:  # unfilter
        if tp is None:
            raise PbzError("NOT_FOUND", f"no filter on {t.name} in role {node.name}")
        text = apply_edits(text, [tmdl.block_span(doc, tp) + ("",)])
        _write(mdef, rel, text)
        return {"verb": "role", "action": "unfiltered", "role": node.name, "table": t.name, "next": "pbz diff, pbz push"}
    d = deps.dax(dax, t.name, deps.Resolver(model), row_home=True)
    if d.unresolved:
        raise PbzError("UNKNOWN_REF", ", ".join(sorted({f"{u.table}[{u.name}]" if u.table else f"[{u.name}]" for u in d.unresolved})))
    body = dax.strip()
    if "\n" in body:
        value = eol + eol.join("\t\t\t" + ln.strip() for ln in body.splitlines() if ln.strip())
    else:
        value = " " + body
    line = f"\ttablePermission {_q(t.name)} ={value}"
    if tp is not None:
        text = apply_edits(text, [(tmdl.block_span(doc, tp)[0], tmdl.block_span(doc, tp)[1], line + eol + eol)])
    else:
        text = text.rstrip("\r\n") + eol + eol + line + eol
    _write(mdef, rel, text)
    return {"verb": "role", "action": "filtered", "role": node.name, "table": t.name,
            "references": sorted({f"{r.table}[{r.name}]" if r.kind == "column" else f"[{r.name}]" for r in d.refs
                                  if r.kind in ("column", "measure")}),
            "next": "pbz diff, pbz push; test with View as role in Desktop (the user)"}


def role_member(ws, project, name: str, action: str, member: str) -> dict:
    mdef, model = _model(project)
    rel, doc, node = _role(mdef, model, name)
    text = doc.text
    eol = _eol(text)
    mem = [c for c in node.children if c.kind == "member"]
    if action == "add":
        if any(c.name.lower() == member.lower() for c in mem):
            return {"verb": "role", "action": "unchanged", "role": node.name}
        text = text.rstrip("\r\n") + eol + eol + f"\tmember {_q(member)}" + eol
        _write(mdef, rel, text)
        return {"verb": "role", "action": "member-added", "role": node.name, "members": len(mem) + 1,
                "note": "the member is tokenized in the mirror after the next pull"}
    hit = next((c for c in mem if c.name == member or c.name.lower() == member.lower()), None)
    if hit is None:
        raise PbzError("NOT_FOUND", "member (pass the token shown in the mirror)")
    text = apply_edits(text, [tmdl.block_span(doc, hit) + ("",)])
    _write(mdef, rel, text)
    return {"verb": "role", "action": "member-removed", "role": node.name, "members": len(mem) - 1}
