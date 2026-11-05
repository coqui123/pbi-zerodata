"""Built-in PBIR integrity checks: the cross-file structure hand edits break most often (page order,
folder and object names, positions, groups, interactions, bookmarks). Runs on the mirror and on the
staged real files, with or without Microsoft's optional schema validator. Names and ids only."""
from __future__ import annotations

import json
import os

from . import pbir
from .common import read_text


def _load(path: str):
    try:
        return pbir.to_py(pbir.parse_json(read_text(path)[0]))
    except (OSError, UnicodeDecodeError, pbir.JsonError):
        return None  # unreadable JSON is reported by the field-reference check


def _tooltip_sections(v) -> list[str]:
    """Page names a visual's report-page tooltip points at (visualTooltip.section literal)."""
    out = []

    def walk(n):
        if isinstance(n, dict):
            for k, x in n.items():
                if k == "visualTooltip" and isinstance(x, list):
                    for entry in x:
                        props = (entry or {}).get("properties") or {}
                        lit = (((props.get("section") or {}).get("expr") or {}).get("Literal") or {}).get("Value")
                        if isinstance(lit, str) and lit.startswith("'") and lit.endswith("'"):
                            out.append(lit[1:-1].replace("''", "'"))
                else:
                    walk(x)
        elif isinstance(n, list):
            for x in n:
                walk(x)

    walk(v)
    return out


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _custom_visuals(add, ddir: str, report_dir: str, instances: dict, has_package) -> None:
    """Registration of custom visuals (as Microsoft's validator reads it) and data roles of every
    visual whose capabilities are known (built-in catalog)."""
    from . import visuals
    report = _load(os.path.join(ddir, "report.json")) or {}
    reg: dict[str, str] = {}
    for g in report.get("publicCustomVisuals") or []:
        if isinstance(g, str) and g:
            reg[g] = "public"
    for p in report.get("resourcePackages") or []:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str):
            continue
        name, typ = p["name"], p.get("type")
        if typ not in ("CustomVisual", "OrganizationalStoreCustomVisual"):
            continue
        kind = "private" if typ == "CustomVisual" else "organizational"
        if name in reg and reg[name] != kind:
            add("error", "PBIR_CUSTOM_VISUAL_CONFLICT", "definition/report.json",
                f"{name} is registered as {reg[name]} and {kind}")
        reg[name] = kind
        if kind == "organizational" and not name.endswith(visuals.ORG):
            add("error", "PBIR_ORGSTORE_NAME", "definition/report.json",
                f"organizational visual package {name} must end with {visuals.ORG} (and so must its visualType)")
        if kind == "private" and has_package is not None and not has_package(name):
            add("error", "PBIR_PRIVATE_VISUAL_PACKAGE_MISSING", "definition/report.json",
                f"private visual {name} has no package in CustomVisuals/{name}/ (pbz visual import --as private)")
    core = visuals.core()
    for vt, uses in sorted(instances.items()):
        contract = core.get(vt)
        if contract is None:
            if vt not in reg:
                hint = f" (registered as {vt[:-len(visuals.ORG)]}?)" if vt.endswith(visuals.ORG) else ""
                add("warning", "PBIR_VISUAL_TYPE_UNREGISTERED", uses[0][0],
                    f"visualType {vt} is neither built-in nor registered in report.json{hint}: "
                    f"Desktop shows a placeholder ({len(uses)} visual(s))")
            continue
        known = {r["name"] for r in contract.get("roles") or []}
        for vrel, roles in uses:
            for role in sorted(roles - known):
                add("warning", "PBIR_ROLE_UNKNOWN", vrel, f"{vt} has no data role {role}")


def _literal(node):
    e = node.get("expr") if isinstance(node, dict) else None
    return (e.get("Literal") or {}).get("Value") if isinstance(e, dict) and isinstance(e.get("Literal"), dict) else None


