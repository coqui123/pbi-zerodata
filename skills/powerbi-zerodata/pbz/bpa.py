"""`pbz bpa`: best practice analysis of the model and its reports, with Measure Killer-style scores.

The rule catalog is data (rules/model.json, rules/report.json: id, severity, evidence, message, source);
the logic is one predicate per rule id below. Rules are adapted from microsoft/Analysis-Services
BestPracticeRules (MIT), NatVanG/fab-inspector Base-rules (MIT), akhilannan/pbir-utils (MIT) and
microsoft/semantic-link-labs (MIT); each catalog entry names its source. Everything runs on the
sanitized mirror. Findings name objects and count things; they never quote literal values from DAX, M
or PBIR. Data-derived rules (evidence "data") are listed in the catalog but not run.

Scores: model = sum(severity x violations) x log10(tables + columns + measures + relationships), graded
at 75 / 300 / 600 / 900 / 1200; report = sum(severity x (1 + excess)), graded at 50 / 100 / 150 / 250 / 300.
`per100` is the score per 100 objects and `rate` (0-100) the severity-weighted share of checks passed,
so models of different sizes compare. Scores only compare under the same `ruleset` hash.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field

from . import deps, lexers, pbir, tmdl, usage
from .common import PbzError, read_text

RULES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules")
CUTOFFS = {"model": (75, 300, 600, 900, 1200), "report": (50, 100, 150, 250, 300)}
LABELS = {"letters": ["A", "B", "C", "D", "E", "F"],
          "measure-killer": ["Power BI Pro", "Good", "Ok", "Poor", "Awful", "Power BI Criminal"]}
IGNORE = "BestPracticeAnalyzer_IgnoreRules"
MAX_FINDINGS = 500
NUMERIC = {"int64", "double", "decimal"}
DATE_WORDS = {"date", "dates", "calendar", "calendars"}
GEO_WORDS = {"country", "countries", "continent", "city", "cities", "address", "latitude", "longitude",
             "postal", "zip", "postcode", "province", "county"}
TEXT_FUNCS = {"FORMAT", "CONCATENATE", "CONCATENATEX", "UNICHAR", "COMBINEVALUES", "LEFT", "RIGHT", "MID",
              "UPPER", "LOWER", "SUBSTITUTE", "REPLACE", "REPT", "TRIM", "FIXED", "SELECTEDMEASURENAME",
              "USERPRINCIPALNAME", "USERNAME", "USEROBJECTID"}
NUMERIC_AGGS = {"SUM", "AVERAGE", "MEDIAN", "PRODUCT", "STDEV.P", "STDEV.S", "VAR.P", "VAR.S"}
RLS_FUNCS = {"RIGHT", "LEFT", "UPPER", "LOWER", "FIND"}
DECORATIVE = {"textbox", "image", "shape", "basicShape", "actionButton", "pageNavigator", "bookmarkNavigator",
              "group", ""}
TABLE_VISUALS = {"tableEx", "pivotTable", "table", "matrix"}
HEX = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")
DEFAULT_PAGE = re.compile(r"(?i)^(?:page|seite|p[aá]gina|pagina)\s*\d+$|^duplicate of ")
_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def words(name: str) -> list[str]:
    """Whole words of a name: 'DimDate' -> dim, date; 'Updates' -> updates; 'FYMonth' -> fy, month."""
    return [w.lower() for w in _WORD.findall(name)]


@dataclass
class Hit:
    obj: str                  # object label (names only)
    kind: str                 # model table column measure calc_item relationship role perspective report page visual ...
    table: str = ""
    excess: int = 0
    node: object = None       # TMDL node, for ignore annotations
    detail: str = ""
    count: int | None = None
    id: str = ""              # stable id of a report object (page / visual / bookmark)
    visual_type: str = ""


CHECKS: dict = {}


def check(*rule_ids: str):
    def deco(fn):
        for rid in rule_ids:
            CHECKS[rid] = fn
        return fn
    return deco


# ---------------------------------------------------------------- context


@dataclass
class RVisual:
    page: str
    id: str
    type: str
    hidden: bool
    root: object
    rel: str
    fields: list = field(default_factory=list)   # [(FieldRef, path)]


@dataclass
class RPage:
    id: str
    name: str
    hidden: bool
    type: str
    height: float
    root: object
    visuals: list = field(default_factory=list)


class Cx:
    def __init__(self, ws, project, g, thresholds: dict, extra_reports=()):
        self.ws, self.project, self.g = ws, project, g
        self.model = m = g.model
        self.res = deps.Resolver(m)
        self.tables = [t for t in m.tables.values() if not t.private]
        self.columns = [c for t in self.tables for c in t.columns.values()]
        self.measures = [x for t in self.tables for x in t.measures.values()]
        private = {t.name.lower() for t in m.tables.values() if t.private}
        self.rels = [r for r in m.relationships
                     if r.from_table.lower() not in private and r.to_table.lower() not in private]
        self.thresholds = thresholds
        self.extra_reports = extra_reports
        self._sig: dict = {}
        self._reports = None

    def sig(self, text: str) -> list:
        if text not in self._sig:
            self._sig[text] = lexers.significant(lexers.lex_dax(text))
        return self._sig[text]

    def threshold(self, rule: dict):
        return self.thresholds.get(rule["id"], rule.get("threshold"))

    def hidden_table(self, name: str) -> bool:
        t = self.model.table(name)
        return bool(t and t.hidden)

    def dax_text(self, node) -> str:
        doc = self.model.docs[node.file]
        return "\n".join(doc.text[r.start:r.end] for r in tmdl.node_regions(doc, node, deep=False) if r.kind == "dax")

    def dax_items(self):
        """(kind, label, table, node, text) for measures, calculated columns and calculation items."""
        for t in self.tables:
            for x in t.measures.values():
                yield "measure", f"[{x.name}]", t.name, x.node, x.expression
            for c in t.columns.values():
                if c.calculated and c.node is not None:
                    yield "column", f"{t.name}[{c.name}]", t.name, c.node, self.dax_text(c.node)
            for ci, node in t.calc_items.items():
                yield "calc_item", f"{t.name}.{ci}", t.name, node, self.dax_text(node)

    def reports(self) -> list[dict]:
        if self._reports is None:
            reps = list(usage.bound_reports(self.ws, self.project)) if self.ws is not None else []
            for rdir in self.extra_reports:
                rdir = os.path.abspath(rdir)
                reps.append((os.path.basename(rdir).rsplit(".", 1)[0], rdir))
            self._reports = [_load_report(name, rdir, self.model) for name, rdir in reps]
        return self._reports


def _s(node) -> str:
    return node.value if isinstance(node, pbir.JStr) else ""


def _true(node) -> bool:
    return isinstance(node, pbir.JLit) and node.raw == "true"


def _load_report(name: str, rdir: str, model) -> dict:
    ddir = os.path.join(rdir, "definition")
    rep = {"name": name, "pages": {}, "visuals": [], "bookmarks": [], "bookmark_text": "", "report": None,
           "fields": 0}
    for path in pbir.report_files(rdir):
        rel = os.path.relpath(path, ddir).replace("\\", "/")
        try:
            text = read_text(path)[0]
            root = pbir.parse_json(text)
        except (OSError, UnicodeDecodeError, pbir.JsonError):
            continue  # usage.build already reports unreadable files (and FIX_INVALID_REFERENCES sees them)
        kind = usage._file_kind(rel)
        parts = rel.split("/")
        refs = pbir.walk_fields_with_path(root, model)
        rep["fields"] += len(refs)
        if kind == "page":
            binding = root.get("pageBinding")
            ptype = _s(binding.get("type")) if isinstance(binding, pbir.JObj) else ""
            h = root.get("height")
            rep["pages"][parts[1]] = RPage(parts[1], _s(root.get("displayName")) or parts[1],
                                          _s(root.get("visibility")) == "HiddenInViewMode",
                                          (ptype or _s(root.get("type"))).lower(),
                                          float(h.raw) if isinstance(h, pbir.JLit) and h.raw not in ("true", "false", "null") else 0.0,
                                          root)
        elif kind == "visual":
            v = root.get("visual")
            vt = _s(v.get("visualType")) if isinstance(v, pbir.JObj) else ("group" if "visualGroup" in root else "")
            rep["visuals"].append(RVisual(parts[1], parts[3], vt, _true(root.get("isHidden")), root, rel, refs))
        elif kind == "bookmark":
            es = root.get("explorationState")
            opts = root.get("options")
            tv = opts.get("targetVisualNames") if isinstance(opts, pbir.JObj) else None
            rep["bookmarks"].append({"id": _s(root.get("name")) or parts[-1], "name": _s(root.get("displayName")),
                                     "active": _s(es.get("activeSection")) if isinstance(es, pbir.JObj) else "",
                                     "targets": [_s(x) for x in (tv.items if isinstance(tv, pbir.JArr) else [])]})
            rep["bookmark_text"] += text
        elif kind == "report":
            rep["report"] = root
    for v in rep["visuals"]:
        if v.page in rep["pages"]:
            rep["pages"][v.page].visuals.append(v)
    return rep


# ---------------------------------------------------------------- DAX helpers


def _calls(sig: list, names: set) -> list[str]:
    return sorted({t.text.upper() for i, t in enumerate(sig[:-1]) if t.kind == "ident" and t.text.upper() in names
                   and sig[i + 1].kind == "op" and sig[i + 1].text == "("})


def _locals(sig: list) -> set[str]:
    return {sig[i + 1].text.lower() for i, t in enumerate(sig[:-1])
            if t.kind == "ident" and t.text.upper() == "VAR" and sig[i + 1].kind == "ident"}


def _qualifier(sig: list, i: int, res, local: set) -> str | None:
    """Table name qualifying the bracket at sig[i], or None for a bare [x]."""
    prev = sig[i - 1] if i else None
    if prev is None:
        return None
    if prev.kind == "table":
        return prev.value
    if prev.kind == "ident" and (prev.text.lower() in local or res.table(prev.text)):
        return prev.text
    return None


def _ref_forms(sig: list, res) -> tuple[list[str], list[str]]:
    """(bare column references, table-qualified measure references) by lexical form."""
    local = _locals(sig)
    strings = {t.value.lower() for t in sig if t.kind == "str"}
    bare, qual = set(), set()
    for i, t in enumerate(sig):
        if t.kind != "bracket":
            continue
        q = _qualifier(sig, i, res, local)
        if q is None:
            if not res.measure(t.value) and t.value.lower() not in strings and res.by_column.get(t.value.lower()):
                bare.add(f"[{t.value}]")
        elif q.lower() not in local:
            tab = res.table(q)
            if tab and not res.column(tab, t.value) and res.measure(t.value):
                qual.add(f"{tab}[{t.value}]")
    return sorted(bare), sorted(qual)


def _divides(sig: list) -> bool:
    for i, t in enumerate(sig):
        if t.kind == "op" and t.text == "/":
            nxt = sig[i + 1] if i + 1 < len(sig) else None
            if nxt is not None and nxt.kind == "num" and _num(nxt.text) not in (None, 0.0):
                continue  # division by a non-zero constant cannot fail
            return True
    return False


def _num(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _one_minus(sig: list) -> bool:
    for i in range(len(sig) - 2):
        if sig[i].kind == "num" and _num(sig[i].text) == 1.0 and sig[i + 1].kind == "op" and sig[i + 1].text in "-+" \
                and sig[i + 2].kind == "op" and sig[i + 2].text == "(":
            depth = 0
            for t in sig[i + 2:]:
                if t.kind == "op" and t.text == "(":
                    depth += 1
                elif t.kind == "op" and t.text == ")":
                    depth -= 1
                    if depth == 0:
                        break
                elif t.kind == "op" and t.text == "/" and depth == 1:
                    return True
    return False


def _adds_zero(sig: list) -> bool:
    for i, t in enumerate(sig):
        if t.kind != "num" or _num(t.text) != 0.0:
            continue
        prev = sig[i - 1] if i else None
        nxt = sig[i + 1] if i + 1 < len(sig) else None
        if prev is not None and prev.kind == "op" and prev.text == "+" and (
                nxt is None or (nxt.kind == "op" and nxt.text in (")", ","))):
            return True
        if (prev is None or (prev.kind == "op" and prev.text in ("(", ","))) and nxt is not None \
                and nxt.kind == "op" and nxt.text == "+":
            return True
    return False


def _whole_table_filters(sig: list, res) -> list[tuple[str, bool]]:
    """[(table, condition uses a measure)] for every FILTER(<whole table>, condition)."""
    out = []
    local = _locals(sig)
    for i in range(len(sig) - 3):
        t = sig[i]
        if not (t.kind == "ident" and t.text.upper() == "FILTER" and sig[i + 1].kind == "op" and sig[i + 1].text == "("):
            continue
        a, b = sig[i + 2], sig[i + 3]
        name = a.value if a.kind == "table" else (a.text if a.kind == "ident" and a.text.lower() not in local else "")
        tab = res.table(name) if name else None
        if not tab or not (b.kind == "op" and b.text == ","):
            continue
        depth, uses_measure = 1, False
        for j in range(i + 4, len(sig)):
            x = sig[j]
            if x.kind == "op" and x.text == "(":
                depth += 1
            elif x.kind == "op" and x.text == ")":
                depth -= 1
                if depth == 0:
                    break
            elif x.kind == "bracket" and _qualifier(sig, j, res, local) is None and res.measure(x.value) \
                    and not res.column(tab, x.value):
                uses_measure = True
        out.append((tab, uses_measure))
    return out


def _text_aggregations(sig: list, res, model) -> list[str]:
    out = []
    for i in range(len(sig) - 4):
        t, p, a, b, c = sig[i:i + 5]
        if t.kind == "ident" and t.text.upper() in NUMERIC_AGGS and p.kind == "op" and p.text == "(" \
                and a.kind in ("table", "ident") and b.kind == "bracket" and c.kind == "op" and c.text == ")":
            tab = res.table(a.value if a.kind == "table" else a.text)
            col = model.column(tab, b.value) if tab else None
            if col is not None and col.data_type.lower() == "string":
                out.append(f"{tab}[{col.name}]")
    return sorted(set(out))


def _is_text_measure(sig: list) -> bool:
    if any(t.kind == "op" and t.text == "&" for t in sig) or _calls(sig, TEXT_FUNCS):
        return True
    return len(sig) == 1 and sig[0].kind in ("str", "pbztok")


def _canon(sig: list, res) -> str:
    """Formula fingerprint: case-insensitive names and functions, literals exact, trivia dropped."""
    parts = []
    for i, t in enumerate(sig):
        nxt = sig[i + 1] if i + 1 < len(sig) else None
        if t.kind == "ident":
            if res.table(t.text) and not (nxt is not None and nxt.kind == "op" and nxt.text == "("):
                parts.append("t:" + res.table(t.text).lower())
            else:
                parts.append(t.text.upper())
        elif t.kind == "table":
            parts.append("t:" + t.value.lower())
        elif t.kind == "bracket":
            parts.append("b:" + t.value.lower())
        elif t.kind == "num":
            v = _num(t.text)
            parts.append("n:" + (repr(v) if v is not None else t.text))
        elif t.kind in ("str", "pbztok"):
            parts.append("s:" + (t.value or t.text))
        else:
            parts.append(t.text)
    return " ".join(parts)


def _strip_parens(sig: list) -> list:
    while len(sig) >= 2 and sig[0].kind == "op" and sig[0].text == "(" and sig[-1].kind == "op" and sig[-1].text == ")":
        depth = 0
        for j, t in enumerate(sig):
            depth += (t.kind == "op" and t.text == "(") - (t.kind == "op" and t.text == ")")
            if depth == 0 and j < len(sig) - 1:
                return sig
        sig = sig[1:-1]
    return sig


def _card(r, side: str) -> str:
    default = "many" if side == "from" else "one"
    return r.node.props.get(f"{side}Cardinality", default).strip().lower() if r.node is not None else default


def _rel_label(r) -> str:
    return f"{r.from_table}[{r.from_column}] -> {r.to_table}[{r.to_column}]"


def _described(model, node) -> bool:
    return node is not None and any(r.prop == "description" and r.node is node for r in model.docs[node.file].regions)


def _field_param(t) -> bool:
    return any(k == "calculated" and "NAMEOF" in e.upper() for _n, k, e in t.partitions)


def _whatif(t) -> bool:
    return any(k == "calculated" and "GENERATESERIES" in e.upper() for _n, k, e in t.partitions)


def _calc_group(t) -> bool:
    return bool(t.calc_items) or bool(t.node and any(c.kind == "calculationGroup" for c in t.node.children))


def _measure_table(t) -> bool:
    return bool(t.measures) and all(c.hidden for c in t.columns.values())


# ---------------------------------------------------------------- model rules


@check("REMOVE_AUTO-DATE_TABLE")
def _auto_date(cx, rule):
    tabs = list(cx.model.tables.values())
    return len(tabs), [Hit(t.name, "table", t.name, node=t.node) for t in tabs if t.private]


@check("DATE/CALENDAR_TABLES_SHOULD_BE_MARKED_AS_A_DATE_TABLE")
def _date_marked(cx, rule):
    hits = []
    for t in cx.tables:
        if DATE_WORDS & set(words(t.name)) and any(c.data_type.lower() == "datetime" for c in t.columns.values()) \
                and (t.node is None or t.node.props.get("dataCategory", "").strip().lower() != "time"):
            hits.append(Hit(t.name, "table", t.name, node=t.node))
    return len(cx.tables), hits


@check("MODEL_SHOULD_HAVE_A_DATE_TABLE")
def _model_date(cx, rule):
    marked = any(t.node is not None and t.node.props.get("dataCategory", "").strip().lower() == "time" for t in cx.tables)
    dated = any(c.data_type.lower() == "datetime" for c in cx.columns)
    return 1, [] if marked or not dated else [Hit("model", "model", node=cx.model.model_node)]


@check("AVOID_FLOATING_POINT_DATA_TYPES")
def _float(cx, rule):
    return len(cx.columns), [Hit(f"{c.table}[{c.name}]", "column", c.table, node=c.node)
                             for c in cx.columns if c.data_type.lower() == "double"]


@check("NUMERIC_COLUMN_SUMMARIZE_BY")
def _summarize(cx, rule):
    hits, n = [], 0
    for t in cx.tables:
        if _calc_group(t):
            continue
        for c in t.columns.values():
            if c.data_type.lower() not in NUMERIC or c.hidden or t.hidden:
                continue
            n += 1
            # TMDL omits summarizeBy when it is "default", which summarizes numeric columns
            if c.node is not None and c.node.props.get("summarizeBy", "default").strip().lower() != "none":
                hits.append(Hit(f"{t.name}[{c.name}]", "column", t.name, node=c.node))
    return n, hits


def _keys(cx, side: str) -> list:
    out = []
    for r in cx.rels:
        if side == "from" and _card(r, "from") != "one":
            out.append((r.from_table, r.from_column))
        elif side == "to" and _card(r, "to") == "one":
            out.append((r.to_table, r.to_column))
    return out


@check("HIDE_FOREIGN_KEYS")
def _fk(cx, rule):
    hits, seen = [], set()
    keys = _keys(cx, "from")
    for t, c in keys:
        col = cx.model.column(t, c)
        if col is None or (t.lower(), c.lower()) in seen:
            continue
        seen.add((t.lower(), c.lower()))
        if not col.hidden and not cx.hidden_table(t):
            hits.append(Hit(f"{col.table}[{col.name}]", "column", col.table, node=col.node))
    return len(seen), hits


@check("MARK_PRIMARY_KEYS")
def _pk(cx, rule):
    hits, seen = [], set()
    for t, c in _keys(cx, "to"):
        col = cx.model.column(t, c)
        if col is None or (t.lower(), c.lower()) in seen:
            continue
        seen.add((t.lower(), c.lower()))
        if col.node is not None and not col.node.flag("isKey"):
            hits.append(Hit(f"{col.table}[{col.name}]", "column", col.table, node=col.node))
    return len(seen), hits


def _types(cx, r) -> tuple[str, str] | None:
    a, b = cx.model.column(r.from_table, r.from_column), cx.model.column(r.to_table, r.to_column)
    if a is None or b is None:
        return None
    return a.data_type.lower(), b.data_type.lower()


@check("RELATIONSHIP_COLUMNS_SHOULD_BE_OF_INTEGER_DATA_TYPE")
def _rel_int(cx, rule):
    hits = []
    for r in cx.rels:
        ty = _types(cx, r)
        if ty and any(x not in ("int64", "datetime") for x in ty):
            hits.append(Hit(_rel_label(r), "relationship", node=r.node,
                            detail="/".join(sorted({x for x in ty if x not in ("int64", "datetime")}))))
    return len(cx.rels), hits


@check("RELATIONSHIP_COLUMNS_SAME_DATA_TYPE")
def _rel_same(cx, rule):
    hits = []
    for r in cx.rels:
        ty = _types(cx, r)
        if ty and ty[0] != ty[1]:
            hits.append(Hit(_rel_label(r), "relationship", node=r.node, detail=f"{ty[0]} -> {ty[1]}"))
    return len(cx.rels), hits


@check("AVOID_EXCESSIVE_BI-DIRECTIONAL_OR_MANY-TO-MANY_RELATIONSHIPS")
def _excessive(cx, rule):
    n = len(cx.rels)
    if not n:
        return 0, []
    bad = sum(1 for r in cx.rels if r.both) + sum(1 for r in cx.rels if _card(r, "to") == "many" and _card(r, "from") != "one")
    limit = float(cx.threshold(rule) or 0.3)
    return 1, [Hit("model", "model", node=cx.model.model_node, detail=f"{bad} of {n}")] if bad / n > limit else []


@check("MANY-TO-MANY_RELATIONSHIPS_SHOULD_BE_SINGLE-DIRECTION")
def _m2m(cx, rule):
    m2m = [r for r in cx.rels if _card(r, "to") == "many" and _card(r, "from") != "one"]
    return len(m2m), [Hit(_rel_label(r), "relationship", node=r.node) for r in m2m if r.both]


@check("INACTIVE_RELATIONSHIPS_THAT_ARE_NEVER_ACTIVATED")
def _inactive(cx, rule):
    inactive = [r for r in cx.rels if not r.active]
    hits = []
    for r in inactive:
        k = usage.key("relationship", "", r.name)
        if not any(e.via in ("userelationship", "crossfilter") for e in cx.g.inc.get(k, [])):
            hits.append(Hit(_rel_label(r), "relationship", node=r.node))
    return len(inactive), hits


@check("ENSURE_TABLES_HAVE_RELATIONSHIPS")
def _isolated(cx, rule):
    related = {x.lower() for r in cx.rels for x in (r.from_table, r.to_table)}
    hits, n = [], 0
    for t in cx.tables:
        if _calc_group(t) or _field_param(t) or _whatif(t) or _measure_table(t):
            continue
        n += 1
        if t.name.lower() not in related:
            hits.append(Hit(t.name, "table", t.name, node=t.node))
    return n, hits


@check("SNOWFLAKE_SCHEMA_ARCHITECTURE")
def _snowflake(cx, rule):
    rels = [r for r in cx.rels if r.from_table.lower() != r.to_table.lower()]
    frm = {r.from_table.lower() for r in rels}
    to = {r.to_table.lower() for r in rels}
    return len(cx.tables), [Hit(t.name, "table", t.name, node=t.node) for t in cx.tables
                            if t.name.lower() in frm and t.name.lower() in to]


@check("UNNECESSARY_COLUMNS")
def _unneeded_cols(cx, rule):
    hits, n = [], 0
    for c in cx.columns:
        if not (c.hidden or cx.hidden_table(c.table)):
            continue
        n += 1
        if cx.g.state.get(usage.key("column", c.table, c.name)) in ("unused", "used_by_unused"):
            hits.append(Hit(f"{c.table}[{c.name}]", "column", c.table, node=c.node))
    return n, hits


@check("UNNECESSARY_MEASURES")
def _unneeded_measures(cx, rule):
    hits, n = [], 0
    for x in cx.measures:
        if not ((x.node is not None and x.node.flag("isHidden")) or cx.hidden_table(x.table)):
            continue
        n += 1
        if cx.g.state.get(usage.key("measure", x.table, x.name)) in ("unused", "used_by_unused"):
            hits.append(Hit(f"[{x.name}]", "measure", x.table, node=x.node))
    return n, hits


@check("REDUCE_USAGE_OF_CALCULATED_COLUMNS_THAT_USE_THE_RELATED_FUNCTION")
def _related(cx, rule):
    calc = [c for c in cx.columns if c.calculated and c.node is not None]
    return len(calc), [Hit(f"{c.table}[{c.name}]", "column", c.table, node=c.node) for c in calc
                       if _calls(cx.sig(cx.dax_text(c.node)), {"RELATED"})]


@check("REDUCE_USAGE_OF_CALCULATED_TABLES")
def _calc_tables(cx, rule):
    calc = [t for t in cx.tables if t.calculated and not (_field_param(t) or _whatif(t) or _calc_group(t))]
    return len(cx.tables), [Hit(t.name, "table", t.name, node=t.node) for t in calc]


def _dax_rule(pred):
    """Check every measure, calculated column and calculation item with pred(sig, cx) -> detail | ''."""
    def run(cx, rule):
        hits, n = [], 0
        for kind, label, table, node, text in cx.dax_items():
            if not text.strip():
                continue
            n += 1
            detail = pred(cx.sig(text), cx)
            if detail:
                hits.append(Hit(label, kind, table, node=node, detail=detail if isinstance(detail, str) else ""))
        return n, hits
    return run


CHECKS["USE_THE_DIVIDE_FUNCTION_FOR_DIVISION"] = _dax_rule(lambda s, cx: _divides(s))
CHECKS["AVOID_USING_THE_IFERROR_FUNCTION"] = _dax_rule(lambda s, cx: bool(_calls(s, {"IFERROR"})))
CHECKS["USE_THE_TREATAS_FUNCTION_INSTEAD_OF_INTERSECT"] = _dax_rule(lambda s, cx: bool(_calls(s, {"INTERSECT"})))
CHECKS["EVALUATEANDLOG_SHOULD_NOT_BE_USED_IN_PRODUCTION_MODELS"] = _dax_rule(
    lambda s, cx: bool(_calls(s, {"EVALUATEANDLOG"})))
CHECKS["DAX_COLUMNS_FULLY_QUALIFIED"] = _dax_rule(lambda s, cx: ", ".join(_ref_forms(s, cx.res)[0][:5]))
CHECKS["DAX_MEASURES_UNQUALIFIED"] = _dax_rule(lambda s, cx: ", ".join(_ref_forms(s, cx.res)[1][:5]))
CHECKS["FILTER_COLUMN_VALUES"] = _dax_rule(
    lambda s, cx: ", ".join(sorted({t for t, m in _whole_table_filters(s, cx.res) if not m})))
CHECKS["FILTER_MEASURE_VALUES_BY_COLUMNS"] = _dax_rule(
    lambda s, cx: ", ".join(sorted({t for t, m in _whole_table_filters(s, cx.res) if m})))
CHECKS["AVOID_USING_'1-(X/Y)'_SYNTAX"] = _dax_rule(lambda s, cx: _one_minus(s))
CHECKS["AVOID_AGGREGATING_TEXT_COLUMNS"] = _dax_rule(lambda s, cx: ", ".join(_text_aggregations(s, cx.res, cx.model)))


@check("AVOID_ADDING_0_TO_A_MEASURE")
def _plus_zero(cx, rule):
    ms = [x for x in cx.measures if x.expression.strip()]
    return len(ms), [Hit(f"[{x.name}]", "measure", x.table, node=x.node) for x in ms if _adds_zero(cx.sig(x.expression))]


@check("AVOID_DUPLICATE_MEASURES")
def _duplicates(cx, rule):
    groups: dict[str, list] = {}
    ms = [x for x in cx.measures if x.expression.strip()]
    for x in ms:
        groups.setdefault(_canon(cx.sig(x.expression), cx.res), []).append(x)
    hits = []
    for same in groups.values():
        if len(same) < 2:
            continue
        for x in same:
            others = sorted(f"[{o.name}]" for o in same if o is not x)
            hits.append(Hit(f"[{x.name}]", "measure", x.table, node=x.node, detail=", ".join(others[:5])))
    return len(ms), hits


@check("MEASURES_SHOULD_NOT_BE_DIRECT_REFERENCES_OF_OTHER_MEASURES")
def _alias(cx, rule):
    hits = []
    for x in cx.measures:
        sig = _strip_parens(cx.sig(x.expression))
        if len(sig) == 1 and sig[0].kind == "bracket" and cx.res.measure(sig[0].value) \
                and sig[0].value.lower() != x.name.lower():
            hits.append(Hit(f"[{x.name}]", "measure", x.table, node=x.node, detail=f"[{cx.res.measure(sig[0].value)[1]}]"))
    return len(cx.measures), hits


@check("LIMIT_ROW_LEVEL_SECURITY_(RLS)_LOGIC")
def _rls_logic(cx, rule):
    hits, n = [], 0
    for role, node in cx.model.role_nodes.items():
        for tp in (c for c in node.children if c.kind == "tablePermission"):
            n += 1
            funcs = _calls(cx.sig(cx.dax_text(tp)), RLS_FUNCS)
            if funcs:
                hits.append(Hit(f"{role} on {tp.name}", "role", tp.name, node=node, detail=", ".join(funcs)))
    return n, hits


@check("AVOID_THE_USERELATIONSHIP_FUNCTION_AND_RLS_AGAINST_THE_SAME_TABLE")
def _userel_rls(cx, rule):
    secured = {tp.name.lower() for node in cx.model.role_nodes.values() for tp in node.children if tp.kind == "tablePermission"}
    hits, n = {}, 0
    for r in cx.model.relationships:
        users = [e.src for e in cx.g.inc.get(usage.key("relationship", "", r.name), []) if e.via == "userelationship"]
        n += len(users)
        tabs = sorted({x for x in (r.from_table, r.to_table) if x.lower() in secured})
        for src in users if tabs else []:
            o = cx.g.objects[src]
            t = cx.model.table(o.table)
            node = t.measures[o.name].node if o.kind == "measure" and t and o.name in t.measures else None
            hits.setdefault(src, Hit(o.label(), o.kind, o.table, node=node, detail=", ".join(tabs)))
    return n, list(hits.values())


@check("ISAVAILABLEINMDX_FALSE_NONATTRIBUTE_COLUMNS")
def _mdx(cx, rule):
    hits, n = [], 0
    for c in cx.columns:
        if c.node is None or not (c.hidden or cx.hidden_table(c.table)):
            continue
        n += 1
        k = usage.key("column", c.table, c.name)
        attribute = any(e.via in ("sortBy", "level") for e in cx.g.inc.get(k, [])) or bool(c.sort_by) or \
            any(ch.kind == "variation" for ch in c.node.children)
        # TMDL omits isAvailableInMdx when it is true (the default)
        if not attribute and c.node.props.get("isAvailableInMdx", "true").strip().lower() != "false":
            hits.append(Hit(f"{c.table}[{c.name}]", "column", c.table, node=c.node))
    return n, hits


@check("MONTH_(AS_A_STRING)_MUST_BE_SORTED")
def _month(cx, rule):
    months = [c for c in cx.columns if c.data_type.lower() == "string" and "month" in words(c.name)]
    return len(months), [Hit(f"{c.table}[{c.name}]", "column", c.table, node=c.node) for c in months if not c.sort_by]


@check("PROVIDE_FORMAT_STRING_FOR_MEASURES")
def _format(cx, rule):
    hits, n = [], 0
    for x in cx.measures:
        if x.node is None or x.node.flag("isHidden") or cx.hidden_table(x.table):
            continue
        n += 1
        dynamic = any(ch.kind == "formatStringDefinition" for ch in x.node.children)
        if not x.format_string.strip() and not dynamic and not _is_text_measure(cx.sig(x.expression)):
            hits.append(Hit(f"[{x.name}]", "measure", x.table, node=x.node))
    return n, hits


@check("OBJECTS_WITH_NO_DESCRIPTION")
def _descriptions(cx, rule):
    items = [(f"[{x.name}]", "measure", x.table, x.node) for x in cx.measures
             if x.node is not None and not x.node.flag("isHidden") and not cx.hidden_table(x.table)]
    items += [(t.name, "table", t.name, t.node) for t in cx.tables if not t.hidden and t.node is not None]
    return len(items), [Hit(lab, kind, tab, node=node) for lab, kind, tab, node in items if not _described(cx.model, node)]


def _named(cx):
    """(label, kind, table, node, name) of every named model object."""
    for t in cx.tables:
        yield t.name, "table", t.name, t.node, t.name
        for c in t.columns.values():
            yield f"{t.name}[{c.name}]", "column", t.name, c.node, c.name
        for x in t.measures.values():
            yield f"[{x.name}]", "measure", t.name, x.node, x.name
        for h in (n for n in (t.node.children if t.node else []) if n.kind == "hierarchy"):
            yield f"{t.name}.{h.name}", "hierarchy", t.name, h, h.name
    for role, node in cx.model.role_nodes.items():
        yield role, "role", "", node, role
    for p, node in cx.model.perspectives.items():
        yield p, "perspective", "", node, p


@check("OBJECTS_SHOULD_NOT_START_OR_END_WITH_A_SPACE")
def _spaces(cx, rule):
    items = list(_named(cx))
    return len(items), [Hit(lab, kind, tab, node=node) for lab, kind, tab, node, name in items if name != name.strip()]


@check("AVOID_INVALID_NAME_CHARACTERS")
def _control(cx, rule):
    items = list(_named(cx))
    return len(items), [Hit(lab.encode("unicode_escape").decode("ascii"), kind, tab, node=node)
                        for lab, kind, tab, node, name in items if re.search(r"[\x00-\x1f\x7f]", name)]


@check("FIRST_LETTER_OF_OBJECTS_MUST_BE_CAPITALIZED")
def _capital(cx, rule):
    items = [x for x in _named(cx) if x[1] in ("table", "column", "measure")]
    return len(items), [Hit(lab, kind, tab, node=node) for lab, kind, tab, node, name in items
                        if name[:1].isalpha() and name[:1].islower()]


@check("EXPRESSIONS_WITH_UNRESOLVED_REFERENCES")
def _unresolved(cx, rule):
    hits = []
    objs = [o for o in cx.g.objects.values() if o.kind in ("measure", "column", "calc_item", "function", "role", "table")
            and not o.private]
    for o in objs:
        if o.issues:
            t = cx.model.table(o.table or o.name)
            node = None
            if o.kind == "measure" and t:
                node = t.measures[o.name].node if o.name in t.measures else None
            elif o.kind == "column" and t:
                node = t.columns[o.name].node if o.name in t.columns else None
            elif o.kind == "table" and t:
                node = t.node
            hits.append(Hit(o.label(), o.kind, o.table, node=node, detail=", ".join(o.issues[:5])))
    return len(objs), hits


@check("DATA_COLUMNS_MUST_HAVE_A_SOURCE_COLUMN")
def _source(cx, rule):
    hits, n = [], 0
    for t in cx.tables:
        if t.calculated or _calc_group(t) or not t.partition_nodes:
            continue
        for c in t.columns.values():
            if c.calculated:
                continue
            n += 1
            if not c.source_column:
                hits.append(Hit(f"{t.name}[{c.name}]", "column", t.name, node=c.node))
    return n, hits


@check("CALCULATION_GROUPS_WITH_NO_CALCULATION_ITEMS")
def _empty_cg(cx, rule):
    groups = [t for t in cx.tables if _calc_group(t)]
    return len(groups), [Hit(t.name, "table", t.name, node=t.node) for t in groups if not t.calc_items]


@check("PERSPECTIVES_WITH_NO_OBJECTS")
def _empty_perspective(cx, rule):
    ps = cx.model.perspectives
    return len(ps), [Hit(p, "perspective", node=node) for p, node in ps.items()
                     if not any(c.kind == "perspectiveTable" for c in node.children)]


@check("ADD_DATA_CATEGORY_FOR_COLUMNS")
def _geo(cx, rule):
    text = [c for c in cx.columns if c.data_type.lower() == "string"]
    hits = []
    for c in text:
        w = words(c.name)
        if (GEO_WORDS & set(w) or w == ["state"]) and c.node is not None and not c.node.props.get("dataCategory", "").strip():
            hits.append(Hit(f"{c.table}[{c.name}]", "column", c.table, node=c.node))
    return len(text), hits


@check("INTEGER_FORMATTING")
def _int_format(cx, rule):
    items = [(f"[{x.name}]", "measure", x.table, x.node, x.format_string) for x in cx.measures
             if x.node is not None and not x.node.flag("isHidden")]
    items += [(f"{c.table}[{c.name}]", "column", c.table, c.node, c.node.props.get("formatString", "").strip())
              for c in cx.columns if c.node is not None and not c.hidden and c.data_type.lower() == "int64"]
    return len(items), [Hit(lab, kind, tab, node=node) for lab, kind, tab, node, fmt in items if fmt.strip() == "0"]


@check("PERCENTAGE_FORMATTING")
def _pct_format(cx, rule):
    items = [x for x in cx.measures if "%" in x.format_string]
    return len(items), [Hit(f"[{x.name}]", "measure", x.table, node=x.node) for x in items
                        if not x.format_string.strip().startswith("#,0.0")]


@check("DATECOLUMN_FORMATSTRING")
def _date_format(cx, rule):
    dates = [c for c in cx.columns if c.data_type.lower() == "datetime" and not c.hidden and c.node is not None]
    return len(dates), [Hit(f"{c.table}[{c.name}]", "column", c.table, node=c.node) for c in dates
                        if c.node.props.get("formatString", "").strip().lower() in ("", "general date")]


# ---------------------------------------------------------------- report rules


def _pages(cx):
    for rep in cx.reports():
        for p in rep["pages"].values():
            yield rep, p


def _visuals(cx):
    for rep in cx.reports():
        for v in rep["visuals"]:
            yield rep, v


def _page_label(rep, page_id: str) -> str:
    p = rep["pages"].get(page_id)
    return f"{rep['name']} | {p.name if p else page_id}"


def _vhit(rep, v, **kw) -> Hit:
    return Hit(f"{_page_label(rep, v.page)} | {v.id}", "visual", id=v.id, visual_type=v.type or "visual", **kw)


def _filters(root) -> list:
    fc = root.get("filterConfig") if isinstance(root, pbir.JObj) else None
    fl = fc.get("filters") if isinstance(fc, pbir.JObj) else None
    return [x for x in (fl.items if isinstance(fl, pbir.JArr) else []) if isinstance(x, pbir.JObj)]


def _query_fields(v) -> list:
    return [f for f, path in v.fields if "queryState" in path]


def _has_true(node, key: str) -> bool:
    if isinstance(node, pbir.JObj):
        return any((k.value == key and _true(v)) or _has_true(v, key) for k, v in node.pairs)
    if isinstance(node, pbir.JArr):
        return any(_has_true(v, key) for v in node.items)
    return False


def _has_key(node, key: str) -> bool:
    if isinstance(node, pbir.JObj):
        return any(k.value == key or _has_key(v, key) for k, v in node.pairs)
    if isinstance(node, pbir.JArr):
        return any(_has_key(v, key) for v in node.items)
    return False


def _hex_colors(node, under: bool = False) -> int:
    if isinstance(node, pbir.JObj):
        lit = node.get("Literal")
        if under and isinstance(lit, pbir.JObj) and isinstance(lit.get("Value"), pbir.JStr):
            val, vtype = pbir.decode_literal(lit.get("Value").value)
            return 1 if vtype == "string" and HEX.match(str(val)) else 0
        return sum(_hex_colors(v, under or k.value in ("objects", "visualContainerObjects")) for k, v in node.pairs)
    if isinstance(node, pbir.JArr):
        return sum(_hex_colors(v, under) for v in node.items)
    return 0


def _threshold_hits(cx, rule, items) -> tuple[int, list]:
    """items: [(Hit, count)] -> hits over the threshold with excess = count - threshold."""
    thr = int(cx.threshold(rule))
    hits = []
    for h, count in items:
        if count > thr:
            h.count, h.excess = count, count - thr
            hits.append(h)
    return len(items), hits


@check("REDUCE_PAGES")
def _many_pages(cx, rule):
    return _threshold_hits(cx, rule, [(Hit(r["name"], "report", id=r["name"]), len(r["pages"])) for r in cx.reports()])


@check("REDUCE_BOOKMARKS")
def _many_bookmarks(cx, rule):
    return _threshold_hits(cx, rule, [(Hit(r["name"], "report", id=r["name"]), len(r["bookmarks"])) for r in cx.reports()])


@check("REDUCE_VISUALS_ON_PAGE")
def _busy_page(cx, rule):
    return _threshold_hits(cx, rule, [(Hit(_page_label(rep, p.id), "page", id=p.id),
                                       sum(1 for v in p.visuals if not v.hidden and v.type != "group"))
                                      for rep, p in _pages(cx)])


@check("REDUCE_OBJECTS_WITHIN_VISUALS")
def _busy_visual(cx, rule):
    return _threshold_hits(cx, rule, [(_vhit(rep, v), len(_query_fields(v))) for rep, v in _visuals(cx)
                                      if v.type not in TABLE_VISUALS and v.type not in DECORATIVE])


def _page_filters(cx, rep, p) -> list:
    return _filters(p.root) + [f for v in p.visuals for f in _filters(v.root)]


@check("REDUCE_TOPN_FILTERS")
def _topn(cx, rule):
    return _threshold_hits(cx, rule, [(Hit(_page_label(rep, p.id), "page", id=p.id),
                                       sum(1 for f in _page_filters(cx, rep, p) if _s(f.get("type")) == "TopN"))
                                      for rep, p in _pages(cx)])


@check("REDUCE_ADVANCED_FILTERS")
def _advanced(cx, rule):
    return _threshold_hits(cx, rule, [(Hit(_page_label(rep, p.id), "page", id=p.id),
                                       sum(1 for f in _page_filters(cx, rep, p) if _s(f.get("type")) == "Advanced"))
                                      for rep, p in _pages(cx)])


@check("REDUCE_MEASURE_FILTERS")
def _measure_filters(cx, rule):
    def is_measure(f):
        ref = pbir.field_of(f.get("field"), {}, cx.model)
        return ref is not None and ref.kind == "measure"
    return _threshold_hits(cx, rule, [(Hit(_page_label(rep, p.id), "page", id=p.id),
                                       sum(1 for f in _page_filters(cx, rep, p) if is_measure(f)))
                                      for rep, p in _pages(cx)])


@check("AVOID_SHOW_ITEMS_WITH_NO_DATA")
def _show_all(cx, rule):
    vs = list(_visuals(cx))
    return len(vs), [_vhit(rep, v) for rep, v in vs if _has_true(v.root, "showAll")]


@check("HIDE_TOOLTIP_DRILLTHROUGH_PAGES")
def _visible_special(cx, rule):
    special = [(rep, p) for rep, p in _pages(cx) if p.type in ("tooltip", "drillthrough")]
    return len(special), [Hit(_page_label(rep, p.id), "page", id=p.id, detail=p.type) for rep, p in special if not p.hidden]


@check("AVOID_TALL_PAGES")
def _tall(cx, rule):
    thr = float(cx.threshold(rule))
    pages = list(_pages(cx))
    hits = []
    for rep, p in pages:
        if p.height > thr:
            hits.append(Hit(_page_label(rep, p.id), "page", id=p.id, count=int(p.height),
                            excess=int((p.height - thr) // thr)))
    return len(pages), hits


@check("REMOVE_UNUSED_CUSTOM_VISUALS")
def _custom(cx, rule):
    hits, n = [], 0
    for rep in cx.reports():
        root = rep["report"]
        pcv = root.get("publicCustomVisuals") if isinstance(root, pbir.JObj) else None
        declared = [_s(x) for x in (pcv.items if isinstance(pcv, pbir.JArr) else []) if _s(x)]
        used = {v.type for v in rep["visuals"]}
        n += len(declared)
        hits += [Hit(f"{rep['name']} | {cv}", "custom_visual", id=cv) for cv in declared if cv not in used]
    return n, hits


@check("AVOID_HARD_CODED_COLORS")
def _colors(cx, rule):
    vs = list(_visuals(cx))
    hits = []
    for rep, v in vs:
        n = _hex_colors(v.root)
        if n:
            hits.append(_vhit(rep, v, count=n))
    return len(vs), hits


@check("AVOID_IMPLICIT_MEASURES")
def _implicit(cx, rule):
    vs = [(rep, v) for rep, v in _visuals(cx) if _query_fields(v)]
    hits = []
    for rep, v in vs:
        n = sum(1 for f in _query_fields(v) if f.kind == "aggregation")
        if n:
            hits.append(_vhit(rep, v, count=n))
    return len(vs), hits


@check("FIX_INVALID_REFERENCES")
def _invalid_refs(cx, rule):
    names = {r["name"] for r in cx.reports()}
    bad = [w for w in cx.g.warnings if w.get("code") == "UNKNOWN_FIELD" and w.get("report") in names]
    hits = [Hit(f"{w['report']} | {w['where']} | {w.get('field', '')}", "field", id=w["where"], detail=w.get("field", ""))
            for w in bad]
    return sum(r["fields"] for r in cx.reports()) or len(hits), hits


@check("AVOID_REPORT_LEVEL_MEASURES")
def _report_measures(cx, rule):
    rms = [o for o in cx.g.objects.values() if o.kind == "report_measure"]
    return len(rms), [Hit(f"{o.extra.get('report', '')} | [{o.name}]", "report_measure", o.table, id=o.name) for o in rms]


@check("REMOVE_INVALID_BOOKMARKS")
def _bad_bookmarks(cx, rule):
    hits, n = [], 0
    for rep in cx.reports():
        vids = {v.id for v in rep["visuals"]}
        for b in rep["bookmarks"]:
            n += 1
            label = f"{rep['name']} | {b['name'] or b['id']}"
            if b["active"] and b["active"] not in rep["pages"]:
                hits.append(Hit(label, "bookmark", id=b["id"], detail="page"))
            elif any(t not in vids for t in b["targets"]):
                hits.append(Hit(label, "bookmark", id=b["id"], detail="visual"))
    return n, hits


@check("REMOVE_HIDDEN_VISUALS_NEVER_SHOWN")
def _hidden_visuals(cx, rule):
    hidden = [(rep, v) for rep, v in _visuals(cx) if v.hidden]
    return len(hidden), [_vhit(rep, v) for rep, v in hidden if f'"{v.id}"' not in rep["bookmark_text"]]


@check("REMOVE_UNREFERENCED_HIDDEN_PAGES")
def _hidden_pages(cx, rule):
    names = {r["name"] for r in cx.reports()}
    pages = [o for o in cx.g.report_objects if o["kind"] == "page" and o["report"] in names and o["hidden"]
             and o["type"] not in ("tooltip", "drillthrough")]
    return len(pages), [Hit(f"{o['report']} | {o['name']}", "page", id=o["id"]) for o in pages if not o.get("referenced")]


@check("AVOID_PIE_CHARTS")
def _pie(cx, rule):
    vs = list(_visuals(cx))
    return len(vs), [_vhit(rep, v) for rep, v in vs if v.type in ("pieChart", "donutChart")]


@check("GIVE_VISIBLE_PAGES_MEANINGFUL_NAMES")
def _page_names(cx, rule):
    visible = [(rep, p) for rep, p in _pages(cx) if not p.hidden]
    return len(visible), [Hit(_page_label(rep, p.id), "page", id=p.id) for rep, p in visible if DEFAULT_PAGE.match(p.name)]


@check("ENSURE_ALTTEXT")
def _alt_text(cx, rule):
    vs = [(rep, v) for rep, v in _visuals(cx) if v.type not in DECORATIVE and not v.hidden]
    return len(vs), [_vhit(rep, v) for rep, v in vs if not _has_key(v.root, "altText")]


# ---------------------------------------------------------------- run


class _Safe(dict):
    def __missing__(self, key):
        return ""


def grade(score: float, scope: str, labels: str = "letters") -> str:
    names = LABELS[labels]
    for i, cut in enumerate(CUTOFFS[scope]):
        if score < cut:
            return names[i]
    return names[-1]


def catalog(scope: str = "all") -> list[dict]:
    rules = []
    for name in ("model", "report"):
        if scope in (name, "all"):
            with open(os.path.join(RULES_DIR, f"{name}.json"), encoding="utf-8") as f:
                rules += [dict(r, scope=name) for r in json.load(f)["rules"]]
    return rules


def _overlay(rules_path: str | None) -> dict:
    if not rules_path:
        return {}
    try:
        if rules_path.lstrip().startswith("{"):
            data = json.loads(rules_path)
        else:
            with open(rules_path, encoding="utf-8-sig") as f:
                data = json.load(f)
    except (OSError, ValueError):
        raise PbzError("BAD_REQUEST", "--rules must be a JSON file (or inline JSON)")
    if not isinstance(data, dict):
        raise PbzError("BAD_REQUEST", "--rules must hold a JSON object")
    lists = {k: [x for x in data.get(k, []) if isinstance(x, str)] if isinstance(data.get(k), list) else []
             for k in ("enable", "disable")}
    maps = {k: {i: v for i, v in data.get(k, {}).items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
            if isinstance(data.get(k), dict) else {} for k in ("severity", "thresholds")}
    maps["severity"] = {i: min(3, max(1, int(v))) for i, v in maps["severity"].items()}
    return {**lists, **maps}


def _rule_ids(model, ann) -> set[str]:
    doc = model.docs.get(ann.file)
    text = "".join(doc.text[r.start:r.end] for r in tmdl.node_regions(doc, ann, deep=False)) if doc else ""
    try:
        data = json.loads(text)
        ids = data.get("RuleIDs", []) if isinstance(data, dict) else data
    except ValueError:
        ids = re.findall(r'"([^"]+)"', text)
    return {i for i in ids if isinstance(i, str)} if isinstance(ids, list) else set()


def ignored_on(model, node) -> set[str]:
    """Rule ids in a Tabular Editor ignore annotation on one object."""
    out = set()
    for ch in (node.children if node is not None else []):
        if ch.kind == "annotation" and ch.name == IGNORE:
            out |= _rule_ids(model, ch)
    return out


def ignored_model(model) -> set[str]:
    """Rule ids ignored for the whole model (annotation on the model object)."""
    out = set()
    mfile = model.model_node.file if model.model_node is not None else "model.tmdl"
    for rel, doc in model.docs.items():
        for n in doc.nodes:
            if n.kind == "annotation" and n.name == IGNORE and (
                    (n.parent is None and rel == mfile) or (n.parent is not None and n.parent.kind == "model")):
                out |= _rule_ids(model, n)
    return out


def run(ws, project, scope: str = "all", rules_path: str | None = None, opinion: bool = False,
        labels: str = "letters", extra_reports=()) -> dict:
    if scope not in ("model", "report", "all"):
        raise PbzError("BAD_REQUEST", "scope must be model, report or all")
    if labels not in LABELS:
        raise PbzError("BAD_REQUEST", "labels must be letters or measure-killer")
    overlay = _overlay(rules_path)
    sev_over = overlay.get("severity", {})
    enable, disable = set(overlay.get("enable", [])), set(overlay.get("disable", []))
    g = usage.build(ws, project, extra_reports)
    cx = Cx(ws, project, g, overlay.get("thresholds", {}), extra_reports)
    model_ignored = ignored_model(cx.model)
    findings, suppressed, effective = [], [], []
    stats = {s: {"raw": 0.0, "bad": 0.0, "possible": 0.0, "findings": 0, "by_rule": {}} for s in ("model", "report")}
    coverage = {"evaluated": 0, "applicable": 0, "skipped_data_rules": [], "disabled": []}
    for rule in catalog(scope):
        rid, sc = rule["id"], rule["scope"]
        sev = int(sev_over.get(rid, rule["severity"]))
        on = (rule.get("enabled", True) and (opinion or not rule.get("opinion")) or rid in enable) and rid not in disable
        thr = cx.threshold(rule)
        effective.append([rid, sev, bool(on), thr])
        if rule.get("evidence") == "data":
            coverage["skipped_data_rules"].append(rid)
            continue
        if not on or rid not in CHECKS:
            coverage["disabled"].append(rid)
            continue
        examined, hits = CHECKS[rid](cx, rule)
        coverage["evaluated"] += 1
        coverage["applicable"] += bool(examined)
        seen, kept = set(), []
        for h in hits:
            if h.obj in seen:
                continue
            seen.add(h.obj)
            by = "model annotation" if rid in model_ignored else (
                "object annotation" if h.node is not None and rid in ignored_on(cx.model, h.node) else "")
            if by:
                suppressed.append({"rule": rid, "object": h.obj, "kind": h.kind, "by": by})
                continue
            kept.append(h)
            rec = {"rule": rid, "severity": sev, "category": rule.get("category", ""), "scope": sc, "kind": h.kind,
                   "object": h.obj, "message": rule["message"].format_map(_Safe(
                       object=h.obj, table=h.table, kind=h.kind, detail=h.detail,
                       count="" if h.count is None else h.count, threshold="" if thr is None else thr,
                       visual_type=h.visual_type or "visual")),
                   "fix": rule.get("fix", "")}
            if h.table and h.kind in ("column", "measure", "calc_item"):
                rec["table"] = h.table
            if h.id:
                rec["id"] = h.id
            if h.excess:
                rec["excess"] = h.excess
            findings.append(rec)
        st = stats[sc]
        st["raw"] += sev * sum(1 + (h.excess if sc == "report" else 0) for h in kept)
        st["bad"] += sev * min(len(kept), examined)
        st["possible"] += sev * examined
        st["findings"] += len(kept)
        if kept:
            st["by_rule"][rid] = len(kept)
    findings.sort(key=lambda f: (-f["severity"], f["scope"], f["rule"], f["object"].lower()))
    out = {"verb": "bpa", "project": project.name, "scope": scope, "labels": labels,
           "ruleset": hashlib.sha256(json.dumps(effective, sort_keys=True).encode()).hexdigest()[:12]}
    if scope in ("model", "all"):
        n = len(cx.tables) + len(cx.columns) + len(cx.measures) + len(cx.rels)
        out["model"] = _summary(stats["model"], n, stats["model"]["raw"] * (math.log10(n) if n > 1 else 0.0),
                                "model", labels)
    if scope in ("report", "all"):
        reps = cx.reports()
        n = sum(len(r["pages"]) + len(r["visuals"]) for r in reps)
        out["report"] = dict(_summary(stats["report"], n, stats["report"]["raw"], "report", labels),
                             reports=[r["name"] for r in reps])
        if not reps:
            out["report"]["note"] = "no report bound to this model by path; pass --report <folder>"
    out["coverage"] = coverage
    out["findings"] = findings[:MAX_FINDINGS]
    if len(findings) > MAX_FINDINGS:
        out["truncated"] = len(findings) - MAX_FINDINGS
    if suppressed:
        out["suppressed"] = suppressed[:200]
    return out


def _summary(st: dict, objects: int, score: float, scope: str, labels: str) -> dict:
    rate = 100.0 if not st["possible"] else max(0.0, 100.0 * (1 - st["bad"] / st["possible"]))
    return {"score": round(score, 1), "grade": grade(score, scope, labels),
            "per100": round(score / max(objects, 1) * 100, 1), "rate": round(rate, 1), "objects": objects,
            "findings": st["findings"], "by_rule": dict(sorted(st["by_rule"].items()))}
