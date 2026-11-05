"""Parity verbs on a synthetic project: deps resolver, usage graph states, unused / lineage / suggest,
unused --remove (TOM-validated push, cascade, restore) and m-lineage. No Desktop needed.

Run: python tests/test_parity.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import common, deps, lexers, mirror, mlineage, suggest, tmdl, unused, usage, validate  # noqa: E402

SECRETS = ["Jane Doe", "secret-host.example.invalid", "SalesDbSecret"]

MODEL = """model Model
\tculture: en-US
\tdefaultPowerBIDataSourceVersion: powerBI_V3
\tdiscourageImplicitMeasures

annotation PBI_QueryOrder = ["ServerName","Staging","Sales","Customer","Orphan"]

ref table Sales
ref table Customer
ref table 'Date'
ref table Orphan
ref table Param
ref table 'Time Calc'

ref role 'Region Reader'

ref perspective 'Sales View'

ref cultureInfo de-DE
"""

SALES = """table Sales
\tlineageTag: 10000000-0000-0000-0000-000000000001

\tmeasure 'Total Sales' = SUM(Sales[Amount])
\t\tformatString: #,0
\t\tlineageTag: 10000000-0000-0000-0000-000000000002

\tmeasure 'Sales Copy' = SUM(Sales[Amount])
\t\tlineageTag: 10000000-0000-0000-0000-000000000003

\tmeasure 'Base Unused' = [Total Sales] * 2
\t\tlineageTag: 10000000-0000-0000-0000-000000000004

\t/// Unused on purpose; the VAR is never read.
\tmeasure 'Top Unused' =
\t\t\tVAR unusedVar = 1
\t\t\tRETURN [Base Unused] + 1
\t\tlineageTag: 10000000-0000-0000-0000-000000000005

\tmeasure 'Ship Sales' = CALCULATE([Total Sales], USERELATIONSHIP(Sales[ShipDateKey], 'Date'[Date]))
\t\tlineageTag: 10000000-0000-0000-0000-000000000006

\tmeasure 'Named Sales' = CALCULATE([Total Sales], Customer[Name] = "Jane Doe")
\t\tlineageTag: 10000000-0000-0000-0000-000000000007

\tcolumn Amount
\t\tdataType: double
\t\tlineageTag: 10000000-0000-0000-0000-000000000010
\t\tsummarizeBy: sum
\t\tsourceColumn: Amount

\tcolumn CustomerKey
\t\tdataType: int64
\t\tlineageTag: 10000000-0000-0000-0000-000000000011
\t\tsummarizeBy: none
\t\tsourceColumn: CustomerKey

\tcolumn DateKey
\t\tdataType: dateTime
\t\tlineageTag: 10000000-0000-0000-0000-000000000012
\t\tsummarizeBy: none
\t\tsourceColumn: DateKey

\tcolumn ShipDateKey
\t\tdataType: dateTime
\t\tlineageTag: 10000000-0000-0000-0000-000000000013
\t\tsummarizeBy: none
\t\tsourceColumn: ShipDateKey

\tcolumn DueDateKey
\t\tdataType: dateTime
\t\tlineageTag: 10000000-0000-0000-0000-000000000014
\t\tsummarizeBy: none
\t\tsourceColumn: DueDateKey

\tcolumn Region
\t\tdataType: string
\t\tlineageTag: 10000000-0000-0000-0000-000000000015
\t\tsummarizeBy: none
\t\tsourceColumn: Region

\tcolumn Comment
\t\tdataType: string
\t\tlineageTag: 10000000-0000-0000-0000-000000000016
\t\tsummarizeBy: none
\t\tsourceColumn: Comment

\tcolumn Ghost
\t\tdataType: string
\t\tlineageTag: 10000000-0000-0000-0000-000000000017
\t\tsummarizeBy: none
\t\tsourceColumn: Ghost

\tpartition Sales = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet
\t\t\t\t    Source = Sql.Database(ServerName, "SalesDbSecret"),
\t\t\t\t    Nav = Source{[Schema="dbo",Item="FactSales"]}[Data],
\t\t\t\t    Kept = Table.SelectColumns(Nav, {"Amount", "CustomerKey", "DateKey", "ShipDateKey", "DueDateKey", "Region", "Comment", "Extra"})
\t\t\t\tin
\t\t\t\t    Kept
"""

CUSTOMER = """table Customer
\tlineageTag: 20000000-0000-0000-0000-000000000001

