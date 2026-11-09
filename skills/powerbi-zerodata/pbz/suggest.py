"""`pbz suggest`: cleanup and optimisation suggestions (Measure Killer's suggestion list, metadata only).
Each item names objects and the fix; savings are cardinality buckets from `pbz index` when known."""
from __future__ import annotations

import os

from . import lexers, pbir, usage
from .common import read_definition_text

TEXT_VISUALS = {"textbox", "image", "shape", "basicShape", "actionButton", "pageNavigator", "bookmarkNavigator",
                "group", ""}


def _unused_vars(expr: str) -> list[str]:
    sig = lexers.significant(lexers.lex_dax(expr))
    out = []
    for i, t in enumerate(sig):
        if t.kind == "ident" and t.text.upper() == "VAR" and i + 1 < len(sig) and sig[i + 1].kind == "ident":
            name = sig[i + 1].text.lower()
            later = [x for x in sig[i + 2:] if x.kind == "ident" and x.text.lower() == name]
            if not later:
                out.append(sig[i + 1].text)
    return out


def run(ws, project, extra_reports=()) -> dict:
    g = usage.build(ws, project, extra_reports)
    model = g.model
    items: list[dict] = []

    def add(sid: str, obj: str, why: str, fix: str, **kw):
        items.append({"id": sid, "object": obj, "why": why, "fix": fix, **kw})

    by_state: dict[str, int] = {}
    for k, o in g.objects.items():
        if not o.private and o.kind in ("measure", "column", "table", "relationship") and g.state[k] == "unused":
            by_state[o.kind] = by_state.get(o.kind, 0) + 1
    if by_state:
        add("UNUSED_OBJECTS", ", ".join(f"{n} {k}(s)" for k, n in sorted(by_state.items())),
            "not used by the scanned reports, the model or security", "review `pbz unused`, then `pbz unused --remove`")
    local = sorted(o.name for o in g.objects.values() if o.kind == "table" and o.private)
    if local:
        add("AUTO_DATE_TIME", f"{len(local)} auto date/time table(s)",
            "every date column gets a hidden calendar table", "turn off Auto date/time (File > Options > Data Load) "
            "and use a marked date table", tables=local[:20])
    for m in model.all_measures():
        for reg in (r for r in model.docs[m.node.file].regions if r.node is m.node and r.kind == "dax") if m.node else []:
            for v in _unused_vars(model.text_of(reg)):
                add("UNUSED_VARIABLE", f"[{m.name}]", f"VAR {v} is never used", "delete the variable")
    for t in model.tables.values():
        fields = [r for r in t.partitions if r[1] == "calculated" and "nameof" in r[2].lower()]
        if fields and r_count(fields[0][2]) == 1:
            add("SINGLE_FIELD_PARAMETER", t.name, "field parameter with one field", "use the field directly")
    for k, o in g.objects.items():
        if o.kind == "relationship" and not o.extra.get("active", True) and g.state[k] in ("unused", "used_by_unused"):
            add("INACTIVE_RELATIONSHIP_UNUSED", o.label(), "inactive and no USERELATIONSHIP/CROSSFILTER activates it",
                "delete it, or activate it in the measures that need it")
        if o.kind == "column" and g.state[k] == "locked" and not o.hidden and not g.usages.get(k) and any(
                e.via == "relationship" and g.objects[e.src].extra.get("from_table", "").lower() == o.table.lower()
                and g.objects[e.src].extra.get("from_column", "").lower() == o.name.lower() for e in g.inc.get(k, [])):
            add("VISIBLE_KEY_COLUMN", o.label(), "relationship key only, never shown", "set isHidden")
        if o.kind == "column" and o.hidden and g.state[k] in ("locked", "used") and not g.usages.get(k):
            node = model.column(o.table, o.name)
            node = node.node if node else None
            mdx = node.props.get("isAvailableInMdx", "true").strip().lower() if node else "true"
            in_hier = any(e.via in ("level", "sortBy") for e in g.inc.get(k, []))
            if mdx != "false" and not in_hier:
                add("IS_AVAILABLE_IN_MDX", o.label(), "hidden helper column keeps an attribute hierarchy",
                    "set isAvailableInMdx: false (smaller model, faster refresh)")
    for o in g.report_objects:
        if o["kind"] == "page" and o["hidden"] and o["type"] not in ("tooltip", "drillthrough") and not o.get("referenced"):
            add("HIDDEN_PAGE_UNREFERENCED", f"{o['report']} | {o['name']}", "hidden page with no navigation, tooltip "
                "or drillthrough pointing at it", "delete the page or link it")
        if o["kind"] == "visual" and o.get("hidden") and not o.get("in_bookmark"):
            add("HIDDEN_VISUAL", f"{o['report']} | {o['page']} | {o['id']}", "hidden visual no bookmark captures",
                "delete it, or add it to the bookmark that should show it")
        if o["kind"] == "custom_visual" and not o.get("referenced"):
            add("UNUSED_CUSTOM_VISUAL", f"{o['report']} | {o['id']}", "declared custom visual with no visual of that type",
                "`pbz unused --remove --only custom_visual`")
    for rname, rdir in usage.bound_reports(ws, project) if ws is not None else []:
        _report_checks(rname, rdir, model, add)
    order = ["UNUSED_OBJECTS", "AUTO_DATE_TIME", "INACTIVE_RELATIONSHIP_UNUSED", "VISIBLE_KEY_COLUMN",
             "IS_AVAILABLE_IN_MDX", "UNUSED_VARIABLE", "SINGLE_FIELD_PARAMETER", "EMPTY_FILTER", "VISUAL_WITHOUT_FIELDS",
             "HIDDEN_PAGE_UNREFERENCED", "HIDDEN_VISUAL", "UNUSED_CUSTOM_VISUAL"]
    items.sort(key=lambda x: (order.index(x["id"]) if x["id"] in order else 99, x["object"].lower()))
    counts: dict[str, int] = {}
    for it in items:
        counts[it["id"]] = counts.get(it["id"], 0) + 1
    return {"verb": "suggest", "project": project.name, "counts": counts, "suggestions": items[:300]}


def r_count(dax: str) -> int:
    return dax.upper().count("NAMEOF")


def _report_checks(rname: str, rdir: str, model, add) -> None:
    ddir = os.path.join(rdir, "definition")
    for path in pbir.report_files(rdir):
        rel = os.path.relpath(path, ddir).replace("\\", "/")
        try:
            root = pbir.parse_json(read_definition_text(path)[0])
        except (OSError, UnicodeDecodeError, pbir.JsonError):
            continue
        fc = root.get("filterConfig")
        filters = fc.get("filters") if isinstance(fc, pbir.JObj) else None
        for f in (filters.items if isinstance(filters, pbir.JArr) else []):
            if isinstance(f, pbir.JObj) and "filter" not in f:
                refs = pbir.walk_fields(f, model)
                name = f"{refs[0].table}[{refs[0].name}]" if refs else (f.get("name").value if isinstance(f.get("name"), pbir.JStr) else "?")
                add("EMPTY_FILTER", f"{rname} | {rel} | {name}", "filter card with no condition",
                    "remove it or lock it if it is meant for users")
        v = root.get("visual")
        if rel.endswith("/visual.json") and isinstance(v, pbir.JObj):
            vt = v.get("visualType").value if isinstance(v.get("visualType"), pbir.JStr) else ""
            if vt not in TEXT_VISUALS and not pbir.walk_fields(v.get("query"), model):
                add("VISUAL_WITHOUT_FIELDS", f"{rname} | {rel}", f"{vt} with no fields", "bind fields or delete it")
