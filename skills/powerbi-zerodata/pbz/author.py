"""`pbz add page|visual`: create report pages and visuals in the mirror, bound to model fields by name.
Visual types and their data roles come from the visual catalog (`pbz visual`): every built-in type in
Microsoft's core visual capabilities, plus registered custom visuals with a known contract. New objects
carry no filters, selections or value-specific formatting. The $schema versions follow the report's
existing files. Then `pbz layout`, `pbz validate`, `pbz push`."""
from __future__ import annotations

import collections
import json
import os
import re
import secrets

from . import images, pbir, tmdl, visuals
from .common import PbzError, commit_bytes, read_text, write_text
from .tokens import TOKEN_RE

SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"
SIZES = {"card": (220, 120), "cardVisual": (260, 140), "multiRowCard": (300, 160), "kpi": (260, 160),
         "gauge": (300, 200), "tableEx": (560, 320), "pivotTable": (560, 320), "slicer": (220, 160),
         "listSlicer": (220, 220), "textSlicer": (220, 80), "advancedSlicerVisual": (300, 160), "textbox": (320, 60),
         "actionButton": (160, 50), "shape": (160, 120), "basicShape": (160, 120)}
NUMERIC = {"int64", "double", "decimal", "currency"}
ACTIVE_ROLES = {"Category", "Rows", "Series"}


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


def add_page(ws, project, name: str, width: int | None = None, height: int | None = None, hidden: bool = False,
             after: str | None = None, tooltip: bool = False, drillthrough: list[str] | None = None) -> dict:
    """A plain page, a report-page tooltip (`tooltip`, 320x240, hidden) or a drillthrough target
    (`drillthrough`: the columns it drills on; a Back button is added)."""
    if TOKEN_RE.search(name) or not name.strip():
        raise PbzError("BAD_REQUEST", "a page needs a plain display name")
    if tooltip and drillthrough:
        raise PbzError("BAD_REQUEST", "a page is a tooltip page or a drillthrough page, not both")
    rdir = project.mirror_report
    pdir, meta_p, meta, pages = _pages(rdir)
    if not os.path.isdir(pdir):
        raise PbzError("NO_MIRROR", "run `pbz pull` first (no report pages in the mirror)")
    if any(str(p.get("displayName", "")).lower() == name.lower() for p in pages.values()):
        raise PbzError("NAME_TAKEN", f"page {name}")
    if after:
        _find_page(pages, after)  # unknown anchor refuses before any write
    ver = _versions(rdir).get("page", "2.1.0")
    pid = _id()
    width = width or (320 if tooltip else 1280)
    height = height or (240 if tooltip else 720)
    page = {"$schema": f"{SCHEMA}/page/{ver}/schema.json", "name": pid, "displayName": name,
            "displayOption": "ActualSize" if tooltip else "FitToPage", "height": height, "width": width}
    if hidden or tooltip:
        page["visibility"] = "HiddenInViewMode"
    if tooltip:
        page["type"] = "Tooltip"
        page["pageBinding"] = {"name": _id(), "type": "Tooltip", "referenceScope": "Default"}
    if drillthrough:
        from . import filters
        model = tmdl.load_model(os.path.join(project.mirror_model, "definition"))
        taken = filters._all_filter_names(project)
        flts = []
        for ref in drillthrough:
            if _parse_field(model, ref)[0] == "measure":
                raise PbzError("BAD_REQUEST", f"{ref}: drillthrough fields are columns")
            f = filters.build(model, ref)
            while f["name"] in taken:
                f["name"] = filters._id()
            taken.add(f["name"])
            f["howCreated"] = "Drillthrough"
            flts.append(f)
        page["type"] = "Drillthrough"
        page["filterConfig"] = {"filters": flts}
        page["pageBinding"] = {"name": _id(), "type": "Drillthrough", "referenceScope": "Default",
                               "parameters": [{"name": _id(), "boundFilter": f["name"], "fieldExpr": f["field"]} for f in flts]}
    page_path = os.path.join(pdir, pid, "page.json")
    order = list(meta.get("pageOrder") or [])
    anchor = _find_page(pages, after) if after else None
    pos = order.index(anchor) + 1 if anchor is not None else len(order)
    order.insert(pos, pid)
    meta["pageOrder"] = order
    _t, bom = read_text(meta_p) if os.path.exists(meta_p) else ("", b"")
    ops: list[tuple] = [
        ("write", page_path, json.dumps(page, indent=2, ensure_ascii=False) + "\n", b""),
        ("write", meta_p, json.dumps(meta, indent=2) + "\n", bom),
    ]
    files = [os.path.relpath(p, ws.mirror).replace("\\", "/") for p in (page_path, meta_p)]
    out = {"verb": "add", "kind": "page", "page": name, "id": pid, "position": pos, "files": files,
           "next": f"pbz add visual <type> --page \"{name}\" --field ..., then pbz validate / pbz push"}
    if tooltip:
        out["type"] = "tooltip"
        out["next"] = f"pbz add visual ... --page \"{name}\", then pbz link <page> <visual> --tooltip \"{name}\""
    if drillthrough:
        btn, btn_ops = add_visual(ws, project, "actionButton", name, [], pos="16,16,96,40",
                                  _commit=False, _extra_pages={pid: page})
        bpath, btext = btn_ops[0][1], btn_ops[0][2]
        bdoc = json.loads(btext)
        body = bdoc["visual"]
        body.setdefault("visualContainerObjects", {})["visualLink"] = [{"properties": {
            "show": {"expr": {"Literal": {"Value": "true"}}},
            "type": {"expr": {"Literal": {"Value": "'Back'"}}},
        }}]
        ops.append(("write", bpath, json.dumps(bdoc, indent=2, ensure_ascii=False) + "\n", b""))
        ops.extend(btn_ops[1:])
        out.update(type="drillthrough", drill_on=[filters._label(f) for f in flts], back_button=btn["id"])
        out["files"] = files + btn["files"]
        out["next"] = (f"pbz add visual ... --page \"{name}\"; a button elsewhere can open it with "
                       f"pbz link <page> <button> --drillthrough \"{name}\"")
    commit_bytes(ops)
    return out


