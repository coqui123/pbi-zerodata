"""Interaction contract checks. Static evidence is never represented as native UI execution."""
from __future__ import annotations

import json
import os
from . import control
from .common import PbzError

ACTIONS = {"slicer", "navigate", "drillthrough", "back", "hover"}


def capabilities():
    return {"verb": "interactions capabilities", "static_contracts": sorted(ACTIONS),
            "native_actions": [], "native": "UNSUPPORTED",
            "note": "The installed Desktop bridge does not expose validated native action APIs. No UI or screenshot is read."}


def _literal(properties, key):
    value = properties.get(key, {}).get("expr", {}).get("Literal", {}).get("Value", "")
    return value[1:-1].replace("''", "'") if value.startswith("'") else value


def _fields(node):
    result = set()
    def visit(x):
        if isinstance(x, dict):
            for kind in ["Column", "Measure"]:
                obj = x.get(kind)
                if isinstance(obj, dict):
                    table = obj.get("Expression", {}).get("SourceRef", {}).get("Entity")
                    if table and obj.get("Property"):
                        result.add((kind, table, obj["Property"]))
            for value in x.values():
                visit(value)
        elif isinstance(x, list):
            for value in x:
                visit(value)
    visit(node)
    return result


def run(ws, project, spec, mode="static"):
    data = control.load(spec, root=ws.root)
    control.keys(data, {"version", "cases"}, {"cases"})
    cases = data["cases"]
    if not isinstance(cases, list) or not cases or len(cases) > 200:
        raise PbzError("BAD_REQUEST", "cases must be a nonempty bounded list")
    ids = set()
    for case in cases:
        control.keys(case, {"id", "action", "page", "visual", "target"}, {"id", "action", "page", "visual"})
        ident = control.identifier(case["id"])
        if ident in ids or not isinstance(case["action"], str) or case["action"] not in ACTIONS:
            raise PbzError("BAD_REQUEST", "duplicate case id or unsupported action")
        ids.add(ident)
        for name in ["page", "visual"]:
            control.identifier(case[name])
        if "target" in case:
            control.identifier(case["target"])
    if mode == "native":
        return {"verb": "interactions", "status": "UNSUPPORTED", "coverage": "NONE",
                "reason": "NATIVE_ACTION_BACKEND_UNAVAILABLE", "executed": False,
                "results": [{"id": c["id"], "status": "UNSUPPORTED"} for c in cases]}
    if mode != "static":
        raise PbzError("BAD_REQUEST", "mode must be static or native")
    pages_dir = os.path.realpath(os.path.join(project.mirror_report, "definition", "pages"))
    pages = {}
    for name in os.listdir(pages_dir) if os.path.isdir(pages_dir) else []:
        path = os.path.realpath(os.path.join(pages_dir, name))
        if os.path.commonpath([pages_dir, path]) != pages_dir:
            raise PbzError("BAD_REQUEST", "page path escapes mirror")
        meta = os.path.join(path, "page.json")
        if not os.path.isfile(meta):
            continue
        with open(meta, encoding="utf-8-sig") as handle:
            page = json.load(handle)
        visuals = {}
        vdir = os.path.join(path, "visuals")
        for vid in os.listdir(vdir) if os.path.isdir(vdir) else []:
            f = os.path.realpath(os.path.join(vdir, vid, "visual.json"))
            if os.path.commonpath([pages_dir, f]) != pages_dir:
                raise PbzError("BAD_REQUEST", "visual path escapes mirror")
            if os.path.isfile(f):
                with open(f, encoding="utf-8-sig") as handle:
                    visuals[vid] = json.load(handle)
        pages[name] = (page, visuals)
    results = []
    for case in cases:
        page, visuals = pages.get(case["page"], ({}, {}))
        root = visuals.get(case["visual"], {})
        visual = root.get("visual", {})
        objects = visual.get("visualContainerObjects", {})
        link = objects.get("visualLink", [{}])[0].get("properties", {})
        action = case["action"]
        valid, reason = bool(root), "VISUAL_MISSING"
        if valid:
            if action == "slicer":
                valid = visual.get("visualType") == "slicer"
                reason = "SLICER_CONTRACT"
                group = visual.get("syncGroup", {}).get("groupName")
                fields = _fields(visual.get("query", {}))
                if group:
                    for _, peers in pages.values():
                        for peer in peers.values():
                            pv = peer.get("visual", {})
                            if pv.get("syncGroup", {}).get("groupName") == group and _fields(pv.get("query", {})) != fields:
                                valid = False
                                reason = "SYNC_FIELD_MISMATCH"
            elif action == "back":
                valid = _literal(link, "type") == "Back"
                reason = "BACK_ACTION_CONTRACT"
            elif action == "navigate":
                target = _literal(link, "navigationSection")
                valid = _literal(link, "type") == "PageNavigation" and target in pages and target == case.get("target")
                reason = "NAVIGATION_TARGET_CONTRACT"
            elif action == "drillthrough":
                target = case.get("target")
                binding = pages.get(target, ({}, {}))[0].get("pageBinding", {})
                required = set().union(*(_fields(p.get("fieldExpr", {})) for p in binding.get("parameters", [])))
                is_button = visual.get("visualType") == "actionButton"
                source_ok = (_literal(link, "type") == "Drillthrough" and _literal(link, "drillthroughSection") == target) if is_button else required <= _fields(visual.get("query", {}))
                valid = binding.get("type") == "Drillthrough" and bool(required) and source_ok and binding.get("acceptsFilterContext", "Default") == "Default"
                reason = "DRILLTHROUGH_CONTEXT_CONTRACT"
            elif action == "hover":
                help_text = objects.get("visualHeaderTooltip", [{}])[0].get("properties", {})
                tooltip = objects.get("visualTooltip", [{}])[0].get("properties", {})
                header = objects.get("visualHeader", [{}])[0].get("properties", {})
                valid = bool(_literal(help_text, "text")) and _literal(header, "showTooltipButton") == "true"
                valid |= bool(_literal(link, "tooltip") or _literal(link, "enabledTooltip"))
                target = _literal(tooltip, "section")
                valid |= bool(target in pages and pages[target][0].get("pageBinding", {}).get("type") == "Tooltip")
                reason = "TOOLTIP_CONTRACT"
        results.append({"id": case["id"], "status": "PASS" if valid else "FAIL", "reason": reason})
    return {"verb": "interactions", "result": "STATIC_ONLY", "coverage": "METADATA_ONLY", "native_executed": False,
            "results": results, "note": "Contracts only; selection, tooltip display and Back history still require native or human review."}
