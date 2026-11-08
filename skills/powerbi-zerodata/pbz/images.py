"""`pbz image`: report images (canvas background, wallpaper, image visuals, button, chart and card images).

An image is a file in <Report>/StaticResources/RegisteredResources/, registered in report.json
(`resourcePackages` -> package RegisteredResources, item type "Image", item name == path == file name)
and referenced from formatting with a ResourcePackageItem expression (PackageType 1):

    image visual       objects.general[].properties.imageUrl = {"expr": {"ResourcePackageItem": ...}}
    canvas, wallpaper  page.json objects.background / outspace: properties.image = {"image": {"name",
                       "url", "scaling"}} (nested twice; the flat form fails silently in Desktop)
    other images       visual properties of the image type (plotArea.image, fill.image, icon.image, ...)

The mirror never holds image files and pbz never prints image content. `pbz image add` checks the file
type from its header, copies the bytes into the private store and registers the item in the mirror's
report.json; `pbz push` installs staged images with the edit and deletes the files of images whose
registration was dropped (backed up first). Agents see item names, file kind, size and pixel size only:
an image can show data (a pasted chart, a screenshot), so nobody reads it through pbz.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import xml.etree.ElementTree as ET

from .common import PbzError, _contained, _reparse_link, commit_bytes, read_text, remove_file, replace_bytes, write_text

PACKAGE = "RegisteredResources"
PACKAGE_TYPE = 1
KINDS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".gif": "gif", ".bmp": "bmp", ".svg": "svg"}
ITEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _().-]{0,198}\.(png|jpe?g|gif|bmp|svg)$", re.IGNORECASE)
MAX_BYTES = 10 * 1024 * 1024
LARGE_BYTES = 1024 * 1024
PENDING = "pending-images"  # under the private store, per project: staged files until push
SCALING = {"fit": "Fit", "fill": "Fill", "normal": "Normal"}


# ---------------------------------------------------------------- files

def sniff(data: bytes) -> tuple[str, int | None, int | None]:
    """(kind, width, height) read from the file header. Content is never returned."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR":
        return "png", int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return "gif", int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:2] == b"BM" and len(data) >= 26:
        w = int.from_bytes(data[18:22], "little", signed=True)
        h = int.from_bytes(data[22:26], "little", signed=True)
        return "bmp", abs(w), abs(h)
    if data[:3] == b"\xff\xd8\xff":
        w, h = _jpeg_size(data)
        return "jpeg", w, h
    head = data[:2048].lstrip(b"\xef\xbb\xbf").lstrip()
    if head.startswith(b"<") and b"<svg" in data[:262144].lower():
        w, h = _svg_size(data)
        return "svg", w, h
    raise PbzError("BAD_IMAGE", "not a PNG, JPEG, GIF, BMP or SVG image")


def _jpeg_size(data: bytes) -> tuple[int | None, int | None]:
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # start of frame
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return None, None


def _svg_size(data: bytes) -> tuple[int | None, int | None]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise PbzError("BAD_IMAGE", "an SVG must be UTF-8 text")
    low = text.lower()
    if "<!doctype" in low or "<!entity" in low:
        raise PbzError("BAD_IMAGE", "an SVG with a DTD or entities is not accepted")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise PbzError("BAD_IMAGE", "the SVG is not well-formed XML")
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise PbzError("BAD_IMAGE", "the root element is not <svg>")
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1].lower() in ("script", "foreignobject"):
            raise PbzError("BAD_IMAGE", "an SVG with scripts or embedded HTML is not accepted")
        for k, v in el.attrib.items():
            if k.rsplit("}", 1)[-1].lower().startswith("on") or "javascript:" in v.lower():
                raise PbzError("BAD_IMAGE", "an SVG with event handlers or script links is not accepted")

    def num(v):
        m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(px)?\s*$", v or "")
        return round(float(m.group(1))) if m else None

    w, h = num(root.get("width")), num(root.get("height"))
    box = re.split(r"[\s,]+", (root.get("viewBox") or "").strip())
    if (w is None or h is None) and len(box) == 4:
        try:
            w, h = w or round(float(box[2])), h or round(float(box[3]))
        except ValueError:
            pass
    return w, h


def _rr_dir(report_dir: str) -> str:
    return os.path.join(report_dir, "StaticResources", PACKAGE)


