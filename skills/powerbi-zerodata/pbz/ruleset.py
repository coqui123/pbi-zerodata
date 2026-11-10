"""A safe subset of Best Practice Analyzer expressions.

Microsoft rule files are data. This evaluator runs only the boolean, string and collection
operations listed below, and only against mirror metadata. Identifiers that would read
VertiPaq statistics are refused before anything is evaluated. Fix expressions are single
assignments of a property `pbz set` already knows; anything else is reported and not applied.
"""
from __future__ import annotations

import json
import os
import re

from . import props
from .common import PbzError

FORBIDDEN = {
    "vertipaq", "cardinality", "rowcount", "distinctcount", "datasize", "usedsize",
    "segmentcount", "statistics", "min", "max",
}
METHODS = {
    "startswith", "endswith", "contains", "isnullorwhitespace", "isnullorempty",
    "any", "count", "tolower", "toupper", "trim", "ismatch",
}
_SET_PROPS = {name.lower(): name for name in props.PROPS}
_TOKEN = re.compile(
    r"\s+|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|<=|>=|<>|!=|==|&&|\|\||[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|[().=,<>!]")
_HEAVY = re.compile(r"(\.\*){2,}|\(\.[*+]\)[*+]|(\([^)]*[+*][^)]*\))[*+]")


class Refused(Exception):
    def __init__(self, reason: str):
        self.reason = reason


def _lex(text: str) -> list[tuple[str, str]]:
    out, i = [], 0
    while i < len(text):
        m = _TOKEN.match(text, i)
        if not m:
            raise Refused("BAD_TOKEN")
        raw = m.group(0)
        i = m.end()
        if raw.strip() == "":
            continue
        if raw[0] in "\"'":
            out.append(("str", bytes(raw[1:-1], "utf-8").decode("unicode_escape")))
        elif raw[0].isdigit():
            out.append(("num", raw))
        elif raw[0].isalpha() or raw[0] == "_":
            out.append(("id", raw))
        else:
            out.append(("op", raw))
    return out


class _P:
    def __init__(self, toks):
        self.toks = toks
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, kind=None, value=None):
        tok = self.peek()
        if tok is None:
            raise Refused("TRUNCATED")
        if kind and tok[0] != kind:
            raise Refused("SYNTAX")
        if value and tok[1].lower() != value:
            raise Refused("SYNTAX")
        self.i += 1
        return tok

    def parse(self):
        expr = self._or()
        if self.peek() is not None:
            raise Refused("TRAILING")
        return expr

    def _or(self):
        left = self._and()
        while self.peek() in (("id", "or"), ("op", "||")) or (
                self.peek() and self.peek()[0] == "id" and self.peek()[1].lower() == "or"):
            self.take()
            left = ("or", left, self._and())
        return left

    def _and(self):
        left = self._not()
        while self.peek() == ("op", "&&") or (
                self.peek() and self.peek()[0] == "id" and self.peek()[1].lower() == "and"):
            self.take()
            left = ("and", left, self._not())
        return left

    def _not(self):
        if self.peek() == ("op", "!") or (
                self.peek() and self.peek()[0] == "id" and self.peek()[1].lower() == "not"):
            self.take()
            return ("not", self._not())
        return self._cmp()

    def _cmp(self):
        left = self._sum()
        tok = self.peek()
        if tok and tok[0] == "op" and tok[1] in ("=", "==", "<>", "!=", "<", ">", "<=", ">="):
            self.take()
            return ("cmp", tok[1], left, self._sum())
        return left

    def _sum(self):
        return self._unary()

    def _unary(self):
        node = self._primary()
        while self.peek() == ("op", "."):
            self.take()
            name = self.take("id")[1]
            if self.peek() == ("op", "("):
                node = ("call", node, name, self._args())
            else:
                node = ("member", node, name)
        return node

    def _primary(self):
        tok = self.peek()
        if tok is None:
            raise Refused("TRUNCATED")
        if tok == ("op", "("):
            self.take()
            inner = self._or()
            self.take("op", ")")
            return inner
        if tok[0] == "str":
            self.take()
            return ("lit", tok[1])
        if tok[0] == "num":
            self.take()
            return ("lit", float(tok[1]) if "." in tok[1] else int(tok[1]))
        if tok[0] == "id" and tok[1].lower() in ("true", "false", "null"):
            self.take()
            return ("lit", {"true": True, "false": False, "null": None}[tok[1].lower()])
        if tok[0] == "id":
            self.take()
            if tok[1].lower() in FORBIDDEN:
                raise Refused(tok[1])
            if self.peek() == ("op", "("):
                return ("call", None, tok[1], self._args())
            return ("name", tok[1])
        raise Refused("SYNTAX")

    def _args(self):
        self.take("op", "(")
        args = []
        if self.peek() != ("op", ")"):
            args.append(self._or())
            while self.peek() == ("op", ","):
                self.take()
                args.append(self._or())
        self.take("op", ")")
        return args


