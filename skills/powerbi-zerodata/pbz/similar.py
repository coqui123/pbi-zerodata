"""`pbz similar`: duplicate and near-duplicate measures from normalized DAX. Names and scores only.

canon(): significant tokens with functions uppercased, names casefolded, bare [x] resolved to T[x] or
the measure, numbers as exact decimals, strings and mirror tokens as opaque literal slots, VAR names renamed
$v1.., outer parentheses and a trivial `VAR x = E RETURN x` stripped. A filterless top-level
CALCULATE(E) is stripped for candidate discovery only (it performs context transition), never for
"identical". Groups: identical (same canon, literals compared exactly), alias (the body is one other
measure), same_shape (same canon once literals, columns and measures become slots: a calculation
group candidate). Fuzzy pairs (difflib) are blocked by shared token 3-grams. Literal values are never
shown, only that a literal differs.
"""
from __future__ import annotations

import difflib
from decimal import Decimal, InvalidOperation
import hashlib
import os
from dataclasses import dataclass, field

from . import deps, lexers, tmdl
from .common import PbzError

LIT_KINDS = ("str", "num", "pbztok")
SLOT_KINDS = ("lit", "col", "meas")
MIN_TOKENS = 5
MIN_SHAPE_TOKENS = 8


@dataclass
class Canon:
    toks: list = field(default_factory=list)   # [(token, kind, value)]; kind "" | lit | col | meas
    uncertain: bool = False

    def words(self, literals: str = "hash") -> list[str]:
        """Token strings. literals: "hash" (private exact compare), "slot" (any literal), "shape" (all slots)."""
        out = []
        for tok, kind, val in self.toks:
            if kind == "lit":
                out.append("LIT:" + val if literals == "hash" else "LIT")
            elif kind in ("col", "meas") and literals == "shape":
                out.append(kind.upper())
            else:
                out.append(tok)
        return out

    def refs(self) -> set[str]:
        return {val for _t, kind, val in self.toks if kind in ("col", "meas")}

    def measures(self) -> set[str]:
        return {val for _t, kind, val in self.toks if kind == "meas"}


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _num(text: str) -> str:
    try:
        sign, digits, exponent = Decimal(text).as_tuple()
        digits = list(digits)
        while digits and digits[-1] == 0:
            digits.pop()
            exponent += 1
        if not digits:
            return "N0"
        return "N" + ("-" if sign else "") + "".join(map(str, digits)) + "e" + str(exponent)
    except InvalidOperation:
        return "N" + text


