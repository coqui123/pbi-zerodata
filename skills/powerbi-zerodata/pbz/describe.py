"""Data dictionary for the agent: names, types, classes, buckets, category labels, usage. No raw values."""
from __future__ import annotations

import json
import os
import re

from . import lexers, pbir, policy as policy_mod, tmdl, tokens
from .classify import Classifier
from .common import PbzError, read_text_checked


def _index_summary(ws) -> dict:
    try:
        with open(os.path.join(ws.private, "index-summary.json"), encoding="utf-8") as f:  # pbz-allow-open: private index
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for t in data.get("tables", []):
        out[t.get("table")] = t
    return out


def field_usage(project, model) -> dict:
    """(kind, table, name) -> [page | visual | type] from the mirror report."""
    use: dict = {}
    pages_dir = os.path.join(project.mirror_report, "definition", "pages")
    if not os.path.isdir(pages_dir):
        return use
    for pid in sorted(os.listdir(pages_dir)):
        pjson = os.path.join(pages_dir, pid, "page.json")
        if not os.path.isfile(pjson):
            continue
        proot = pbir.parse_json(read_text_checked(pjson, root=os.path.dirname(project.mirror_report))[0])
        pname = proot.get("displayName").value if isinstance(proot.get("displayName"), pbir.JStr) else pid
        for ref in pbir.walk_fields(proot, model):
            use.setdefault((ref.kind, ref.table, ref.name), set()).add(f"{pname} | (page filter)")
        vdir = os.path.join(pages_dir, pid, "visuals")
        for vid in sorted(os.listdir(vdir)) if os.path.isdir(vdir) else []:
            vjson = os.path.join(vdir, vid, "visual.json")
            if not os.path.isfile(vjson):
                continue
            vroot = pbir.parse_json(read_text_checked(vjson, root=os.path.dirname(project.mirror_report))[0])
            v = vroot.get("visual")
            vt = v.get("visualType").value if isinstance(v, pbir.JObj) and isinstance(v.get("visualType"), pbir.JStr) else "?"
            for ref in pbir.walk_fields(vroot, model):
                kind = "column" if ref.kind in ("aggregation", "hierarchy") else ref.kind
                use.setdefault((kind, ref.table, ref.name), set()).add(f"{pname} | {vid} | {vt}")
    return {k: sorted(v) for k, v in use.items()}


def _deps(expr: str, table: str, model) -> list[str]:
    out = []
    sig = lexers.significant(lexers.lex_dax(expr))
    for i, t in enumerate(sig):
        if t.kind != "bracket":
            continue
        prev = sig[i - 1] if i else None
        if prev is not None and prev.end == t.start and prev.kind in ("table", "ident"):
            out.append(f"{prev.value}[{t.value}]")
        elif model.measure(t.value):
            out.append(f"[{t.value}]")
        elif model.column(table, t.value):
            out.append(f"{table}[{t.value}]")
    return sorted(set(out))


def _parse_target(target: str):
    m = re.match(r"^\s*'((?:[^']|'')+)'\s*\[(.+)\]\s*$", target) or re.match(r"^\s*([^\[\]']+?)\s*\[(.+)\]\s*$", target)
    if m:
        return "column", m.group(1).replace("''", "'"), m.group(2)
    m = re.match(r"^\s*\[(.+)\]\s*$", target)
    if m:
        return "measure", None, m.group(1)
    return "name", None, target.strip().strip("'")


def overview(ws, project) -> dict:
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    model = tmdl.load_model(mdef)
    summ = _index_summary(ws)
    pages_dir = os.path.join(project.mirror_report, "definition", "pages")
    pages = []
    if os.path.isdir(pages_dir):
        for pid in sorted(os.listdir(pages_dir)):
            pj = os.path.join(pages_dir, pid, "page.json")
            if os.path.isfile(pj):
                root = pbir.parse_json(read_text_checked(pj, root=os.path.dirname(project.mirror_report))[0])
                name = root.get("displayName").value if isinstance(root.get("displayName"), pbir.JStr) else pid
                vdir = os.path.join(pages_dir, pid, "visuals")
                pages.append({"id": pid, "name": name, "visuals": len(os.listdir(vdir)) if os.path.isdir(vdir) else 0})
    return {"verb": "describe", "project": project.name,
            "tables": [{"table": t.name, "columns": len(t.columns), "measures": len(t.measures),
                        "storage": sorted({k for _n, k, _e in t.partitions}) or ["none"], "hidden": t.hidden,
                        "rows_bucket": summ.get(t.name, {}).get("rows_bucket", "unknown")}
                       for t in model.tables.values()],
            "relationships": len(model.relationships), "roles": model.roles, "pages": pages}


