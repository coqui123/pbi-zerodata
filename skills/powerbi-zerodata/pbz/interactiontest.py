"""Interaction contract checks. Static evidence is never represented as native UI execution."""
from __future__ import annotations

import hashlib
import json
import os

from . import control
from .common import PbzError, _reparse_link, anchor_mirror, open_mirror_file_readonly

ACTIONS = {"slicer", "navigate", "drillthrough", "back", "hover"}

# Frozen closed vocabulary (Round 18). Unmapped internal reasons fail closed to REFUSED.
RESULTS = frozenset({
    "MATCH", "MISMATCH", "DEFAULT_UNRESOLVED", "MISSING_ID", "RUNTIME_ONLY",
    "LEGACY", "UNKNOWN_TYPE", "UNSUPPORTED", "REFUSED",
})


def capabilities():
    return {"verb": "interactions capabilities", "contract": 2, "static_contracts": sorted(ACTIONS),
            "native_actions": [], "native": "UNSUPPORTED", "results": sorted(RESULTS),
            "note": "The installed Desktop bridge does not expose validated native action APIs. No UI or screenshot is read."}


_KNOWN = {
    "VISUAL_MISSING", "SLICER_CONTRACT", "SYNC_FIELD_MISMATCH", "BACK_ACTION_CONTRACT",
    "NAVIGATION_TARGET_CONTRACT", "DRILLTHROUGH_CONTEXT_CONTRACT", "TOOLTIP_CONTRACT",
    "DEFAULT_UNRESOLVED", "UNKNOWN_TYPE", "UNSUPPORTED_FORMAT", "UNSUPPORTED_RUNTIME",
    "NATIVE_ACTION_BACKEND_UNAVAILABLE", "REFUSED",
}


def _token(internal: str, *, ok: bool) -> str:
    """Map a recognized internal reason. Anything else is REFUSED."""
    if internal not in _KNOWN:
        return "REFUSED"
    if internal == "VISUAL_MISSING":
        return "MISSING_ID"
    if internal == "SYNC_FIELD_MISMATCH":
        return "MISMATCH"
    if internal == "DEFAULT_UNRESOLVED":
        return "DEFAULT_UNRESOLVED"
    if internal == "UNKNOWN_TYPE":
        return "UNKNOWN_TYPE"
    if internal == "UNSUPPORTED_FORMAT":
        return "LEGACY"
    if internal == "UNSUPPORTED_RUNTIME":
        return "RUNTIME_ONLY"
    if internal == "NATIVE_ACTION_BACKEND_UNAVAILABLE":
        return "UNSUPPORTED"
    if internal == "REFUSED":
        return "REFUSED"
    return "MATCH" if ok else "MISMATCH"


def _literal(properties, key):
    if not isinstance(properties, dict):
        raise TypeError("properties")
    node = properties.get(key, {})
    if node in ({}, None):
        return ""
    if not isinstance(node, dict):
        raise TypeError("property")
    expr = node.get("expr", {})
    if not isinstance(expr, dict):
        raise TypeError("expr")
    literal = expr.get("Literal", {})
    if not isinstance(literal, dict):
        raise TypeError("literal")
    value = literal.get("Value", "")
    if value == "":
        return ""
    if not isinstance(value, str):
        raise TypeError("literal")
    return value[1:-1].replace("''", "'") if value.startswith("'") else value


def _enabled(value: str) -> bool:
    """A tooltip flag is on unless the literal is empty or an explicit false."""
    return value.strip().lower() not in {"", "false", "0"}


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


def _load_json(path: str, mirror_root: str) -> dict:
    raw = open_mirror_file_readonly(path, mirror_root=mirror_root)
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise PbzError("REFUSED", "UNUSABLE") from None
    if not isinstance(data, dict):
        raise PbzError("REFUSED", "UNUSABLE")
    return data


def _spec_digest(data: dict) -> str:
    """Digest the already-validated spec object. Do not re-open the spec path."""
    blob = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "s" + hashlib.sha256(blob).hexdigest()[:12]


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
    digest = _spec_digest(data)
    if mode == "native":
        return {"verb": "interactions", "contract": 2, "status": "UNSUPPORTED", "coverage": "NONE",
                "reason": "UNSUPPORTED", "executed": False, "native_executed": False,
                "stage": "UNSUPPORTED",
                "results": [{"id": c["id"], "status": "UNSUPPORTED", "reason": "UNSUPPORTED"} for c in cases]}
    if mode != "static":
        raise PbzError("BAD_REQUEST", "mode must be static or native")

    mirror_root = anchor_mirror(ws)
    pages_dir = os.path.join(project.mirror_report, "definition", "pages")
    for link_path in (project.mirror_report, os.path.dirname(pages_dir), pages_dir):
        if _reparse_link(link_path):
            raise PbzError("UNSAFE_PATH", "refusing to write through a link")
    pages = {}
    if os.path.isdir(pages_dir):
        for name in os.listdir(pages_dir):
            path = os.path.join(pages_dir, name)
            if _reparse_link(path):
                raise PbzError("UNSAFE_PATH", "refusing to write through a link")
            if not os.path.isdir(path):
                continue
            meta = os.path.join(path, "page.json")
            if not os.path.lexists(meta):
                continue
            page = _load_json(meta, mirror_root)
            visuals = {}
            vdir = os.path.join(path, "visuals")
            if _reparse_link(vdir):
                raise PbzError("UNSAFE_PATH", "refusing to write through a link")
            if os.path.isdir(vdir):
                for vid in os.listdir(vdir):
                    if _reparse_link(os.path.join(vdir, vid)):
                        raise PbzError("UNSAFE_PATH", "refusing to write through a link")
                    f = os.path.join(vdir, vid, "visual.json")
                    if os.path.lexists(f):
                        visuals[vid] = _load_json(f, mirror_root)
            pages[name] = (page, visuals)

    results = []
    for case in cases:
        try:
            token = _one_case(case, pages)
        except PbzError:
            raise
        except (AttributeError, TypeError, KeyError, IndexError, ValueError):
            token = "REFUSED"
        if token not in RESULTS:
            token = "REFUSED"
        results.append({"id": case["id"], "status": token, "reason": token})
    return {
        "verb": "interactions",
        "contract": 2,
        "result": "STATIC_ONLY",
        "coverage": "METADATA_ONLY",
        "native_executed": False,
        "executed": False,
        "stage": "STATIC_CONTRACT",
        "spec_digest": digest,
        "results": results,
        "note": "Contracts only; selection, tooltip display and Back history still require native or human review.",
    }


