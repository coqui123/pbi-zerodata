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
TOPNLIKE = {"TOPN", "SAMPLE", "INDEX", "OFFSET"}
PREDICATE_HOSTS = {"FILTER", "CALCULATETABLE"}
TRANSPARENT_WRAPPERS = frozenset({"", "FILTER", "CALCULATETABLE", "KEEPFILTERS"})
COLUMN_PROJECTORS = frozenset({"VALUES", "DISTINCT", "ALL", "ALLSELECTED", "ALLNOBLANKROW"})


def _is_table_arg(fr: dict) -> bool:
    fn = fr.get("fn", "")
    arg = fr.get("arg", 0)
    if fn in TOPNLIKE:
        return arg == 1
    return fn in ITERATORS and arg == 0


def _is_predicate_pos(fr: dict) -> bool:
    fn = fr.get("fn", "")
    arg = fr.get("arg", 0)
    if fn == "FILTER":
        return arg >= 1
    if fn in ("CALCULATE", "CALCULATETABLE"):
        return arg >= 1
    if fn in TOPNLIKE:
        return arg >= 2
    if fn in ("GENERATE", "GENERATEALL"):
        # In DAX GENERATE(Table1, Table2), Table2 (arg 1) is evaluated in row context of Table1.
        # There is no boolean predicate argument, so this position is never a filter predicate.
        return False
    return fn in ITERATORS and arg >= 1



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

    def teaching_vetoed(k: int, ref: Ref) -> bool:
        for j in range(k + 1, len(stack)):
            g = stack[j]
            if g["fn"] in TOPNLIKE and g["arg"] == 0:
                return True
            if g["fn"] in PREDICATE_HOSTS and g["arg"] >= 1:
                if ref.kind == "table" and j == len(stack) - 1:
                    continue
                return True
            if g["fn"] in TOPNLIKE and g["arg"] >= 2:
                return True
            if g["fn"] in TOPNLIKE and g["arg"] == 1 and g.get("contested_by") and ref.table != g["contested_by"]:
                return True
        return False

    def add(ref: Ref, at: int, uncertain: bool = False, veto_teach: bool = False):
        tok = sig[at]
        qualified = at + 1 < n and sig[at + 1].kind == "bracket" and tok.kind in ("table", "ident")
        name = sig[at + 1] if qualified else tok
        out.bindings.append(Binding(ref, name.start, name.end, home, row_home,
                                    (tok.start, tok.end) if qualified else None,
                                    ref.kind == "unresolved" or uncertain))
        out.refs.append(ref)
        if ref.kind == "unresolved":
            out.unresolved.append(ref)
        if ref.kind in ("table", "column") and ref.table:
            for g in stack:
                if g["fn"] in TOPNLIKE and g["arg"] == 0 and not g.get("contested_by"):
                    g["contested_by"] = ref.table
        if veto_teach:
            pass
        else:
            for k, fr in enumerate(stack):
                if _is_table_arg(fr) and ref.table and ref.kind in ("table", "column"):
                    frames = stack[k + 1:]
                    cj_info = [(j, g) for j, g in enumerate(frames) if g["fn"] == "CROSSJOIN"]
                    if cj_info:
                        cj_idx, cj_fr = cj_info[-1]
                        between = frames[:cj_idx]
                        if not any(_is_predicate_pos(g) for g in between):
                            fr["has_crossjoin"] = True
                            if (
                                fr.get("src_var")
                                or cj_fr.get("src_var")
                                or any(g.get("src_var") for g in between)
                                or not all(g["fn"] in TRANSPARENT_WRAPPERS for g in between)
                                or fr.get("fn") in TOPNLIKE
                            ):
                                fr["cj_opaque"] = True
                            cj_arg = cj_fr["arg"]
                            sub = frames[cj_idx + 1:]
                            if any(_is_predicate_pos(g) for g in sub):
                                pass
                            elif teaching_vetoed(k, ref):
                                cj_fr.setdefault("ops", {}).setdefault(cj_arg, []).append(("opaque", None, None))
                                cj_fr["opaque"] = True
                            elif len(sub) == 1 and sub[0]["fn"] in COLUMN_PROJECTORS and ref.kind == "column":
                                cj_fr.setdefault("ops", {}).setdefault(cj_arg, []).append(("col", ref.table, ref.name))
                            elif not sub and ref.kind == "table":
                                prev = sig[at - 1] if at else None
                                if prev is not None and prev.text in ("(", ","):
                                    cj_fr.setdefault("ops", {}).setdefault(cj_arg, []).append(("tab", ref.table, None))
                                else:
                                    cj_fr.setdefault("ops", {}).setdefault(cj_arg, []).append(("opaque", None, None))
                                    cj_fr["opaque"] = True
                            elif all(g["arg"] == 0 and g["fn"] in (TRANSPARENT_WRAPPERS | COLUMN_PROJECTORS) for g in sub) and ref.kind == "table":
                                cj_fr.setdefault("ops", {}).setdefault(cj_arg, []).append(("tab", ref.table, None))
                            else:
                                cj_fr.setdefault("ops", {}).setdefault(cj_arg, []).append(("opaque", None, None))
                                cj_fr["opaque"] = True
                    elif not teaching_vetoed(k, ref):
                        if fr["row_table"] is None and not fr.get("has_crossjoin"):
                            fr["row_table"] = ref.table
                        if any(_is_predicate_pos(g) for g in frames) or any(g.get("arg", 0) != 0 for g in frames[:-1]):
                            pass
                        elif (
                            fr.get("src_var")
                            or any(g.get("src_var") for g in frames)
                            or fr.get("fn") in TOPNLIKE
                        ):
                            fr["source_kind"] = "unknown"
                        elif len(frames) == 0 and ref.kind == "table":
                            if fr.get("source_kind") is None:
                                fr["source_kind"] = "whole"
                            elif fr.get("source_kind") != "whole" or fr.get("row_table") != ref.table:
                                fr["source_kind"] = "unknown"
                        elif all(g["fn"] in TRANSPARENT_WRAPPERS and g.get("arg", 0) == 0 for g in frames) and ref.kind == "table":
                            if fr.get("source_kind") is None:
                                fr["source_kind"] = "whole"
                            elif fr.get("source_kind") != "whole" or fr.get("row_table") != ref.table:
                                fr["source_kind"] = "unknown"
                        elif (
                            all(g["fn"] in TRANSPARENT_WRAPPERS and g.get("arg", 0) == 0 for g in frames[:-1])
                            and len(frames) >= 1
                            and frames[-1]["fn"] in COLUMN_PROJECTORS
                            and ref.kind == "column"
                        ):
                            if fr.get("source_kind") is None or fr.get("source_kind") == "projected":
                                fr["source_kind"] = "projected"
                                fr.setdefault("projected_cols", set()).add((ref.table, ref.name))
                            else:
                                fr["source_kind"] = "unknown"
                        else:
                            fr["source_kind"] = "unknown"
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
                popped = stack.pop()
                if popped.get("fn") == "CROSSJOIN":
                    total = popped["arg"] + 1
                    ops = popped.get("ops", {})
                    is_opaque = (
                        popped.get("opaque", False)
                        or popped.get("src_var", False)
                    )
                    for a in range(total):
                        if a not in ops or any(op[0] == "opaque" for op in ops[a]):
                            is_opaque = True
                    path_from_top = []
                    for fr in reversed(stack):
                        if _is_predicate_pos(fr):
                            break
                        if _is_table_arg(fr):
                            fr["has_crossjoin"] = True
                            fr["cj_ops"] = ops
                            if (
                                fr.get("src_var", False)
                                or is_opaque
                                or any(g.get("src_var", False) for g in path_from_top)
                                or not all(g["fn"] in TRANSPARENT_WRAPPERS for g in path_from_top)
                                or fr.get("fn") in TOPNLIKE
                            ):
                                fr["cj_opaque"] = True
                        path_from_top.append(fr)
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
            ref, source, supplier = _bare_full(t.value, home, res, row_home, row_tables(), extension)
            taint = False
            supplier_idx = None
            ctx = [(idx, fr) for idx, fr in enumerate(stack) if fr["fn"] in ROW_CONTEXT and fr["arg"] >= 1]
            if any(fr.get("has_crossjoin") for _, fr in ctx):
                def _check_outer_competes(chosen_table: str, chosen_column: str, curr_idx: int) -> bool:
                    for j, g in ctx:
                        if j < curr_idx:
                            if g.get("has_crossjoin"):
                                for op_list in g.get("cj_ops", {}).values():
                                    for op in op_list:
                                        if op[1]:
                                            if op[0] == "col" and op[2].lower() == t.value.lower():
                                                if op[1].lower() != chosen_table.lower() or op[2].lower() != chosen_column.lower():
                                                    return True
                                            elif op[0] == "tab" and res.column(op[1], t.value):
                                                if op[1].lower() != chosen_table.lower() or res.column(op[1], t.value).lower() != chosen_column.lower():
                                                    return True
                            else:
                                sk = g.get("source_kind")
                                if sk == "whole" and g.get("row_table"):
                                    g_tab = g["row_table"]
                                    if res.column(g_tab, t.value):
                                        if g_tab.lower() != chosen_table.lower() or res.column(g_tab, t.value).lower() != chosen_column.lower():
                                            return True
                                elif sk == "projected" and "projected_cols" in g:
                                    for tab, c in g["projected_cols"]:
                                        if c.lower() == t.value.lower():
                                            if tab.lower() != chosen_table.lower() or c.lower() != chosen_column.lower():
                                                return True
                                else:
                                    return True
                    if row_home and bool(home):
                        home_col = res.column(home, t.value)
                        if home_col and (home.lower() != chosen_table.lower() or home_col.lower() != chosen_column.lower()):
                            return True
                    return False

                for idx, fr in reversed(ctx):
                    if fr.get("has_crossjoin"):
                        supplying_ops = []
                        ops = fr.get("cj_ops", {})
                        for a, op_list in ops.items():
                            for op_type, tab, col_name in op_list:
                                if op_type == "col" and col_name and col_name.lower() == t.value.lower():
                                    supplying_ops.append((a, tab, col_name))
                                elif op_type == "tab" and tab and res.column(tab, t.value):
                                    supplying_ops.append((a, tab, res.column(tab, t.value)))
                        if supplying_ops:
                            distinct_ops = {a for a, _, _ in supplying_ops}
                            chosen_tab = supplying_ops[0][1]
                            col = supplying_ops[0][2]
                            ref = Ref("column", chosen_tab, col)
                            source = "row"
                            supplier = chosen_tab
                            supplier_idx = idx
                            outer_competes = _check_outer_competes(chosen_tab, col, idx)
                            if (
                                len(distinct_ops) > 1
                                or fr.get("cj_opaque")
                                or t.value.lower() in extension
                                or res.measure(t.value) is not None
                                or outer_competes
                            ):
                                taint = True
                            break
                        else:
                            continue
                    else:
                        sk = fr.get("source_kind")
                        if sk == "projected":
                            matching = [c for tab, c in fr.get("projected_cols", set()) if c.lower() == t.value.lower()]
                            if not matching:
                                continue
                            col = matching[0]
                            rt = [tab for tab, c in fr["projected_cols"] if c.lower() == t.value.lower()][0]
                        elif sk == "whole" and fr.get("row_table"):
                            rt = fr["row_table"]
                            col = res.column(rt, t.value)
                            if not col:
                                continue
                        else:
                            taint = True
                            break

                        ref = Ref("column", rt, col)
                        source = "row"
                        supplier = rt
                        supplier_idx = idx
                        outer_competes = _check_outer_competes(rt, col, idx)
                        if (
                            outer_competes
                            or any(g.get("cj_opaque") for j, g in ctx if j > idx and g.get("has_crossjoin"))
                            or t.value.lower() in extension
                            or res.measure(t.value) is not None
                        ):
                            taint = True
                        break

            if any(fr.get("cj_opaque") for fr in stack if fr.get("has_crossjoin")):
                taint = True
            elif source not in ("extension", "measure", "row_home"):
                if supplier_idx is None and any(fr.get("has_crossjoin") for _, fr in ctx):
                    taint = True
            untaught = [idx for idx, fr in ctx if not fr["row_table"] and not fr.get("has_crossjoin")]
            if untaught:
                if source == "row":
                    sup = [supplier_idx] if supplier_idx is not None else [idx for idx, fr in ctx if fr["row_table"] == supplier]
                    taint = taint or (not sup) or any(u > max(sup) for u in untaught)
                else:
                    taint = True
            add(ref, i, uncertain or taint, veto_teach=taint)
        elif t.kind == "table":
            tab = res.table(t.value)
            add(Ref("table", tab, tab) if tab else Ref("unresolved", t.value, ""), i)
        elif t.kind == "ident":
            low = t.text.lower()
            if t.text.upper() == "VAR":
                for g in reversed(stack):
                    if _is_predicate_pos(g):
                        break
                    g["src_var"] = True
                    if g["fn"] == "CROSSJOIN":
                        g.setdefault("ops", {}).setdefault(g["arg"], []).append(("opaque", None, None))
                        g["opaque"] = True
                    elif g.get("has_crossjoin"):
                        g["cj_opaque"] = True
                    else:
                        g["opaque"] = True
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
    return _bare_full(name, home, res, row_home, row_tables, extension)[0]


