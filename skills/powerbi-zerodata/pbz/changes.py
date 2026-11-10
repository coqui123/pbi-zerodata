"""`pbz changes`: semantic diff between two sanitized mirror snapshots, or a snapshot and the current
mirror, by object. Names, statuses and counts only: literal changes are counted, never shown.

Model objects are matched by lineageTag, else by kind + table + name (relationships by endpoints,
partitions by table + ordinal), else by identical body (a rename). Report objects are matched by
page / visual / bookmark / filter name. Expression changes are classed format_only, comments_only,
literal_only or logic, with references added / removed. Impact lists what depends on a changed
object in the current mirror's usage graph. Snapshots are taken on every `pbz pull` and `pbz push`.
"""
from __future__ import annotations

import difflib
import json
import os
from dataclasses import dataclass, field

from . import deps, lexers, mirror, pbir, tmdl, usage
from .common import PbzError, find_workspace, sha

OBJECT_KINDS = {"model", "database", "table", "column", "measure", "hierarchy", "partition", "relationship", "role",
                "tablePermission", "expression", "function", "calculationItem", "perspective", "dataSource",
                "queryGroup", "calendar"}
IGNORE_PROPS = {"lineageTag", "sourceLineageTag"}
IGNORE_NODES = {"changedProperty"}
# property values worth showing: closed vocabularies and metadata names, never data
SHOW_PROPS = {"dataType", "isHidden", "summarizeBy", "dataCategory", "crossFilteringBehavior", "isActive", "mode",
              "fromCardinality", "toCardinality", "isKey", "isAvailableInMdx", "displayFolder", "formatString",
              "sortByColumn", "modelPermission", "fromColumn", "toColumn", "sourceColumn", "column",
              "securityFilteringBehavior", "isPrivate", "showAsVariationsOnly", "precedence", "compatibilityLevel"}
LIT = ("str", "num", "pbztok")
GRAPH_KIND = {"calculationItem": "calc_item", "expression": "m_query"}


@dataclass
class Rec:
    kind: str
    table: str
    name: str
    file: str
    lt: str
    pkey: tuple
    props: dict = field(default_factory=dict)
    exprs: dict = field(default_factory=dict)   # part -> (lang, text)
    desc: str = ""
    ann: dict = field(default_factory=dict)     # annotation name -> value hash
    members: int = 0

    def body(self) -> str:
        return sha(json.dumps([self.kind, self.props, sorted(self.exprs.items()), self.ann], sort_keys=True))


def _table_of(node) -> str:
    n = node.parent
    while n is not None and n.kind != "table":
        n = n.parent
    return n.name if n is not None else ""


def _owned(node) -> list:
    out = [node]
    for c in node.children:
        if c.kind not in OBJECT_KINDS and c.kind not in IGNORE_NODES:
            out += _owned(c)
    return out


def _model_side(texts: dict, project) -> tuple[list[Rec], object]:
    prefix = f"{project.name}.SemanticModel/definition/"
    docs = {rel[len(prefix):]: t for rel, t in texts.items() if rel.startswith(prefix) and rel.endswith(".tmdl")}
    model = tmdl.load_model("", texts=docs)
    recs: list[Rec] = []
    ordinal: dict[str, int] = {}
    for relp, doc in sorted(model.docs.items()):
        model_rec = None
        for node in doc.nodes:
            if node.kind == "annotation" and node.parent is None and model_rec is not None:
                if not node.name.startswith("PBI_"):
                    reg = next((r for r in doc.regions if r.node is node), None)
                    model_rec.ann[node.name] = sha(doc.text[reg.start:reg.end]) if reg else ""
                continue
            if node.kind not in OBJECT_KINDS:
                continue
            table = node.name if node.kind == "table" else _table_of(node)
            if node.kind == "relationship":
                pkey = ("relationship", node.props.get("fromColumn", "").strip().lower(),
                        node.props.get("toColumn", "").strip().lower())
            elif node.kind == "partition":
                ordinal[table] = ordinal.get(table, -1) + 1
                pkey = ("partition", table.lower(), ordinal[table])
            elif node.kind == "tablePermission":
                pkey = ("tablePermission", node.parent.name.lower() if node.parent else "", node.name.lower())
            elif node.kind in ("model", "database"):
                pkey = (node.kind,)
            else:
                pkey = (node.kind, table.lower() if node.kind != "table" else "", node.name.lower())
            r = Rec(node.kind, table if node.kind != "table" else "", node.name, relp,
                    node.props.get("lineageTag", "").strip(), pkey)
            owned = _owned(node)
            ids = {id(x) for x in owned}
            for x in owned:
                pre = "" if x is node else f"{x.kind} {x.name}.".replace(" .", ".")
                if x.kind in ("annotation", "extendedProperty"):
                    if not x.name.startswith("PBI_"):
                        reg = next((g for g in doc.regions if g.node is x), None)
                        r.ann[x.name] = sha(doc.text[reg.start:reg.end]) if reg else ""
                    continue
                for p, v in x.props.items():
                    if p not in IGNORE_PROPS:
                        r.props[pre + p] = v.strip()
            for reg in doc.regions:
                if id(reg.node) not in ids or reg.node.kind in ("annotation", "extendedProperty"):
                    continue
                if reg.kind in ("dax", "m"):
                    part = reg.prop if reg.node is node else f"{reg.node.kind}.{reg.prop}"
                    r.exprs[part] = (reg.kind, doc.text[reg.start:reg.end])
                elif reg.kind == "text" and reg.node is node:
                    r.desc += doc.text[reg.start:reg.end]
                elif reg.kind == "member":
                    r.members += 1
            if node.kind == "model":
                model_rec = r
            recs.append(r)
    return recs, model


