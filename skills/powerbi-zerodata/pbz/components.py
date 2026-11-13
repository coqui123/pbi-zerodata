"""`pbz copy page|visual`: duplicate report pages and visuals (or visual groups with their children) in the
mirror, with every identifier remapped so the copy is a new, independent object:

- page: new page name (id) and display name, new visual names, `parentGroupName` remapped, page
  `visualInteractions` remapped (pairs that leave the page are dropped), a new `pageBinding.name` for
  tooltip and drillthrough pages and new parameter names, new filter names at page and visual level
  (unique across the report; drillthrough filters keep `howCreated: Drillthrough` and the parameters'
  `boundFilter` follows the rename). Inserted into `pages.json` after the source page (or `after`).
- links: a `visualLink` (navigationSection / drillthroughSection) or `visualTooltip.section` that
  pointed at the copied page itself now points at the copy; links to every other page stay as they are.
- slicers keep their `syncGroup` (copies stay in sync with the originals) unless independent_slicers,
  which gives every copied sync group a new name shared by the copied slicers only.
- visual: a visual or a group and all its descendants onto the same or another page, at a free spot
  (or `pos`), above the target page's visuals, with new names and filter names; a copied grouped
  visual becomes top-level; interactions are not copied. Positions are page coordinates, so a group
  and its children move by the same offset.

Tokens are copied as tokens (same model, same token map). Bookmarks are not copied: they name the
source page and its visuals, and their state may carry data."""
from __future__ import annotations

import copy
import json
import os

from . import author, filters
from .common import PbzError, anchor_mirror, commit_bytes, read_definition_text
from .layout import LABELS
from .tokens import TOKEN_RE


def _load(ws, path: str) -> tuple[dict, bytes]:
    rel = _rel(ws, path)
    try:
        text, bom = read_definition_text(path)
        doc = json.loads(text)
    except (OSError, ValueError, UnicodeDecodeError):
        raise PbzError("REFUSED", rel)
    if not isinstance(doc, dict):
        raise PbzError("REFUSED", rel)
    return doc, bom


def _read_blob(ws, path: str) -> tuple[str, bytes]:
    try:
        return read_definition_text(path)
    except (OSError, UnicodeDecodeError):
        raise PbzError("REFUSED", _rel(ws, path))


def _json_text(doc: dict) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _require_pages(ws, rdir: str) -> None:
    """Parse pages.json and every page.json before a helper can raise a raw parser error."""
    pdir = os.path.join(rdir, "definition", "pages")
    meta_p = os.path.join(pdir, "pages.json")
    if os.path.isfile(meta_p):
        _load(ws, meta_p)
    if not os.path.isdir(pdir):
        return
    for pid in os.listdir(pdir):
        pj = os.path.join(pdir, pid, "page.json")
        if os.path.isfile(pj):
            _load(ws, pj)


def _rel(ws, path: str) -> str:
    return os.path.relpath(path, ws.mirror).replace("\\", "/")


def _visual_names(pdir: str) -> set:
    names = set()
    for pid in os.listdir(pdir):
        vdir = os.path.join(pdir, pid, "visuals")
        if os.path.isdir(vdir):
            names |= set(os.listdir(vdir))
    return names


def _fresh(taken: set) -> str:
    new = author._id()
    while new in taken:
        new = author._id()
    taken.add(new)
    return new


def _rename_filters(doc: dict, taken: set) -> dict:
    """New, report-unique names for the filters of one page.json or visual.json. Returns old -> new."""
    mp = {}
    for f in (doc.get("filterConfig") or {}).get("filters") or [] if isinstance(doc.get("filterConfig"), dict) else []:
        if isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"]:
            new = filters._id()
            while new in taken:
                new = filters._id()
            taken.add(new)
            mp[f["name"]] = new
            f["name"] = new
    return mp


def _retarget(body: dict, refs: set, new_pid: str) -> int:
    """Point visualLink / visualTooltip page references to `refs` (the copied page's id or display
    name) at the copy. Returns how many changed."""
    vco = body.get("visualContainerObjects")
    if not isinstance(vco, dict):
        return 0
    n = 0
    for obj, keys in (("visualLink", ("navigationSection", "drillthroughSection")), ("visualTooltip", ("section",))):
        for entry in vco.get(obj) or [] if isinstance(vco.get(obj), list) else []:
            props = (entry or {}).get("properties") if isinstance(entry, dict) else None
            for k in keys:
                lit = (((props or {}).get(k) or {}).get("expr") or {}).get("Literal") if isinstance(props, dict) else None
                val = lit.get("Value") if isinstance(lit, dict) else None
                if isinstance(val, str) and len(val) >= 2 and val[0] == val[-1] == "'" and val[1:-1].replace("''", "'") in refs:
                    lit["Value"] = "'" + new_pid + "'"
                    n += 1
    return n


