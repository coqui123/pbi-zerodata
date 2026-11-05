"""`pbz audit`: efficiency and consolidation audit of the semantic model.

Static analysis of the mirror (names, DAX and M only) plus optional bucketed evidence: the value index
(distinct and row buckets from `pbz index`) and storage bands (`pbz audit memory`). Every finding has a
stable fingerprint (`id`), a category (efficiency, consolidation, design), a severity, a confidence, the
evidence it rests on, a fix and a way to verify it. Smells are candidates for review, not measured
improvements: a nested iterator or a high-cardinality column says "look here", not "this is slow".

Rules that overlap `pbz bpa` (DIVIDE, IFERROR, FILTER on tables, bidirectional and many-to-many counts,
RELATED in calculated columns, auto date/time) stay in bpa; audit adds context, evidence and
consolidation plans. `coverage` says which rules ran and which needed evidence that was missing.

Baselines and waivers live in `.pbz/audit/` (fingerprints and names only): `--save-baseline <name>`,
`--since <name>` (new / persistent / resolved), `pbz audit waive <id> --reason ...`, `--fail-on`.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from dataclasses import dataclass, field

from . import deps, lexers, mlineage, similar, tmdl, usage
from .common import PbzError

SEVERITY = ("high", "medium", "low", "info")
HIGH_COUNT = "10k+"                       # top distinct / row bucket of `pbz index`
BIG_BANDS = ("100-1000 MiB", ">=1000 MiB")
MID_BANDS = ("10-100 MiB",) + BIG_BANDS
SOME_BANDS = ("1-10 MiB",) + MID_BANDS
HIGH_SHARE = ("5-20%", ">=20%")
SMALL_MODEL = ("<1 MiB", "1-10 MiB")
MAX_FINDINGS = 400
SUBEXPR_MIN_TOKENS = 15
SUBEXPR_MIN_MEASURES = 3
FAMILY_MIN = 3

RULES = {
    # efficiency
    "ITERATOR_MEASURE_CALL": ("efficiency", "index", "SQLBI, DAX Optimizer",
                              "a measure is evaluated for every row of a fact table (context transition per row)"),
    "NESTED_ITERATOR": ("efficiency", "index", "SQLBI",
                        "an iterator over a table runs inside another iterator's row expression"),
    "SUMMARIZE_EXTENSION_COLUMNS": ("efficiency", "static", "SQLBI",
                                    "SUMMARIZE computes extension columns (slower, can return wrong results)"),
    "CALC_COLUMN_AS_MEASURE": ("efficiency", "index", "Microsoft guidance, VertiPaq Analyzer",
                               "a calculated column is only ever aggregated: a measure avoids storing it"),
    "CALC_COLUMN_UPSTREAM": ("efficiency", "static", "Microsoft guidance",
                             "a row-local calculated column could be computed in Power Query or the source"),
    "UNUSED_HIGH_CARDINALITY": ("efficiency", "index", "VertiPaq Analyzer, Measure Killer",
                                "an unused column with many distinct values or a large storage band"),
    "HIGH_CARDINALITY_BIDIRECTIONAL": ("efficiency", "index", "Microsoft BPA (100k distinct)",
                                       "a bidirectional relationship on a high-cardinality key"),
    "MIXED_STORAGE_DIMENSION": ("efficiency", "static", "Microsoft composite model guidance",
                                "an Import dimension filters a DirectQuery table (limited relationship)"),
    "MEMORY_HOTSPOT": ("efficiency", "memory", "VertiPaq Analyzer",
                       "a column holds a large share of the model's storage"),
    "DATETIME_TIME_PART": ("efficiency", "memory", "Microsoft BPA (split date and time)",
                           "a date/time column stores times of day: split or truncate it"),
    "DOUBLE_WITHOUT_FRACTIONS": ("efficiency", "memory", "VertiPaq Analyzer",
                                 "a decimal column holds whole numbers only: store it as a whole number"),
    "LONG_TEXT": ("efficiency", "memory", "Microsoft BPA (long-length columns)",
                  "a text column holds long values and a large storage band"),
    "DICTIONARY_HEAVY": ("efficiency", "memory", "VertiPaq Analyzer",
                         "most of a column's storage is its dictionary (many distinct values)"),
    "LARGE_RELATIONSHIP": ("efficiency", "memory", "VertiPaq Analyzer",
                           "a relationship structure is large: its key has very many distinct values"),
    "AUTO_DATETIME_STORAGE": ("efficiency", "memory", "Microsoft guidance, Bravo",
                              "hidden auto date/time tables hold a visible share of the model"),
    "M_BUFFER_BEFORE_FILTER": ("efficiency", "static", "Microsoft Power Query guidance",
                               "Table.Buffer runs before a later row filter"),
    # consolidation
    "DUPLICATE_MEASURE": ("consolidation", "static", "pbz similar",
                          "measures with identical DAX"),
    "CALC_GROUP_FAMILY": ("consolidation", "static", "SQLBI, Tabular Editor",
                          "the same transformation applied to several base measures: a calculation group"),
    "FILTERED_FAMILY": ("consolidation", "static", "pbz similar",
                        "measures that differ only by literal filter values"),
    "PARAMETERIZED_FAMILY": ("consolidation", "static", "pbz similar",
                             "measures that differ only by the columns they use: a DAX function candidate"),
    "REPEATED_SUBEXPRESSION": ("consolidation", "static", "SQLBI (DAX UDF guidance)",
                               "the same DAX sub-expression is copied into several measures"),
    "DUPLICATE_TABLE": ("consolidation", "static", "Microsoft guidance",
                        "tables loaded from the same query with the same columns"),
    "MULTIPLE_DATE_TABLES": ("consolidation", "static", "Bravo, Microsoft guidance",
                             "more than one date table"),
    "RELATED_COPY_COLUMN": ("consolidation", "static", "Microsoft BPA (RELATED in calculated columns)",
                            "a calculated column only copies a dimension column with RELATED"),
    "COPY_COLUMN": ("consolidation", "static", "pbz",
                    "a calculated column only repeats another column of its table"),
    "M_SHARED_STEPS": ("consolidation", "static", "Microsoft Power Query guidance",
                       "queries repeat the same source and transformation steps"),
    # design
    "UNFOLDERED_MEASURES": ("design", "static", "Tabular Editor (more than 10 visible objects)",
                            "a table shows more than 10 measures without display folders"),
    "MEASURES_SCATTERED": ("design", "static", "SQLBI, Tabular Editor",
                           "measures live in many tables and the model has no measure table"),
}

KEYWORDS = {"VAR", "RETURN", "DEFINE", "EVALUATE", "MEASURE", "ORDER", "BY", "ASC", "DESC", "START", "AT", "IN",
            "COLUMN", "TABLE", "FUNCTION"}
ITERATORS = {"SUMX", "AVERAGEX", "MINX", "MAXX", "COUNTX", "COUNTAX", "PRODUCTX", "MEDIANX", "GEOMEANX",
             "STDEVX.P", "STDEVX.S", "VARX.P", "VARX.S", "PERCENTILEX.INC", "PERCENTILEX.EXC", "CONCATENATEX",
             "RANKX", "FILTER", "ADDCOLUMNS", "SELECTCOLUMNS", "GENERATE", "GENERATEALL"}
WRAPPERS = {"FILTER", "CALCULATETABLE", "RELATEDTABLE", "KEEPFILTERS", "ALL", "ALLNOBLANKROW", "ALLSELECTED",
            "ALLEXCEPT", "VALUES", "DISTINCT", "ALLCROSSFILTERED"}
AGG1 = {"SUM": "SUMX", "AVERAGE": "AVERAGEX", "MIN": "MINX", "MAX": "MAXX"}
ROW_LOCAL = {
    "IF", "SWITCH", "AND", "OR", "NOT", "TRUE", "FALSE", "BLANK", "ISBLANK", "DIVIDE", "ROUND", "ROUNDUP",
    "ROUNDDOWN", "INT", "TRUNC", "ABS", "MOD", "POWER", "SQRT", "EXP", "LN", "LOG", "LOG10", "SIGN", "CEILING",
    "FLOOR", "MROUND", "CONCATENATE", "LEFT", "RIGHT", "MID", "LEN", "UPPER", "LOWER", "TRIM", "SUBSTITUTE",
    "REPLACE", "REPT", "SEARCH", "FIND", "FORMAT", "VALUE", "FIXED", "CONTAINSSTRING", "CONTAINSSTRINGEXACT",
    "EXACT", "UNICHAR", "UNICODE", "COMBINEVALUES", "YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND", "DATE",
    "TIME", "DATEVALUE", "TIMEVALUE", "WEEKDAY", "WEEKNUM", "EOMONTH", "EDATE", "DATEDIFF", "QUARTER",
    "YEARFRAC", "CONVERT", "CURRENCY", "ISNUMBER", "ISTEXT", "ISNONTEXT", "ISLOGICAL", "ISEVEN", "ISODD",
    "COALESCE", "BITAND", "BITOR", "BITXOR", "BITLSHIFT", "BITRSHIFT", "NETWORKDAYS", "EVEN", "ODD", "QUOTIENT",
    "GCD", "LCM", "PI", "RADIANS", "DEGREES", "SIN", "COS", "TAN"}


# ---------------------------------------------------------------- DAX call tree

@dataclass
class Call:
    name: str
    at: int                      # index of the function name in the significant tokens
    open: int
    close: int = -1
    args: list = field(default_factory=list)   # [(start, end)) token index ranges
    parent: "Call | None" = None
    parent_arg: int = -1
    _start: int = 0


def call_tree(sig: list) -> list[Call]:
    calls: list[Call] = []
    stack: list = []
    for i, t in enumerate(sig):
        if t.kind == "op" and t.text in ("(", "{"):
            prev = sig[i - 1] if i else None
            if t.text == "(" and prev is not None and prev.kind == "ident" and prev.text.upper() not in KEYWORDS:
                parent = next((c for c in reversed(stack) if c is not None), None)
                c = Call(prev.text.upper(), i - 1, i, parent=parent,
                         parent_arg=len(parent.args) if parent is not None else -1, _start=i + 1)
                calls.append(c)
                stack.append(c)
            else:
                stack.append(None)
        elif t.kind == "op" and t.text in (")", "}"):
            if stack:
                c = stack.pop()
                if c is not None:
                    if i > c._start or c.args:
                        c.args.append((c._start, i))
                    c.close = i
        elif t.kind == "op" and t.text == "," and stack and stack[-1] is not None:
            c = stack[-1]
            c.args.append((c._start, i))
            c._start = i + 1
    return [c for c in calls if c.close >= 0]


def _ref_at(sig: list, i: int, res, home: str):
    """(kind, table, name, width) for a column or measure reference starting at token i, else None."""
    t = sig[i]
    nxt = sig[i + 1] if i + 1 < len(sig) else None
    if t.kind in ("table", "ident") and nxt is not None and nxt.kind == "bracket" and nxt.start == t.end:
        tab = res.table(t.value if t.kind == "table" else t.text)
        if not tab:
            return None
        col = res.column(tab, nxt.value)
        if col:
            return ("column", tab, col, 2)
        m = res.measure(nxt.value)
        return ("measure", m[0], m[1], 2) if m else None
    if t.kind == "bracket" and not (i and sig[i - 1].kind in ("table", "ident") and sig[i - 1].end == t.start):
        m = res.measure(t.value)
        if m:
            return ("measure", m[0], m[1], 1)
        col = res.column(home, t.value) if home else None
        if col:
            return ("column", home, col, 1)
    return None


def _refs_in(sig, s: int, e: int, res, home: str) -> list[tuple]:
    out, i = [], s
    while i < e:
        r = _ref_at(sig, i, res, home)
        if r:
            out.append((r[0], r[1], r[2], i))
            i += r[3]
        else:
            i += 1
    return out


def _domain(sig, c: Call, by_at: dict, res) -> str | None:
    """Physical table an iterator walks (FILTER / CALCULATETABLE / ALL(table) wrappers followed), else None."""
    if not c.args:
        return None
    s, e = c.args[0]
    toks = sig[s:e]
    if len(toks) == 1 and toks[0].kind in ("table", "ident"):
        return res.table(toks[0].value if toks[0].kind == "table" else toks[0].text)
    if toks and toks[0].kind == "ident" and toks[0].text.upper() in WRAPPERS and s in by_at:
        inner = by_at[s]
        if inner.name in ("VALUES", "DISTINCT", "ALL", "ALLNOBLANKROW", "ALLSELECTED", "ALLCROSSFILTERED") and \
                inner.args and inner.args[0][1] - inner.args[0][0] != 1:
            return None   # a column list: the distinct values, not the table rows
        return _domain(sig, inner, by_at, res)
    return None


# ---------------------------------------------------------------- helpers

@dataclass
class Finding:
    rule: str
    obj: str
    kind: str
    message: str
    severity: str = "low"
    confidence: str = "medium"
    evidence: list = field(default_factory=list)
    fix: str = ""
    verify: str = ""
    blockers: list = field(default_factory=list)
    members: list = field(default_factory=list)
    identity: str = ""

    def out(self) -> dict:
        cat = RULES[self.rule][0]
        rec = {"id": fingerprint(self.rule, self.identity or self.obj), "rule": self.rule, "category": cat,
               "severity": self.severity, "confidence": self.confidence, "object": self.obj, "kind": self.kind,
               "message": self.message, "evidence": self.evidence or ["static"]}
        for k in ("fix", "verify"):
            if getattr(self, k):
                rec[k] = getattr(self, k)
        if self.blockers:
            rec["blockers"] = self.blockers
        if self.members:
            rec["members"] = self.members
        return rec


def fingerprint(rule: str, identity: str) -> str:
    return "a" + hashlib.sha256(f"{rule}|{identity}".lower().encode("utf-8")).hexdigest()[:11]


def _tag(node, fallback: str) -> str:
    return (node.props.get("lineageTag", "").strip() if node is not None else "") or fallback


def _dax_text(model, node) -> str:
    if node is None:
        return ""
    doc = model.docs[node.file]
    return "\n".join(doc.text[r.start:r.end] for r in tmdl.node_regions(doc, node, deep=False) if r.kind == "dax")


def _index(ws) -> dict:
    try:
        with open(os.path.join(ws.private, "index-summary.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for t in data.get("tables", []):
        out[(t.get("table") or "").lower()] = {"rows": t.get("rows_bucket"),
                                               "cols": {(c.get("column") or "").lower(): c.get("card_bucket")
                                                        for c in t.get("columns", [])}}
    return out


def _memory(ws, project):
    try:
        from . import memory
    except ImportError:
        return None
    try:
        return memory.load(ws, project)
    except Exception:  # noqa: BLE001  (evidence is optional; a bad file means no evidence)
        return None


def _mem_index(mem) -> dict:
    out = {}
    for t in (mem or {}).get("tables", []):
        tname = (t.get("name") or "").lower()
        out[(tname, "")] = t
        for c in t.get("columns", []):
            out[(tname, (c.get("name") or "").lower())] = c
    return out


def _mode(t) -> str:
    modes = {(p.props.get("mode", "") or "import").strip().lower() for p in t.partition_nodes}
    return modes.pop() if len(modes) == 1 else ("mixed" if modes else "import")


def _is_special(t) -> bool:
    """Calculation groups, field parameters and engine-private tables."""
    if t.private or t.calc_items:
        return True
    return any("nameof" in (src or "").lower() for _n, kind, src in t.partitions if kind == "calculated")


def _fact_tables(model) -> set[str]:
    many = {r.from_table for r in model.relationships}
    one = {r.to_table for r in model.relationships}
    return {t for t in many if t not in one}


def _m_text(model, t) -> str:
    return "\n".join(src for _n, kind, src in t.partitions if kind in ("m", "") and src)


def _m_queries(model) -> list[tuple[str, str]]:
    out = []
    for t in model.tables.values():
        if not t.private:
            src = _m_text(model, t)
            if src.strip():
                out.append((t.name, src))
    for name, node in model.expressions.items():
        if node is not None and node.expr is not None:
            src = model.text_of(node.expr)
            if "let" in src:
                out.append((name, src))
    return out


def _norm_steps(src: str) -> list[str] | None:
    steps, _result = mlineage.split_steps(src)
    if not steps:
        return None
    names = {n.lower(): i for i, (n, _t) in enumerate(steps) if n}
    out = []
    for _n, toks in steps:
        words = []
        for t in toks:
            key = (t.value or t.text) if t.kind == "qident" else t.text
            words.append(f"${names[key.lower()]}" if t.kind in ("ident", "qident") and key.lower() in names else t.text)
        out.append(" ".join(words))
    return out


def _nontrivial(step: str) -> bool:
    return any(w.startswith(("Table.", "List.", "Text.", "Date.", "Number.")) for w in step.split())


# ---------------------------------------------------------------- analysers

class Audit:
    def __init__(self, ws, project, g, idx: dict, mem):
        self.ws, self.project, self.g = ws, project, g
        self.model = g.model
        self.res = deps.Resolver(self.model)
        self.idx, self.mem = idx, _mem_index(mem) if mem else {}
        self.has_mem = mem is not None
        self.mem_stale = bool(mem and mem.get("stale"))
        self.mem_model = ((mem or {}).get("model") or {}).get("size_band")
        self.mem_rels = (mem or {}).get("relationships") or []
        self.mem_auto = (mem or {}).get("auto_datetime")
        self.facts = _fact_tables(self.model)
        self.findings: list[Finding] = []
        self._sig: dict = {}
        self._identical: list = []

    def sig(self, text: str):
        if text not in self._sig:
            sig = lexers.significant(lexers.lex_dax(text))
            self._sig[text] = (sig, call_tree(sig))
        return self._sig[text]

    def add(self, f: Finding) -> None:
        self.findings.append(f)

    # evidence ----------------------------------------------------------
    def rows(self, table: str) -> str | None:
        return (self.idx.get(table.lower()) or {}).get("rows")

    def card(self, table: str, col: str) -> str | None:
        return ((self.idx.get(table.lower()) or {}).get("cols") or {}).get(col.lower())

    def band(self, table: str, col: str = "") -> dict:
        return self.mem.get((table.lower(), col.lower()), {})

    def mem_ev(self, table: str, col: str = "") -> list[str]:
        b = self.band(table, col)
        if not b:
            return []
        tag = "memory (stale)" if self.mem_stale else "memory"
        if b.get("size_band") is None:
            return [f"{tag}: bands withheld (strict disclosure)"]
        return [f"{tag}: {b.get('size_band')}, {b.get('share_band', '?')} of the model"]

    def measures(self):
        for t in self.model.tables.values():
            if t.private:
                continue
            for m in t.measures.values():
                if m.expression.strip():
                    yield m

    # DAX: iterators ----------------------------------------------------
    def _iterates(self, text: str, home: str, depth: int, seen: set) -> list[str]:
        """Physical tables walked by iterators in text, or in measures it calls (depth-limited)."""
        sig, calls = self.sig(text)
        by_at = {c.at: c for c in calls}
        out = []
        for c in calls:
            if c.name in ITERATORS:
                d = _domain(sig, c, by_at, self.res)
                if d:
                    out.append(d)
        if depth > 0:
            for kind, tab, name, _i in _refs_in(sig, 0, len(sig), self.res, home):
                if kind == "measure" and (tab, name) not in seen:
                    seen.add((tab, name))
                    m = self.model.tables[tab].measures.get(name)
                    if m is not None:
                        out += [f"{x} (via [{name}])" for x in self._iterates(m.expression, tab, depth - 1, seen)]
        return out

    def iterators(self) -> None:
        for m in self.measures():
            sig, calls = self.sig(m.expression)
            by_at = {c.at: c for c in calls}
            reported = set()
            for c in calls:
                if c.name not in ITERATORS:
                    continue
                dom = _domain(sig, c, by_at, self.res)
                if not dom:
                    continue
                fact = dom in self.facts
                rows = self.rows(dom)
                big = rows == HIGH_COUNT
                ev = [f"rows: {rows} (index)"] if rows else []
                # nested iterators, direct or through called measures
                inner = []
                for k, (s, e) in enumerate(c.args):
                    if k == 0:
                        continue
                    for c2 in calls:
                        if c2.name in ITERATORS and s <= c2.at < e:
                            d2 = _domain(sig, c2, by_at, self.res)
                            if d2:
                                inner.append(d2)
                    for kind, tab, name, _i in _refs_in(sig, s, e, self.res, m.table):
                        if kind == "measure":
                            mm = self.model.tables[tab].measures.get(name)
                            if mm is not None:
                                inner += [f"{x} (via [{name}])" if "(via" not in x else x
                                          for x in self._iterates(mm.expression, tab, 3, {(m.table, m.name)})]
                key = (c.name, dom)
                if key in reported:
                    continue
                if inner:
                    reported.add(key)
                    sev = "medium" if (fact and (big or rows is None)) or big else "low"
                    self.add(Finding(
                        "NESTED_ITERATOR", f"[{m.name}]", "measure",
                        f"{c.name}('{dom}') iterates {', '.join(sorted(set(inner))[:4])} for every row: reduce the "
                        "outer grain (iterate VALUES of the needed columns) or hoist the inner part into a VAR when it "
                        "does not depend on the row",
                        severity=sev, confidence="medium", evidence=ev or ["static (rows unknown: run pbz index)"],
                        fix="rewrite, then compare old and new with pbz synth check (equal) before pbz check",
                        identity=f"{_tag(m.node, m.name)}|{c.name}|{dom}"))
                    continue
                if not fact and not big:
                    continue
                called = sorted({f"[{name}]" for k, (s, e) in enumerate(c.args) if k
                                 for kind, _t, name, _i in _refs_in(sig, s, e, self.res, m.table) if kind == "measure"})
                if not called:
                    continue
                reported.add(key)
                self.add(Finding(
                    "ITERATOR_MEASURE_CALL", f"[{m.name}]", "measure",
                    f"{c.name}('{dom}') evaluates {', '.join(called[:4])} once per row of '{dom}' (context transition "
                    "per row). Iterate the distinct values that matter (VALUES or SUMMARIZE of the grouping columns) or "
                    "aggregate the column directly when the measure is a plain aggregation",
                    severity="medium" if big else "low", confidence="medium" if fact else "low",
                    evidence=ev or ["static (rows unknown: run pbz index)"],
                    fix="rewrite, then compare old and new with pbz synth check (equal) before pbz check",
                    identity=f"{_tag(m.node, m.name)}|{c.name}|{dom}"))

    def summarize(self) -> None:
        items = [(f"[{m.name}]", "measure", m.table, m.expression, m.node) for m in self.measures()]
        for t in self.model.tables.values():
            if t.private:
                continue
            for _n, kind, src in t.partitions:
                if kind == "calculated":
                    items.append((t.name, "table", t.name, src, t.node))
            for c in t.columns.values():
                if c.calculated:
                    items.append((f"{t.name}[{c.name}]", "column", t.name, _dax_text(self.model, c.node), c.node))
        for label, kind, home, text, node in items:
            sig, calls = self.sig(text)
            for c in calls:
                if c.name == "SUMMARIZE" and any(e - s == 1 and sig[s].kind == "str" for s, e in c.args[1:]):
                    self.add(Finding(
                        "SUMMARIZE_EXTENSION_COLUMNS", label, kind,
                        "SUMMARIZE with name/expression pairs: use ADDCOLUMNS(SUMMARIZE(<table>, <group columns>), "
                        "\"<name>\", CALCULATE(<expression>)) or SUMMARIZECOLUMNS",
                        severity="low", confidence="high",
                        fix="rewrite, then compare old and new with pbz synth check (equal)",
                        identity=f"{_tag(node, label)}|summarize"))
                    break

    # calculated columns ------------------------------------------------
    def calc_columns(self) -> None:
        for t in self.model.tables.values():
            if t.private or t.calculated:
                continue
            for c in t.columns.values():
                if not c.calculated or c.node is None:
                    continue
                text = _dax_text(self.model, c.node)
                sig, calls = self.sig(text)
                if not sig:
                    continue
                label = f"'{t.name}'[{c.name}]"
                ident = _tag(c.node, label)
                k = usage.key("column", t.name, c.name)
                refs = _refs_in(sig, 0, len(sig), self.res, t.name)
                # pure copies
                core = [x for x in sig if not (x.kind == "op" and x.text in "()")]
                if len(calls) == 1 and calls[0].name == "RELATED" and len(core) == 3 and refs:
                    _k, rt, rc, _i = refs[0]
                    self.add(Finding(
                        "RELATED_COPY_COLUMN", label, "column",
                        f"copies '{rt}'[{rc}] into '{t.name}': bind visuals and measures to '{rt}'[{rc}] instead and "
                        "remove the copy (keeps one column and one dictionary)",
                        severity="low", confidence="high", blockers=self._blockers(k, allow_dax=False),
                        fix=f"move the bindings to '{rt}'[{rc}], then pbz unused --remove --only columns",
                        identity=ident))
                    continue
                if not calls and len(refs) == 1 and refs[0][0] == "column" and len(core) <= 2 and refs[0][1] == t.name \
                        and refs[0][2] != c.name:
                    self.add(Finding(
                        "COPY_COLUMN", label, "column", f"repeats '{t.name}'[{refs[0][2]}]: use that column",
                        severity="low", confidence="high", blockers=self._blockers(k, allow_dax=False),
                        fix=f"pbz consolidate merge is for measures; move the bindings to '{t.name}'[{refs[0][2]}] "
                            "and remove the copy", identity=ident))
                    continue
                aggregated = self._aggregate_only(k, t.name, c.name)
                if aggregated is not None and c.data_type.lower() in ("int64", "double", "decimal"):
                    fns, users = aggregated
                    rows = self.rows(t.name)
                    big = rows == HIGH_COUNT or self.band(t.name, c.name).get("size_band") in SOME_BANDS
                    xs = ", ".join(sorted({AGG1[f] for f in fns}))
                    self.add(Finding(
                        "CALC_COLUMN_AS_MEASURE", label, "column",
                        f"only aggregated ({', '.join(sorted(fns))}) in {len(users)} measure(s): replace each "
                        f"aggregation with {xs}('{t.name}', <the column's expression>) and remove the column. "
                        "Trades storage for query time: keep it if those measures become slow",
                        severity="medium" if big else "low", confidence="medium",
                        evidence=([f"rows: {rows} (index)"] if rows else []) + self.mem_ev(t.name, c.name),
                        members=sorted(users)[:20],
                        fix="rewrite the measures, then pbz unused --remove --only columns",
                        verify="pbz synth check equal of each measure before and after (old expression as left)",
                        identity=ident))
                    continue
                fn_names = {x.name for x in calls}
                row_local = fn_names <= ROW_LOCAL and all(kind == "column" and tab == t.name
                                                          for kind, tab, _n, _i in refs)
                if row_local and _m_text(self.model, t).strip():
                    card = self.card(t.name, c.name)
                    self.add(Finding(
                        "CALC_COLUMN_UPSTREAM", label, "column",
                        "row-local expression (same-table columns, scalar functions only): add it in Power Query "
                        "(Table.AddColumn) or the source. Columns computed before compression usually compress "
                        "better and refresh faster",
                        severity="medium" if card == HIGH_COUNT else "low", confidence="medium",
                        evidence=([f"distinct: {card} (index)"] if card else []) + self.mem_ev(t.name, c.name),
                        fix="add the Power Query step, refresh, then pbz unused --remove the old column after moving "
                            "its bindings", verify="pbz snapshot save before, pbz snapshot compare after refresh",
                        identity=ident))

    def _blockers(self, k, allow_dax: bool = True) -> list[str]:
        out = []
        for e in self.g.inc.get(k, []):
            if e.via in ("sortBy", "level", "relationship", "rls", "alternateOf", "variation"):
                out.append(f"{e.via} ({self.g.objects[e.src].label()})")
            elif e.via == "dax" and not allow_dax:
                out.append(f"used in {self.g.objects[e.src].label()}")
        out += [f"locked: {r}" for r in self.g.locks.get(k, [])]
        if self.g.ext_usages.get(k):
            out.append("used by registered external consumers")
        return sorted(set(out))[:10]

    def _aggregate_only(self, k, table: str, col: str):
        """({aggregations}, {measures}) when every use of the column is SUM/AVERAGE/MIN/MAX(col) in a measure."""
        if self.g.usages.get(k) or self.g.ext_usages.get(k) or self.g.locks.get(k):
            return None
        fns, users = set(), set()
        for e in self.g.inc.get(k, []):
            src = self.g.objects.get(e.src)
            if e.via != "dax" or src is None or src.kind != "measure":
                return None
            m = self.model.tables[src.table].measures.get(src.name)
            if m is None:
                return None
            sig, calls = self.sig(m.expression)
            found = False
            for kind, tab, name, i in _refs_in(sig, 0, len(sig), self.res, m.table):
                if kind != "column" or tab != table or name != col:
                    continue
                c = next((x for x in calls if x.args and x.args[0][0] <= i < x.args[0][1]
                          and len(x.args) == 1 and x.args[0][1] - x.args[0][0] in (1, 2)), None)
                if c is None or c.name not in AGG1:
                    return None
                fns.add(c.name)
                found = True
            if not found:   # referenced elsewhere in the measure (format string, detail rows): cannot prove it
                return None
            users.add(f"[{m.name}]")
        return (fns, users) if users else None

    # relationships and storage -----------------------------------------
    def relationships(self) -> None:
        for r in self.model.relationships:
            if r.both:
                card = self.card(r.to_table, r.to_column)
                if card == HIGH_COUNT:
                    self.add(Finding(
                        "HIGH_CARDINALITY_BIDIRECTIONAL", f"{r.from_table}[{r.from_column}] <-> {r.to_table}[{r.to_column}]",
                        "relationship", "filters both directions on a key with 10k+ distinct values (Microsoft's rule "
                        "flags above 100k; the index bucket cannot tell): single direction plus CROSSFILTER in the "
                        "measures that need it", severity="medium", confidence="low",
                        evidence=[f"distinct: {card} (index)"],
                        fix=f"pbz relationship set \"'{r.from_table}'[{r.from_column}]\" \"'{r.to_table}'[{r.to_column}]\" "
                            "(single direction), CROSSFILTER(..., Both) where needed",
                        verify="pbz synth check of the affected measures, then pbz smoke on the pages that slice by it",
                        identity=_tag(r.node, r.name)))
        tables = self.model.tables
        for r in self.model.relationships:
            f, d = tables.get(r.from_table), tables.get(r.to_table)
            if f is None or d is None or f.private or d.private:
                continue
            if _mode(f) == "directquery" and _mode(d) == "import":
                self.add(Finding(
                    "MIXED_STORAGE_DIMENSION", d.name, "table",
                    f"Import dimension filters the DirectQuery table '{f.name}': the relationship is limited and "
                    "queries join across storage engines. Set the dimension's partitions to mode: dual",
                    severity="medium", confidence="medium",
                    fix="mode: dual on the dimension's partitions (same source as the fact)",
                    verify="pbz desktop open, pbz health, pbz smoke", identity=_tag(d.node, d.name) + "|" + f.name))

    # memory evidence ---------------------------------------------------
    def memory(self) -> None:
        if not self.has_mem:
            return
        small_model = self.mem_model in SMALL_MODEL   # shares of a tiny model point at nothing worth fixing
        for t in self.model.tables.values():
            if t.private:
                continue
            for c in t.columns.values():
                b = self.band(t.name, c.name)
                if not b:
                    continue
                label = f"'{t.name}'[{c.name}]"
                ident = _tag(c.node, label)
                k = usage.key("column", t.name, c.name)
                state = self.g.state.get(k, "unknown")
                ev = self.mem_ev(t.name, c.name)
                size, share = b.get("size_band"), b.get("share_band")
                banded = size is not None          # the strict tier withholds bands, predicates stay
                hot = banded and not small_model and (share in HIGH_SHARE or size in BIG_BANDS)
                dtype = c.data_type.lower()
                if hot:
                    advice = ("unused: remove it first (pbz unused --remove)" if state == "unused" else
                              "reduce its distinct values (split date and time, round, move long text out, keep keys "
                              "as whole numbers) or move it to the source if reports never show it")
                    self.add(Finding("MEMORY_HOTSPOT", label, "column", f"{size}, {share} of the model; {state}: {advice}",
                                     severity="high", confidence="high", evidence=ev, identity=ident))
                if dtype.startswith("datetime") and b.get("time_part") is True and (not banded or size in SOME_BANDS):
                    self.add(Finding(
                        "DATETIME_TIME_PART", label, "column",
                        "stores times of day: split into a date column and a time column, or truncate to the date in "
                        "Power Query (a date format string does not remove the time)",
                        severity="medium" if banded else "low", confidence="high",
                        evidence=ev + ["predicate: time part present"],
                        verify="pbz snapshot save before, compare after refresh (results by date must not change)",
                        identity=ident))
                if dtype in ("double", "decimal") and b.get("fractional") is False and (not banded or size in SOME_BANDS):
                    self.add(Finding(
                        "DOUBLE_WITHOUT_FRACTIONS", label, "column",
                        "holds whole numbers only: change the type to whole number (Int64.Type) in Power Query; make "
                        "sure the source cannot deliver fractions later",
                        severity="low", confidence="medium", evidence=ev + ["predicate: no fractional values"],
                        identity=ident))
                if dtype == "string" and b.get("long_text") is True and (not banded or size in MID_BANDS):
                    self.add(Finding(
                        "LONG_TEXT", label, "column",
                        "long text values: keep comments and descriptions out of the model or trim them in Power Query",
                        severity="medium" if banded else "low", confidence="medium",
                        evidence=ev + ["predicate: values over 100 characters"], identity=ident))
                if banded and b.get("dictionary_heavy") and size in MID_BANDS and not hot:
                    self.add(Finding(
                        "DICTIONARY_HEAVY", label, "column",
                        "most of its storage is the dictionary of distinct values: lower the cardinality or remove it "
                        "if unused", severity="medium", confidence="high", evidence=ev, identity=ident))
        for r in self.mem_rels:
            if r.get("size_band") in MID_BANDS:
                self.add(Finding(
                    "LARGE_RELATIONSHIP", f"{r.get('from', '?')} -> {r.get('to', '?')}", "relationship",
                    f"relationship structure in the {r['size_band']} band: its key has very many distinct values. Check "
                    "the grain (a fact-to-fact or detail-level relationship) and use whole-number surrogate keys",
                    severity="medium", confidence="high", evidence=[f"memory: {r['size_band']}"],
                    identity=f"rel|{r.get('from', '')}|{r.get('to', '')}"))
        auto = self.mem_auto or {}
        if auto.get("share_band") in ("1-5%",) + HIGH_SHARE and not small_model:
            self.add(Finding(
                "AUTO_DATETIME_STORAGE", "auto date/time tables", "model",
                f"hidden auto date/time tables hold {auto['share_band']} of the model: turn off Auto date/time "
                "(File > Options > Data Load, current file) once visuals use a marked date table",
                severity="medium" if auto.get("share_band") in HIGH_SHARE else "low", confidence="high",
                evidence=[f"memory: {auto.get('size_band', '?')}, {auto['share_band']} of the model"],
                fix="pbz bpa (REMOVE_AUTO-DATE_TABLE) lists the columns; the Desktop option is the user's",
                identity="model|auto-datetime"))

    def unused_columns(self) -> None:
        for t in self.model.tables.values():
            if t.private:
                continue
            for c in t.columns.values():
                k = usage.key("column", t.name, c.name)
                if self.g.state.get(k) != "unused":
                    continue
                card, b = self.card(t.name, c.name), self.band(t.name, c.name)
                # text columns reserve dictionary space, so "1-10 MiB" alone does not make a column expensive
                big = b.get("size_band") in MID_BANDS or (b.get("share_band") in HIGH_SHARE
                                                          and self.mem_model not in SMALL_MODEL)
                if card != HIGH_COUNT and not big:
                    continue
                self.add(Finding(
                    "UNUSED_HIGH_CARDINALITY", f"'{t.name}'[{c.name}]", "column",
                    "unused and expensive: remove it before smaller cleanups",
                    severity="medium", confidence="high",
                    evidence=([f"distinct: {card} (index)"] if card else []) + self.mem_ev(t.name, c.name),
                    fix="pbz unused --remove --only columns --dry-run, then without --dry-run",
                    identity=_tag(c.node, f"{t.name}[{c.name}]")))

    # Power Query --------------------------------------------------------
    def power_query(self) -> None:
        norm = {}
        for name, src in _m_queries(self.model):
            steps = _norm_steps(src)
            if steps:
                norm[name] = steps
            if steps:
                buf = [i for i, s in enumerate(steps) if "Table.Buffer" in s.split()]
                if buf and any("Table.SelectRows" in s.split() for s in steps[buf[0] + 1:]):
                    self.add(Finding(
                        "M_BUFFER_BEFORE_FILTER", name, "query",
                        "Table.Buffer holds the whole table in memory and stops query folding before a later "
                        "Table.SelectRows: filter first, or drop the buffer", severity="low", confidence="medium",
                        identity=f"{name}|buffer"))
        groups: dict = {}
        for name, steps in norm.items():
            if len(steps) >= 3 and sum(_nontrivial(s) for s in steps[:3]) >= 2:
                groups.setdefault(tuple(steps[:3]), []).append(name)
        for key, names in groups.items():
            if len(names) < 2:
                continue
            shared = 3
            while all(len(norm[n]) > shared for n in names) and len({norm[n][shared] for n in names}) == 1:
                shared += 1
            self.add(Finding(
                "M_SHARED_STEPS", ", ".join(sorted(names)), "query",
                f"{len(names)} queries repeat the same source and their first {shared} steps: move them into one "
                "staging query (load disabled) or a function and reference it. This consolidates code; each "
                "referencing query still evaluates the source (Power Query has no cross-query cache)",
                severity="low", confidence="high", members=sorted(names),
                identity="|".join(sorted(n.lower() for n in names))))

    # consolidation -------------------------------------------------------
    def duplicates_and_families(self) -> None:
        sim = similar.run(self.ws, self.project, max_pairs=0)
        self._identical = [grp["measures"] for grp in sim.get("identical", [])]
        for grp in sim.get("identical", []):
            ms = grp["measures"]
            keep = self._most_used(ms)
            rest = [x for x in ms if x != keep]
            self.add(Finding(
                "DUPLICATE_MEASURE", ", ".join(ms), "measure",
                f"{len(ms)} measures have identical DAX: keep {keep}, merge the others into it",
                severity="low", confidence="high", members=ms,
                fix="; ".join(f"pbz consolidate merge \"{x}\" --into \"{keep}\"" for x in rest[:5]),
                verify="push validates the rewritten references; pbz smoke on the pages that used them",
                identity="|".join(sorted(self._mtag(x) for x in ms))))
        fams = self._families()
        in_family = {(x if x.startswith("[") else f"[{x}]").lower() for f in fams for x in f.get("members", [])}
        for f in fams:
            members = [x if x.startswith("[") else f"[{x}]" for x in f.get("members", [])]
            items = [tp.get("item") for tp in f.get("templates", [])]
            bases = [x if x.startswith("[") else f"[{x}]" for x in f.get("bases", [])]
            strong = len(items) >= 2 and len(bases) >= FAMILY_MIN
            self.add(Finding(
                "CALC_GROUP_FAMILY", ", ".join(members[:6]) + (" ..." if len(members) > 6 else ""), "measure",
                f"{len(items)} transformation(s) ({', '.join(str(i) for i in items[:5])}) applied to {len(bases)} base "
                f"measures ({', '.join(bases[:5])}): one calculation group with {len(items)} item(s) replaces "
                f"{len(members)} measures' logic (the measures can stay as thin wrappers so reports keep working)",
                severity="medium" if strong else "low", confidence=f.get("confidence", "medium"), members=members,
                fix=f"pbz consolidate calc-group --family {f.get('id')} --name \"<group name>\"",
                verify="the verb writes a check spec: pbz synth check --spec <file>, then pbz check <file> after push",
                identity="|".join(sorted(self._mtag(x) for x in members))))
        # families by slot kind: literals only (filtered variants) or columns only (one formula, other columns)
        lit_groups: dict = {}
        col_groups: dict = {}
        dup_rest = {x.lower() for grp in self._identical for x in grp[1:]}
        for m in self.measures():
            if f"[{m.name}]".lower() in dup_rest:
                continue
            cn = similar.canon(m.expression, m.table, self.res)
            if len(cn.toks) < similar.MIN_SHAPE_TOKENS:
                continue
            if any(k == "lit" for _t, k, _v in cn.toks):
                lit_groups.setdefault(similar._h(" ".join(cn.words("slot"))), []).append(f"[{m.name}]")
            if any(k == "col" for _t, k, _v in cn.toks):
                words = ["COL" if k == "col" else ("LIT:" + v if k == "lit" else t) for t, k, v in cn.toks]
                col_groups.setdefault(similar._h(" ".join(words)), []).append(f"[{m.name}]")
        for kind, groups in (("lit", lit_groups), ("col", col_groups)):
            for ms in groups.values():
                if len(ms) < FAMILY_MIN:
                    continue
                if kind == "col":
                    self.add(Finding(
                        "PARAMETERIZED_FAMILY", ", ".join(ms[:6]), "measure",
                        f"{len(ms)} measures share one formula over different columns: a DAX user-defined function "
                        "with column parameters can hold the logic once (compatibility level 1702)",
                        severity="low", confidence="medium", members=ms,
                        fix="pbz function add <Name> --params \"(col : ANYREF)\" --body ... --upgrade-compat, then call it",
                        verify="pbz synth check equal of each measure before and after",
                        identity="|".join(sorted(self._mtag(x) for x in ms))))
                else:
                    self.add(Finding(
                        "FILTERED_FAMILY", ", ".join(ms[:6]), "measure",
                        f"{len(ms)} measures differ only by literal values: one base measure plus a slicer, a field "
                        "parameter or a calculation group may replace them. Keep them when each value is its own "
                        "business definition", severity="info", confidence="medium", members=ms,
                        identity="|".join(sorted(self._mtag(x) for x in ms))))

    def _families(self) -> list[dict]:
        try:
            from . import consolidate
            return list(consolidate.families(self.model))
        except (ImportError, AttributeError):
            return []

    def _mtag(self, label: str) -> str:
        m = self.model.measure(label.strip("[]"))
        return _tag(m.node if m else None, label)

    def _most_used(self, labels: list[str]) -> str:
        def score(label):
            m = self.model.measure(label.strip("[]"))
            if m is None:
                return (0, label)
            k = usage.key("measure", m.table, m.name)
            return (len(self.g.usages.get(k, [])) + len(self.g.inc.get(k, [])), label)
        return sorted(labels, key=lambda x: (-score(x)[0], x.lower()))[0]

    def subexpressions(self) -> None:
        seen: dict[str, dict] = {}
        dup_rest = {x.lower() for grp in self._identical for x in grp[1:]}   # duplicates count once
        for m in self.measures():
            if f"[{m.name}]".lower() in dup_rest:
                continue
            sig, calls = self.sig(m.expression)
            text = m.expression
            for c in calls:
                n = c.close - c.at + 1
                if n < SUBEXPR_MIN_TOKENS or (c.at == 0 and c.close == len(sig) - 1):
                    continue
                sub = text[sig[c.at].start:sig[c.close].end]
                key = similar._h(" ".join(similar.canon(sub, m.table, self.res).words("hash")))
                rec = seen.setdefault(key, {"n": n, "measures": {}, "ctx": set()})
                rec["measures"].setdefault(m.name, (c.at, c.close))
                ctx = "row" if any((p.name in ITERATORS or p.name in ("CALCULATE", "CALCULATETABLE"))
                                   and q.parent_arg >= 1 for p, q in _ancestor_pairs(c)) else "filter"
                rec["ctx"].add(ctx)
        cands = sorted(((k, v) for k, v in seen.items() if len(v["measures"]) >= SUBEXPR_MIN_MEASURES),
                       key=lambda kv: (-kv[1]["n"], sorted(kv[1]["measures"])))
        kept: dict[str, list] = {}
        reported = 0
        for key, v in cands:
            spans = v["measures"]
            if all(any(s <= spans[mn][0] and spans[mn][1] <= e for s, e in kept.get(mn, [])) for mn in spans):
                continue
            for mn, sp in spans.items():
                kept.setdefault(mn, []).append(sp)
            names = sorted(f"[{x}]" for x in spans)
            how = ("a helper measure is safe (filter context only)" if v["ctx"] == {"filter"} else
                   "it runs in row or filter-argument context: use a VAR per measure or a DAX user-defined "
                   "function (a measure there would add context transition)")
            self.add(Finding(
                "REPEATED_SUBEXPRESSION", ", ".join(names[:6]) + (" ..." if len(names) > 6 else ""), "measure",
                f"the same {v['n']}-token sub-expression appears in {len(names)} measures: {how}",
                severity="low", confidence="medium", members=names,
                fix="pbz function add <Name> --params ... --body ... (or a new measure), then call it",
                verify="pbz synth check equal of each measure before and after",
                identity=key))
            reported += 1
            if reported >= 50:
                break

    def design(self) -> None:
        homes, has_measure_table = [], False
        for t in self.model.tables.values():
            if _is_special(t):
                continue
            visible = [m for m in t.measures.values() if m.node is None or not m.node.flag("isHidden")]
            if visible:
                homes.append(t.name)
            if t.measures and all(c.hidden for c in t.columns.values()):
                has_measure_table = True
            loose = [m for m in visible if not m.folder]
            if len(loose) > 10:
                self.add(Finding(
                    "UNFOLDERED_MEASURES", t.name, "table",
                    f"{len(loose)} visible measures without a display folder: group them so report authors find them",
                    severity="info", confidence="high",
                    fix=f"pbz set --table \"{t.name}\" --kind measure --match \"<pattern>\" displayFolder=<Folder>",
                    identity=_tag(t.node, t.name)))
        total = sum(len(self.model.tables[h].measures) for h in homes)
        if len(homes) >= 4 and total >= 20 and not has_measure_table:
            self.add(Finding(
                "MEASURES_SCATTERED", ", ".join(sorted(homes)[:8]) + (" ..." if len(homes) > 8 else ""), "model",
                f"measures live in {len(homes)} tables and there is no measure table: one table of measures "
                "(with display folders) is easier to navigate and to secure",
                severity="info", confidence="medium",
                fix="pbz new measure-table Measures, then pbz move measure <name> --to Measures (visuals follow)",
                identity="model|measure-homes"))

    def tables(self) -> None:
        groups: dict = {}
        dates = []
        for t in self.model.tables.values():
            if _is_special(t):
                continue
            srcs = []
            for _n, kind, src in t.partitions:
                if kind == "calculated":
                    srcs.append("dax:" + " ".join(similar.canon(src, t.name, self.res).words("hash")))
                else:
                    steps = _norm_steps(src)
                    srcs.append("m:" + (" ; ".join(steps) if steps else " ".join(
                        x.text for x in lexers.significant(lexers.lex_m(src)))))
            cols = sorted(c.name.lower() for c in t.columns.values() if not c.calculated)
            if srcs and cols:
                groups.setdefault((tuple(sorted(srcs)), tuple(cols)), []).append(t.name)
            text = " ".join(src for _n, _k, src in t.partitions).upper()
            if (t.node is not None and t.node.prop("dataCategory").lower() == "time") or "CALENDAR(" in \
                    text.replace(" ", "") or "CALENDARAUTO(" in text.replace(" ", "") or "LIST.DATES" in text:
                dates.append(t.name)
        for names in groups.values():
            if len(names) < 2:
                continue
            facts = [{r.from_table for r in self.model.relationships if r.to_table == n} for n in names]
            role = bool(set.intersection(*facts)) if all(facts) else False
            self.add(Finding(
                "DUPLICATE_TABLE", ", ".join(sorted(names)), "table",
                f"{len(names)} tables load the same query with the same columns" + (
                    ": role-playing copies of one dimension. Keep them only if both roles must filter at once; "
                    "otherwise one table plus USERELATIONSHIP" if role else ": load it once and relate to that table"),
                severity="low", confidence="high", members=sorted(names),
                evidence=[f"rows: {self.rows(n)} (index)" for n in names if self.rows(n)][:1] or ["static"],
                identity="|".join(sorted(_tag(self.model.tables[n].node, n) for n in names))))
        if len(dates) > 1:
            roles = []
            for d in sorted(dates):
                to = sorted({r.from_table for r in self.model.relationships if r.to_table == d})
                roles.append(f"{d} ({', '.join(to) if to else 'no relationships'})")
            self.add(Finding(
                "MULTIPLE_DATE_TABLES", ", ".join(sorted(dates)), "table",
                f"{len(dates)} date tables: {'; '.join(roles)}. One marked date table plus USERELATIONSHIP for "
                "other date roles is simpler; keep separate tables only when two date roles must filter at once, "
                "and then generate them from one shared definition", severity="low", confidence="medium",
                members=sorted(dates), identity="|".join(sorted(_tag(self.model.tables[d].node, d) for d in dates))))


def _ancestor_pairs(c: Call):
    """(ancestor, child-on-path) pairs: child.parent_arg says which argument of the ancestor holds c."""
    q, p = c, c.parent
    while p is not None:
        yield p, q
        q, p = p, p.parent


# ---------------------------------------------------------------- baselines, waivers, gate

def _audit_dir(ws) -> str:
    return os.path.join(ws.pbz_dir, "audit")


def _load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def _baseline_path(ws, name: str) -> str:
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_.") or "baseline"
    return os.path.join(_audit_dir(ws), "baselines", safe + ".json")


def waive(ws, project, fid: str, reason: str | None, expires: str | None = None, remove: bool = False,
          by: str = "agent") -> dict:
    path = os.path.join(_audit_dir(ws), "waivers.json")
    data = _load_json(path, {})
    if remove:
        if fid not in data:
            raise PbzError("NOT_FOUND", f"no waiver {fid}")
        del data[fid]
        _save_json(path, data)
        return {"verb": "audit", "action": "unwaive", "id": fid, "result": "REMOVED"}
    if not reason or not reason.strip():
        raise PbzError("BAD_REQUEST", "a waiver needs --reason")
    if expires:
        try:
            _dt.date.fromisoformat(expires)
        except ValueError:
            raise PbzError("BAD_REQUEST", "--expires takes YYYY-MM-DD") from None
    current = {f["id"]: f for f in run(ws, project, _gate_only=True)["findings"]}
    if fid not in current:
        raise PbzError("NOT_FOUND", f"no current finding {fid} (run pbz audit)")
    f = current[fid]
    data[fid] = {"rule": f["rule"], "object": f["object"], "reason": reason.strip()[:300], "expires": expires,
                 "created": _dt.date.today().isoformat(), "by": by}
    _save_json(path, data)
    return {"verb": "audit", "action": "waive", "id": fid, "rule": f["rule"], "object": f["object"],
            "expires": expires, "result": "WAIVED"}


def run(ws, project, rules: list[str] | None = None, category: str | None = None, min_severity: str | None = None,
        since: str | None = None, save_baseline: str | None = None, fail_on: str | None = None,
        coverage: bool = False, max_findings: int = MAX_FINDINGS, _gate_only: bool = False) -> dict:
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    if rules:
        bad = [r for r in rules if r not in RULES]
        if bad:
            raise PbzError("BAD_REQUEST", f"unknown rule(s) {', '.join(bad)} (known: {', '.join(sorted(RULES))})")
    if category and category not in ("efficiency", "consolidation", "design"):
        raise PbzError("BAD_REQUEST", "category is efficiency, consolidation or design")
    if min_severity and min_severity not in SEVERITY:
        raise PbzError("BAD_REQUEST", f"min severity is one of {', '.join(SEVERITY)}")
    g = usage.build(ws, project)
    idx = _index(ws)
    mem = _memory(ws, project)
    a = Audit(ws, project, g, idx, mem)
    for step in (a.iterators, a.summarize, a.calc_columns, a.relationships, a.memory, a.unused_columns,
                 a.power_query, a.duplicates_and_families, a.subexpressions, a.tables, a.design):
        step()
    out_f = [f.out() for f in a.findings]
    seen_ids, uniq = set(), []
    for f in out_f:
        if f["id"] not in seen_ids:
            seen_ids.add(f["id"])
            uniq.append(f)
    out_f = uniq
    if rules:
        out_f = [f for f in out_f if f["rule"] in rules]
    if category:
        out_f = [f for f in out_f if f["category"] == category]
    if min_severity:
        out_f = [f for f in out_f if SEVERITY.index(f["severity"]) <= SEVERITY.index(min_severity)]
    out_f.sort(key=lambda f: (SEVERITY.index(f["severity"]), f["category"], f["rule"], f["object"].lower()))
    waivers = _load_json(os.path.join(_audit_dir(ws), "waivers.json"), {})
    today = _dt.date.today().isoformat()
    waived, expired, active = [], [], []
    for f in out_f:
        w = waivers.get(f["id"])
        if w and (not w.get("expires") or w["expires"] >= today):
            waived.append({"id": f["id"], "rule": f["rule"], "object": f["object"], "reason": w.get("reason"),
                           "expires": w.get("expires")})
        else:
            if w:
                expired.append(f["id"])
            active.append(f)
    if _gate_only:
        return {"findings": out_f}
    result = {"verb": "audit", "project": project.name,
              "evidence": {"static": True, "index": bool(idx),
                           "memory": "none" if mem is None else ("stale" if mem.get("stale") else "fresh")}}
    by_cat, by_sev = {}, {}
    for f in active:
        by_cat[f["category"]] = by_cat.get(f["category"], 0) + 1
        by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
    result["summary"] = {"findings": len(active), "by_category": by_cat, "by_severity": by_sev,
                         "waived": len(waived)}
    if since:
        base = _load_json(_baseline_path(ws, since), None)
        if base is None:
            raise PbzError("NOT_FOUND", f"no audit baseline {since} (pbz audit --save-baseline {since})")
        old = {r["id"]: r for r in base.get("findings", [])}
        for f in active:
            f["status"] = "persistent" if f["id"] in old else "new"
        now = {f["id"] for f in out_f}
        result["baseline"] = {"name": since, "saved": base.get("saved"),
                              "new": sum(1 for f in active if f["status"] == "new"),
                              "persistent": sum(1 for f in active if f["status"] == "persistent"),
                              "resolved": [{"id": i, "rule": r["rule"], "object": r["object"]}
                                           for i, r in old.items() if i not in now][:100]}
    result["findings"] = active[:max_findings]
    if len(active) > max_findings:
        result["more"] = len(active) - max_findings
    if waived:
        result["waived"] = waived[:100]
    if expired:
        result["expired_waivers"] = expired
    skipped = []
    for rid, (cat, need, src, about) in sorted(RULES.items()):
        status = "ran"
        if need == "memory" and mem is None:
            status = "skipped: needs pbz audit memory (Desktop open)"
        elif need == "memory" and mem.get("stale"):
            status = "ran on stale memory bands (rerun pbz audit memory)"
        elif need == "index" and not idx:
            status = "ran without buckets (pbz index adds severity evidence)"
        elif rid == "CALC_GROUP_FAMILY" and not _has_families():
            status = "skipped: consolidate module unavailable"
        if coverage:
            skipped.append({"rule": rid, "category": cat, "evidence": need, "source": src, "about": about,
                            "status": status})
        elif status != "ran":
            skipped.append({"rule": rid, "status": status})
    if skipped:
        result["coverage"] = skipped
    if save_baseline:
        _save_json(_baseline_path(ws, save_baseline), {
            "saved": _dt.datetime.now().isoformat(timespec="seconds"), "project": project.name,
            "findings": [{"id": f["id"], "rule": f["rule"], "object": f["object"], "severity": f["severity"]}
                         for f in out_f]})
        result["baseline_saved"] = save_baseline
    if fail_on:
        if fail_on not in ("new", "any", "high"):
            raise PbzError("BAD_REQUEST", "--fail-on is new, any or high")
        if fail_on == "new" and not since:
            raise PbzError("BAD_REQUEST", "--fail-on new needs --since <baseline>")
        bad = [f for f in active if (fail_on == "any" and f["severity"] != "info")
               or (fail_on == "high" and f["severity"] == "high")
               or (fail_on == "new" and f.get("status") == "new")]
        result["gate"] = "FAIL" if bad else "PASS"
        if bad:
            result["gate_findings"] = [f["id"] for f in bad[:50]]
    result["next"] = ("fix by finding (each has fix / verify), or accept known debt: pbz audit waive <id> --reason ...; "
                      "track change: pbz audit --save-baseline <name>, later --since <name>")
    return result


def _has_families() -> bool:
    try:
        from . import consolidate
        return hasattr(consolidate, "families")
    except ImportError:
        return False