def _ref_label(ref: deps.Ref) -> str:
    if ref.kind == "column":
        return f"{ref.table}[{ref.name}]"
    if ref.kind == "measure":
        return f"[{ref.name}]"
    if ref.kind == "table":
        return f"table {ref.name}"
    if ref.kind == "function":
        return f"{ref.name}()"
    if ref.kind == "m_query":
        return f"query {ref.name}"
    if ref.kind == "unresolved":
        return (f"{ref.table}[{ref.name}]" if ref.table else f"[{ref.name}]") + " (unresolved)"
    return ""


def _refs(lang: str, text: str, home: str, res) -> set[str]:
    refs = deps.dax(text, home, res).refs if lang == "dax" else deps.m(text, res)
    return {x for x in map(_ref_label, refs) if x}


def expr_change(a: str, b: str, lang: str) -> dict | None:
    """Class of change between two expression texts; literal changes are counted, never shown."""
    if a == b:
        return None
    if " ".join(a.split()) == " ".join(b.split()):
        return {"class": "format_only"}
    lex = lexers.lex_dax if lang == "dax" else lexers.lex_m
    ta, tb = lexers.significant(lex(a)), lexers.significant(lex(b))
    if [t.text for t in ta] == [t.text for t in tb]:
        return {"class": "comments_only"}
    if len(ta) == len(tb) and all(x.kind == y.kind and (x.kind in LIT or x.text == y.text) for x, y in zip(ta, tb)):
        return {"class": "literal_only", "literals_changed": sum(x.text != y.text for x, y in zip(ta, tb))}
    wa = ["\0LIT" if t.kind in LIT else t.text for t in ta]
    wb = ["\0LIT" if t.kind in LIT else t.text for t in tb]
    changed = 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes():
        if op == "equal":
            changed += sum(x.text != y.text for x, y in zip(ta[i1:i2], tb[j1:j2]) if x.kind in LIT)
        else:
            changed += max(sum(t.kind in LIT for t in ta[i1:i2]), sum(t.kind in LIT for t in tb[j1:j2]))
    out = {"class": "logic"}
    if changed:
        out["literals_changed"] = changed
    return out


def _match(a: list, b: list, keyfns) -> tuple[list, list, list]:
    """Pair records by each key function in turn; returns (pairs, removed, added)."""
    pairs, left, right = [], list(a), list(b)
    for fn in keyfns:
        idx: dict = {}
        for r in right:
            k = fn(r)
            if k is not None:
                idx.setdefault(k, []).append(r)
        nl = []
        for r in left:
            k = fn(r)
            cand = idx.get(k) if k is not None else None
            if cand:
                other = cand.pop(0)
                pairs.append((r, other))
                right.remove(other)
            else:
                nl.append(r)
        left = nl
    return pairs, left, right


