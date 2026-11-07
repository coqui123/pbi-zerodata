"""`pbz style`: visual formatting in the mirror, checked against Microsoft's formatting catalog.

The catalog (rules/visual-objects.json, compacted from @microsoft/powerbi-core-visual-schema by
install/build_visual_catalog.py) lists every formatting object and property of the built-in visuals
(`visual.objects`) and of the visual container (`visual.visualContainerObjects`: title, subTitle,
background, border, dropShadow, padding, visualHeader, ...), with value types, enumeration members
and the instance states some objects support (button hover, slicer pressed, ...).

Values are authored formatting only: flags, numbers, colors (hex, theme colors), enumeration members,
text you type, and field values from a measure. Selectors are limited to a series of the visual
(`--series`, metadata selector) and an instance state (`--state`); data-point selectors carry data
values and are never written. Edits are mirror edits: `pbz validate`, then `pbz push --reload`.
"""
from __future__ import annotations

import difflib
import json
import os
import re

from . import pbir, tmdl
from .common import PbzError, commit_bytes, read_text, write_text
from .tokens import TOKEN_RE

_CAT: dict | None = None
UNITS = {"auto": 0, "none": 1, "thousands": 1000, "millions": 1000000, "billions": 1000000000,
         "trillions": 1000000000000}
BOOL = {"true": True, "on": True, "yes": True, "false": False, "off": False, "no": False}
SUPPORTED = ("bool", "numeric", "integer", "text", "color", "fontSize", "fontFamily", "alignment",
             "displayUnits", "formatString", "dateTime", "variant")


# ---------------------------------------------------------------- catalog

def prop_code(t):
    """Compact value type of a capabilities property type descriptor."""
    if not isinstance(t, dict):
        return "other"
    for k in ("bool", "numeric", "integer", "text", "dateTime", "variant"):
        if t.get(k):
            return k
    if isinstance(t.get("image"), dict):  # {"image": {"subProperties": {name, url, scaling}}}
        return "image"
    if isinstance(t.get("misc"), dict) and t["misc"].get("imageUrl"):
        return "imageUrl"
    if isinstance(t.get("fill"), dict) and "solid" in t["fill"]:
        return "color"
    e = t.get("enumeration")
    if isinstance(e, dict):
        return [m.get("value") for m in e.get("allMembers") or [] if isinstance(m, dict)]
    f = t.get("formatting")
    if isinstance(f, dict):
        for k, c in (("fontSize", "fontSize"), ("fontFamily", "fontFamily"), ("alignment", "alignment"),
                     ("labelDisplayUnits", "displayUnits"), ("displayUnitsWithoutAuto", "displayUnits"),
                     ("formatString", "formatString"), ("altText", "text")):
            if f.get(k):
                return c
    return "other"


def compact_objects(objects: dict) -> dict:
    out = {}
    for on, ob in sorted((objects or {}).items()):
        if on.startswith("_") or not isinstance(ob, dict):
            continue
        out[on] = {pn: prop_code(p.get("type") if isinstance(p, dict) else None)
                   for pn, p in sorted((ob.get("properties") or {}).items())}
    return out


def catalog() -> dict:
    global _CAT
    if _CAT is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules", "visual-objects.json")
        with open(path, encoding="utf-8") as f:
            _CAT = json.load(f)
    return _CAT


def _states(vt: str) -> dict:
    st = catalog().get("states", {})
    return st.get(vt) or st.get(vt[:-6] if vt.endswith("Visual") else vt + "Visual") or {}


def _objects_for(ws, project, vt: str) -> tuple[dict | None, str]:
    """(visual objects {obj: {prop: code}}, source) for a type: the built-in catalog, or the object
    names of a custom visual's contract (types unknown there)."""
    cat = catalog()["visuals"]
    if vt in cat:
        return cat[vt], "catalog"
    from . import visuals
    c, source = visuals.load_contract(ws, project, vt)
    objs = (c or {}).get("objects")
    if objs:
        return {o: {p: "auto" for p in props} for o, props in objs.items()}, "contract"
    return None, "none"


