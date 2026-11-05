"""`pbz add page|visual`: create report pages and visuals in the mirror from built-in templates, bound to
model fields by name. New objects carry no filters, selections or value-specific formatting. The $schema
versions follow the report's existing files. Then `pbz layout`, `pbz validate`, `pbz push`."""
from __future__ import annotations

import collections
import json
import os
import re
import secrets

from . import pbir, tmdl
from .common import PbzError, read_text, write_text
from .tokens import TOKEN_RE

SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"
CHARTS = {"clusteredBarChart", "clusteredColumnChart", "barChart", "columnChart", "lineChart", "areaChart",
          "lineClusteredColumnComboChart", "pieChart", "donutChart", "scatterChart", "waterfallChart", "funnel"}
ROLES = {  # visual type -> (roles in order, role for a column, role for a measure)
    "card": (["Values"], "Values", "Values"),
    "multiRowCard": (["Values"], "Values", "Values"),
    "tableEx": (["Values"], "Values", "Values"),
    "pivotTable": (["Rows", "Columns", "Values"], "Rows", "Values"),
    "slicer": (["Values"], "Values", None),
    "textbox": ([], None, None),
}
for _t in CHARTS:
    ROLES[_t] = (["Category", "Y", "Series"], "Category", "Y")
SIZES = {"card": (220, 120), "multiRowCard": (300, 160), "tableEx": (560, 320), "pivotTable": (560, 320),
         "slicer": (220, 160), "textbox": (320, 60)}
NUMERIC = {"int64", "double", "decimal", "currency"}


def _id() -> str:
    return secrets.token_hex(10)


def _versions(rdir: str) -> dict:
    """Most common $schema version per definition kind in this report (page, visualContainer)."""
    seen: dict = collections.defaultdict(collections.Counter)
    for path in pbir.report_files(rdir):
        try:
            sch = json.loads(read_text(path)[0]).get("$schema", "")
        except (OSError, ValueError, AttributeError):
            continue
        m = re.search(r"/definition/([A-Za-z]+)/(\d+\.\d+\.\d+)/schema\.json$", sch or "")
        if m:
            seen[m.group(1)][m.group(2)] += 1
    return {k: c.most_common(1)[0][0] for k, c in seen.items()}


def _pages(rdir: str):
    pdir = os.path.join(rdir, "definition", "pages")
    meta_p = os.path.join(pdir, "pages.json")
    meta = json.loads(read_text(meta_p)[0]) if os.path.exists(meta_p) else {"pageOrder": []}
    out = {}
    for pid in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
        pj = os.path.join(pdir, pid, "page.json")
        if os.path.isfile(pj):
            out[pid] = json.loads(read_text(pj)[0])
    return pdir, meta_p, meta, out


def _find_page(pages: dict, page: str) -> str:
    for pid, pj in pages.items():
        if page.lower() in (pid.lower(), str(pj.get("displayName", "")).lower()):
            return pid
    raise PbzError("NOT_FOUND", f"page {page}")


def add_page(ws, project, name: str, width: int = 1280, height: int = 720, hidden: bool = False,
             after: str | None = None) -> dict:
    if TOKEN_RE.search(name) or not name.strip():
        raise PbzError("BAD_REQUEST", "a page needs a plain display name")
    rdir = project.mirror_report
    pdir, meta_p, meta, pages = _pages(rdir)
    if not os.path.isdir(pdir):
        raise PbzError("NO_MIRROR", "run `pbz pull` first (no report pages in the mirror)")
    if any(str(p.get("displayName", "")).lower() == name.lower() for p in pages.values()):
        raise PbzError("NAME_TAKEN", f"page {name}")
    ver = _versions(rdir).get("page", "2.1.0")
    pid = _id()
    page = {"$schema": f"{SCHEMA}/page/{ver}/schema.json", "name": pid, "displayName": name,
            "displayOption": "FitToPage", "height": height, "width": width}
    if hidden:
        page["visibility"] = "HiddenInViewMode"
    write_text(os.path.join(pdir, pid, "page.json"), json.dumps(page, indent=2) + "\n")
    order = list(meta.get("pageOrder") or [])
    pos = order.index(_find_page(pages, after)) + 1 if after else len(order)
    order.insert(pos, pid)
    meta["pageOrder"] = order
    _t, bom = read_text(meta_p) if os.path.exists(meta_p) else ("", b"")
    write_text(meta_p, json.dumps(meta, indent=2) + "\n", bom)
    return {"verb": "add", "kind": "page", "page": name, "id": pid, "position": pos,
            "files": [os.path.relpath(p, ws.mirror).replace("\\", "/") for p in (os.path.join(pdir, pid, "page.json"), meta_p)],
            "next": f"pbz add visual <type> --page \"{name}\" --field ..., then pbz validate / pbz push"}


def _parse_field(model, ref: str):
    m = re.match(r"^\s*'((?:[^']|'')+)'\s*\[(.+)\]\s*$", ref) or re.match(r"^\s*([^\[\]']+?)\s*\[(.+)\]\s*$", ref)
    if m:
        t, n = m.group(1).replace("''", "'"), m.group(2)
        c = model.column(t, n)
        if c:
            return "column", c.table, c.name, c.data_type.lower()
        mm = model.measure(n)
        if mm and mm.table.lower() == t.lower():
            return "measure", mm.table, mm.name, ""
        raise PbzError("NOT_FOUND", ref)
    m = re.match(r"^\s*\[(.+)\]\s*$", ref)
    mm = model.measure(m.group(1)) if m else None
    if mm is None:
        raise PbzError("NOT_FOUND", f"{ref}: use [Measure] or 'Table'[Column]")
    return "measure", mm.table, mm.name, ""


