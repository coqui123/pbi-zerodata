"""Join Power Query origins to model usage, and describe a source-column change.

Native SQL is read only when the query text is visible. A tokenized query is OPAQUE and is not
detokenized. Server and database names are HMAC'd, or REDACTED when this workspace has no key.
OpenLineage output is a local file. Nothing is sent to a network.
"""
from __future__ import annotations

import json
import os
import re
import uuid

from . import dpapi, mlineage, tmdl, unused
from .common import PbzError, write_text, _workspace_root
from .tokens import TOKEN_RE

_SQL_DB = re.compile(r"(?is)Sql\.Database\s*\(\s*(?P<q1>\"\"\"|\")(?P<server>.*?)(?P=q1)\s*,\s*(?P<q2>\"\"\"|\")(?P<db>.*?)(?P=q2)")
_SELECT = re.compile(r"(?is)\bselect\b(?P<cols>.*?)\bfrom\b")
_NATIVE_CALL = re.compile(r"(?i)(?:Value\.)?NativeQuery\s*\(")


def _key(ws, value: str) -> str:
    try:
        key = dpapi.load_key(ws.private, create=False)
    except (OSError, FileNotFoundError):
        return "REDACTED"
    except Exception:
        return "REDACTED"
    return dpapi.hmac_hex(key, value)


def _dependents(node, objects: list, visuals: list) -> None:
    """Walk a lineage node. Children carry object names; used_in carries visual keys. No nested trees."""
    if not isinstance(node, dict):
        return
    used = node.get("used_in")
    if isinstance(used, dict):
        for key, vals in used.items():
            if isinstance(key, str) and key and key not in visuals:
                visuals.append(key)
            if isinstance(vals, list):
                for item in vals:
                    if isinstance(item, str) and item and item not in visuals:
                        visuals.append(item)
    for child in node.get("children") or []:
        if not isinstance(child, dict):
            continue
        name = child.get("object")
        if isinstance(name, str) and name and name not in objects:
            objects.append(name)
        _dependents(child, objects, visuals)


def _skip_m_string(text: str, i: int) -> int | None:
    """Index after an M `"` string starting at i, treating `""` as an escaped quote."""
    if i >= len(text) or text[i] != '"':
        return None
    i += 1
    n = len(text)
    while i < n:
        if text[i] == '"':
            if i + 1 < n and text[i + 1] == '"':
                i += 2
                continue
            return i + 1
        i += 1
    return None


def _comma_after_balanced_arg(text: str, i: int) -> int | None:
    """Index of the comma that ends NativeQuery's first argument.

    Parentheses, braces, brackets, and quotes nest, so `Sql.Database(server, database)`
    and `Source{[Schema="x",Item="y"]}[Data]` each stay one argument. A comma inside a
    quote, comment, or « » token does not end the argument.
    """
    n = len(text)
    paren = brace = bracket = 0
    while i < n:
        if text.startswith("«", i):
            j = text.find("»", i + 1)
            if j < 0 or "\n" in text[i:j]:
                return None
            i = j + 1
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                return None
            i = j + 2
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        ch = text[i]
        if ch == '"':
            j = _skip_m_string(text, i)
            if j is None:
                return None
            i = j
            continue
        if ch == "(":
            paren += 1
            i += 1
            continue
        if ch == ")":
            if paren == 0:
                return None
            paren -= 1
            i += 1
            continue
        if ch == "{":
            brace += 1
            i += 1
            continue
        if ch == "}":
            if brace == 0:
                return None
            brace -= 1
            i += 1
            continue
        if ch == "[":
            bracket += 1
            i += 1
            continue
        if ch == "]":
            if bracket == 0:
                return None
            bracket -= 1
            i += 1
            continue
        if ch == "," and paren == 0 and brace == 0 and bracket == 0:
            return i
        i += 1
    return None


def _skip_ws_and_comments(text: str, i: int) -> int | None:
    n = len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                return None
            i = j + 2
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        return i
    return i


def _sql_argument_closed(text: str, end: int) -> bool:
    """True when the SQL literal is the whole argument, not the start of an `&` chain."""
    nxt = _skip_ws_and_comments(text, end)
    return nxt is not None and nxt < len(text) and text[nxt] in ",)"


def _sql_literal(text: str, i: int) -> tuple[str, str] | None:
    """(quote, body) of the SQL argument, or None when it is not one string or token."""
    i = _skip_ws_and_comments(text, i)
    if i is None or i >= len(text):
        return None
    if text.startswith("«", i):
        j = text.find("»", i + 1)
        if j < 0 or "\n" in text[i:j] or not _sql_argument_closed(text, j + 1):
            return None
        return "«", text[i + 1:j]
    # M has no triple-quoted strings. `"""..."""` is a normal string whose value
    # starts and ends with a quote, so the `""` unescape leaves a `"` in the body.
    if text[i] != '"':
        return None
    j = _skip_m_string(text, i)
    if j is None or not _sql_argument_closed(text, j):
        return None
    return '"', text[i + 1:j - 1].replace('""', '"')


def _native_query_sql(text: str) -> tuple[str, str] | None:
    """SQL string passed to the first NativeQuery in `text`, if its arguments parse."""
    for match in _NATIVE_CALL.finditer(text):
        comma = _comma_after_balanced_arg(text, match.end())
        if comma is None:
            continue
        found = _sql_literal(text, comma + 1)
        if found is not None:
            return found
    return None


