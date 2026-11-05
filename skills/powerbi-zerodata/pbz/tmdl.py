"""Line-based TMDL reader that keeps exact character spans.

Finds object declarations, properties, and expression regions (DAX or M) so literals can be
tokenized in place and references analysed, without re-serializing anything.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

DECL_KINDS = {
    "model", "database", "table", "column", "measure", "partition", "expression", "hierarchy", "level",
    "relationship", "role", "tablePermission", "columnPermission", "member", "calculationGroup",
    "calculationItem", "annotation", "extendedProperty", "perspective", "perspectiveTable",
    "perspectiveColumn", "perspectiveMeasure", "perspectiveHierarchy", "culture", "linguisticMetadata",
    "dataSource", "queryGroup", "ref", "function", "calendar", "calendarColumnGroup", "variation",
    "objectTranslation", "alternateOf", "refreshPolicy", "formatStringDefinition", "detailRowsDefinition",
    "kpi",
}
# Known expression properties. Any other `name = value` property line is ALSO treated as an
# expression region (fail closed), so its literals are tokenized like the rest.
EXPR_PROPS = {"source", "expression", "filterExpression", "formatStringDefinition", "detailRowsDefinition",
              "defaultExpression", "pollingExpression", "sourceExpression", "targetExpression", "statusExpression",
              "trendExpression", "noSelectionExpression", "multipleOrEmptySelectionExpression"}
DAX_DECLS = {"measure", "column", "calculationItem", "tablePermission", "columnPermission", "function"}

_NAME = r"'(?:[^']|'')*'|[^\s=:'][^\s=:]*"
DECL_RE = re.compile(rf"^(?P<ind>\t*)(?P<kw>[A-Za-z]+)(?:\s+(?P<name>(?:{_NAME})(?:\.(?:{_NAME}))*))?\s*(?:=\s?(?P<rest>.*))?$")
PROP_RE = re.compile(r"^(?P<ind>\t*)(?P<prop>[A-Za-z]+)\s*:\s?(?P<val>.*)$")
PROP_EXPR_RE = re.compile(r"^(?P<ind>\t*)(?P<prop>[A-Za-z]+)\s*=\s?(?P<rest>.*)$")


def unquote(name: str | None) -> str:
    if not name:
        return ""
    name = name.strip()
    if len(name) >= 2 and name[0] == "'" and name[-1] == "'":
        return name[1:-1].replace("''", "'")
    return name


def split_dotted(ref: str) -> list[str]:
    """'Table Name'.'Col' or Table.Col -> [Table Name, Col]."""
    parts, i, n = [], 0, len(ref)
    while i < n:
        if ref[i] == "'":
            j = i + 1
            while j < n:
                if ref[j] == "'" and j + 1 < n and ref[j + 1] == "'":
                    j += 2
                    continue
                if ref[j] == "'":
                    break
                j += 1
            parts.append(ref[i + 1:j].replace("''", "'"))
            i = j + 1
        else:
            j = ref.find(".", i)
            j = n if j < 0 else j
            parts.append(ref[i:j])
            i = j
        if i < n and ref[i] == ".":
            i += 1
    return parts


@dataclass
class Region:
    kind: str           # dax, m, text, member
    start: int
    end: int
    node: "Node"
    prop: str = ""


@dataclass
class Node:
    kind: str
    name: str
    indent: int
    line: int
    parent: "Node | None" = None
    props: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    expr: Region | None = None
    decl_value: str = ""   # e.g. "m" / "calculated" for partitions
    file: str = ""         # document path the node was parsed from

    def __repr__(self) -> str:  # parent/children links make the default repr recursive
        return f"Node({self.kind} {self.name!r} @ {self.file}:{self.line + 1})"

    def flag(self, prop: str) -> bool:
        """Boolean property: bare flag line (isHidden) or `prop: true`."""
        return self.props.get(prop, "false").strip().lower() == "true"

    def prop(self, prop: str, default: str = "") -> str:
        return unquote(self.props.get(prop, default).strip()) if prop in self.props else default

    def descendants(self):
        for c in self.children:
            yield c
            yield from c.descendants()


@dataclass
class TmdlDoc:
    path: str
    text: str
    nodes: list
    regions: list


def _lines(text: str) -> list[tuple[int, str]]:
    out, pos = [], 0
    for raw in text.splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        out.append((pos, line))
        pos += len(raw)
    return out


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip("\t"))


def parse(path: str, text: str) -> TmdlDoc:
    lines = _lines(text)
    nodes: list[Node] = []
    regions: list[Region] = []
    stack: list[Node] = []
    pending_desc: list[tuple[int, int]] = []
    i, n = 0, len(lines)

    def owner_for(ind: int) -> Node | None:
        while stack and stack[-1].indent >= ind:
            stack.pop()
        return stack[-1] if stack else None

    def lang_for(node: Node, prop: str) -> str:
        if node.kind == "partition":
            return "m" if node.decl_value.strip().lower() in ("m", "") and prop == "source" else "dax"
        if node.kind == "expression" or prop in ("pollingExpression", "sourceExpression"):
            return "m"
        return "dax"

    def take_expr(line_idx: int, ind: int, rest: str, rest_offset: int, node: Node, prop: str) -> int:
        """Record an expression region starting at rest_offset; return index of next unconsumed line."""
        lang = lang_for(node, prop)
        if rest.strip().startswith("```"):
            j = line_idx + 1
            start = lines[j][0] if j < n else rest_offset + len(rest)
            while j < n and lines[j][1].strip() != "```":
                j += 1
            end = lines[j][0] if j < n else len(text)
            reg = Region(lang, start, end, node, prop)
            regions.append(reg)
            return j + 1
        start = rest_offset
        end = rest_offset + len(rest)
        j = line_idx + 1
        last_content = line_idx
        while j < n:
            off, ln = lines[j]
            if ln.strip() == "":
                j += 1
                continue
            if _indent(ln) <= ind + 1:
                break
            last_content = j
            j += 1
        if last_content > line_idx:
            end = lines[last_content][0] + len(lines[last_content][1])
            if not rest.strip():
                start = lines[line_idx + 1][0]
        reg = Region(lang, start, end, node, prop)
        if end > start:
            regions.append(reg)
        return last_content + 1

    while i < n:
        off, line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        ind = _indent(line)
        if stripped.startswith("///"):
            s = off + line.index("///") + 3
            pending_desc.append((s, off + len(line)))
            i += 1
            continue
        m = DECL_RE.match(line)
        known = bool(m) and m.group("kw") in DECL_KINDS
        unknown_expr = bool(m) and not known and m.group("name") and m.group("rest") is not None
        if m and (known or unknown_expr) and not PROP_RE.match(line):
            kw = m.group("kw")
            parent = owner_for(ind)
            name = m.group("name") or ""
            node = Node(kw, unquote(name), ind, i, parent, file=path)
            nodes.append(node)
            if parent:
                parent.children.append(node)
            stack.append(node)
            for s, e in pending_desc:
                regions.append(Region("text", s, e, node, "description"))
            pending_desc = []
            rest = m.group("rest")
            if kw == "member" and name:
                s = off + line.index(name, len(m.group("ind")) + len(kw))
                regions.append(Region("member", s, s + len(name), node, "member"))
            if rest is not None:
                rest_offset = off + m.start("rest")
                if kw == "partition":
                    node.decl_value = rest.strip()
                    i += 1
                    continue
                if kw == "annotation" or kw == "extendedProperty":
                    j = take_expr(i, ind, rest, rest_offset, node, "value")
                    for r in regions[-1:]:
                        if r.node is node:
                            r.kind = "text"
                    i = j
                    continue
                if kw in DAX_DECLS or kw == "expression" or kw == "calculationItem":
                    i = take_expr(i, ind, rest, rest_offset, node, "expression")
                    continue
                # any other declaration carrying `= ...` (table shorthand, future object kinds):
                # treat the value as an expression so it is lexed and tokenized (fail closed)
                i = take_expr(i, ind, rest, rest_offset, node, "expression")
                continue
            i += 1
            continue
        pe = PROP_EXPR_RE.match(line)
        if pe and not PROP_RE.match(line):
            owner = owner_for(ind)
            node = owner or Node("orphan", "", ind, i, file=path)
            rest_offset = off + pe.start("rest")
            i = take_expr(i, ind, pe.group("rest"), rest_offset, node, pe.group("prop"))
            continue
        pm = PROP_RE.match(line)
        if pm:
            owner = owner_for(ind)
            if owner is not None:
                owner.props[pm.group("prop")] = pm.group("val")
            i += 1
            continue
        if re.fullmatch(r"\t*[A-Za-z]+", line):  # boolean flag property, e.g. isHidden
            owner = owner_for(ind)
            if owner is not None:
                owner.props[stripped] = "true"
        i += 1
    return TmdlDoc(path, text, nodes, regions)


def block_span(doc: TmdlDoc, node: Node) -> tuple[int, int]:
    """Character span of a node's whole block: its /// description lines, its own line, every deeper
    line, and the blank lines that follow it (up to the next line at the same or a lower indent)."""
    lines = _lines(doc.text)
    first = node.line
    while first > 0 and lines[first - 1][1].strip().startswith("///") and _indent(lines[first - 1][1]) == node.indent:
        first -= 1
    j = node.line + 1
    in_fence = False
    while j < len(lines):
        ln = lines[j][1]
        if ln.strip().startswith("```"):
            in_fence = not in_fence
            j += 1
            continue
        if not in_fence and ln.strip() and _indent(ln) <= node.indent:
            break
        j += 1
    end = lines[j][0] if j < len(lines) else len(doc.text)
    return lines[first][0], end


def prop_edit(doc: TmdlDoc, node: Node, prop: str, value: str | None) -> list[tuple[int, int, str]]:
    """Edits that set `prop: value` on a node (value None: a bare flag line such as isHidden).
    Replaces an existing value in place, else inserts a line right after the declaration and its
    expression. Returns [] when the property already has that value."""
    lines = _lines(doc.text)
    s, e = block_span(doc, node)
    for off, ln in lines:
        if off < s or off >= e or _indent(ln) != node.indent + 1:
            continue
        m = PROP_RE.match(ln)
        if m and m.group("prop") == prop:
            if value is None or m.group("val").strip() == value:
                return []
            return [(off + m.start("val"), off + len(ln), value)]
        if ln.strip() == prop:
            return [] if value is None else [(off + len(ln) - len(ln.lstrip("\t")), off + len(ln), f"{prop}: {value}")]
    regs = [r for r in doc.regions if r.node is node and r.prop == "expression"]
    after = max([r.end for r in regs] + [lines[node.line][0] + len(lines[node.line][1])])
    nl = doc.text.find("\n", after)
    eol = "\r\n" if "\r\n" in doc.text else "\n"
    line = "\t" * (node.indent + 1) + prop + (f": {value}" if value is not None else "") + eol
    if nl < 0:
        return [(len(doc.text), len(doc.text), eol + line)]
    return [(nl + 1, nl + 1, line)]


def node_regions(doc: TmdlDoc, node: Node, deep: bool = True) -> list[Region]:
    """Expression regions owned by a node (and, with deep, by its child nodes: KPIs, format strings...)."""
    ids = {id(node)} | ({id(d) for d in node.descendants()} if deep else set())
    return [r for r in doc.regions if id(r.node) in ids]


# ---------------------------------------------------------------- model index

@dataclass
class Column:
    table: str
    name: str
    data_type: str = ""
    calculated: bool = False
    source_column: str = ""
    sort_by: str = ""
    hidden: bool = False
    node: Node | None = field(default=None, repr=False, compare=False)


@dataclass
class Measure:
    table: str
    name: str
    expression: str = ""
    format_string: str = ""
    folder: str = ""
    node: Node | None = field(default=None, repr=False, compare=False)


@dataclass
class Table:
    name: str
    file: str = ""
    columns: dict = field(default_factory=dict)
    measures: dict = field(default_factory=dict)
    partitions: list = field(default_factory=list)   # (name, kind, source expr)
    hierarchies: dict = field(default_factory=dict)  # hierarchy -> {level: column}
    calculated: bool = False
    hidden: bool = False
    node: Node | None = field(default=None, repr=False, compare=False)
    calc_items: dict = field(default_factory=dict)   # calculation group items: name -> Node
    partition_nodes: list = field(default_factory=list, repr=False, compare=False)

    @property
    def private(self) -> bool:
        """Auto date/time tables and other engine-private tables."""
        n = self.node
        return self.name.startswith(("LocalDateTable_", "DateTableTemplate_")) or bool(
            n and (n.flag("isPrivate") or n.flag("showAsVariationsOnly")))


@dataclass
class Relationship:
    name: str
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    both: bool = False
    active: bool = True
    node: Node | None = field(default=None, repr=False, compare=False)


@dataclass
class Model:
    tables: dict = field(default_factory=dict)
    relationships: list = field(default_factory=list)
    roles: list = field(default_factory=list)
    docs: dict = field(default_factory=dict)  # relpath -> TmdlDoc
    expressions: dict = field(default_factory=dict)  # shared M queries and parameters: name -> Node
    functions: dict = field(default_factory=dict)    # DAX user-defined functions: name -> Node
    role_nodes: dict = field(default_factory=dict)   # name -> Node
    model_node: Node | None = None
    perspectives: dict = field(default_factory=dict)  # name -> Node

    def text_of(self, reg: Region) -> str:
        return self.docs[reg.node.file].text[reg.start:reg.end]

    def table(self, name: str) -> "Table | None":
        return self.tables.get(name) or next((v for k, v in self.tables.items() if k.lower() == name.lower()), None)

    def column(self, table: str, col: str) -> Column | None:
        t = self.tables.get(table) or next((v for k, v in self.tables.items() if k.lower() == table.lower()), None)
        if not t:
            return None
        return t.columns.get(col) or next((v for k, v in t.columns.items() if k.lower() == col.lower()), None)

    def measure(self, name: str) -> Measure | None:
        for t in self.tables.values():
            m = t.measures.get(name) or next((v for k, v in t.measures.items() if k.lower() == name.lower()), None)
            if m:
                return m
        return None

    def all_measures(self) -> list[Measure]:
        return [m for t in self.tables.values() for m in t.measures.values()]

    def all_columns(self) -> list[Column]:
        return [c for t in self.tables.values() for c in t.columns.values()]

    def object_names(self) -> set[str]:
        names = set(self.tables)
        for t in self.tables.values():
            names |= set(t.columns) | set(t.measures) | set(t.hierarchies)
        return names


def tmdl_files(definition_dir: str) -> list[str]:
    out = []
    for cur, _dirs, files in os.walk(definition_dir):
        for f in files:
            if f.lower().endswith(".tmdl"):
                out.append(os.path.join(cur, f))
    return sorted(out)


def load_model(definition_dir: str, texts: dict | None = None) -> Model:
    """Build a Model from a TMDL definition folder (or from {relpath: text})."""
    model = Model()
    if texts is None:
        texts = {}
        for p in tmdl_files(definition_dir):
            with open(p, "rb") as f:
                raw = f.read()
            if raw.startswith(b"\xef\xbb\xbf"):
                raw = raw[3:]
            texts[os.path.relpath(p, definition_dir).replace("\\", "/")] = raw.decode("utf-8")
    for relp, text in texts.items():
        if relp.lower().startswith("cultures/"):
            continue  # translations and linguistic metadata, not model objects
        doc = parse(relp, text)
        model.docs[relp] = doc
        for node in doc.nodes:
            if node.kind == "table" and node.parent is None:
                t = model.tables.setdefault(node.name, Table(node.name, relp))
                t.file = relp
                t.hidden = "isHidden" in node.props
                t.node = node
            elif node.kind == "column" and node.parent and node.parent.kind == "table":
                t = model.tables.setdefault(node.parent.name, Table(node.parent.name, relp))
                t.columns[node.name] = Column(
                    t.name, node.name, node.props.get("dataType", "").strip(), node.expr is not None or any(
                        r.node is node and r.kind == "dax" for r in doc.regions),
                    unquote(node.props.get("sourceColumn", "").strip()), unquote(node.props.get("sortByColumn", "").strip()),
                    "isHidden" in node.props, node)
            elif node.kind == "measure" and node.parent and node.parent.kind == "table":
                t = model.tables.setdefault(node.parent.name, Table(node.parent.name, relp))
                reg = next((r for r in doc.regions if r.node is node and r.kind == "dax"), None)
                t.measures[node.name] = Measure(t.name, node.name, text[reg.start:reg.end] if reg else "",
                                                node.props.get("formatString", "").strip(),
                                                node.props.get("displayFolder", "").strip(), node)
            elif node.kind == "partition" and node.parent and node.parent.kind == "table":
                t = model.tables.setdefault(node.parent.name, Table(node.parent.name, relp))
                reg = next((r for r in doc.regions if r.node is node), None)
                kind = node.decl_value.strip().lower() or "m"
                t.partitions.append((node.name, kind, text[reg.start:reg.end] if reg else ""))
                t.partition_nodes.append(node)
                if kind == "calculated":
                    t.calculated = True
            elif node.kind == "calculationItem":
                tnode = node.parent.parent if node.parent and node.parent.kind == "calculationGroup" else None
                if tnode is not None and tnode.kind == "table":
                    model.tables.setdefault(tnode.name, Table(tnode.name, relp)).calc_items[node.name] = node
            elif node.kind == "expression" and node.parent is None:
                model.expressions[node.name] = node
            elif node.kind == "function" and node.parent is None:
                model.functions[node.name] = node
            elif node.kind == "model" and node.parent is None:
                model.model_node = node
            elif node.kind == "perspective" and node.parent is None:
                model.perspectives[node.name] = node
            elif node.kind == "level" and node.parent and node.parent.kind == "hierarchy":
                h = node.parent
                if h.parent and h.parent.kind == "table":
                    t = model.tables.setdefault(h.parent.name, Table(h.parent.name, relp))
                    t.hierarchies.setdefault(h.name, {})[node.name] = unquote(node.props.get("column", "").strip())
            elif node.kind == "relationship":
                fr = split_dotted(node.props.get("fromColumn", "").strip())
                to = split_dotted(node.props.get("toColumn", "").strip())
                if len(fr) >= 2 and len(to) >= 2:
                    model.relationships.append(Relationship(
                        node.name, fr[0], fr[1], to[0], to[1],
                        node.props.get("crossFilteringBehavior", "").strip() == "bothDirections",
                        node.props.get("isActive", "true").strip().lower() != "false", node))
            elif node.kind == "role":
                model.roles.append(node.name)
                model.role_nodes[node.name] = node
    return model
