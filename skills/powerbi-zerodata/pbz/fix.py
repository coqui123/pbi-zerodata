"""`pbz bpa --fix RULE`: apply the deterministic, property-only fix of a best-practice rule to the
mirror (then `pbz diff` / `pbz push`). Rules whose fix needs judgement (DAX rewrites, format choices,
relationship direction) are never auto-fixed. Names only."""
from __future__ import annotations

import os

from . import bpa, tmdl, usage
from .common import PbzError, commit_bytes, read_text
from .tokens import apply_edits

# rule id -> (property, value) set on each offending column; value None = bare flag line
PROPERTY_FIXES = {
    "HIDE_FOREIGN_KEYS": ("isHidden", None),
    "NUMERIC_COLUMN_SUMMARIZE_BY": ("summarizeBy", "none"),
    "ISAVAILABLEINMDX_FALSE_NONATTRIBUTE_COLUMNS": ("isAvailableInMdx", "false"),
}
DATE_TABLE_RULE = "DATE/CALENDAR_TABLES_SHOULD_BE_MARKED_AS_A_DATE_TABLE"
FIXABLE = sorted(PROPERTY_FIXES) + [DATE_TABLE_RULE]


def _date_key(t):
    """The date column to mark as key: the only non-calculated dateTime column, or one named Date."""
    dates = [c for c in t.columns.values() if c.data_type.lower().startswith("date") and not c.calculated]
    named = [c for c in dates if c.name.lower() == "date"]
    return named[0] if len(named) == 1 else (dates[0] if len(dates) == 1 else None)


def run(ws, project, rules: list[str], dry_run: bool = False) -> dict:
    unknown = [r for r in rules if r not in FIXABLE]
    if unknown:
        raise PbzError("UNSUPPORTED", "auto-fix exists only for: " + ", ".join(FIXABLE))
    g = usage.build(ws, project)
    cx = bpa.Cx(ws, project, g, {}, ())
    model = cx.model
    cat = {r["id"]: r for r in bpa.catalog("model")}
    edits: dict[str, list] = {}
    fixed, skipped = [], []
    for rid in rules:
        _examined, hits = bpa.CHECKS[rid](cx, cat[rid])
        for h in hits:
            node = h.node
            if node is None or rid in bpa.ignored_on(model, node):
                continue
            doc = model.docs[node.file]
            if rid == DATE_TABLE_RULE:
                t = model.table(node.name)
                key = _date_key(t) if t else None
                if key is None or key.node is None:
                    skipped.append({"rule": rid, "object": h.obj, "reason": "no single date column to mark as key"})
                    continue
                ed = tmdl.prop_edit(doc, node, "dataCategory", "Time") + tmdl.prop_edit(doc, key.node, "isKey", None)
                note = f"key {t.name}[{key.name}]: confirm uniqueness with `pbz check` (unique, no_blank_keys)"
            else:
                prop, value = PROPERTY_FIXES[rid]
                ed = tmdl.prop_edit(doc, node, prop, value)
                note = ""
            if not ed:
                continue
            edits.setdefault(node.file, []).extend(ed)
            fixed.append({"rule": rid, "object": h.obj, **({"note": note} if note else {})})
    files = []
    planned = []
    mdef = os.path.join(project.mirror_model, "definition")
    for rel, eds in edits.items():
        shown = f"{project.name}.SemanticModel/definition/{rel}"
        files.append(shown)
        path = os.path.join(mdef, *rel.split("/"))
        try:
            _text, bom = read_text(path)
        except (OSError, UnicodeError, ValueError):
            raise PbzError("REFUSED", shown) from None
        planned.append((path, apply_edits(model.docs[rel].text, sorted(set(eds))), bom))
    if planned and not dry_run:
        commit_bytes([("write", path, text, bom) for path, text, bom in planned])
    return {"verb": "bpa-fix", "project": project.name, "result": "DRY_RUN" if dry_run else ("FIXED" if fixed else "NOTHING_TO_FIX"),
            "fixed": fixed, "skipped": skipped, "files": sorted(files),
            **({} if dry_run or not fixed else {"next": "pbz diff, pbz bpa (rerun), then pbz push"})}