def _value_ok(code, node) -> bool:
    """Does a stored formatting value fit the catalog type? Non-literal values (field values, rules,
    expressions) are not judged."""
    if code == "color":
        return isinstance(node, dict) and ("solid" in node or "expr" in node)
    raw = _literal(node)
    if not isinstance(raw, str):
        return True
    val, kind = pbir.decode_literal(raw)
    if code == "bool":
        return kind == "bool"
    if code in ("numeric", "fontSize", "displayUnits", "variant", "integer"):
        return kind in ("int", "double")
    if code in ("text", "fontFamily", "alignment", "formatString"):
        return kind == "string"
    if isinstance(code, list):
        return val in code or (isinstance(val, float) and val.is_integer() and int(val) in code)
    return True


def _formatting(add, items: list) -> None:
    """Formatting objects, properties and literal value types of built-in visuals against Microsoft's
    formatting catalog (unknown names are typos or hand edits Desktop ignores)."""
    from . import style
    cat = style.catalog()
    for vrel, vt, body in items:
        vobjs = cat["visuals"].get(vt)
        if vobjs is None:
            continue
        for section, objs, label in (("objects", vobjs, ""), ("visualContainerObjects", cat["container"], "container ")):
            sec = body.get(section)
            for obj, entries in (sec.items() if isinstance(sec, dict) else []):
                if obj not in objs:
                    add("warning", "PBIR_OBJECT_UNKNOWN", vrel, f"{vt} has no {label}formatting object {obj}")
                    continue
                for e in entries if isinstance(entries, list) else []:
                    for prop, val in ((e or {}).get("properties") or {}).items() if isinstance(e, dict) else []:
                        code = objs[obj].get(prop)
                        if code is None:
                            add("warning", "PBIR_PROPERTY_UNKNOWN", vrel, f"{vt} {label}object {obj} has no property {prop}")
                        elif not _value_ok(code, val):
                            add("warning", "PBIR_PROPERTY_VALUE", vrel, f"{label}{obj}.{prop} value does not fit its type "
                                f"({code if isinstance(code, str) else 'one of the listed values'})")


