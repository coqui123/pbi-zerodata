"""Tests for `pbz similar`, `pbz changes` and `pbz docs` on a synthetic mini project (no engine).

Run: python tests/test_tools.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import changes, common, docs, mirror, rename, similar, validate  # noqa: E402

SECRETS = ["Zed Quill", "jane.doe@example.invalid", "CANARY-TOOLS-SECRET", "918273"]

LONG = ("CALCULATE(DIVIDE(SUM(Hours[Hours]), COUNTROWS(FILTER(Hours, Hours[Hours] {op} 0 && NOT ISBLANK(Hours[Person])"
        " && Hours[Kind] <> \"Admin\"))), REMOVEFILTERS(Hours[Kind]), KEEPFILTERS(Hours[Hours] < 1000))")

MODEL_TMDL = """model Model
\tculture: en-US

ref table Hours
ref table Person
ref table Date
"""

HOURS_TMDL = """table Hours
\tlineageTag: 10000000-0000-0000-0000-000000000001

\tmeasure 'Total Hours' = SUM(Hours[Hours])
\t\tformatString: #,0
\t\tlineageTag: 10000000-0000-0000-0000-000000000002

\tmeasure 'Total Hours Copy' = SUM ( Hours[Hours] )
\t\tformatString: #,0
\t\tlineageTag: 10000000-0000-0000-0000-000000000003

\tmeasure 'Hours Alias' = [Total Hours]
\t\tlineageTag: 10000000-0000-0000-0000-000000000004

\tmeasure 'Hours 2025' = CALCULATE([Total Hours], 'Date'[Year] = 2025, Hours[Kind] = "Project")
\t\tlineageTag: 10000000-0000-0000-0000-000000000005

\tmeasure 'Hours 2026' = CALCULATE([Total Hours], 'Date'[Year] = 2026, Hours[Kind] = "Project")
\t\tlineageTag: 10000000-0000-0000-0000-000000000006

\tmeasure 'Avg Hours' = """ + LONG.format(op=">") + """
\t\tformatString: 0.0
\t\tlineageTag: 10000000-0000-0000-0000-000000000007

\tmeasure 'Average Hours' = """ + LONG.format(op=">=") + """
\t\tformatString: 0.0
\t\tlineageTag: 10000000-0000-0000-0000-000000000008

\tmeasure 'Secret Hours' = CALCULATE([Total Hours], Person[Name] = "Zed Quill")
\t\tlineageTag: 10000000-0000-0000-0000-000000000009

\tmeasure Weird = VAR x = MAX(Hours[Hours]) RETURN x * 2
\t\tlineageTag: 10000000-0000-0000-0000-000000000010

\tmeasure 'Odd Ratio' = DIVIDE([Total Hours], [Hours Alias])
\t\tlineageTag: 10000000-0000-0000-0000-000000000011

\tcolumn Person
\t\tdataType: string
\t\tlineageTag: 10000000-0000-0000-0000-000000000012
\t\tsummarizeBy: none
\t\tsourceColumn: Person

\tcolumn Kind
\t\tdataType: string
\t\tlineageTag: 10000000-0000-0000-0000-000000000013
\t\tsummarizeBy: none
\t\tsourceColumn: Kind

\tcolumn Date
\t\tdataType: dateTime
\t\tlineageTag: 10000000-0000-0000-0000-000000000014
\t\tsummarizeBy: none
\t\tsourceColumn: Date

\tcolumn Hours
\t\tdataType: double
\t\tlineageTag: 10000000-0000-0000-0000-000000000015
\t\tsourceColumn: Hours

\tpartition Hours = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet
\t\t\t\t    Source = #"Base Query",
\t\t\t\t    Typed = Table.TransformColumnTypes(Source, {{"Hours", type number}})
\t\t\t\tin
\t\t\t\t    Typed
"""

PERSON_TMDL = """table Person
\tlineageTag: 20000000-0000-0000-0000-000000000001

\tcolumn Name
\t\tdataType: string
\t\tlineageTag: 20000000-0000-0000-0000-000000000002
\t\tsummarizeBy: none
\t\tsourceColumn: Name

\tcolumn Email
\t\tdataType: string
\t\tlineageTag: 20000000-0000-0000-0000-000000000003
\t\tsummarizeBy: none
\t\tsourceColumn: Email

\tpartition Person = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet
\t\t\t\t    Source = OData.Feed("https://example.invalid/odata/People", null, [Implementation="2.0"]),
\t\t\t\t    Kept = Table.SelectRows(Source, each [Email] <> "jane.doe@example.invalid")
\t\t\t\tin
\t\t\t\t    Kept
"""

DATE_TMDL = """table Date
\tlineageTag: 30000000-0000-0000-0000-000000000001
\tdataCategory: Time

