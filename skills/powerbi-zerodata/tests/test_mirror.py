"""Mirror / tokenizer tests on a synthetic mini project (no engine, heuristic classification).

Run: python tests/test_mirror.py
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import common, mirror, rename, tokens, validate  # noqa: E402

SECRETS = ["Jane Doe", "jane.doe@example.invalid", "Zed Quill", "918273", "CANARY-BLOB", "$1,234,567",
           "Ana Ruiz", "ana.ruiz@example.invalid"]
VISIBLE = ["Active", "Current Year", "PTO", "Total Hours", "Owner Hours"]

BLOB = base64.b64encode(("CANARY-BLOB " * 40).encode()).decode()

MODEL_TMDL = """model Model
\tculture: en-US

ref table Person
ref table Project
"""

PERSON_TMDL = f"""/// Owners. Contact ana.ruiz@example.invalid for access.
table Person
\tlineageTag: 11111111-1111-1111-1111-111111111111

\tmeasure 'Owner Hours' = CALCULATE([Total Hours], Person[Name] = "Jane Doe", Project[Status] = "Active")
\t\tformatString: #,0
\t\tlineageTag: 22222222-2222-2222-2222-222222222222

\tmeasure 'Total Hours' =
\t\t\tVAR who = SELECTEDVALUE(Person[Name])
\t\t\tRETURN
\t\t\t\tIF(who = "Zed Quill", 0, SUM(Project[Hours])) // check with Ana Ruiz
\t\tformatString: 0.0
\t\tlineageTag: 33333333-3333-3333-3333-333333333333

\tmeasure 'PTO Hours' = CALCULATE([Total Hours], CONTAINSSTRING(Project[Work Type], "PTO"))
\t\tlineageTag: 44444444-4444-4444-4444-444444444444

\tcolumn Name
\t\tdataType: string
\t\tlineageTag: 55555555-5555-5555-5555-555555555555
\t\tsummarizeBy: none
\t\tsourceColumn: Name

\tcolumn Email
\t\tdataType: string
\t\tlineageTag: 66666666-6666-6666-6666-666666666666
\t\tsummarizeBy: none
\t\tsourceColumn: Email

\tpartition Person = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet
\t\t\t\t    Source = Table.FromRows(Json.Document(Binary.Decompress(Binary.FromText("{BLOB}", BinaryEncoding.Base64), Compression.Deflate))),
\t\t\t\t    Kept = Table.SelectRows(Source, each [Name] <> "Jane Doe" and [Email] <> "jane.doe@example.invalid")
\t\t\t\tin
\t\t\t\t    Kept
"""

PROJECT_TMDL = """table Project
\tlineageTag: 77777777-7777-7777-7777-777777777777

\tcolumn ProjectID
\t\tdataType: int64
\t\tlineageTag: 88888888-8888-8888-8888-888888888888
\t\tsummarizeBy: none
\t\tsourceColumn: ProjectID

\tcolumn Status
\t\tdataType: string
\t\tlineageTag: 99999999-9999-9999-9999-999999999999
\t\tsummarizeBy: none
\t\tsourceColumn: Status

\tcolumn 'Work Type'
\t\tdataType: string
\t\tlineageTag: aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
\t\tsummarizeBy: none
\t\tsourceColumn: Work Type

\tcolumn Hours
\t\tdataType: double
\t\tlineageTag: bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
\t\tsourceColumn: Hours

\tmeasure 'Big Project Hours' = CALCULATE(SUM(Project[Hours]), Project[ProjectID] IN {918273, 5}, Project[Hours] > 40)
\t\tlineageTag: cccccccc-cccc-cccc-cccc-cccccccccccc