def canon(expr: str, home: str, res: deps.Resolver) -> Canon:
    sig = lexers.significant(lexers.lex_dax(expr))
    bindings = deps.dax_bindings(expr, home, res)
    by_span = {(b.start, b.end): b for b in bindings.bindings}
    var_names: dict[str, str] = {}
    var_rhs: dict[str, list] = {}
    for i, t in enumerate(sig):
        if t.kind == "ident" and t.text.upper() == "VAR" and i + 1 < len(sig) and sig[i + 1].kind == "ident":
            var_names.setdefault(sig[i + 1].text.lower(), f"$v{len(var_names) + 1}")
            if i + 2 < len(sig) and sig[i + 2].kind == "op" and sig[i + 2].text == "=":
                rhs, depth = [], 0
                for u in sig[i + 3:]:
                    if u.kind == "op" and u.text in ("(", "{"):
                        depth += 1
                    elif u.kind == "op" and u.text in (")", "}"):
                        depth -= 1
                    if depth == 0 and u.kind == "ident" and u.text.upper() in ("RETURN", "VAR"):
                        break
                    rhs.append(u)
                var_rhs.setdefault(sig[i + 1].text.lower(), []).append(rhs)

    SCALAR_FUNCS = {"MAX", "MIN", "SUM", "AVERAGE", "COUNT", "COUNTA", "COUNTROWS", "COUNTAX",
                    "DISTINCTCOUNT", "DISTINCTCOUNTNOBLANK", "SUMX", "AVERAGEX", "MINX", "MAXX",
                    "COUNTX", "PRODUCTX", "GEOMEANX", "MEDIANX", "CONCATENATEX", "RANKX",
                    "ABS", "INT", "ROUND", "ROUNDUP", "ROUNDDOWN", "TRUNC", "MOD", "DIVIDE",
                    "POWER", "SQRT", "EXP", "LN", "LOG", "LOG10", "SIGN", "CEILING", "FLOOR",
                    "MROUND", "VALUE", "FORMAT", "LEN", "UPPER", "LOWER", "TRIM", "LEFT", "RIGHT",
                    "MID", "SUBSTITUTE", "REPLACE", "CONCATENATE", "EXACT", "FIND", "SEARCH",
                    "YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND", "DATE", "TIME", "TODAY",
                    "NOW", "UTCNOW", "EDATE", "EOMONTH", "DATEDIFF", "YEARFRAC", "WEEKDAY",
                    "WEEKNUM", "NETWORKDAYS", "BLANK", "TRUE", "FALSE", "ISBLANK", "ISERROR",
                    "ISNUMBER", "ISTEXT", "ISLOGICAL", "ISNONTEXT", "ISEMPTY", "HASONEVALUE",
                    "HASONEFILTER", "SELECTEDVALUE", "USERNAME", "USERPRINCIPALNAME", "CUSTOMDATA",
                    "CALCULATE", "RAND", "RANDBETWEEN", "PI", "CURRENCY"}

    def _call_span(rhs, open_idx):
        depth = 0
        for j in range(open_idx, len(rhs)):
            u = rhs[j]
            if u.kind == "op" and u.text in ("(", "{"):
                depth += 1
            elif u.kind == "op" and u.text in (")", "}"):
                depth -= 1
                if depth == 0:
                    return j
        return None

    def _is_scalar_expr(rhs, scalar_vars) -> bool:
        if not rhs:
            return False
        i = 0
        while i < len(rhs):
            u = rhs[i]
            nxt = rhs[i + 1] if i + 1 < len(rhs) else None
            if u.kind in ("num", "str", "bracket"):
                i += 1
                continue
            if u.kind == "op":
                if u.text == "{":
                    return False
                if u.text == "(":
                    j = _call_span(rhs, i)
                    if j is None or not _is_scalar_expr(rhs[i + 1:j], scalar_vars):
                        return False
                    i = j + 1
                    continue
                i += 1
                continue
            if u.kind == "table":
                if nxt is not None and nxt.kind == "bracket":
                    i += 2
                    continue
                return False
            if u.kind == "ident":
                if nxt is not None and nxt.kind == "bracket":
                    i += 2
                    continue
                up = u.text.upper()
                if nxt is not None and nxt.kind == "op" and nxt.text == "(":
                    j = _call_span(rhs, i + 1)
                    if j is None:
                        return False
                    if up in SCALAR_FUNCS:
                        i = j + 1
                        continue
                    if up in ("IF", "IFERROR", "SWITCH", "COALESCE") \
                            and _is_scalar_expr(rhs[i + 2:j], scalar_vars):
                        i = j + 1
                        continue
                    return False
                if u.text.lower() in scalar_vars or up in ("AND", "OR", "NOT", "IN"):
                    i += 1
                    continue
                return False
            return False
        return True

    scalar_vars: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, rhss in var_rhs.items():
            if name not in scalar_vars and all(_is_scalar_expr(rhs, scalar_vars) for rhs in rhss):
                scalar_vars.add(name)
                changed = True
    table_vars = set(var_rhs) - scalar_vars
    extension = {t.value.lower() for t in sig if t.kind == "str"}
    toks: list[tuple[str, str, str]] = []
    uncertain = False
    stack: list[dict] = []   # {"fn", "arg", "row_table"}: mirrors the deps.dax_bindings frames

    PREDICATE_EXEMPT = {"FILTER", "CALCULATETABLE", "ADDCOLUMNS"}
    CONDITIONALS = {"IF", "IFERROR", "SWITCH", "COALESCE"}
    TABLE_INPUT_ARG0 = {"VALUES", "DISTINCT", "ALL", "ALLEXCEPT", "ALLNOBLANKROW", "ALLSELECTED",
                        "FILTER", "CALCULATETABLE", "SUMMARIZE", "SUMMARIZECOLUMNS", "ADDCOLUMNS",
                        "SELECTCOLUMNS", "GENERATE", "UNION", "INTERSECT", "EXCEPT", "CROSSJOIN",
                        "TREATAS", "NATURALINNERJOIN", "NATURALLEFTOUTERJOIN", "GROUPBY",
                        "KEEPFILTERS", "FILTERS"}
    KNOWN_TABLE_FRAMES = TABLE_INPUT_ARG0 | {"TOPN", "SAMPLE", "INDEX", "OFFSET", "GENERATEALL",
                                              "DATEADD", "DATESYTD", "DATESMTD", "DATESQTD",
                                              "DATESBETWEEN", "SAMEPERIODLASTYEAR"}

    X_ITERATORS = {"SUMX", "AVERAGEX", "MINX", "MAXX", "COUNTX", "COUNTAX", "PRODUCTX",
                   "CONCATENATEX", "RANKX", "MEDIANX", "PERCENTILEX.INC", "PERCENTILEX.EXC",
                   "GEOMEANX", "STDEVX.P", "STDEVX.S", "VARX.P", "VARX.S"}

    def _is_table_frame(g) -> bool:
        return g["fn"] in KNOWN_TABLE_FRAMES or g["fn"] in X_ITERATORS

    def _source_frame(idx):
        for g in reversed(stack[idx + 1:]):
            if _is_table_frame(g):
                return g
        return None

    def _taint_frame(idx):
        for g in reversed(stack[idx + 1:]):
            if g["fn"] and g["fn"] not in CONDITIONALS:
                return g
        return None

    def _crossing_ok(p, d) -> bool:
        if p["fn"] in CONDITIONALS or p["fn"] == "":
            return True
        if p["fn"] == "WINDOW":
            return d in (2, 3, 4)
        if p["fn"] == "RANK":
            return d in (0, 1)
        if p["fn"] == "ROWNUMBER":
            return d == 0
        if p["fn"] in ("TOPN", "SAMPLE", "INDEX", "OFFSET"):
            return d == 1
        if p["fn"] in ("UNION", "INTERSECT", "EXCEPT", "CROSSJOIN", "GENERATE", "GENERATEALL",
                       "NATURALINNERJOIN", "NATURALLEFTOUTERJOIN"):
            return True
        if p["fn"] in TABLE_INPUT_ARG0 and d == 0:
            return True
        if _is_table_frame(p) and d == 0:
            return True
        return False

    def _first_nonsource(frames, last_d):
        for j, p in enumerate(frames):
            d = frames[j + 1]["parent_arg"] if j + 1 < len(frames) else last_d
            if not _crossing_ok(p, d):
                return p, d
        return None

    def _in_source_region(idx, last_d) -> bool:
        frames = stack[idx + 1:]
        if not frames:
            return True
        return _first_nonsource(frames, last_d) is None

    TOPN_LIKE = ("TOPN", "SAMPLE", "INDEX", "OFFSET")

    def _teaches_at(fr) -> bool:
        if fr["fn"] in TOPN_LIKE:
            return fr["arg"] == 1
        if fr["fn"] == "RANK":
            return fr["arg"] in (0, 1)
        if fr["fn"] == "WINDOW":
            return fr["arg"] in (2, 3, 4)
        return fr["arg"] == 0

    def _taint_cleared(fr, idx) -> bool:
        g = fr.get("taint")
        if g is None:
            return False
        frames = stack[idx + 1:]
        for j, p in enumerate(frames):
            if p is g:
                path = frames[j:]
                for k, q in enumerate(path):
                    d = path[k + 1]["parent_arg"] if k + 1 < len(path) else stack[-1]["arg"]
                    if q["fn"] in CONDITIONALS or q["fn"] == "":
                        continue
                    if q["fn"] in ("SELECTCOLUMNS", "SUMMARIZECOLUMNS", "SUMMARIZE", "GROUPBY",
                                   "ALLEXCEPT", "TREATAS"):
                        return False
                    if not _is_table_frame(q):
                        return False
                    lineage = 1 if q["fn"] in TOPN_LIKE else (4 if q["fn"] == "WINDOW" else 0)
                    if d != lineage:
                        return False
                return True
        return False

    PROJECTING = ("SELECTCOLUMNS", "SUMMARIZECOLUMNS", "SUMMARIZE", "GROUPBY",
                  "ALLEXCEPT", "TREATAS")
    NARROWING = ("VALUES", "DISTINCT", "ALL", "ALLEXCEPT", "ALLSELECTED", "ALLNOBLANKROW",
                 "FILTERS")

    def learn(table, bare: bool = False, column=None) -> None:
        if table:
            for idx, fr in enumerate(stack):
                if fr["fn"] in deps.ITERATORS and _teaches_at(fr) and not fr["poisoned"]:
                    trusted = _in_source_region(idx, stack[-1]["arg"] if len(stack) > idx + 1 else 0) \
                        and not any(g["fn"] == "TREATAS" for g in stack[idx + 1:])
                    eff_bare = bare and trusted
                    if fr["row_table"] is None:
                        fr["row_table"] = table
                        if any(g["fn"] in PROJECTING for g in stack[idx + 1:]):
                            fr["projected"] = True
                        src_fr = _source_frame(idx)
                        if not bare and column and src_fr is not None \
                                and src_fr["fn"] in NARROWING:
                            fr["only_columns"] = {column.lower()}
                            fr["only_src"] = src_fr
                        if not bare and column and fr["fn"] in (
                                "FIRSTNONBLANK", "LASTNONBLANK",
                                "FIRSTNONBLANKVALUE", "LASTNONBLANKVALUE"):
                            fr["only_columns"] = {column.lower()}
                            fr["only_src"] = fr
                        if not eff_bare:
                            g = _source_frame(idx)
                            exempt = trusted and g is not None and stack[-1] is g \
                                and g["arg"] == 0 and g["fn"] in TABLE_INPUT_ARG0 \
                                and g["fn"] != "TREATAS"
                            if not exempt:
                                target = g if g is not None else _taint_frame(idx)
                                if target is not None and fr.get("taint") is None:
                                    fr["taint"] = target
                    elif eff_bare and fr["row_table"] != table:
                        fr["row_table"] = None
                        fr["poisoned"] = True
                    if not bare and column and fr.get("only_src") is not None \
                            and _source_frame(idx) is fr.get("only_src"):
                        fr["only_columns"].add(column.lower())
                    if eff_bare and fr["row_table"] == table and fr.get("only_columns") is not None:
                        frames = stack[idx + 1:]
                        widens = True
                        for j, p in enumerate(frames):
                            d = frames[j + 1]["parent_arg"] if j + 1 < len(frames) else stack[-1]["arg"]
                            if p["fn"] in CONDITIONALS or p["fn"] == "":
                                continue
                            if p["fn"] in ("INTERSECT", "EXCEPT", "UNION"):
                                if d != 0:
                                    widens = False
                                    break
                                continue
                            if p["fn"] in ("CROSSJOIN", "NATURALINNERJOIN",
                                            "NATURALLEFTOUTERJOIN", "GENERATE", "GENERATEALL"):
                                continue
                            if not _crossing_ok(p, d):
                                widens = False
                                break
                        if widens:
                            fr["only_columns"] = None
                            fr["only_src"] = None
                    if eff_bare and fr.get("taint") is not None and fr["row_table"] == table \
                            and _taint_cleared(fr, idx):
                        fr["taint"] = None

    def _span_args(span):
        args, depth, cur = [], 0, []
        for u in span:
            if u.kind == "op" and u.text in ("(", "{"):
                depth += 1
            elif u.kind == "op" and u.text in (")", "}"):
                depth -= 1
            if depth == 0 and u.kind == "op" and u.text == ",":
                args.append(cur)
                cur = []
            else:
                cur.append(u)
        args.append(cur)
        return args

    _span_memo: dict = {}
    _cyclic_vars: set = set()
    for _nm, _rhss in var_rhs.items():
        _deps = {u.text.lower() for _r in _rhss for u in _r
                 if u.kind == "ident" and u.text.lower() in var_rhs}
        _walk, _seen = list(_deps), set()
        while _walk:
            _x = _walk.pop()
            if _x == _nm:
                _cyclic_vars.add(_nm)
                break
            if _x not in _seen:
                _seen.add(_x)
                _walk.extend({u.text.lower() for _r in var_rhs.get(_x, []) for u in _r
                              if u.kind == "ident" and u.text.lower() in var_rhs})

    def _span_table(span, seen=frozenset()):
        while len(span) >= 2 and span[0].kind == "op" and span[0].text == "(" \
                and _call_span(span, 0) == len(span) - 1:
            span = span[1:-1]
        if not span:
            return None, None
        if len(span) == 1:
            u = span[0]
            if u.kind == "table":
                return (res.table(u.value) or u.value), None
            if u.kind == "ident":
                if res.table(u.text):
                    return res.table(u.text), None
                low = u.text.lower()
                if low in seen:
                    return None, None
                if low in _span_memo:
                    return _span_memo[low]
                rhss = var_rhs.get(low)
                if rhss:
                    vals = {_span_table(r, seen | {low}) for r in rhss}
                    result = vals.pop() if len(vals) == 1 else (None, None)
                    if low not in _cyclic_vars:
                        _span_memo[low] = result
                    return result
            return None, None
        if span[0].kind == "ident" and span[1].kind == "op" and span[1].text == "(" \
                and _call_span(span, 1) == len(span) - 1:
            fn = span[0].text.upper()
            args = _span_args(span[2:-1])
            if fn in ("FILTER", "CALCULATETABLE", "KEEPFILTERS"):
                return _span_table(args[0], seen) if args else (None, None)
            if fn in ("VALUES", "DISTINCT", "FILTERS"):
                return _span_table(args[0], seen) if args else (None, None)
            if fn in ("ALL", "ALLSELECTED", "ALLNOBLANKROW"):
                if len(args) == 1:
                    return _span_table(args[0], seen)
                tab, cols = None, set()
                for a in args:
                    t2, c2 = _span_table(a, seen)
                    if t2 is None or (tab is not None and t2 != tab):
                        return None, None
                    tab = t2
                    if c2 is None:
                        return tab, None
                    kind, s = c2
                    if kind != "only":
                        return None, None
                    cols |= s
                return tab, ("only", frozenset(cols))
            if fn == "ALLEXCEPT":
                return None, None
            if fn in ("TOPN", "SAMPLE", "INDEX", "OFFSET"):
                return _span_table(args[1], seen) if len(args) > 1 else (None, None)
            if fn in ("INTERSECT", "EXCEPT", "CROSSJOIN", "NATURALINNERJOIN",
                      "NATURALLEFTOUTERJOIN", "GENERATE", "GENERATEALL"):
                vals = {_span_table(a, seen) for a in args}
                return vals.pop() if len(vals) == 1 else (None, None)
            return None, None
        if len(span) == 2 and span[1].kind == "bracket" \
                and (span[0].kind == "table" or (span[0].kind == "ident" and res.table(span[0].text))):
            tab = res.table(span[0].value if span[0].kind == "table" else span[0].text)
            return tab, ("only", frozenset({span[1].value.lower()}))
        return None, None

    def _only_has(fr, name) -> bool:
        cols = fr.get("only_columns")
        return cols is None or name in cols

    def _x_has(fr, name) -> bool:
        cols = fr.get("x_cols")
        if cols is None:
            return True
        kind, s = cols
        return (name in s) if kind == "only" else (name not in s)

    def poison_qualified(tab) -> None:
        if not tab:
            return
        for idx, fr in enumerate(stack):
            if fr["fn"] in deps.ITERATORS and _teaches_at(fr) and not fr["poisoned"] \
                    and fr["row_table"] is not None and fr["row_table"] != tab:
                frames = stack[idx + 1:]
                if not frames:
                    continue
                bad = _first_nonsource(frames, stack[-1]["arg"])
                if bad is None:
                    fr["row_table"] = None
                    fr["poisoned"] = True
                else:
                    p, d = bad
                    if p["fn"] in PREDICATE_EXEMPT and d >= 1:
                        continue
                    if p["fn"] in ("TOPN", "SAMPLE", "INDEX", "OFFSET"):
                        continue
                    if p["fn"] in SCALAR_FUNCS or p["fn"] in X_ITERATORS or p["fn"] == "{":
                        continue
                    fr["row_table"] = None
                    fr["poisoned"] = True

    pending_vars: list = []
    var_names_defined: dict[str, set] = {}
    i, n = 0, len(sig)
    while i < n:
        t = sig[i]
        nxt = sig[i + 1] if i + 1 < n else None
        for fr in stack:
            if fr.get("x_span") is not None and fr["arg"] == fr.get("span_arg", 0) \
                    and not (t.kind == "op" and t.text in (",", ")") and stack[-1] is fr):
                fr["x_span"].append(t)
        if t.kind == "ident" and t.text.upper() == "VAR" \
                and nxt is not None and nxt.kind == "ident":
            if pending_vars and pending_vars[-1][1] == len(stack):
                pending_vars.pop()
            pending_vars.append((nxt.text.lower(), len(stack)))
        elif t.kind == "ident" and t.text.upper() == "RETURN" \
                and pending_vars and pending_vars[-1][1] == len(stack):
            pending_vars.pop()
        if t.kind == "op" and t.text in ("(", "{"):
            prev = sig[i - 1] if i else None
            fn = "{" if t.text == "{" else (prev.text.upper() if prev is not None and prev.kind == "ident" else "")
            frame = {"fn": fn, "arg": 0, "row_table": None, "poisoned": False, "taint": None,
                     "parent_arg": stack[-1]["arg"] if stack else None,
                     "x_span": [] if fn in ("XNPV", "XIRR", "SUBSTITUTEWITHINDEX") else None,
                     "x_skip": True,
                     "span_arg": 2 if fn == "SUBSTITUTEWITHINDEX" else 0}
            stack.append(frame)
            if fn and fn not in SCALAR_FUNCS and fn not in CONDITIONALS:
                for f_idx, fr in enumerate(stack[:-1]):
                    if fr["fn"] in deps.ITERATORS and _teaches_at(fr) \
                            and fr["row_table"] is not None and not fr["poisoned"]:
                        if fr.get("taint") is None \
                                and _first_nonsource(stack[f_idx + 1:-1], frame["parent_arg"]) is None:
                            fr["taint"] = frame
        elif t.kind == "op" and t.text in (")", "}"):
            if stack:
                closed = stack.pop()
                if closed.get("names"):
                    for f_idx, fr in enumerate(stack):
                        path = stack[f_idx + 1:]
                        first_d = path[0]["parent_arg"] if path else closed["parent_arg"]
                        if fr["fn"] in ("TOPN", "SAMPLE", "INDEX", "OFFSET"):
                            in_source = first_d == 1
                        elif fr["fn"] == "RANK":
                            in_source = first_d in (0, 1)
                        elif fr["fn"] == "WINDOW":
                            in_source = first_d in (2, 3, 4)
                        else:
                            in_source = first_d == 0
                        if not in_source:
                            continue
                        if closed["fn"] == "ROLLUPADDISSUBTOTAL" and path \
                                and path[-1]["fn"] == "SUMMARIZECOLUMNS":
                            ok = _first_nonsource(path[:-1], path[-1]["parent_arg"]) is None
                        else:
                            ok = _first_nonsource(path, closed["parent_arg"]) is None
                        if ok:
                            fr.setdefault("captured", set()).update(closed["names"])
                    if pending_vars:
                        var_names_defined.setdefault(pending_vars[-1][0], set()).update(closed["names"])
                if closed.get("x_span") is not None and closed["arg"] == closed.get("span_arg", 0) \
                        and closed.get("x_table") is None:
                    closed["x_table"], closed["x_cols"] = _span_table(closed["x_span"])
                for fr in stack:
                    if fr.get("taint") is closed:
                        fr["taint"] = None
                        fr["row_table"] = None
                        fr["poisoned"] = True
                if closed["fn"] and closed["fn"] not in SCALAR_FUNCS \
                        and closed["fn"] not in CONDITIONALS:
                    for f_idx, fr in enumerate(stack):
                        if fr["fn"] in deps.ITERATORS and _teaches_at(fr) \
                                and fr["row_table"] is None and not fr["poisoned"] \
                                and _first_nonsource(stack[f_idx + 1:], closed["parent_arg"]) is None:
                            fr["poisoned"] = True
        elif t.kind == "op" and t.text == "," and stack:
            top = stack[-1]
            if top.get("expect") is not None:
                if top["expect"] == "item":
                    top["expect"] = "expr" if top.pop("item_named", False) else "item"
                else:
                    top["expect"] = "item"
            if top.get("x_span") is not None and top["arg"] == top.get("span_arg", 0) \
                    and top.get("x_table") is None:
                top["x_table"], top["x_cols"] = _span_table(top["x_span"])
            top["arg"] += 1
        if stack and t.kind == "str" and stack[-1]["fn"] in (
                "SELECTCOLUMNS", "SUMMARIZECOLUMNS", "ADDCOLUMNS", "SUMMARIZE", "GROUPBY",
                "ROLLUPADDISSUBTOTAL", "ROW", "DATATABLE") \
                and (i == 0 or (sig[i - 1].kind == "op" and sig[i - 1].text in (",", "("))) \
                and (nxt is not None and nxt.kind == "op" and nxt.text in (",", ")")):
            fr0 = stack[-1]
            if fr0["fn"] == "ROLLUPADDISSUBTOTAL":
                fr0.setdefault("names", set()).add(t.value.lower())
            elif fr0.get("expect") is None:
                if fr0["fn"] not in ("SELECTCOLUMNS", "ADDCOLUMNS") or fr0["arg"] >= 1 \
                        or fr0["fn"] in ("ROW", "DATATABLE"):
                    fr0["expect"] = "item"
                    fr0["item_named"] = True
                    fr0.setdefault("names", set()).add(t.value.lower())
            elif fr0["expect"] == "item":
                fr0["item_named"] = True
                fr0.setdefault("names", set()).add(t.value.lower())
        if t.kind in LIT_KINDS:
            val = _num(t.text) if t.kind == "num" else ("T" + t.text if t.kind == "pbztok" else "S" + t.value)
            toks.append(("LIT", "lit", val))
        elif t.kind in ("table", "ident") and nxt is not None and nxt.kind == "bracket" and (
                t.kind == "table" or t.text.lower() in var_names or res.table(t.text)):
            binding = by_span.get((nxt.start, nxt.end))
            if binding is not None:
                uncertain |= binding.uncertain
            else:
                uncertain = True
            if t.kind == "ident" and t.text.lower() in var_names:
                toks.append((f"{var_names[t.text.lower()]}[{nxt.value.lower()}]", "", ""))
            else:
                qual = t.value if t.kind == "table" else t.text
                tab = res.table(qual)
                col = res.column(tab, nxt.value) if tab else None
                if col:
                    lab = f"{tab}[{col}]"
                    toks.append((lab.lower(), "col", lab))
                    learn(tab, column=nxt.value)
                    poison_qualified(tab)
                elif res.measure(nxt.value):
                    lab = f"[{res.measure(nxt.value)[1]}]"
                    toks.append((lab.lower(), "meas", lab))
                else:
                    lab = f"{qual}[{nxt.value}]"
                    toks.append((lab.lower(), "col", lab))
            for fr in stack:
                if fr.get("x_span") is not None and fr["arg"] == fr.get("span_arg", 0):
                    fr["x_span"].append(nxt)
            i += 2
            continue
        elif t.kind == "bracket":
            CALLER_ARGS = {"RANKX": (2, None), "PERCENTILEX.INC": (2, None),
                           "PERCENTILEX.EXC": (2, None), "CONCATENATEX": (2, 2)}

            def _in_caller_args(g) -> bool:
                ca = CALLER_ARGS.get(g["fn"])
                return ca is not None and g["arg"] >= ca[0] and (ca[1] is None or g["arg"] <= ca[1])

            binding = by_span.get((t.start, t.end))
            if binding is not None:
                ref = binding.ref
                uncertain |= binding.uncertain
            else:
                uncertain = True
                ref = deps._bare(t.value, home, res, False, [], extension)
            def _governed_from(start):
                if ref.kind != "column":
                    return -1
                for j in range(len(stack) - 1, start - 1, -1):
                    g = stack[j]
                    if g["fn"] in deps.ROW_CONTEXT and g["arg"] >= 1 \
                            and not _in_caller_args(g):
                        return j if (g["row_table"] == ref.table
                                     and not g.get("projected")
                                     and _only_has(g, ref.name.lower())) else -1
                return -1

            governed_idx = _governed_from(0)
            governed = governed_idx >= 0
            if not governed and any(fr["fn"] in deps.ROW_CONTEXT and fr["arg"] >= 1
                                    and fr["row_table"] is None
                                    for fr in stack):
                uncertain = True
            for c_idx, fr in enumerate(stack):
                if fr["fn"] == "{":
                    ctor_governed = ref.kind == "column" and any(
                        g["fn"] in deps.ROW_CONTEXT and g["arg"] >= 1
                        and not _in_caller_args(g) and g["row_table"] == ref.table
                        and not g.get("projected") and _only_has(g, ref.name.lower())
                        for g in stack[c_idx + 1:])
                    if ctor_governed:
                        continue
                    ctx = None
                    found = False
                    for below in reversed(stack[:c_idx]):
                        if below["fn"] in deps.ROW_CONTEXT and below["arg"] >= 1:
                            ctx = below["row_table"]
                            found = True
                            break
                    if found and (ctx is None
                                  or (ref.kind == "column" and ctx != ref.table)):
                        uncertain = True
                    elif not found and ref.kind == "column":
                        uncertain = True
            if ref.kind == "column" and not governed and any(
                    fr["fn"] in deps.ROW_CONTEXT and fr["arg"] >= 1
                    and (fr.get("projected") and fr["row_table"] == ref.table
                         or not _only_has(fr, ref.name.lower()))
                    for fr in stack):
                uncertain = True
            if any(fr["fn"] in deps.ROW_CONTEXT and fr["arg"] >= 1
                   and t.value.lower() in fr.get("captured", ())
                   for fr in (stack[governed_idx:] if governed else stack)):
                uncertain = True
            for c_idx, fr in enumerate(stack):
                if fr["fn"] in ("TOPN", "SAMPLE", "INDEX", "OFFSET") and fr["arg"] >= 2 \
                        and _governed_from(c_idx + 1) < 0 \
                        and (fr["row_table"] is None
                             or (ref.kind == "column" and fr["row_table"] != ref.table)
                             or (ref.kind == "measure" and fr["row_table"] is not None
                                 and _only_has(fr, t.value.lower())
                                 and res.column(fr["row_table"], t.value) is not None)
                             or t.value.lower() in fr.get("captured", ())):
                    uncertain = True
            for c_idx, fr in enumerate(stack):
                if fr["fn"] in ("ORDERBY", "PARTITIONBY", "MATCHBY") and c_idx > 0 \
                        and _governed_from(c_idx + 1) < 0:
                    parent = stack[c_idx - 1]
                    if parent["row_table"] is None \
                            or (ref.kind == "column" and parent["row_table"] != ref.table) \
                            or (ref.kind == "measure" and parent["row_table"] is not None
                                and _only_has(parent, t.value.lower())
                                and res.column(parent["row_table"], t.value) is not None) \
                            or t.value.lower() in parent.get("captured", ()):
                        uncertain = True
            for c_idx, fr in enumerate(stack):
                if fr["fn"] == "SUBSTITUTEWITHINDEX" and fr["arg"] >= 3 and fr["arg"] % 2 == 1 \
                        and ((ref.kind == "column" and fr.get("x_table") != ref.table
                              and _x_has(fr, ref.name.lower()))
                             or (ref.kind == "measure" and (fr.get("x_table") is None
                                 or (_x_has(fr, t.value.lower())
                                     and res.column(fr.get("x_table"), t.value) is not None)))
                             or t.value.lower() in fr.get("captured", ())) \
                        and not (ref.kind == "column" and any(
                                    g["fn"] in deps.ROW_CONTEXT and g["arg"] >= 1
                                    and not _in_caller_args(g) and g["row_table"] == ref.table
                                    and not g.get("projected")
                                    and _only_has(g, ref.name.lower())
                                    for g in stack[c_idx + 1:])):
                    uncertain = True

            for c_idx, fr in enumerate(stack):
                if fr["fn"] in ("XNPV", "XIRR") and fr["arg"] in (1, 2) \
                        and ((ref.kind == "column" and fr.get("x_table") != ref.table
                              and _x_has(fr, ref.name.lower()))
                             or (ref.kind == "measure" and (fr.get("x_table") is None
                                 or (_x_has(fr, t.value.lower())
                                     and res.column(fr.get("x_table"), t.value) is not None)))
                             or t.value.lower() in fr.get("captured", ())) \
                        and not (ref.kind == "column" and any(
                                    g["fn"] in deps.ROW_CONTEXT and g["arg"] >= 1
                                    and not _in_caller_args(g) and g["row_table"] == ref.table
                                    and not g.get("projected")
                                    and _only_has(g, ref.name.lower())
                                    for g in stack[c_idx + 1:])):
                    uncertain = True
                caller_args = CALLER_ARGS.get(fr["fn"])
                if caller_args is not None and ref.kind == "column" \
                        and fr["arg"] >= caller_args[0] \
                        and (caller_args[1] is None or fr["arg"] <= caller_args[1]) \
                        and not any(g["fn"] in deps.ROW_CONTEXT and g["arg"] >= 1
                                    and not _in_caller_args(g) and g["row_table"] == ref.table
                                    and not g.get("projected")
                                    and _only_has(g, ref.name.lower())
                                    for g in stack[c_idx + 1:]):
                    caller = None
                    for below in reversed(stack[:c_idx]):
                        if below["fn"] in ("XNPV", "XIRR") and below["arg"] in (1, 2):
                            if not _x_has(below, t.value.lower()):
                                continue
                            caller = below.get("x_table")
                            break
                        if below["fn"] == "SUBSTITUTEWITHINDEX" and below["arg"] >= 3 \
                                and below["arg"] % 2 == 1:
                            if not _x_has(below, t.value.lower()):
                                continue
                            caller = below.get("x_table")
                            break
                        if below["fn"] in ("TOPN", "SAMPLE", "INDEX", "OFFSET") \
                                and below["arg"] >= 2 and not below.get("projected") \
                                and _only_has(below, t.value.lower()):
                            caller = below["row_table"]
                            break
                        if below["fn"] in deps.ROW_CONTEXT and below["arg"] >= 1:
                            caller = below["row_table"]
                            break
                    if caller is None or caller != ref.table:
                        uncertain = True
            if ref.kind == "column":
                learn(ref.table)
            if ref.kind == "measure":
                lab = f"[{ref.name}]"
                toks.append((lab.lower(), "meas", lab))
            elif ref.kind == "column":
                lab = f"{ref.table}[{ref.name}]"
                toks.append((lab.lower(), "col", lab))
            else:
                toks.append((f"[{t.value.lower()}]", "", ""))
        elif t.kind == "table":
            binding = by_span.get((t.start, t.end))
            if binding is not None:
                uncertain |= binding.uncertain
            tab = res.table(t.value) or t.value
            toks.append(("t:" + tab.lower(), "", ""))
            learn(res.table(t.value), bare=True)
        elif t.kind == "ident":
            low = t.text.lower()
            if nxt is not None and nxt.kind == "op" and nxt.text == "(":
                toks.append(("fn:" + low if low in res.functions else t.text.upper(), "", ""))
            elif low in var_names:
                toks.append((var_names[low], "", ""))
                prev = sig[i - 1] if i else None
                if low in var_names_defined and not (prev is not None and prev.kind == "ident"
                                                     and prev.text.upper() == "VAR"):
                    for f_idx, fr in enumerate(stack):
                        if _first_nonsource(stack[f_idx + 1:],
                                            stack[-1]["arg"] if len(stack) > f_idx + 1 else 0) is None:
                            fr.setdefault("captured", set()).update(var_names_defined[low])
                    if pending_vars and pending_vars[-1][0] != low:
                        var_names_defined.setdefault(pending_vars[-1][0], set()).update(
                            var_names_defined[low])
                if low in table_vars and not (prev is not None and prev.kind == "ident"
                                              and prev.text.upper() == "VAR"):
                    for f_idx, fr in enumerate(stack):
                        if fr["fn"] in deps.ITERATORS and _teaches_at(fr) \
                                and _in_source_region(f_idx, stack[-1]["arg"] if len(stack) > f_idx + 1 else 0):
                            fr["row_table"] = None
                            fr["poisoned"] = True
            elif res.table(t.text):
                toks.append(("t:" + res.table(t.text).lower(), "", ""))
                learn(res.table(t.text), bare=True)
            else:
                if t.text.upper() not in ("ASC", "DESC", "DENSE", "SKIP", "AND", "OR", "NOT",
                                          "IN", "TRUE", "FALSE", "BLANK", "ABS", "REL",
                                          "VAR", "RETURN", "DAY", "MONTH", "QUARTER", "WEEK",
                                          "YEAR", "HOUR", "MINUTE", "SECOND", "BOTH", "NONE",
                                          "ONEWAY", "DEFAULT", "FIRST", "LAST", "KEEP"):
                    uncertain = True
                toks.append((t.text.upper(), "", ""))
        else:
            toks.append((t.text, "", ""))
        i += 1
    if any(t.kind == "ident" and t.text.upper() == "COUNTROWS"
           and j + 2 < n and sig[j + 1].kind == "op" and sig[j + 1].text == "("
           and sig[j + 2].kind == "op" and sig[j + 2].text == ")"
           for j, t in enumerate(sig)):
        toks.append((f"home:{home.lower()}", "", ""))
    toks = _strip_parens(toks)
    words = [x[0] for x in toks]
    if words.count("VAR") == 1 and len(words) >= 6 and words[0] == "VAR" and words[-2] == "RETURN" \
            and words[-1] == words[1] and words[2] == "=":
        toks = _strip_parens(toks[3:-2])
    return Canon(toks, uncertain)