def describe(ws, project, target: str | None) -> dict:
    if not target:
        return overview(ws, project)
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    model = tmdl.load_model(mdef)
    clf = Classifier(tmdl.load_model(os.path.join(project.mirror_model, "definition")), ws.private, policy_mod.load(ws))
    summ = _index_summary(ws)
    usage = field_usage(project, model)
    kind, table, name = _parse_target(target)
    if kind == "name" and name in model.tables:
        return _table(model, clf, summ, usage, model.tables[name])
    if kind == "name" and model.measure(name) or kind == "measure":
        m = model.measure(name)
        if not m:
            raise PbzError("NOT_FOUND", target)
        dependents = sorted(f"[{x.name}]" for x in model.all_measures()
                            if x.name != m.name and f"[{m.name}]" in _deps(x.expression, x.table, model))
        return {"verb": "describe", "measure": m.name, "table": m.table, "format": m.format_string,
                "folder": m.folder, "dax": m.expression.strip(), "depends_on": _deps(m.expression, m.table, model),
                "used_by_measures": dependents, "used_by_visuals": usage.get(("measure", m.table, m.name), [])}
    if kind == "column":
        col = model.column(table, name)
        if not col:
            raise PbzError("NOT_FOUND", target)
        return {"verb": "describe", **_column(model, clf, summ, col),
                "used_by_visuals": usage.get(("column", col.table, col.name), [])}
    if kind == "name":
        hits = [c for c in model.all_columns() if c.name.lower() == name.lower()]
        if len(hits) == 1:
            return describe(ws, project, f"'{hits[0].table}'[{hits[0].name}]")
    raise PbzError("NOT_FOUND", target)


def _column(model, clf, summ, col) -> dict:
    info = next((c for c in summ.get(col.table, {}).get("columns", []) if c.get("column") == col.name), {})
    cls = clf.column_class(col.table, col.name)
    keys = [f"{r.from_table}[{r.from_column}] -> {r.to_table}[{r.to_column}]" for r in model.relationships
            if (r.from_table, r.from_column) == (col.table, col.name) or (r.to_table, r.to_column) == (col.table, col.name)]
    out = {"column": f"{col.table}[{col.name}]", "type": col.data_type, "class": cls,
           "calculated": col.calculated, "hidden": col.hidden}
    if col.sort_by:
        out["sort_by"] = col.sort_by
    if keys:
        out["relationships"] = keys
    for k in ("card_bucket", "blank_bucket"):
        if k in info:
            out[k] = info[k]
    vals = clf.category_values(col.table, col.name)
    if vals is not None:
        out["values"] = vals
    return out


def _table(model, clf, summ, usage, t) -> dict:
    src = []
    for pname, pkind, expr in t.partitions:
        funcs = sorted(set(re.findall(r"\b((?:[A-Z][A-Za-z]+\.)+[A-Z][A-Za-z]+)\s*\(", expr)))
        src.append({"partition": pname, "kind": pkind, "functions": funcs[:12]})
    out = {"verb": "describe", "table": t.name, "file": t.file, "hidden": t.hidden, "calculated": t.calculated,
           "partitions": src, "rows_bucket": summ.get(t.name, {}).get("rows_bucket", "unknown (run pbz index)"),
           "columns": [_column(model, clf, summ, c) for c in t.columns.values()],
           "measures": [{"name": m.name, "folder": m.folder} for m in t.measures.values()],
           "hierarchies": t.hierarchies,
           "relationships": [f"{r.from_table}[{r.from_column}] -> {r.to_table}[{r.to_column}]"
                             + (" (both)" if r.both else "") + ("" if r.active else " (inactive)")
                             for r in model.relationships if t.name in (r.from_table, r.to_table)]}
    return out


def values(ws, project, column: str) -> dict:
    kind, table, name = _parse_target(column)
    if kind != "column":
        raise PbzError("BAD_REQUEST", "use 'Table'[Column]")
    real = tmdl.load_model(os.path.join(project.mirror_model, "definition"))
    clf = Classifier(real, ws.private, policy_mod.load(ws))
    lab = f"{table}[{name}]"
    cls = clf.column_class(table, name)
    out = {"verb": "values", "column": lab, "class": cls, "classification": clf.mode}
    vals = clf.category_values(table, name)
    if vals is not None:
        out["values"] = vals
    else:
        info = next((c for c in _index_summary(ws).get(table, {}).get("columns", []) if c.get("column") == name), {})
        if "card_bucket" in info:
            out["distinct_bucket"] = info["card_bucket"]
        tm = tokens.TokenMap(ws.private)
        out["tokens_in_mirror"] = sorted(t for t, r in tm.data.get("tokens", {}).items() if r.get("label") == lab)
        if cls == "category":
            out["note"] = "category column but no index yet: run `pbz index` with Desktop open"
    return out