\tcolumn CustomerKey
\t\tdataType: int64
\t\tlineageTag: 20000000-0000-0000-0000-000000000002
\t\tsummarizeBy: none
\t\tsourceColumn: CustomerKey

\tcolumn Name
\t\tdataType: string
\t\tlineageTag: 20000000-0000-0000-0000-000000000003
\t\tsummarizeBy: none
\t\tsourceColumn: Name

\tcolumn Segment
\t\tdataType: string
\t\tlineageTag: 20000000-0000-0000-0000-000000000004
\t\tsummarizeBy: none
\t\tsourceColumn: Segment
\t\tsortByColumn: SortOrder

\tcolumn SortOrder
\t\tdataType: int64
\t\tisHidden
\t\tlineageTag: 20000000-0000-0000-0000-000000000005
\t\tsummarizeBy: none
\t\tsourceColumn: SortOrder

\tcolumn 'Unused Col'
\t\tdataType: string
\t\tlineageTag: 20000000-0000-0000-0000-000000000006
\t\tsummarizeBy: none
\t\tsourceColumn: Unused Col

\tpartition Customer = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet
\t\t\t\t    Source = #table(type table [CustomerKey = number, Name = text, Segment = text, SortOrder = number, #"Unused Col" = text], {})
\t\t\t\tin
\t\t\t\t    Source
"""

DATE = """table 'Date'
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

\tcolumn MonthNo = MONTH('Date'[Date])
\t\tdataType: int64
\t\tisHidden
\t\tlineageTag: 30000000-0000-0000-0000-000000000004
\t\tsummarizeBy: none

\tcolumn Month = FORMAT('Date'[Date], "MMM")
\t\tdataType: string
\t\tlineageTag: 30000000-0000-0000-0000-000000000005
\t\tsummarizeBy: none
\t\tsortByColumn: MonthNo

\thierarchy Calendar
\t\tlineageTag: 30000000-0000-0000-0000-000000000006

\t\tlevel Year
\t\t\tlineageTag: 30000000-0000-0000-0000-000000000007
\t\t\tcolumn: Year

\t\tlevel Month
\t\t\tlineageTag: 30000000-0000-0000-0000-000000000008
\t\t\tcolumn: Month

\tpartition Date = calculated
\t\tmode: import
\t\tsource = CALENDAR(DATE(2025, 1, 1), DATE(2026, 12, 31))
"""

ORPHAN = """table Orphan
\tlineageTag: 40000000-0000-0000-0000-000000000001

\tcolumn Code
\t\tdataType: string
\t\tlineageTag: 40000000-0000-0000-0000-000000000002
\t\tsummarizeBy: none
\t\tsourceColumn: Code

\tpartition Orphan = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet
\t\t\t\t    Source = #table(type table [Code = text], {})
\t\t\t\tin
\t\t\t\t    Source
"""

PARAM = """table Param
\tlineageTag: 50000000-0000-0000-0000-000000000001

\tcolumn Param
\t\tdataType: string
\t\tlineageTag: 50000000-0000-0000-0000-000000000002
\t\tsummarizeBy: none
\t\tsourceColumn: [Value1]
\t\tsortByColumn: 'Param Order'

\t\trelatedColumnDetails
\t\t\tgroupByColumn: 'Param Fields'

\tcolumn 'Param Fields'
\t\tdataType: string
\t\tisHidden
\t\tlineageTag: 50000000-0000-0000-0000-000000000003
\t\tsummarizeBy: none
\t\tsourceColumn: [Value2]
\t\tsortByColumn: 'Param Order'

\t\textendedProperty ParameterMetadata =
\t\t\t\t{
\t\t\t\t  "version": 3,
\t\t\t\t  "kind": 2
\t\t\t\t}

\tcolumn 'Param Order'
\t\tdataType: int64
\t\tisHidden
\t\tlineageTag: 50000000-0000-0000-0000-000000000004
\t\tsummarizeBy: sum
\t\tsourceColumn: [Value3]