def _close_of(toks, i: int) -> int:
    depth = 0
    for j in range(i, len(toks)):
        if toks[j][0] in ("(", "{"):
            depth += 1
        elif toks[j][0] in (")", "}"):
            depth -= 1
            if depth == 0:
                return j
    return -1


def _strip_parens(toks: list) -> list:
    while len(toks) >= 2 and toks[0][0] == "(" and _close_of(toks, 0) == len(toks) - 1:
        toks = toks[1:-1]
    return toks


def discovery(c: Canon) -> Canon:
    """Canon with a filterless top-level CALCULATE(E) reduced to E (candidate discovery only)."""
    toks = c.toks
    while len(toks) >= 4 and toks[0][0] == "CALCULATE" and toks[1][0] == "(" and _close_of(toks, 1) == len(toks) - 1:
        inner, depth = toks[2:-1], 0
        top_comma = False
        for tok, _k, _v in inner:
            depth += tok in ("(", "{")
            depth -= tok in (")", "}")
            top_comma = top_comma or (tok == "," and depth == 0)
        if top_comma:
            break
        toks = _strip_parens(inner)
    return Canon(toks, c.uncertain)


def _grams(words: list[str]) -> set:
    return {tuple(words[i:i + 3]) for i in range(len(words) - 2)}


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _slot_diffs(a: Canon, b: Canon) -> list[str]:
    out, lit = [], 0
    sa = [(k, v) for _t, k, v in a.toks if k in SLOT_KINDS]
    sb = [(k, v) for _t, k, v in b.toks if k in SLOT_KINDS]
    for (ka, va), (kb, vb) in zip(sa, sb):
        if va == vb:
            continue
        if ka == "lit" or kb == "lit":
            lit += 1
        else:
            out.append(f"reference differs: {va} vs {vb}")
    if lit:
        out.insert(0, "literal differs" if lit == 1 else f"{lit} literals differ")
    return out