\tpartition Project = m
\t\tmode: import
\t\tsource = OData.Feed("https://example.invalid/odata/Projects", null, [Implementation="2.0"])
"""

PAGE = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.1.0/schema.json",
    "name": "p1", "displayName": "Overview", "displayOption": "FitToPage", "height": 720, "width": 1280,
    "filterConfig": {"filters": [
        {"name": "f1", "field": {"Column": {"Expression": {"SourceRef": {"Entity": "Person"}}, "Property": "Name"}},
         "type": "Categorical",
         "filter": {"Version": 2, "From": [{"Name": "p", "Entity": "Person", "Type": 0}],
                    "Where": [{"Condition": {"In": {"Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "p"}}, "Property": "Name"}}],
                                                    "Values": [[{"Literal": {"Value": "'Jane Doe'"}}], [{"Literal": {"Value": "'Zed Quill'"}}]]}}}]}},
        {"name": "f2", "field": {"Column": {"Expression": {"SourceRef": {"Entity": "Project"}}, "Property": "Status"}},
         "type": "Categorical",
         "filter": {"Version": 2, "From": [{"Name": "q", "Entity": "Project", "Type": 0}],
                    "Where": [{"Condition": {"In": {"Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "q"}}, "Property": "Status"}}],
                                                    "Values": [[{"Literal": {"Value": "'Active'"}}]]}}}]}},
        {"name": "f3", "field": {"Column": {"Expression": {"SourceRef": {"Entity": "Project"}}, "Property": "ProjectID"}},
         "type": "Advanced",
         "filter": {"Version": 2, "From": [{"Name": "q", "Entity": "Project", "Type": 0}],
                    "Where": [{"Condition": {"Comparison": {"ComparisonKind": 0,
                                                            "Left": {"Column": {"Expression": {"SourceRef": {"Source": "q"}}, "Property": "ProjectID"}},
                                                            "Right": {"Literal": {"Value": "918273L"}}}}}]}}]},
}
CARD = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.10.0/schema.json",
    "name": "card1", "position": {"x": 10, "y": 10, "z": 0, "width": 200, "height": 100},
    "visual": {"visualType": "card", "query": {"queryState": {"Values": {"projections": [
        {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "Person"}}, "Property": "Owner Hours"}},
         "queryRef": "Person.Owner Hours", "nativeQueryRef": "Owner Hours"}]}}},
        "objects": {"general": [{"properties": {"title": {"expr": {"Literal": {"Value": "'Hours for Current Year'"}}}}}]}},
}
TEXTBOX = {
    "$schema": CARD["$schema"], "name": "tb1", "position": {"x": 10, "y": 120, "z": 1, "width": 300, "height": 60},
    "visual": {"visualType": "textbox", "objects": {"general": [{"properties": {"paragraphs": [
        {"textRuns": [{"value": "Budget was $1,234,567 in 2026, owner Jane Doe."}]}]}}]}},
}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def build(root):
    write(os.path.join(root, "Mini.pbip"), json.dumps({"version": "1.0", "artifacts": [{"report": {"path": "Mini.Report"}}]}))
    d = os.path.join(root, "Mini.SemanticModel", "definition")
    write(os.path.join(d, "model.tmdl"), MODEL_TMDL)
    write(os.path.join(d, "tables", "Person.tmdl"), PERSON_TMDL)
    write(os.path.join(d, "tables", "Project.tmdl"), PROJECT_TMDL.replace("\n", "\r\n"))  # CRLF file
    write(os.path.join(root, "Mini.SemanticModel", "definition.pbism"), json.dumps({"version": "4.2", "settings": {}}))
    write(os.path.join(root, "Mini.SemanticModel", ".pbi", "cache.abf"), "binary")
    r = os.path.join(root, "Mini.Report", "definition")
    write(os.path.join(root, "Mini.Report", "definition.pbir"), json.dumps({"version": "4.0", "datasetReference": {"byPath": {"path": "../Mini.SemanticModel"}}}))
    write(os.path.join(r, "pages", "p1", "page.json"), json.dumps(PAGE, indent=2))
    write(os.path.join(r, "pages", "p1", "visuals", "card1", "visual.json"), json.dumps(CARD, indent=2))
    write(os.path.join(r, "pages", "p1", "visuals", "tb1", "visual.json"), json.dumps(TEXTBOX, indent=2))


def read_tree(base):
    out = {}
    for cur, _d, files in os.walk(base):
        for f in files:
            p = os.path.join(cur, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, base)] = fh.read()
    return out


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="pbzmirror-")
    root = os.path.join(tmp, "ws")
    build(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")  # keep test secrets out of the real store
    project = ws.project()
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    before = read_tree(os.path.join(root))
    res = mirror.pull(ws, project)
    check(not res["lossy_files"], f"lossy files: {res['lossy_files']}")
    mtexts = {rel: open(os.path.join(ws.mirror, *rel.split("/")), encoding="utf-8").read()
              for rel in mirror.load_manifest(ws, project)["files"]}
    blob = "\n".join(mtexts.values())
    for s in SECRETS:
        check(s not in blob, f"secret visible in mirror: {s!r}")
    for v in VISIBLE:
        check(v in blob, f"expected visible text missing: {v!r}")
    check("«Person[Name]#" in blob, "column-bound token for Person[Name] missing")
    check("«payload:Person#" in blob, "Enter Data payload marker missing")
    check("«pii#" in blob, "pii token missing")
    check("«num#" in blob, "textbox number token missing")
    check(not os.path.exists(os.path.join(ws.mirror, "Mini.SemanticModel", ".pbi")), ".pbi mirrored")
    check(os.path.exists(os.path.join(ws.mirror, "MIRROR.md")), "MIRROR.md missing")

    # zero-edit push is a no-op and real files are untouched
    check(mirror.push(ws, project)["result"] == "NOTHING_TO_PUSH", "zero-edit push not a no-op")
    check(read_tree(root) == {**before, **{k: v for k, v in read_tree(root).items() if k.startswith(("pbz-workspace", ".pbz"))}},
          "real files changed by pull")

    # edit the mirror: change a format string, copy a token into a new measure, add a visible literal
    pt = os.path.join(ws.mirror, "Mini.SemanticModel", "definition", "tables", "Person.tmdl")
    text = open(pt, encoding="utf-8", newline="").read()
    tok = next(t for t in tokens.TOKEN_RE.findall(text) if t.startswith("«Person[Name]#"))
    text = text.replace("formatString: #,0", "formatString: #,0.0")
    text += f'\n\tmeasure \'Copy Hours\' = CALCULATE([Total Hours], Person[Name] = "{tok}", Project[Status] = "Closed")\n\t\tlineageTag: dddddddd-dddd-dddd-dddd-dddddddddddd\n'
    open(pt, "w", encoding="utf-8", newline="").write(text)
    validate.run_stage = lambda *a, **k: []  # report validator needs a full Desktop-made report; covered in e2e
    out = mirror.push(ws, project)
    check(out["result"] == "PUSHED", f"push failed: {out}")
    real = open(os.path.join(root, "Mini.SemanticModel", "definition", "tables", "Person.tmdl"), encoding="utf-8").read()
    check("formatString: #,0.0" in real, "edit not pushed")
    check('Person[Name] = "Jane Doe"' in real and real.count("Jane Doe") >= 3, "token not restored in real file")
    check("«" not in real, "token leaked into real file")
    check("Closed" in real, "new literal not pushed")
    check(BLOB in real, "payload not restored exactly")
    proj_real = open(os.path.join(root, "Mini.SemanticModel", "definition", "tables", "Project.tmdl"), "rb").read()
    check(proj_real == before[os.path.join("Mini.SemanticModel", "definition", "tables", "Project.tmdl")], "untouched file changed")

    # rename a measure used by a visual, then push
    r = rename.rename(ws, project, "measure", "Owner Hours", "Owner Hrs")
    check(any("card1" in f for f in r["files"]), f"rename missed visual: {r}")
    out = mirror.push(ws, project)
    check(out["result"] == "PUSHED", f"rename push failed: {out}")
    card = open(os.path.join(root, "Mini.Report", "definition", "pages", "p1", "visuals", "card1", "visual.json"), encoding="utf-8").read()
    check('"Owner Hrs"' in card and "Person.Owner Hrs" in card and "Owner Hours" not in card, "visual refs not renamed")
    page = open(os.path.join(root, "Mini.Report", "definition", "pages", "p1", "page.json"), encoding="utf-8").read()
    check(page == json.dumps(PAGE, indent=2), "page.json not byte-identical after round trip")

    # describe / smoke query builder / handoff / scan (metadata only, no engine)
    from pbz import describe, handoff, pbir as pbir_mod, scan, smoke, tmdl as tmdl_mod
    ov = describe.overview(ws, project)
    check([t["table"] for t in ov["tables"]] == ["Person", "Project"], f"overview tables: {ov['tables']}")
    dm = describe.describe(ws, project, "[Owner Hrs]")
    check(dm["used_by_visuals"] and "«Person[Name]#" in dm["dax"], f"describe measure: {dm}")
    dc = describe.describe(ws, project, "'Project'[Status]")
    check(dc["class"] == "category", f"describe column class: {dc}")
    model = tmdl_mod.load_model(os.path.join(project.mirror_model, "definition"))
    tm = tokens.TokenMap(ws.private)
    vroot = pbir_mod.parse_json(open(os.path.join(project.mirror_report, "definition", "pages", "p1", "visuals", "card1", "visual.json"), encoding="utf-8").read())
    proot = pbir_mod.parse_json(open(os.path.join(project.mirror_report, "definition", "pages", "p1", "page.json"), encoding="utf-8").read())
    dax, note = smoke.build_query(vroot, proot, model, tm)
    check(dax and "TREATAS" in dax and '"Jane Doe"' in dax and "[Owner Hrs]" in dax and "918273" in dax,
          f"smoke query: {dax!r} {note!r}")
    ho = handoff.build(ws, project)
    check(ho.get("checklist") and "Overview" in ho["checklist"] and "Jane" not in ho["checklist"], f"handoff: {ho}")
    write(os.path.join(root, "exports", "a.csv"), "x")
    write(os.path.join(root, "exports", "b.csv"), "y")
    write(os.path.join(root, "report-figures.md"), "Totals: $1,234 and 45% and 12,345 rows and 9,876 more " * 20)
    write(os.path.join(root, "tools", "pull.py"), "import requests\nrequests.get('https://x/odata/Projects?$expand=Fields')\n")
    sc = scan.scan(ws, apply=True)
    check("exports/**" in sc["added"]["deny"] and "report-figures.md" in sc["added"]["deny"], f"scan deny: {sc['added']}")
    check("pull.py" in sc["added"]["deny_scripts"], f"scan scripts: {sc['added']}")

    # conflict: real file changes behind the mirror's back
    rp = os.path.join(root, "Mini.SemanticModel", "definition", "tables", "Project.tmdl")
    open(rp, "ab").write(b"\r\n")
    mp = os.path.join(ws.mirror, "Mini.SemanticModel", "definition", "tables", "Project.tmdl")
    open(mp, "a", encoding="utf-8", newline="").write("\r\n\t// note\r\n")
    check(mirror.push(ws, project)["result"] == "CONFLICT", "conflict not detected")

    shutil.rmtree(tmp, ignore_errors=True)
    for f in fails:
        print("FAIL", f)
    print("ok" if not fails else f"{len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
