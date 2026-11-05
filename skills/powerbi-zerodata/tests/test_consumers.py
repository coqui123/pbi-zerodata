"""External consumers on the synthetic parity project: every consumer kind keeps the objects it uses
(used_external, never removable), opaque or missing coverage blocks removal (unknown), and planted
canaries (cached pivot items, cell values, member keys, query literals, parameter defaults, scanner
users and connection secrets) never reach any output, the registry or the private cache.

Run: python tests/test_consumers.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import zipfile

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import common, consumers, guard, mirror, rename, unused, usage  # noqa: E402
from pbz.common import PbzError  # noqa: E402

CANARIES = ["Zorblax Industries", "ZorblaxRegion", "987654.321", "zara.secret@example.com", "QuietPassword9",
            "Glimmerfen", "Vantablack Ledger"]
Q = "EVAL" + "UATE"  # keep the query keyword out of this file's text
MAIN = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
X14 = 'xmlns:x14="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"'
REL = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'


def make_xlsx(path: str) -> None:
    ext = "Provider=MSOLAP.8;Data Source=powerbi://example/myorg/ws;Initial Catalog=Parity;Password=QuietPassword9"
    dax = f"{Q} ROW(&quot;x&quot;, CALCULATE(COUNTROWS('Param'), 'Param'[Param Fields] = &quot;Zorblax Industries&quot;))"
    files = {
        "[Content_Types].xml": "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'/>",
        "xl/workbook.xml": f"<workbook {MAIN}><definedNames><definedName name='S'>"
                           "CUBESET(\"Parity Model\",\"[Sales].[Region].&amp;[ZorblaxRegion]\")</definedName></definedNames></workbook>",
        "xl/connections.xml": f"<connections {MAIN}>"
                              f"<connection id='1' name='Parity Model' type='5'><dbPr connection='{ext}' command='Model' commandType='1'/>"
                              "<olapPr sendLocale='1'/></connection>"
                              "<connection id='2' name='ThisWorkbookDataModel' type='5'>"
                              "<dbPr connection='Data Source=$Embedded$;Provider=MSOLAP' command='Model' commandType='1'/></connection>"
                              f"<connection id='3' name='Query Table' type='5'><dbPr connection='{ext}' command=\"{dax}\" commandType='4'/></connection>"
                              "</connections>",
        "xl/pivotCache/pivotCacheDefinition1.xml": (
            f"<pivotCacheDefinition {MAIN}><cacheSource type='external' connectionId='1'/>"
            "<cacheFields count='2'>"
            "<cacheField name='[Customer].[Segment].[Segment]' hierarchy='1' level='1'><sharedItems>"
            "<s v='Zorblax Industries'/><s v='Glimmerfen'/></sharedItems></cacheField>"
            "<cacheField name='[Measures].[Top Unused]' hierarchy='3'/></cacheFields>"
            "<cacheHierarchies count='4'>"
            "<cacheHierarchy uniqueName='[Customer].[Unused Col]' caption='Unused Col' attribute='1' "
            "defaultMemberUniqueName='[Customer].[Unused Col].&amp;[Zorblax Industries]' count='0'/>"
            "<cacheHierarchy uniqueName='[Customer].[Segment]' caption='Segment' count='2'><fieldsUsage count='1'>"
            "<fieldUsage x='0'/></fieldsUsage></cacheHierarchy>"
            "<cacheHierarchy uniqueName='[Sales].[Ghost]' caption='Ghost' attribute='1' count='0'/>"
            "<cacheHierarchy uniqueName='[Measures].[Top Unused]' measure='1' count='0'><fieldsUsage count='1'>"
            "<fieldUsage x='1'/></fieldsUsage></cacheHierarchy></cacheHierarchies>"
            "<calculatedMembers><calculatedMember name='[Measures].[Odd]' mdx='[Measures].[Zorblax Industries] * 2'/>"
            "</calculatedMembers></pivotCacheDefinition>"),
        "xl/pivotCache/pivotCacheRecords1.xml": f"<pivotCacheRecords {MAIN}><r><s v='Zorblax Industries'/><n v='987654.321'/></r></pivotCacheRecords>",
        "xl/pivotCache/pivotCacheDefinition2.xml": (
            f"<pivotCacheDefinition {MAIN}><cacheSource type='external' connectionId='2'/><cacheFields count='1'>"
            "<cacheField name='[Measures].[Named Sales]'/></cacheFields><cacheHierarchies count='1'>"
            "<cacheHierarchy uniqueName='[Measures].[Named Sales]' measure='1'><fieldsUsage count='1'><fieldUsage x='0'/>"
            "</fieldsUsage></cacheHierarchy></cacheHierarchies></pivotCacheDefinition>"),
        "xl/pivotTables/pivotTable1.xml": f"<pivotTableDefinition {MAIN} name='P1' cacheId='1'><pageFields count='1'>"
                                          "<pageField fld='0' hier='2' name='[Sales].[Ghost].&amp;[Vantablack Ledger]'/></pageFields>"
                                          "</pivotTableDefinition>",
        "xl/pivotTables/_rels/pivotTable1.xml.rels": f"<Relationships {REL}><Relationship Id='rId1' "
                                                     "Type='x/pivotCacheDefinition' Target='../pivotCache/pivotCacheDefinition1.xml'/></Relationships>",
        "xl/slicerCaches/slicerCache1.xml": (
            f"<slicerCacheDefinition {X14} name='Slicer_Comment' sourceName='[Sales].[Comment]'><data><olap pivotCacheId='1'>"
            "<levels count='1'><level uniqueName='[Sales].[Comment].[Comment]' sourceCaption='Comment' count='0'/></levels>"
            "<selections count='1'><selection n='[Sales].[Comment].&amp;[Glimmerfen]'/></selections></olap></data>"
            "</slicerCacheDefinition>"),
        "xl/sharedStrings.xml": f"<sst {MAIN}><si><t>Zorblax Industries</t></si></sst>",
        "xl/worksheets/sheet1.xml": (
            f"<worksheet {MAIN}><sheetData><row r='1'>"
            "<c r='A1'><f>CUBEVALUE(\"Parity Model\",\"[Measures].[Sales Copy]\",\"[Sales].[Region].&amp;[ZorblaxRegion]\")</f>"
            "<v>987654.321</v></c>"
            "<c r='B1'><f>CUBEVALUE(\"ThisWorkbookDataModel\",\"[Measures].[Named Sales]\")</f><v>1</v></c>"
            "<c r='C1' t='s'><v>0</v></c></row></sheetData></worksheet>"),
        "xl/model/item.data": "Zorblax Industries 987654.321",
    }
    with zipfile.ZipFile(path, "w") as z:
        for n, t in files.items():
            z.writestr(n, t)


def make_pbix(path: str) -> None:
    config = {"name": "v1", "singleVisual": {"visualType": "tableEx", "prototypeQuery": {
        "Version": 2, "From": [{"Name": "c", "Entity": "Customer", "Type": 0}],
        "Select": [{"Column": {"Expression": {"SourceRef": {"Source": "c"}}, "Property": "Unused Col"},
                    "Name": "Customer.Unused Col"}]}}}
    filters = [{"name": "f1", "expression": {"Column": {"Expression": {"SourceRef": {"Entity": "Customer"}}, "Property": "Name"}},
                "filter": {"Version": 2, "From": [{"Name": "c", "Entity": "Customer", "Type": 0}],
                           "Where": [{"Condition": {"In": {"Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "c"}},
                                                                                       "Property": "Name"}}],
                                                           "Values": [[{"Literal": {"Value": "'Zorblax Industries'"}}]]}}}]}}]
    layout = {"id": 0, "sections": [{"name": "s1", "displayName": "Page 1",
                                     "visualContainers": [{"x": 0, "y": 0, "config": json.dumps(config),
                                                           "filters": json.dumps(filters)}]}]}
    conn = {"Version": 3, "Connections": [{"Name": "EntityDataSource", "ConnectionType": "pbiServiceLive",
                                           "ConnectionString": "Data Source=pbiazure://example;Initial Catalog=Parity;Password=QuietPassword9"}],
            "RemoteArtifacts": [{"DatasetId": "d1", "ReportId": "R-1"}]}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Report/Layout", json.dumps(layout).encode("utf-16-le"))
        z.writestr("Connections", json.dumps(conn))
        z.writestr("DataModel", b"Zorblax Industries 987654.321")


def make_thin_pbir(root: str) -> str:
    rdir = os.path.join(root, "Thin.Report")
    vdir = os.path.join(rdir, "definition", "pages", "p1", "visuals", "v1")
    os.makedirs(vdir)
    with open(os.path.join(rdir, "definition.pbir"), "w", encoding="utf-8") as f:
        json.dump({"version": "4.0", "datasetReference": {"byConnection": {
            "connectionString": "Data Source=powerbi://example/myorg/ws;Initial Catalog=Parity"}}}, f)
    visual = {"name": "v1", "position": {"x": 0, "y": 0, "width": 100, "height": 100}, "visual": {
        "visualType": "tableEx", "query": {"queryState": {"Values": {"projections": [
            {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "Orphan"}}, "Property": "Code"}},
             "queryRef": "Orphan.Code"}]}}},
        "objects": {"general": [{"properties": {"x": {"expr": {"Literal": {"Value": "'Glimmerfen'"}}}}}]}}}
    with open(os.path.join(vdir, "visual.json"), "w", encoding="utf-8") as f:
        json.dump(visual, f)
    return root


def make_composite(root: str) -> str:
    mdir = os.path.join(root, "Downstream.SemanticModel")
    tdir = os.path.join(mdir, "definition", "tables")
    os.makedirs(tdir)
    with open(os.path.join(mdir, "definition", "model.tmdl"), "w", encoding="utf-8") as f:
        f.write("model Model\n\tculture: en-US\n")
    with open(os.path.join(mdir, "definition", "expressions.tmdl"), "w", encoding="utf-8") as f:
        f.write("expression 'DirectQuery to AS - Parity' = AnalysisServices.Database(\"powerbi://example\", \"Parity\")\n"
                "\tlineageTag: 22222222-2222-2222-2222-222222222222\n")
    with open(os.path.join(tdir, "Sales.tmdl"), "w", encoding="utf-8") as f:
        f.write("table Sales\n\tlineageTag: 33333333-3333-3333-3333-333333333333\n\n"
                "\tcolumn DueDateKey\n\t\tdataType: int64\n\t\tsourceColumn: DueDateKey\n\n"
                "\tpartition Sales = entity\n\t\tmode: directQuery\n\t\tsource\n\t\t\tentityName: Sales\n"
                "\t\t\texpressionSource: 'DirectQuery to AS - Parity'\n")
    return mdir


def main() -> int:
    fails: list[str] = []
    outputs: list = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    def keep(res):
        outputs.append(res)
        return res

    tmp = tempfile.mkdtemp(prefix="pbzcons-")
    root = os.path.join(tmp, "ws")
    test_parity.build(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    mirror.pull(ws, project)
    files = os.path.join(tmp, "consumer-files")
    os.makedirs(files)

    def state(kind, table, name):
        g = usage.build(ws, project)
        return g.state.get(usage.key(kind, table, name))

    check(state("measure", "Sales", "Top Unused") == "unused", "baseline: Top Unused should start unused")
    none = keep(consumers.list_(ws, project))
    check(none["consumers"] == [] and "none registered" in none["note"], f"empty registry: {none}")
    rep = keep(unused.report(ws, project))
    check(rep["scope"]["external_coverage"].startswith("none registered"), f"coverage note: {rep['scope']}")

    # ---------------------------------------------------------------- Excel
    xl = os.path.join(files, "Finance pack.xlsx")
    make_xlsx(xl)
    r = keep(consumers.add(ws, project, xl))
    check(r["kind"] == "xlsx" and r["status"] == "ok" and r["binding"] == "model name", f"xlsx add: {r}")
    g = usage.build(ws, project)
    st = lambda k, t, n: g.state.get(usage.key(k, t, n))  # noqa: E731
    check(st("measure", "Sales", "Top Unused") == "used_external", "pivot measure not kept")
    check(st("measure", "Sales", "Base Unused") == "used_external", "dependency of a kept measure not kept")
    check(st("measure", "Sales", "Sales Copy") == "used_external", "CUBE formula measure not kept")
    check(st("column", "Sales", "Comment") == "used_external", "slicer column not kept")
    check(st("column", "Sales", "Ghost") == "used_external", "pivot page field (hierarchy usage) not kept")
    check(st("column", "Param", "Param Fields") == "used_external", "DAX query table column not kept")
    check(st("column", "Customer", "Unused Col") == "unused", "a cube hierarchy that no pivot uses counted as used")
    check(st("measure", "Sales", "Named Sales") == "unused", "the workbook's own data model counted as this model")
    rep = keep(unused.report(ws, project))
    top = next(o for o in rep["objects"] if o["name"] == "Top Unused")
    check(top["state"] == "used_external" and top.get("consumers") == ["c1"], f"report record: {top}")
    check(rep["scope"]["external_coverage"] == "registered consumers complete", f"scope: {rep['scope']}")
    plan = keep(unused.remove(ws, project, dry_run=True))
    check(not any("Top Unused" in x or "Sales Copy" in x for x in plan.get("removed", [])), f"removal plan: {plan}")
    lin = keep(unused.lineage(ws, project, "[Top Unused]"))
    check("used_externally" in json.dumps(lin), f"lineage shows no external use: {lin}")
    shown = keep(consumers.show(ws, project, "c1"))
    objs = {u["object"] for u in shown["uses"]}
    check({"[Top Unused]", "[Sales Copy]", "Sales[Comment]"} <= objs, f"show: {sorted(objs)}")

    # ---------------------------------------------------------------- thin reports, composite, queries, manual
    pbix = os.path.join(files, "Thin pbix.pbix")
    make_pbix(pbix)
    r = keep(consumers.add(ws, project, pbix))
    check(r["kind"] == "pbix" and r["status"] == "ok", f"pbix add: {r}")
    check(state("column", "Customer", "Unused Col") == "used_external", "legacy layout field (via alias) not kept")
    r = keep(consumers.add(ws, project, make_thin_pbir(os.path.join(files, "git-export"))))
    check(r["kind"] == "pbir" and r["status"] == "ok", f"pbir add: {r}")
    check(state("column", "Orphan", "Code") == "used_external", "thin PBIR field not kept")
    r = keep(consumers.add(ws, project, make_composite(files)))
    check(r["kind"] == "model" and r["binding"] == "composite", f"composite add: {r}")
    check(state("column", "Sales", "DueDateKey") == "used_external", "composite model column not kept")
    log = os.path.join(files, "queries.csv")
    with open(log, "w", encoding="utf-8", newline="") as f:
        f.write("ApplicationName,EventText\n")
        f.write(f"Microsoft Excel,\"{Q} SUMMARIZECOLUMNS('Date'[Year], \"\"v\"\", [Named Sales], "
                f"FILTER(ALL('Customer'[Name]), 'Customer'[Name] = \"\"Zorblax Industries\"\"))\"\n")
    r = keep(consumers.add(ws, project, log))
    check(r["kind"] == "queries" and r["coverage"] == "observed", f"queries add: {r}")
    check(state("measure", "Sales", "Named Sales") == "used_external", "query-log measure not kept")
    r = keep(consumers.add(ws, project, objects=["'Customer'[SortOrder]", "[No Such Measure]"], label="Board pack"))
    check(r["kind"] == "manual", f"manual add: {r}")
    rep = keep(unused.report(ws, project))
    left = [o for o in rep["objects"] if o["state"] == "unused" and o["kind"] in ("column", "measure", "table")]
    check(not left, f"objects still unused after every consumer: {left}")
    lst = keep(consumers.list_(ws, project))
    man = next(c for c in lst["consumers"] if c["kind"] == "manual")
    check(man.get("unresolved") == 1, f"manual unresolved count: {man}")
    try:
        consumers.add(ws, project, pbix)
        check(False, "duplicate consumer accepted")
    except PbzError as e:
        check(e.code == "NAME_TAKEN", f"duplicate: {e.code}")
    try:
        consumers.add(ws, project, os.path.join(ws.mirror, "Parity.Report"))
        check(False, "mirror registered as a consumer")
    except PbzError as e:
        check(e.code == "BAD_REQUEST", f"mirror consumer: {e.code}")

    # ---------------------------------------------------------------- missing coverage blocks removal
    rdl = os.path.join(files, "Paged.rdl")
    with open(rdl, "w", encoding="utf-8") as f:
        f.write("<Report xmlns='http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition'>"
                "<DataSources><DataSource Name='ds'><ConnectionProperties><DataProvider>PBIDATASET</DataProvider>"
                "<ConnectString>Data Source=powerbi://example;Initial Catalog=Parity;Password=QuietPassword9</ConnectString>"
                "</ConnectionProperties></DataSource></DataSources><DataSets>"
                f"<DataSet Name='a'><Query><DataSourceName>ds</DataSourceName><CommandText>{Q} VALUES('Sales'[Region])"
                "</CommandText><QueryParameters><QueryParameter Name='p'><Value>Zorblax Industries</Value></QueryParameter>"
                "</QueryParameters></Query><Fields><Field Name='Region'><DataField>Sales[Region]</DataField></Field></Fields></DataSet>"
                "<DataSet Name='b'><Query><DataSourceName>ds</DataSourceName><CommandText>=Code.BuildQuery()</CommandText></Query>"
                "</DataSet></DataSets><ReportParameters><ReportParameter Name='p'><DefaultValue><Values><Value>Glimmerfen</Value>"
                "</Values></DefaultValue></ReportParameter></ReportParameters></Report>")
    r = keep(consumers.add(ws, project, rdl))
    check(r["kind"] == "rdl" and r.get("gaps") == {"expression_query": 1}, f"rdl add: {r}")
    rep = keep(unused.report(ws, project))
    check(rep["analysis"] == "incomplete" and rep["scope"]["external_coverage"] == "incomplete", f"rdl gap: {rep['scope']}")
    try:
        unused.remove(ws, project, dry_run=True)
        check(False, "removal allowed with an opaque consumer")
    except PbzError as e:
        check(e.code == "INCOMPLETE", f"removal with gap: {e.code}")
    keep(consumers.remove(ws, r["id"]))
    scan = os.path.join(files, "scan.json")
    with open(scan, "w", encoding="utf-8") as f:
        json.dump({"workspaces": [{"id": "w1", "users": [{"emailAddress": "zara.secret@example.com"}],
                                   "datasets": [{"id": "D1", "name": "Parity", "expressions": [{"name": "p", "expression": "\"Zorblax Industries\""}]}],
                                   "reports": [{"id": "R-1", "datasetId": "D1", "name": "Thin pbix"},
                                               {"id": "R-2", "datasetId": "D1", "name": "Mystery"}]}]}, f)
    r = keep(consumers.add(ws, project, scan))
    check(r["kind"] == "scan", f"scan add: {r}")
    lst = keep(consumers.list_(ws, project))
    sc = next(c for c in lst["consumers"] if c["kind"] == "scan")
    check(sc.get("gaps") == {"service_report": 1} and lst["coverage"] == "incomplete",
          f"scanner gaps (R-1 is covered by the pbix): {sc}")
    keep(consumers.remove(ws, sc["id"]))
    rep = keep(unused.report(ws, project))
    check(rep["analysis"] == "complete", f"coverage back after removing the gaps: {rep['analysis']}")
    gone = os.path.join(files, "gone.xlsx")
    make_xlsx(gone)
    r = keep(consumers.add(ws, project, gone))
    os.remove(gone)
    rep = keep(unused.report(ws, project))
    check(rep["analysis"] == "incomplete", "a missing consumer file did not block the analysis")
    keep(consumers.remove(ws, r["id"]))
    bad = os.path.join(files, "broken.xlsx")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("Zorblax Industries is not a workbook")
    r = keep(consumers.add(ws, project, bad))
    check(r["status"] == "unreadable" and r.get("error") and "Zorblax" not in json.dumps(r), f"broken file: {r}")
    check(keep(unused.report(ws, project))["analysis"] == "incomplete", "an unreadable consumer did not block the analysis")
    keep(consumers.remove(ws, r["id"]))

    # ---------------------------------------------------------------- rename warning, guard
    rn = keep(rename.rename(ws, project, "measure", "Sales Copy", "Sales Copy 2"))
    check(any(w["code"] == "USED_EXTERNALLY" and "c1" in w["consumers"] for w in rn.get("warnings", [])),
          f"rename of an externally used measure did not warn: {rn}")
    gw = os.path.join(tmp, "realws")
    os.makedirs(os.path.join(gw, ".pbz"))
    open(os.path.join(gw, "Real.pbip"), "w").close()
    reg_path = guard.norm(os.path.join(gw, ".pbz", "consumers.json"))
    why = guard.check_write(reg_path, "Write", {"file_path": reg_path, "content": "{}"}, guard.norm(gw))
    check(why == "consumers", f"guard let an agent edit the consumer registry: {why}")

    # ---------------------------------------------------------------- canaries
    blobs = [json.dumps(outputs, ensure_ascii=False)]
    for base in (os.path.join(ws.private, consumers.CACHE), os.path.join(root, ".pbz"), ws.mirror):
        for cur, _d, fs in os.walk(base):
            for fn in fs:
                with open(os.path.join(cur, fn), encoding="utf-8", errors="replace") as h:
                    blobs.append(h.read())
    text = "\n".join(blobs)
    for c in CANARIES:
        check(c not in text, f"canary leaked: {c}")

    if fails:
        print("\n".join("FAIL " + f for f in fails))
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
