"""`pbz set` / `pbz get`: typed property edits on model objects in the mirror, one object or many at
once (Tabular Editor's property grid and its bulk scripts, without scripting): descriptions, display
folders, format strings, hidden, key, data category, sort-by column, summarize-by, MDX availability.

Names and metadata only. Values are what you type; mirror tokens are refused in new text (an old
description that held tokens is replaced as a whole). Data types stay with Desktop or the TMDL
text. `lineageTag=regenerate` writes a new id. Detail rows and alternateOf are blocks, not
`prop: value` lines.
"""
from __future__ import annotations

import os
import re
import uuid

from . import tmdl
from .common import PbzError, commit_bytes, read_text
from .lexers import tmdl_name
from .tokens import TOKEN_RE, apply_edits

CATEGORIES = ["Uncategorized", "Address", "City", "Continent", "Country", "County", "Latitude", "Longitude",
              "Place", "PostalCode", "StateOrProvince", "WebUrl", "ImageUrl", "Barcode", "Organization",
              "Product", "Years", "Months", "Quarters", "Days", "Time"]
SUMMARIZE = ["none", "sum", "count", "min", "max", "average", "distinctCount"]
ENCODING = ["Hash", "Value"]
SUMMARIZATION = ["sum", "count", "min", "max", "average", "distinctCount", "groupBy"]
PROPS = {  # property -> (object kinds, value type, allowed values)
    "description": (("table", "column", "measure", "hierarchy"), "description", None),
    "displayFolder": (("column", "measure", "hierarchy"), "text", None),
    "formatString": (("column", "measure"), "text", None),
    "isHidden": (("table", "column", "measure", "hierarchy"), "flag", None),
    "isKey": (("column",), "flag", None),
    "dataCategory": (("table", "column"), "enum", CATEGORIES),
    "summarizeBy": (("column",), "enum", SUMMARIZE),
    "sortByColumn": (("column",), "column", None),
    "isAvailableInMdx": (("column",), "bool", None),
    "encodingHint": (("column",), "enum", ENCODING),
    "isNullable": (("column",), "bool", None),
    "lineageTag": (("table", "column", "measure", "hierarchy"), "lineage", None),
    "detailRowsExpression": (("measure",), "daxblock", None),
    "alternateOf": (("measure",), "altblock", None),
}
_BLOCK_TYPES = {"lineage", "daxblock", "altblock"}


def _objects(model, target: str | None, kind: str | None, table: str | None, match: str | None,
             folder: str | None) -> list[tuple[str, str, object]]:
    """[(kind, label, node)] selected by a target ('T'[C], [M], T, T.Hierarchy) or by filters."""
    out = []
    if target:
        t = target.strip()
        m = re.match(r"^'((?:[^']|'')+)'\s*\[(.+)\]$", t) or re.match(r"^([^\[\]'.]+?)\s*\[(.+)\]$", t)
        if m:
            tab, name = m.group(1).replace("''", "'"), m.group(2)
            c = model.column(tab, name)
            if c:
                return [("column", f"'{c.table}'[{c.name}]", c.node)]
            mm = model.measure(name)
            if mm and mm.table.lower() == tab.lower():
                return [("measure", f"[{mm.name}]", mm.node)]
            raise PbzError("NOT_FOUND", target)
        m = re.match(r"^\[(.+)\]$", t)
        if m:
            mm = model.measure(m.group(1))
            if not mm:
                raise PbzError("NOT_FOUND", target)
            return [("measure", f"[{mm.name}]", mm.node)]
        m = re.match(r"^'?((?:[^'.]|'')+)'?\.(.+)$", t)
        if m and model.table(m.group(1).replace("''", "'")):
            tb = model.table(m.group(1).replace("''", "'"))
            h = next((h for h in tb.hierarchies if h.lower() == m.group(2).lower()), None)
            node = next((n for n in (tb.node.children if tb.node else []) if n.kind == "hierarchy" and n.name == h), None)
            if node is not None:
                return [("hierarchy", f"'{tb.name}'.{h}", node)]
        tb = model.table(t.strip("'"))
        if tb is None:
            raise PbzError("NOT_FOUND", target)
        return [("table", f"'{tb.name}'", tb.node)]
    if not (kind or table or match or folder):
        raise PbzError("BAD_REQUEST", "name an object, or select with --kind / --table / --match / --folder")
    rx = re.compile(match, re.I) if match else None
    for tb in model.tables.values():
        if tb.private or (table and tb.name.lower() != table.lower()):
            continue
        cands = []
        if kind in (None, "table"):
            cands.append(("table", tb.name, f"'{tb.name}'", tb.node, ""))
        if kind in (None, "column"):
            cands += [("column", c.name, f"'{tb.name}'[{c.name}]", c.node,
                       tmdl.unquote(c.node.props.get("displayFolder", "")) if c.node else "") for c in tb.columns.values()]
        if kind in (None, "measure"):
            cands += [("measure", mm.name, f"[{mm.name}]", mm.node, mm.folder) for mm in tb.measures.values()]
        if kind in (None, "hierarchy"):
            cands += [("hierarchy", n.name, f"'{tb.name}'.{n.name}", n, n.props.get("displayFolder", ""))
                      for n in (tb.node.children if tb.node else []) if n.kind == "hierarchy"]
        for k, name, label, node, fld in cands:
            if node is None or (rx and not rx.search(name)):
                continue
            if folder is not None and (fld or "").strip().strip("'\"").lower() != folder.lower():
                continue
            out.append((k, label, node))
    if not out:
        raise PbzError("NOT_FOUND", "no object matches the selection")
    return out