def _compare_model(a: list, b: list, ares, bres) -> list[dict]:
    pairs, removed, added = _match(a, b, [
        lambda r: ("lt", r.kind, r.lt) if r.lt else None,
        lambda r: r.pkey,
        lambda r: ("body", r.kind, r.table.lower(), r.body()) if r.exprs or r.props else None,
    ])
    out = []
    for r in removed:
        out.append({"status": "removed", "kind": r.kind, "table": r.table, "name": r.name, "file": r.file})
    for r in added:
        out.append({"status": "added", "kind": r.kind, "table": r.table, "name": r.name, "file": r.file})
    for x, y in pairs:
        rec = {"status": "modified", "kind": y.kind, "table": y.table, "name": y.name, "file": y.file}
        if x.name != y.name:
            rec["status"] = "renamed"
            rec["renamed_from"] = x.name
        if x.table.lower() != y.table.lower() and y.kind in ("column", "measure", "hierarchy"):
            rec["status"] = "moved" if rec["status"] == "modified" else rec["status"]
            rec["moved_from_table"] = x.table
        parts = []
        for part in sorted(set(x.exprs) | set(y.exprs)):
            if part not in x.exprs or part not in y.exprs:
                parts.append({"part": part, "class": "added" if part in y.exprs else "removed"})
                continue
            (la, ta), (lb, tb) = x.exprs[part], y.exprs[part]
            ch = expr_change(ta, tb, lb)
            if ch is None:
                continue
            if ch["class"] in ("logic", "literal_only"):
                ra, rb = _refs(la, ta, x.table, ares), _refs(lb, tb, y.table, bres)
                if rb - ra:
                    ch["refs_added"] = sorted(rb - ra)
                if ra - rb:
                    ch["refs_removed"] = sorted(ra - rb)
            parts.append({"part": part, **ch})
        if parts:
            rec["expressions"] = parts
        props = []
        for p in sorted(set(x.props) | set(y.props)):
            va, vb = x.props.get(p), y.props.get(p)
            if va == vb:
                continue
            base = p.rsplit(".", 1)[-1]
            item = {"prop": p}
            if base in SHOW_PROPS:
                item["from"], item["to"] = va, vb
            else:
                item["change"] = "added" if va is None else ("removed" if vb is None else "changed")
            props.append(item)
        if props:
            rec["props"] = props
        if x.desc != y.desc:
            rec["description"] = "added" if not x.desc else ("removed" if not y.desc else "changed")
        anns = sorted(k for k in set(x.ann) | set(y.ann) if x.ann.get(k) != y.ann.get(k))
        if anns:
            rec["annotations"] = anns
        if x.members != y.members:
            rec["members"] = f"{x.members} -> {y.members}"
        if rec["status"] != "modified" or len(rec) > 5:
            out.append(rec)
    return out


# ---------------------------------------------------------------- report side


def _label(f: pbir.FieldRef) -> str:
    if f.kind == "measure":
        return f"[{f.name}]"
    if f.kind == "hierarchy":
        return f"{f.table}.{f.hierarchy or f.name}.{f.level}"
    return f"{f.table}[{f.name}]"


def _hash(node) -> str:
    return sha(json.dumps(pbir.to_py(node), sort_keys=True)) if node is not None else ""


def _filters(root) -> dict:
    fc = root.get("filterConfig") if isinstance(root, pbir.JObj) else None
    lst = fc.get("filters") if isinstance(fc, pbir.JObj) else None
    out = {}
    for i, f in enumerate(lst.items if isinstance(lst, pbir.JArr) else []):
        if not isinstance(f, pbir.JObj):
            continue
        name = f.get("name").value if isinstance(f.get("name"), pbir.JStr) else f"#{i}"
        fields = sorted({_label(r) for r in pbir.walk_fields(f.get("field"))})
        ftype = f.get("type").value if isinstance(f.get("type"), pbir.JStr) else ""
        out[name] = {"fields": fields, "type": ftype, "condition": _hash(f.get("filter")),
                     "other": _hash(pbir.JObj([p for p in f.pairs if p[0].value not in ("field", "type", "filter")], 0, 0))}
    return out


def _s(node) -> str:
    return node.value if isinstance(node, pbir.JStr) else ""


