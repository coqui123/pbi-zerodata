"""Model lint rules (metadata only). Findings name objects, never values."""
from __future__ import annotations

import re

from . import lexers

DATE_NAME = re.compile(r"(?i)date|calendar")


def date_tables(model) -> set[str]:
    out = set()
    for t in model.tables.values():
        if DATE_NAME.search(t.name):
            out.add(t.name)
    for r in model.relationships:
        col = model.column(r.to_table, r.to_column)
        if col and col.data_type.lower().startswith("date"):
            out.add(r.to_table)
    return out


def expanded(model, table: str) -> set[str]:
    """Tables in the expanded table of `table` (follow many -> one along active relationships)."""
    seen, todo = {table}, [table]
    while todo:
        t = todo.pop()
        for r in model.relationships:
            if r.active and r.from_table == t and r.to_table not in seen:
                seen.add(r.to_table)
                todo.append(r.to_table)
    return seen


def _whole_table_calls(expr: str, funcs=("REMOVEFILTERS", "ALL", "ALLNOBLANKROW", "ALLCROSSFILTERED")):
    sig = lexers.significant(lexers.lex_dax(expr))
    for i, t in enumerate(sig):
        if t.kind == "ident" and t.text.upper() in funcs and i + 3 < len(sig) and sig[i + 1].text == "(":
            a = sig[i + 2]
            if a.kind in ("table", "ident") and sig[i + 3].text == ")" and not (i + 3 < len(sig) and sig[i + 3].kind == "bracket"):
                yield t.text.upper(), a.value if a.kind == "table" else a.text


def run(model) -> list[dict]:
    out: list[dict] = []
    seen: dict[str, str] = {}
    for m in model.all_measures():
        key = m.name.lower()
        if key in seen:
            out.append({"severity": "error", "code": "DUP_MEASURE", "where": f"{m.table}[{m.name}]",
                        "message": f"measure name also used in table {seen[key]}"})
        seen[key] = m.table
    dates = date_tables(model)
    names = {t.lower() for t in model.tables}
    for m in model.all_measures():
        for fn, tab in _whole_table_calls(m.expression):
            if tab.lower() not in names:
                continue
            hit = sorted(d for d in expanded(model, tab) & dates if d != tab)
            if hit:
                out.append({"severity": "warning", "code": "EXPANDED_TABLE_CLEARS_DATE", "where": f"{m.table}[{m.name}]",
                            "message": f"{fn}('{tab}') also clears filters on {', '.join(hit)} through the expanded "
                                       f"table. Scope it to columns or use ALLSELECTED for the selected window."})
        sig = lexers.significant(lexers.lex_dax(m.expression))
        for i, t in enumerate(sig):
            if t.kind == "ident" and t.text.upper() in ("CONTAINSSTRING", "SEARCH", "FIND") and i + 1 < len(sig):
                for k in range(i + 2, min(i + 8, len(sig))):
                    if sig[k].kind == "str" and 0 < len(sig[k].value.strip()) <= 4:
                        out.append({"severity": "info", "code": "SHORT_SUBSTRING", "where": f"{m.table}[{m.name}]",
                                    "message": f"{t.text.upper()} with a {len(sig[k].value.strip())}-character literal "
                                               "matches inside longer values (case-insensitive). Prefer exact match."})
                        break
            if t.kind == "table" and i + 1 < len(sig) and sig[i + 1].kind == "bracket" and sig[i + 1].start == t.end:
                tab, col = t.value, sig[i + 1].value
                if tab.lower() in names and not model.column(tab, col) and not model.measure(col):
                    out.append({"severity": "warning", "code": "UNKNOWN_REF", "where": f"{m.table}[{m.name}]",
                                "message": f"reference '{tab}'[{col}] not found in the model"})
    both = [r for r in model.relationships if r.both]
    if both:
        out.append({"severity": "info", "code": "BIDIRECTIONAL", "where": "relationships",
                    "message": f"{len(both)} relationship(s) filter both directions: "
                               + ", ".join(f"{r.from_table}->{r.to_table}" for r in both[:8])})
    return out