def _bare_full(name: str, home: str, res: Resolver, row_home: bool, row_tables: list[str], extension: set[str]):
    for rt in row_tables:
        if res.column(rt, name):
            return Ref("column", rt, res.column(rt, name)), "row", rt
    if row_home and res.column(home, name):
        return Ref("column", res.table(home) or home, res.column(home, name)), "row_home", ""
    hit = res.measure(name)
    if hit:
        return Ref("measure", hit[0], hit[1]), "measure", ""
    if home and res.column(home, name):
        return Ref("column", res.table(home) or home, res.column(home, name)), "home", ""
    tabs = res.by_column.get(name.lower(), [])
    if len(tabs) == 1:
        return Ref("column", tabs[0], res.column(tabs[0], name)), "unique", ""
    if name.lower() in extension:
        return Ref("extension", "", name), "extension", ""
    return Ref("unresolved", "", name), "unresolved", ""


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
                        if inner and all(tok.kind == "ident" for tok in inner):
                            start, end = inner[0].start, inner[-1].end
                            nm = expr[start - base:end - base]
                            if nm in res.expressions and nm != own:
                                found.append((i + 2, nm, False, (start, end)))
                        elif inner:
                            uncertain = True
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
        # Merge a plain scan for skipped declarations/selectors with decoded
        # bindings, preserving their explicit spans and local flags on duplicates.
        seen = {(nm, explicit_span or span(i)) for i, nm, local, explicit_span in found if not local}
        for i in range(len(sig)):
            nm = possible_name(i)
            key = (nm, span(i))
            if nm in res.expressions and nm != own and key not in seen:
                found.append((i, nm, False, None))
                seen.add(key)
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