def _report_side(texts: dict) -> dict:
    idx: dict = {}
    for rel, text in texts.items():
        top, _, sub = rel.partition("/")
        if not top.lower().endswith(".report"):
            continue
        rname = top[:-7]
        if not sub.startswith("definition/"):
            idx[("file", rname, sub)] = {"kind": "file", "report": rname, "id": sub, "hash": sha(text)}
            continue
        sub = sub[len("definition/"):]
        try:
            root = pbir.parse_json(text)
        except pbir.JsonError:
            idx[("file", rname, sub)] = {"kind": "file", "report": rname, "id": sub, "hash": sha(text)}
            continue
        fk = usage._file_kind(sub)
        parts = sub.split("/")
        if fk == "page":
            pid = parts[1]
            binding = root.get("pageBinding")
            idx[("page", rname, pid)] = {
                "kind": "page", "report": rname, "id": pid, "name": _s(root.get("displayName")) or pid,
                "aspects": {"visibility": _s(root.get("visibility")), "type": _hash(binding),
                            "size": _hash(pbir.JObj([p for p in root.pairs if p[0].value in ("width", "height", "displayOption")], 0, 0)),
                            "format": _hash(root.get("objects"))},
                "hash": sha(text)}
            for fname, f in _filters(root).items():
                idx[("filter", rname, pid, fname)] = {"kind": "filter", "report": rname, "scope": "page", "page_id": pid,
                                                      "id": fname, **f}
        elif fk == "visual":
            pid, vid = parts[1], parts[3]
            v = root.get("visual")
            vt = _s(v.get("visualType")) if isinstance(v, pbir.JObj) else ("group" if "visualGroup" in root else "")
            roles: dict = {}
            for f, path in pbir.walk_fields_with_path(v.get("query") if isinstance(v, pbir.JObj) else None):
                if "queryState" in path:
                    i = path.index("queryState")
                    roles.setdefault(path[i + 1] if i + 1 < len(path) else "?", set()).add(_label(f))
            sort = []
            q = v.get("query") if isinstance(v, pbir.JObj) else None
            sd = q.get("sortDefinition") if isinstance(q, pbir.JObj) else None
            for f in pbir.walk_fields(sd):
                sort.append(_label(f))
            fmt = [_hash(v.get(k)) if isinstance(v, pbir.JObj) else "" for k in ("objects", "visualContainerObjects")]
            fmt.append(_hash(root.get("visualContainerObjects")))
            rest = pbir.JObj([p for p in root.pairs if p[0].value not in ("position", "visual", "filterConfig", "isHidden")], 0, 0)
            idx[("visual", rname, pid, vid)] = {
                "kind": "visual", "report": rname, "page_id": pid, "id": vid, "type": vt,
                "fields": {k: sorted(s) for k, s in roles.items()},
                "aspects": {"type": vt, "sort": sha(json.dumps(sort)), "format": sha("".join(fmt)),
                            "visibility": _hash(root.get("isHidden")), "other": _hash(rest),
                            "query_other": _hash(q) if not roles and not sort else ""},
                "position": _hash(root.get("position")), "hash": sha(text)}
            for fname, f in _filters(root).items():
                idx[("filter", rname, f"{pid}/{vid}", fname)] = {"kind": "filter", "report": rname, "scope": "visual",
                                                                 "page_id": pid, "visual": vid, "id": fname, **f}
        elif fk == "mobile":
            idx[("mobile", rname, parts[1], parts[3])] = {"kind": "mobile layout", "report": rname, "page_id": parts[1],
                                                          "id": parts[3], "hash": sha(text)}
        elif fk == "bookmark":
            bid = _s(root.get("name")) or parts[-1]
            idx[("bookmark", rname, bid)] = {"kind": "bookmark", "report": rname, "id": bid,
                                             "name": _s(root.get("displayName")),
                                             "aspects": {"state": _hash(root.get("explorationState")),
                                                         "options": _hash(root.get("options"))}, "hash": sha(text)}
        elif sub == "pages/pages.json":
            order = root.get("pageOrder")
            idx[("page_order", rname)] = {"kind": "page order", "report": rname, "id": "pages.json",
                                          "hash": sha(json.dumps([_s(x) for x in (order.items if isinstance(order, pbir.JArr) else [])]))}
        elif fk == "report":
            idx[("report", rname)] = {"kind": "report", "report": rname, "id": "report.json",
                                      "aspects": {"theme": _hash(root.get("themeCollection")),
                                                  "settings": _hash(root.get("settings")),
                                                  "custom_visuals": _hash(root.get("publicCustomVisuals")),
                                                  "resources": _hash(root.get("resourcePackages")),
                                                  "objects": _hash(root.get("objects"))},
                                      "hash": sha(text)}
            for fname, f in _filters(root).items():
                idx[("filter", rname, "report", fname)] = {"kind": "filter", "report": rname, "scope": "report",
                                                           "id": fname, **f}
        elif fk == "extensions":
            for ent in (root.get("entities").items if isinstance(root.get("entities"), pbir.JArr) else []):
                if not isinstance(ent, pbir.JObj):
                    continue
                for mm in (ent.get("measures").items if isinstance(ent.get("measures"), pbir.JArr) else []):
                    if isinstance(mm, pbir.JObj) and _s(mm.get("name")):
                        idx[("report_measure", rname, _s(ent.get("name")), _s(mm.get("name")))] = {
                            "kind": "report measure", "report": rname, "id": f"[{_s(mm.get('name'))}]",
                            "table": _s(ent.get("name")), "expression": _s(mm.get("expression")),
                            "hash": _hash(mm)}
        else:
            idx[("file", rname, sub)] = {"kind": "file", "report": rname, "id": sub, "hash": sha(text)}
    return idx