def run(report_dir: str, prefix: str = "", has_package=None) -> list[dict]:
    """`has_package(name)`: is the private visual package present? Defaults to the report's own
    CustomVisuals folder when it has one (staged real files); None skips the check."""
    out: list[dict] = []
    if has_package is None and os.path.isdir(os.path.join(report_dir, "CustomVisuals")):
        has_package = lambda n: os.path.isfile(os.path.join(report_dir, "CustomVisuals", n, "package.json"))  # noqa: E731
    instances: dict[str, list] = {}
    formatting: list = []
    links: list = []

    def add(sev, code, where, msg):
        out.append({"severity": sev, "code": code, "where": prefix + where, "message": msg})

    ddir = os.path.join(report_dir, "definition")
    pbir_file = os.path.join(report_dir, "definition.pbir")
    ref = _load(pbir_file) if os.path.exists(pbir_file) else None
    if ref is None:
        add("error", "PBIR_NO_DATASET_REFERENCE", "definition.pbir", "missing or unreadable definition.pbir")
    elif not isinstance(ref.get("datasetReference"), dict) or not (
            ref["datasetReference"].get("byPath") or ref["datasetReference"].get("byConnection")):
        add("error", "PBIR_NO_DATASET_REFERENCE", "definition.pbir", "datasetReference needs byPath or byConnection")
    if not os.path.isdir(ddir):
        return out
    if not os.path.exists(os.path.join(ddir, "report.json")):
        add("error", "PBIR_NO_REPORT_JSON", "definition/report.json", "report.json is missing")
    if not os.path.exists(os.path.join(ddir, "version.json")):
        add("warning", "PBIR_NO_VERSION", "definition/version.json", "version.json is missing")

    pages_dir = os.path.join(ddir, "pages")
    page_ids = sorted(d for d in os.listdir(pages_dir) if os.path.isdir(os.path.join(pages_dir, d))) \
        if os.path.isdir(pages_dir) else []
    visuals_of: dict[str, set] = {}
    bindings: dict[str, list] = {}
    sync_groups: dict[str, dict] = {}
    tooltip_refs: list[tuple[str, str]] = []
    for pid in page_ids:
        rel = f"definition/pages/{pid}/page.json"
        page = _load(os.path.join(pages_dir, pid, "page.json"))
        if page is None:
            if not os.path.exists(os.path.join(pages_dir, pid, "page.json")):
                add("error", "PAGE_JSON_MISSING", f"definition/pages/{pid}", "page folder without page.json")
            continue
        if page.get("name") != pid:
            add("error", "PAGE_NAME_MISMATCH", rel, f"page name must equal its folder name ({pid})")
        pb = page.get("pageBinding")
        if isinstance(pb, dict) and pb.get("name"):
            bindings.setdefault(pb["name"], []).append(pid)
        if not page.get("displayName"):
            add("warning", "PAGE_NO_DISPLAY_NAME", rel, "page has no displayName")
        vdir = os.path.join(pages_dir, pid, "visuals")
        vids = sorted(d for d in os.listdir(vdir) if os.path.isdir(os.path.join(vdir, d))) if os.path.isdir(vdir) else []
        visuals_of[pid] = set(vids)
        groups, parents = set(), {}
        for vid in vids:
            vrel = f"definition/pages/{pid}/visuals/{vid}/visual.json"
            v = _load(os.path.join(vdir, vid, "visual.json"))
            if v is None:
                if not os.path.exists(os.path.join(vdir, vid, "visual.json")):
                    add("error", "VISUAL_JSON_MISSING", f"definition/pages/{pid}/visuals/{vid}", "visual folder without visual.json")
                continue
            if v.get("name") != vid:
                add("error", "VISUAL_NAME_MISMATCH", vrel, f"visual name must equal its folder name ({vid})")
            pos = v.get("position")
            if not isinstance(pos, dict) or not all(_num(pos.get(k)) for k in ("x", "y", "width", "height")):
                add("error", "VISUAL_POSITION", vrel, "position needs numeric x, y, width and height")
            elif pos["width"] <= 0 or pos["height"] <= 0:
                add("warning", "VISUAL_ZERO_SIZE", vrel, "visual has zero width or height")
            if "visualGroup" in v:
                groups.add(vid)
            elif not isinstance(v.get("visual"), dict):
                add("error", "VISUAL_EMPTY", vrel, "visual.json needs a visual or a visualGroup")
            for misplaced in ("visualContainerObjects", "objects", "query", "visualType"):
                if misplaced in v:
                    add("error", "PBIR_MISPLACED_PROPERTY", vrel, f"{misplaced} belongs inside \"visual\", not at the root")
            if v.get("parentGroupName"):
                parents[vid] = v["parentGroupName"]
            body = v.get("visual") if isinstance(v.get("visual"), dict) else {}
            qs = ((body.get("query") or {}).get("queryState") or {}) if isinstance(body.get("query"), dict) else {}
            if isinstance(body.get("visualType"), str) and body["visualType"]:
                instances.setdefault(body["visualType"], []).append(
                    (vrel, set(qs) if isinstance(qs, dict) else set()))
                formatting.append((vrel, body["visualType"], body))
            vco = body.get("visualContainerObjects") if isinstance(body.get("visualContainerObjects"), dict) else {}
            for entry in vco.get("visualLink") or [] if isinstance(vco.get("visualLink"), list) else []:
                links.append((vrel, (entry or {}).get("properties") or {} if isinstance(entry, dict) else {}))
            fields = set()
            for role, bucket in qs.items() if isinstance(qs, dict) else []:
                for proj in (bucket or {}).get("projections") or [] if isinstance(bucket, dict) else []:
                    if isinstance(proj, dict) and "field" in proj and not proj.get("queryRef"):
                        add("warning", "PROJECTION_NO_QUERYREF", vrel, f"a {role} projection has no queryRef")
                    if isinstance(proj, dict):
                        fields.add(json.dumps(proj.get("field"), sort_keys=True))
            sg = body.get("syncGroup")
            if isinstance(sg, dict) and sg.get("groupName"):
                sync_groups.setdefault(sg["groupName"], {})[f"{pid}/{vid}"] = frozenset(fields)
            for sec in _tooltip_sections(v):
                tooltip_refs.append((vrel, sec))
        for vid, grp in parents.items():
            if grp not in groups:
                add("error", "GROUP_MISSING", f"definition/pages/{pid}/visuals/{vid}/visual.json",
                    f"parentGroupName {grp} is not a visual group on this page")
        for it in page.get("visualInteractions") or []:
            if not isinstance(it, dict):
                continue
            for side in ("source", "target"):
                if it.get(side) and it[side] not in visuals_of[pid]:
                    add("error", "INTERACTION_MISSING_VISUAL", rel, f"visual interaction {side} {it[side]} does not exist")

    for name, pids in bindings.items():
        if len(pids) > 1:
            add("error", "DUPLICATE_PAGE_BINDING", "definition/pages", f"pageBinding name shared by pages {', '.join(pids)}")
    for grp, members in sync_groups.items():
        if len(set(members.values())) > 1:
            add("warning", "SYNC_GROUP_FIELDS_DIFFER", "definition/pages",
                f"slicers in sync group {grp} bind different fields: {', '.join(sorted(members))}")
    names = {}
    for pid in page_ids:
        page = _load(os.path.join(pages_dir, pid, "page.json")) or {}
        names[pid] = pid
        if page.get("displayName"):
            names[page["displayName"]] = pid
    for vrel, sec in tooltip_refs:
        if sec not in names:
            add("error", "TOOLTIP_PAGE_MISSING", vrel, f"report page tooltip points at missing page {sec}")
    meta = _load(os.path.join(pages_dir, "pages.json")) if os.path.exists(os.path.join(pages_dir, "pages.json")) else None
    if page_ids and meta is None and not os.path.exists(os.path.join(pages_dir, "pages.json")):
        add("error", "PAGES_JSON_MISSING", "definition/pages/pages.json", "pages.json is missing")
    if isinstance(meta, dict):
        order = meta.get("pageOrder") or []
        seen = set()
        for pid in order:
            if pid in seen:
                add("error", "PAGE_ORDER_DUPLICATE", "definition/pages/pages.json", f"page {pid} listed twice")
            seen.add(pid)
            if pid not in visuals_of and pid not in page_ids:
                add("error", "PAGE_ORDER_MISSING_PAGE", "definition/pages/pages.json", f"pageOrder lists missing page {pid}")
        for pid in page_ids:
            if pid not in seen:
                add("warning", "PAGE_NOT_IN_ORDER", "definition/pages/pages.json", f"page {pid} is not in pageOrder")
        active = meta.get("activePageName")
        if active and active not in page_ids:
            add("error", "ACTIVE_PAGE_MISSING", "definition/pages/pages.json", f"activePageName {active} does not exist")

    bdir = os.path.join(ddir, "bookmarks")
    if os.path.isdir(bdir):
        files = {f[:-len(".bookmark.json")] for f in os.listdir(bdir) if f.endswith(".bookmark.json")}
        listed = set()
        bm = _load(os.path.join(bdir, "bookmarks.json")) if os.path.exists(os.path.join(bdir, "bookmarks.json")) else None
        for it in (bm or {}).get("items") or []:
            if not isinstance(it, dict):
                continue
            names = [c for c in it.get("children") or [] if isinstance(c, str)] or ([it["name"]] if it.get("name") and not it.get("children") else [])
            for name in names:
                listed.add(name)
                if name not in files:
                    add("error", "BOOKMARK_MISSING_FILE", "definition/bookmarks/bookmarks.json", f"bookmark {name} has no file")
        for name in sorted(files - listed):
            add("warning", "BOOKMARK_NOT_LISTED", f"definition/bookmarks/{name}.bookmark.json", "bookmark file not in bookmarks.json")
        for name in sorted(files):
            b = _load(os.path.join(bdir, f"{name}.bookmark.json"))
            if not isinstance(b, dict):
                continue
            brel = f"definition/bookmarks/{name}.bookmark.json"
            if b.get("name") and b["name"] != name:
                add("error", "BOOKMARK_NAME_MISMATCH", brel, "bookmark name must equal its file name")
            es = b.get("explorationState") or {}
            act = es.get("activeSection")
            if act and act not in page_ids:
                add("error", "BOOKMARK_MISSING_PAGE", brel, f"activeSection {act} does not exist")
            for pid, sec in (es.get("sections") or {}).items():
                if pid not in page_ids:
                    add("warning", "BOOKMARK_MISSING_PAGE", brel, f"section {pid} does not exist")
                    continue
                for vid in (sec or {}).get("visualContainers") or {}:
                    if vid not in visuals_of.get(pid, set()):
                        add("warning", "BOOKMARK_MISSING_VISUAL", brel, f"visual {vid} no longer exists on page {pid}")
            all_visuals = set().union(*visuals_of.values()) if visuals_of else set()
            for vid in (b.get("options") or {}).get("targetVisualNames") or []:
                if vid not in all_visuals:
                    add("warning", "BOOKMARK_MISSING_VISUAL", brel, f"target visual {vid} does not exist")

    ext = _load(os.path.join(ddir, "reportExtensions.json")) if os.path.exists(os.path.join(ddir, "reportExtensions.json")) else None
    if isinstance(ext, dict):
        seen = set()
        for ent in ext.get("entities") or []:
            for m in (ent or {}).get("measures") or []:
                key = (str((ent or {}).get("name", "")).lower(), str((m or {}).get("name", "")).lower())
                if key in seen:
                    add("error", "DUPLICATE_REPORT_MEASURE", "definition/reportExtensions.json",
                        f"report measure {m.get('name')} defined twice")
                seen.add(key)
    _custom_visuals(add, ddir, report_dir, instances, has_package)
    _formatting(add, formatting)
    _links(add, ddir, page_ids, links)
    return out


