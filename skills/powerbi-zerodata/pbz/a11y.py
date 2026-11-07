"""`pbz a11y` and `pbz mobile`: report accessibility audit, keyboard tab-order fix, and phone layout.

Audit (mirror only, ids and codes, never values):
- data visuals without alt text, alt text that is only a mirror token (it held a data value) or
  longer than Power BI's 250 characters
- data visuals whose title is switched off
- tab order that does not follow reading order (top to bottom, then left to right, groups kept
  together), duplicate tab orders, hidden visuals still in the tab order
- font sizes below a threshold in any property Microsoft's formatting catalog types as a font size
- low contrast (WCAG ratio below 4.5, 3 for large text) where a text color and its background are
  both static hex colors in the same visual container
- visible pages without any phone layout (info)

Phone layout: one `mobile.json` per visual (`pages/<page>/visuals/<visual>/mobile.json`, schema family
visualContainerMobileState, `position` with x, y, width, height and optional z, tabOrder), checked
against Microsoft's report validator on a Desktop-made report.
"""
from __future__ import annotations

import collections
import json
import math
import os
import re

from .common import PbzError, commit_bytes, read_text, write_text
from .tokens import TOKEN_RE

MOBILE_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainerMobileState"
PHONE_WIDTH = 320
ALT_MAX = 250
ROW_TOLERANCE = 20
NO_ALT_TYPES = {"textbox", "shape", "basicShape", "actionButton", "pageNavigator", "bookmarkNavigator", "group"}
HEX_RE = re.compile(r"^'#([0-9a-fA-F]{6})'$")


# ---------------------------------------------------------------- helpers

def _pages(project) -> list[tuple[str, str, dict, str]]:
    """[(page id, display name, page.json, page dir)] in report order."""
    pdir = os.path.join(project.mirror_report, "definition", "pages")
    if not os.path.isdir(pdir):
        raise PbzError("NO_MIRROR", "run `pbz pull` first (no report pages in the mirror)")
    order = []
    meta = os.path.join(pdir, "pages.json")
    if os.path.exists(meta):
        try:
            order = list(json.loads(read_text(meta)[0]).get("pageOrder") or [])
        except ValueError:
            order = []
    ids = order + [p for p in sorted(os.listdir(pdir)) if os.path.isdir(os.path.join(pdir, p)) and p not in order]
    out = []
    for pid in ids:
        pj = os.path.join(pdir, pid, "page.json")
        if not os.path.isfile(pj):
            continue
        try:
            page = json.loads(read_text(pj)[0])
        except ValueError:
            continue
        out.append((pid, page.get("displayName") or pid, page, os.path.join(pdir, pid)))
    return out


def _select_pages(project, page: str | None) -> list:
    pages = _pages(project)
    if page:
        pages = [p for p in pages if page.lower() in (p[0].lower(), str(p[1]).lower())]
        if not pages:
            raise PbzError("NOT_FOUND", f"page {page}")
    return pages


def _rel(project, path: str) -> str:
    return os.path.relpath(path, project.ws.mirror).replace("\\", "/")


def _require_json(project, path: str) -> tuple[dict, str, bytes]:
    """Parse a document a mutation will use. The detail is the mirror-relative path only."""
    rel = _rel(project, path)
    try:
        text, bom = read_text(path)
        doc = json.loads(text)
    except (OSError, ValueError, UnicodeDecodeError):
        raise PbzError("REFUSED", rel)
    if not isinstance(doc, dict):
        raise PbzError("REFUSED", rel)
    return doc, text, bom


def _mutation_visuals(project, page_dir: str) -> list[dict]:
    """Every visual on the page. A present but unreadable visual or phone layout is refused, not skipped."""
    out = []
    vdir = os.path.join(page_dir, "visuals")
    if not os.path.isdir(vdir):
        return out
    for vid in sorted(os.listdir(vdir)):
        path = os.path.join(vdir, vid, "visual.json")
        if not os.path.isfile(path):
            continue
        doc, text, bom = _require_json(project, path)
        body = doc.get("visual") if isinstance(doc.get("visual"), dict) else None
        vt = body.get("visualType") if body else ("group" if "visualGroup" in doc else "?")
        pos = doc.get("position") if isinstance(doc.get("position"), dict) else {}
        mobile = os.path.join(vdir, vid, "mobile.json")
        mobile_doc = None
        if os.path.isfile(mobile):
            mobile_doc = _require_json(project, mobile)[0]
        out.append({"id": vid, "type": vt, "doc": doc, "text": text, "bom": bom, "body": body or {}, "pos": pos,
                    "path": path, "hidden": doc.get("isHidden") is True, "group": doc.get("parentGroupName") or "",
                    "mobile": mobile, "mobile_doc": mobile_doc})
    return out


