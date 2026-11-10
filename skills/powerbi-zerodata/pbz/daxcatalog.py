"""Offline arity check for well-known DAX functions.

Unknown functions and model user-defined functions are not errors. A known function with the
wrong number of arguments is. The catalog is conservative so a correct call is not flagged.
"""
from __future__ import annotations

from . import lexers

# (min, max). max None means variadic.
CATALOG = {
    "SUM": (1, 1), "SUMX": (2, 2), "DIVIDE": (2, 3), "IF": (2, 3), "SWITCH": (3, None),
    "CALCULATE": (1, None), "CALCULATETABLE": (1, None), "COUNTROWS": (1, 1), "RELATED": (1, 1),
    "USERELATIONSHIP": (2, 2), "FILTER": (2, 2), "ALL": (0, None), "ALLEXCEPT": (1, None),
    "VALUES": (1, 1), "DISTINCT": (1, 1), "COUNT": (1, 1), "COUNTA": (1, 1), "AVERAGE": (1, 1),
    "AVERAGEX": (2, 2), "MIN": (1, 2), "MAX": (1, 2), "AND": (2, 2), "OR": (2, 2), "NOT": (1, 1),
    "IFERROR": (2, 2), "BLANK": (0, 0), "TODAY": (0, 0), "NOW": (0, 0), "DATE": (3, 3),
    "YEAR": (1, 1), "MONTH": (1, 1), "DAY": (1, 1), "FORMAT": (2, 2), "CONCATENATE": (2, 2),
    "LEFT": (2, 2), "RIGHT": (2, 2), "LEN": (1, 1), "ROUND": (2, 3), "INT": (1, 1), "ABS": (1, 1),
    "SQRT": (1, 1), "SELECTEDVALUE": (1, 2), "HASONEVALUE": (1, 1), "ISBLANK": (1, 1),
    "ISEMPTY": (1, 1), "RANKX": (2, 5), "TOPN": (2, None), "ADDCOLUMNS": (2, None),
    "SELECTCOLUMNS": (2, None), "SUMMARIZE": (1, None), "TREATAS": (2, None), "CROSSFILTER": (3, 3),
    "COUNTX": (2, 2), "MINX": (2, 2), "MAXX": (2, 2), "CONCATENATEX": (2, None),
    "LOOKUPVALUE": (2, None), "CONTAINSSTRING": (2, 2), "SEARCH": (2, 4), "FIND": (2, 4),
    "SUBSTITUTE": (3, 4), "REPLACE": (4, 4), "VALUE": (1, 1), "TEXT": (2, 2), "TRUE": (0, 0),
    "FALSE": (0, 0), "WEEKDAY": (1, 2), "EOMONTH": (2, 2), "DATEDIFF": (3, 3), "DATEADD": (3, 3),
    "SAMEPERIODLASTYEAR": (1, 1), "TOTALYTD": (2, 4), "CALENDAR": (2, 2), "CALENDARAUTO": (0, 1),
    "GENERATESERIES": (2, 3), "ROW": (2, None), "DATATABLE": (1, None), "UNION": (2, None),
    "INTERSECT": (2, 2), "EXCEPT": (2, 2), "SELECTEDMEASURE": (0, 0), "ISSELECTEDMEASURE": (1, None),
    "CALCULATE": (1, None),
}


def calls(expr: str) -> list[tuple[str, int]]:
    """(function, argument count) for every call, including calls nested inside other calls.

    Commas inside `{ }` are table-constructor elements, not arguments of the enclosing function.
    """
    sig = lexers.significant(lexers.lex_dax(expr or ""))
    out = []
    i = 0
    while i < len(sig):
        tok = sig[i]
        if tok.kind == "ident" and i + 1 < len(sig) and sig[i + 1].text == "(":
            depth, args, seen = 0, 0, False
            j = i + 1
            while j < len(sig):
                text = sig[j].text
                if text in ("(", "{"):
                    depth += 1
                elif text == "}":
                    depth -= 1
                elif text == ")":
                    depth -= 1
                    if depth == 0:
                        if seen:
                            args += 1
                        out.append((tok.text.upper(), args))
                        break
                elif text == "," and depth == 1:
                    args += 1
                    seen = False
                elif depth >= 1 and text != ",":
                    seen = True
                j += 1
            i += 1
        i += 1
    return out


def _udf_arity(model) -> dict[str, int]:
    out = {}
    for name, node in getattr(model, "functions", {}).items():
        text = ""
        doc = model.docs.get(node.file)
        if doc is not None:
            reg = next((r for r in doc.regions if r.node is node and r.kind == "dax"), None)
            if reg is not None:
                text = doc.text[reg.start:reg.end]
        params = text.split("=>", 1)[0]
        inner = params[params.find("(") + 1:params.rfind(")")] if "(" in params else ""
        count = 0
        depth = 0
        piece = ""
        for ch in inner:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                if piece.strip():
                    count += 1
                piece = ""
            else:
                piece += ch
        if piece.strip():
            count += 1
        out[name.upper()] = count
    return out


def findings(model) -> list[dict]:
    udf = _udf_arity(model)
    tables = {t.name.upper() for t in model.tables.values()}
    out = []
    seen = set()
    for table in model.tables.values():
        exprs = [(f"[{m.name}]", m.expression) for m in table.measures.values()]
        exprs += [(f"'{table.name}'[{c.name}]", "") for c in table.columns.values() if c.calculated]
        for label, expr in exprs:
            if not expr:
                continue
            for name, argc in calls(expr):
                if name in tables or name in udf:
                    if name in udf and argc != udf[name]:
                        key = (label, name, argc)
                        if key not in seen:
                            seen.add(key)
                            out.append({"severity": "error", "code": "DAX_ARITY", "object": label,
                                        "message": f"{name} has the wrong number of arguments"})
                    continue
                bounds = CATALOG.get(name)
                if bounds is None:
                    continue
                lo, hi = bounds
                if argc < lo or (hi is not None and argc > hi):
                    key = (label, name, argc)
                    if key not in seen:
                        seen.add(key)
                        out.append({"severity": "error", "code": "DAX_ARITY", "object": label,
                                    "message": f"{name} has the wrong number of arguments"})
    return out


def compile_note(ws, project, model) -> dict:
    """Ask Desktop to bind measure expressions when it is open. A closed Desktop is NOT_RUN."""
    from . import desktop
    from .common import PbzError, engine_call
    try:
        port = desktop.require_port(project)
    except PbzError:
        return {"status": "NOT_RUN"}
    items = []
    for table in model.tables.values():
        for measure in list(table.measures.values())[:200]:
            if measure.expression.strip():
                items.append({"id": measure.name[:120], "table": table.name, "expression": measure.expression})
    if not items:
        return {"status": "NOT_RUN"}
    try:
        res = engine_call("compiles", {"private_dir": ws.private, "port": port, "items": items}, timeout=120)
    except PbzError:
        return {"status": "NOT_RUN"}
    if not isinstance(res, dict) or res.get("ok") is not True:
        return {"status": "NOT_RUN"}
    counts = {}
    for item in res.get("items") or []:
        if isinstance(item, dict):
            status = item.get("status") if item.get("status") in ("PASS", "ERROR") else "ERROR"
            counts[status] = counts.get(status, 0) + 1
    return {"status": "DONE", "counts": counts}
