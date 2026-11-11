"""`pbz layout`: see a report page without seeing it. A text canvas map of every visual's box plus layout
lint (overlaps, off-canvas, tiny or zero-size visuals, duplicate z / tab order). Reads the mirror PBIR
only: positions, types, field names. No values, no rendering."""
from __future__ import annotations

import os

from . import pbir
from .common import PbzError, read_definition_text

COLS, ROWS = 96, 30
LABELS = "123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
OVERLAY_TYPES = {"shape", "basicShape", "image", "actionButton", "textbox", "group"}  # often layered on purpose


def _load(path):
    try:
        return pbir.parse_json(read_definition_text(path)[0])
    except PbzError:
        raise
    except (OSError, UnicodeDecodeError, pbir.JsonError):
        return None


def _num(node, key, default=0.0):
    v = node.get(key) if isinstance(node, pbir.JObj) else None
    try:
        return float(v.raw) if isinstance(v, pbir.JLit) else default
    except ValueError:
        return default


def _s(node) -> str:
    return node.value if isinstance(node, pbir.JStr) else ""


def _visuals(page_dir: str) -> list[dict]:
    out = []
    vdir = os.path.join(page_dir, "visuals")
    for vid in sorted(os.listdir(vdir)) if os.path.isdir(vdir) else []:
        root = _load(os.path.join(vdir, vid, "visual.json"))
        if root is None:
            continue
        pos = root.get("position")
        v = root.get("visual")
        vt = _s(v.get("visualType")) if isinstance(v, pbir.JObj) else ("group" if "visualGroup" in root else "?")
        hidden = isinstance(root.get("isHidden"), pbir.JLit) and root.get("isHidden").raw == "true"
        fields = []
        q = v.get("query") if isinstance(v, pbir.JObj) else None
        qs = q.get("queryState") if isinstance(q, pbir.JObj) else None
        for role, proj in (qs.pairs if isinstance(qs, pbir.JObj) else []):
            for f in pbir.walk_fields(proj):
                label = f"{f.table}[{f.name}]" if f.kind != "measure" else f"[{f.name}]"
                fields.append(f"{role.value}: {label}")
        out.append({"id": vid, "type": vt, "x": _num(pos, "x"), "y": _num(pos, "y"), "w": _num(pos, "width"),
                    "h": _num(pos, "height"), "z": _num(pos, "z"), "tab": _num(pos, "tabOrder", -1),
                    "hidden": hidden, "group": _s(root.get("parentGroupName")), "fields": fields})
    return out


def _canvas(vis: list[dict], width: float, height: float) -> list[str]:
    grid = [[" "] * COLS for _ in range(ROWS)]
    sx, sy = COLS / max(width, 1), ROWS / max(height, 1)
    for i, v in sorted(enumerate(vis), key=lambda iv: iv[1]["z"]):
        if v["type"] == "group" or v["hidden"]:
            continue
        x0, y0 = int(v["x"] * sx), int(v["y"] * sy)
        x1, y1 = max(x0 + 1, int((v["x"] + v["w"]) * sx) - 1), max(y0 + 1, int((v["y"] + v["h"]) * sy) - 1)
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(COLS - 1, x1), min(ROWS - 1, y1)
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                edge_x, edge_y = x in (x0, x1), y in (y0, y1)
                grid[y][x] = "+" if edge_x and edge_y else ("-" if edge_y else ("|" if edge_x else " "))
        lab = LABELS[i] if i < len(LABELS) else "#"
        if x0 + 1 <= x1 and y0 + 1 <= y1:
            grid[min(y0 + 1, y1)][min(x0 + 1, x1)] = lab
        else:
            grid[y0][x0] = lab
    border = "+" + "-" * COLS + "+"
    return [border] + ["|" + "".join(r) + "|" for r in grid] + [border]