def _mutation_page(project, page: str):
    """The named page plus every visual on it, parsed before any helper can skip a bad document."""
    root = os.path.join(project.mirror_report, "definition", "pages")
    if not os.path.isdir(root):
        raise PbzError("NO_MIRROR", "run `pbz pull` first (no report pages in the mirror)")
    hits = []
    for pid in sorted(os.listdir(root)):
        pj = os.path.join(root, pid, "page.json")
        if not os.path.isfile(pj):
            continue
        forced = page.lower() == pid.lower()
        if forced:
            doc, _text, _bom = _require_json(project, pj)
        else:
            try:
                doc = json.loads(read_text(pj)[0])
                if not isinstance(doc, dict):
                    raise ValueError
            except (OSError, ValueError, UnicodeDecodeError):
                continue
        name = doc.get("displayName") or pid
        if forced or page.lower() in (pid.lower(), str(name).lower()):
            hits.append((pid, name, doc, os.path.join(root, pid)))
    if not hits:
        raise PbzError("NOT_FOUND", f"page {page}")
    pid, pname, pjson, pdir = hits[0]
    return pid, pname, pjson, pdir, _mutation_visuals(project, pdir)


def _phone_pos(pos: str) -> tuple[float, float, float, float]:
    try:
        x, y, w, h = [float(t) for t in pos.split(",")]
    except ValueError:
        raise PbzError("BAD_REQUEST", "--pos is x,y,width,height")
    if not all(math.isfinite(n) for n in (x, y, w, h)) or w <= 0 or h <= 0 or x < 0 or y < 0:
        raise PbzError("BAD_REQUEST", "phone positions are non-negative with a positive size")
    return x, y, w, h


def _visuals(page_dir: str) -> list[dict]:
    """Every visual container on a page with its parsed JSON (groups included)."""
    out = []
    vdir = os.path.join(page_dir, "visuals")
    for vid in sorted(os.listdir(vdir)) if os.path.isdir(vdir) else []:
        path = os.path.join(vdir, vid, "visual.json")
        try:
            doc = json.loads(read_text(path)[0])
        except (OSError, ValueError):
            continue
        body = doc.get("visual") if isinstance(doc.get("visual"), dict) else None
        vt = body.get("visualType") if body else ("group" if "visualGroup" in doc else "?")
        pos = doc.get("position") if isinstance(doc.get("position"), dict) else {}
        out.append({"id": vid, "type": vt, "doc": doc, "body": body or {}, "pos": pos, "path": path,
                    "hidden": doc.get("isHidden") is True, "group": doc.get("parentGroupName") or "",
                    "mobile": os.path.join(vdir, vid, "mobile.json")})
    return out


def _num(v, default=0.0) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _lit(node):
    e = node.get("expr") if isinstance(node, dict) else None
    lit = e.get("Literal") if isinstance(e, dict) else None
    return lit.get("Value") if isinstance(lit, dict) else None


def _hex(node) -> str | None:
    """Static #RRGGBB of a color property value ({solid: {color: expr Literal}}), else None."""
    if isinstance(node, dict) and isinstance(node.get("solid"), dict):
        node = node["solid"].get("color")
    v = _lit(node)
    m = HEX_RE.match(v) if isinstance(v, str) else None
    return m.group(1).upper() if m else None


def _font_pt(node) -> float | None:
    v = _lit(node)
    if isinstance(v, str):
        m = re.match(r"^(-?\d+(?:\.\d+)?)[DLM]?$", v)
        if m:
            return float(m.group(1))
    return None


