"""Reference resolution: which model objects a DAX or M expression depends on. Names only, no values.

DAX rules follow Tabular Editor 2's FormulaFixup / DaxDependencyHelper (MIT):
- 'T'[x] or T[x] is a column of T, else a measure (qualified measure reference), else unresolved.
- a bare [x] is a measure; else a column of the innermost iterator table (row context); else a column
  of the home table; else the only column of that name in the model; else an extension column
  (ADDCOLUMNS / SELECTCOLUMNS name) or unresolved.
- calculated columns, RLS filters and calculation items have a row context on the home table, so a bare
  [x] there is a home column first.
- ident( is a function call (a user-defined function when the model defines one of that name);
  a bare ident is a table only when the model has a table of that name and no VAR shadows it.
- mirror tokens «...» are opaque literals, never references.
M rules: an identifier or #"quoted identifier" naming a shared expression (query or parameter) is a
reference to it, unless a let step of the same name shadows it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import lexers

# Functions whose first argument is a table expression that sets the row context of later arguments.
ITERATORS = {
    "SUMX", "AVERAGEX", "MINX", "MAXX", "COUNTX", "COUNTAX", "PRODUCTX", "CONCATENATEX", "RANKX", "FILTER",
    "ADDCOLUMNS", "SELECTCOLUMNS", "GENERATE", "GENERATEALL", "MEDIANX", "PERCENTILEX.INC", "PERCENTILEX.EXC",
    "GEOMEANX", "STDEVX.P", "STDEVX.S", "VARX.P", "VARX.S", "MAXA", "TOPN", "SUMMARIZE", "GROUPBY",
    "FIRSTNONBLANK", "LASTNONBLANK", "FIRSTNONBLANKVALUE", "LASTNONBLANKVALUE", "CALCULATETABLE", "CALCULATE",
    "COUNTROWS", "ADDMISSINGITEMS", "NATURALINNERJOIN", "NATURALLEFTOUTERJOIN", "SAMPLE", "INDEX", "OFFSET",
    "WINDOW", "RANK", "ROWNUMBER", "PARTITIONBY", "ORDERBY", "MATCHBY", "LINEST", "LINESTX",
}
# Iterators whose row context really applies to later arguments (CALCULATE etc. only scope tables).
ROW_CONTEXT = ITERATORS - {"CALCULATE", "CALCULATETABLE", "COUNTROWS", "TOPN", "SAMPLE", "INDEX", "OFFSET",
                           "WINDOW", "RANK", "ROWNUMBER", "PARTITIONBY", "ORDERBY", "MATCHBY",
                           "NATURALINNERJOIN", "NATURALLEFTOUTERJOIN"}
RELATIONSHIP_FUNCS = {"USERELATIONSHIP", "CROSSFILTER"}


@dataclass(frozen=True)
class Ref:
    kind: str    # column | measure | table | function | extension | unresolved
    table: str   # table of a column / qualified reference ("" for measures, functions, unresolved bare refs)
    name: str
    via: str = "dax"


@dataclass
class DaxDeps:
    refs: list = field(default_factory=list)            # [Ref]
    relationships: list = field(default_factory=list)   # [(fn, (table, column), (table, column))]
    unresolved: list = field(default_factory=list)      # [Ref] that matched nothing in the model


class Resolver:
    """Model name indexes (case-insensitive, as DAX resolves names)."""

    def __init__(self, model):
        self.model = model
        self.tables = {t.lower(): t for t in model.tables}
        self.measures = {}
        self.columns = {}       # table_lower -> {col_lower: col}
        self.by_column = {}     # col_lower -> [table]
        for t in model.tables.values():
            cols = {c.lower(): c for c in t.columns}
            self.columns[t.name.lower()] = cols
            for c in t.columns:
                self.by_column.setdefault(c.lower(), []).append(t.name)
            for m in t.measures:
                self.measures.setdefault(m.lower(), (t.name, m))
        self.functions = {f.lower(): f for f in model.functions}
        self.expressions = {e.lower(): e for e in model.expressions}

    def table(self, name: str) -> str | None:
        return self.tables.get(name.lower())

    def column(self, table: str, col: str) -> str | None:
        return self.columns.get(table.lower(), {}).get(col.lower())

    def measure(self, name: str):
        return self.measures.get(name.lower())


def _vars_and_params(sig: list, header: str = "") -> set[str]:
    names = set()
    for i, t in enumerate(sig):
        if t.kind == "ident" and t.text.upper() == "VAR" and i + 1 < len(sig) and sig[i + 1].kind == "ident":
            names.add(sig[i + 1].text.lower())
    for m in re.finditer(r"([A-Za-z_][\w.]*)\s*(?::[^,()]*)?(?=[,)])", header):
        names.add(m.group(1).lower())
    return names


def _function_header(text: str) -> tuple[str, str]:
    """Split a UDF definition `(a, b: x) => body` into (parameter list, body)."""
    m = re.match(r"\s*\((?P<params>[^()]*)\)\s*=>", text)
    if not m:
        return "", text
    return m.group("params") + ")", text[m.end():]


def dax(expr: str, home: str, res: Resolver, row_home: bool = False, function: bool = False) -> DaxDeps:
    """References made by one DAX expression whose home table is `home`.

    row_home: the expression has a row context on the home table (calculated column, RLS filter,
    calculation item); function: the text is a UDF definition with a parameter list.
    """
    header = ""
    if function:
        header, expr = _function_header(expr)
    sig = lexers.significant(lexers.lex_dax(expr))
    local = _vars_and_params(sig, header)
    extension = {t.value.lower() for t in sig if t.kind == "str"}
    out = DaxDeps()
    stack: list[dict] = []   # {"fn", "arg", "row_table"}
    rel_calls: list[dict] = []
    n = len(sig)

    def add(ref: Ref, at: int):
        out.refs.append(ref)
        if ref.kind == "unresolved":
            out.unresolved.append(ref)
        for fr in stack:
            if fr["fn"] in ITERATORS and fr["arg"] == 0 and fr["row_table"] is None and ref.table and ref.kind in ("table", "column"):
                fr["row_table"] = ref.table
        for rc in rel_calls:
            if rc["depth"] <= len(stack) and ref.kind == "column":
                rc["cols"].append((ref.table, ref.name))

    def row_tables() -> list[str]:
        return [fr["row_table"] for fr in reversed(stack) if fr["fn"] in ROW_CONTEXT and fr["arg"] >= 1 and fr["row_table"]]

    i = 0
    while i < n:
        t = sig[i]
        nxt = sig[i + 1] if i + 1 < n else None
        if t.kind == "op" and t.text == "(":
            prev = sig[i - 1] if i else None
            fn = prev.text.upper() if prev is not None and prev.kind == "ident" else ""
            stack.append({"fn": fn, "arg": 0, "row_table": None})
            if fn in RELATIONSHIP_FUNCS:
                rel_calls.append({"fn": fn, "depth": len(stack), "cols": []})
        elif t.kind == "op" and t.text == ")":
            if rel_calls and rel_calls[-1]["depth"] == len(stack):
                rc = rel_calls.pop()
                if len(rc["cols"]) >= 2:
                    out.relationships.append((rc["fn"], rc["cols"][0], rc["cols"][1]))
            if stack:
                stack.pop()
        elif t.kind == "op" and t.text == "," and stack:
            stack[-1]["arg"] += 1
        elif t.kind in ("table", "ident") and nxt is not None and nxt.kind == "bracket" and (
                t.kind == "table" or t.text.lower() in local or res.table(t.text)):
            qual = t.value if t.kind == "table" else t.text
            if t.kind == "ident" and qual.lower() in local:
                add(Ref("extension", qual, nxt.value), i)   # VAR table column: t[Hours]
            else:
                tab = res.table(qual)
                if tab is None:
                    add(Ref("unresolved", qual, nxt.value), i)
                elif res.column(tab, nxt.value):
                    add(Ref("column", tab, res.column(tab, nxt.value)), i)
                elif res.measure(nxt.value):
                    mt, mn = res.measure(nxt.value)
                    add(Ref("measure", mt, mn), i)
                else:
                    add(Ref("unresolved", tab, nxt.value), i)
            i += 2
            continue
        elif t.kind == "bracket":
            add(_bare(t.value, home, res, row_home, row_tables(), extension), i)
        elif t.kind == "table":
            tab = res.table(t.value)
            add(Ref("table", tab, tab) if tab else Ref("unresolved", t.value, ""), i)
        elif t.kind == "ident":
            low = t.text.lower()
            if nxt is not None and nxt.kind == "op" and nxt.text == "(":
                if low in res.functions:
                    add(Ref("function", "", res.functions[low]), i)
            elif low == "dt" and nxt is not None and nxt.kind == "str" and nxt.start == t.end:
                pass  # dt"2026-01-31" date literal
            elif low not in local and res.table(t.text):
                tab = res.table(t.text)
                add(Ref("table", tab, tab), i)
        i += 1
    return out


def _bare(name: str, home: str, res: Resolver, row_home: bool, row_tables: list[str], extension: set[str]) -> Ref:
    if row_home and res.column(home, name):
        return Ref("column", res.table(home) or home, res.column(home, name))
    hit = res.measure(name)
    if hit:
        return Ref("measure", hit[0], hit[1])
    for rt in row_tables:
        if res.column(rt, name):
            return Ref("column", rt, res.column(rt, name))
    if home and res.column(home, name):
        return Ref("column", res.table(home) or home, res.column(home, name))
    tabs = res.by_column.get(name.lower(), [])
    if len(tabs) == 1:
        return Ref("column", tabs[0], res.column(tabs[0], name))
    if name.lower() in extension:
        return Ref("extension", "", name)
    return Ref("unresolved", "", name)


def m(expr: str, res: Resolver, own: str = "") -> list[Ref]:
    """Shared expressions (queries, parameters) referenced by an M expression."""
    sig = lexers.significant(lexers.lex_m(expr))
    steps = set()
    for i, t in enumerate(sig[:-1]):
        if t.kind in ("ident", "qident") and sig[i + 1].kind == "op" and sig[i + 1].text == "=":
            steps.add((t.value or t.text).lower())
    out, seen = [], set()
    for i, t in enumerate(sig):
        if t.kind not in ("ident", "qident"):
            continue
        name = t.value if t.kind == "qident" else t.text
        low = name.lower()
        if low in steps or low == own.lower() or low not in res.expressions or low in seen:
            continue
        prev = sig[i - 1] if i else None
        if prev is not None and prev.kind == "op" and prev.text == "." and prev.end == t.start:
            continue
        seen.add(low)
        out.append(Ref("m_query", "", res.expressions[low], "m"))
    return out