def _page_visuals(pdir: str, pid: str) -> list[tuple[str, str]]:
    """[(visual name, path)] in the order `pbz layout` labels them."""
    vdir = os.path.join(pdir, pid, "visuals")
    out = []
    for vid in sorted(os.listdir(vdir)) if os.path.isdir(vdir) else []:
        p = os.path.join(vdir, vid, "visual.json")
        if os.path.isfile(p):
            out.append((vid, p))
    return out


def _bookmarks_on(rdir: str, pid: str) -> int:
    bdir = os.path.join(rdir, "definition", "bookmarks")
    n = 0
    for f in os.listdir(bdir) if os.path.isdir(bdir) else []:
        if f.endswith(".bookmark.json"):
            try:
                es = json.loads(read_definition_text(os.path.join(bdir, f))[0]).get("explorationState") or {}
            except ValueError:
                continue
            if es.get("activeSection") == pid or pid in (es.get("sections") or {}):
                n += 1
    return n


def copy_page(ws, project, page: str, new_name: str, after: str | None = None,
              independent_slicers: bool = False) -> dict:
    if not new_name.strip() or TOKEN_RE.search(new_name):
        raise PbzError("BAD_REQUEST", "the copy needs a plain display name")
    rdir = project.mirror_report
    _require_pages(ws, rdir)
    pdir, meta_p, meta, pages = author._pages(rdir)
    if not os.path.isdir(pdir):
        raise PbzError("NO_MIRROR", "run `pbz pull` first (no report pages in the mirror)")
    src = author._find_page(pages, page)
    if any(str(p.get("displayName", "")).lower() == new_name.strip().lower() for p in pages.values()):
        raise PbzError("NAME_TAKEN", f"page {new_name}")
    anchor = author._find_page(pages, after) if after else src
    new_pid = _fresh(set(pages) | set(os.listdir(pdir)))
    taken_filters = filters._all_filter_names(project)
    taken_visuals = _visual_names(pdir)
    src_dir = os.path.join(pdir, src)
    dst_dir = os.path.join(pdir, new_pid)
    vis = _page_visuals(pdir, src)
    vmap = {vid: _fresh(taken_visuals) for vid, _p in vis}
    pdoc, pbom = _load(ws, os.path.join(src_dir, "page.json"))
    pdoc = copy.deepcopy(pdoc)
    src_display = str(pdoc.get("displayName") or src)
    pdoc["name"] = new_pid
    pdoc["displayName"] = new_name.strip()
    fmap = _rename_filters(pdoc, taken_filters)
    pb = pdoc.get("pageBinding")
    binding = None
    if isinstance(pb, dict):
        pb["name"] = author._id()
        binding = pb.get("type")
        for prm in pb.get("parameters") or [] if isinstance(pb.get("parameters"), list) else []:
            if isinstance(prm, dict):
                prm["name"] = author._id()
                if prm.get("boundFilter") in fmap:
                    prm["boundFilter"] = fmap[prm["boundFilter"]]
    dropped = 0
    if isinstance(pdoc.get("visualInteractions"), list):
        kept = []
        for it in pdoc["visualInteractions"]:
            s = it.get("source") if isinstance(it, dict) else None
            t = it.get("target") if isinstance(it, dict) else None
            if s in vmap and t in vmap:
                kept.append(dict(it, source=vmap[s], target=vmap[t]))
            else:
                dropped += 1
        if kept:
            pdoc["visualInteractions"] = kept
        else:
            pdoc.pop("visualInteractions")
    ops: list[tuple] = []
    files = []

    def plan(path: str, text: str, bom: bytes) -> None:
        ops.append(("write", path, text, bom))
        files.append(path)

    plan(os.path.join(dst_dir, "page.json"), _json_text(pdoc), pbom)
    for cur, dirs, fs in os.walk(src_dir):  # other page-level files (not page.json, not visuals) as they are
        rel = os.path.relpath(cur, src_dir)
        if rel.split(os.sep)[0] == "visuals":
            dirs[:] = []
            continue
        for f in fs:
            if rel == "." and f == "page.json":
                continue
            src_f = os.path.join(cur, f)
            text, bom = _read_blob(ws, src_f)
            plan(os.path.join(dst_dir, rel, f), text, bom)
    sync, retargeted, vfilters, kept_sync = {}, 0, 0, False
    refs = {src, src_display}
    for vid, path in vis:
        vsrc = os.path.dirname(path)
        vdst = os.path.join(dst_dir, "visuals", vmap[vid])
        for cur, _dirs, fs in os.walk(vsrc):
            rel = os.path.relpath(cur, vsrc)
            for f in fs:
                s_f = os.path.join(cur, f)
                d_f = os.path.join(vdst, rel, f)
                if rel == "." and f == "visual.json":
                    v, bom = _load(ws, s_f)
                    v = copy.deepcopy(v)
                    v["name"] = vmap[vid]
                    if v.get("parentGroupName") in vmap:
                        v["parentGroupName"] = vmap[v["parentGroupName"]]
                    vfilters += len(_rename_filters(v, taken_filters))
                    body = v.get("visual") if isinstance(v.get("visual"), dict) else {}
                    sg = body.get("syncGroup")
                    if isinstance(sg, dict) and isinstance(sg.get("groupName"), str):
                        kept_sync = True
                        if independent_slicers:
                            sync.setdefault(sg["groupName"], f"{sg['groupName']}_{new_pid[:8]}")
                            sg["groupName"] = sync[sg["groupName"]]
                    retargeted += _retarget(body, refs, new_pid)
                    plan(d_f, _json_text(v), bom)
                else:
                    text, bom = _read_blob(ws, s_f)
                    plan(d_f, text, bom)
    order = list(meta.get("pageOrder") or [])
    pos = order.index(anchor) + 1 if anchor in order else len(order)
    order.insert(pos, new_pid)
    meta["pageOrder"] = order
    _t, mbom = read_definition_text(meta_p) if os.path.exists(meta_p) else ("", b"")
    plan(meta_p, _json_text(meta), mbom)
    commit_bytes(ops, root=anchor_mirror(ws))
    out = {"verb": "copy", "kind": "page", "from": src_display, "page": new_name.strip(), "id": new_pid,
           "position": pos, "visuals": len(vis), "filters_renamed": len(fmap) + vfilters,
           "files": sorted({_rel(ws, f) for f in files}),
           "next": "pbz layout --page <copy>, pbz validate, then pbz push --reload (report-only change)"}
    if binding:
        out["binding"] = binding
    if retargeted:
        out["links_to_copy"] = retargeted
    if dropped:
        out["interactions_dropped"] = dropped
    if sync:
        out["sync_groups"] = "renamed (independent of the originals)"
    elif kept_sync:
        out["sync_groups"] = "kept (the copies stay in sync with the original slicers)"
    marks = _bookmarks_on(rdir, src)
    out["notes"] = [f"bookmarks are not copied ({marks} on the source page)" if marks else "bookmarks are not copied"]
    return out