def _one_case(case, pages) -> str:
    page, visuals = pages.get(case["page"], ({}, {}))
    root = visuals.get(case["visual"], {})
    if not isinstance(root, dict):
        raise TypeError("visual")
    visual = root.get("visual", {})
    if not isinstance(visual, dict):
        raise TypeError("visual")
    objects = visual.get("visualContainerObjects", {})
    if not isinstance(objects, dict):
        raise TypeError("objects")
    link_list = objects.get("visualLink") or [{}]
    if not isinstance(link_list, list) or not link_list:
        raise TypeError("link")
    link = link_list[0].get("properties", {}) if isinstance(link_list[0], dict) else {}
    if not isinstance(link, dict):
        raise TypeError("link")
    action = case["action"]
    ok, internal = bool(root), "VISUAL_MISSING"
    if ok:
        schema = str(root.get("$schema") or "")
        vtype = visual.get("visualType")
        if "legacy" in schema.lower() or (isinstance(vtype, str) and vtype.startswith("legacy.")):
            ok, internal = False, "UNSUPPORTED_FORMAT"
        elif visual.get("nativeOnly") is True or (isinstance(vtype, str) and vtype.startswith("runtime:")):
            ok, internal = False, "UNSUPPORTED_RUNTIME"
        elif not isinstance(vtype, str) or vtype not in {"slicer", "actionButton", "tableEx", "card"}:
            ok, internal = False, "UNKNOWN_TYPE"
        elif action == "slicer":
            ok = visual.get("visualType") == "slicer"
            internal = "SLICER_CONTRACT"
            group = visual.get("syncGroup", {}).get("groupName") if isinstance(visual.get("syncGroup"), dict) else None
            fields = _fields(visual.get("query", {}))
            if group:
                for _, peers in pages.values():
                    for peer in peers.values():
                        if not isinstance(peer, dict):
                            raise TypeError("peer")
                        pv = peer.get("visual", {})
                        if not isinstance(pv, dict):
                            raise TypeError("peer")
                        sync = pv.get("syncGroup")
                        peer_group = sync.get("groupName") if isinstance(sync, dict) else None
                        if peer_group == group and _fields(pv.get("query", {})) != fields:
                            ok = False
                            internal = "SYNC_FIELD_MISMATCH"
        elif action == "back":
            ok = _literal(link, "type") == "Back"
            internal = "BACK_ACTION_CONTRACT"
        elif action == "navigate":
            target = _literal(link, "navigationSection")
            ok = (_literal(link, "type") == "PageNavigation"
                  and target in pages and target == case.get("target"))
            internal = "NAVIGATION_TARGET_CONTRACT"
        elif action == "drillthrough":
            target = case.get("target")
            page_doc = pages.get(target, ({}, {}))[0]
            if not isinstance(page_doc, dict):
                raise TypeError("page")
            binding = page_doc.get("pageBinding", {})
            if not isinstance(binding, dict):
                raise TypeError("binding")
            parameters = binding.get("parameters", [])
            if not isinstance(parameters, list) or any(not isinstance(p, dict) for p in parameters):
                raise TypeError("parameters")
            required = set().union(*(_fields(p.get("fieldExpr", {}))
                                     for p in parameters if isinstance(p, dict)))
            is_button = visual.get("visualType") == "actionButton"
            if is_button:
                source_ok = (_literal(link, "type") == "Drillthrough"
                             and _literal(link, "drillthroughSection") == target)
            else:
                source_ok = required <= _fields(visual.get("query", {}))
            accepts = binding.get("acceptsFilterContext", "Default")
            typed = binding.get("type") == "Drillthrough" and bool(required) and source_ok
            if accepts == "Default" and not binding.get("type"):
                ok, internal = False, "DEFAULT_UNRESOLVED"
            else:
                ok = typed and accepts == "Default"
                internal = "DRILLTHROUGH_CONTEXT_CONTRACT"
        elif action == "hover":
            def _props(key):
                block = objects.get(key) or [{}]
                if not isinstance(block, list) or not block:
                    raise TypeError("tooltip")
                return block[0].get("properties", {}) if isinstance(block[0], dict) else {}
            help_text = _props("visualHeaderTooltip")
            tooltip = _props("visualTooltip")
            header = _props("visualHeader")
            ok = bool(_literal(help_text, "text")) and _literal(header, "showTooltipButton") == "true"
            ok |= _enabled(_literal(link, "tooltip")) or _enabled(_literal(link, "enabledTooltip"))
            target = _literal(tooltip, "section")
            tip = pages.get(target, ({}, {}))[0] if target in pages else {}
            binding = tip.get("pageBinding", {}) if isinstance(tip, dict) else {}
            ok |= bool(isinstance(binding, dict) and binding.get("type") == "Tooltip")
            internal = "TOOLTIP_CONTRACT"
        else:
            ok, internal = False, "UNKNOWN_TYPE"
    return _token(internal, ok=ok)