def _compare_report(a: dict, b: dict) -> list[dict]:
    pages_a = {(v["report"], v["id"]): v["name"] for k, v in a.items() if k[0] == "page"}
    pages_b = {(v["report"], v["id"]): v["name"] for k, v in b.items() if k[0] == "page"}

    def page(v):
        pid = v.get("page_id")
        return pages_b.get((v["report"], pid)) or pages_a.get((v["report"], pid)) or pid

    out = []
    for k in sorted(set(a) | set(b), key=lambda x: [str(p) for p in x]):
        x, y = a.get(k), b.get(k)
        v = y or x
        rec = {"status": "", "kind": v["kind"], "report": v["report"]}
        if v.get("page_id") and v["kind"] != "page":
            rec["page"] = page(v)
        rec["id"] = v.get("name") if v["kind"] == "page" else v["id"]
        if x is None or y is None:
            rec["status"] = "added" if x is None else "removed"
            if v["kind"] == "visual":
                rec["type"] = v["type"]
            if v["kind"] == "filter":
                rec["fields"] = v["fields"]
            out.append(rec)
            continue
        if x["hash"] == y["hash"] if "hash" in x else x == y:
            continue
        if v["kind"] == "page" and x["name"] != y["name"]:
            rec["status"] = "renamed"
            rec["renamed_from"] = x["name"]
        aspects = sorted(n for n in set(x.get("aspects", {})) | set(y.get("aspects", {}))
                         if x.get("aspects", {}).get(n) != y.get("aspects", {}).get(n))
        if v["kind"] == "visual":
            added = {r: sorted(set(y["fields"].get(r, [])) - set(x["fields"].get(r, []))) for r in y["fields"]}
            removed = {r: sorted(set(x["fields"].get(r, [])) - set(y["fields"].get(r, []))) for r in x["fields"]}
            added = {r: f for r, f in added.items() if f}
            removed = {r: f for r, f in removed.items() if f}
            if added:
                rec["fields_added"] = added
            if removed:
                rec["fields_removed"] = removed
            if added or removed:
                aspects.append("fields")
            moved = x["position"] != y["position"]
            if moved:
                aspects.append("position")
            if aspects == ["position"]:
                rec["status"] = "moved"
            aspects = [s for s in aspects if s != "query_other" or not (added or removed)]
        if v["kind"] == "filter":
            for n in ("fields", "type", "condition", "other"):
                if x.get(n) != y.get(n):
                    aspects.append({"condition": "condition (values not shown)"}.get(n, n))
        if v["kind"] == "report measure":
            ch = expr_change(x["expression"], y["expression"], "dax")
            if ch:
                rec["expression"] = ch
        if v["kind"] == "bookmark" and x.get("name") != y.get("name"):
            rec["status"] = "renamed"
            rec["renamed_from"] = x.get("name")
        rec["status"] = rec["status"] or "modified"
        if aspects:
            rec["aspects"] = sorted(set(aspects))
        out.append(rec)
    return out


# ---------------------------------------------------------------- entry points


def list_snapshots(ws, project) -> dict:
    return {"verb": "changes", "project": project.name, "snapshots": mirror.history_list(ws, project)}


