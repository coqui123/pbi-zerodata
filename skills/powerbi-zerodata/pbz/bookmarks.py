"""`pbz bookmark`: bookmarks that capture a page and which of its visuals are shown or hidden, the
usual show/hide toggles behind buttons. They never capture data state (filters, slicer selections,
sorts): `suppressData` is set, so applying one keeps the reader's filters. Edits are mirror edits;
point a button at a bookmark with `pbz link <page> <button> --bookmark <name>`."""
from __future__ import annotations

import json
import os
import secrets

from .common import PbzError, commit_bytes, read_definition_text
from .tokens import TOKEN_RE

SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"


def _bdir(project) -> str:
    return os.path.join(project.mirror_report, "definition", "bookmarks")


def _meta(project) -> tuple[str, dict, bytes]:
    path = os.path.join(_bdir(project), "bookmarks.json")
    if os.path.exists(path):
        rel = os.path.relpath(path, project.ws.mirror).replace("\\", "/")
        try:
            text, bom = read_definition_text(path)
            meta = json.loads(text)
        except (OSError, ValueError, UnicodeDecodeError):
            raise PbzError("REFUSED", rel)
        if not isinstance(meta, dict):
            raise PbzError("REFUSED", rel)
        return path, meta, bom
    return path, {"$schema": f"{SCHEMA}/bookmarksMetadata/1.0.0/schema.json", "items": []}, b""


def _all(project) -> dict[str, dict]:
    out = {}
    d = _bdir(project)
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if f.endswith(".bookmark.json"):
            try:
                out[f[:-len(".bookmark.json")]] = json.loads(read_definition_text(os.path.join(d, f))[0])
            except ValueError:
                continue
    return out


def find(project, ref: str) -> str:
    for bid, b in _all(project).items():
        if ref.lower() in (bid.lower(), str(b.get("displayName", "")).lower()):
            return bid
    raise PbzError("NOT_FOUND", f"bookmark {ref}")


def add(ws, project, name: str, page: str, hide: list[str] | None = None, show: list[str] | None = None,
        only: list[str] | None = None) -> dict:
    """A bookmark of `page` with `hide` visuals hidden and every other visual shown (`show` makes a
    hidden one visible). `only`: the bookmark applies to these visuals alone."""
    from .author import _versions
    from .style import targets
    if not name.strip() or TOKEN_RE.search(name):
        raise PbzError("BAD_REQUEST", "a bookmark needs a plain display name")
    if any(str(b.get("displayName", "")).lower() == name.lower() for b in _all(project).values()):
        raise PbzError("NAME_TAKEN", f"bookmark {name}")
    vis = targets(project, page, None, None, True)
    if not vis:
        raise PbzError("BAD_REQUEST", f"page {page} has no visuals to show or hide")
    by_ref = {}
    for t in vis:
        by_ref[t["id"]] = t
        by_ref[t["label"]] = t

    def pick(refs):
        out = []
        for r in refs or []:
            if r not in by_ref:
                raise PbzError("NOT_FOUND", f"visual {r} on page {page} (ids and labels: `pbz layout --page`)")
            out.append(by_ref[r]["id"])
        return out

    hidden, shown, scope = set(pick(hide)), set(pick(show)), pick(only)
    if hidden & shown:
        raise PbzError("BAD_REQUEST", "a visual cannot be both hidden and shown")
    pid = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(vis[0]["path"]))))
    containers = {}
    for t in vis:
        doc = json.loads(read_definition_text(t["path"])[0])
        was_hidden = doc.get("isHidden") is True
        sv = {"visualType": t["type"]}
        if t["id"] in hidden or (was_hidden and t["id"] not in shown):
            sv["display"] = {"mode": "hidden"}
        containers[t["id"]] = {"singleVisual": sv}
    if scope:
        containers = {k: v for k, v in containers.items() if k in scope}
    ver = _versions(project.mirror_report).get("bookmark", "1.4.0")
    bid = "Bookmark" + secrets.token_hex(10)
    options = {"suppressData": True}
    if scope:
        options.update(applyOnlyToTargetVisuals=True, targetVisualNames=scope)
    bm = {"$schema": f"{SCHEMA}/bookmark/{ver}/schema.json", "displayName": name, "name": bid, "options": options,
          "explorationState": {"version": "1.3", "activeSection": pid, "sections": {pid: {"visualContainers": containers}}}}
    path = os.path.join(_bdir(project), bid + ".bookmark.json")
    mpath, meta, bom = _meta(project)
    meta.setdefault("items", []).append({"name": bid})
    commit_bytes([
        ("write", path, json.dumps(bm, indent=2, ensure_ascii=False) + "\n", b""),
        ("write", mpath, json.dumps(meta, indent=2, ensure_ascii=False) + "\n", bom),
    ])
    return {"verb": "bookmark", "action": "add", "name": bid, "display_name": name, "page": vis[0]["page"],
            "hidden": sorted(k for k, v in containers.items() if "display" in v["singleVisual"]),
            "files": [os.path.relpath(p, ws.mirror).replace("\\", "/") for p in (path, mpath)],
            "next": f"pbz link <page> <button> --bookmark \"{name}\", then pbz validate / pbz push --reload"}


def remove(ws, project, ref: str) -> dict:
    bid = find(project, ref)
    path = os.path.join(_bdir(project), bid + ".bookmark.json")
    mpath, meta, bom = _meta(project)
    items = []
    for it in meta.get("items") or []:
        if it.get("name") == bid:
            continue
        if isinstance(it.get("children"), list):
            it = dict(it, children=[c for c in it["children"] if c != bid])
        items.append(it)
    meta["items"] = items
    commit_bytes([
        ("delete", path),
        ("write", mpath, json.dumps(meta, indent=2, ensure_ascii=False) + "\n", bom),
    ])
    return {"verb": "bookmark", "action": "remove", "name": bid,
            "files": [os.path.relpath(p, ws.mirror).replace("\\", "/") for p in (path, mpath)],
            "next": "pbz validate (buttons that pointed at it are flagged), then pbz push --reload"}


def list_(ws, project) -> dict:
    rows = []
    for bid, b in _all(project).items():
        es = b.get("explorationState") or {}
        secs = es.get("sections") or {}
        hidden = sum(1 for s in secs.values() for c in (s.get("visualContainers") or {}).values()
                     if ((c.get("singleVisual") or {}).get("display") or {}).get("mode") == "hidden")
        opts = b.get("options") or {}
        rows.append({"name": bid, "display_name": b.get("displayName", ""), "page": es.get("activeSection"),
                     "hidden_visuals": hidden, "captures_data": not opts.get("suppressData", False),
                     **({"only": opts.get("targetVisualNames")} if opts.get("applyOnlyToTargetVisuals") else {})})
    return {"verb": "bookmark", "action": "list", "bookmarks": rows}