def _projection(kind, table, name, dtype, role, vtype):
    src = {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}
    if kind == "measure":
        return {"field": {"Measure": src}, "queryRef": f"{table}.{name}", "nativeQueryRef": name}
    if role in ("Y", "Values") and vtype not in ("tableEx", "slicer", "pivotTable") and dtype in NUMERIC:
        return {"field": {"Aggregation": {"Expression": {"Column": src}, "Function": 0}},
                "queryRef": f"Sum({table}.{name})", "nativeQueryRef": f"Sum of {name}"}
    p = {"field": {"Column": src}, "queryRef": f"{table}.{name}", "nativeQueryRef": name}
    if role in ("Category", "Rows", "Series"):
        p["active"] = True
    return p


def _free_spot(existing: list[dict], w: float, h: float, pw: float, ph: float, gap: float = 20.0):
    """First free spot (top-left first, 10px steps) that keeps a gap to every visual on the page."""
    boxes = [(p.get("x", 0), p.get("y", 0), p.get("width", 0), p.get("height", 0)) for p in existing]

    def free(x, y):
        return all(x + w + gap <= bx or bx + bw + gap <= x or y + h + gap <= by or by + bh + gap <= y
                   for bx, by, bw, bh in boxes)

    y = gap
    while y + h <= ph - gap / 2:
        x = gap
        while x + w <= pw - gap / 2:
            if free(x, y):
                return x, y, "free_spot"
            x += 10
        y += 10
    return gap, gap, "NO_FREE_SPACE (overlaps; pass --pos or resize the page)"


def _lit(text: str) -> dict:
    return {"expr": {"Literal": {"Value": "'" + text.replace("'", "''") + "'"}}}


def add_visual(ws, project, vtype: str, page: str, fields: list[str], pos: str | None = None,
               title: str | None = None, text: str | None = None) -> dict:
    if vtype not in ROLES:
        raise PbzError("BAD_REQUEST", "type must be one of: " + ", ".join(sorted(ROLES)))
    for s in (title, text):
        if s and TOKEN_RE.search(s):
            raise PbzError("BAD_REQUEST", "mirror tokens cannot be used in titles or text")
    mdef = os.path.join(project.mirror_model, "definition")
    model = tmdl.load_model(mdef)
    rdir = project.mirror_report
    pdir, _mp, _meta, pages = _pages(rdir)
    pid = _find_page(pages, page)
    roles, col_role, meas_role = ROLES[vtype]
    state: dict = {}
    for f in fields:
        role, _, ref = f.partition("=") if "=" in f.split("[", 1)[0] else ("", "", f)
        kind, table, name, dtype = _parse_field(model, ref)
        numeric_value = kind == "column" and dtype in NUMERIC and meas_role and vtype not in ("tableEx",)
        role = role.strip() or (meas_role if kind == "measure" or numeric_value else col_role)
        if not role or role not in roles:
            raise PbzError("BAD_REQUEST", f"{vtype} has no role for {ref}; roles: {', '.join(roles) or 'none'}")
        if vtype == "slicer" and kind == "measure":
            raise PbzError("BAD_REQUEST", "a slicer needs a column")
        state.setdefault(role, {"projections": []})["projections"].append(_projection(kind, table, name, dtype, role, vtype))
    if vtype != "textbox" and not state:
        raise PbzError("BAD_REQUEST", "--field is required (ROLE=ref or ref)")
    vdir = os.path.join(pdir, pid, "visuals")
    existing = []
    for vid in os.listdir(vdir) if os.path.isdir(vdir) else []:
        try:
            existing.append(json.loads(read_text(os.path.join(vdir, vid, "visual.json"))[0]).get("position") or {})
        except (OSError, ValueError):
            continue
    w, h = SIZES.get(vtype, (420, 300))
    placed = "given"
    if pos:
        try:
            x, y, w, h = [float(v) for v in pos.split(",")]
        except ValueError:
            raise PbzError("BAD_REQUEST", "--pos is x,y,width,height")
    else:
        x, y, placed = _free_spot(existing, float(w), float(h), float(pages[pid].get("width", 1280)),
                                  float(pages[pid].get("height", 720)))
    z = max((p.get("z", 0) for p in existing), default=0) + 1000
    tab = max((p.get("tabOrder", 0) for p in existing), default=0) + 1000
    ver = _versions(rdir).get("visualContainer", "2.4.0")
    vid = _id()
    visual: dict = {"visualType": vtype}
    if state:
        visual["query"] = {"queryState": {r: state[r] for r in roles if r in state}}
    if vtype == "textbox":
        visual["objects"] = {"general": [{"properties": {"paragraphs": [{"textRuns": [{"value": text or ""}]}]}}]}
    visual["drillFilterOtherVisuals"] = True
    if title:  # container formatting lives inside "visual", next to "objects"
        visual["visualContainerObjects"] = {"title": [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}},
                                                                      "text": _lit(title)}}]}
    container = {"$schema": f"{SCHEMA}/visualContainer/{ver}/schema.json", "name": vid,
                 "position": {"x": x, "y": y, "z": z, "height": h, "width": w, "tabOrder": tab}, "visual": visual}
    path = os.path.join(vdir, vid, "visual.json")
    write_text(path, json.dumps(container, indent=2) + "\n")
    return {"verb": "add", "kind": "visual", "type": vtype, "page": pages[pid].get("displayName", pid), "id": vid,
            "box": [int(x), int(y), int(w), int(h)], "placement": placed,
            "roles": {r: len(v["projections"]) for r, v in state.items()},
            "files": [os.path.relpath(path, ws.mirror).replace("\\", "/")],
            "next": "pbz layout --page <page>, pbz validate, then pbz push (--reload for report-only changes)"}