def _lint(vis: list[dict], width: float, height: float, page: str) -> list[dict]:
    out = []
    shown = [v for v in vis if v["type"] != "group" and not v["hidden"]]
    for v in vis:
        if v["w"] <= 0 or v["h"] <= 0:
            out.append({"code": "ZERO_SIZE", "visual": v["id"]})
        elif v["type"] != "group" and (v["w"] < 20 or v["h"] < 20):
            out.append({"code": "TINY", "visual": v["id"]})
        if v["x"] < 0 or v["y"] < 0 or v["x"] + v["w"] > width + 1 or v["y"] + v["h"] > height + 1:
            out.append({"code": "OFF_CANVAS", "visual": v["id"]})
    for i, a in enumerate(shown):
        for b in shown[i + 1:]:
            if a["group"] and a["group"] == b["group"]:
                continue
            ix = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
            iy = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
            if ix > 4 and iy > 4 and a["type"] not in OVERLAY_TYPES and b["type"] not in OVERLAY_TYPES:
                share = ix * iy / max(1.0, min(a["w"] * a["h"], b["w"] * b["h"]))
                if share >= 0.15:
                    out.append({"code": "OVERLAP", "visuals": [a["id"], b["id"]], "share": "most" if share >= 0.5 else "part"})
    for key, code in (("z", "DUPLICATE_Z"), ("tab", "DUPLICATE_TAB_ORDER")):
        seen: dict = {}
        for v in shown:
            if v[key] >= 0:
                seen.setdefault(v[key], []).append(v["id"])
        out += [{"code": code, "visuals": ids} for ids in seen.values() if len(ids) > 1]
    for f in out:
        f["page"] = page
    return out


def _page_segment(name: str) -> bool:
    """True when a page id is one path segment. Traversal and nested ids are ignored."""
    if not isinstance(name, str) or not name or name in (".", ".."):
        return False
    if any(c in name for c in "/\\\x00"):
        return False
    return os.path.basename(name) == name


def run(ws, project, page: str | None = None, show_map: bool = True) -> dict:
    pages_dir = os.path.join(project.mirror_report, "definition", "pages")
    if not os.path.isdir(pages_dir):
        raise PbzError("NO_MIRROR", "run `pbz pull` first (no report pages in the mirror)")
    meta = _load(os.path.join(pages_dir, "pages.json"))
    order = [_s(x) for x in meta.get("pageOrder").items] if isinstance(meta, pbir.JObj) and isinstance(meta.get("pageOrder"), pbir.JArr) else []
    order = [p for p in order if _page_segment(p)]
    ids = order + [p for p in sorted(os.listdir(pages_dir))
                   if _page_segment(p) and os.path.isdir(os.path.join(pages_dir, p)) and p not in order]
    out_pages, findings = [], []
    for pid in ids:
        proot = _load(os.path.join(pages_dir, pid, "page.json"))
        if proot is None:
            continue
        name = _s(proot.get("displayName")) or pid
        if page and page.lower() not in (pid.lower(), name.lower()):
            continue
        width, height = _num(proot, "width", 1280.0), _num(proot, "height", 720.0)
        vis = _visuals(os.path.join(pages_dir, pid))
        findings += _lint(vis, width, height, name)
        rec = {"page": name, "id": pid, "size": [int(width), int(height)],
               "hidden": _s(proot.get("visibility")) == "HiddenInViewMode",
               "visuals": [{"label": LABELS[i] if i < len(LABELS) else "#", "id": v["id"], "type": v["type"],
                            "box": [int(v["x"]), int(v["y"]), int(v["w"]), int(v["h"])], "z": int(v["z"]),
                            **({"hidden": True} if v["hidden"] else {}), **({"group": v["group"]} if v["group"] else {}),
                            **({"fields": v["fields"][:12]} if v["fields"] else {})} for i, v in enumerate(vis)]}
        if show_map and (page or len(ids) == 1):
            rec["map"] = _canvas(vis, width, height)
        out_pages.append(rec)
    if page and not out_pages:
        raise PbzError("NOT_FOUND", f"page {page}")
    return {"verb": "layout", "project": project.name, "pages": out_pages, "findings": findings,
            "note": "map shows visible visuals only; pass --page to draw one page"}