\tpartition Param = calculated
\t\tmode: import
\t\tsource =
\t\t\t\t{
\t\t\t\t    ("Total Sales", NAMEOF('Sales'[Total Sales]), 0),
\t\t\t\t    ("Amount", NAMEOF('Sales'[Amount]), 1)
\t\t\t\t}
"""

TIMECALC = """table 'Time Calc'
\tlineageTag: 60000000-0000-0000-0000-000000000001

\tcalculationGroup

\t\tcalculationItem Current = SELECTEDMEASURE()

\t\tcalculationItem YTD = CALCULATE(SELECTEDMEASURE(), DATESYTD('Date'[Date]))

\tcolumn Name
\t\tdataType: string
\t\tlineageTag: 60000000-0000-0000-0000-000000000002
\t\tsummarizeBy: none
\t\tsourceColumn: Name
\t\tsortByColumn: Ordinal

\tcolumn Ordinal
\t\tdataType: int64
\t\tisHidden
\t\tlineageTag: 60000000-0000-0000-0000-000000000003
\t\tsummarizeBy: sum
\t\tsourceColumn: Ordinal

\tpartition 'Time Calc' = calculationGroup
\t\tmode: import
"""

RELS = """relationship r1
\tfromColumn: Sales.CustomerKey
\ttoColumn: Customer.CustomerKey

relationship r2
\tfromColumn: Sales.DateKey
\ttoColumn: 'Date'.Date

relationship r3
\tisActive: false
\tfromColumn: Sales.ShipDateKey
\ttoColumn: 'Date'.Date

relationship r4
\tisActive: false
\tfromColumn: Sales.DueDateKey
\ttoColumn: 'Date'.Date
"""

EXPRESSIONS = """expression ServerName = "secret-host.example.invalid" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]
\tlineageTag: 70000000-0000-0000-0000-000000000001

\tannotation PBI_ResultType = Text

expression Staging =
\t\tlet
\t\t    Source = #table(type table [A = text], {})
\t\tin
\t\t    Source
\tlineageTag: 70000000-0000-0000-0000-000000000002

\tannotation PBI_ResultType = Table
"""

ROLE = """role 'Region Reader'
\tmodelPermission: read

\ttablePermission Customer = [Name] = "Jane Doe"
"""

PERSPECTIVE = """perspective 'Sales View'

\tperspectiveTable Sales

\t\tperspectiveMeasure 'Sales Copy'

\t\tperspectiveMeasure 'Total Sales'

\t\tperspectiveColumn Comment

\tperspectiveTable Orphan