def _pending_dir(ws, project) -> str:
    return os.path.join(ws.private, PENDING, project.name)


def _report_json(report_dir: str) -> str:
    return os.path.join(report_dir, "definition", "report.json")


def valid_item(name) -> bool:
    return isinstance(name, str) and bool(ITEM_RE.match(name)) and os.path.basename(name) == name and ".." not in name


def display_name(item: str) -> str:
    """The name Desktop shows for an item: the file name without the unique digits pbz / Desktop add."""
    return re.sub(r"\d{6,}(?=\.[^.]+$)", "", item) or item


def _file_of(ws, project, item: str) -> str | None:
    if not valid_item(item):
        return None
    for p in (os.path.join(_pending_dir(ws, project), item), os.path.join(_rr_dir(project.report_dir), item)):
        if os.path.isfile(p):
            return p
    return None


def present(ws, project, item: str) -> bool:
    """Staged for push or already in the real report."""
    return _file_of(ws, project, item) is not None


def pixels(ws, project, item: str) -> list[int] | None:
    p = _file_of(ws, project, item)
    return _facts(p).get("pixels") if p else None


def _sha(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _facts(path: str) -> dict:
    size = os.path.getsize(path)
    out = {"size_kib": max(1, round(size / 1024))}
    try:
        with open(path, "rb") as f:
            kind, w, h = sniff(f.read(MAX_BYTES + 1))
        out["kind"] = kind
        if w and h:
            out["pixels"] = [w, h]
    except PbzError:
        out["kind"] = "unreadable"
    return out


# ---------------------------------------------------------------- report.json

def _packages(data: dict) -> list:
    pk = data.get("resourcePackages")
    return pk if isinstance(pk, list) else []


def _rr_package(data: dict) -> dict | None:
    for p in _packages(data):
        if isinstance(p, dict) and PACKAGE in (p.get("name"), p.get("type")):
            return p
    return None


def registered_in(data) -> dict[str, dict]:
    """{item name: item} of the Image items of the RegisteredResources package (any name, valid or not)."""
    out: dict[str, dict] = {}
    p = _rr_package(data) if isinstance(data, dict) else None
    for it in (p or {}).get("items") or []:
        if isinstance(it, dict) and it.get("type") == "Image" and isinstance(it.get("name"), str):
            out[it["name"]] = it
    return out


def registered(report_json_text: str) -> dict[str, dict]:
    try:
        return registered_in(json.loads(report_json_text))
    except ValueError:
        return {}


def _parse_report(project):
    """report.json as data, or a closed refusal. Called before any pending-store change."""
    path = _report_json(project.mirror_report)
    rel = os.path.relpath(path, project.ws.mirror).replace("\\", "/")
    if not os.path.isfile(path):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    try:
        text, bom = read_text(path)
        data = json.loads(text)
    except (OSError, UnicodeError, ValueError):
        raise PbzError("REFUSED", rel) from None
    if not isinstance(data, dict):
        raise PbzError("REFUSED", rel)
    return path, rel, text, bom, data


def _render(text: str, data: dict) -> str:
    nl = "\r\n" if "\r\n" in text else "\n"
    new = json.dumps(data, indent=2, ensure_ascii=False)
    if nl != "\n":
        new = new.replace("\n", nl)
    if text.endswith("\n"):
        new += nl
    return new


def _edit_report(project, fn) -> str:
    path, _rel, text, bom, data = _parse_report(project)
    fn(data)
    write_text(path, _render(text, data), bom)
    return path


def _register(data: dict, item: str) -> None:
    p = _rr_package(data)
    if p is None:
        p = {"name": PACKAGE, "type": PACKAGE, "items": []}
        data.setdefault("resourcePackages", [])
        if not isinstance(data["resourcePackages"], list):
            raise PbzError("BAD_REQUEST", "resourcePackages in report.json is not an array")
        data["resourcePackages"].append(p)
    p.setdefault("items", []).append({"name": item, "path": item, "type": "Image"})


def _unregister(data: dict, item: str) -> None:
    p = _rr_package(data)
    if p is None:
        return
    p["items"] = [it for it in p.get("items") or [] if not (isinstance(it, dict) and it.get("name") == item
                                                             and it.get("type") == "Image")]
    if not p["items"]:
        data["resourcePackages"] = [q for q in _packages(data) if q is not p]
        if not data["resourcePackages"]:
            data.pop("resourcePackages")


# ---------------------------------------------------------------- references

def ref_expr(item: str) -> dict:
    return {"expr": {"ResourcePackageItem": {"PackageName": PACKAGE, "PackageType": PACKAGE_TYPE, "ItemName": item}}}


def composite(item: str, scaling: str | None = None) -> dict:
    """Value of an image-type formatting property: {"image": {name, url[, scaling]}}."""
    img = {"name": {"expr": {"Literal": {"Value": "'" + display_name(item).replace("'", "''") + "'"}}},
           "url": ref_expr(item)}
    if scaling:
        img["scaling"] = {"expr": {"Literal": {"Value": f"'{scaling}'"}}}
    return {"image": img}


def item_of(value) -> str | None:
    """ItemName of a stored image-type or imageUrl property value, else None."""
    for path, rpi in _rpis(value, []):
        return rpi.get("ItemName")
    return None


def _rpis(node, path):
    if isinstance(node, dict):
        rpi = node.get("ResourcePackageItem")
        if isinstance(rpi, dict):
            yield path, rpi
        for k, v in node.items():
            if k != "ResourcePackageItem":
                yield from _rpis(v, path + [k])
    elif isinstance(node, list):
        for v in node:
            yield from _rpis(v, path)


def _in_objects(objects):
    """(object, property, path below the property, ResourcePackageItem) in a PBIR objects map."""
    for obj, entries in (objects.items() if isinstance(objects, dict) else []):
        for e in entries if isinstance(entries, list) else []:
            props = e.get("properties") if isinstance(e, dict) else None
            for prop, val in (props.items() if isinstance(props, dict) else []):
                for path, rpi in _rpis(val, []):
                    yield obj, prop, path, rpi


def scan(report_dir: str) -> list[dict]:
    """Every ResourcePackageItem reference in the report definition: item, where (readable), file,
    package (name, type) and whether an image-type property is written flat (url at the top level)."""
    out: list[dict] = []
    ddir = os.path.join(report_dir, "definition")
    pdir = os.path.join(ddir, "pages")
    seen_files: set[str] = set()
    for pid in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
        pj = os.path.join(pdir, pid, "page.json")
        page = _load(pj)
        seen_files.add(os.path.normcase(pj))
        pname = (page or {}).get("displayName") or pid
        for obj, prop, path, rpi in _in_objects((page or {}).get("objects")):
            label = {"background": "canvas background", "outspace": "wallpaper"}.get(obj, obj)
            out.append(_ref(rpi, f"page '{pname}' {label}" + ("" if prop == "image" else f" {prop}"),
                            f"definition/pages/{pid}/page.json", path))
        vdir = os.path.join(pdir, pid, "visuals")
        for vid in sorted(os.listdir(vdir)) if os.path.isdir(vdir) else []:
            vj = os.path.join(vdir, vid, "visual.json")
            doc = _load(vj)
            seen_files.add(os.path.normcase(vj))
            body = (doc or {}).get("visual") if isinstance((doc or {}).get("visual"), dict) else {}
            vt = body.get("visualType", "?")
            for section, pre in (("objects", ""), ("visualContainerObjects", "container.")):
                for obj, prop, path, rpi in _in_objects(body.get(section)):
                    out.append(_ref(rpi, f"page '{pname}' visual {vid} ({vt}) {pre}{obj}.{prop}",
                                    f"definition/pages/{pid}/visuals/{vid}/visual.json", path))
    for cur, _dirs, files in os.walk(ddir):  # bookmarks, report.json, extensions: anything else
        for f in sorted(files):
            p = os.path.join(cur, f)
            if not f.lower().endswith(".json") or os.path.normcase(p) in seen_files:
                continue
            rel = os.path.relpath(p, report_dir).replace("\\", "/")
            for path, rpi in _rpis(_load(p), []):
                out.append(_ref(rpi, rel, rel, None))
    return out


def _ref(rpi: dict, where: str, file: str, path) -> dict:
    return {"item": rpi.get("ItemName"), "where": where, "file": file, "package": rpi.get("PackageName"),
            "package_type": rpi.get("PackageType"), "flat": path is not None and path[:1] == ["url"]}


def _load(path: str):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _strict_definition(report_dir: str, mirror: str | None = None) -> None:
    """Every definition JSON must parse before a removal can trust `uses`."""
    ddir = os.path.join(report_dir, "definition")
    root = mirror or os.path.dirname(report_dir)
    rel_dir = os.path.relpath(ddir, root).replace("\\", "/")
    if not os.path.isdir(ddir):
        raise PbzError("REFUSED", rel_dir)
    for cur, _dirs, files in os.walk(ddir):
        for name in files:
            if not name.lower().endswith(".json"):
                continue
            path = os.path.join(cur, name)
            rel = os.path.relpath(path, root).replace("\\", "/")
            try:
                json.loads(read_text(path)[0])
            except (OSError, UnicodeError, ValueError):
                raise PbzError("REFUSED", rel) from None


def uses(report_dir: str, *, strict: bool = False, mirror: str | None = None) -> dict[str, list[str]]:
    if strict:
        _strict_definition(report_dir, mirror)
    out: dict[str, list[str]] = {}
    for r in scan(report_dir):
        if r["package"] == PACKAGE and isinstance(r["item"], str):
            out.setdefault(r["item"], []).append(r["where"])
    return out


def resolve(ws, project, raw: str) -> str:
    """A registered image item from what the agent typed: the item name or `image:<item>`."""
    name = raw.strip()
    if name.lower().startswith("image:"):
        name = name[6:].strip()
    text = read_text(_report_json(project.mirror_report))[0] if os.path.exists(_report_json(project.mirror_report)) else ""
    reg = registered(text)
    if name in reg:
        return name
    hits = [i for i in reg if display_name(i).lower() == name.lower()]
    if len(hits) == 1:
        return hits[0]
    avail = ", ".join(sorted(reg)[:12]) or "none (pbz image add <file>)"
    raise PbzError("NOT_FOUND", f"image {name} is not registered; registered images: {avail}")


# ---------------------------------------------------------------- verbs

def _stem(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_-")[:40]
    return s or "image"


def add(ws, project, path: str, name: str | None = None) -> dict:
    if not os.path.isdir(project.mirror_report):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    src = os.path.abspath(path)
    if not os.path.isfile(src):
        raise PbzError("NOT_FOUND", "no image file at that path")
    ext = os.path.splitext(src)[1].lower()
    if ext not in KINDS:
        raise PbzError("BAD_IMAGE", "use a .png, .jpg, .jpeg, .gif, .bmp or .svg file")
    size = os.path.getsize(src)
    if size > MAX_BYTES:
        raise PbzError("TOO_LARGE", f"{round(size / 1048576, 1)} MiB; the limit is {MAX_BYTES // 1048576} MiB "
                                    "(every viewer downloads it): compress or resize the image")
    with open(src, "rb") as f:
        data = f.read()
    kind, w, h = sniff(data)
    if KINDS[ext] != kind:
        raise PbzError("BAD_IMAGE", f"the file extension says {KINDS[ext]} but the content is {kind}")
    rjson, rel, text, bom, doc = _parse_report(project)
    reg = registered_in(doc)
    digest = hashlib.sha256(data).hexdigest()
    facts = {"kind": kind, "size_kib": max(1, round(size / 1024)), **({"pixels": [w, h]} if w and h else {})}
    for item in sorted(reg):
        p = _file_of(ws, project, item)
        if p and _sha(p) == digest:
            return {"verb": "image", "action": "add", "result": "PRESENT", "item": item, **facts, "files": [],
                    "next": _next(item)}
    stem = _stem(os.path.splitext(name)[0] if name else os.path.splitext(os.path.basename(src))[0])
    rr = _rr_dir(project.report_dir)
    item = ""
    for _ in range(20):
        item = f"{stem}{secrets.randbelow(10 ** 15):015d}{ext}"
        if item not in reg and not os.path.exists(os.path.join(rr, item)):
            break
    pend = _pending_dir(ws, project)
    ops: list[tuple] = []
    if os.path.isdir(pend):
        for fname in os.listdir(pend):  # staged files whose registration was reverted
            if fname not in reg:
                ops.append(("delete", os.path.join(pend, fname)))
    ops.append(("write_bytes", os.path.join(pend, item), data))
    _register(doc, item)
    ops.append(("write", rjson, _render(text, doc), bom))
    _commit(ws, project, ops)
    out = {"verb": "image", "action": "add", "result": "STAGED", "item": item, "display_name": display_name(item),
           **facts, "files": [rel], "next": _next(item)}
    if size > LARGE_BYTES:
        out["warning"] = "large image: every report viewer downloads it; compress it if you can"
    return out


def _commit(ws, project, ops: list[tuple]) -> None:
    """Private pending files and mirror report.json each stay inside their own root."""
    roots = {}
    private = os.path.abspath(ws.private)
    home = os.path.abspath(project.root)
    for op in ops:
        path = os.path.abspath(op[1])
        roots[op[1]] = ws.private if path == private or path.startswith(private + os.sep) else home
    commit_bytes(ops, private=True, roots=roots)


def _next(item: str) -> str:
    return (f'use it: pbz style "<page>" --canvas --set background.image={item} (or wallpaper.image=...), '
            f"pbz add visual image --page <page> --image {item}, or an image property of a visual "
            f"(pbz style <page> <visual> --set plotArea.image={item}); then pbz validate / pbz push")


def list_(ws, project) -> dict:
    rjson = _report_json(project.mirror_report)
    if not os.path.exists(rjson):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    reg = registered(read_text(rjson)[0])
    used = uses(project.mirror_report)
    rows = []
    for item in sorted(reg):
        p = _file_of(ws, project, item)
        staged = p is not None and os.path.dirname(p) == _pending_dir(ws, project)
        row = {"item": item, "status": "staged" if staged else "present" if p else "missing"}
        if not valid_item(item):
            row["status"] = "bad_name"
        elif p:
            row.update(_facts(p))
        row["used_by"] = used.get(item, [])
        rows.append(row)
    real_rj = _report_json(project.report_dir)
    real_reg = registered(read_text(real_rj)[0]) if os.path.exists(real_rj) else {}
    rr = _rr_dir(project.report_dir)
    files = sorted(f for f in (os.listdir(rr) if os.path.isdir(rr) else []) if os.path.splitext(f)[1].lower() in KINDS)
    out = {"verb": "image", "action": "list", "project": project.name, "images": rows}
    drop = sorted(set(real_reg) - set(reg))
    if drop:
        out["removed_on_push"] = drop
    orphans = [f for f in files if f not in reg and f not in real_reg]
    if orphans:
        out["unregistered_files"] = orphans
    unknown = sorted(i for i in used if i not in reg)
    if unknown:
        out["unregistered_references"] = {i: used[i] for i in unknown}
    return out


def remove(ws, project, item: str) -> dict:
    rjson, rel, text, bom, doc = _parse_report(project)
    item = resolve(ws, project, item)
    where = uses(project.mirror_report, strict=True, mirror=ws.mirror).get(item)
    if where:
        raise PbzError("IN_USE", f"{item} is used by: {'; '.join(where[:8])}"
                                 f"{' ...' if len(where) > 8 else ''}. Unset or delete those first "
                                 "(pbz style ... --unset <object>.image, or delete the image visual)")
    _unregister(doc, item)
    ops: list[tuple] = [("write", rjson, _render(text, doc), bom)]
    pend = os.path.join(_pending_dir(ws, project), item)
    was_staged = os.path.isfile(pend)
    if was_staged:
        ops.append(("delete", pend))
    _commit(ws, project, ops)
    out = {"verb": "image", "action": "remove", "item": item, "files": [rel]}
    out["next"] = ("unstaged; nothing to push for the file" if was_staged else
                   "pbz push deletes the file from the report (backed up with the push)")
    return out


# ---------------------------------------------------------------- push support

def staged(ws, project, report_json_text: str) -> dict[str, str]:
    """{item: staged file} for staged images that this report.json registers."""
    d = _pending_dir(ws, project)
    return {i: os.path.join(d, i) for i in sorted(registered(report_json_text))
            if valid_item(i) and os.path.isfile(os.path.join(d, i))}


def dropped(old_text: str, new_text: str) -> list[str]:
    """Images registered before and not any more: their files leave the report with the push."""
    return sorted(i for i in set(registered(old_text)) - set(registered(new_text)) if valid_item(i))


def _preflight(path: str, root: str | None) -> None:
    """Refuse a link or a junction below `root` before any byte of `path` is read."""
    if _reparse_link(path):
        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    _contained(path, root)


def install(files: dict[str, str], report_dir: str, backup_dir: str | None = None, *,
            root: str | None = None) -> list[str]:
    rr = _rr_dir(report_dir)
    planned = []
    for item, src in sorted(files.items()):
        if not valid_item(item):
            continue
        dst = os.path.join(rr, item)
        bdst = os.path.join(backup_dir, "StaticResources", PACKAGE, item) if backup_dir else None
        _preflight(dst, root)
        _preflight(src, root)
        if bdst is not None:
            _preflight(bdst, root)
        planned.append((item, src, dst, bdst))
    done = []
    for item, src, dst, bdst in planned:
        _preflight(dst, root)
        _preflight(src, root)
        exists = os.path.lexists(dst)
        if exists:
            _preflight(dst, root)
            info = os.lstat(dst)
            if not stat.S_ISREG(info.st_mode) or _reparse_link(dst):
                raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        if exists and bdst is not None:
            _preflight(bdst, root)
            os.makedirs(os.path.dirname(bdst), exist_ok=True)
            with open(dst, "rb") as f:
                payload = f.read()
            replace_bytes(bdst, payload, root=root)
        elif not exists:
            os.makedirs(rr, exist_ok=True)
        with open(src, "rb") as f:
            payload = f.read()
        replace_bytes(dst, payload, root=root)
        done.append(item)
    return done


def delete(items: list[str], report_dir: str, backup_dir: str | None = None, *,
           root: str | None = None) -> list[str]:
    rr = _rr_dir(report_dir)
    planned = []
    for item in items:
        if not valid_item(item):
            continue
        p = os.path.join(rr, item)
        bdst = os.path.join(backup_dir, "StaticResources", PACKAGE, item) if backup_dir else None
        _preflight(p, root)
        if bdst is not None:
            _preflight(bdst, root)
        if not os.path.lexists(p):
            continue
        if _reparse_link(p):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
        info = os.lstat(p)
        if not stat.S_ISREG(info.st_mode):
            continue
        planned.append((item, p, bdst))
    done = []
    for item, p, bdst in planned:
        _preflight(p, root)
        if bdst is not None:
            _preflight(bdst, root)
            os.makedirs(os.path.dirname(bdst), exist_ok=True)
            with open(p, "rb") as f:
                payload = f.read()
            replace_bytes(bdst, payload, root=root)
        remove_file(p, root=root)
        done.append(item)
    return done


def clear_pending(ws, project, items) -> None:
    for i in items:
        p = os.path.join(_pending_dir(ws, project), i)
        if valid_item(i) and os.path.isfile(p):
            remove_file(p, root=_pending_dir(ws, project))


# ---------------------------------------------------------------- checks (pbircheck)

def check(add, report_dir: str, has_image=None) -> None:
    """Registration and references of images, as Desktop needs them."""
    data = _load(_report_json(report_dir))
    reg = registered_in(data) if isinstance(data, dict) else {}
    for name, it in sorted(reg.items()):
        if not valid_item(name):
            add("error", "PBIR_IMAGE_NAME", "definition/report.json",
                f"image item {name!r} needs a plain file name with a .png, .jpg, .jpeg, .gif, .bmp or .svg extension")
            continue
        if it.get("path") != name:
            add("error", "PBIR_IMAGE_PATH", "definition/report.json",
                f"image item {name}: name and path must both be the file name")
        if has_image is not None and not has_image(name):
            add("error", "PBIR_IMAGE_MISSING", "definition/report.json",
                f"image {name} is registered but its file is neither in the report nor staged (pbz image add)")
    for r in scan(report_dir):
        if r["package"] != PACKAGE:
            continue
        if r["item"] not in reg:
            add("error", "PBIR_IMAGE_NOT_REGISTERED", r["file"],
                f"{r['where']} uses image {r['item']}, which report.json does not register (pbz image list)")
        if r["package_type"] != PACKAGE_TYPE:
            add("error", "PBIR_IMAGE_PACKAGE_TYPE", r["file"], f"{r['where']}: ResourcePackageItem PackageType must be 1")
        if r["flat"]:
            add("error", "PBIR_IMAGE_FLAT", r["file"],
                f"{r['where']}: an image property nests as image.image.{{name,url,scaling}}; the flat form shows "
                "nothing in Desktop (use pbz style ... --set <object>.image=<item>)")
