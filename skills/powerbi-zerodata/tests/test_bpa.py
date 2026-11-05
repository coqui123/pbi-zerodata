"""Best practice analysis tests on two synthetic projects (no engine, heuristic classification).

Every model and report rule gets a planted positive and a near-miss negative; then suppression
annotations, the overlay file, opinion rules, the score formula and grade cutoffs, and a secret scan.
Run: python tests/test_bpa.py
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import bpa, common, mirror  # noqa: E402

SECRETS = ["Zed Quill", "jane.doe@example.invalid", "918273"]
SCHEMA_V = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.4.0/schema.json"
SCHEMA_P = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.1.0/schema.json"
SCHEMA_B = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/bookmark/1.4.0/schema.json"


def col(name, dtype, *props, source=True, expr=None):
    head = f"\tcolumn {name}" + (f" = {expr}" if expr else "")
    lines = [head, f"\t\tdataType: {dtype}", *[f"\t\t{p}" for p in props]]
    if source and not expr:
        lines.append(f"\t\tsourceColumn: {name.strip(chr(39))}")
    return "\n".join(lines) + "\n\n"


def meas(name, expr, *props):
    return "\n".join([f"\tmeasure {name} = {expr}", *[f"\t\t{p}" for p in props]]) + "\n\n"


def m_part(table):
    return (f"\tpartition {table} = m\n\t\tmode: import\n\t\tsource =\n\t\t\t\tlet\n"
            f"\t\t\t\t    Source = #table(type table [x = text], {{}})\n\t\t\t\tin\n\t\t\t\t    Source\n")


def table(name, body, part=None):
    return f"table {name}\n\tlineageTag: 00000000-0000-0000-0000-{abs(hash(name)) % 10**12:012d}\n\n" + body + \
        (part if part is not None else m_part(name))


SALES = table("Sales", "".join([
    meas("'Total Amount'", "SUM(Sales[Amount])", "formatString: #,0"),
    "\t/// Quantity sold.\n" + meas("'Total Qty'", "SUM(Sales[Qty])", "formatString: #,0"),
    meas("'Qty Copy'", "sum( Sales[Qty] )", "formatString: #,0"),
    meas("'Amount Ratio'", "[Total Amount] / [Total Qty]", "formatString: 0.00"),
    meas("'Amount Pct'", "DIVIDE([Total Amount], [Total Qty])", "formatString: 0.0%"),
    meas("'Half Amount'", "[Total Amount] / 2", "formatString: #,0"),
    meas("'Safe Amount'", "IFERROR([Total Amount], 0)", "formatString: #,0"),
    meas("'Log Amount'", "EVALUATEANDLOG([Total Amount])", "formatString: #,0"),
    meas("'Intersect Amount'", "CALCULATE([Total Amount], INTERSECT(VALUES(Sales[CustomerKey]), VALUES(Customer[CustomerKey])))", "formatString: #,0"),
    meas("'Unqualified Col'", "SUMX(Sales, [Amount])", "formatString: #,0"),
    meas("'Qualified Measure'", "Sales[Total Amount] * 2", "formatString: #,0"),
    meas("'Alias Amount'", "[Total Amount]", "formatString: #,0"),
    meas("'Filtered Amount'", "CALCULATE([Total Amount], FILTER(Sales, Sales[Qty] > 1))", "formatString: #,0"),
    meas("'Measure Filter'", "CALCULATE([Total Qty], FILTER(Customer, [Total Amount] > 100))", "formatString: #,0"),
    meas("'Filter All'", "CALCULATE([Total Amount], FILTER(ALL(Sales[Qty]), Sales[Qty] > 1))", "formatString: #,0"),
    meas("'Text Sum'", "SUM(Customer[City])", "formatString: #,0"),
    meas("'Plus Zero'", "[Total Amount] + 0", "formatString: #,0"),
    meas("'Label Text'", '"Total: " & FORMAT([Total Amount], "#,0")'),
    meas("'No Format'", "[Total Amount] * 1.1"),
    meas("'One Minus'", "1 - ([Total Qty] / [Total Amount])", "formatString: 0.0%"),
    meas("'Rel Measure'", "CALCULATE([Total Amount], USERELATIONSHIP(Sales[ShipDate], 'Date'[Date]))", "formatString: #,0"),
    meas("'Alt Customer Amount'", "CALCULATE([Total Amount], USERELATIONSHIP(Sales[AltCustomerKey], Customer[CustomerKey]))", "formatString: #,0"),
    meas("'Secret Measure'", 'CALCULATE([Total Amount], Customer[Name] = "Zed Quill", Sales[CustomerKey] = 918273)', "formatString: #,0"),
    meas("'Ignored Ratio'", "[Total Amount] / [Total Qty]", "formatString: 0.00", "",
         'annotation BestPracticeAnalyzer_IgnoreRules = {"RuleIDs":["USE_THE_DIVIDE_FUNCTION_FOR_DIVISION"]}'),
    meas("'Old Measure'", "[Total Amount] * 3", "formatString: #,0", "isHidden"),
    meas("'Helper Measure'", "[Total Amount] * 4", "formatString: #,0", "isHidden"),
    meas("'Uses Helper'", "[Helper Measure] + [Total Qty]", "formatString: #,0"),
    meas("'Dynamic Format'", "[Total Amount] * 5", "", 'formatStringDefinition = "#,0"'),
    meas("'Trailing '", "[Total Amount] * 6", "formatString: #,0"),
    meas("Broken", "SUM(Sales[NoSuchCol])", "formatString: #,0"),
    col("SaleID", "int64", "isHidden", "summarizeBy: none", "isAvailableInMdx: false"),
    col("CustomerKey", "int64", "summarizeBy: none"),
    col("AltCustomerKey", "int64", "isHidden", "summarizeBy: none", "isAvailableInMdx: false"),
    col("ProductKey", "string", "isHidden", "summarizeBy: none", "isAvailableInMdx: false"),
    col("StoreKey", "int64", "isHidden", "summarizeBy: none", "isAvailableInMdx: false"),
    col("OrderDate", "dateTime", "isHidden", "formatString: Short Date", "summarizeBy: none", "isAvailableInMdx: false",
        "", "variation Variation", "\tisDefault", "\trelationship: aaaa0009", "\tdefaultHierarchy: LocalDateTable_1a2b.'Date Hierarchy'"),
    col("ShipDate", "dateTime", "isHidden", "formatString: Short Date", "summarizeBy: none", "isAvailableInMdx: false"),
    col("DueDate", "dateTime", "isHidden", "formatString: Short Date", "summarizeBy: none", "isAvailableInMdx: false"),
    col("Amount", "double", "summarizeBy: sum"),
    col("Qty", "int64", "summarizeBy: none"),
    col("Discount", "decimal"),
    col("Notes", "string", "isHidden", "summarizeBy: none"),
    col("Margin", "decimal", "isHidden", "summarizeBy: none", "isAvailableInMdx: false"),
    col("Orphan", "string", "summarizeBy: none", source=False),
    col("'Customer City'", "string", "summarizeBy: none", expr="RELATED(Customer[City])"),
    col("'Amount x2'", "double", "isHidden", "summarizeBy: none", "isAvailableInMdx: false", expr="Sales[Amount] * 2"),
]))
SALES = "/// Owner jane.doe@example.invalid\n" + SALES

CUSTOMER = table("Customer", "".join([
    col("CustomerKey", "int64", "summarizeBy: none"),
    col("Name", "string", "summarizeBy: none"),
    col("City", "string", "summarizeBy: none"),
    col("'Capacity Band'", "string", "summarizeBy: none"),
    col("'Postal Code'", "string", "summarizeBy: none", "dataCategory: PostalCode"),
    col("RegionKey", "int64", "isHidden", "summarizeBy: none", "isAvailableInMdx: false"),
]))
REGION = table("Region", col("RegionKey", "int64", "summarizeBy: none") + col("'Region Name'", "string", "summarizeBy: none"))
PRODUCT = table("Product", col("ProductKey", "string", "summarizeBy: none") + col("Category", "string", "summarizeBy: none"))
STORE = table("Store", col("StoreKey", "string", "summarizeBy: none") + col("'Store Name'", "string", "summarizeBy: none"))
DATE = table("Date", "\tdataCategory: Time\n\n" + "".join([
    col("Date", "dateTime", "isKey", "formatString: Short Date", "summarizeBy: none"),
    col("Year", "int64", "formatString: 0", "summarizeBy: none", expr="YEAR('Date'[Date])"),
    col("'Month Name'", "string", "summarizeBy: none", "sortByColumn: 'Month Number'", expr="FORMAT('Date'[Date], \"MMMM\")"),
    col("'Month Number'", "int64", "isHidden", "summarizeBy: none", expr="MONTH('Date'[Date])"),
    col("'Month Short'", "string", "summarizeBy: none", expr="LEFT('Date'[Month Name], 3)"),
    col("'Months Since'", "string", "summarizeBy: none", expr='"x"'),
]), "\tpartition Date = calculated\n\t\tmode: import\n\t\tsource = CALENDAR(DATE(2025, 1, 1), DATE(2026, 12, 31))\n")
UPDATES = table("Updates", col("'Changed On'", "dateTime", "formatString: General Date", "summarizeBy: none")
                + col("Item", "string", "summarizeBy: none"))
HOLIDAY = table("'Holiday Calendar'", col("Day", "dateTime", "isHidden", "formatString: Short Date", "summarizeBy: none",
                                          "isAvailableInMdx: false") + col("Holiday", "string", "summarizeBy: none"))
MEASURES = table("Measures", meas("'Margin Total'", "SUM(Sales[Margin])", "formatString: #,0")
                 + col("Column1", "string", "isHidden", "summarizeBy: none", "isAvailableInMdx: false"))
FIELDS = table("Fields", "".join([
    col("Fields", "string", "summarizeBy: none", "sortByColumn: 'Fields Order'"),
    col("'Fields Fields'", "string", "isHidden", "summarizeBy: none", "isAvailableInMdx: false"),
    col("'Fields Order'", "int64", "isHidden", "summarizeBy: none"),
]), "\tpartition Fields = calculated\n\t\tmode: import\n\t\tsource =\n\t\t\t\t{\n"
    "\t\t\t\t    (\"Amount\", NAMEOF('Sales'[Total Amount]), 0),\n\t\t\t\t    (\"Qty\", NAMEOF('Sales'[Total Qty]), 1)\n\t\t\t\t}\n")
WHATIF = table("'Discount Param'", meas("'Discount Param Value'", "SELECTEDVALUE('Discount Param'[Discount Param], 0)", "formatString: 0.00")
               + col("'Discount Param'", "decimal", "formatString: 0.00", "summarizeBy: none"),
               "\tpartition 'Discount Param' = calculated\n\t\tmode: import\n\t\tsource = GENERATESERIES(0, 1, 0.05)\n")
CALCGROUP = table("'Time Calc'", "\tcalculationGroup\n\t\tprecedence: 1\n\n\t\tcalculationItem Current = SELECTEDMEASURE()\n\n"
                  "\t\tcalculationItem 'Prior Year' = CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR('Date'[Date]))\n\n"
                  + col("Name", "string", "summarizeBy: none", "sortByColumn: Ordinal")
                  + col("Ordinal", "int64", "isHidden", "summarizeBy: none"),
                  "\tpartition 'Time Calc' = calculationGroup\n\t\tmode: import\n")
LOCAL = ("table LocalDateTable_1a2b\n\tisHidden\n\tshowAsVariationsOnly\n\n"
         + col("Date", "dateTime", "isHidden", "summarizeBy: none")
         + col("Year", "int64", "isHidden", "summarizeBy: none", expr="YEAR([Date])")
         + "\thierarchy 'Date Hierarchy'\n\n\t\tlevel Year\n\t\t\tcolumn: Year\n\n"
         + "\tpartition LocalDateTable_1a2b = calculated\n\t\tmode: import\n"
           "\t\tsource = Calendar(Date(2025, 1, 1), Date(2026, 12, 31))\n")


def rel(rid, frm, to, *props):
    return "\n".join([f"relationship {rid}", *[f"\t{p}" for p in props], f"\tfromColumn: {frm}", f"\ttoColumn: {to}"]) + "\n\n"


RELS = "".join([
    rel("aaaa0001", "Sales.CustomerKey", "Customer.CustomerKey"),
    rel("aaaa0002", "Sales.AltCustomerKey", "Customer.CustomerKey", "isActive: false"),
    rel("aaaa0003", "Sales.ProductKey", "Product.ProductKey"),
    rel("aaaa0004", "Sales.StoreKey", "Store.StoreKey"),
    rel("aaaa0005", "Sales.OrderDate", "Date.Date"),
    rel("aaaa0006", "Sales.ShipDate", "Date.Date", "isActive: false"),
    rel("aaaa0007", "Sales.DueDate", "Date.Date", "isActive: false"),
    rel("aaaa0008", "Customer.RegionKey", "Region.RegionKey"),
    rel("aaaa0009", "Sales.OrderDate", "LocalDateTable_1a2b.Date", "joinOnDateBehavior: datePartOnly"),
    rel("aaaa0010", "'Holiday Calendar'.Day", "Date.Date"),
])
ROLES = {"Readers": "role Readers\n\tmodelPermission: read\n\n\ttablePermission Customer = UPPER(Customer[Name]) = USERPRINCIPALNAME()\n",
         "Simple": "role Simple\n\tmodelPermission: read\n\n\ttablePermission Region = Region[RegionKey] = 1\n"}
PERSPECTIVE = "perspective 'Sales View'\n\n\tperspectiveTable Sales\n\n\t\tperspectiveMeasure 'Total Amount'\n"

LITE = {
    "model.tmdl": 'model Model\n\tculture: en-US\n\nannotation BestPracticeAnalyzer_IgnoreRules = {"RuleIDs":["AVOID_FLOATING_POINT_DATA_TYPES"]}\n',
    "tables/A.tmdl": table("A", col("k", "int64") + col("j", "int64") + col("d", "dateTime", "formatString: Short Date")
                           + col("v", "double", "summarizeBy: none")),
    "tables/B.tmdl": table("B", col("k", "int64", "summarizeBy: none")),
    "tables/C.tmdl": table("C", col("j", "int64", "summarizeBy: none")),
    "tables/D.tmdl": table("D", col("id", "int64", "summarizeBy: none")),
    "tables/E.tmdl": table("E", col("id", "int64", "summarizeBy: none")),
    "tables/Empty Group.tmdl": table("'Empty Group'", "\tcalculationGroup\n\t\tprecedence: 2\n\n"
                                     + col("Name", "string", "summarizeBy: none"),
                                     "\tpartition 'Empty Group' = calculationGroup\n\t\tmode: import\n"),
    "relationships.tmdl": rel("bbbb0001", "A.k", "B.k", "crossFilteringBehavior: bothDirections")
                          + rel("bbbb0002", "A.j", "C.j", "toCardinality: many", "crossFilteringBehavior: bothDirections")
                          + rel("bbbb0003", "D.id", "E.id", "fromCardinality: one"),
    "perspectives/Empty.tmdl": "perspective Empty\n",
}


# ---------------------------------------------------------------- report


def mref(entity, prop, schema=None):
    sr = {"Entity": entity} if not schema else {"Schema": schema, "Entity": entity}
    return {"Measure": {"Expression": {"SourceRef": sr}, "Property": prop}}


def cref(entity, prop):
    return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def proj(field):
    return {"field": field, "queryRef": "x"}


def visual(vid, vtype, roles=None, z=0, **extra):
    v = {"visualType": vtype}
    if roles:
        v["query"] = {"queryState": {r: {"projections": [proj(f) for f in fs]} for r, fs in roles.items()}}
    v.update(extra.pop("visual_extra", {}))
    out = {"$schema": SCHEMA_V, "name": vid, "position": {"x": 0, "y": 0, "z": z, "width": 100, "height": 100}, "visual": v}
    out.update(extra)
    return out


def fconf(*filters):
    return {"filterConfig": {"filters": list(filters)}}


def topn(name):
    return {"name": name, "field": cref("Product", "Category"), "type": "TopN"}


def mfilter(name, prop="Total Amount"):
    return {"name": name, "field": mref("Sales", prop), "type": "Advanced"}


SLICER_FILTER = {"filter": {"Version": 2, "From": [{"Name": "c", "Entity": "Customer", "Type": 0}],
                            "Where": [{"Condition": {"In": {"Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "c"}}, "Property": "Name"}}],
                                                            "Values": [[{"Literal": {"Value": "'Zed Quill'"}}]]}}}]}}
OVERVIEW = [
    visual("v_card", "card", {"Values": [mref("Sales", "Total Amount")]},
           visual_extra={"visualContainerObjects": {"general": [{"properties": {"altText": {"expr": {"Literal": {"Value": "'Sales card'"}}}}}]}},
           **fconf(mfilter("mf1"))),
    visual("v_pie", "pieChart", {"Category": [cref("Customer", "City")], "Y": [mref("Sales", "Total Amount")]}, **fconf(topn("t1"))),
    visual("v_table", "tableEx", {"Values": [cref("Customer", "City"), cref("Customer", "Name"), cref("Product", "Category"),
                                             mref("Sales", "Total Amount"), mref("Sales", "Total Qty"), mref("Sales", "Amount Pct"),
                                             mref("Sales", "Half Amount"), mref("Sales", "Margin Total")]}, **fconf(mfilter("mf2"))),
    visual("v_col", "clusteredColumnChart", {"Category": [cref("Product", "Category")],
                                             "Y": [mref("Sales", m) for m in ("Total Amount", "Total Qty", "Amount Pct",
                                                                               "Half Amount", "Safe Amount", "Log Amount")]},
           **fconf(topn("t2"))),
    visual("v_line", "lineChart", {"Category": [cref("Date", "Year")],
                                   "Y": [{"Aggregation": {"Expression": cref("Sales", "Amount"), "Function": 0}}]}, **fconf(topn("t3"))),
    visual("v_showall", "barChart", {"Category": [cref("Product", "Category")], "Y": [mref("Sales", "Uses Helper")]},
           visual_extra={"query": {"queryState": {"Category": {"projections": [proj(cref("Product", "Category"))], "showAll": True},
                                                  "Y": {"projections": [proj(mref("Sales", "Uses Helper"))]}}}}),
    visual("v_red", "clusteredBarChart", {"Category": [cref("Product", "Category")], "Y": [mref("Sales", "Total Qty")]},
           visual_extra={"objects": {"dataPoint": [{"properties": {"fill": {"solid": {"color": {"expr": {"Literal": {"Value": "'#FF0000'"}}}}}}}]}},
           **fconf(mfilter("mf3"))),
    visual("v_theme", "clusteredBarChart", {"Category": [cref("Product", "Category")], "Y": [mref("Sales", "Total Qty")]},
           visual_extra={"objects": {"dataPoint": [{"properties": {"fill": {"solid": {"color": {"expr": {"ThemeDataColor": {"ColorId": 2, "Percent": 0}}}}}}}]}}),
    visual("v_hidden1", "card", {"Values": [mref("Sales", "Total Qty")]}, isHidden=True),
    visual("v_hidden2", "card", {"Values": [mref("Sales", "Total Qty")]}, isHidden=True),
    visual("v_ghost", "card", {"Values": [mref("Sales", "Ghost Measure")]}),
    visual("v_slicer", "slicer", {"Values": [cref("Customer", "Name")]},
           visual_extra={"objects": {"general": [{"properties": {"filter": SLICER_FILTER}}]}}),
    visual("v_custom", "usedVisual5678", {"Values": [mref("Sales", "Total Qty")]}),
    visual("v_button", "actionButton", visual_extra={"objects": {"visualLink": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "true"}}}, "type": {"expr": {"Literal": {"Value": "'PageNavigation'"}}},
        "navigationSection": {"expr": {"Literal": {"Value": "'p_linked'"}}}}}]}}),
    visual("v_rmeasure", "card", {"Values": [mref("Sales", "Report Measure", schema="extension")]}),
]
PAGES = {
    "p_over": ({"displayName": "Overview"}, OVERVIEW),
    "p_page1": ({"displayName": "Page 1", "height": 2400}, [visual("t_p1", "textbox")]),
    "p_tip": ({"displayName": "Tooltip Detail", "pageBinding": {"name": "pb1", "type": "Tooltip", "parameters": []}},
              [visual("v_tip", "card", {"Values": [mref("Sales", "Total Qty")]})]),
    "p_drill": ({"displayName": "Drill", "visibility": "HiddenInViewMode",
                 "pageBinding": {"name": "pb2", "type": "Drillthrough", "parameters": []}},
                [visual("v_drill", "card", {"Values": [mref("Sales", "Total Qty")]})]),
    "p_old": ({"displayName": "Old Hidden", "visibility": "HiddenInViewMode"}, [visual("t_old", "textbox")]),
    "p_linked": ({"displayName": "Linked Hidden", "visibility": "HiddenInViewMode"}, [visual("t_linked", "textbox")]),
    "p_busy": ({"displayName": "Busy"}, [visual(f"t_busy{i:02d}", "textbox", z=i) for i in range(22)]),
}
BOOKMARKS = {
    "bm_show": {"displayName": "Show Hidden", "options": {"targetVisualNames": ["v_hidden2"]},
                "explorationState": {"version": "1.3", "activeSection": "p_over", "sections": {"p_over": {
                    "visualContainers": {"v_hidden2": {"singleVisual": {"visualType": "card", "display": {"mode": "visible"}}}}}}}},
    "bm_dead": {"displayName": "Dead", "explorationState": {"version": "1.3", "activeSection": "p_missing"}},
    "bm_badvis": {"displayName": "Bad Visual", "options": {"targetVisualNames": ["v_nope"]},
                  "explorationState": {"version": "1.3", "activeSection": "p_over"}},
}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def wjson(path, obj):
    write(path, json.dumps(obj, indent=2))


def build(root):
    write(os.path.join(root, "Bpa.pbip"), json.dumps({"version": "1.0", "artifacts": [{"report": {"path": "Bpa.Report"}}]}))
    d = os.path.join(root, "Bpa.SemanticModel", "definition")
    write(os.path.join(root, "Bpa.SemanticModel", "definition.pbism"), json.dumps({"version": "4.2", "settings": {}}))
    write(os.path.join(d, "model.tmdl"), "model Model\n\tculture: en-US\n")
    for name, text in (("Sales", SALES), ("Customer", CUSTOMER), ("Region", REGION), ("Product", PRODUCT),
                       ("Store", STORE), ("Date", DATE), ("Updates", UPDATES), ("Holiday Calendar", HOLIDAY),
                       ("Measures", MEASURES), ("Fields", FIELDS), ("Discount Param", WHATIF), ("Time Calc", CALCGROUP),
                       ("LocalDateTable_1a2b", LOCAL)):
        write(os.path.join(d, "tables", f"{name}.tmdl"), text)
    write(os.path.join(d, "relationships.tmdl"), RELS)
    for name, text in ROLES.items():
        write(os.path.join(d, "roles", f"{name}.tmdl"), text)
    write(os.path.join(d, "perspectives", "Sales View.tmdl"), PERSPECTIVE)
    r = os.path.join(root, "Bpa.Report")
    wjson(os.path.join(r, "definition.pbir"), {"version": "4.0", "datasetReference": {"byPath": {"path": "../Bpa.SemanticModel"}}})
    rd = os.path.join(r, "definition")
    wjson(os.path.join(rd, "report.json"), {"publicCustomVisuals": ["fakeVisual1234", "usedVisual5678"]})
    wjson(os.path.join(rd, "reportExtensions.json"), {"name": "extension", "entities": [
        {"name": "Sales", "measures": [{"name": "Report Measure", "dataType": "Double", "expression": "[Total Amount] * 2"}]}]})
    wjson(os.path.join(rd, "pages", "pages.json"), {"pageOrder": list(PAGES), "activePageName": "p_over"})
    for pid, (props, visuals) in PAGES.items():
        page = {"$schema": SCHEMA_P, "name": pid, "displayOption": "FitToPage", "height": 720, "width": 1280}
        page.update(props)
        wjson(os.path.join(rd, "pages", pid, "page.json"), page)
        for v in visuals:
            wjson(os.path.join(rd, "pages", pid, "visuals", v["name"], "visual.json"), v)
    for bid, bm in BOOKMARKS.items():
        wjson(os.path.join(rd, "bookmarks", f"{bid}.bookmark.json"), {"$schema": SCHEMA_B, "name": bid, **bm})
    wjson(os.path.join(rd, "bookmarks", "bookmarks.json"), {"items": [{"name": b} for b in BOOKMARKS]})
    # second project: model-level positives (no report)
    write(os.path.join(root, "Lite.pbip"), json.dumps({"version": "1.0", "artifacts": []}))
    write(os.path.join(root, "Lite.SemanticModel", "definition.pbism"), json.dumps({"version": "4.2", "settings": {}}))
    for rel, text in LITE.items():
        write(os.path.join(root, "Lite.SemanticModel", "definition", *rel.split("/")), text)


# ---------------------------------------------------------------- checks


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="pbzbpa-")
    root = os.path.join(tmp, "ws")
    build(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")
    fails: list[str] = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    main_p, lite_p = ws.project("Bpa"), ws.project("Lite")
    for p in (main_p, lite_p):
        res = mirror.pull(ws, p)
        check(not res["lossy_files"], f"lossy files in {p.name}: {res['lossy_files']}")
    outputs = []

    def run(project, **kw):
        out = bpa.run(ws, project, **kw)
        outputs.append(json.dumps(out, ensure_ascii=False))
        return out

    def hits(out, rule):
        return [f for f in out["findings"] if f["rule"] == rule]

    def has(out, rule, obj=None, id=None):
        return any((obj is None or f["object"] == obj) and (id is None or f.get("id") == id) for f in hits(out, rule))

    out = run(main_p)
    yes = lambda rule, obj=None, id=None: check(has(out, rule, obj, id), f"expected {rule} on {obj or id}")
    no = lambda rule, obj=None, id=None: check(not has(out, rule, obj, id), f"unexpected {rule} on {obj or id}")

    # --- model rules: positive and negative
    yes("REMOVE_AUTO-DATE_TABLE", "LocalDateTable_1a2b")
    no("REMOVE_AUTO-DATE_TABLE", "Date")
    yes("DATE/CALENDAR_TABLES_SHOULD_BE_MARKED_AS_A_DATE_TABLE", "Holiday Calendar")
    no("DATE/CALENDAR_TABLES_SHOULD_BE_MARKED_AS_A_DATE_TABLE", "Updates")      # word boundary: up-dates
    no("DATE/CALENDAR_TABLES_SHOULD_BE_MARKED_AS_A_DATE_TABLE", "Date")         # marked
    no("DATE/CALENDAR_TABLES_SHOULD_BE_MARKED_AS_A_DATE_TABLE", "LocalDateTable_1a2b")
    no("MODEL_SHOULD_HAVE_A_DATE_TABLE")
    yes("AVOID_FLOATING_POINT_DATA_TYPES", "Sales[Amount]")
    no("AVOID_FLOATING_POINT_DATA_TYPES", "Sales[Margin]")                      # decimal
    yes("NUMERIC_COLUMN_SUMMARIZE_BY", "Sales[Amount]")
    yes("NUMERIC_COLUMN_SUMMARIZE_BY", "Sales[Discount]")                  # summarizeBy omitted = default
    no("NUMERIC_COLUMN_SUMMARIZE_BY", "Sales[Qty]")
    no("NUMERIC_COLUMN_SUMMARIZE_BY", "Sales[SaleID]")                     # hidden
    yes("HIDE_FOREIGN_KEYS", "Sales[CustomerKey]")
    no("HIDE_FOREIGN_KEYS", "Sales[OrderDate]")                                 # hidden
    no("HIDE_FOREIGN_KEYS", "Customer[CustomerKey]")                            # one side
    yes("RELATIONSHIP_COLUMNS_SHOULD_BE_OF_INTEGER_DATA_TYPE", "Sales[ProductKey] -> Product[ProductKey]")
    no("RELATIONSHIP_COLUMNS_SHOULD_BE_OF_INTEGER_DATA_TYPE", "Sales[OrderDate] -> Date[Date]")
    no("RELATIONSHIP_COLUMNS_SHOULD_BE_OF_INTEGER_DATA_TYPE", "Sales[CustomerKey] -> Customer[CustomerKey]")
    yes("RELATIONSHIP_COLUMNS_SAME_DATA_TYPE", "Sales[StoreKey] -> Store[StoreKey]")
    no("RELATIONSHIP_COLUMNS_SAME_DATA_TYPE", "Sales[ProductKey] -> Product[ProductKey]")
    no("AVOID_EXCESSIVE_BI-DIRECTIONAL_OR_MANY-TO-MANY_RELATIONSHIPS")
    yes("INACTIVE_RELATIONSHIPS_THAT_ARE_NEVER_ACTIVATED", "Sales[DueDate] -> Date[Date]")
    no("INACTIVE_RELATIONSHIPS_THAT_ARE_NEVER_ACTIVATED", "Sales[ShipDate] -> Date[Date]")
    no("INACTIVE_RELATIONSHIPS_THAT_ARE_NEVER_ACTIVATED", "Sales[AltCustomerKey] -> Customer[CustomerKey]")
    yes("ENSURE_TABLES_HAVE_RELATIONSHIPS", "Updates")
    for t in ("Measures", "Fields", "Discount Param", "Time Calc", "LocalDateTable_1a2b", "Sales"):
        no("ENSURE_TABLES_HAVE_RELATIONSHIPS", t)
    yes("SNOWFLAKE_SCHEMA_ARCHITECTURE", "Customer")
    no("SNOWFLAKE_SCHEMA_ARCHITECTURE", "Sales")
    no("SNOWFLAKE_SCHEMA_ARCHITECTURE", "Region")
    yes("UNNECESSARY_COLUMNS", "Sales[Notes]")
    no("UNNECESSARY_COLUMNS", "Sales[Margin]")                           # used through a used measure
    no("UNNECESSARY_COLUMNS", "Sales[Qty]")                              # visible
    yes("UNNECESSARY_MEASURES", "[Old Measure]")
    no("UNNECESSARY_MEASURES", "[Helper Measure]")                       # used by a used measure
    yes("REDUCE_USAGE_OF_CALCULATED_COLUMNS_THAT_USE_THE_RELATED_FUNCTION", "Sales[Customer City]")
    no("REDUCE_USAGE_OF_CALCULATED_COLUMNS_THAT_USE_THE_RELATED_FUNCTION", "Sales[Amount x2]")
    yes("REDUCE_USAGE_OF_CALCULATED_TABLES", "Date")
    for t in ("Fields", "Discount Param", "Time Calc", "LocalDateTable_1a2b", "Measures"):
        no("REDUCE_USAGE_OF_CALCULATED_TABLES", t)
    yes("USE_THE_DIVIDE_FUNCTION_FOR_DIVISION", "[Amount Ratio]")
    no("USE_THE_DIVIDE_FUNCTION_FOR_DIVISION", "[Amount Pct]")
    no("USE_THE_DIVIDE_FUNCTION_FOR_DIVISION", "[Half Amount]")                 # constant divisor
    no("USE_THE_DIVIDE_FUNCTION_FOR_DIVISION", "[Ignored Ratio]")               # suppressed
    check(any(s["rule"] == "USE_THE_DIVIDE_FUNCTION_FOR_DIVISION" and s["object"] == "[Ignored Ratio]"
              and s["by"] == "object annotation" for s in out.get("suppressed", [])), "object annotation not honored")
    yes("AVOID_USING_THE_IFERROR_FUNCTION", "[Safe Amount]")
    no("AVOID_USING_THE_IFERROR_FUNCTION", "[Amount Pct]")
    yes("USE_THE_TREATAS_FUNCTION_INSTEAD_OF_INTERSECT", "[Intersect Amount]")
    yes("EVALUATEANDLOG_SHOULD_NOT_BE_USED_IN_PRODUCTION_MODELS", "[Log Amount]")
    yes("DAX_COLUMNS_FULLY_QUALIFIED", "[Unqualified Col]")
    no("DAX_COLUMNS_FULLY_QUALIFIED", "[Total Amount]")
    no("DAX_COLUMNS_FULLY_QUALIFIED", "[Alias Amount]")                         # bare measure reference is fine
    yes("DAX_MEASURES_UNQUALIFIED", "[Qualified Measure]")
    no("DAX_MEASURES_UNQUALIFIED", "[Total Amount]")                            # qualified column is fine
    yes("AVOID_DUPLICATE_MEASURES", "[Total Qty]")
    yes("AVOID_DUPLICATE_MEASURES", "[Qty Copy]")
    no("AVOID_DUPLICATE_MEASURES", "[Total Amount]")
    yes("MEASURES_SHOULD_NOT_BE_DIRECT_REFERENCES_OF_OTHER_MEASURES", "[Alias Amount]")
    no("MEASURES_SHOULD_NOT_BE_DIRECT_REFERENCES_OF_OTHER_MEASURES", "[Plus Zero]")
    yes("FILTER_COLUMN_VALUES", "[Filtered Amount]")
    no("FILTER_COLUMN_VALUES", "[Filter All]")
    no("FILTER_COLUMN_VALUES", "[Measure Filter]")
    yes("FILTER_MEASURE_VALUES_BY_COLUMNS", "[Measure Filter]")
    no("FILTER_MEASURE_VALUES_BY_COLUMNS", "[Filtered Amount]")
    yes("AVOID_ADDING_0_TO_A_MEASURE", "[Plus Zero]")
    no("AVOID_ADDING_0_TO_A_MEASURE", "[Safe Amount]")
    yes("AVOID_USING_'1-(X/Y)'_SYNTAX", "[One Minus]")
    no("AVOID_USING_'1-(X/Y)'_SYNTAX", "[Amount Ratio]")
    yes("AVOID_AGGREGATING_TEXT_COLUMNS", "[Text Sum]")
    no("AVOID_AGGREGATING_TEXT_COLUMNS", "[Total Qty]")
    yes("LIMIT_ROW_LEVEL_SECURITY_(RLS)_LOGIC", "Readers on Customer")
    no("LIMIT_ROW_LEVEL_SECURITY_(RLS)_LOGIC", "Simple on Region")
    yes("AVOID_THE_USERELATIONSHIP_FUNCTION_AND_RLS_AGAINST_THE_SAME_TABLE", "[Alt Customer Amount]")
    no("AVOID_THE_USERELATIONSHIP_FUNCTION_AND_RLS_AGAINST_THE_SAME_TABLE", "[Rel Measure]")
    yes("ISAVAILABLEINMDX_FALSE_NONATTRIBUTE_COLUMNS", "Sales[Notes]")          # default isAvailableInMdx = true
    no("ISAVAILABLEINMDX_FALSE_NONATTRIBUTE_COLUMNS", "Sales[SaleID]")          # already false
    no("ISAVAILABLEINMDX_FALSE_NONATTRIBUTE_COLUMNS", "Date[Month Number]")     # sort-by target
    no("ISAVAILABLEINMDX_FALSE_NONATTRIBUTE_COLUMNS", "Time Calc[Ordinal]")
    yes("MONTH_(AS_A_STRING)_MUST_BE_SORTED", "Date[Month Short]")
    no("MONTH_(AS_A_STRING)_MUST_BE_SORTED", "Date[Month Name]")
    no("MONTH_(AS_A_STRING)_MUST_BE_SORTED", "Date[Months Since]")
    yes("PROVIDE_FORMAT_STRING_FOR_MEASURES", "[No Format]")
    no("PROVIDE_FORMAT_STRING_FOR_MEASURES", "[Label Text]")                    # text measure
    no("PROVIDE_FORMAT_STRING_FOR_MEASURES", "[Dynamic Format]")
    no("PROVIDE_FORMAT_STRING_FOR_MEASURES", "[Total Amount]")
    yes("OBJECTS_SHOULD_NOT_START_OR_END_WITH_A_SPACE", "[Trailing ]")
    no("OBJECTS_SHOULD_NOT_START_OR_END_WITH_A_SPACE", "[Total Amount]")
    yes("EXPRESSIONS_WITH_UNRESOLVED_REFERENCES", "[Broken]")
    no("EXPRESSIONS_WITH_UNRESOLVED_REFERENCES", "[Total Amount]")
    yes("DATA_COLUMNS_MUST_HAVE_A_SOURCE_COLUMN", "Sales[Orphan]")
    no("DATA_COLUMNS_MUST_HAVE_A_SOURCE_COLUMN", "Sales[Amount]")
    no("DATA_COLUMNS_MUST_HAVE_A_SOURCE_COLUMN", "Sales[Customer City]")        # calculated
    no("CALCULATION_GROUPS_WITH_NO_CALCULATION_ITEMS", "Time Calc")
    no("PERSPECTIVES_WITH_NO_OBJECTS", "Sales View")
    yes("ADD_DATA_CATEGORY_FOR_COLUMNS", "Customer[City]")
    no("ADD_DATA_CATEGORY_FOR_COLUMNS", "Customer[Capacity Band]")              # word boundary: capa-city
    no("ADD_DATA_CATEGORY_FOR_COLUMNS", "Customer[Postal Code]")                # has a category
    for opinion_rule in ("MARK_PRIMARY_KEYS", "INTEGER_FORMATTING", "PERCENTAGE_FORMATTING", "DATECOLUMN_FORMATSTRING",
                         "FIRST_LETTER_OF_OBJECTS_MUST_BE_CAPITALIZED", "OBJECTS_WITH_NO_DESCRIPTION"):
        check(not hits(out, opinion_rule), f"opinion rule on by default: {opinion_rule}")
        check(opinion_rule in out["coverage"]["disabled"], f"opinion rule not listed as disabled: {opinion_rule}")
    for data_rule in ("SPLIT_DATE_AND_TIME", "LARGE_TABLES_SHOULD_BE_PARTITIONED", "FIX_REFERENTIAL_INTEGRITY_VIOLATIONS"):
        check(data_rule in out["coverage"]["skipped_data_rules"] and not hits(out, data_rule), f"data rule ran: {data_rule}")

    # --- report rules
    no("REDUCE_PAGES")
    no("REDUCE_BOOKMARKS")
    yes("REDUCE_VISUALS_ON_PAGE", id="p_busy")
    no("REDUCE_VISUALS_ON_PAGE", id="p_over")
    check(any(f.get("excess") == 2 for f in hits(out, "REDUCE_VISUALS_ON_PAGE")), "visuals per page excess")
    yes("REDUCE_OBJECTS_WITHIN_VISUALS", id="v_col")
    no("REDUCE_OBJECTS_WITHIN_VISUALS", id="v_table")                           # tables may be wide
    yes("REDUCE_TOPN_FILTERS", id="p_over")
    no("REDUCE_ADVANCED_FILTERS", id="p_over")                                  # exactly at the limit
    yes("REDUCE_MEASURE_FILTERS", id="p_over")
    yes("AVOID_SHOW_ITEMS_WITH_NO_DATA", id="v_showall")
    no("AVOID_SHOW_ITEMS_WITH_NO_DATA", id="v_col")
    yes("HIDE_TOOLTIP_DRILLTHROUGH_PAGES", id="p_tip")
    no("HIDE_TOOLTIP_DRILLTHROUGH_PAGES", id="p_drill")
    yes("AVOID_TALL_PAGES", id="p_page1")
    no("AVOID_TALL_PAGES", id="p_over")
    yes("REMOVE_UNUSED_CUSTOM_VISUALS", id="fakeVisual1234")
    no("REMOVE_UNUSED_CUSTOM_VISUALS", id="usedVisual5678")
    yes("AVOID_HARD_CODED_COLORS", id="v_red")
    no("AVOID_HARD_CODED_COLORS", id="v_theme")
    yes("AVOID_IMPLICIT_MEASURES", id="v_line")
    no("AVOID_IMPLICIT_MEASURES", id="v_card")
    check(any("Ghost Measure" in f["object"] for f in hits(out, "FIX_INVALID_REFERENCES")), "invalid reference not found")
    check(not any("Report Measure" in f["object"] for f in hits(out, "FIX_INVALID_REFERENCES")), "report measure flagged invalid")
    yes("AVOID_REPORT_LEVEL_MEASURES", id="Report Measure")
    yes("REMOVE_INVALID_BOOKMARKS", id="bm_dead")
    yes("REMOVE_INVALID_BOOKMARKS", id="bm_badvis")
    no("REMOVE_INVALID_BOOKMARKS", id="bm_show")
    yes("REMOVE_HIDDEN_VISUALS_NEVER_SHOWN", id="v_hidden1")
    no("REMOVE_HIDDEN_VISUALS_NEVER_SHOWN", id="v_hidden2")
    yes("REMOVE_UNREFERENCED_HIDDEN_PAGES", id="p_old")
    no("REMOVE_UNREFERENCED_HIDDEN_PAGES", id="p_linked")
    no("REMOVE_UNREFERENCED_HIDDEN_PAGES", id="p_drill")
    yes("AVOID_PIE_CHARTS", id="v_pie")
    no("AVOID_PIE_CHARTS", id="v_col")
    yes("GIVE_VISIBLE_PAGES_MEANINGFUL_NAMES", id="p_page1")
    no("GIVE_VISIBLE_PAGES_MEANINGFUL_NAMES", id="p_over")
    check(not hits(out, "ENSURE_ALTTEXT"), "alt text rule on by default")

    # --- second project: model-level positives, model-wide suppression, cardinality defaults
    lite = run(lite_p, scope="model")
    lyes = lambda rule, obj=None: check(has(lite, rule, obj), f"Lite: expected {rule} on {obj}")
    lno = lambda rule, obj=None: check(not has(lite, rule, obj), f"Lite: unexpected {rule} on {obj}")
    lyes("MODEL_SHOULD_HAVE_A_DATE_TABLE")
    lyes("AVOID_EXCESSIVE_BI-DIRECTIONAL_OR_MANY-TO-MANY_RELATIONSHIPS")
    lyes("MANY-TO-MANY_RELATIONSHIPS_SHOULD_BE_SINGLE-DIRECTION", "A[j] -> C[j]")
    lno("MANY-TO-MANY_RELATIONSHIPS_SHOULD_BE_SINGLE-DIRECTION", "A[k] -> B[k]")
    lno("HIDE_FOREIGN_KEYS", "D[id]")                                           # one-to-one: no many side
    lyes("HIDE_FOREIGN_KEYS", "A[k]")
    lyes("CALCULATION_GROUPS_WITH_NO_CALCULATION_ITEMS", "Empty Group")
    lyes("PERSPECTIVES_WITH_NO_OBJECTS", "Empty")
    lno("AVOID_FLOATING_POINT_DATA_TYPES", "A[v]")
    check(any(s["rule"] == "AVOID_FLOATING_POINT_DATA_TYPES" and s["by"] == "model annotation"
              for s in lite.get("suppressed", [])), "model annotation not honored")
    check("report" not in lite, "model scope printed a report score")

    # --- opinion flag and overlay
    op = run(main_p, opinion=True, scope="model")
    check(has(op, "MARK_PRIMARY_KEYS", "Customer[CustomerKey]"), "opinion: MARK_PRIMARY_KEYS")
    check(not has(op, "MARK_PRIMARY_KEYS", "Date[Date]"), "opinion: isKey honored")
    check(has(op, "INTEGER_FORMATTING", "Date[Year]") and not has(op, "INTEGER_FORMATTING", "[Total Amount]"), "opinion: integer format")
    check(has(op, "PERCENTAGE_FORMATTING", "[Amount Pct]"), "opinion: percentage format")
    check(has(op, "DATECOLUMN_FORMATSTRING", "Updates[Changed On]") and not has(op, "DATECOLUMN_FORMATSTRING", "Date[Date]"),
          "opinion: date format")
    check(has(op, "OBJECTS_WITH_NO_DESCRIPTION", "[Total Amount]"), "opinion: description missing")
    check(not has(op, "OBJECTS_WITH_NO_DESCRIPTION", "[Total Qty]"), "opinion: /// description honored")
    check(not has(op, "OBJECTS_WITH_NO_DESCRIPTION", "[Old Measure]"), "opinion: hidden measure needs no description")
    check(not has(op, "OBJECTS_WITH_NO_DESCRIPTION", "Sales"), "opinion: table description honored")
    check(not has(op, "FIRST_LETTER_OF_OBJECTS_MUST_BE_CAPITALIZED", "[Plus Zero]"), "opinion: capitalized name flagged")
    lop = run(lite_p, scope="model", opinion=True)
    check(has(lop, "FIRST_LETTER_OF_OBJECTS_MUST_BE_CAPITALIZED", "A[k]")
          and not has(lop, "FIRST_LETTER_OF_OBJECTS_MUST_BE_CAPITALIZED", "A"), "opinion: lowercase name")
    check(op["ruleset"] != run(main_p, scope="model")["ruleset"], "ruleset hash ignores the opinion switch")
    ov_path = os.path.join(tmp, "overlay.json")
    with open(ov_path, "w", encoding="utf-8") as f:
        json.dump({"disable": ["AVOID_PIE_CHARTS"], "enable": ["ENSURE_ALTTEXT"], "severity": {"HIDE_FOREIGN_KEYS": 3},
                   "thresholds": {"REDUCE_PAGES": 5}}, f)
    ov = run(main_p, rules_path=ov_path)
    check(not hits(ov, "AVOID_PIE_CHARTS") and "AVOID_PIE_CHARTS" in ov["coverage"]["disabled"], "overlay disable")
    check(has(ov, "ENSURE_ALTTEXT", id="v_pie") and not has(ov, "ENSURE_ALTTEXT", id="v_card")
          and not has(ov, "ENSURE_ALTTEXT", id="t_p1"), "overlay enable (alt text)")
    check(all(f["severity"] == 3 for f in hits(ov, "HIDE_FOREIGN_KEYS")), "overlay severity")
    check(any(f.get("excess") == 2 for f in hits(ov, "REDUCE_PAGES")), "overlay threshold (7 pages, limit 5)")
    check(ov["ruleset"] != out["ruleset"], "ruleset hash ignores the overlay")

    # --- scores: formula and cutoffs
    for o, label in ((out, "default"), (ov, "overlay")):
        n = o["model"]["objects"]
        model_raw = sum(f["severity"] for f in o["findings"] if f["scope"] == "model")
        check(abs(o["model"]["score"] - round(model_raw * math.log10(n), 1)) < 0.11, f"model score formula ({label})")
        report_raw = sum(f["severity"] * (1 + f.get("excess", 0)) for f in o["findings"] if f["scope"] == "report")
        check(abs(o["report"]["score"] - report_raw) < 0.11, f"report score formula ({label})")
        check(0 <= o["model"]["rate"] <= 100 and 0 <= o["report"]["rate"] <= 100, f"rate range ({label})")
        check(o["model"]["grade"] == bpa.grade(o["model"]["score"], "model"), f"model grade ({label})")
    hfk = len(hits(out, "HIDE_FOREIGN_KEYS"))
    check(abs(ov["model"]["score"] - out["model"]["score"] - hfk * math.log10(out["model"]["objects"])) < 0.25,
          "severity overlay does not move the score by severity x violations x log10(objects)")
    for score, scope, want in ((0, "model", "A"), (74.9, "model", "A"), (75, "model", "B"), (299.9, "model", "B"),
                               (300, "model", "C"), (600, "model", "D"), (900, "model", "E"), (1199.9, "model", "E"),
                               (1200, "model", "F"), (49.9, "report", "A"), (50, "report", "B"), (100, "report", "C"),
                               (150, "report", "D"), (250, "report", "E"), (300, "report", "F")):
        check(bpa.grade(score, scope) == want, f"grade({score}, {scope}) != {want}")
    check(bpa.grade(10, "model", "measure-killer") == "Power BI Pro"
          and bpa.grade(5000, "model", "measure-killer") == "Power BI Criminal", "measure-killer labels")
    mk = run(main_p, labels="measure-killer", scope="report")
    check(mk["report"]["grade"] in bpa.LABELS["measure-killer"] and "model" not in mk, "labels / scope output")

    # --- name matching, catalog shape, no values anywhere
    check(bpa.words("DimDate") == ["dim", "date"] and bpa.words("Updates") == ["updates"]
          and bpa.words("FYMonth") == ["fy", "month"] and "city" not in bpa.words("Capacity"), "word splitting")
    ids = [r["id"] for r in bpa.catalog()]
    check(len(ids) == len(set(ids)), "duplicate rule ids")
    for r in bpa.catalog():
        check(r["evidence"] == "data" or r["id"] in bpa.CHECKS, f"rule without a predicate: {r['id']}")
        check({"id", "category", "object", "severity", "evidence", "opinion", "message", "fix", "source"} <= set(r),
              f"catalog entry incomplete: {r['id']}")
        check(r["severity"] in (1, 2, 3), f"severity out of range: {r['id']}")
    blob = "\n".join(outputs)
    for s in SECRETS:
        check(s not in blob, f"secret in bpa output: {s!r}")

    shutil.rmtree(tmp, ignore_errors=True)
    for f in fails:
        print("FAIL", f)
    print("ok" if not fails else f"{len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