# ---------------------------------------------------------------- values

def _lit(value: str) -> dict:
    return {"expr": {"Literal": {"Value": value}}}


def _num(raw: str) -> str:
    try:
        v = float(raw)
    except ValueError:
        raise PbzError("BAD_REQUEST", f"{raw} is not a number")
    return (str(int(v)) if v.is_integer() and abs(v) < 1e15 else repr(v)) + "D"


def _text(raw: str) -> str:
    if TOKEN_RE.search(raw):
        raise PbzError("BAD_REQUEST", "mirror tokens cannot be used in formatting values")
    return "'" + raw.replace("'", "''") + "'"


def _measure_expr(model, ref: str) -> dict:
    m = re.match(r"^\s*(?:'((?:[^']|'')+)'|([^\[\]']*?))?\s*\[(.+)\]\s*$", ref)
    meas = model.measure(m.group(3)) if m else None
    if meas is None:
        raise PbzError("NOT_FOUND", f"measure {ref} (field values come from a measure)")
    return {"expr": {"Measure": {"Expression": {"SourceRef": {"Entity": meas.table}}, "Property": meas.name}}}


def _color(raw: str, model) -> dict:
    low = raw.strip().lower()
    if low.startswith("measure:"):
        return {"solid": {"color": _measure_expr(model, raw.strip()[8:])}}
    m = re.match(r"^theme:(\d+)(?::(-?\d+(?:\.\d+)?))?$", low)
    if m:  # Percent is a shade fraction: -0.25 is 25% darker, 0.4 is 40% lighter
        pct = float(m.group(2) or 0)
        pct = pct / 100 if abs(pct) > 1 else pct
        if abs(pct) > 1:
            raise PbzError("BAD_REQUEST", "theme color shade is -100..100 percent")
        return {"solid": {"color": {"expr": {"ThemeDataColor": {"ColorId": int(m.group(1)),
                                                                 "Percent": int(pct) if pct.is_integer() else round(pct, 4)}}}}}
    m = re.match(r"^#?([0-9a-f]{3}|[0-9a-f]{6})$", low)
    if not m:
        raise PbzError("BAD_REQUEST", f"{raw}: a color is #RRGGBB, theme:<n>[:<percent>] or measure:[Measure]")
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return {"solid": {"color": _lit("'#" + h.upper() + "'")}}


