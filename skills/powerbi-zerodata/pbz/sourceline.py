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

_SQL_STRING = re.compile(r"(?is)(?:Value\.)?NativeQuery\s*\(\s*[^,]+,\s*(?P<q>\"\"\"|\"|«)(?P<body>.*?)(?P=q)")
_SQL_DB = re.compile(r"(?is)Sql\.Database\s*\(\s*(?P<q1>\"\"\"|\")(?P<server>.*?)(?P=q1)\s*,\s*(?P<q2>\"\"\"|\")(?P<db>.*?)(?P=q2)")
_SELECT = re.compile(r"(?is)\bselect\b(?P<cols>.*?)\bfrom\b")


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
            sql = _SQL_STRING.search(text)
            if (sql is None or sql.group("q") == "«" or '"' in sql.group("body")
                    or TOKEN_RE.search(sql.group("body")) or not re.search(r"(?i)\bselect\b", sql.group("body"))):
                item["status"] = "OPAQUE"
            else:
                item["status"] = "PARSED"
                item["columns"] = _select_list(sql.group("body"))
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