def _select_list(sql: str) -> list[str]:
    """Bare select-list identifiers. A quote or a comment fails closed so a literal is not a column."""
    match = _SELECT.search(sql)
    if not match:
        return []
    cols = match.group("cols")
    if "'" in cols or "--" in cols or "/*" in cols:
        return []
    names = []
    for part in cols.split(","):
        ident = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", part)
        if not ident or "'" in part or ident[-1].lower() in ("as", "distinct", "top"):
            continue
        names.append(ident[-1])
    return names


def native_sql(ws, project) -> dict:
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    model = tmdl.load_model(mdef)
    queries = []
    for table in model.tables.values():
        for part in table.partition_nodes:
            doc = model.docs.get(part.file)
            if doc is None:
                continue
            reg = next((r for r in doc.regions if r.node is part and r.kind == "m"), None)
            if reg is None:
                continue
            text = doc.text[reg.start:reg.end]
            if "NativeQuery" not in text and "Sql.Database" not in text:
                continue
            item = {"table": table.name, "partition": part.name}
            sql = _native_query_sql(text)
            body = "" if sql is None else sql[1]
            if (sql is None or sql[0] == "«" or '"' in body or "#(" in body
                    or TOKEN_RE.search(body) or not re.match(r"(?i)\s*select\b", body)):
                item["status"] = "OPAQUE"
            else:
                item["status"] = "PARSED"
                item["columns"] = _select_list(body)
            db = _SQL_DB.search(text)
            server, database = (db.group("server"), db.group("db")) if db else ("", "")
            if db and not TOKEN_RE.search(server) and not TOKEN_RE.search(database):
                item["server"] = _key(ws, server)
                item["database"] = _key(ws, database)
            elif db:
                item["server"] = "OPAQUE"
                item["database"] = "OPAQUE"
            queries.append(item)
    return {"verb": "lineage", "native_sql": queries}


def _mapping(ws, project):
    report = mlineage.run(ws, project)
    rows = []
    for table in report.get("tables") or []:
        for item in table.get("columns") or []:
            rows.append({"table": table.get("table"), **item})
    return rows


def from_source(ws, project, table: str, column: str) -> dict:
    rows = [r for r in _mapping(ws, project)
            if r.get("table", "").lower() == table.lower() and str(r.get("source", "")).lower() == column.lower()]
    trees = []
    for row in rows[:5]:
        ref = f"'{row['table']}'[{row['column']}]"
        try:
            trees.append(unused.lineage(ws, project, ref, "down", 4))
        except PbzError:
            trees.append({"object": ref, "status": "NOT_FOUND"})
    return {"verb": "lineage", "from_source": {"table": table, "column": column},
            "columns": [{"table": r["table"], "column": r["column"], "origin": r.get("origin")} for r in rows],
            "trees": trees}


def impact(ws, project, source: str, mode: str, new_name: str | None = None) -> dict:
    rows = [r for r in _mapping(ws, project) if str(r.get("source", "")).lower() == source.lower()
            or source.lower() in str(r.get("origin", "")).lower()]
    objects = []
    for row in rows[:8]:
        ref = f"'{row['table']}'[{row['column']}]"
        dependents, visuals = [], []
        try:
            tree = unused.lineage(ws, project, ref, "down", 3)
        except PbzError:
            tree = None
        for node in (tree or {}).get("trees") or []:
            _dependents(node, dependents, visuals)
        objects.append({"column": ref, "origin": row.get("origin"),
                        "dependents": dependents[:12], "visuals": visuals[:12]})
    out = {"verb": "impact", "action": mode, "source_column": source, "columns": objects,
           "steps": sorted({str(r.get("origin")) for r in rows if r.get("origin")})[:20]}
    if mode == "rename":
        out["to"] = new_name
    return out


def openlineage(ws, project, path: str) -> dict:
    if re.match(r"(?i)^[a-z][a-z0-9+.-]*://", path or ""):
        raise PbzError("REFUSED", "OpenLineage output is a local file")
    rows = _mapping(ws, project)
    sql = native_sql(ws, project)
    inputs = []
    by_table: dict[str, list] = {}
    for row in rows:
        by_table.setdefault(row.get("table") or "", []).append(row.get("column"))
    for name, cols in sorted(by_table.items()):
        if not name:
            continue
        inputs.append({"namespace": "pbz", "name": name,
                       "facets": {"schema": {"fields": [{"name": c} for c in cols if c]}}})
    for query in sql["native_sql"]:
        if query.get("server"):
            inputs.append({"namespace": query["server"], "name": query.get("database", "OPAQUE"),
                           "facets": {"schema": {"fields": [{"name": c} for c in query.get("columns") or []]}}})
    event = {"eventType": "COMPLETE", "eventTime": "1970-01-01T00:00:00Z", "producer": "pbz",
             "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json",
             "run": {"runId": str(uuid.uuid4())},
             "job": {"namespace": "pbz", "name": project.name},
             "inputs": inputs, "outputs": [{"namespace": "pbz", "name": project.name}]}
    write_text(path, json.dumps(event) + "\n", root=_workspace_root(ws))
    return {"verb": "lineage", "format": "openlineage", "file": os.path.basename(path),
            "inputs": len(inputs)}
