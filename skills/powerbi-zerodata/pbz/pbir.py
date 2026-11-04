"""PBIR JSON with exact spans, plus semantic-query helpers (fields, literals, filters)."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------- span-preserving JSON

@dataclass
class JStr:
    value: str
    start: int
    end: int


@dataclass
class JLit:
    raw: str
    start: int
    end: int

    @property
    def value(self):
        return json.loads(self.raw)


@dataclass
class JArr:
    items: list
    start: int
    end: int


@dataclass
class JObj:
    pairs: list  # [(JStr key, node)]
    start: int
    end: int

    def get(self, key: str, default=None):
        for k, v in self.pairs:
            if k.value == key:
                return v
        return default

    def __contains__(self, key: str) -> bool:
        return any(k.value == key for k, _ in self.pairs)


class JsonError(ValueError):
    pass


_WS = " \t\r\n"
_NUM_RE = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")


def parse_json(text: str):
    pos = 0
    n = len(text)

    def ws(i):
        while i < n and text[i] in _WS:
            i += 1
        return i

    def string(i):
        if text[i] != '"':
            raise JsonError(f"expected string at {i}")
        j = i + 1
        while j < n:
            c = text[j]
            if c == "\\":
                j += 2
                continue
            if c == '"':
                return JStr(json.loads(text[i:j + 1]), i, j + 1), j + 1
            j += 1
        raise JsonError("unterminated string")

    def value(i):
        i = ws(i)
        if i >= n:
            raise JsonError("unexpected end")
        c = text[i]
        if c == "{":
            pairs, j = [], ws(i + 1)
            if j < n and text[j] == "}":
                return JObj(pairs, i, j + 1), j + 1
            while True:
                j = ws(j)
                k, j = string(j)
                j = ws(j)
                if text[j] != ":":
                    raise JsonError(f"expected : at {j}")
                v, j = value(j + 1)
                pairs.append((k, v))
                j = ws(j)
                if text[j] == ",":
                    j += 1
                    continue
                if text[j] == "}":
                    return JObj(pairs, i, j + 1), j + 1
                raise JsonError(f"expected , or }} at {j}")
        if c == "[":
            items, j = [], ws(i + 1)
            if j < n and text[j] == "]":
                return JArr(items, i, j + 1), j + 1
            while True:
                v, j = value(j)
                items.append(v)
                j = ws(j)
                if text[j] == ",":
                    j += 1
                    continue
                if text[j] == "]":
                    return JArr(items, i, j + 1), j + 1
                raise JsonError(f"expected , or ] at {j}")
        if c == '"':
            return string(i)
        for word in ("true", "false", "null"):
            if text.startswith(word, i):
                return JLit(word, i, i + len(word)), i + len(word)
        m = _NUM_RE.match(text, i)
        if m:
            return JLit(m.group(0), i, m.end()), m.end()
        raise JsonError(f"unexpected char at {i}")

    node, pos = value(pos)
    if ws(pos) != n:
        raise JsonError("trailing content")
    return node


def to_py(node):
    if isinstance(node, JObj):
        return {k.value: to_py(v) for k, v in node.pairs}
    if isinstance(node, JArr):
        return [to_py(v) for v in node.items]
    if isinstance(node, JStr):
        return node.value
    return node.value


# ---------------------------------------------------------------- PBIR literal encoding

LIT_RE = re.compile(r"^(?:'(?P<s>(?:[^']|'')*)'|(?P<i>-?\d+)L|(?P<d>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)[DM]|"
                    r"(?P<b>true|false)|(?P<null>null)|datetime'(?P<dt>[^']*)'|datetimeoffset'(?P<dto>[^']*)')$")


def decode_literal(raw: str):
    """PBIR literal Value string -> (python value, vtype). Unknown forms -> (raw, 'raw')."""
    m = LIT_RE.match(raw)
    if not m:
        return raw, "raw"
    if m.group("s") is not None:
        return m.group("s").replace("''", "'"), "string"
    if m.group("i") is not None:
        return int(m.group("i")), "int"
    if m.group("d") is not None:
        return float(m.group("d")), "double"
    if m.group("b") is not None:
        return m.group("b") == "true", "bool"
    if m.group("null") is not None:
        return None, "null"
    if m.group("dt") is not None:
        return m.group("dt"), "datetime"
    return m.group("dto"), "datetime"


def encode_literal(value, vtype: str) -> str:
    if vtype == "string":
        return "'" + str(value).replace("'", "''") + "'"
    if vtype == "int":
        return f"{int(value)}L"
    if vtype == "double":
        return f"{value}D"
    if vtype == "bool":
        return "true" if value else "false"
    if vtype == "null":
        return "null"
    if vtype == "datetime":
        return f"datetime'{value}'"
    return str(value)


# ---------------------------------------------------------------- semantic query walking

@dataclass
class LiteralSite:
    node: JStr                 # the Value string node
    column: tuple | None       # (table, column) or None
    raw: str                   # PBIR-encoded literal, e.g. 'Jane'
    where: str                 # filter | selector | formatting | other


@dataclass
class FieldRef:
    kind: str                  # column | measure | aggregation | hierarchy
    table: str
    name: str                  # column / measure / hierarchy name
    level: str = ""
    function: int | None = None
    node: JObj | None = None   # the object holding Entity/Property
    prop_node: JStr | None = None
    entity_node: JStr | None = None


def _source_entity(expr: JObj | None, aliases: dict) -> tuple[str, JStr | None]:
    if not isinstance(expr, JObj):
        return "", None
    sr = expr.get("SourceRef")
    if isinstance(sr, JObj):
        ent = sr.get("Entity")
        if isinstance(ent, JStr):
            return ent.value, ent
        src = sr.get("Source")
        if isinstance(src, JStr):
            return aliases.get(src.value, src.value), None
    return "", None


def field_of(node, aliases: dict, model=None) -> FieldRef | None:
    """Resolve a Column / Measure / Aggregation / HierarchyLevel object to a FieldRef."""
    if not isinstance(node, JObj):
        return None
    for kind, key in (("column", "Column"), ("measure", "Measure")):
        inner = node.get(key)
        if isinstance(inner, JObj):
            table, ent = _source_entity(inner.get("Expression"), aliases)
            prop = inner.get("Property")
            if isinstance(prop, JStr):
                return FieldRef(kind, table, prop.value, node=inner, prop_node=prop, entity_node=ent)
    agg = node.get("Aggregation")
    if isinstance(agg, JObj):
        inner = field_of(agg.get("Expression"), aliases, model)
        fn = agg.get("Function")
        if inner:
            inner.kind = "aggregation" if inner.kind == "column" else inner.kind
            inner.function = fn.value if isinstance(fn, JLit) else None
            return inner
    hl = node.get("HierarchyLevel")
    if isinstance(hl, JObj):
        h = hl.get("Expression")
        h = h.get("Hierarchy") if isinstance(h, JObj) else None
        level = hl.get("Level")
        if isinstance(h, JObj):
            table, ent = _source_entity(h.get("Expression"), aliases)
            hname = h.get("Hierarchy")
            hname = hname.value if isinstance(hname, JStr) else ""
            lname = level.value if isinstance(level, JStr) else ""
            col = ""
            if model is not None and table in model.tables:
                col = model.tables[table].hierarchies.get(hname, {}).get(lname, "")
            return FieldRef("hierarchy", table, col or hname, lname, entity_node=ent)
    return None


def walk_literals(root, model=None) -> list[LiteralSite]:
    """All Literal Value strings with the column they constrain (if any)."""
    sites: list[LiteralSite] = []

    def lit_at(node, col, where):
        if isinstance(node, JObj):
            lit = node.get("Literal")
            if isinstance(lit, JObj):
                val = lit.get("Value")
                if isinstance(val, JStr):
                    sites.append(LiteralSite(val, col, val.value, where))
                    return True
        return False

    def col_key(f: FieldRef | None):
        if f and f.kind in ("column", "aggregation", "hierarchy") and f.table and f.name:
            return (f.table, f.name)
        return None

    def visit(node, aliases, where):
        if isinstance(node, JArr):
            for v in node.items:
                visit(v, aliases, where)
            return
        if not isinstance(node, JObj):
            return
        frm = node.get("From")
        if isinstance(frm, JArr):
            aliases = dict(aliases)
            for e in frm.items:
                if isinstance(e, JObj) and isinstance(e.get("Name"), JStr) and isinstance(e.get("Entity"), JStr):
                    aliases[e.get("Name").value] = e.get("Entity").value
        here = where
        if "selector" in node or "scopeId" in node:
            here = "selector"
        if "Where" in node or "filter" in node or "filterConfig" in node:
            here = "filter" if where != "selector" else where
        if lit_at(node, None, here if here != "other" else "formatting"):
            return
        # hierarchy slicer expansion state: levels[d].identityKeys[i] <-> root.children...identityValues[i]
        levels, tree = node.get("levels"), node.get("root")
        if isinstance(levels, JArr) and isinstance(tree, JObj):
            level_cols = []
            for lv in levels.items:
                keys = lv.get("identityKeys") if isinstance(lv, JObj) else None
                level_cols.append([col_key(field_of(k, aliases, model)) for k in (keys.items if isinstance(keys, JArr) else [])])

            def walk_tree(obj, depth):
                ch = obj.get("children") if isinstance(obj, JObj) else None
                for child in ch.items if isinstance(ch, JArr) else []:
                    if not isinstance(child, JObj):
                        continue
                    vals = child.get("identityValues")
                    for i, v in enumerate(vals.items if isinstance(vals, JArr) else []):
                        cols = level_cols[depth] if depth < len(level_cols) else []
                        if not lit_at(v, cols[i] if i < len(cols) else None, "selection"):
                            visit(v, aliases, "selection")
                    walk_tree(child, depth + 1)

            walk_tree(tree, 0)
            for k, v in node.pairs:
                if k.value != "root":
                    visit(v, aliases, here)
            return
        # filter annotations: decomposedIdentities {columns: [{value: field}], values: [[...Literal...]]}
        dcols, dvals = node.get("columns"), node.get("values")
        if isinstance(dcols, JArr) and isinstance(dvals, JArr) and dcols.items and \
                all(isinstance(c, JObj) and "value" in c for c in dcols.items):
            cols = [col_key(field_of(c.get("value"), aliases, model)) for c in dcols.items]

            def walk_vals(n, last):
                if isinstance(n, JArr):
                    for i, v in enumerate(n.items):
                        walk_vals(v, i)
                elif isinstance(n, JObj):
                    col = cols[0] if len(cols) == 1 else (cols[last] if last < len(cols) else None)
                    if not lit_at(n, col, "selection"):
                        visit(n, aliases, "selection")

            walk_vals(dvals, 0)
            return
        for key, cond_kind in (("Comparison", "cmp"), ("Contains", "cmp"), ("StartsWith", "cmp"),
                               ("EndsWith", "cmp"), ("Between", "between"), ("In", "in")):
            c = node.get(key)
            if not isinstance(c, JObj):
                continue
            if cond_kind == "cmp":
                left, right = c.get("Left"), c.get("Right")
                lcol = col_key(field_of(left, aliases, model))
                rcol = col_key(field_of(right, aliases, model))
                if not lit_at(right, lcol, here):
                    visit(right, aliases, here)
                if not lit_at(left, rcol, here):
                    visit(left, aliases, here)
            elif cond_kind == "between":
                ecol = col_key(field_of(c.get("Expression"), aliases, model))
                for b in ("LowerBound", "UpperBound"):
                    if not lit_at(c.get(b), ecol, here):
                        visit(c.get(b), aliases, here)
            else:
                exprs = c.get("Expressions")
                cols = [col_key(field_of(e, aliases, model)) for e in (exprs.items if isinstance(exprs, JArr) else [])]
                vals = c.get("Values")
                if isinstance(vals, JArr):
                    for row in vals.items:
                        cells = row.items if isinstance(row, JArr) else [row]
                        for j, cell in enumerate(cells):
                            if not lit_at(cell, cols[j] if j < len(cols) else None, here):
                                visit(cell, aliases, here)
                for k, v in c.pairs:
                    if k.value not in ("Values",):
                        visit(v, aliases, here)
            for k, v in node.pairs:
                if k.value != key:
                    visit(v, aliases, here)
            return
        for _k, v in node.pairs:
            visit(v, aliases, here)

    visit(root, {}, "other")
    return sites


def walk_fields(root, model=None) -> list[FieldRef]:
    """Every Column / Measure / Aggregation / HierarchyLevel reference in a PBIR document."""
    refs: list[FieldRef] = []

    def visit(node, aliases):
        if isinstance(node, JArr):
            for v in node.items:
                visit(v, aliases)
            return
        if not isinstance(node, JObj):
            return
        frm = node.get("From")
        if isinstance(frm, JArr):
            aliases = dict(aliases)
            for e in frm.items:
                if isinstance(e, JObj) and isinstance(e.get("Name"), JStr) and isinstance(e.get("Entity"), JStr):
                    aliases[e.get("Name").value] = e.get("Entity").value
        f = field_of(node, aliases, model)
        if f:
            refs.append(f)
            if f.kind == "aggregation":
                return
        for _k, v in node.pairs:
            visit(v, aliases)

    visit(root, {})
    return refs


def string_nodes(root) -> list[tuple[str, JStr]]:
    """(parent key, JStr) for every string value (not keys) in document order."""
    out = []

    def visit(node, key):
        if isinstance(node, JStr):
            out.append((key, node))
        elif isinstance(node, JArr):
            for v in node.items:
                visit(v, key)
        elif isinstance(node, JObj):
            for k, v in node.pairs:
                visit(v, k.value)

    visit(root, "")
    return out


def report_files(report_dir: str) -> list[str]:
    out = []
    base = os.path.join(report_dir, "definition")
    for cur, _dirs, files in os.walk(base):
        for f in files:
            if f.lower().endswith(".json"):
                out.append(os.path.join(cur, f))
    return sorted(out)