LINKABLE = {"actionButton", "shape", "basicShape", "image"}


def _bookmark_id(rdir: str, ref: str) -> str:
    bdir = os.path.join(rdir, "definition", "bookmarks")
    for f in sorted(os.listdir(bdir)) if os.path.isdir(bdir) else []:
        if not f.endswith(".bookmark.json"):
            continue
        bid = f[:-len(".bookmark.json")]
        try:
            disp = json.loads(read_text(os.path.join(bdir, f))[0]).get("displayName", "")
        except ValueError:
            disp = ""
        if ref.lower() in (bid.lower(), str(disp).lower()):
            return bid
    raise PbzError("NOT_FOUND", f"bookmark {ref}")


def link(ws, project, page: str, visual: str, to_page: str | None = None, back: bool = False,
         bookmark: str | None = None, drillthrough: str | None = None, url: str | None = None,
         tooltip: str | None = None, clear: bool = False) -> dict:
    """Button actions (page navigation, back, bookmark, drillthrough, web URL) and report-page
    tooltips, written as the visual container's visualLink / visualTooltip."""
    from .style import targets
    t = targets(project, page, visual, None, False)[0]
    rdir = project.mirror_report
    _pdir, _mp, _meta, pages = _pages(rdir)
    actions = [x for x in (to_page, bookmark, drillthrough, url) if x] + ([True] if back else [])
    if len(actions) > 1:
        raise PbzError("BAD_REQUEST", "one action per button: --to-page, --back, --bookmark, --drillthrough or --url")
    if (actions or clear) and t["type"] not in LINKABLE:
        raise PbzError("BAD_REQUEST", f"actions go on buttons, shapes and images, not {t['type']}")
    if not (actions or clear or tooltip):
        raise PbzError("BAD_REQUEST", "pass an action, --tooltip <page> or --clear")
    lit = lambda v: {"expr": {"Literal": {"Value": v}}}  # noqa: E731
    quoted = lambda s: "'" + s.replace("'", "''") + "'"  # noqa: E731
    text, bom = read_text(t["path"])
    doc = json.loads(text)
    body = doc["visual"]
    vco = body.setdefault("visualContainerObjects", {})
    done = []
    if clear:
        if vco.pop("visualLink", None) is not None:
            done.append("action cleared")
    elif actions:
        props = {"show": lit("true")}
        if back:
            props["type"] = lit("'Back'")
            done.append("back")
        elif to_page:
            pid = _find_page(pages, to_page)
            props.update(type=lit("'PageNavigation'"), navigationSection=lit(quoted(pid)))
            done.append(f"page {pages[pid].get('displayName', pid)}")
        elif drillthrough:
            pid = _find_page(pages, drillthrough)
            if (pages[pid].get("pageBinding") or {}).get("type") != "Drillthrough":
                raise PbzError("BAD_REQUEST", f"{drillthrough} is not a drillthrough page (`pbz add page --drillthrough`)")
            props.update(type=lit("'Drillthrough'"), drillthroughSection=lit(quoted(pid)))
            done.append(f"drillthrough {pages[pid].get('displayName', pid)}")
        elif bookmark:
            props.update(type=lit("'Bookmark'"), bookmark=lit(quoted(_bookmark_id(rdir, bookmark))))
            done.append(f"bookmark {bookmark}")
        else:
            if TOKEN_RE.search(url) or not re.match(r"^https?://", url):
                raise PbzError("BAD_REQUEST", "a web link is an http(s) URL without mirror tokens")
            props.update(type=lit("'WebUrl'"), webUrl=lit(quoted(url)))
            done.append("web link")
        vco["visualLink"] = [{"properties": props}]
    if tooltip:
        pid = _find_page(pages, tooltip)
        if (pages[pid].get("pageBinding") or {}).get("type") != "Tooltip":
            raise PbzError("BAD_REQUEST", f"{tooltip} is not a tooltip page (`pbz add page --tooltip`)")
        vco["visualTooltip"] = [{"properties": {"show": lit("true"), "type": lit("'Canvas'"), "section": lit(quoted(pid))}}]
        done.append(f"tooltip page {pages[pid].get('displayName', pid)}")
    if not vco:
        body.pop("visualContainerObjects", None)
    write_text(t["path"], json.dumps(doc, indent=2, ensure_ascii=False) + "\n", bom)
    return {"verb": "link", "page": t["page"], "visual": t["id"], "type": t["type"], "set": done,
            "files": [os.path.relpath(t["path"], ws.mirror).replace("\\", "/")],
            "next": "pbz validate, then pbz push --reload"}


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