def parse(text: str):
    if not isinstance(text, str) or len(text) > 4000:
        raise Refused("SIZE")
    for tok in _lex(text):
        if tok[0] == "id" and tok[1].lower() in FORBIDDEN:
            raise Refused(tok[1])
    if _HEAVY.search(text):
        raise Refused("REGEX")
    return _P(_lex(text)).parse()


def _known(kind: str) -> set[str]:
    return {
        "table": {"name", "ishidden", "description", "columns", "measures", "partitions"},
        "column": {"name", "table", "datatype", "ishidden", "iskey", "sourcecolumn", "expression", "description"},
        "measure": {"name", "table", "expression", "ishidden", "displayfolder", "formatstring", "description"},
        "relationship": {"name", "fromtable", "fromcolumn", "totable", "tocolumn", "isactive", "crossfilteringbehavior"},
        "partition": {"name", "mode"},
        "hierarchy": {"name", "table"},
        "calculationitem": {"name", "table"},
        "role": {"name"},
        "perspective": {"name"},
        "function": {"name"},
    }.get(kind, set())


class Obj:
    def __init__(self, kind: str, data: dict, children: dict | None = None):
        self.kind = kind
        self.data = {k.lower(): v for k, v in data.items()}
        self.children = {k.lower(): v for k, v in (children or {}).items()}

    def get(self, name: str):
        key = name.lower()
        if key in FORBIDDEN or key not in _known(self.kind):
            raise Refused(name)
        if key in self.children:
            return self.children[key]
        return self.data.get(key)


def _truth(value) -> bool:
    if isinstance(value, list):
        return len(value) > 0
    return bool(value)


def _cmp(op, left, right) -> bool:
    if op in ("=", "=="):
        return left == right
    if op in ("<>", "!="):
        return left != right
    if left is None or right is None:
        return False
    return {">": left > right, "<": left < right, ">=": left >= right, "<=": left <= right}[op]


def _regex(text, pattern: str) -> bool:
    if not isinstance(pattern, str) or len(pattern) > 80 or _HEAVY.search(pattern):
        raise Refused("REGEX")
    return re.search(pattern, "" if text is None else str(text)) is not None


def _call(receiver, name: str, args, item):
    method = name.lower()
    if method not in METHODS and not (receiver is None and method in METHODS):
        raise Refused(name)
    if method == "ismatch":
        if len(args) != 2:
            raise Refused("ARITY")
        return _regex(_eval(args[0], receiver if isinstance(receiver, Obj) else item, item), _eval(args[1], item, item))
    if method in ("isnullorwhitespace", "isnullorempty") and receiver is None:
        if len(args) != 1:
            raise Refused("ARITY")
        text = _eval(args[0], item, item)
        if text is None:
            return True
        return str(text).strip() == "" if method == "isnullorwhitespace" else str(text) == ""
    if isinstance(receiver, list):
        if method == "count" and not args:
            return len(receiver)
        if method == "any" and not args:
            return len(receiver) > 0
        if method == "count":
            return sum(1 for child in receiver if _truth(_eval(args[0], child, child)))
        if method == "any":
            return any(_truth(_eval(args[0], child, child)) for child in receiver)
        raise Refused(name)
    text = "" if receiver is None else str(receiver)
    if method == "startswith" and len(args) == 1:
        return text.startswith(str(_eval(args[0], item, item)))
    if method == "endswith" and len(args) == 1:
        return text.endswith(str(_eval(args[0], item, item)))
    if method == "contains" and len(args) == 1:
        return str(_eval(args[0], item, item)) in text
    if method == "tolower" and not args:
        return text.lower()
    if method == "toupper" and not args:
        return text.upper()
    if method == "trim" and not args:
        return text.strip()
    if method in ("isnullorwhitespace", "isnullorempty") and not args:
        return text.strip() == "" if method == "isnullorwhitespace" else text == ""
    raise Refused(name)