def _side(ws, project, ref: str, hist: list) -> tuple[str, dict]:
    if ref == "mirror":
        return "mirror", mirror.mirror_texts(ws, project)
    if not hist:
        raise PbzError("NO_HISTORY", "no snapshots yet: run `pbz pull`")
    if ref in ("last", "latest"):
        stamp = hist[-1]["stamp"]
    elif ref == "previous":
        if len(hist) < 2:
            raise PbzError("NO_HISTORY", "only one snapshot so far")
        stamp = hist[-2]["stamp"]
    else:
        stamp = ref
    return stamp, mirror.history_texts(ws, project, stamp)


def _impact(g, rec: dict) -> dict | None:
    kind = GRAPH_KIND.get(rec["kind"], rec["kind"])
    k = usage.key(kind, rec["table"], rec["name"])
    if k not in g.objects:
        return None
    seen, todo = set(), [k]
    while todo:
        x = todo.pop()
        for e in g.inc.get(x, []):
            if e.via in ("home", "calc_item") or e.src in seen or e.src == k:
                continue
            seen.add(e.src)
            todo.append(e.src)
    visuals = {(u.report, u.page, u.visual) for x in seen | {k} for u in g.usages.get(x, [])}
    if not seen and not visuals:
        return None
    out = {"visuals": len(visuals)}
    if seen:
        out["objects"] = sorted(g.label(x) for x in seen)[:10]
        out["objects_total"] = len(seen)
    return out


def run(ws, project, from_: str = "last", to: str = "mirror") -> dict:
    hist = mirror.history_list(ws, project)
    a_name, a = _side(ws, project, from_, hist)
    b_name, b = _side(ws, project, to, hist)
    files = {"changed": sorted(r for r in set(a) & set(b) if a[r] != b[r]),
             "added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b))}
    out = {"verb": "changes", "project": project.name, "from": a_name, "to": b_name}
    if not any(files.values()):
        return {**out, "result": "NO_CHANGES"}
    ra, ma = _model_side(a, project)
    rb, mb = _model_side(b, project)
    model = _compare_model(ra, rb, deps.Resolver(ma), deps.Resolver(mb))
    report = _compare_report(_report_side(a), _report_side(b))
    if model and b_name == "mirror":
        try:
            g = usage.build(ws, project)
        except PbzError:
            g = None
        for rec in model if g is not None else []:
            if rec["status"] != "removed" and rec["kind"] in ("measure", "column", "table", "hierarchy",
                                                              "calculationItem", "expression", "function"):
                imp = _impact(g, rec)
                if imp:
                    rec["impact"] = imp
    summary: dict = {}
    for rec in model + report:
        summary[rec["status"]] = summary.get(rec["status"], 0) + 1
    return {**out, "result": "CHANGED", "summary": summary, "model": model, "report": report, "files": files}


def _raw_semantic_model(path: str) -> bool:
    cur = os.path.abspath(path)
    for _ in range(12):
        if os.path.basename(cur).lower().endswith(".semanticmodel"):
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent
    return False


def compare_against(ws, project, other: str) -> dict:
    """Semantic diff of this project's mirror against another workspace's mirror.

    `other` is a workspace root (or any path inside it). A raw *.SemanticModel path is refused.
    """
    if not isinstance(other, str) or not other.strip():
        raise PbzError("BAD_REQUEST", "name another workspace")
    if _raw_semantic_model(other):
        raise PbzError("REFUSED", "compare a workspace, not a raw semantic model")
    other_ws = find_workspace(other)
    other_project = other_ws.project()
    if not os.path.isdir(os.path.join(project.mirror_model, "definition")) or not os.path.isdir(
            os.path.join(other_project.mirror_model, "definition")):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    left, left_model = _model_side(mirror.mirror_texts(ws, project), project)
    right, right_model = _model_side(mirror.mirror_texts(other_ws, other_project), other_project)
    model = _compare_model(left, right, deps.Resolver(left_model), deps.Resolver(right_model))
    out = {"verb": "changes", "action": "against", "project": project.name, "against": other_project.name}
    if not model:
        return {**out, "result": "NO_CHANGES", "model": []}
    summary: dict = {}
    for rec in model:
        summary[rec["status"]] = summary.get(rec["status"], 0) + 1
    return {**out, "result": "CHANGED", "summary": summary, "model": model}