\tcolumn Date
\t\tdataType: dateTime
\t\tisKey
\t\tlineageTag: 30000000-0000-0000-0000-000000000002
\t\tsummarizeBy: none
\t\tsourceColumn: [Date]

\tcolumn Year = YEAR('Date'[Date])
\t\tdataType: int64
\t\tlineageTag: 30000000-0000-0000-0000-000000000003
\t\tsummarizeBy: none

\tpartition Date = calculated
\t\tmode: import
\t\tsource = CALENDAR(DATE(2025, 1, 1), DATE(2026, 12, 31))
"""

REL_TMDL = """relationship 40000000-0000-0000-0000-000000000001
\tfromColumn: Hours.Person
\ttoColumn: Person.Name

relationship 40000000-0000-0000-0000-000000000002
\tfromColumn: Hours.Date
\ttoColumn: Date.Date
"""

EXPR_TMDL = """expression 'Base Query' =
\t\tlet
\t\t    Source = OData.Feed("https://example.invalid/odata/Hours", null, [Implementation="2.0"]),
\t\t    Rows = Table.SelectRows(Source, each [Hours] > 0)
\t\tin
\t\t    Rows
\tlineageTag: 50000000-0000-0000-0000-000000000001
"""

SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"


def field(kind, entity, prop):
    return {kind: {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


PAGE = {
    "$schema": f"{SCHEMA}/page/2.1.0/schema.json", "name": "p1", "displayName": "Overview",
    "displayOption": "FitToPage", "height": 720, "width": 1280,
    "filterConfig": {"filters": [
        {"name": "f1", "field": field("Column", "Person", "Name"), "type": "Categorical",
         "filter": {"Version": 2, "From": [{"Name": "p", "Entity": "Person", "Type": 0}],
                    "Where": [{"Condition": {"In": {
                        "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "p"}}, "Property": "Name"}}],
                        "Values": [[{"Literal": {"Value": "'CANARY-TOOLS-SECRET'"}}]]}}}]}}]},
}
PAGES = {"$schema": f"{SCHEMA}/pagesMetadata/1.1.0/schema.json", "pageOrder": ["p1"], "activePageName": "p1"}
CARD = {
    "$schema": f"{SCHEMA}/visualContainer/2.4.0/schema.json", "name": "card1",
    "position": {"x": 10, "y": 10, "z": 0, "width": 200, "height": 100},
    "visual": {"visualType": "card", "query": {"queryState": {"Values": {"projections": [
        {"field": field("Measure", "Hours", "Total Hours"), "queryRef": "Hours.Total Hours"}]}}}},
}
BAR = {
    "$schema": f"{SCHEMA}/visualContainer/2.4.0/schema.json", "name": "bar1",
    "position": {"x": 300, "y": 10, "z": 1, "width": 400, "height": 300},
    "visual": {"visualType": "clusteredBarChart", "query": {"queryState": {
        "Category": {"projections": [{"field": field("Column", "Person", "Name"), "queryRef": "Person.Name"}]},
        "Y": {"projections": [{"field": field("Measure", "Hours", "Hours 2025"), "queryRef": "Hours.Hours 2025"}]}}}},
}
TEXTBOX = {
    "$schema": f"{SCHEMA}/visualContainer/2.4.0/schema.json", "name": "tb1",
    "position": {"x": 10, "y": 400, "z": 2, "width": 300, "height": 60},
    "visual": {"visualType": "textbox", "objects": {"general": [{"properties": {"paragraphs": [
        {"textRuns": [{"value": "Budget code 918273 owner Zed Quill"}]}]}}]}},
}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def build(root):
    write(os.path.join(root, "Tools.pbip"), json.dumps({"version": "1.0", "artifacts": [{"report": {"path": "Tools.Report"}}]}))
    d = os.path.join(root, "Tools.SemanticModel", "definition")
    write(os.path.join(d, "model.tmdl"), MODEL_TMDL)
    write(os.path.join(d, "tables", "Hours.tmdl"), HOURS_TMDL)
    write(os.path.join(d, "tables", "Person.tmdl"), PERSON_TMDL)
    write(os.path.join(d, "tables", "Date.tmdl"), DATE_TMDL)
    write(os.path.join(d, "relationships.tmdl"), REL_TMDL)
    write(os.path.join(d, "expressions.tmdl"), EXPR_TMDL)
    write(os.path.join(root, "Tools.SemanticModel", "definition.pbism"), json.dumps({"version": "4.2", "settings": {}}))
    r = os.path.join(root, "Tools.Report", "definition")
    write(os.path.join(root, "Tools.Report", "definition.pbir"),
          json.dumps({"version": "4.0", "datasetReference": {"byPath": {"path": "../Tools.SemanticModel"}}}))
    write(os.path.join(r, "pages", "pages.json"), json.dumps(PAGES, indent=2))
    write(os.path.join(r, "pages", "p1", "page.json"), json.dumps(PAGE, indent=2))
    write(os.path.join(r, "pages", "p1", "visuals", "card1", "visual.json"), json.dumps(CARD, indent=2))
    write(os.path.join(r, "pages", "p1", "visuals", "bar1", "visual.json"), json.dumps(BAR, indent=2))
    write(os.path.join(r, "pages", "p1", "visuals", "tb1", "visual.json"), json.dumps(TEXTBOX, indent=2))


def edit(path, old, new):
    text = open(path, encoding="utf-8", newline="").read()
    assert old in text, f"edit anchor missing in {path}: {old!r}"
    open(path, "w", encoding="utf-8", newline="").write(text.replace(old, new, 1))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="pbztools-")
    root = os.path.join(tmp, "ws")
    build(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")  # keep test secrets out of the real store
    project = ws.project()
    fails, outputs = [], []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    def keep(obj):
        outputs.append(json.dumps(obj, ensure_ascii=False))
        return obj

    res = mirror.pull(ws, project)
    check(not res["lossy_files"], f"lossy files: {res['lossy_files']}")
    blob = "\n".join(mirror.mirror_texts(ws, project).values())
    for s in SECRETS:
        check(s not in blob, f"secret visible in mirror (test setup): {s!r}")

    # ---- similar
    sim = keep(similar.run(ws, project))
    ident = [set(g["measures"]) for g in sim["identical"]]
    check({"[Total Hours]", "[Total Hours Copy]"} in ident, f"identical group: {sim['identical']}")
    check({"measure": "[Hours Alias]", "table": "Hours", "alias_of": "[Total Hours]"} in sim["aliases"],
          f"alias: {sim['aliases']}")
    shape = next((g for g in sim["same_shape"] if {"[Hours 2025]", "[Hours 2026]"} <= set(g["measures"])), None)
    check(shape is not None and any("literal differs" in d["slots"] for d in shape["differences"]),
          f"same shape: {sim['same_shape']}")
    near = next((p for p in sim["pairs"] if {p["a"], p["b"]} == {"[Avg Hours]", "[Average Hours]"}), None)
    check(near is not None and near["class"] == "near_duplicate", f"near duplicate: {sim['pairs']}")
    check(not any({p["a"], p["b"]} == {"[Total Hours]", "[Total Hours Copy]"} for p in sim["pairs"]),
          "identical measures also listed as a fuzzy pair")
    check(not any("[Odd Ratio]" in (p["a"], p["b"]) and "[Hours Alias]" in (p["a"], p["b"]) for p in sim["pairs"]),
          "dependent pair listed")

    # ---- changes: nothing pending right after pull
    ch = keep(changes.run(ws, project))
    check(ch.get("result") == "NO_CHANGES", f"fresh pull not clean: {ch}")

    mdef = os.path.join(project.mirror_model, "definition")
    hours = os.path.join(mdef, "tables", "Hours.tmdl")
    edit(hours, "MAX(Hours[Hours]) RETURN x * 2", "MAX(Hours[Hours]) RETURN x * 3 + [Total Hours]")   # logic
    edit(hours, "'Date'[Year] = 2025", "'Date'[Year] = 2024")                                           # literal only
    edit(hours, "SUM ( Hours[Hours] )", "SUM (  Hours[Hours]  )")                                       # format only
    edit(hours, "formatString: 0.0\n\t\tlineageTag: 10000000-0000-0000-0000-000000000007",
         "formatString: 0.00\n\t\tlineageTag: 10000000-0000-0000-0000-000000000007")                   # property
    edit(hours, "Typed = Table.TransformColumnTypes(Source, {{\"Hours\", type number}})",
         "Typed = Table.TransformColumnTypes(Source, {{\"Hours\", type number}}),\n\t\t\t\t    Top = Table.FirstN(Typed, 10)")
    edit(hours, "in\n\t\t\t\t    Typed", "in\n\t\t\t\t    Top")                                             # M logic
    rename.rename(ws, project, "measure", "Odd Ratio", "Odd Ratio 2")                                   # rename
    rdef = os.path.join(project.mirror_report, "definition")
    edit(os.path.join(rdef, "pages", "p1", "visuals", "card1", "visual.json"), '"x": 10,', '"x": 20,')  # moved
    write(os.path.join(rdef, "pages", "p2", "page.json"),
          json.dumps({**{k: v for k, v in PAGE.items() if k != "filterConfig"}, "name": "p2", "displayName": "Detail"}, indent=2))
    edit(os.path.join(rdef, "pages", "pages.json"), '"p1"\n  ]', '"p1",\n    "p2"\n  ]')                 # added page

    ch = keep(changes.run(ws, project, "last", "mirror"))
    check(ch.get("result") == "CHANGED", f"changes result: {ch.get('result')}")
    model = {(r["kind"], r["name"]): r for r in ch.get("model", [])}
    report = ch.get("report", [])

    def expr_class(kind, name):
        r = model.get((kind, name), {})
        return [e.get("class") for e in r.get("expressions", [])], r

    cls, r = expr_class("measure", "Weird")
    check(cls == ["logic"] and "[Total Hours]" in r["expressions"][0].get("refs_added", []) and
          r["expressions"][0].get("literals_changed") == 1, f"logic change: {r}")
    cls, r = expr_class("measure", "Hours 2025")
    check(cls == ["literal_only"] and r["expressions"][0].get("literals_changed") == 1, f"literal-only change: {r}")
    cls, r = expr_class("measure", "Total Hours Copy")
    check(cls == ["format_only"], f"format-only change: {r}")
    r = model.get(("measure", "Avg Hours"), {})
    check({"prop": "formatString", "from": "0.0", "to": "0.00"} in r.get("props", []), f"property change: {r}")
    r = model.get(("measure", "Odd Ratio 2"), {})
    check(r.get("status") == "renamed" and r.get("renamed_from") == "Odd Ratio", f"rename: {r}")
    cls, r = expr_class("partition", "Hours")
    check(cls == ["logic"], f"M change: {r}")
    tot = model.get(("measure", "Total Hours"))
    check(tot is None, f"untouched measure reported: {tot}")
    w = model.get(("measure", "Weird"), {})
    check("impact" not in w or isinstance(w["impact"].get("visuals"), int), f"impact shape: {w}")
    check(any(x["kind"] == "visual" and x["id"] == "card1" and x["status"] == "moved" for x in report),
          f"moved visual: {report}")
    check(any(x["kind"] == "page" and x["id"] == "Detail" and x["status"] == "added" for x in report),
          f"added page: {report}")
    check(any(x["kind"] == "page order" and x["status"] == "modified" for x in report), f"page order: {report}")
    r = model.get(("measure", "Hours 2025"), {})
    check(r.get("impact", {}).get("visuals") == 1, f"impact of a measure on one visual: {r}")

    # push, then the same changes appear between the pull and push snapshots
    validate.run_stage = lambda *a, **k: []  # report validator needs a Desktop-made report; covered in e2e
    out = mirror.push(ws, project)
    check(out.get("result") == "PUSHED", f"push failed: {out}")
    snaps = keep(changes.list_snapshots(ws, project))["snapshots"]
    check([s["event"] for s in snaps][-2:] == ["pull", "push"], f"snapshots: {snaps}")
    ch2 = keep(changes.run(ws, project, "previous", "last"))
    check({(r["kind"], r["name"], r["status"]) for r in ch2.get("model", [])} ==
          {(r["kind"], r["name"], r["status"]) for r in ch.get("model", [])}, "previous -> last differs from pending")
    check(keep(changes.run(ws, project))["result"] == "NO_CHANGES", "pending changes after push")

    # ---- docs
    for fmt in ("md", "html", "json"):
        d = keep(docs.run(ws, project, fmt))
        path = os.path.join(root, *d["file"].split("/"))
        check(os.path.exists(path), f"docs {fmt} not written")
        text = open(path, encoding="utf-8").read()
        outputs.append(text)
        check("«" not in text, f"token in docs without --dax ({fmt})")
        check("Total Hours" in text and "Overview" in text, f"docs {fmt} missing names")
        if fmt == "html":
            check("<script" not in text.lower() and "http" not in text.replace("http-equiv", ""),
                  "html docs has scripts or external links")
        if fmt == "md":
            check("erDiagram" in text and "OData.Feed" in text and "Kept" in text, "md docs missing ER diagram or M summary")
    d = keep(docs.run(ws, project, "md", dax=True))
    text = open(os.path.join(root, *d["file"].split("/")), encoding="utf-8").read()
    outputs.append(text)
    check("SUM(Hours[Hours])" in text and "«" in text, "docs --dax missing DAX or tokens")

    # ---- no planted secret anywhere an agent can see
    seen = "\n".join(outputs)
    for s in SECRETS:
        check(s not in seen, f"SECRET LEAK: {s!r}")

    shutil.rmtree(tmp, ignore_errors=True)
    for f in fails:
        print("FAIL", f)
    print("ok" if not fails else f"{len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