def encode(code, raw: str, model, image_ref=None) -> dict:
    """Formatting property value for one catalog type. `image_ref(raw)` resolves a registered image item."""
    raw = raw.strip()
    if code in ("image", "imageUrl"):
        from . import images
        if code == "imageUrl" and re.match(r"(?i)^https?://", raw):
            return _lit(_text(raw))  # a public web image (it must load without sign-in)
        if image_ref is None:
            raise PbzError("UNSUPPORTED", "image properties need the image registry (pbz style)")
        item = image_ref(raw)
        return images.composite(item) if code == "image" else images.ref_expr(item)
    if code == "auto":  # custom visual object: no type information, infer from the value
        code = ("bool" if raw.lower() in BOOL else "color" if re.match(r"^#[0-9a-fA-F]{3,6}$|^theme:", raw)
                else "numeric" if re.match(r"^-?\d+(\.\d+)?$", raw) else "text")
    if isinstance(code, list):
        want = str(UNITS[raw.lower()]) if raw.lower() in UNITS and any(isinstance(v, int) for v in code) else raw
        hit = next((v for v in code if str(v).lower() == want.lower()), None)
        if hit is None:
            raise PbzError("BAD_REQUEST", f"{raw}: allowed values {', '.join(str(v) for v in code)}")
        return _lit(_text(hit) if isinstance(hit, str) else _num(str(hit)))
    if code == "bool":
        if raw.lower() not in BOOL:
            raise PbzError("BAD_REQUEST", f"{raw}: use true or false")
        return _lit("true" if BOOL[raw.lower()] else "false")
    if code in ("numeric", "fontSize", "variant"):
        return _lit(_num(raw))
    if code == "displayUnits":
        return _lit(_num(str(UNITS.get(raw.lower(), raw))))
    if code == "integer":
        try:
            return _lit(f"{int(float(raw))}L")
        except ValueError:
            raise PbzError("BAD_REQUEST", f"{raw} is not a whole number")
    if code == "color":
        return _color(raw, model)
    if code in ("text", "fontFamily", "formatString"):
        if raw.lower().startswith("measure:") and code == "text":
            return _measure_expr(model, raw[8:])
        return _lit(_text(raw))
    if code == "alignment":
        return _lit(_text(raw.lower()))
    if code == "dateTime":
        if not re.match(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?$", raw):
            raise PbzError("BAD_REQUEST", f"{raw}: use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")
        return _lit(f"datetime'{raw}'")
    raise PbzError("UNSUPPORTED", "this property holds an image, rule, filter or other complex value: set it in Desktop")


def decode(node) -> str:
    """Readable form of a stored property value (mirror text: tokens stay tokens)."""
    from . import images
    item = images.item_of(node)
    if item is not None:
        scal = ((node.get("image") or {}).get("scaling") if isinstance(node, dict) and isinstance(node.get("image"), dict)
                else None)
        return f"image:{item}" + (f" ({decode(scal).lower()})" if scal else "")
    if isinstance(node, dict):
        sol = node.get("solid")
        if isinstance(sol, dict):
            return decode(sol.get("color"))
        e = node.get("expr")
        if isinstance(e, dict):
            if "Literal" in e:
                v = (e["Literal"] or {}).get("Value")
                if not isinstance(v, str):
                    return str(v)
                val, kind = pbir.decode_literal(v)
                if kind in ("int", "double"):
                    return str(int(val)) if float(val).is_integer() else str(val)
                return str(val).lower() if kind == "bool" else str(val)
            if "ThemeDataColor" in e:
                t = e["ThemeDataColor"] or {}
                return f"theme:{t.get('ColorId')}:{t.get('Percent', 0)}"
            if "Measure" in e:
                return f"measure:[{(e['Measure'] or {}).get('Property', '')}]"
    return "(complex)"


# ---------------------------------------------------------------- targets

def _pages(project) -> tuple[str, list[tuple[str, str]]]:
    pdir = os.path.join(project.mirror_report, "definition", "pages")
    if not os.path.isdir(pdir):
        raise PbzError("NO_MIRROR", "run `pbz pull` first (no report pages in the mirror)")
    out = []
    for pid in sorted(os.listdir(pdir)):
        pj = os.path.join(pdir, pid, "page.json")
        if os.path.isfile(pj):
            try:
                name = json.loads(read_text(pj)[0]).get("displayName") or pid
            except ValueError:
                name = pid
            out.append((pid, name))
    return pdir, out


def _mirror_rel(project, path: str) -> str:
    return os.path.relpath(path, project.ws.mirror).replace("\\", "/")


def _strict_documents(project, page: str | None, vtype: str | None, all_: bool, include_visuals: bool = True) -> None:
    """Parse candidate page and visual documents before a helper can skip or rename a bad one.

    Opt-in: bookmarks, filters and button links keep the lenient discovery in `targets`.
    """
    pdir = os.path.join(project.mirror_report, "definition", "pages")
    if not os.path.isdir(pdir):
        return
    whole = page is None and bool(vtype or all_)
    chosen = []
    for pid in sorted(os.listdir(pdir)):
        pj = os.path.join(pdir, pid, "page.json")
        if not os.path.isfile(pj):
            continue
        rel = _mirror_rel(project, pj)
        try:
            doc = json.loads(read_text(pj)[0])
            if not isinstance(doc, dict):
                raise ValueError
            name = doc.get("displayName") or pid
        except (OSError, ValueError, UnicodeDecodeError):
            if whole or (page and page.lower() == pid.lower()):
                raise PbzError("REFUSED", rel)
            continue
        if page and page.lower() not in (pid.lower(), str(name).lower()):
            continue
        chosen.append(pid)
    if not include_visuals:
        return
    for pid in chosen:
        vdir = os.path.join(pdir, pid, "visuals")
        if not os.path.isdir(vdir):
            continue
        for vid in sorted(os.listdir(vdir)):
            path = os.path.join(vdir, vid, "visual.json")
            if not os.path.isfile(path):
                continue
            rel = _mirror_rel(project, path)
            try:
                doc = json.loads(read_text(path)[0])
                if not isinstance(doc, dict):
                    raise ValueError
            except (OSError, ValueError, UnicodeDecodeError):
                raise PbzError("REFUSED", rel)


def targets(project, page: str | None, visual: str | None, vtype: str | None, all_: bool,
            strict: bool = False) -> list[dict]:
    from .layout import LABELS, _visuals
    if strict:
        _strict_documents(project, page, vtype, all_)
    pdir, pages = _pages(project)
    if page:
        pages = [p for p in pages if page.lower() in (p[0].lower(), str(p[1]).lower())]
        if not pages:
            raise PbzError("NOT_FOUND", f"page {page}")
    elif visual or not vtype:
        raise PbzError("BAD_REQUEST", "give a page (and a visual id or layout label), or --type for every visual of a type")
    out = []
    for pid, pname in pages:
        vis = _visuals(os.path.join(pdir, pid))
        for i, v in enumerate(vis):
            label = LABELS[i] if i < len(LABELS) else "#"
            if visual and visual not in (v["id"], label):
                continue
            if vtype and v["type"].lower() != vtype.lower():
                continue
            if v["type"] in ("group", "?"):
                continue
            out.append({"page": pname, "id": v["id"], "label": label, "type": v["type"],
                        "path": os.path.join(pdir, pid, "visuals", v["id"], "visual.json")})
    if visual and not out:
        raise PbzError("NOT_FOUND", f"visual {visual} on page {page} (ids and labels: `pbz layout --page`)")
    if page and not visual and not vtype and not all_:
        raise PbzError("BAD_REQUEST", "name a visual, or pass --type <type> or --all for every visual on the page")
    return out


def _parse_key(key: str) -> tuple[str, str, str]:
    """[container.|visual.]object.property"""
    parts = key.strip().split(".")
    where = ""
    if parts and parts[0] in ("container", "visual"):
        where = parts.pop(0)
    if len(parts) != 2 or not all(parts):
        raise PbzError("BAD_REQUEST", f"{key}: use object.property (optionally container. or visual. first)")
    return where, parts[0], parts[1]


def _resolve(ws, project, vt: str, where: str, obj: str, prop: str):
    """('objects' | 'visualContainerObjects', code) or a refusal with close matches."""
    vobjs, _src = _objects_for(ws, project, vt)
    cobjs = catalog()["container"]
    in_v = vobjs is not None and prop in vobjs.get(obj, {})
    in_c = prop in cobjs.get(obj, {})
    if where == "visual" and in_v or (not where and in_v and not in_c):
        return "objects", vobjs[obj][prop]
    if where == "container" and in_c or (not where and in_c):
        return "visualContainerObjects", cobjs[obj][prop]
    pool = [f"{o}.{p}" for o, ps in (vobjs or {}).items() for p in ps] + [f"container.{o}.{p}" for o, ps in cobjs.items() for p in ps]
    near = difflib.get_close_matches(f"{obj}.{prop}", pool, n=4, cutoff=0.6)
    hint = f"; did you mean {', '.join(near)}?" if near else ""
    if vobjs is None:
        hint += " (custom visual without object names: `pbz visual import <file.pbiviz>`)"
    raise PbzError("BAD_REQUEST", f"{vt} has no formatting property {obj}.{prop}{hint}")


# ---------------------------------------------------------------- edit

def _entry(entries: list, selector: dict | None, create: bool) -> dict | None:
    for e in entries:
        if isinstance(e, dict) and (e.get("selector") or None) == selector:
            return e
    if not create:
        return None
    e = {"properties": {}}
    if selector:
        e["selector"] = selector
    entries.append(e)
    return e


def _series_selector(visual: dict, model, ref: str) -> dict:
    qs = ((visual.get("query") or {}).get("queryState") or {}) if isinstance(visual.get("query"), dict) else {}
    want = ref.strip()
    m = re.match(r"^\s*(?:'((?:[^']|'')+)'|([^\[\]']*?))?\s*\[(.+)\]\s*$", want)
    name = m.group(3).lower() if m else want.lower()
    table = (m.group(1) or m.group(2) or "").replace("''", "'").lower() if m else ""
    for bucket in qs.values() if isinstance(qs, dict) else []:
        for p in (bucket or {}).get("projections") or [] if isinstance(bucket, dict) else []:
            f = p.get("field") or {}
            inner = f.get("Measure") or f.get("Column") or (f.get("Aggregation") or {}).get("Expression", {}).get("Column")
            if not isinstance(inner, dict):
                continue
            ent = ((inner.get("Expression") or {}).get("SourceRef") or {}).get("Entity", "")
            if str(inner.get("Property", "")).lower() == name and (not table or str(ent).lower() == table):
                if p.get("queryRef"):
                    return {"metadata": p["queryRef"]}
    raise PbzError("NOT_FOUND", f"{ref} is not a field of this visual (series formatting needs a projected field)")


def run(ws, project, page: str | None = None, visual: str | None = None, sets: list[str] | None = None,
        unsets: list[str] | None = None, vtype: str | None = None, all_: bool = False, state: str | None = None,
        series: str | None = None, show: bool = False) -> dict:
    sets, unsets = list(sets or []), list(unsets or [])
    if not (sets or unsets or show):
        raise PbzError("BAD_REQUEST", "pass --set object.property=value, --unset object.property or --show")
    mutating = bool(sets or unsets)
    tg = targets(project, page, visual, vtype, all_, strict=mutating)
    if series and len(tg) != 1:
        raise PbzError("BAD_REQUEST", "--series formats one visual: name it")
    model = tmdl.load_model(os.path.join(project.mirror_model, "definition"))
    parsed = []
    for s in sets:
        key, eq, raw = s.partition("=")
        if not eq:
            raise PbzError("BAD_REQUEST", f"{s}: use object.property=value")
        parsed.append(("set", *_parse_key(key), raw))
    parsed += [("unset", *_parse_key(k), None) for k in unsets]
    from . import images
    img = lambda raw: images.resolve(ws, project, raw)  # noqa: E731
    changed, skipped, files, shown = [], [], [], []
    loaded = []
    for t in tg:
        rel = os.path.relpath(t["path"], ws.mirror).replace("\\", "/")
        try:
            text, bom = read_text(t["path"])
            doc = json.loads(text)
        except (OSError, ValueError, UnicodeDecodeError):
            if mutating:
                raise PbzError("REFUSED", rel)
            raise
        if not isinstance(doc, dict):
            if mutating:
                raise PbzError("REFUSED", rel)
            continue
        loaded.append((t, text, bom, doc, rel))
    pending = []
    for t, text, bom, doc, rel in loaded:
        body = doc.get("visual") if isinstance(doc.get("visual"), dict) else None
        if body is None:
            skipped.append({"visual": t["id"], "reason": "not a visual"})
            continue
        if show:
            shown.append(_show(t, body))
        done, problems = [], []
        for op, where, obj, prop, raw in parsed:
            try:
                section, code = _resolve(ws, project, t["type"], where, obj, prop)
                selector = _series_selector(body, model, series) if series else None
                if state:
                    allowed = _states(t["type"]).get(obj, [])
                    if state not in allowed:
                        raise PbzError("BAD_REQUEST", f"{t['type']}.{obj} has states {', '.join(allowed) or 'none'}")
                    selector = dict(selector or {}, id=state)
                bucket = body.setdefault(section, {}) if op == "set" else body.get(section) or {}
                entries = bucket.setdefault(obj, []) if op == "set" else bucket.get(obj) or []
                e = _entry(entries, selector, op == "set")
                if op == "set" and code == "image":
                    # {"image": {name, url, scaling}}: Microsoft's validator reads a nested key that is also a
                    # property of the same object (plotArea.url, fillCustom.url) as misplaced and rejects it
                    pool = (catalog()["container"] if section == "visualContainerObjects"
                            else _objects_for(ws, project, t["type"])[0] or {}).get(obj, {})
                    clash = sorted(set(pool) & {"image", "name", "url", "scaling"} - {prop})
                    if clash:
                        raise PbzError("UNSUPPORTED", f"{obj}.{prop}: Microsoft's report validator rejects an image "
                                                      f"here ({obj} also has a {', '.join(clash)} property); set this "
                                                      "image in Desktop")
                if op == "set":
                    e.setdefault("properties", {})[prop] = encode(code, raw, model, image_ref=img)
                    done.append(f"{'container.' if section == 'visualContainerObjects' else ''}{obj}.{prop}")
                elif e is not None and prop in (e.get("properties") or {}):
                    del e["properties"][prop]
                    if not e["properties"] and set(e) <= {"properties", "selector"}:
                        entries.remove(e)
                    if not entries:
                        bucket.pop(obj, None)
                    if not bucket:
                        body.pop(section, None)
                    done.append(f"-{'container.' if section == 'visualContainerObjects' else ''}{obj}.{prop}")
            except PbzError as err:
                if len(tg) == 1:
                    raise
                problems.append(err.detail or err.code)
        if done:
            pending.append((t["path"], json.dumps(doc, indent=2, ensure_ascii=False) + "\n", bom))
            files.append(rel)
            changed.append({"page": t["page"], "visual": t["id"], "label": t["label"], "type": t["type"], "changes": done})
        if problems:
            skipped.append({"page": t["page"], "visual": t["id"], "type": t["type"], "reasons": sorted(set(problems))[:3]})
    if pending:
        commit_bytes([("write", path, new, bom) for path, new, bom in pending])
    out = {"verb": "style", "project": project.name, "changed": changed, "files": files}
    if skipped:
        out["skipped"] = skipped
    if show:
        out["formatting"] = shown
    if changed:
        out["next"] = "pbz validate, then pbz push --reload (report-only change)"
    return out


# ---------------------------------------------------------------- page canvas

CANVAS_OBJECTS = {"background": "background", "canvas": "background", "wallpaper": "outspace", "outspace": "outspace"}
CANVAS_LABEL = {"background": "background", "outspace": "wallpaper"}
CANVAS_PROPS = ("color", "transparency", "image", "scaling")


def _canvas_key(key: str) -> tuple[str, str]:
    parts = key.strip().split(".")
    if len(parts) != 2 or parts[0].lower() not in CANVAS_OBJECTS or parts[1] not in CANVAS_PROPS:
        raise PbzError("BAD_REQUEST", f"{key}: page formatting is background.<p> or wallpaper.<p>, "
                                      f"p = {', '.join(CANVAS_PROPS)}")
    return CANVAS_OBJECTS[parts[0].lower()], parts[1]


def _canvas_rows(doc: dict) -> list[str]:
    rows = []
    for obj in ("background", "outspace"):
        for e in (doc.get("objects") or {}).get(obj) or []:
            for prop, val in sorted(((e or {}).get("properties") or {}).items()):
                rows.append(f"{CANVAS_LABEL[obj]}.{prop} = {decode(val)}")
    return rows


def run_canvas(ws, project, page: str | None = None, sets: list[str] | None = None, unsets: list[str] | None = None,
               all_: bool = False, show: bool = False) -> dict:
    """Page canvas formatting (page.json objects): background (canvas) and wallpaper (outspace) color,
    transparency (0-100) and image (a registered image, with scaling fit, fill or normal)."""
    from . import images
    sets, unsets = list(sets or []), list(unsets or [])
    if not (sets or unsets or show):
        raise PbzError("BAD_REQUEST", "pass --set background.color=#RRGGBB (wallpaper.image=<item>, ...), --unset or --show")
    pdir, pages = _pages(project)
    if page:
        pages = [p for p in pages if page.lower() in (p[0].lower(), str(p[1]).lower())]
        if not pages:
            raise PbzError("NOT_FOUND", f"page {page}")
    elif not all_:
        raise PbzError("BAD_REQUEST", "--canvas formats one page (name it) or every page (--all)")
    mutating = bool(sets or unsets)
    if mutating:
        _strict_documents(project, page, None, bool(all_), include_visuals=False)
    parsed = []
    for s in sets:
        key, eq, raw = s.partition("=")
        if not eq:
            raise PbzError("BAD_REQUEST", f"{s}: use object.property=value")
        parsed.append(("set", *_canvas_key(key), raw.strip()))
    parsed += [("unset", *_canvas_key(k), None) for k in unsets]
    parsed.sort(key=lambda p: p[2] == "scaling")  # an image set in the same call comes first
    model = tmdl.load_model(os.path.join(project.mirror_model, "definition"))
    changed, files, shown, notes = [], [], [], []
    loaded = []
    for pid, pname in pages:
        path = os.path.join(pdir, pid, "page.json")
        rel = os.path.relpath(path, ws.mirror).replace("\\", "/")
        try:
            text, bom = read_text(path)
            doc = json.loads(text)
        except (OSError, ValueError, UnicodeDecodeError):
            if mutating:
                raise PbzError("REFUSED", rel)
            raise
        if not isinstance(doc, dict):
            if mutating:
                raise PbzError("REFUSED", rel)
            continue
        loaded.append((path, pname, text, bom, doc, rel))
    pending = []
    for path, pname, text, bom, doc, rel in loaded:
        if show:
            shown.append({"page": pname, "properties": _canvas_rows(doc)})
        done = []
        for op, obj, prop, raw in parsed:
            label = f"{CANVAS_LABEL[obj]}.{prop}"
            objects = doc.setdefault("objects", {}) if op == "set" else doc.get("objects") or {}
            entries = objects.setdefault(obj, []) if op == "set" else objects.get(obj) or []
            e = _entry(entries, None, op == "set")
            if op == "set":
                props = e.setdefault("properties", {})
                if prop == "color":
                    if raw.lower().startswith("measure:"):
                        raise PbzError("UNSUPPORTED", "Desktop does not evaluate field values for canvas or wallpaper "
                                                      "colors: use #RRGGBB or theme:<n>")
                    props["color"] = _color(raw, model)
                elif prop == "transparency":
                    try:
                        ok = 0 <= float(raw) <= 100
                    except ValueError:
                        ok = False
                    if not ok:
                        raise PbzError("BAD_REQUEST", "transparency is 0 (opaque) to 100 (invisible)")
                    props["transparency"] = _lit(_num(raw))
                elif prop == "image":
                    item = images.resolve(ws, project, raw)
                    old = ((props.get("image") or {}).get("image") or {}).get("scaling")
                    val = images.composite(item, None if old else "Fit")
                    if old:
                        val["image"]["scaling"] = old  # keep the fit the page had
                    props["image"] = val
                    if "transparency" not in props and not any(p[2] == "transparency" and p[0] == "set" and p[1] == obj
                                                               for p in parsed):
                        props["transparency"] = _lit("0D")
                        notes.append(f"{CANVAS_LABEL[obj]}.transparency set to 0 so the image shows")
                else:  # scaling of the image
                    img = (props.get("image") or {}).get("image")
                    if not isinstance(img, dict):
                        raise PbzError("BAD_REQUEST", f"{CANVAS_LABEL[obj]} has no image: set {CANVAS_LABEL[obj]}.image first")
                    mode = images.SCALING.get(raw.lower())
                    if mode is None:
                        raise PbzError("BAD_REQUEST", f"{raw}: scaling is fit, fill or normal")
                    img["scaling"] = _lit(f"'{mode}'")
                done.append(label)
            elif e is not None:
                props = e.get("properties") or {}
                if prop == "scaling":
                    img = (props.get("image") or {}).get("image")
                    if isinstance(img, dict) and img.pop("scaling", None) is not None:
                        done.append("-" + label)
                elif prop in props:
                    del props[prop]
                    done.append("-" + label)
                if not props and set(e) <= {"properties"}:
                    entries.remove(e)
                if not entries:
                    objects.pop(obj, None)
                if not objects:
                    doc.pop("objects", None)
        if done:
            pending.append((path, json.dumps(doc, indent=2, ensure_ascii=False) + ("\n" if text.endswith("\n") else ""), bom))
            files.append(rel)
            changed.append({"page": pname, "changes": done})
    if pending:
        commit_bytes([("write", path, new, bom) for path, new, bom in pending])
    out = {"verb": "style", "target": "canvas", "project": project.name, "changed": changed, "files": files}
    if notes:
        out["notes"] = sorted(set(notes))
    if show:
        out["formatting"] = shown
    if changed:
        out["next"] = "pbz validate, then pbz push --reload (report-only change)"
    return out


def _show(t: dict, body: dict) -> dict:
    rows = []
    for section, prefix in (("objects", ""), ("visualContainerObjects", "container.")):
        for obj, entries in sorted((body.get(section) or {}).items()):
            for e in entries if isinstance(entries, list) else []:
                sel = e.get("selector") if isinstance(e, dict) else None
                tag = ""
                if isinstance(sel, dict):
                    tag = " [" + ", ".join(f"{k}={v}" for k, v in sel.items() if k in ("metadata", "id")) + \
                          (" data" if "data" in sel else "") + "]"
                for prop, val in sorted(((e or {}).get("properties") or {}).items()):
                    rows.append(f"{prefix}{obj}.{prop}{tag} = {decode(val)}")
    return {"page": t["page"], "visual": t["id"], "label": t["label"], "type": t["type"], "properties": rows[:200]}


def list_(ws, project, vtype: str, obj: str | None = None) -> dict:
    from . import visuals
    vt = visuals.core_name(vtype) or vtype
    vobjs, source = _objects_for(ws, project, vt)
    cobjs = catalog()["container"]

    def fmt(props):
        return {p: (c if isinstance(c, str) else "one of: " + ", ".join(str(v) for v in c)) for p, c in props.items()}

    if obj:
        hit_v = (vobjs or {}).get(obj)
        hit_c = cobjs.get(obj)
        if hit_v is None and hit_c is None:
            raise PbzError("NOT_FOUND", f"{vt} has no formatting object {obj}")
        out = {"verb": "style", "action": "list", "type": vt, "object": obj}
        if hit_v is not None:
            out["visual"] = fmt(hit_v)
        if hit_c is not None:
            out["container"] = fmt(hit_c)
        states = _states(vt).get(obj)
        if states:
            out["states"] = states
        return out
    return {"verb": "style", "action": "list", "type": vt, "source": source,
            "visual_objects": sorted(vobjs or {}), "container_objects": sorted(cobjs),
            "states": _states(vt) or {}, "next": f"pbz style --list --type {vt} --object <name> for its properties"}