def _value(prop: str, raw: str, model, kind: str, node) -> str | None:
    kinds, typ, allowed = PROPS[prop]
    if typ in ("text", "description"):
        if TOKEN_RE.search(raw):
            raise PbzError("BAD_REQUEST", "mirror tokens cannot be written into new text")
        if typ == "text" and ("\n" in raw or "\r" in raw):
            raise PbzError("BAD_REQUEST", f"{prop} is one line")
        return raw
    if typ == "flag":
        low = raw.lower()
        if low not in ("true", "false", "yes", "no", "on", "off"):
            raise PbzError("BAD_REQUEST", f"{prop}: true or false")
        return "" if low in ("true", "yes", "on") else None
    if typ == "bool":
        low = raw.lower()
        if low not in ("true", "false"):
            raise PbzError("BAD_REQUEST", f"{prop}: true or false")
        return low
    if typ == "enum":
        hit = next((v for v in allowed if v.lower() == raw.lower()), None)
        if hit is None:
            raise PbzError("BAD_REQUEST", f"{prop}: one of {', '.join(allowed)}")
        if prop == "dataCategory" and kind == "table" and hit != "Time":
            raise PbzError("BAD_REQUEST", "a table's data category is Time (marks a date table) or nothing")
        return hit
    if typ == "column":
        table = node.parent.name if node.parent is not None else ""
        col = model.column(table, raw.strip("'[]"))
        if col is None:
            raise PbzError("NOT_FOUND", f"sort-by column {raw} in table {table}")
        if col.name.lower() == node.name.lower():
            raise PbzError("BAD_REQUEST", "a column cannot sort by itself")
        if col.sort_by and col.sort_by.lower() == node.name.lower():
            raise PbzError("BAD_REQUEST", f"{col.name} already sorts by {node.name}: the sort would loop")
        return tmdl_name(col.name)
    raise PbzError("UNSUPPORTED", prop)


def _desc_edits(doc, node, text: str | None) -> list[tuple[int, int, str]]:
    """Replace (or insert, or with None remove) the /// description lines above a declaration."""
    lines = doc.text.splitlines(keepends=True)
    starts, pos = [], 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln)
    i = node.line
    first = i
    while first > 0 and lines[first - 1].strip().startswith("///"):
        first -= 1
    eol = "\r\n" if "\r\n" in doc.text else "\n"
    ind = "\t" * node.indent
    new = "".join(f"{ind}/// {part}{eol}" for part in (text.split("\n") if text else [])) if text is not None else ""
    old = doc.text[starts[first]:starts[i]]
    return [] if old == new else [(starts[first], starts[i], new)]


def _eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _insert_at_end(doc, node, block: str) -> list[tuple[int, int, str]]:
    """Insert `block` at the end of a node's own block, ahead of the trailing blank lines."""
    eol = _eol(doc.text)
    end = tmdl.block_span(doc, node)[1]
    prefix = doc.text[:end]
    trail = len(prefix) - len(prefix.rstrip("\r\n"))
    at = end - trail + (len(eol) if trail else 0)
    return [(at, at, block if trail else eol + block)]


def _swap_child(doc, node, kind: str, block: str | None) -> list[tuple[int, int, str]]:
    """Insert, replace, or (block None) remove one child declaration. Uses block_span, not prop_edit."""
    child = next((c for c in node.children if c.kind == kind), None)
    if block is None:
        if child is None:
            return []
        s, e = tmdl.block_span(doc, child)
        return [(s, e, "")]
    if child is not None:
        s, e = tmdl.block_span(doc, child)
        return [] if doc.text[s:e] == block else [(s, e, block)]
    return _insert_at_end(doc, node, block)