def luminance(hex6: str) -> float:
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex6[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _entries(body: dict, section: str):
    sec = body.get(section)
    for obj, entries in (sec.items() if isinstance(sec, dict) else []):
        for e in entries if isinstance(entries, list) else []:
            if isinstance(e, dict) and isinstance(e.get("properties"), dict):
                yield obj, e


def _data_visual(v: dict) -> bool:
    from . import visuals as vis_catalog
    vt = v["type"]
    if vt in NO_ALT_TYPES or vt == "?":
        return False
    if vt == "image":
        return True
    c = vis_catalog.core().get(vt)
    return True if c is None else bool(c.get("roles"))  # custom visuals count as data visuals


def reading_order(vis: list[dict]) -> list[str]:
    """Visual ids in reading order: rows top to bottom (a row holds tops within ROW_TOLERANCE),
    left to right inside a row; a group's members follow the group container, in their own order."""
    by_id = {v["id"]: v for v in vis}
    kids = collections.defaultdict(list)
    for v in vis:
        if v["group"] and v["group"] in by_id:
            kids[v["group"]].append(v)
    tops = [v for v in vis if not (v["group"] and v["group"] in by_id)]

    def rows(items):
        items = sorted(items, key=lambda v: (_num(v["pos"].get("y")), _num(v["pos"].get("x"))))
        out, row, top = [], [], None
        for v in items:
            y = _num(v["pos"].get("y"))
            if row and y - top > ROW_TOLERANCE:
                out += sorted(row, key=lambda w: _num(w["pos"].get("x")))
                row = []
            if not row:
                top = y
            row.append(v)
        return out + sorted(row, key=lambda w: _num(w["pos"].get("x")))

    order = []

    def emit(items, depth=0):
        for v in rows(items):
            order.append(v["id"])
            if v["id"] in kids and depth < 8:
                emit(kids[v["id"]], depth + 1)

    emit(tops)
    return order


def _hint_alt(page: str, vid: str) -> str:
    return f'pbz style "{page}" {vid} --set container.general.altText="<what the visual shows and why>"'


# ---------------------------------------------------------------- audit

def audit(ws, project, page: str | None = None, min_font: float = 9.0) -> dict:
    from . import style
    cat = style.catalog()
    findings = []

    def add(sev, code, pname, vid, msg, hint=""):
        rec = {"severity": sev, "code": code, "page": pname, "visual": vid, "message": msg}
        if hint:
            rec["hint"] = hint
        findings.append(rec)

    pages = _select_pages(project, page)
    n_vis = 0
    for pid, pname, pjson, pdir in pages:
        vis = _visuals(pdir)
        n_vis += len(vis)
        page_hidden = pjson.get("visibility") == "HiddenInViewMode" or (pjson.get("pageBinding") or {}).get("type") in ("Tooltip", "Drillthrough")
        for v in vis:
            body, vid = v["body"], v["id"]
            vco = body.get("visualContainerObjects") if isinstance(body.get("visualContainerObjects"), dict) else {}
            if _data_visual(v) and not v["hidden"]:
                alt = None
                for _o, e in _entries(body, "visualContainerObjects"):
                    if _o == "general" and "altText" in e["properties"]:
                        alt = e["properties"]["altText"]
                lit = _lit(alt) if alt is not None else None
                if alt is None or (isinstance(lit, str) and lit.strip("'").strip() == ""):
                    add("warning", "A11Y_ALT_TEXT_MISSING", pname, vid, f"{v['type']} has no alt text", _hint_alt(pname, vid))
                elif isinstance(lit, str):
                    text = lit[1:-1].replace("''", "'") if lit.startswith("'") and lit.endswith("'") else lit
                    if TOKEN_RE.fullmatch(text.strip()) or TOKEN_RE.fullmatch(lit.strip()):
                        add("warning", "A11Y_ALT_TEXT_TOKEN", pname, vid,
                            "alt text is a data value: describe the visual's purpose instead", _hint_alt(pname, vid))
                    elif len(text) > ALT_MAX:
                        add("warning", "A11Y_ALT_TEXT_LONG", pname, vid,
                            f"alt text is {len(text)} characters; Power BI keeps {ALT_MAX}", _hint_alt(pname, vid))
                title = (vco.get("title") or [{}])[0].get("properties", {}) if isinstance(vco.get("title"), list) and vco.get("title") else {}
                if _lit(title.get("show")) == "false":
                    add("warning" if alt is None else "info", "A11Y_TITLE_HIDDEN", pname, vid,
                        "the title is switched off: screen readers announce titles",
                        f'pbz style "{pname}" {vid} --set container.title.show=true')
            # font sizes typed by the catalog
            vobjs = cat["visuals"].get(v["type"]) or {}
            for section, objs in (("objects", vobjs), ("visualContainerObjects", cat["container"])):
                for obj, e in _entries(body, section):
                    for prop, val in e["properties"].items():
                        if (objs.get(obj) or {}).get(prop) != "fontSize":
                            continue
                        pt = _font_pt(val)
                        if pt is not None and pt < min_font:
                            pre = "container." if section == "visualContainerObjects" else ""
                            add("warning", "A11Y_SMALL_FONT", pname, vid, f"{pre}{obj}.{prop} is {pt:g}pt (below {min_font:g})",
                                f'pbz style "{pname}" {vid} --set {pre}{obj}.{prop}={max(min_font, 10):g}')
            # contrast of static colors in the same container
            vbg = None
            for obj, e in _entries(body, "visualContainerObjects"):
                if obj == "background" and _lit(e["properties"].get("show")) != "false":
                    vbg = _hex(e["properties"].get("color")) or vbg
            for section in ("objects", "visualContainerObjects"):
                objs = (vobjs if section == "objects" else cat["container"])
                for obj, e in _entries(body, section):
                    props = e["properties"]
                    types = objs.get(obj) or {}
                    texts = [p for p in props if types.get(p) == "color" and re.search(r"(font|text|label)?colou?r$", p, re.I)
                             and not re.search(r"back|fill|border|outline|grid|shadow|line", p, re.I)]
                    bgs = [p for p in props if types.get(p) == "color" and re.search(r"back|fill", p, re.I)]
                    bg = next((_hex(props[p]) for p in bgs if _hex(props[p])), None)
                    if bg is None and section == "visualContainerObjects" and obj in ("title", "subTitle"):
                        bg = vbg
                    big = _font_pt(props.get("fontSize")) or 0
                    need = 3.0 if big >= 18 or (big >= 14 and _lit(props.get("bold")) == "true") else 4.5
                    for p in texts:
                        fg = _hex(props[p])
                        if fg and bg:
                            ratio = contrast(fg, bg)
                            if ratio < need:
                                pre = "container." if section == "visualContainerObjects" else ""
                                add("warning", "A11Y_LOW_CONTRAST", pname, vid,
                                    f"{pre}{obj}.{p} on its background has contrast {ratio:.2f}:1 (needs {need:g}:1)",
                                    f'pbz style "{pname}" {vid} --set {pre}{obj}.{p}=#000000 (or a darker / lighter theme color)')
        # keyboard order
        shown = [v for v in vis if not v["hidden"]]
        with_tab = [v for v in shown if _num(v["pos"].get("tabOrder"), -1) >= 0]
        counts = collections.Counter(_num(v["pos"].get("tabOrder")) for v in with_tab)
        dups = sorted(v["id"] for v in with_tab if counts[_num(v["pos"].get("tabOrder"))] > 1)
        if dups:
            add("warning", "A11Y_TAB_ORDER_DUPLICATE", pname, "", f"{len(dups)} visuals share a tab order: {', '.join(dups[:8])}",
                f'pbz a11y --page "{pname}" --fix-tab-order')
        want = [i for i in reading_order(shown) if any(v["id"] == i for v in with_tab)]
        have = [v["id"] for v in sorted(with_tab, key=lambda v: (_num(v["pos"].get("tabOrder")), v["id"]))]
        if len(want) > 1 and want != have and not dups:
            moved = sum(1 for a, b in zip(want, have) if a != b)
            add("warning", "A11Y_TAB_ORDER", pname, "", f"keyboard order differs from reading order at {moved} position(s)",
                f'pbz a11y --page "{pname}" --fix-tab-order')
        for v in vis:
            if v["hidden"] and _num(v["pos"].get("tabOrder"), -1) >= 0:
                add("info", "A11Y_HIDDEN_IN_TAB_ORDER", pname, v["id"], "a hidden visual keeps a tab order position")
        if not page_hidden and vis and not any(os.path.exists(v["mobile"]) for v in vis):
            add("info", "A11Y_NO_MOBILE_LAYOUT", pname, "", "the page has no phone layout",
                f'pbz mobile "{pname}" all')
    summary = dict(sorted(collections.Counter(f["code"] for f in findings).items()))
    return {"verb": "a11y", "project": project.name, "pages": len(pages), "visuals": n_vis,
            "findings": findings, "summary": summary,
            "note": "metadata checks: screen-reader usability and rendered text fit still need a human pass (pbz handoff)"}


# ---------------------------------------------------------------- fixes

def fix_tab_order(ws, project, page: str) -> dict:
    """Rewrite position.tabOrder in reading order (visible visuals first, hidden ones after)."""
    _pid, pname, _pj, _pdir, vis = _mutation_page(project, page)
    shown = [v for v in vis if not v["hidden"]]
    hidden = [v for v in vis if v["hidden"]]
    order = reading_order(shown) + reading_order(hidden)
    by_id = {v["id"]: v for v in vis}
    changed, ops = [], []
    for i, vid in enumerate(order):
        v = by_id[vid]
        new = i * 1000
        if _num(v["pos"].get("tabOrder"), -1) == new:
            continue
        doc = json.loads(v["text"])
        doc.setdefault("position", {})["tabOrder"] = new
        ops.append(("write", v["path"], json.dumps(doc, indent=2, ensure_ascii=False) + "\n", v["bom"]))
        changed.append(vid)
    if ops:
        commit_bytes(ops)
    return {"verb": "a11y", "action": "fix-tab-order", "page": pname, "order": order, "changed": changed,
            "files": [os.path.relpath(by_id[i]["path"], ws.mirror).replace("\\", "/") for i in changed],
            **({"next": "pbz validate, then pbz push --reload"} if changed else {})}


def _mobile_version(project) -> str:
    """Most common mobile.json schema version in the report, else the oldest 2.x (read by every
    Desktop that supports PBIR phone layouts)."""
    seen = collections.Counter()
    for _pid, _n, _pj, pdir in _pages(project):
        for v in _visuals(pdir):
            if os.path.exists(v["mobile"]):
                try:
                    sch = json.loads(read_text(v["mobile"])[0]).get("$schema", "")
                except ValueError:
                    continue
                m = re.search(r"/visualContainerMobileState/(\d+\.\d+\.\d+)/schema\.json$", sch or "")
                if m:
                    seen[m.group(1)] += 1
    return seen.most_common(1)[0][0] if seen else "2.0.0"


def mobile(ws, project, page: str, visual: str, pos: str | None = None, remove: bool = False) -> dict:
    """Phone layout of one visual (`visual`) or of every visual on the page (`all`): `pos` x,y,w,h,
    or automatic placement (stacked full width below the visuals already laid out, in reading order);
    `remove` drops it."""
    _pid, pname, _pj, _pdir, vis = _mutation_page(project, page)
    by_id = {v["id"]: v for v in vis}
    if visual == "all":
        pick = [by_id[i] for i in reading_order([v for v in vis if not v["hidden"] and v["type"] != "group"])]
    else:
        from .layout import LABELS
        labels = {LABELS[i]: v for i, v in enumerate(vis) if i < len(LABELS)}
        v = by_id.get(visual) or labels.get(visual)
        if v is None:
            raise PbzError("NOT_FOUND", f"visual {visual} on page {pname} (ids and labels: `pbz layout --page`)")
        pick = [v]
    if pos and len(pick) != 1:
        raise PbzError("BAD_REQUEST", "--pos places one visual: name it")
    placed = _phone_pos(pos) if pos else None
    files, done = [], []
    if remove:
        ops = [("delete", v["mobile"]) for v in pick if os.path.isfile(v["mobile"])]
        if ops:
            commit_bytes(ops)
        for kind, path in ops:
            files.append(os.path.relpath(path, ws.mirror).replace("\\", "/"))
            done.append(os.path.basename(os.path.dirname(path)))
        return {"verb": "mobile", "page": pname, "removed": done, "files": files}
    ver = _mobile_version(project)
    bottom = 0.0
    for v in vis:
        if v["mobile_doc"] is not None and v not in pick:
            p = v["mobile_doc"].get("position") or {}
            bottom = max(bottom, _num(p.get("y")) + _num(p.get("height")))
    ops = []
    for v in pick:
        if placed:
            x, y, w, h = placed
        else:
            dw, dh = _num(v["pos"].get("width"), 300), _num(v["pos"].get("height"), 200)
            w = float(PHONE_WIDTH)
            h = float(min(480, max(60, round(PHONE_WIDTH * dh / dw) if dw > 0 else 200)))
            x, y = 0.0, bottom + (8 if bottom else 0)
            bottom = y + h
        doc = dict(v["mobile_doc"] or {})
        doc["$schema"] = doc.get("$schema") or f"{MOBILE_SCHEMA}/{ver}/schema.json"
        position = {"x": x, "y": y, "z": _num(v["pos"].get("z")), "width": w, "height": h}
        if _num(v["pos"].get("tabOrder"), -1) >= 0:
            position["tabOrder"] = _num(v["pos"].get("tabOrder"))
        doc["position"] = position
        ops.append(("write", v["mobile"], json.dumps(doc, indent=2, ensure_ascii=False) + "\n", b""))
        files.append(os.path.relpath(v["mobile"], ws.mirror).replace("\\", "/"))
        done.append({"visual": v["id"], "box": [int(x), int(y), int(w), int(h)]})
    if ops:
        commit_bytes(ops)
    return {"verb": "mobile", "page": pname, "placed": done, "files": files,
            "next": "pbz validate, then pbz push --reload; check the phone layout in Desktop (View > Mobile layout)"}