def _eval(node, obj, item):
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "name":
        if node[1].lower() == "regex":
            return "regex"
        if isinstance(item, Obj):
            try:
                return item.get(node[1])
            except Refused:
                if item is obj:
                    raise
        if isinstance(obj, Obj):
            return obj.get(node[1])
        raise Refused(node[1])
    if kind == "member":
        base = _eval(node[1], obj, item)
        if isinstance(base, str) and base.lower() == "regex" and node[1][0] == "name" and node[1][1].lower() == "regex":
            return "regex"
        if base == "regex":
            return ("regex", node[2])
        if isinstance(base, Obj):
            return base.get(node[2])
        if isinstance(base, list) and node[2].lower() == "count":
            return len(base)
        raise Refused(node[2])
    if kind == "call":
        host, name, args = node[1], node[2], node[3]
        if host is None:
            return _call(None, name, args, item if isinstance(item, Obj) else obj)
        receiver = _eval(host, obj, item)
        if isinstance(receiver, tuple) and receiver[0] == "regex":
            return _call(None, "ismatch", args, item)
        if receiver == "regex":
            return _call(None, name, args, item)
        return _call(receiver, name, args, item if isinstance(item, Obj) else obj)
    if kind == "not":
        return not _truth(_eval(node[1], obj, item))
    if kind == "and":
        return _truth(_eval(node[1], obj, item)) and _truth(_eval(node[2], obj, item))
    if kind == "or":
        return _truth(_eval(node[1], obj, item)) or _truth(_eval(node[2], obj, item))
    if kind == "cmp":
        return _cmp(node[1], _eval(node[2], obj, item), _eval(node[3], obj, item))
    raise Refused("SYNTAX")


def classify(expression: str) -> tuple[str, str]:
    try:
        parse(expression or "")
    except Refused as err:
        return "REFUSED", err.reason
    return "SUPPORTED", ""


def fix_status(expression: str | None) -> str:
    if not expression or not str(expression).strip():
        return "NONE"
    match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*", str(expression))
    if not match or match.group(1).lower() not in _SET_PROPS:
        return "NOT_APPLIED"
    return "APPLICABLE"


def _severity(value) -> int:
    if isinstance(value, int) and value in (1, 2, 3):
        return value
    text = str(value or "").strip().lower()
    return {"informational": 1, "info": 1, "warning": 2, "error": 3}.get(text, 2)


def load_document(text: str) -> dict:
    try:
        data = json.loads(text)
    except (ValueError, UnicodeError) as err:
        raise PbzError("BAD_REQUEST", "rules file is not JSON") from err
    if isinstance(data, list):
        rules = data
    elif isinstance(data, dict):
        rules = data.get("Rules", data.get("rules"))
    else:
        rules = None
    if not isinstance(rules, list):
        raise PbzError("BAD_REQUEST", "rules file needs a Rules array")
    out = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        expr = str(rule.get("Expression") or rule.get("expression") or "")
        status, reason = classify(expr)
        out.append({
            "id": str(rule.get("ID") or rule.get("id") or "RULE")[:80],
            "name": str(rule.get("Name") or rule.get("name") or "")[:120],
            "scope": str(rule.get("Scope") or rule.get("scope") or "Table"),
            "severity": _severity(rule.get("Severity", rule.get("severity", 2))),
            "expression": expr,
            "status": status,
            "reason": reason,
            "fix": fix_status(rule.get("FixExpression") or rule.get("fixExpression")),
        })
    return {"rules": out, "supported": sum(1 for r in out if r["status"] == "SUPPORTED"),
            "refused": sum(1 for r in out if r["status"] == "REFUSED")}


