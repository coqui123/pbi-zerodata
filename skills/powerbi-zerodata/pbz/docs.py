"""`pbz docs`: documentation of the model and its reports, built from the sanitized mirror only.

Writes pbz-workspace/docs/<Project>.md | .html | .json. Names, types, structure, dependencies and usage
counts; never values. DAX and M appear only with --dax, exactly as in the mirror (tokens stay tokens).
Power Query is summarized as connector / function names and step names, without literal arguments.
The HTML file is self-contained: escaped text, <details> sections, inline CSS, no scripts, no links.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re

from . import deps, lexers, mirror, pbir, tmdl, usage
from .common import PbzError, anchor_mirror, pbz_state_path, preflight_regular, read_checked, read_text, write_text

SECTIONS = ["overview", "tables", "measures", "calculation_groups", "functions", "field_parameters",
            "relationships", "power_query", "security", "reports", "unused", "changelog"]


def _desc(model, node) -> bool:
    doc = model.docs.get(node.file) if node is not None else None
    return bool(doc and any(r.node is node and r.kind == "text" and r.prop == "description" for r in doc.regions))


def _used_by(g, k) -> dict:
    visuals = {(u.report, u.page, u.visual) for u in g.usages.get(k, [])}
    objs = sorted({g.label(e.src) for e in g.inc.get(k, []) if e.via not in ("home", "calc_item")})
    return {"objects": objs, "visuals": len(visuals)}


def _m_summary(text: str) -> tuple[list[str], list[str]]:
    """(function names, let step names) of an M expression; no literal arguments."""
    sig = lexers.significant(lexers.lex_m(text))
    funcs, steps = [], []
    for i, t in enumerate(sig):
        nxt = sig[i + 1] if i + 1 < len(sig) else None
        if t.kind == "ident" and "." in t.text and nxt is not None and nxt.kind == "op" and nxt.text == "(":
            if t.text not in funcs:
                funcs.append(t.text)
        elif t.kind in ("ident", "qident") and nxt is not None and nxt.kind == "op" and nxt.text == "=":
            prev = sig[i - 1] if i else None
            if prev is None or (prev.kind == "op" and prev.text == ",") or (prev.kind == "ident" and prev.text == "let"):
                name = t.value if t.kind == "qident" else t.text
                if name not in steps:
                    steps.append(name)
    return funcs, steps


def _label(f) -> str:
    if f.kind == "measure":
        return f"[{f.name}]"
    if f.kind == "hierarchy":
        return f"{f.table}.{f.hierarchy or f.name}.{f.level}"
    return f"{f.table}[{f.name}]"


def _s(node) -> str:
    return node.value if isinstance(node, pbir.JStr) else ""


def _filters(root) -> list[dict]:
    fc = root.get("filterConfig") if isinstance(root, pbir.JObj) else None
    lst = fc.get("filters") if isinstance(fc, pbir.JObj) else None
    out = []
    for f in (lst.items if isinstance(lst, pbir.JArr) else []):
        if isinstance(f, pbir.JObj):
            fields = sorted({_label(r) for r in pbir.walk_fields(f.get("field"))})
            out.append({"field": ", ".join(fields) or "?", "type": _s(f.get("type")) or "?",
                        "has_condition": "filter" in f})
    return out


def _report(name: str, rdir: str, model) -> dict:
    ddir = os.path.join(rdir, "definition")

    def load(*parts):
        p = os.path.join(ddir, *parts)
        try:
            return pbir.parse_json(read_text(p)[0])
        except (OSError, UnicodeDecodeError, pbir.JsonError):
            return None

    rep = {"name": name, "pages": [], "filters": [], "bookmarks": []}
    root = load("report.json")
    if root is not None:
        rep["filters"] = _filters(root)
    order = []
    meta = load("pages", "pages.json")
    if meta is not None and isinstance(meta.get("pageOrder"), pbir.JArr):
        order = [_s(x) for x in meta.get("pageOrder").items]
    pdir = os.path.join(ddir, "pages")
    pids = sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []
    pids = [p for p in order if p in pids] + [p for p in pids if p not in order]
    for pid in pids:
        proot = load("pages", pid, "page.json")
        if proot is None:
            continue
        binding = proot.get("pageBinding")
        page = {"name": _s(proot.get("displayName")) or pid, "id": pid,
                "hidden": _s(proot.get("visibility")) == "HiddenInViewMode",
                "type": (_s(binding.get("type")) if isinstance(binding, pbir.JObj) else "") or "page",
                "filters": _filters(proot), "visuals": []}
        vdir = os.path.join(pdir, pid, "visuals")
        for vid in sorted(os.listdir(vdir)) if os.path.isdir(vdir) else []:
            vroot = load("pages", pid, "visuals", vid, "visual.json")
            if vroot is None:
                continue
            v = vroot.get("visual")
            vt = _s(v.get("visualType")) if isinstance(v, pbir.JObj) else ("group" if "visualGroup" in vroot else "?")
            roles: dict = {}
            for f, path in pbir.walk_fields_with_path(v.get("query") if isinstance(v, pbir.JObj) else None, model):
                if "queryState" in path:
                    i = path.index("queryState")
                    lst = roles.setdefault(path[i + 1] if i + 1 < len(path) else "?", [])
                    if _label(f) not in lst:
                        lst.append(_label(f))
            page["visuals"].append({"id": vid, "type": vt, "fields": roles, "filters": _filters(vroot)})
        rep["pages"].append(page)
    bdir = os.path.join(ddir, "bookmarks")
    for f in sorted(os.listdir(bdir)) if os.path.isdir(bdir) else []:
        if f.endswith(".bookmark.json"):
            b = load("bookmarks", f)
            if b is not None:
                rep["bookmarks"].append(_s(b.get("displayName")) or f[:-14])
    return rep


def collect(ws, project, dax: bool = False) -> dict:
    mdef = os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    g = usage.build(ws, project)
    model = g.model
    res = deps.Resolver(model)
    man = mirror.load_manifest(ws, project)
    doc: dict = {"project": project.name, "generated": dt.datetime.now().isoformat(timespec="seconds"),
                 "classification": man.get("classification", "unknown")}
    tables, measures, cgroups, fparams, pq = [], [], [], [], []
    for t in sorted(model.tables.values(), key=lambda x: x.name.lower()):
        if t.private:
            continue
        tn = t.node
        cols = []
        for c in t.columns.values():
            k = usage.key("column", t.name, c.name)
            ub = _used_by(g, k)
            rec = {"name": c.name, "type": c.data_type or "?", "hidden": c.hidden, "calculated": c.calculated,
                   "sort_by": c.sort_by, "format": c.node.prop("formatString") if c.node else "",
                   "description": _desc(model, c.node), "used_by": len(ub["objects"]) + ub["visuals"],
                   "state": g.state.get(k, "")}
            if dax and c.calculated and c.node is not None:
                rec["dax"] = "".join(model.text_of(r) for r in tmdl.node_regions(model.docs[c.node.file], c.node)
                                     if r.kind == "dax").strip()
            cols.append(rec)
        hier = [{"name": h.name, "levels": [lv.prop("column") for lv in h.children if lv.kind == "level"]}
                for h in (tn.children if tn else []) if h.kind == "hierarchy"]
        tables.append({"name": t.name, "storage": sorted({k for _n, k, _e in t.partitions}) or ["none"],
                       "hidden": t.hidden, "calculated": t.calculated, "description": _desc(model, tn),
                       "date_table": bool(tn and tn.props.get("dataCategory", "").strip().lower() == "time"),
                       "state": g.state.get(usage.key("table", "", t.name), ""), "columns": cols,
                       "measures": len(t.measures), "hierarchies": hier})
        for m in t.measures.values():
            k = usage.key("measure", t.name, m.name)
            d = deps.dax(m.expression, t.name, res)
            ub = _used_by(g, k)
            rec = {"name": m.name, "table": t.name, "folder": m.folder, "format": m.format_string,
                   "description": _desc(model, m.node),
                   "depends_on": sorted({f"{r.table}[{r.name}]" if r.kind == "column" else f"[{r.name}]"
                                         for r in d.refs if r.kind in ("column", "measure")}),
                   "used_by_measures": [x for x in ub["objects"] if x.startswith("[")],
                   "used_by_visuals": ub["visuals"], "state": g.state.get(k, "")}
            if dax:
                rec["dax"] = m.expression.strip()
            measures.append(rec)
        if t.calc_items or (tn and any(c.kind == "calculationGroup" for c in tn.children)):
            cg = next((c for c in tn.children if c.kind == "calculationGroup"), None) if tn else None
            items = []
            for name, node in t.calc_items.items():
                it = {"name": name, "format_string_expression": any(c.kind == "formatStringDefinition" for c in node.children)}
                if dax:
                    it["dax"] = "".join(model.text_of(r) for r in tmdl.node_regions(model.docs[node.file], node, deep=False)
                                        if r.kind == "dax").strip()
                items.append(it)
            cgroups.append({"table": t.name, "precedence": cg.prop("precedence") if cg else "", "items": items})
        for _pn, kind, expr in t.partitions:
            if kind == "calculated":
                if "nameof" in expr.lower():
                    refs = deps.dax(expr, t.name, res).refs
                    fparams.append({"table": t.name, "fields": sorted({f"{r.table}[{r.name}]" if r.kind == "column"
                                                                       else f"[{r.name}]" for r in refs
                                                                       if r.kind in ("column", "measure")})})
            else:
                funcs, steps = _m_summary(expr)
                rec = {"object": t.name, "kind": f"partition ({kind})", "functions": funcs, "steps": steps}
                if dax:
                    rec["m"] = expr.strip()
                pq.append(rec)
    for name, node in sorted(model.expressions.items()):
        text = "".join(model.text_of(r) for r in tmdl.node_regions(model.docs[node.file], node, deep=False))
        param = "isparameterquery" in text.replace(" ", "").lower()
        funcs, steps = _m_summary(text)
        rec = {"object": name, "kind": "parameter" if param else "query", "functions": [] if param else funcs,
               "steps": [] if param else steps, "state": g.state.get(usage.key("m_query", "", name), "")}
        if dax and not param:
            rec["m"] = text.strip()
        pq.append(rec)
    functions = []
    for name, node in sorted(model.functions.items()):
        rec = {"name": name, "state": g.state.get(usage.key("function", "", name), "")}
        if dax:
            rec["dax"] = "".join(model.text_of(r) for r in tmdl.node_regions(model.docs[node.file], node, deep=False)
                                 if r.kind == "dax").strip()
        functions.append(rec)
    rels = []
    for r in model.relationships:
        n = r.node
        fc = n.prop("fromCardinality", "many") if n else "many"
        tc = n.prop("toCardinality", "one") if n else "one"
        rels.append({"from": f"{r.from_table}[{r.from_column}]", "to": f"{r.to_table}[{r.to_column}]",
                     "from_table": r.from_table, "to_table": r.to_table, "cardinality": f"{fc}:{tc}",
                     "cross_filter": "both" if r.both else "single", "active": r.active})
    security = []
    for name, node in sorted(model.role_nodes.items()):
        tps = [c for c in node.children if c.kind == "tablePermission"]
        security.append({"role": name, "permission": node.prop("modelPermission", "read"),
                         "filtered_tables": sorted(tp.name for tp in tps),
                         "ols_columns": sum(1 for tp in tps for c in tp.children if c.kind == "columnPermission"),
                         "members": sum(1 for c in node.children if c.kind == "member")})
    reports = [_report(name, rdir, model) for name, rdir in usage.bound_reports(ws, project)]
    summary: dict = {}
    unused = []
    for k, o in g.objects.items():
        if o.private:
            continue
        st = g.state.get(k, "")
        summary.setdefault(o.kind, {}).setdefault(st, 0)
        summary[o.kind][st] += 1
        if st == "unused" and o.kind in ("table", "column", "measure", "hierarchy", "relationship", "m_query", "function"):
            unused.append(f"{o.kind} {o.label()}")
    changelog, skipped = _changelog(ws, project)
    doc["overview"] = {"tables": len(tables), "columns": sum(len(t["columns"]) for t in tables),
                       "measures": len(measures), "relationships": len(rels), "roles": len(security),
                       "calculation_groups": len(cgroups), "functions": len(functions),
                       "queries": sum(1 for p in pq if p["kind"] in ("query", "parameter")),
                       "reports": len(reports), "pages": sum(len(r["pages"]) for r in reports),
                       "visuals": sum(len(p["visuals"]) for r in reports for p in r["pages"]),
                       "bookmarks": sum(len(r["bookmarks"]) for r in reports),
                       "auto_date_tables": sum(1 for t in model.tables.values() if t.private)}
    doc.update({"tables": tables, "measures": measures, "calculation_groups": cgroups, "functions": functions,
                "field_parameters": fparams, "relationships": rels, "power_query": pq, "security": security,
                "reports": reports, "unused": {"analysis": "incomplete" if g.incomplete else "complete",
                                               "scope": g.reports, "summary": summary, "objects": unused[:300]},
                "changelog": changelog, "changelog_skipped": skipped,
                "changelog_status": "INCOMPLETE" if skipped else "COMPLETE"})
    return doc


def _changelog(ws, project) -> tuple[list[dict], int]:
    """Keep well-formed lines for this project. A bad line is counted and never copied into the doc."""
    path = pbz_state_path(ws, "push-log.jsonl")
    if not os.path.lexists(path):
        return [], 0
    try:
        lines = read_checked(path, root=ws.root).decode("utf-8").splitlines(keepends=True)
    except PbzError:
        raise
    except (OSError, UnicodeError):
        return [], 0
    kept, skipped = [], 0
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(entry, dict) or entry.get("project") != project.name:
            if not isinstance(entry, dict):
                skipped += 1
            continue
        stamp, objects = entry.get("stamp"), entry.get("objects")
        if not isinstance(stamp, str) or not isinstance(objects, list) or any(not isinstance(x, str) for x in objects):
            skipped += 1
            continue
        kept.append({"stamp": stamp, "objects": objects[:30]})
    return kept[-20:], skipped


# ---------------------------------------------------------------- rendering


def _cell(v) -> str:
    if isinstance(v, bool):
        return "yes" if v else ""
    if isinstance(v, (list, tuple)):
        v = ", ".join(str(x) for x in v)
    return str(v).replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _md_table(head: list[str], rows: list[list]) -> list[str]:
    if not rows:
        return ["_(none)_", ""]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return out + [""]


def _fence(text: str, lang: str) -> list[str]:
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return [ticks + lang, text, ticks, ""]


def _mermaid_name(s: str) -> str:
    return re.sub(r"\W", "_", s) or "_"


def render_md(d: dict) -> str:
    L = [f"# {d['project']}: model and report documentation", "",
         f"Generated {d['generated']} by `pbz docs` from the sanitized mirror: names, formulas and structure only, "
         f"no data. Classification mode: {d['classification']}.", "", "## Overview", ""]
    L += _md_table(["Item", "Count"], [[k.replace("_", " "), v] for k, v in d["overview"].items()])
    L += ["## Tables", ""]
    for t in d["tables"]:
        flags = [x for x, on in (("hidden", t["hidden"]), ("calculated", t["calculated"]),
                                 ("date table", t["date_table"]), (t["state"], t["state"] not in ("", "used"))) if on]
        L += [f"### {t['name']}", "", f"Storage: {', '.join(t['storage'])}. Measures: {t['measures']}."
              + (f" ({', '.join(flags)})" if flags else ""), ""]
        L += _md_table(["Column", "Type", "Hidden", "Calculated", "Sort by", "Format", "Used by", "State"],
                       [[c["name"], c["type"], c["hidden"], c["calculated"], c["sort_by"], c["format"], c["used_by"],
                         c["state"]] for c in t["columns"]])
        for c in t["columns"]:
            if c.get("dax"):
                L += [f"`{t['name']}[{c['name']}]` =", ""] + _fence(c["dax"], "dax")
        for h in t["hierarchies"]:
            L += [f"- Hierarchy **{h['name']}**: {' > '.join(h['levels'])}"]
        if t["hierarchies"]:
            L.append("")
    L += ["## Measures", ""]
    L += _md_table(["Measure", "Table", "Folder", "Format", "Description", "Depends on", "Used by measures",
                    "Visuals", "State"],
                   [[m["name"], m["table"], m["folder"], m["format"], m["description"], m["depends_on"],
                     m["used_by_measures"], m["used_by_visuals"], m["state"]] for m in d["measures"]])
    for m in d["measures"]:
        if "dax" in m:
            L += [f"### [{m['name']}]", ""] + _fence(m["dax"], "dax")
    L += ["## Calculation groups", ""]
    for cg in d["calculation_groups"]:
        L += [f"### {cg['table']} (precedence {cg['precedence'] or '-'})", ""]
        L += _md_table(["Item", "Format string expression"], [[i["name"], i["format_string_expression"]] for i in cg["items"]])
        for i in cg["items"]:
            if i.get("dax"):
                L += [f"`{i['name']}` =", ""] + _fence(i["dax"], "dax")
    if not d["calculation_groups"]:
        L += ["_(none)_", ""]
    L += ["## Functions", ""]
    L += _md_table(["Function", "State"], [[f["name"], f["state"]] for f in d["functions"]])
    for f in d["functions"]:
        if f.get("dax"):
            L += _fence(f["dax"], "dax")
    L += ["## Field parameters", ""]
    L += _md_table(["Table", "Fields"], [[f["table"], f["fields"]] for f in d["field_parameters"]])
    L += ["## Relationships", ""]
    L += _md_table(["From", "To", "Cardinality", "Cross filter", "Active"],
                   [[r["from"], r["to"], r["cardinality"], r["cross_filter"], r["active"]] for r in d["relationships"]])
    if d["relationships"]:
        ends = {"many": "}o", "one": "||"}
        L += ["```mermaid", "erDiagram"]
        for r in d["relationships"]:
            fc, tc = r["cardinality"].split(":")
            left = ends.get(fc, "}o")
            right = {"many": "o{", "one": "||"}.get(tc, "||")
            L.append(f"    {_mermaid_name(r['from_table'])} {left}--{right} {_mermaid_name(r['to_table'])} : "
                     f"\"{r['from'].split('[', 1)[-1].rstrip(']').replace(chr(34), '')}\"")
        L += ["```", ""]
    L += ["## Power Query", ""]
    L += _md_table(["Object", "Kind", "Functions", "Steps"],
                   [[p["object"], p["kind"], p["functions"], p["steps"]] for p in d["power_query"]])
    for p in d["power_query"]:
        if p.get("m"):
            L += [f"### {p['object']}", ""] + _fence(p["m"], "powerquery")
    L += ["## Security", ""]
    L += _md_table(["Role", "Permission", "Filtered tables", "OLS columns", "Members"],
                   [[s["role"], s["permission"], s["filtered_tables"], s["ols_columns"], s["members"]]
                    for s in d["security"]])
    L += ["## Reports", ""]
    for r in d["reports"]:
        L += [f"### {r['name']}", ""]
        if r["filters"]:
            L += ["Report filters: " + "; ".join(f"{f['field']} ({f['type']})" for f in r["filters"]), ""]
        for p in r["pages"]:
            flags = [x for x, on in (("hidden", p["hidden"]), (p["type"], p["type"] != "page")) if on]
            L += [f"#### {p['name']}" + (f" ({', '.join(flags)})" if flags else ""), ""]
            if p["filters"]:
                L += ["Page filters: " + "; ".join(f"{f['field']} ({f['type']})" for f in p["filters"]), ""]
            L += _md_table(["Visual", "Type", "Fields"],
                           [[v["id"], v["type"], "; ".join(f"{role}: {', '.join(fs)}" for role, fs in v["fields"].items())]
                            for v in p["visuals"]])
        if r["bookmarks"]:
            L += ["Bookmarks: " + ", ".join(r["bookmarks"]), ""]
    if not d["reports"]:
        L += ["_(no report bound to this model by path)_", ""]
    u = d["unused"]
    L += ["## Unused objects", "", f"Analysis {u['analysis']}; scanned reports: {', '.join(u['scope']) or 'none'}. "
          "Unused means unused in the scanned reports (service reports, Excel and composite models are not visible).", ""]
    L += _md_table(["Kind"] + list(usage.STATES),
                   [[k] + [s.get(st, 0) for st in usage.STATES] for k, s in sorted(u["summary"].items())])
    L += [f"- {x}" for x in u["objects"]] + ([""] if u["objects"] else [])
    L += ["## Change log", ""]
    if d.get("changelog_status") == "INCOMPLETE":
        L += [f"INCOMPLETE: {int(d.get('changelog_skipped') or 0)} line(s) skipped.", ""]
    for e in reversed(d["changelog"]):
        L += [f"- {e['stamp']}: " + "; ".join(e["objects"])]
    if not d["changelog"] and d.get("changelog_status") != "INCOMPLETE":
        L += ["_(no pushes recorded)_"]
    return "\n".join(L) + "\n"


def _h(s) -> str:
    return html.escape(str(s), quote=True)


def _html_table(head: list[str], rows: list[list]) -> str:
    if not rows:
        return "<p><em>(none)</em></p>"
    def cell(v):
        if isinstance(v, bool):
            return "yes" if v else ""
        if isinstance(v, (list, tuple)):
            return ", ".join(_h(x) for x in v)
        return _h(v)
    return ("<table><thead><tr>" + "".join(f"<th>{_h(x)}</th>" for x in head) + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f"<td>{cell(c)}</td>" for c in r) + "</tr>" for r in rows) + "</tbody></table>")


def render_html(d: dict) -> str:
    md_sections = []

    def sec(title: str, body: str, open_: bool = False) -> None:
        md_sections.append(f"<details{' open' if open_ else ''}><summary>{_h(title)}</summary>{body}</details>")

    sec("Overview", _html_table(["Item", "Count"], [[k.replace("_", " "), v] for k, v in d["overview"].items()]), True)
    parts = []
    for t in d["tables"]:
        body = (f"<p>Storage: {_h(', '.join(t['storage']))}. Measures: {t['measures']}."
                f"{' Hidden.' if t['hidden'] else ''}{' Calculated.' if t['calculated'] else ''}"
                f"{' Date table.' if t['date_table'] else ''}{' State: ' + _h(t['state']) + '.' if t['state'] not in ('', 'used') else ''}</p>")
        body += _html_table(["Column", "Type", "Hidden", "Calculated", "Sort by", "Format", "Used by", "State"],
                            [[c["name"], c["type"], c["hidden"], c["calculated"], c["sort_by"], c["format"],
                              c["used_by"], c["state"]] for c in t["columns"]])
        for c in t["columns"]:
            if c.get("dax"):
                body += f"<p>{_h(t['name'])}[{_h(c['name'])}] =</p><pre>{_h(c['dax'])}</pre>"
        if t["hierarchies"]:
            body += "<ul>" + "".join(f"<li>Hierarchy <b>{_h(h['name'])}</b>: {_h(' > '.join(h['levels']))}</li>"
                                     for h in t["hierarchies"]) + "</ul>"
        parts.append(f"<details><summary>{_h(t['name'])}</summary>{body}</details>")
    sec(f"Tables ({len(d['tables'])})", "".join(parts))
    body = _html_table(["Measure", "Table", "Folder", "Format", "Description", "Depends on", "Used by measures",
                        "Visuals", "State"],
                       [[m["name"], m["table"], m["folder"], m["format"], m["description"], m["depends_on"],
                         m["used_by_measures"], m["used_by_visuals"], m["state"]] for m in d["measures"]])
    body += "".join(f"<details><summary>[{_h(m['name'])}]</summary><pre>{_h(m['dax'])}</pre></details>"
                    for m in d["measures"] if "dax" in m)
    sec(f"Measures ({len(d['measures'])})", body)
    body = ""
    for cg in d["calculation_groups"]:
        body += f"<h4>{_h(cg['table'])} (precedence {_h(cg['precedence'] or '-')})</h4>"
        body += _html_table(["Item", "Format string expression"], [[i["name"], i["format_string_expression"]] for i in cg["items"]])
        body += "".join(f"<pre>{_h(i['dax'])}</pre>" for i in cg["items"] if i.get("dax"))
    sec("Calculation groups", body or "<p><em>(none)</em></p>")
    sec("Functions", _html_table(["Function", "State"], [[f["name"], f["state"]] for f in d["functions"]])
        + "".join(f"<pre>{_h(f['dax'])}</pre>" for f in d["functions"] if f.get("dax")))
    sec("Field parameters", _html_table(["Table", "Fields"], [[f["table"], f["fields"]] for f in d["field_parameters"]]))
    sec("Relationships", _html_table(["From", "To", "Cardinality", "Cross filter", "Active"],
                                     [[r["from"], r["to"], r["cardinality"], r["cross_filter"], r["active"]]
                                      for r in d["relationships"]]))
    sec("Power Query", _html_table(["Object", "Kind", "Functions", "Steps"],
                                   [[p["object"], p["kind"], p["functions"], p["steps"]] for p in d["power_query"]])
        + "".join(f"<details><summary>{_h(p['object'])}</summary><pre>{_h(p['m'])}</pre></details>"
                  for p in d["power_query"] if p.get("m")))
    sec("Security", _html_table(["Role", "Permission", "Filtered tables", "OLS columns", "Members"],
                                [[s["role"], s["permission"], s["filtered_tables"], s["ols_columns"], s["members"]]
                                 for s in d["security"]]))
    body = ""
    for r in d["reports"]:
        rb = ""
        if r["filters"]:
            rb += "<p>Report filters: " + _h("; ".join(f"{f['field']} ({f['type']})" for f in r["filters"])) + "</p>"
        for p in r["pages"]:
            flags = [x for x, on in (("hidden", p["hidden"]), (p["type"], p["type"] != "page")) if on]
            pb = ""
            if p["filters"]:
                pb += "<p>Page filters: " + _h("; ".join(f"{f['field']} ({f['type']})" for f in p["filters"])) + "</p>"
            pb += _html_table(["Visual", "Type", "Fields"],
                              [[v["id"], v["type"], "; ".join(f"{role}: {', '.join(fs)}" for role, fs in v["fields"].items())]
                               for v in p["visuals"]])
            rb += f"<details><summary>{_h(p['name'])}{_h(' (' + ', '.join(flags) + ')') if flags else ''}</summary>{pb}</details>"
        if r["bookmarks"]:
            rb += "<p>Bookmarks: " + _h(", ".join(r["bookmarks"])) + "</p>"
        body += f"<h4>{_h(r['name'])}</h4>{rb}"
    sec("Reports", body or "<p><em>(no report bound to this model by path)</em></p>")
    u = d["unused"]
    body = (f"<p>Analysis {_h(u['analysis'])}; scanned reports: {_h(', '.join(u['scope']) or 'none')}. Unused means "
            "unused in the scanned reports.</p>"
            + _html_table(["Kind"] + list(usage.STATES), [[k] + [s.get(st, 0) for st in usage.STATES]
                                                         for k, s in sorted(u["summary"].items())])
            + ("<ul>" + "".join(f"<li>{_h(x)}</li>" for x in u["objects"]) + "</ul>" if u["objects"] else ""))
    sec("Unused objects", body)
    note = ""
    if d.get("changelog_status") == "INCOMPLETE":
        note = f"<p>INCOMPLETE: {_h(int(d.get('changelog_skipped') or 0))} line(s) skipped.</p>"
    body = note + ("<ul>" + "".join(f"<li>{_h(e['stamp'])}: {_h('; '.join(e['objects']))}</li>"
                                    for e in reversed(d["changelog"])) + "</ul>" if d["changelog"]
                   else "<p><em>(no pushes recorded)</em></p>")
    sec("Change log", body)
    css = ("body{font:14px/1.45 system-ui,sans-serif;margin:24px;max-width:1200px;color:#1b1f24;background:#fff}"
           "summary{cursor:pointer;font-weight:600;margin:6px 0}details details{margin-left:16px}"
           "table{border-collapse:collapse;margin:8px 0}th,td{border:1px solid #d0d7de;padding:3px 8px;text-align:left;"
           "vertical-align:top}th{background:#f6f8fa}pre{background:#f6f8fa;padding:8px;overflow:auto}"
           "@media (prefers-color-scheme:dark){body{background:#0d1117;color:#e6edf3}th,pre{background:#161b22}"
           "th,td{border-color:#30363d}}")
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>{_h(d['project'])} docs</title>"
            f"<style>{css}</style></head><body><h1>{_h(d['project'])}: model and report documentation</h1>"
            f"<p>Generated {_h(d['generated'])} by pbz docs from the sanitized mirror: names, formulas and structure "
            f"only, no data. Classification mode: {_h(d['classification'])}.</p>"
            + "".join(md_sections) + "</body></html>\n")


def run(ws, project, fmt: str = "md", dax: bool = False) -> dict:
    anchor_mirror(ws)
    chosen = fmt if fmt in ("json", "html") else "md"
    rel = f"docs/{project.name}.{chosen}"
    path = os.path.join(ws.mirror, *rel.split("/"))
    preflight_regular(path, root=ws.mirror)
    d = collect(ws, project, dax)
    if chosen == "json":
        text = json.dumps(d, indent=1, ensure_ascii=False) + "\n"
    elif chosen == "html":
        text = render_html(d)
    else:
        text = render_md(d)
    write_text(path, text, root=ws.mirror)
    return {"verb": "docs", "project": project.name, "file": f"pbz-workspace/{rel}", "format": chosen,
            "dax": dax, "sections": SECTIONS, "counts": d["overview"]}