def run(ws, project, threshold: float = 0.80, max_pairs: int = 100) -> dict:
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    model = tmdl.load_model(mdef, root=os.path.dirname(project.mirror_model))
    res = deps.Resolver(model)
    ms = [m for m in model.all_measures() if m.expression.strip()]
    lab = {id(m): f"[{m.name}]" for m in ms}
    canons = {id(m): canon(m.expression, m.table, res) for m in ms}
    disc = {i: discovery(c) for i, c in canons.items()}

    # identical
    by_key: dict[tuple, list] = {}
    for m in ms:
        by_key.setdefault(tuple(canons[id(m)].words("hash")), []).append(m)
    identical = [grp for grp in by_key.values() if len(grp) > 1]
    group_of = {id(m): gi for gi, grp in enumerate(identical) for m in grp}
    # aliases
    aliases = []
    alias_pairs = set()
    for m in ms:
        d = disc[id(m)]
        if len(d.toks) == 1 and d.toks[0][1] == "meas" and d.toks[0][2].lower() != lab[id(m)].lower():
            aliases.append({"measure": lab[id(m)], "table": m.table, "alias_of": d.toks[0][2]})
            alias_pairs.add(frozenset((lab[id(m)].lower(), d.toks[0][2].lower())))
    # same shape (slots: literals, columns, measures)
    shapes: dict[tuple, list] = {}
    for m in ms:
        w = canons[id(m)].words("shape")
        if len(w) >= MIN_SHAPE_TOKENS:
            shapes.setdefault(tuple(w), []).append(m)
    same_shape = []
    shape_of: dict[int, int] = {}
    for grp in shapes.values():
        if len(grp) < 2 or len({group_of.get(id(m), -1 - k) for k, m in enumerate(grp)}) < 2:
            continue
        for m in grp:
            shape_of[id(m)] = len(same_shape)
        first = grp[0]
        diffs = []
        for m in grp[1:]:
            if group_of.get(id(m)) is not None and group_of.get(id(m)) == group_of.get(id(first)):
                continue
            diffs.append({"measure": lab[id(m)], "vs": lab[id(first)],
                          "slots": _slot_diffs(canons[id(first)], canons[id(m)])})
        same_shape.append({"measures": [lab[id(m)] for m in grp], "differences": diffs,
                           "hint": "same pattern over different inputs: calculation group or parameter candidate"})
    # fuzzy pairs blocked by shared 3-grams
    words = {id(m): disc[id(m)].words("slot") for m in ms}
    grams = {i: _grams(w) for i, w in words.items()}
    df: dict = {}
    for gset in grams.values():
        for gr in gset:
            df[gr] = df.get(gr, 0) + 1
    limit = max(3, int(0.2 * len(ms)))
    post: dict = {}
    for m in ms:
        if len(words[id(m)]) < MIN_TOKENS:
            continue
        for gr in grams[id(m)]:
            if df[gr] <= limit:
                post.setdefault(gr, []).append(m)
    shared: dict = {}
    objs: dict = {}
    order = {id(m): i for i, m in enumerate(ms)}
    for lst in post.values():
        for x in range(len(lst)):
            for y in range(x + 1, len(lst)):
                a, b = sorted((lst[x], lst[y]), key=lambda m: order[id(m)])
                k = (id(a), id(b))
                objs[k] = (a, b)
                shared[k] = shared.get(k, 0) + 1
    pairs = []
    for (pa, pb), cnt in shared.items():
        a, b = objs[(pa, pb)]
        if cnt < 2:
            continue
        if group_of.get(pa) is not None and group_of.get(pa) == group_of.get(pb):
            continue
        if shape_of.get(pa) is not None and shape_of.get(pa) == shape_of.get(pb):
            continue  # already reported as same_shape with its slot differences
        if frozenset((lab[pa].lower(), lab[pb].lower())) in alias_pairs:
            continue
        wa, wb = words[pa], words[pb]
        expr = 0.5 * difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio() + \
            0.25 * _jaccard(grams[pa], grams[pb]) + 0.25 * _jaccard(canons[pa].refs(), canons[pb].refs())
        dependent = lab[pb] in canons[pa].measures() or lab[pa] in canons[pb].measures()
        if dependent and expr < 0.95:
            continue
        name = difflib.SequenceMatcher(None, a.name.casefold(), b.name.casefold(), autojunk=False).ratio()
        score = 0.70 * expr + 0.12 * name + 0.06 * (a.format_string == b.format_string) + \
            0.06 * (a.folder == b.folder) + 0.06 * (a.table == b.table)
        if score >= max(threshold, 0.90) and expr >= 0.92:
            cls = "near_duplicate"
        elif score >= threshold and expr >= 0.80:
            cls = "similar"
        else:
            continue
        rec = {"a": lab[pa], "b": lab[pb], "class": cls, "score": round(score, 3), "expr": round(expr, 3)}
        diffs = _slot_diffs(canons[pa], canons[pb]) if canons[pa].words("shape") == canons[pb].words("shape") else []
        if diffs:
            rec["slots"] = diffs
        pairs.append(rec)
    pairs.sort(key=lambda p: (-p["score"], p["a"].lower(), p["b"].lower()))
    return {"verb": "similar", "project": project.name, "measures": len(ms), "threshold": threshold,
            "identical": [{"measures": [lab[id(m)] for m in grp], "tables": sorted({m.table for m in grp})}
                          for grp in identical],
            "aliases": aliases, "same_shape": same_shape, "pairs": pairs[:max_pairs],
            "pairs_total": len(pairs)}