def _lit_str(node) -> str | None:
    raw = _literal(node)
    if isinstance(raw, str) and len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    return None


def _links(add, ddir: str, page_ids: list, links: list) -> None:
    """Button actions point at pages and bookmarks that exist (drillthrough targets are drillthrough pages)."""
    if not links:
        return
    kinds = {}
    for pid in page_ids:
        p = _load(os.path.join(ddir, "pages", pid, "page.json")) or {}
        kinds[pid] = ((p.get("pageBinding") or {}).get("type") if isinstance(p.get("pageBinding"), dict) else None)
    bdir = os.path.join(ddir, "bookmarks")
    marks = {f[:-len(".bookmark.json")] for f in os.listdir(bdir) if f.endswith(".bookmark.json")} if os.path.isdir(bdir) else set()
    for vrel, props in links:
        kind = _lit_str(props.get("type"))
        if kind == "PageNavigation":
            target = _lit_str(props.get("navigationSection"))
            if target is not None and target not in kinds:
                add("error", "PBIR_LINK_TARGET_MISSING", vrel, f"page navigation points at missing page {target}")
        elif kind == "Drillthrough":
            target = _lit_str(props.get("drillthroughSection"))
            if target is not None and target not in kinds:
                add("error", "PBIR_LINK_TARGET_MISSING", vrel, f"drillthrough points at missing page {target}")
            elif target is not None and kinds.get(target) != "Drillthrough":
                add("warning", "PBIR_LINK_NOT_DRILLTHROUGH", vrel, f"drillthrough target {target} is not a drillthrough page")
        elif kind == "Bookmark":
            target = _lit_str(props.get("bookmark"))
            if target is not None and target not in marks:
                add("error", "PBIR_LINK_TARGET_MISSING", vrel, f"bookmark action points at missing bookmark {target}")