def _resolve_visual(pdir: str, pid: str, ref: str) -> str:
    vis = _page_visuals(pdir, pid)
    for i, (vid, _p) in enumerate(vis):
        if ref in (vid, LABELS[i] if i < len(LABELS) else None):
            return vid
    raise PbzError("NOT_FOUND", f"visual {ref} (ids and labels: `pbz layout --page`)")


def _num(v, default=0.0) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def copy_visual(ws, project, page: str, visual: str, to_page: str | None = None, pos: str | None = None) -> dict:
    rdir = project.mirror_report
    _require_pages(ws, rdir)
    pdir, _mp, _meta, pages = author._pages(rdir)
    if not os.path.isdir(pdir):
        raise PbzError("NO_MIRROR", "run `pbz pull` first (no report pages in the mirror)")
    src_pid = author._find_page(pages, page)
    dst_pid = author._find_page(pages, to_page) if to_page else src_pid
    top = _resolve_visual(pdir, src_pid, visual)
    docs = {vid: _load(ws, p) for vid, p in _page_visuals(pdir, src_pid)}
    members = [top]
    if "visualGroup" in docs[top][0]:
        frontier = [top]
        while frontier:
            parent = frontier.pop()
            kids = [vid for vid, (d, _b) in docs.items() if d.get("parentGroupName") == parent and vid not in members]
            members += kids
            frontier += kids
    taken_visuals = _visual_names(pdir)
    vmap = {vid: _fresh(taken_visuals) for vid in members}
    taken_filters = filters._all_filter_names(project)
    tpos = docs[top][0].get("position") or {}
    x0, y0 = _num(tpos.get("x")), _num(tpos.get("y"))
    w, h = _num(tpos.get("width"), 300.0), _num(tpos.get("height"), 200.0)
    existing = [(d.get("position") or {}) for d, _b in (_load(ws, p) for _v, p in _page_visuals(pdir, dst_pid))]
    placed = "given"
    if pos:
        try:
            parts = [float(p) for p in pos.split(",")]
        except ValueError:
            raise PbzError("BAD_REQUEST", "--pos is x,y or x,y,width,height")
        if len(parts) not in (2, 4):
            raise PbzError("BAD_REQUEST", "--pos is x,y or x,y,width,height")
        nx, ny = parts[0], parts[1]
        if len(parts) == 4:
            if len(members) > 1:
                raise PbzError("BAD_REQUEST", "a group copy takes --pos x,y (its children keep their sizes)")
            w, h = parts[2], parts[3]
    else:
        pg = pages[dst_pid]
        nx, ny, placed = author._free_spot(existing, w, h, _num(pg.get("width"), 1280.0), _num(pg.get("height"), 720.0))
    dx, dy = nx - x0, ny - y0
    zbase = max((_num(p.get("z")) for p in existing), default=0.0) + 1000
    tbase = max((_num(p.get("tabOrder")) for p in existing), default=0.0) + 1000
    zmin = min(_num((docs[m][0].get("position") or {}).get("z")) for m in members)
    tmin = min(_num((docs[m][0].get("position") or {}).get("tabOrder")) for m in members)
    extras: dict[str, list[tuple[str, str, bytes]]] = {}
    for vid in members:
        root = os.path.join(pdir, src_pid, "visuals", vid)
        for cur, _dirs, fs in os.walk(root):  # mobile.json and friends, read before any write
            rel = os.path.relpath(cur, root)
            for f in fs:
                if rel == "." and f == "visual.json":
                    continue
                text, fb = _read_blob(ws, os.path.join(cur, f))
                extras.setdefault(vid, []).append((os.path.join(rel, f), text, fb))
    ops: list[tuple] = []
    files, renamed = [], 0
    for vid in members:
        doc, bom = docs[vid]
        v = copy.deepcopy(doc)
        v["name"] = vmap[vid]
        parent = v.get("parentGroupName")
        if parent in vmap:
            v["parentGroupName"] = vmap[parent]
        elif parent is not None:
            v.pop("parentGroupName")  # the copy is top-level: its group stays with the original
        p = v.get("position")
        if isinstance(p, dict):
            p["x"] = _num(p.get("x")) + dx
            p["y"] = _num(p.get("y")) + dy
            if vid == top:
                p["width"], p["height"] = w, h
            p["z"] = zbase + _num(p.get("z")) - zmin
            p["tabOrder"] = tbase + _num(p.get("tabOrder")) - tmin
        renamed += len(_rename_filters(v, taken_filters))
        dst = os.path.join(pdir, dst_pid, "visuals", vmap[vid], "visual.json")
        ops.append(("write", dst, _json_text(v), bom))
        files.append(dst)
        for rel_name, text, fb in extras.get(vid, []):
            d_f = os.path.join(pdir, dst_pid, "visuals", vmap[vid], rel_name)
            ops.append(("write", d_f, text, fb))
            files.append(d_f)
    commit_bytes(ops, root=anchor_mirror(ws))
    tp = (docs[top][0].get("visual") or {}).get("visualType") if isinstance(docs[top][0].get("visual"), dict) else "group"
    out = {"verb": "copy", "kind": "visual", "type": tp or "group", "from": {"page": pages[src_pid].get("displayName", src_pid), "visual": top},
           "page": pages[dst_pid].get("displayName", dst_pid), "id": vmap[top], "copied": len(members),
           "box": [int(nx), int(ny), int(w), int(h)], "placement": placed, "filters_renamed": renamed,
           "files": sorted({_rel(ws, f) for f in files}),
           "next": "pbz layout --page <page>, pbz validate, then pbz push --reload"}
    if len(members) > 1:
        out["children"] = len(members) - 1
    if docs[top][0].get("parentGroupName"):
        out["note"] = "the copy is top-level (its group stays with the original)"
    return out
