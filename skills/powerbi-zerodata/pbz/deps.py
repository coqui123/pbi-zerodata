"""Reference resolution: which model objects a DAX or M expression depends on. Names only, no values.

DAX rules follow Tabular Editor 2's FormulaFixup / DaxDependencyHelper (MIT):
- 'T'[x] or T[x] is a column of T, else a measure (qualified measure reference), else unresolved.
- a bare [x] resolves iterator row contexts innermost-first, then a row-context home column,
  then a measure, then a column of the home table, then the only column of that name in the model,
  then an extension column (ADDCOLUMNS / SELECTCOLUMNS name) or unresolved.
- calculated columns and RLS filters have a row context on the home table, so a bare
  [x] there is a home column first.
- ident( is a function call (a user-defined function when the model defines one of that name);
  a bare ident is a table only when the model has a table of that name and no VAR shadows it.
- mirror tokens «...» are opaque literals, never references.
M rules: identifiers name shared expressions case-sensitively, unless a lexical let, record,
parameter or each binding shadows them. Uncertain scopes retain possible references.
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


@dataclass(frozen=True)
class Binding:
    """One occurrence, with original spans and the context used to bind it.

    start/end cover the name (the bracket for DAX columns/measures); qualifier
    covers a DAX table prefix separately. An uncertain binding must not be edited.
    """
    ref: Ref
    start: int
    end: int
    home: str = ""
    row_home: bool = False
    qualifier: tuple[int, int] | None = None
    uncertain: bool = False


@dataclass
class DaxDeps:
    bindings: list[Binding] = field(default_factory=list)
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
        self.expressions = {e: e for e in model.expressions}

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


def dax_bindings(expr: str, home: str, res: Resolver, row_home: bool = False,
                 function: bool = False, base: int = 0) -> DaxDeps:
    """References made by one DAX expression whose home table is `home`.

    row_home: the expression has a row context on the home table (calculated column or RLS filter).
    function: the text is a UDF definition with a parameter list.
    """
    header = ""
    original_length = len(expr)
    if function:
        header, expr = _function_header(expr)
    sig = lexers.significant(lexers.lex_dax(expr, base + original_length - len(expr)))
    local = _vars_and_params(sig, header)
    extension = {t.value.lower() for t in sig if t.kind == "str"}
    out = DaxDeps()
    stack: list[dict] = []   # {"fn", "arg", "row_table"}
    rel_calls: list[dict] = []
    n = len(sig)

    def add(ref: Ref, at: int, uncertain: bool = False):
        tok = sig[at]
        qualified = at + 1 < n and sig[at + 1].kind == "bracket" and tok.kind in ("table", "ident")
        name = sig[at + 1] if qualified else tok
        out.bindings.append(Binding(ref, name.start, name.end, home, row_home,
                                    (tok.start, tok.end) if qualified else None,
                                    ref.kind == "unresolved" or uncertain))
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
            prev = sig[i - 1] if i else None
            uncertain = bool(prev and prev.end == t.start and prev.kind == "ident" and prev.text.upper() != "VAR"
                             and not res.table(prev.text))
            add(_bare(t.value, home, res, row_home, row_tables(), extension), i, uncertain)
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


def dax(expr: str, home: str, res: Resolver, row_home: bool = False, function: bool = False) -> DaxDeps:
    """Dependency projection of the shared per-occurrence DAX resolver."""
    return dax_bindings(expr, home, res, row_home, function)


def dax_region(text, reg, res: Resolver) -> DaxDeps:
    """Resolve a TMDL region, including bare RLS refs and UDF header offsets."""
    node = reg.node
    row_home = False
    function = False
    while node is not None:
        row_home |= node.kind in {"column", "tablePermission"}
        function |= node.kind == "function"
        if node.kind in {"table", "tablePermission"}:
            break
        node = node.parent
    home = node.name if node else ""
    return dax_bindings(text[reg.start:reg.end], home, res, row_home, function, reg.start)


def _bare(name: str, home: str, res: Resolver, row_home: bool, row_tables: list[str], extension: set[str]) -> Ref:
    for rt in row_tables:
        if res.column(rt, name):
            return Ref("column", rt, res.column(rt, name))
    if row_home and res.column(home, name):
        return Ref("column", res.table(home) or home, res.column(home, name))
    hit = res.measure(name)
    if hit:
        return Ref("measure", hit[0], hit[1])
    if home and res.column(home, name):
        return Ref("column", res.table(home) or home, res.column(home, name))
    tabs = res.by_column.get(name.lower(), [])
    if len(tabs) == 1:
        return Ref("column", tabs[0], res.column(tabs[0], name))
    if name.lower() in extension:
        return Ref("extension", "", name)
    return Ref("unresolved", "", name)


def m_bindings(expr: str, res: Resolver, own: str = "", base: int = 0,
               *, _defined: set[str] | None = None) -> list[Binding]:
    """Lexical M scopes: recursive let/record bindings, lambda parameters and each's _.

    Let names are visible throughout their let; record fields only inside that
    record. Initializers exclude their own binding (exclusive M identifiers). Field selectors are not shared names. Unsupported or malformed scope
    syntax retains every possible shared reference as uncertain, even if local.
    M identifiers and shared names are deliberately case-sensitive.
    """
    sig = lexers.significant(lexers.lex_m(expr, base))
    pairs, stack = {}, []
    uncertain = False
    for i, tok in enumerate(sig):
        if tok.text in ("(", "[", "{") and tok.kind == "op":
            stack.append(i)
        elif tok.text in (")", "]", "}") and tok.kind == "op":
            if not stack or sig[stack[-1]].text != {")": "(", "]": "[", "}": "{"}[tok.text]:
                uncertain = True
            else:
                pairs[stack.pop()] = i
    uncertain |= bool(stack)
    found = []

    def name(i):
        t = sig[i]
        return (t.value if t.kind == "qident" else t.text) if t.kind in ("ident", "qident") else None

    def possible_name(i):
        return sig[i].value if sig[i].kind == "bracket" else name(i)

    def span(i):
        t = sig[i]
        if t.kind == "bracket":
            start = t.start + 1 + len(t.text[1:]) - len(t.text[1:].lstrip())
            return start, start + len(t.value)
        return t.start, t.end

    def parts(lo, hi):
        # Split only at this level: a nested let owns commas until its matching in.
        result, begin, depth, i = [], lo, 0, lo
        while i < hi:
            if i in pairs:
                i = pairs[i] + 1
                continue
            t = sig[i]
            if t.kind == "ident" and t.text == "let":
                depth += 1
            elif t.kind == "ident" and t.text == "in":
                depth -= 1
            elif t.text == "," and depth == 0:
                result.append((begin, i))
                begin = i + 1
            i += 1
        return result + [(begin, hi)]

    def definitions(ranges):
        nonlocal uncertain
        names, bodies = set(), []
        for lo, hi in ranges:
            if lo + 1 < hi and name(lo) is not None and sig[lo + 1].text == "=":
                names.add(name(lo))
                bodies.append((lo + 2, hi, name(lo)))
            else:
                uncertain = True
                bodies.append((lo, hi, None))
        if _defined is not None:
            _defined.update(names)
        return names, bodies

    def walk(lo, hi, local):
        nonlocal uncertain
        i = lo
        while i < hi:
            t = sig[i]
            if t.kind == "ident" and t.text == "if":
                # Branch expressions have separate lexical boundaries: a let or
                # lambda in the then branch cannot hide names in the else branch.
                depth, then, j = 1, None, i + 1
                while j < hi:
                    if j in pairs:
                        j = pairs[j] + 1
                        continue
                    word = sig[j].text if sig[j].kind == "ident" else ""
                    if word == "if":
                        depth += 1
                    elif word == "then" and depth == 1 and then is None:
                        then = j
                    elif word == "else":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                if then is None or j == hi:
                    uncertain = True
                    i += 1
                    continue
                walk(i + 1, then, local)
                walk(then + 1, j, local)
                walk(j + 1, hi, local)
                return
            if t.kind == "ident" and t.text == "let":
                depth, j = 1, i + 1
                while j < hi:
                    if j in pairs:
                        j = pairs[j] + 1
                        continue
                    if sig[j].kind == "ident":
                        if sig[j].text == "let":
                            depth += 1
                        elif sig[j].text == "in":
                            depth -= 1
                            if depth == 0:
                                break
                    j += 1
                if j == hi:
                    uncertain = True
                    i += 1
                    continue
                names, bodies = definitions(parts(i + 1, j))
                for a, b, defined in bodies:
                    walk(a, b, local | (names - {defined}))
                walk(j + 1, hi, local | names)
                return
            if t.kind == "ident" and t.text == "each":
                if _defined is not None:
                    _defined.add("_")
                walk(i + 1, hi, local | {"_"})
                return
            if i in pairs:
                end = pairs[i]
                ranges = parts(i + 1, end)
                if t.text == "(" and end + 1 < hi and sig[end + 1].text == "=>":
                    params = set()
                    for a, b in ranges:
                        if a < b and sig[a].text == "optional":
                            a += 1
                        if a < b and name(a):
                            params.add(name(a))
                            if any(possible_name(k) in res.expressions for k in range(a + 1, b)):
                                uncertain = True
                        elif a != b:
                            uncertain = True
                    if _defined is not None:
                        _defined.update(params)
                    walk(end + 2, hi, local | params)
                    return
                if t.text == "[":
                    # Quoted selectors [#"Field"] have no '=' and aren't records.
                    record = any(a + 1 < b and sig[a + 1].text == "=" for a, b in ranges)
                    if record:
                        names, bodies = definitions(ranges)
                        for a, b, defined in bodies:
                            walk(a, b, local | (names - {defined}))
                    elif not (i + 2 == end and name(i + 1)):
                        uncertain = True
                else:
                    for a, b in ranges:
                        walk(a, b, local)
                i = end + 1
                continue
            if t.text == "#":
                construct = name(i + 1) if i + 1 < hi else None
                if construct == "shared" and i + 2 < hi:
                    selector = sig[i + 2]
                    if selector.kind == "bracket":
                        # Lex the inner text to strip only surrounding M trivia;
                        # the edit span must leave comments and whitespace intact.
                        inner = lexers.significant(lexers.lex_m(selector.text[1:-1], selector.start + 1))
                        if len(inner) == 1 and inner[0].kind == "ident":
                            ident = inner[0]
                            nm = ident.text
                            if nm in res.expressions and nm != own:
                                found.append((i + 2, nm, False, (ident.start, ident.end)))
                        i += 3
                        continue
                    if (i + 4 < hi and selector.text == "[" and pairs.get(i + 2) == i + 4
                            and sig[i + 3].kind == "qident"):
                        nm = sig[i + 3].value
                        if nm in res.expressions and nm != own:
                            found.append((i + 3, nm, False, None))
                        i += 5
                        continue
                if construct not in {"date", "time", "datetime", "datetimezone", "duration", "table", "binary"}:
                    uncertain = True
                else:
                    i += 2
                    continue
            if t.kind == "ident" and t.text == "type":
                # Operators and expression keywords end a type at this level.
                # Nested containers still belong to the type and are scanned.
                j = i + 1
                while j < hi and sig[j].text not in {",", ")", "]", "}", "meta"}:
                    tok = sig[j]
                    if ((tok.kind == "op" and j not in pairs)
                            or (tok.kind == "ident" and tok.text.lower() in {
                                "and", "or", "not", "is", "as", "in", "if", "then", "else", "each", "let"})):
                        break
                    end = pairs.get(j, j)
                    if any(possible_name(k) in res.expressions for k in range(j, end + 1)):
                        uncertain = True
                    j = end + 1
                i = j
                continue
            nm = name(i)
            if t.text in ("=>", "section", "shared", "try", "catch", "meta", "@"):
                uncertain = True
            if nm in res.expressions and nm != own:
                prev = sig[i - 1] if i else None
                if not (prev and prev.text == "."):
                    found.append((i, nm, nm in local, None))
            i += 1

    walk(0, len(sig), set())
    if uncertain:
        # Include tokens skipped as declarations or selectors too: uncertainty must
        # never make the graph less conservative than a plain identifier scan.
        found = [(i, possible_name(i), False, None) for i in range(len(sig))
                 if possible_name(i) in res.expressions and possible_name(i) != own]
    return [Binding(Ref("m_query", "", nm, "m"), *(explicit_span or span(i)),
                    uncertain=uncertain) for i, nm, local, explicit_span in found if not local]


def m(expr: str, res: Resolver, own: str = "") -> list[Ref]:
    """Dependency projection of the shared M tracker."""
    return list(dict.fromkeys(b.ref for b in m_bindings(expr, res, own)))


def m_defined_names(expr: str) -> set[str]:
    """Local declarations in an M region, using the binding tracker's scope walk."""
    names: set[str] = set()
    m_bindings(expr, _EmptyMResolver(), _defined=names)
    return names


class _EmptyMResolver:
    expressions = {}