def _lineage_edit(doc, node) -> list[tuple[int, int, str]]:
    """Write a fresh lineageTag. Not the generic `prop: value` writer."""
    new = str(uuid.uuid4())
    lines = tmdl._lines(doc.text)
    s, e = tmdl.block_span(doc, node)
    for off, ln in lines:
        if off < s or off >= e or tmdl._indent(ln) != node.indent + 1:
            continue
        m = tmdl.PROP_RE.match(ln)
        if m and m.group("prop") == "lineageTag":
            return [(off + m.start("val"), off + len(ln), new)]
    eol = _eol(doc.text)
    line = "\t" * (node.indent + 1) + "lineageTag: " + new + eol
    regs = [r for r in doc.regions if r.node is node and r.prop == "expression"]
    after = max([r.end for r in regs] + [lines[node.line][0] + len(lines[node.line][1])])
    nl = doc.text.find("\n", after)
    if nl < 0:
        return [(len(doc.text), len(doc.text), eol + line)]
    return [(nl + 1, nl + 1, line)]


def _detail_block(node, raw: str, eol: str) -> str:
    if TOKEN_RE.search(raw):
        raise PbzError("BAD_REQUEST", "mirror tokens cannot be written into new text")
    body = raw.strip()
    if not body or "\x00" in body:
        raise PbzError("BAD_REQUEST", "detailRowsExpression needs a DAX expression")
    ind = "\t" * (node.indent + 1)
    if "\n" in body or "\r" in body:
        inner = eol.join(ind + "\t" + ln.strip() for ln in body.splitlines() if ln.strip())
        return f"{ind}detailRowsDefinition ={eol}{inner}{eol}"
    return f"{ind}detailRowsDefinition = {body}{eol}"


def _alternate_block(model, node, raw: str, eol: str) -> str:
    if TOKEN_RE.search(raw):
        raise PbzError("BAD_REQUEST", "mirror tokens cannot be written into new text")
    m = re.match(r"(?i)^(sum|count|min|max|average|distinctcount|groupby)\s*:\s*(.+)$", raw.strip())
    if not m:
        raise PbzError("BAD_REQUEST", "alternateOf: sum: 'Table'[Column] or sum: [Measure]")
    summ = next(s for s in SUMMARIZATION if s.lower() == m.group(1).lower())
    ref = m.group(2).strip()
    ind = "\t" * (node.indent + 1)
    lines = [f"{ind}alternateOf", f"{ind}\tsummarization: {summ}"]
    meas = re.match(r"^\[(.+)\]$", ref)
    col = re.match(r"^'((?:[^']|'')+)'\s*\[(.+)\]$", ref) or re.match(r"^([^\[\]']+?)\s*\[(.+)\]$", ref)
    if meas:
        mm = model.measure(meas.group(1))
        if mm is None:
            raise PbzError("NOT_FOUND", ref)
        lines.append(f"{ind}\tbaseMeasure: {tmdl_name(mm.table)}.{tmdl_name(mm.name)}")
    elif col:
        hit = model.column(col.group(1).replace("''", "'"), col.group(2))
        if hit is None:
            raise PbzError("NOT_FOUND", ref)
        lines.append(f"{ind}\tbaseColumn: {tmdl_name(hit.table)}.{tmdl_name(hit.name)}")
    else:
        raise PbzError("BAD_REQUEST", "alternateOf: sum: 'Table'[Column] or sum: [Measure]")
    return eol.join(lines) + eol


def _block_edits(model, doc, node, prop: str, op: str, raw: str | None) -> list[tuple[int, int, str]]:
    if prop == "lineageTag":
        if op == "unset":
            return _remove_prop(doc, node, "lineageTag")
        if raw is None or raw.strip().lower() != "regenerate" or TOKEN_RE.search(raw):
            raise PbzError("BAD_REQUEST", "lineageTag: regenerate")
        return _lineage_edit(doc, node)
    kind = "detailRowsDefinition" if prop == "detailRowsExpression" else "alternateOf"
    if op == "unset":
        return _swap_child(doc, node, kind, None)
    eol = _eol(doc.text)
    block = _detail_block(node, raw or "", eol) if prop == "detailRowsExpression" else _alternate_block(model, node, raw or "", eol)
    return _swap_child(doc, node, kind, block)


def _remove_prop(doc, node, prop: str) -> list[tuple[int, int, str]]:
    s, e = tmdl.block_span(doc, node)
    pos = 0
    for ln in doc.text.splitlines(keepends=True):
        off, pos = pos, pos + len(ln)
        if off < s or off >= e:
            continue
        body = ln.rstrip("\r\n")
        if len(body) - len(body.lstrip("\t")) != node.indent + 1:
            continue
        m = tmdl.PROP_RE.match(body)
        if (m and m.group("prop") == prop) or body.strip() == prop:
            return [(off, pos, "")]
    return []


