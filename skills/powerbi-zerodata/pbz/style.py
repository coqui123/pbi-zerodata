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
from .common import PbzError, read_text, write_text
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


def encode(code, raw: str, model) -> dict:
    """Formatting property value for one catalog type."""
    raw = raw.strip()
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


def targets(project, page: str | None, visual: str | None, vtype: str | None, all_: bool) -> list[dict]:
    from .layout import LABELS, _visuals
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
    tg = targets(project, page, visual, vtype, all_)
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
    changed, skipped, files, shown = [], [], [], []
    for t in tg:
        text, bom = read_text(t["path"])
        doc = json.loads(text)
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
                if op == "set":
                    e.setdefault("properties", {})[prop] = encode(code, raw, model)
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
            write_text(t["path"], json.dumps(doc, indent=2, ensure_ascii=False) + "\n", bom)
            files.append(os.path.relpath(t["path"], ws.mirror).replace("\\", "/"))
            changed.append({"page": t["page"], "visual": t["id"], "label": t["label"], "type": t["type"], "changes": done})
        if problems:
            skipped.append({"page": t["page"], "visual": t["id"], "type": t["type"], "reasons": sorted(set(problems))[:3]})
    out = {"verb": "style", "project": project.name, "changed": changed, "files": files}
    if skipped:
        out["skipped"] = skipped
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