\t\tperspectiveColumn Code
"""

SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"


def col(entity, prop, schema=None):
    src = {"Entity": entity} if not schema else {"Schema": schema, "Entity": entity}
    return {"Column": {"Expression": {"SourceRef": src}, "Property": prop}}


def meas(entity, prop, schema=None):
    src = {"Entity": entity} if not schema else {"Schema": schema, "Entity": entity}
    return {"Measure": {"Expression": {"SourceRef": src}, "Property": prop}}


def visual(name, vtype, roles=None, extra=None, hidden=False):
    v = {"$schema": f"{SCHEMA}/visualContainer/2.4.0/schema.json", "name": name,
         "position": {"x": 0, "y": 0, "z": 0, "height": 100, "width": 100},
         "visual": {"visualType": vtype}}
    if roles:
        v["visual"]["query"] = {"queryState": {r: {"projections": [{"field": f, "queryRef": f"q{i}"} for i, f in enumerate(fs)]}
                                               for r, fs in roles.items()}}
    if extra:
        v["visual"].update(extra)
    if hidden:
        v["isHidden"] = True
    return v


def hier_level(level):
    return {"HierarchyLevel": {"Expression": {"Hierarchy": {"Expression": {"SourceRef": {"Entity": "Date"}},
                                                            "Hierarchy": "Calendar"}}, "Level": level}}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text if isinstance(text, str) else json.dumps(text, indent=2))


def build(root):
    write(os.path.join(root, "Parity.pbip"), json.dumps({"version": "1.0", "artifacts": [{"report": {"path": "Parity.Report"}}]}))
    d = os.path.join(root, "Parity.SemanticModel", "definition")
    write(os.path.join(root, "Parity.SemanticModel", "definition.pbism"), json.dumps({"version": "4.2", "settings": {}}))
    write(os.path.join(d, "database.tmdl"), "database\n\tcompatibilityLevel: 1600\n")
    write(os.path.join(d, "model.tmdl"), MODEL)
    write(os.path.join(d, "relationships.tmdl"), RELS)
    write(os.path.join(d, "expressions.tmdl"), EXPRESSIONS)
    for name, text in (("Sales", SALES), ("Customer", CUSTOMER), ("Date", DATE), ("Orphan", ORPHAN), ("Param", PARAM),
                       ("Time Calc", TIMECALC)):
        write(os.path.join(d, "tables", f"{name}.tmdl"), text)
    write(os.path.join(d, "roles", "Region Reader.tmdl"), ROLE)
    write(os.path.join(d, "cultures", "de-DE.tmdl"),  # never mirrored; a translated object must not be auto-removed
          "cultureInfo de-DE\n\ttranslations\n\t\tmodel Model\n\t\t\ttable Sales\n\t\t\t\tmeasure 'Sales Copy'\n"
          "\t\t\t\t\tcaption: Kopie\n")
    write(os.path.join(d, "perspectives", "Sales View.tmdl"), PERSPECTIVE)
    r = os.path.join(root, "Parity.Report")
    write(os.path.join(r, "definition.pbir"), {"version": "4.0", "datasetReference": {"byPath": {"path": "../Parity.SemanticModel"}}})
    rd = os.path.join(r, "definition")
    write(os.path.join(rd, "version.json"), {"$schema": f"{SCHEMA}/versionMetadata/1.0.0/schema.json", "version": "2.0.0"})
    write(os.path.join(rd, "report.json"), {"$schema": f"{SCHEMA}/report/3.3.0/schema.json",
                                            "publicCustomVisuals": ["denebViz0000000000000000"], "settings": {}})
    write(os.path.join(rd, "reportExtensions.json"), {
        "$schema": f"{SCHEMA}/reportExtension/1.0.0/schema.json", "name": "extension",
        "entities": [{"name": "Sales", "measures": [
            {"name": "Report Margin", "dataType": "Double", "expression": "[Total Sales] * 0.3"},
            {"name": "Dead RM", "dataType": "Double", "expression": "[Report Margin] + 1"}]}]})
    write(os.path.join(rd, "pages", "pages.json"), {"$schema": f"{SCHEMA}/pagesMetadata/1.1.0/schema.json",
                                                    "pageOrder": ["p1", "p2", "p3"], "activePageName": "p1"})
    p1 = {"$schema": f"{SCHEMA}/page/2.1.0/schema.json", "name": "p1", "displayName": "Overview",
          "displayOption": "FitToPage", "height": 720, "width": 1280,
          "filterConfig": {"filters": [
              {"name": "f1", "field": col("Customer", "Name"), "type": "Categorical",
               "filter": {"Version": 2, "From": [{"Name": "c", "Entity": "Customer", "Type": 0}],
                          "Where": [{"Condition": {"In": {"Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "c"}}, "Property": "Name"}}],
                                                          "Values": [[{"Literal": {"Value": "'Jane Doe'"}}]]}}}]}},
              {"name": "f2", "field": col("Sales", "Region"), "type": "Categorical"}]}}
    write(os.path.join(rd, "pages", "p1", "page.json"), p1)
    visuals = {
        "v_card": visual("v_card", "card", {"Values": [meas("Sales", "Total Sales")]}),
        "v_bar": visual("v_bar", "clusteredBarChart", {"Category": [col("Customer", "Segment")], "Y": [meas("Sales", "Ship Sales")]}),
        "v_slicer": visual("v_slicer", "slicer", {"Values": [col("Time Calc", "Name")]}),
        "v_param": visual("v_param", "tableEx", {"Values": [col("Param", "Param")]}),
        "v_hier": visual("v_hier", "lineChart", {"Category": [hier_level("Year"), hier_level("Month")], "Y": [meas("Sales", "Total Sales")]}),
        "v_rm": visual("v_rm", "card", {"Values": [meas("Sales", "Report Margin", "extension")]}),
        "v_btn": visual("v_btn", "actionButton", extra={"visualContainerObjects": {"visualLink": [{"properties": {
            "show": {"expr": {"Literal": {"Value": "true"}}}, "type": {"expr": {"Literal": {"Value": "'Bookmark'"}}},
            "bookmark": {"expr": {"Literal": {"Value": "'BmUsed'"}}}}}]}}),
        "v_empty": visual("v_empty", "clusteredColumnChart"),
    }
    for name, v in visuals.items():
        write(os.path.join(rd, "pages", "p1", "visuals", name, "visual.json"), v)
    write(os.path.join(rd, "pages", "p2", "page.json"), {"$schema": f"{SCHEMA}/page/2.1.0/schema.json", "name": "p2",
                                                         "displayName": "Old Page", "visibility": "HiddenInViewMode",
                                                         "displayOption": "FitToPage", "height": 720, "width": 1280})
    write(os.path.join(rd, "pages", "p2", "visuals", "v_hidden", "visual.json"),
          visual("v_hidden", "card", {"Values": [meas("Sales", "Total Sales")]}, hidden=True))
    write(os.path.join(rd, "pages", "p3", "page.json"), {"$schema": f"{SCHEMA}/page/2.1.0/schema.json", "name": "p3",
                                                         "displayName": "Tip", "visibility": "HiddenInViewMode",
                                                         "pageBinding": {"name": "tt", "type": "Tooltip"},
                                                         "displayOption": "FitToPage", "height": 240, "width": 320})
    write(os.path.join(rd, "bookmarks", "bookmarks.json"), {"$schema": f"{SCHEMA}/bookmarksMetadata/1.0.0/schema.json",
                                                            "items": [{"name": "BmUsed"}, {"name": "BmOrphan"}]})
    for b in ("BmUsed", "BmOrphan"):
        write(os.path.join(rd, "bookmarks", f"{b}.bookmark.json"),
              {"$schema": f"{SCHEMA}/bookmark/1.4.0/schema.json", "displayName": b, "name": b,
               "explorationState": {"version": "1.3", "activeSection": "p1"}})


def heuristic_canary_scan(check) -> None:
    """Every metadata verb on a copy of the Canary fixture WITHOUT an engine index (heuristic tokenization):
    no canary may appear in any verb output or mirror file."""
    import re
    import subprocess
    fixture = os.path.join(SKILL, "tests", "fixture")
    text = open(os.path.join(fixture, "CANARIES.md"), encoding="utf-8").read()
    blocks = {m.group(1): [x.strip() for x in m.group(2).splitlines() if x.strip()]
              for m in re.finditer(r"```([\w-]+)\n(.*?)```", text, re.S)}
    strings = blocks["canary-strings"] + blocks["canary-blob-strings"]
    numbers = blocks["canary-numbers"] + blocks["canary-derived-numbers"]
    exempt = {s for s in strings if "_NOCOL_" in s or "DT-LABEL" in s}  # DAX identifier / category labels (e2e rule)
    tmp = tempfile.mkdtemp(prefix="pbzcanary-")
    wsd = os.path.join(tmp, "ws")
    shutil.copytree(fixture, wsd, ignore=shutil.ignore_patterns(".pbi", "pbz-workspace", ".pbz", "CANARIES.md"))
    env = {**os.environ, "PBZ_PRIVATE_ROOT": os.path.join(tmp, "private"), "PYTHONUTF8": "1"}
    outs = []
    for args in (["pull"], ["unused", "--all", "--impact"], ["lineage", "[Canary Person Hours]", "--up"], ["lineage", "Person"],
                 ["suggest"], ["m-lineage"], ["similar"], ["changes"], ["docs", "--dax"], ["docs", "--format", "html", "--dax"],
                 ["docs", "--format", "json"], ["unused", "--remove", "--dry-run"], ["bpa"], ["layout", "--page", "Canary Page"],
                 ["format"], ["repair", "--dry-run"], ["status"], ["bpa", "--fix", "HIDE_FOREIGN_KEYS", "--dry-run"]):
        p = subprocess.run([sys.executable, "-I", os.path.join(SKILL, "pbz", "cli.py"), *args], cwd=wsd, env=env,
                           capture_output=True, timeout=600)
        outs.append((" ".join(args), p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")))
        check(p.returncode == 0, f"heuristic run {' '.join(args)} rc {p.returncode}")
    for cur, _d, files in os.walk(os.path.join(wsd, "pbz-workspace")):
        for f in files:
            outs.append((f, open(os.path.join(cur, f), encoding="utf-8", errors="replace").read()))
    for name, blob in outs:
        leaks = [s for s in strings if s not in exempt and s.lower() in blob.lower()]
        leaks += [n for n in numbers if re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", blob)]
        check(not leaks, f"heuristic-mode canary leak in {name}: {len(leaks)} hit(s)")
    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    fails: list[str] = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    heuristic_canary_scan(check)

    # ---------------------------------------------------------------- deps resolver units
    m = tmdl.Model()
    for tname, cols, meass in (("Sales", ["Amount", "Hours"], ["Total"]), ("Date", ["Date"], []), ("Calc", ["Hours"], [])):
        t = tmdl.Table(tname)
        t.columns = {c: tmdl.Column(tname, c) for c in cols}
        t.measures = {x: tmdl.Measure(tname, x) for x in meass}
        m.tables[tname] = t
    m.functions = {"AddTax": None}
    res = deps.Resolver(m)

    def refs(expr, home="Sales", **kw):
        d = deps.dax(expr, home, res, **kw)
        return sorted({(r.kind, r.table, r.name) for r in d.refs}), d

    got, _ = refs("'Sales' [Amount] + Sales[Total]")
    check(("column", "Sales", "Amount") in got and ("measure", "Sales", "Total") in got, f"qualified refs with space: {got}")
    got, _ = refs("VAR t = FILTER(Calc, Calc[Hours] > 1) RETURN SUMX(t, t[Hours]) + [Total]")
    check(("extension", "t", "Hours") in got and ("column", "Calc", "Hours") in got and ("table", "Calc", "Calc") in got,
          f"VAR table columns are not model refs: {got}")
    got, _ = refs("SUMX(Calc, [Hours])")
    check(("column", "Calc", "Hours") in got and ("column", "Sales", "Hours") not in got, f"row context table wins: {got}")
    got, _ = refs("[Hours] * 2", home="Sales", row_home=True)
    check(got == [("column", "Sales", "Hours")], f"calculated column home ref: {got}")
    got, _ = refs('CALCULATE([Total], Sales[Amount] = "«Sales[Amount]#3»", Date[Date] = dt"2026-01-01")')
    check(("measure", "Sales", "Total") in got and not any("«" in r[2] for r in got), f"tokens are literals: {got}")
    got, d = refs("(x : NUMERIC, y) => x * y + [Total] + AddTax(1)", function=True)
    check(("function", "", "AddTax") in got and not d.unresolved, f"UDF params and calls: {got} {d.unresolved}")
    got, d = refs("SUM(Sales[Nope]) + 'Broken")
    check(("unresolved", "Sales", "Nope") in got and len(d.unresolved) >= 1, f"unresolved + unterminated quote: {got}")
    toks = lexers.lex_dax("'unterminated\n+ [Total]")
    check(any(t.kind == "bracket" and t.value == "Total" for t in toks), "unterminated quote swallows the rest")
    got, d = refs('USERELATIONSHIP(Sales[Amount], Date[Date])')
    check(d.relationships and d.relationships[0][0] == "USERELATIONSHIP", f"relationship call: {d.relationships}")

    # ---------------------------------------------------------------- m-lineage units
    r = mlineage.analyse('let\n  A = #table(type table [X = text, #"Y Z" = number], {}),\n  B = Table.RenameColumns(A, {{"X", "X2"}}),\n'
                         '  C = Table.ExpandRecordColumn(B, "Y Z", {"p", "q"}, {"P", "Q"}),\n  D = Table.UnpivotOtherColumns(C, {"X2"}, "Attr", "Val")\nin\n  D', {})
    check(r.get("columns") == ["X2", "Attr", "Val"] and not r["dynamic"], f"m transfer table: {r.get('columns')}")

    # ---------------------------------------------------------------- project
    tmp = tempfile.mkdtemp(prefix="pbzparity-")
    root = os.path.join(tmp, "ws")
    build(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    res_pull = mirror.pull(ws, project)
    check(not res_pull["lossy_files"], f"lossy: {res_pull['lossy_files']}")
    outputs = []

    g = usage.build(ws, project)
    st = {g.label(k) + ("@" + o.table if o.kind == "measure" else ""): g.state[k] for k, o in g.objects.items()}
    expect = {
        "[Total Sales]@Sales": "used", "[Ship Sales]@Sales": "used", "[Sales Copy]@Sales": "unused",
        "[Base Unused]@Sales": "used_by_unused", "[Top Unused]@Sales": "unused", "[Named Sales]@Sales": "unused",
        "Sales[Amount]": "used", "Sales[Region]": "used", "Sales[Comment]": "unused", "Sales[Ghost]": "unused",
        "Sales[CustomerKey]": "locked", "Customer[CustomerKey]": "locked", "Customer[Name]": "used",
        "Customer[Segment]": "used", "Customer[SortOrder]": "used", "Customer[Unused Col]": "unused",
        "Date[Month]": "used", "Date[MonthNo]": "used", "Date[Year]": "used", "Date[Date]": "used",
        "Sales[ShipDateKey]": "used", "Sales[DueDateKey]": "used_by_unused", "Orphan": "unused", "Orphan[Code]": "unused",
        "Param": "used", "Time Calc.YTD": "used", "ServerName": "used", "Staging": "unused",
        "Sales[ShipDateKey] -> Date[Date]": "used", "Sales[DueDateKey] -> Date[Date]": "unused",
        "Sales[CustomerKey] -> Customer[CustomerKey]": "locked", "[Report Margin]": "used", "[Dead RM]": "unused",
        "Region Reader": "locked",
    }
    for lab, want in expect.items():
        check(st.get(lab) == want, f"state {lab}: {st.get(lab)} (want {want})")
    check("RLS filter in role Region Reader" in g.locks.get(usage.key("column", "Customer", "Name"), []), "RLS lock reason")

    out = unused.report(ws, project)
    outputs.append(out)
    check(out["analysis"] == "complete" and out["scope"]["reports"] == ["Parity"], f"unused scope: {out['scope']}")
    rep = {(r["kind"], r["id"]): r["state"] for r in out["report_objects"]}
    check(rep.get(("custom_visual", "denebViz0000000000000000")) == "unused" and rep.get(("bookmark", "BmOrphan")) == "unreferenced"
          and ("bookmark", "BmUsed") not in rep, f"report objects: {rep}")
    lin = unused.lineage(ws, project, "[Total Sales]")
    outputs.append(lin)
    kids = {c["object"] for c in lin["trees"][0].get("children", [])}
    check({"[Base Unused]", "[Ship Sales]", "[Report Margin]", "Param"} <= kids, f"lineage down: {kids}")
    up = unused.lineage(ws, project, "[Ship Sales]", "up")
    outputs.append(up)
    ups = {c["object"] for c in up["trees"][0].get("children", [])}
    check({"[Total Sales]", "Sales[ShipDateKey] -> Date[Date]"} <= ups, f"lineage up: {ups}")
    sg = suggest.run(ws, project)
    outputs.append(sg)
    ids = sg["counts"]
    for sid in ("UNUSED_OBJECTS", "UNUSED_VARIABLE", "INACTIVE_RELATIONSHIP_UNUSED", "HIDDEN_PAGE_UNREFERENCED",
                "HIDDEN_VISUAL", "UNUSED_CUSTOM_VISUAL", "VISUAL_WITHOUT_FIELDS", "EMPTY_FILTER", "VISIBLE_KEY_COLUMN"):
        check(sid in ids, f"suggestion {sid} missing: {ids}")
    check(not any(s["id"] == "HIDDEN_PAGE_UNREFERENCED" and "Tip" in s["object"] for s in sg["suggestions"]),
          "tooltip page flagged as unreferenced")
    ml = mlineage.run(ws, project)
    outputs.append(ml)
    sales = next(t for t in ml["tables"] if t["table"] == "Sales")
    codes = {p["code"] for p in sales["problems"]}
    check(codes == {"COLUMN_NOT_IN_QUERY", "QUERY_COLUMN_NOT_IN_MODEL"}, f"m-lineage problems: {sales['problems']}")
    cust = next(t for t in ml["tables"] if t["table"] == "Customer")
    check(not cust["problems"] and not cust["dynamic"], f"m-lineage #table: {cust}")

    # ---------------------------------------------------------------- remove, push (TOM validated), restore
    validate.report_author = lambda *a, **k: []  # schema validator needs a Desktop-made report; TOM still runs
    before = {rel: text for rel, text in mirror.mirror_texts(ws, project).items()}
    original = {rel: open(os.path.join(root, *rel.split("/")), "rb").read() for rel in before}
    dry = unused.remove(ws, project, dry_run=True)
    check(dry["result"] == "DRY_RUN" and mirror.mirror_texts(ws, project) == before, "dry run changed files")
    rm = unused.remove(ws, project)
    outputs.append(rm)
    gone = set(rm["removed"])
    check("measure [Sales Copy]" not in gone and any(s["object"].startswith("measure [Sales Copy]") and "translation" in s["reason"]
                                                     for s in rm["skipped"]), f"translated measure removed: {rm['skipped']}")
    for want in ("measure [Top Unused]", "measure [Named Sales]", "table Orphan", "m_query Staging",
                 "relationship Sales[DueDateKey] -> Date[Date]", "column Sales[Comment]", "column Customer[Unused Col]",
                 "report_measure [Dead RM]", "custom_visual denebViz0000000000000000"):
        check(want in gone, f"not removed: {want} ({sorted(gone)})")
    check("measure [Base Unused]" not in gone and "column Sales[DueDateKey]" not in gone, "used_by_unused removed in round 1")
    mt = mirror.mirror_texts(ws, project)
    sales_m = mt["Parity.SemanticModel/definition/tables/Sales.tmdl"]
    check('#"pbz Removed Columns" = Table.RemoveColumns(Kept, {' in sales_m and '"Comment"' in sales_m
          and sales_m.rstrip().endswith('#"pbz Removed Columns"'), "M removal step")
    check("ref table Orphan" not in mt["Parity.SemanticModel/definition/model.tmdl"]
          and '"Orphan"' not in mt["Parity.SemanticModel/definition/model.tmdl"], "model.tmdl ref / query order not pruned")
    persp = mt["Parity.SemanticModel/definition/perspectives/Sales View.tmdl"]
    check("Orphan" not in persp and "Comment" not in persp and "Total Sales" in persp and "Sales Copy" in persp,
          "perspective not pruned (or a kept object pruned)")
    check("Dead RM" not in mt["Parity.Report/definition/reportExtensions.json"]
          and "Report Margin" in mt["Parity.Report/definition/reportExtensions.json"], "report measure not removed")
    json.loads(mt["Parity.Report/definition/reportExtensions.json"])
    json.loads(mt["Parity.Report/definition/report.json"])
    pushed = mirror.push(ws, project)
    outputs.append(pushed)
    check(pushed["result"] == "PUSHED" and not [f for f in pushed["findings"] if f["severity"] == "error"],
          f"push after removal: {pushed.get('result')} {pushed.get('findings')}")
    check(not any(f["code"].startswith("TOM_") for f in pushed.get("findings", [])), "TOM validation did not run")
    casc = unused.remove(ws, project, cascade=True)
    outputs.append(casc)
    check("measure [Base Unused]" in casc.get("removed", []) and "column Sales[DueDateKey]" in casc.get("removed", []),
          f"cascade: {casc.get('removed')}")
    rs = unused.restore(ws, project, rm["restore_point"])
    check(rs["result"] == "CONFLICT", "restore must refuse while later edits sit on top of its files")
    rs = unused.restore(ws, project, casc["restore_point"])
    check(rs["result"] == "RESTORED", f"restore: {rs}")
    rs = unused.restore(ws, project, rm["restore_point"])
    check(rs["result"] == "RESTORED" and mirror.mirror_texts(ws, project) == before, "restore chain back to the original")
    pushed = mirror.push(ws, project)
    check(pushed["result"] == "PUSHED", f"push of the restore: {pushed.get('result')}")
    real = {rel: open(os.path.join(root, *rel.split("/")), "rb").read() for rel in before}
    check(all(real[rel] == original[rel] for rel in before), "real files not byte-identical after remove/push/restore/push")

    blob = json.dumps(outputs, ensure_ascii=False)
    for s in SECRETS:
        check(s not in blob, f"secret in verb output: {s!r}")
    shutil.rmtree(tmp, ignore_errors=True)
    for f in fails:
        print("FAIL", f)
    print("ok" if not fails else f"{len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