def _projection(kind, table, name, dtype, role: dict):
    """queryRef and nativeQueryRef sit next to `field`, never inside it."""
    src = {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}
    if kind == "measure":
        return {"field": {"Measure": src}, "queryRef": f"{table}.{name}", "nativeQueryRef": name}
    if role["kind"] == "measure" and dtype in NUMERIC:  # implicit sum, as Desktop does
        return {"field": {"Aggregation": {"Expression": {"Column": src}, "Function": 0}},
                "queryRef": f"Sum({table}.{name})", "nativeQueryRef": f"Sum of {name}"}
    p = {"field": {"Column": src}, "queryRef": f"{table}.{name}", "nativeQueryRef": name}
    if role["kind"] != "measure" and role["name"] in ACTIVE_ROLES:
        p["active"] = True
    return p


def _default_role(roles: list[dict], kind: str, dtype: str) -> dict | None:
    """First role that fits the field: measures go to measure roles, numeric columns to a measure role
    (implicit sum) unless the first grouping role takes either, other columns to grouping roles."""
    usable = [r for r in roles if r["name"] != "Tooltips"] or roles
    meas = [r for r in usable if r["kind"] in ("measure", "either")]
    grp = [r for r in usable if r["kind"] in ("grouping", "either")]
    if kind == "measure":
        return meas[0] if meas else None
    if dtype in NUMERIC and any(r["kind"] == "measure" for r in usable) and not (grp and grp[0]["kind"] == "either"):
        return next(r for r in usable if r["kind"] == "measure")
    return grp[0] if grp else (meas[0] if meas else None)


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
               title: str | None = None, text: str | None = None, image: str | None = None,
               fit: str | None = None, alt: str | None = None, *, _commit: bool = True,
               _extra_pages: dict | None = None):
    for s in (title, text, alt):
        if s and TOKEN_RE.search(s):
            raise PbzError("BAD_REQUEST", "mirror tokens cannot be used in titles, text or alt text")
    if not os.path.isdir(project.mirror_report):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    mdef = os.path.join(project.mirror_model, "definition")
    model = tmdl.load_model(mdef)
    rdir = project.mirror_report
    pdir, _mp, _meta, pages = _pages(rdir)
    if _extra_pages:
        pages.update(_extra_pages)
    pid = _find_page(pages, page)
    vtype, contract, source, notes, pending = visuals.resolve(ws, project, vtype)
    item = None
    if vtype == "image":
        if not image:
            raise PbzError("BAD_REQUEST", "an image visual needs --image <item>: register the file first "
                                          "(pbz image add <file>, then pbz image list)")
        item = images.resolve(ws, project, image)
    elif image or fit:
        raise PbzError("BAD_REQUEST", "--image and --fit are for image visuals; other visuals take images through "
                                      "pbz style <page> <visual> --set <object>.image=<item>")
    scaling = images.SCALING.get((fit or "fit").lower())
    if scaling is None:
        raise PbzError("BAD_REQUEST", f"{fit}: --fit is fit, fill or normal")
    roles = contract.get("roles") or []
    by_name = {r["name"].lower(): r for r in roles}
    names = ", ".join(r["name"] for r in roles) or "none"
    state: dict = {}
    counts: dict[str, int] = {}
    for f in fields:
        role, _, ref = f.partition("=") if "=" in f.split("[", 1)[0] else ("", "", f)
        kind, table, name, dtype = _parse_field(model, ref)
        if role.strip():
            r = by_name.get(role.strip().lower())
            if r is None:
                raise PbzError("BAD_REQUEST", f"{vtype} has no role {role.strip()}; roles: {names}")
        else:
            r = _default_role(roles, kind, dtype)
            if r is None:
                raise PbzError("BAD_REQUEST", f"{vtype} has no role for {ref}; roles: {names}")
        if kind == "measure" and r["kind"] == "grouping":
            raise PbzError("BAD_REQUEST", f"{vtype} role {r['name']} takes columns, not measures"
                           + (" (a slicer needs a column)" if "slicer" in vtype.lower() else ""))
        state.setdefault(r["name"], {"projections": []})["projections"].append(_projection(kind, table, name, dtype, r))
        counts[r["name"]] = counts.get(r["name"], 0) + 1
    if roles and not state and vtype != "textbox":
        raise PbzError("BAD_REQUEST", f"--field is required (ROLE=ref or ref); {vtype} roles: {names}")
    if state and not visuals.fits(contract, counts):
        raise PbzError("BAD_REQUEST", f"{vtype}: fields per role {counts} fit none of the allowed combinations: "
                       + " | ".join(visuals.conditions_text(contract)))
    vdir = os.path.join(pdir, pid, "visuals")
    existing = []
    for vid in os.listdir(vdir) if os.path.isdir(vdir) else []:
        try:
            existing.append(json.loads(read_text(os.path.join(vdir, vid, "visual.json"))[0]).get("position") or {})
        except (OSError, ValueError):
            continue
    w, h = SIZES.get(vtype, (420, 300))
    if item:  # the image's own proportions, at most 320 x 200 and never enlarged
        px = images.pixels(ws, project, item)
        if px:
            k = min(320 / px[0], 200 / px[1], 1.0)
            w, h = max(40, round(px[0] * k)), max(40, round(px[1] * k))
        else:
            w, h = 200, 200
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
        visual["query"] = {"queryState": {r["name"]: state[r["name"]] for r in roles if r["name"] in state}}
    if vtype == "textbox":
        visual["objects"] = {"general": [{"properties": {"paragraphs": [{"textRuns": [{"value": text or ""}]}]}}]}
    if item:  # a local file: general.imageUrl holds the ResourcePackageItem (Microsoft's image visual format)
        visual["objects"] = {"general": [{"properties": {"imageUrl": images.ref_expr(item)}}],
                             "imageScaling": [{"properties": {"imageScalingType": _lit(scaling)}}]}
    visual["drillFilterOtherVisuals"] = True
    vco: dict = {}
    if title:  # container formatting lives inside "visual", next to "objects"
        vco["title"] = [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}, "text": _lit(title)}}]
    if alt:
        vco["general"] = [{"properties": {"altText": _lit(alt)}}]
    if vco:
        visual["visualContainerObjects"] = vco
    container = {"$schema": f"{SCHEMA}/visualContainer/{ver}/schema.json", "name": vid,
                 "position": {"x": x, "y": y, "z": z, "height": h, "width": w, "tabOrder": tab}, "visual": visual}
    path = os.path.join(vdir, vid, "visual.json")
    visual_text = json.dumps(container, indent=2, ensure_ascii=False) + "\n"
    ops: list[tuple] = [("write", path, visual_text, b"")]
    if pending:
        reg_type, change, reg_op = visuals.plan_registration(project, pending[0], pending[1], pending[2])
        if change == "added" and reg_op:
            notes.append(f"registered {reg_type} as {pending[1]} in report.json")
            ops.append(reg_op)
    files = [os.path.relpath(path, ws.mirror).replace("\\", "/")]
    if any(n.startswith("registered") for n in notes):
        files.append(os.path.relpath(os.path.join(rdir, "definition", "report.json"), ws.mirror).replace("\\", "/"))
    out = {"verb": "add", "kind": "visual", "type": vtype, "page": pages[pid].get("displayName", pid), "id": vid,
           "box": [int(x), int(y), int(w), int(h)], "placement": placed,
           "roles": {r: len(v["projections"]) for r, v in state.items()}, "files": files,
           "next": "pbz layout --page <page>, pbz validate, then pbz push (--reload for report-only changes)"}
    if item:
        out["image"] = item
        if not alt:
            out["next"] = f'describe it for screen readers: pbz style "<page>" {vid} --set container.general.altText="..."; ' \
                          + out["next"]
    if source != "core":
        out["custom"] = {"roles_from": source, **({"notes": notes} if notes else {})}
        if vtype in ("deneb7E15AEF80B9E4D4F8E12924291ECE89A",) or "vega" in (contract.get("objects") or {}):
            out["custom"]["setup"] = "spec-driven visual: write its spec in Desktop (it shows a landing page until then)"
    if not _commit:
        return out, ops
    commit_bytes(ops)
    return out