def load_path(path: str) -> dict:
    if re.match(r"(?i)^[a-z][a-z0-9+.-]*://", path or ""):
        raise PbzError("REFUSED", "rules are read from a local file")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError as err:
        raise PbzError("BAD_REQUEST", "rules file could not be read") from err
    return load_document(text)


def _views(model):
    tables, columns, measures, rels = [], [], [], []
    for table in model.tables.values():
        col_views, measure_views = [], []
        for column in table.columns.values():
            view = Obj("column", {
                "Name": column.name, "Table": table.name, "DataType": column.data_type,
                "IsHidden": column.hidden, "IsKey": bool(column.node and "isKey" in column.node.props),
                "SourceColumn": column.source_column, "Expression": "",
                "Description": (column.node.props.get("description", "") if column.node else ""),
            })
            columns.append(view)
            col_views.append(view)
        for measure in table.measures.values():
            hidden = bool(measure.node and "isHidden" in measure.node.props)
            view = Obj("measure", {
                "Name": measure.name, "Table": measure.table, "Expression": measure.expression,
                "IsHidden": hidden, "DisplayFolder": measure.folder, "FormatString": measure.format_string,
                "Description": (measure.node.props.get("description", "") if measure.node else ""),
            })
            measures.append(view)
            measure_views.append(view)
        tables.append(Obj("table", {
            "Name": table.name, "IsHidden": table.hidden,
            "Description": (table.node.props.get("description", "") if table.node else ""),
        }, {"columns": col_views, "measures": measure_views, "partitions": [Obj("partition", {"Name": n, "Mode": k}) for n, k, _s in table.partitions]}))
    for rel in model.relationships:
        rels.append(Obj("relationship", {
            "Name": rel.name, "FromTable": rel.from_table, "FromColumn": rel.from_column,
            "ToTable": rel.to_table, "ToColumn": rel.to_column, "IsActive": rel.active,
            "CrossFilteringBehavior": "Both" if rel.both else "OneDirection",
        }))
    return {"table": tables, "column": columns, "measure": measures, "relationship": rels}


def evaluate(model, document: dict) -> dict:
    """Run supported rules. A refused rule is not evaluated and adds no finding."""
    views = _views(model)
    findings = []
    executed = 0
    for rule in document.get("rules", []):
        if rule.get("status") != "SUPPORTED":
            continue
        try:
            ast = parse(rule.get("expression") or "")
        except Refused:
            continue
        scope = str(rule.get("scope") or "").lower()
        executed += 1
        kept = []
        refused = False
        for obj in views.get(scope, []):
            try:
                if _truth(_eval(ast, obj, obj)):
                    kept.append({"rule": rule["id"], "severity": rule["severity"], "scope": scope,
                                 "kind": scope, "object": obj.get("Name"), "message": rule.get("name") or rule["id"]})
            except Refused:
                refused = True
                break
        if refused:
            continue
        findings.extend(kept)
    return {"executed": executed, "findings": findings}


def fuzz(seed: int = 1, n: int = 40) -> dict:
    """Random expressions either classify or refuse. Nothing else escapes."""
    import random
    rng = random.Random(seed)
    atoms = ['true', 'false', 'not IsHidden', 'Name = "A"', 'Name.StartsWith("A")',
             'IsNullOrWhitespace(Description)', 'Regex.IsMatch(Name, "^A")', '1 = 2']
    errors = 0
    for _ in range(n):
        expr = rng.choice(atoms)
        if rng.random() < 0.5:
            expr = f"({expr}) and ({rng.choice(atoms)})"
        try:
            classify(expr)
        except Exception:
            errors += 1
    return {"verb": "ruleset", "fuzz_errors": errors, "cases": n}