def run(ws, project, target: str | None = None, sets: list[str] | None = None, unsets: list[str] | None = None,
        kind: str | None = None, table: str | None = None, match: str | None = None, folder: str | None = None,
        dry_run: bool = False) -> dict:
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    model = tmdl.load_model(mdef)
    parsed = []
    for s in sets or []:
        prop, eq, raw = s.partition("=")
        if not eq or prop.strip() not in PROPS:
            raise PbzError("BAD_REQUEST", f"{s}: use one of {', '.join(PROPS)} as prop=value")
        parsed.append(("set", prop.strip(), raw))
    for u in unsets or []:
        if u.strip() not in PROPS:
            raise PbzError("BAD_REQUEST", f"{u}: unknown property (known: {', '.join(PROPS)})")
        parsed.append(("unset", u.strip(), None))
    if not parsed:
        raise PbzError("BAD_REQUEST", "pass --set prop=value or --unset prop")
    objs = _objects(model, target, kind, table, match, folder)
    edits: dict[str, list] = {}
    changed, skipped = [], []
    for k, label, node in objs:
        done = []
        for op, prop, raw in parsed:
            kinds = PROPS[prop][0]
            if k not in kinds:
                if target:
                    raise PbzError("BAD_REQUEST", f"{prop} does not apply to a {k}")
                continue
            doc = model.docs[node.file]
            if op == "set" and prop == "formatString" and any(c.kind == "formatStringDefinition" for c in node.children):
                # Desktop refuses to open a measure that has both (TOM accepts it)
                if target:
                    raise PbzError("FORMAT_STRING_CONFLICT", f"{label} has a dynamic format string: "
                                   "pbz format-expr --remove it first, or change that expression")
                continue
            if PROPS[prop][1] in _BLOCK_TYPES:
                ed = _block_edits(model, doc, node, prop, op, raw)
            elif PROPS[prop][1] == "description":
                ed = _desc_edits(doc, node, None if op == "unset" else _value(prop, raw, model, k, node))
            elif op == "unset":
                ed = _remove_prop(doc, node, prop)
            else:
                val = _value(prop, raw, model, k, node)
                if PROPS[prop][1] == "flag":
                    ed = tmdl.prop_edit(doc, node, prop, None) if val == "" else _remove_prop(doc, node, prop)
                else:
                    ed = tmdl.prop_edit(doc, node, prop, val)
            if ed:
                edits.setdefault(node.file, []).extend(ed)
                done.append(("-" if op == "unset" else "") + prop)
        if done:
            changed.append({"object": label, "kind": k, "changes": done})
        elif not target:
            skipped.append(label)
    files = []
    ops = []
    for rel, eds in edits.items():
        files.append(f"{project.name}.SemanticModel/definition/{rel}")
        if not dry_run:
            path = os.path.join(mdef, *rel.split("/"))
            _t, bom = read_text(path)
            ops.append(("write", path, apply_edits(model.docs[rel].text, sorted(set(eds))), bom))
    if ops:
        commit_bytes(ops)
    out = {"verb": "set", "project": project.name, "result": "DRY_RUN" if dry_run else ("CHANGED" if changed else "NOTHING_TO_CHANGE"),
           "changed": changed[:500], "files": sorted(files)}
    if len(changed) > 500:
        out["more"] = len(changed) - 500
    if skipped and not changed:
        out["note"] = "every selected object already had these values or the properties do not apply"
    if changed and not dry_run:
        out["next"] = "pbz diff, pbz validate, then pbz push"
    return out


def get(ws, project, target: str | None = None, kind: str | None = None, table: str | None = None,
        match: str | None = None, folder: str | None = None) -> dict:
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    model = tmdl.load_model(mdef)
    rows = []
    for k, label, node in _objects(model, target, kind, table, match, folder)[:500]:
        doc = model.docs[node.file]
        rec = {"object": label, "kind": k}
        desc = [doc.text[r.start:r.end].strip() for r in doc.regions if r.node is node and r.prop == "description"]
        if desc:
            rec["description"] = "\n".join(desc)
        for prop, (kinds, typ, _a) in PROPS.items():
            if k not in kinds or typ == "description" or typ in _BLOCK_TYPES:
                continue
            if typ == "flag":
                if any(ln.strip() == prop for ln in doc.text[slice(*tmdl.block_span(doc, node))].splitlines()
                       if len(ln) - len(ln.lstrip("\t")) == node.indent + 1):
                    rec[prop] = True
            elif prop in node.props:
                rec[prop] = node.props[prop].strip()
        rows.append(rec)
    return {"verb": "get", "project": project.name, "objects": rows}