_STRUCT = ("tables", "columns", "measures", "relationships")


def _structure(project) -> dict[str, set[str]]:
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    model = tmdl.load_model(mdef, root=os.path.dirname(project.mirror_model))
    tables, columns, measures, rels = set(), set(), set(), set()
    for table in model.tables.values():
        if table.private:
            continue
        tables.add(table.name.lower())
        columns.update(f"{table.name.lower()}[{name.lower()}]" for name in table.columns)
        measures.update(name.lower() for name in table.measures)
    for rel in model.relationships:
        rels.add(f"{rel.from_table.lower()}[{rel.from_column.lower()}]->"
                 f"{rel.to_table.lower()}[{rel.to_column.lower()}]")
    return {"tables": tables, "columns": columns, "measures": measures, "relationships": rels}


def _overlap_band(shared: int, left: int, right: int) -> str:
    if shared <= 0:
        return "NONE"
    base = max(1, min(left, right))
    ratio = shared / base
    if ratio < 0.34:
        return "LOW"
    if ratio < 0.67:
        return "MID"
    return "HIGH"


def models(ws, names, against=None) -> dict:
    """Structural overlap of projects in one workspace. Counts are shared names, never a similarity float."""
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, (list, tuple)) or not names:
        raise PbzError("BAD_REQUEST", "name the projects to compare")
    wanted = list(names)
    if against:
        if not isinstance(against, str):
            raise PbzError("BAD_REQUEST", "against is a project name")
        wanted.append(against)
    by_name = {p.name.lower(): p for p in ws.projects}
    chosen = []
    for name in wanted:
        if not isinstance(name, str) or not name.strip():
            raise PbzError("BAD_REQUEST", "project names are strings")
        hit = by_name.get(name.strip().lower())
        if hit is None:
            raise PbzError("NO_PROJECT", name)
        chosen.append(hit)
    structs = {}
    for project in chosen:
        if project.name not in structs:
            structs[project.name] = _structure(project)
    if against:
        target = by_name[against.strip().lower()].name
        resolved = list(dict.fromkeys(by_name[a.strip().lower()].name for a in names))
        pairs = [(a, target) for a in resolved if a.lower() != target.lower()]
    else:
        resolved = list(dict.fromkeys(by_name[a.strip().lower()].name for a in names))
        pairs = [(resolved[i], resolved[j]) for i in range(len(resolved)) for j in range(i + 1, len(resolved))
                 if resolved[i] != resolved[j]]
    rows = []
    for left_name, right_name in pairs:
        left, right = structs[left_name], structs[right_name]
        shared = {kind: len(left[kind] & right[kind]) for kind in _STRUCT}
        total = sum(shared.values())
        rows.append({"a": left_name, "b": right_name, "overlap_band": _overlap_band(
            total, sum(len(left[k]) for k in _STRUCT), sum(len(right[k]) for k in _STRUCT)),
            "shared": shared})
    return {"verb": "similar", "action": "models", "pairs": rows}
