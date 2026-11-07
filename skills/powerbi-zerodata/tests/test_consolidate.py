"""Consolidation refactors on the synthetic parity project: merge a duplicate measure (every DAX and PBIR
reference retargeted, translations and perspectives pruned, hidden alias for a name an external consumer
binds, cross-table merge fixing PBIR entities), calculation-group families and calc_group (wrappers keep
identity and formats, verify spec in the `pbz check` format). Every result is pushed through the staged
validation with TOM (the engine's tmdl-validate). No Desktop.

Run: python tests/test_consolidate.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import (common, consolidate, consumers, filters, governance as gv, mirror, modeling as md, props,  # noqa: E402
                 scaffold, synthtest, tmdl, usage, validate)
from pbz.common import PbzError  # noqa: E402

SCHEMA = test_parity.SCHEMA
meas, col = test_parity.meas, test_parity.col


def rd(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def wr(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def wj(path: str, obj) -> None:
    wr(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def setup(prefix: str):
    tmp = tempfile.mkdtemp(prefix=prefix)
    root = os.path.join(tmp, "ws")
    test_parity.build(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    mirror.pull(ws, project)
    return tmp, ws, project


def main() -> int:
    fails: list[str] = []
    outputs: list = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    def refused(fn, *args, code, **kw):
        try:
            fn(*args, **kw)
            check(False, f"{fn.__name__}{args[2:]} should fail with {code}")
        except PbzError as e:
            check(e.code == code, f"{fn.__name__}{args[2:]}: {e.code} {e.detail} (want {code})")

    def pushed(ws, project, label):
        res = mirror.push(ws, project)
        outputs.append(res)
        errs = [f for f in res.get("findings", []) if f["severity"] == "error"]
        tom = [f["code"] for f in res.get("findings", []) if f["code"].startswith("TOM_") and f["code"] != "TOM_PASSED"]
        if "TOM_ENGINE_UNAVAILABLE" in tom:
            fails.append(f"push after {label}: ENGINE_UNAVAILABLE (the engine may be rebuilding): rerun the test")
        check(res["result"] == "PUSHED" and not errs and not tom, f"push after {label}: {res.get('result')} {errs} {tom}")

    def clean_mirror(ws, project, label):
        errs = [f for f in validate.run_mirror(ws, project) if f["severity"] == "error"]
        check(not errs, f"pbz validate after {label}: {errs}")

    validate.report_author = lambda *a, **k: []  # schema validator needs a Desktop-made report; TOM still runs

    # ================================================================ 0. families() units (in memory, CRLF text)
    yoy = ("\tmeasure '{m}' =\n\t\t\tVAR cur = [{b}]\n\t\t\t// prior year\n"
           "\t\t\tVAR py = CALCULATE([{b}], SAMEPERIODLASTYEAR('D'[Date]))\n\t\t\tRETURN cur - py\n\n")
    unit = ("table T\n\n\tmeasure A = SUM(T[X])\n\n\tmeasure B = SUM(T[Y])\n\n\tmeasure C = [A] + [B]\n\n"
            "\tmeasure 'A Alias' = CALCULATE([A])\n\n"
            + yoy.format(m="A YoY", b="A") + yoy.format(m="A YoY 2", b="A") + yoy.format(m="B YoY", b="B")
            + yoy.format(m="C YoY", b="C") + "\tcolumn X\n\t\tdataType: double\n\n\tcolumn Y\n\t\tdataType: double\n\n"
            "table D\n\n\tcolumn Date\n\t\tdataType: dateTime\n")
    um = tmdl.load_model("", texts={"tables/T.tmdl": unit.replace("\n", "\r\n")})
    uf = consolidate.families(um)
    check(len(uf) == 1 and uf[0]["bases"] == ["A", "B", "C"] and uf[0]["confidence"] == "medium"
          and [tp["item"] for tp in uf[0]["templates"]] == ["YoY"], f"unit family: {uf}")
    if uf:
        ex = uf[0]["templates"][0]["expression"]
        check(ex.startswith("VAR cur = SELECTEDMEASURE()\n// prior year\n") and "\r" not in ex and "\t" not in ex,
              f"template text: {ex!r}")
        notes = {n["code"]: n for n in uf[0].get("notes", [])}
        check(notes.get("NESTED_MEASURES", {}).get("measures") == ["[C]"]
              and notes.get("DUPLICATE_MEMBERS", {}).get("pairs") == [["[A YoY 2]", "[A YoY]"]], f"family notes: {notes}")
    check(consolidate.families(um, min_bases=4) == [], "min_bases 4")

    # ================================================================ 1. merge 'Sales Copy' into 'Total Sales'
    tmp1, ws, project = setup("pbzcons1-")
    mdef = os.path.join(project.mirror_model, "definition")
    rdef = os.path.join(project.mirror_report, "definition")
    model = lambda: tmdl.load_model(mdef)  # noqa: E731
    # references to the duplicate in every model area
    md.function_add(ws, project, "CopyPlus", "(x : NUMERIC)", "x + [Sales Copy]", upgrade=True)  # level 1702
    sp = os.path.join(mdef, "tables", "Sales.tmdl")
    wr(sp, rd(sp).rstrip("\n") + "\n\n"
       "\tmeasure 'Copy Twice' = [Sales Copy] * 2\n"
       "\t\tlineageTag: 11000000-0000-0000-0000-000000000001\n\n"
       "\t\tdetailRowsDefinition = SELECTCOLUMNS(Sales, \"Amount\", Sales[Amount], \"Copy\", [Sales Copy])\n\n"
       "\t\tformatStringDefinition = IF([Sales Copy] > 1000, \"#,0,K\", \"#,0\")\n\n"
       "\tmeasure 'Copy Qualified' = 'Sales'[Sales Copy] + Sales[Sales Copy]\n"
       "\t\tformatString: #,0\n"
       "\t\tlineageTag: 11000000-0000-0000-0000-000000000002\n\n"
       "\tcolumn 'Copy Col' = [Sales Copy]\n"
       "\t\tdataType: double\n"
       "\t\tlineageTag: 11000000-0000-0000-0000-000000000003\n"
       "\t\tsummarizeBy: none\n\n"
       "\t\tannotation SummarizationSetBy = Automatic\n")
    tp = os.path.join(mdef, "tables", "Time Calc.tmdl")
    wr(tp, rd(tp).replace("\t\tcalculationItem YTD = CALCULATE(SELECTEDMEASURE(), DATESYTD('Date'[Date]))\n",
                          "\t\tcalculationItem YTD = CALCULATE(SELECTEDMEASURE(), DATESYTD('Date'[Date]))\n\n"
                          "\t\tcalculationItem 'Copy Share' = DIVIDE(SELECTEDMEASURE(), [Sales Copy])\n"))
    pp = os.path.join(mdef, "tables", "Param.tmdl")
    wr(pp, rd(pp).replace("NAMEOF('Sales'[Amount]), 1)", "NAMEOF('Sales'[Amount]), 1),\n\t\t\t\t    (\"Copy\", NAMEOF('Sales'[Sales Copy]), 2)"))
    gv.role_add(ws, project, "Copy Reader")
    gv.role_filter(ws, project, "Copy Reader", "Sales", "[Sales Copy] >= 0")
    # ... and in every report area
    vdir = os.path.join(rdef, "pages", "p1", "visuals", "v_copy")
    wj(os.path.join(vdir, "visual.json"), {
        "$schema": f"{SCHEMA}/visualContainer/2.4.0/schema.json", "name": "v_copy",
        "position": {"x": 600, "y": 400, "z": 20, "height": 200, "width": 300},
        "visual": {"visualType": "tableEx",
                   "query": {"queryState": {"Values": {"projections": [
                       {"field": meas("Sales", "Sales Copy"), "queryRef": "Sales.Sales Copy", "nativeQueryRef": "Sales Copy"},
                       {"field": col("Sales", "Region"), "queryRef": "Sales.Region", "nativeQueryRef": "Region"}]}},
                             "sortDefinition": {"sort": [{"field": meas("Sales", "Sales Copy"), "direction": "Descending"}],
                                                "isDefaultSort": True}},
                   "objects": {"values": [{"properties": {"fontColor": {"solid": {"color": {"expr": meas("Sales", "Sales Copy")}}}},
                                           "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}]}}]}}})
    filters.add(ws, project, "[Sales Copy]", page="p1", visual="v_copy", where=["> 0"])
    bp = os.path.join(rdef, "bookmarks", "BmUsed.bookmark.json")
    bm = json.loads(rd(bp))
    bm["explorationState"]["sections"] = {"p1": {"visualContainers": {"v_copy": {"singleVisual": {
        "visualType": "tableEx", "orderBy": [{"Direction": 2, "Expression": meas("Sales", "Sales Copy")}],
        "activeProjections": {"Values": ["Sales.Sales Copy", "Sales.Region"]}}}}}}
    wj(bp, bm)
    xp = os.path.join(rdef, "reportExtensions.json")
    ext = json.loads(rd(xp))
    ext["entities"][0]["measures"].append({"name": "Copy RM", "dataType": "Double", "expression": "'Sales'[Sales Copy] * 0.5",
                                           "references": {"measures": [{"entity": "Sales", "name": "Sales Copy"}]}})
    wj(xp, ext)
    clean_mirror(ws, project, "setup")
    pushed(ws, project, "setup with references to [Sales Copy]")

    # refusals (nothing written)
    before = mirror.mirror_texts(ws, project)
    refused(consolidate.merge, ws, project, "[Sales Copy]", "[Total Sales]", code="FORMAT_DIFFERS")
    refused(consolidate.merge, ws, project, "[Named Sales]", "[Total Sales]", code="NOT_EQUIVALENT")
    refused(consolidate.merge, ws, project, "[Sales Copy]", "[Copy Twice]", force=True, code="CYCLE")
    refused(consolidate.merge, ws, project, "[Nope]", "[Total Sales]", code="NOT_FOUND")
    refused(consolidate.merge, ws, project, "[Total Sales]", "'Sales'[Total Sales]", code="BAD_REQUEST")
    check(mirror.mirror_texts(ws, project) == before, "a refused merge wrote files")
    forced = consolidate.merge(ws, project, "[Sales Copy]", "[Total Sales]", force=True, keep_alias=True, dry_run=True)
    check(forced["result"] == "DRY_RUN" and forced["alias_kept"] and forced["alias"]["reason"] == "forced",
          f"keep_alias=True: {forced}")
    # align the format, then a dry run and the merge
    props.run(ws, project, "[Sales Copy]", ["formatString=#,0"])
    pushed(ws, project, "format alignment")
    before = mirror.mirror_texts(ws, project)
    dry = consolidate.merge(ws, project, "[Sales Copy]", "[Total Sales]", dry_run=True)
    check(dry["result"] == "DRY_RUN" and mirror.mirror_texts(ws, project) == before, "merge dry run wrote files")
    r = consolidate.merge(ws, project, "[Sales Copy]", "[Total Sales]")
    outputs.append(r)
    check(r["result"] == "MERGED" and r["equivalence"] == "identical" and not r["alias_kept"], f"merge: {r}")
    check(dry["rewritten"] == r["rewritten"] and dry["files"] == r["files"], "dry run and merge plans differ")
    want = {"measures": 3, "calculated_columns": 1, "calculation_items": 1, "calculated_tables": 1, "format_strings": 1,
            "detail_rows": 1, "rls_filters": 1, "functions": 1, "visual_projections": 1, "filters": 2, "sorts": 1,
            "conditional_formatting": 1, "bookmarks": 1, "report_measures": 1, "perspectives": 1, "translations": 1}
    got = {k: r["rewritten"].get(k) for k in want}
    check(got == want, f"rewritten per area: {got}")
    check(r["references"] == sum(v for k, v in want.items() if k not in ("perspectives", "translations")),
          f"reference total: {r['references']}")
    mt = mirror.mirror_texts(ws, project)
    blob = "\n".join(mt.values())
    check("Sales Copy" not in blob and "10000000-0000-0000-0000-000000000003" not in blob,
          "the duplicate (or a reference to it) is still in the mirror")
    sales = mt["Parity.SemanticModel/definition/tables/Sales.tmdl"]
    for frag in ("measure 'Copy Twice' = [Total Sales] * 2", "\"Copy\", [Total Sales])", "IF([Total Sales] > 1000",
                 "measure 'Copy Qualified' = 'Sales'[Total Sales] + Sales[Total Sales]", "column 'Copy Col' = [Total Sales]"):
        check(frag in sales, f"Sales.tmdl lacks {frag}")
    check("DIVIDE(SELECTEDMEASURE(), [Total Sales])" in mt["Parity.SemanticModel/definition/tables/Time Calc.tmdl"], "calc item")
    check("NAMEOF('Sales'[Total Sales]), 2)" in mt["Parity.SemanticModel/definition/tables/Param.tmdl"], "calculated table")
    check("[Total Sales] >= 0" in mt["Parity.SemanticModel/definition/roles/Copy Reader.tmdl"], "RLS filter")
    check("x + [Total Sales]" in mt["Parity.SemanticModel/definition/functions.tmdl"], "function")
    persp = mt["Parity.SemanticModel/definition/perspectives/Sales View.tmdl"]
    check(persp.count("perspectiveMeasure 'Total Sales'") == 1, "perspective keeps one Total Sales entry")
    check("measure" not in mt["Parity.SemanticModel/definition/cultures/de-DE.tmdl"], "translation not dropped")
    v = json.loads(mt["Parity.Report/definition/pages/p1/visuals/v_copy/visual.json"])
    pr = v["visual"]["query"]["queryState"]["Values"]["projections"][0]
    check(pr["field"]["Measure"]["Property"] == "Total Sales" and pr["queryRef"] == "Sales.Total Sales"
          and pr["nativeQueryRef"] == "Total Sales", f"projection: {pr}")
    f0 = v["filterConfig"]["filters"][0]
    check(f0["field"]["Measure"]["Property"] == "Total Sales"
          and f0["filter"]["Where"][0]["Condition"]["Comparison"]["Left"]["Measure"]["Property"] == "Total Sales", "filter")
    b = json.loads(mt["Parity.Report/definition/bookmarks/BmUsed.bookmark.json"])
    sv = b["explorationState"]["sections"]["p1"]["visualContainers"]["v_copy"]["singleVisual"]
    check(sv["orderBy"][0]["Expression"]["Measure"]["Property"] == "Total Sales"
          and sv["activeProjections"]["Values"] == ["Sales.Total Sales", "Sales.Region"], f"bookmark: {sv}")
    rm = next(m for m in json.loads(mt["Parity.Report/definition/reportExtensions.json"])["entities"][0]["measures"]
              if m["name"] == "Copy RM")
    check(rm["expression"] == "'Sales'[Total Sales] * 0.5" and rm["references"]["measures"][0]["name"] == "Total Sales",
          f"report measure: {rm}")
    clean_mirror(ws, project, "merge")
    pushed(ws, project, "merge")

    # ================================================================ 2. alias for an external name, cross-table merge
    tmp2, ws, project = setup("pbzcons2-")
    mdef = os.path.join(project.mirror_model, "definition")
    rdef = os.path.join(project.mirror_report, "definition")
    consumers.add(ws, project, objects=["[Sales Copy]"], label="Board pack")
    check(usage.build(ws, project).state.get(usage.key("measure", "Sales", "Sales Copy")) == "used_external",
          "manual consumer did not mark [Sales Copy] used_external")
    no_alias = consolidate.merge(ws, project, "[Sales Copy]", "[Total Sales]", force=True, keep_alias=False, dry_run=True)
    check(not no_alias["alias_kept"] and any(w["code"] == "USED_EXTERNALLY" for w in no_alias.get("warnings", [])),
          f"keep_alias=False on an external name must warn: {no_alias}")
    r = consolidate.merge(ws, project, "[Sales Copy]", "[Total Sales]", force=True)
    outputs.append(r)
    check(r["alias_kept"] and r["equivalence"] == "identical" and "c1" in r["alias"]["reason"], f"alias merge: {r}")
    m = tmdl.load_model(mdef)
    al = m.measure("Sales Copy")
    check(al is not None and al.expression.strip() == "[Total Sales]" and al.node.flag("isHidden")
          and al.node.props.get("lineageTag", "").strip() == "10000000-0000-0000-0000-000000000003", "hidden alias shape")
    check("perspectiveMeasure 'Sales Copy'" in rd(os.path.join(mdef, "perspectives", "Sales View.tmdl"))
          and "Sales Copy" in rd(os.path.join(mdef, "cultures", "de-DE.tmdl")), "the alias keeps perspective and translation")
    clean_mirror(ws, project, "alias merge")
    pushed(ws, project, "alias merge")
    # cross-table: Total Sales (Sales) into a twin in a measure table; filters use From aliases
    scaffold.measure_table(ws, project, "_Measures")
    mp = os.path.join(mdef, "tables", "_Measures.tmdl")
    wr(mp, rd(mp).replace("\tcolumn Value", "\tmeasure 'Sales Total' = SUM(Sales[Amount])\n\t\tformatString: #,0\n"
                                            "\t\tlineageTag: 13000000-0000-0000-0000-000000000001\n\n\tcolumn Value", 1))
    filters.add(ws, project, "[Total Sales]", page="p1", visual="v_card", where=["> 0"])
    filters.add(ws, project, "'Customer'[Name]", page="p1", top=3, by="[Total Sales]")
    clean_mirror(ws, project, "cross-table setup")
    pushed(ws, project, "cross-table setup")
    r = consolidate.merge(ws, project, "'Sales'[Total Sales]", "'_Measures'[Sales Total]")
    outputs.append(r)
    check(r["result"] == "MERGED" and not r["alias_kept"] and r["tables"] == {"dup": "Sales", "into": "_Measures"},
          f"cross-table merge: {r}")
    check(r["rewritten"]["visual_projections"] == 3 and r["rewritten"]["filters"] == 3 and r["rewritten"]["report_measures"] == 1
          and r["rewritten"]["calculated_tables"] == 1 and r["rewritten"]["measures"] >= 4, f"cross-table counts: {r['rewritten']}")
    check(any(w["code"] == "NAME_IN_STRINGS" and w["areas"] == {"calculated_tables": 1} for w in r.get("warnings", [])),
          f"the field parameter label naming the duplicate is not reported: {r.get('warnings')}")
    mt = mirror.mirror_texts(ws, project)
    tm = "\n".join(t for rel, t in mt.items() if rel.endswith(".tmdl"))
    check("[Total Sales]" not in tm and "measure 'Total Sales'" not in tm, "Total Sales still in the model")
    check("NAMEOF('_Measures'[Sales Total])" in mt["Parity.SemanticModel/definition/tables/Param.tmdl"], "requalified NAMEOF")
    check("measure 'Sales Copy' = [Sales Total]" in mt["Parity.SemanticModel/definition/tables/Sales.tmdl"], "alias retargeted")
    persp = mt["Parity.SemanticModel/definition/perspectives/Sales View.tmdl"]
    check("perspectiveTable _Measures" in persp and "perspectiveMeasure 'Sales Total'" in persp
          and "'Total Sales'" not in persp, f"perspective moved to the target table: {persp}")
    card = json.loads(mt["Parity.Report/definition/pages/p1/visuals/v_card/visual.json"])
    fld = card["visual"]["query"]["queryState"]["Values"]["projections"][0]["field"]["Measure"]
    check(fld["Expression"]["SourceRef"]["Entity"] == "_Measures" and fld["Property"] == "Sales Total", f"entity: {fld}")
    vf = card["filterConfig"]["filters"][0]
    check(vf["field"]["Measure"]["Expression"]["SourceRef"]["Entity"] == "_Measures"
          and vf["filter"]["From"][0]["Entity"] == "_Measures", f"filter alias entity: {vf}")
    page = json.loads(mt["Parity.Report/definition/pages/p1/page.json"])
    topn = next(f for f in page["filterConfig"]["filters"] if f.get("type") == "TopN")
    sub = topn["filter"]["From"][0]["Expression"]["Subquery"]["Query"]
    check({e["Entity"] for e in sub["From"]} == {"Customer", "_Measures"}
          and sub["OrderBy"][0]["Expression"]["Measure"]["Property"] == "Sales Total", f"TopN subquery: {sub}")
    rms = json.loads(mt["Parity.Report/definition/reportExtensions.json"])["entities"][0]["measures"]
    check(next(x for x in rms if x["name"] == "Report Margin")["expression"] == "[Sales Total] * 0.3", "report measure")
    check(usage.build(ws, project).state.get(usage.key("measure", "_Measures", "Sales Total")) == "used", "usage after merge")
    clean_mirror(ws, project, "cross-table merge")
    pushed(ws, project, "cross-table merge")

    # ================================================================ 3. families and calc_group
    tmp3, ws, project = setup("pbzcons3-")
    mdef = os.path.join(project.mirror_model, "definition")
    rdef = os.path.join(project.mirror_report, "definition")
    sp = os.path.join(mdef, "tables", "Sales.tmdl")
    tag = "12000000-0000-0000-0000-0000000000"
    wr(sp, rd(sp).rstrip("\n") + "\n\n"
       f"\tmeasure 'Customer Count' = DISTINCTCOUNT(Sales[CustomerKey])\n\t\tformatString: 0\n\t\tlineageTag: {tag}01\n\n"
       f"\tmeasure 'Sales Count' = COUNTROWS(Sales)\n\t\tformatString: 0\n\t\tlineageTag: {tag}02\n\n"
       "\t/// Year to date of Total Sales.\n"
       "\tmeasure 'Total Sales YTD' = CALCULATE([Total Sales], DATESYTD('Date'[Date]))\n"
       f"\t\tformatString: #,0\n\t\tdisplayFolder: Time\n\t\tlineageTag: {tag}03\n\n"
       "\tmeasure 'Customer Count YTD' =\n\t\t\tCALCULATE(\n\t\t\t    [Customer Count],\n\t\t\t    DATESYTD('Date'[Date])\n\t\t\t)\n"
       f"\t\tformatString: 0\n\t\tlineageTag: {tag}04\n\n"
       "\tmeasure 'Sales Count YTD' = calculate('Sales'[Sales Count], datesytd('Date'[Date]))\n"
       f"\t\tisHidden\n\t\tlineageTag: {tag}05\n\n"
       "\tmeasure 'Total Sales PY' = CALCULATE([Total Sales], SAMEPERIODLASTYEAR('Date'[Date]))\n"
       f"\t\tformatString: #,0\n\t\tlineageTag: {tag}06\n\n"
       "\tmeasure 'Customer Count PY' = CALCULATE([Customer Count], SAMEPERIODLASTYEAR('Date'[Date]))\n"
       f"\t\tlineageTag: {tag}07\n\n"
       "\tmeasure 'Sales Count PY' = CALCULATE([Sales Count], SAMEPERIODLASTYEAR('Date'[Date]))\n"
       f"\t\tlineageTag: {tag}08\n")
    wj(os.path.join(rdef, "pages", "p1", "visuals", "v_impl", "visual.json"), {
        "$schema": f"{SCHEMA}/visualContainer/2.4.0/schema.json", "name": "v_impl",
        "position": {"x": 900, "y": 500, "z": 30, "height": 100, "width": 100},
        "visual": {"visualType": "card", "query": {"queryState": {"Values": {"projections": [
            {"field": {"Aggregation": {"Expression": col("Sales", "Amount"), "Function": 0}},
             "queryRef": "Sum(Sales.Amount)", "nativeQueryRef": "Sum of Amount"}]}}}}})
    pushed(ws, project, "family setup")
    model = tmdl.load_model(mdef)
    fams = consolidate.families(model)
    outputs.append(fams)
    check(len(fams) == 1, f"one family expected: {fams}")
    fam = fams[0] if fams else {"id": "", "templates": [], "bases": [], "members": [], "confidence": ""}
    check(fam["bases"] == ["Customer Count", "Sales Count", "Total Sales"] and len(fam["templates"]) == 2
          and fam["confidence"] == "high" and len(fam["members"]) == 6, f"family shape: {fam}")
    items = {tp["item"]: tp for tp in fam["templates"]}
    check(set(items) == {"YTD", "PY"}, f"item names: {sorted(items)}")
    if "YTD" in items:
        check(items["YTD"]["members"] == {"Customer Count": "Customer Count YTD", "Sales Count": "Sales Count YTD",
                                          "Total Sales": "Total Sales YTD"}, f"YTD members: {items['YTD']['members']}")
        check("SELECTEDMEASURE()" in items["YTD"]["expression"] and "DATESYTD('Date'[Date])" in items["YTD"]["expression"]
              and "[" not in items["YTD"]["expression"].replace("'Date'[Date]", ""), f"YTD template: {items['YTD']['expression']}")
    check(re.fullmatch(r"[0-9a-f]{10}", fam["id"] or "") and consolidate.families(tmdl.load_model(mdef))[0]["id"] == fam["id"],
          "family id not stable")
    check(consolidate.families(model, min_bases=4) == [], "min_bases not honoured")
    refused(consolidate.calc_group, ws, project, family=fam["id"], name="Time Calc", code="NAME_TAKEN")
    refused(consolidate.calc_group, ws, project, family="0000000000", code="NOT_FOUND")
    refused(consolidate.calc_group, ws, project, code="BAD_REQUEST")
    before = mirror.mirror_texts(ws, project)
    dry = consolidate.calc_group(ws, project, measures=["[Total Sales YTD]", "Sales Count PY"], name="Time Variants", dry_run=True)
    check(dry["result"] == "DRY_RUN" and dry["family"] == fam["id"] and mirror.mirror_texts(ws, project) == before
          and not os.path.exists(dry["verify"]["spec"]), f"calc-group dry run: {dry}")
    r = consolidate.calc_group(ws, project, family=fam["id"], name="Time Variants")
    outputs.append(r)
    check(r["result"] == "CREATED" and r["precedence"] == 1 and len(r["members"]) == 6, f"calc group: {r}")
    codes = {w["code"] for w in r.get("warnings", [])}
    check({"IMPLICIT_MEASURES", "OTHER_CALC_GROUPS"} <= codes, f"warnings: {codes}")
    m = tmdl.load_model(mdef)
    cg = m.table("Time Variants")
    check(cg is not None and set(cg.calc_items) == {"YTD", "PY"} and "Name" in cg.columns and "Ordinal" in cg.columns,
          "calculation group table")
    ytd = m.measure("Total Sales YTD")
    check(ytd.expression.strip() == "CALCULATE([Total Sales], 'Time Variants'[Name] = \"YTD\")", f"wrapper: {ytd.expression}")
    check(ytd.node.props.get("lineageTag", "").strip() == f"{tag}03" and ytd.format_string == "#,0" and ytd.folder == "Time",
          "wrapper lost its lineageTag, format or folder")
    st = rd(sp)
    check("\t/// Year to date of Total Sales.\n\tmeasure 'Total Sales YTD' = CALCULATE([Total Sales]" in st, "description lost")
    check(m.measure("Sales Count YTD").node.flag("isHidden")
          and m.measure("Sales Count YTD").expression.strip() == "CALCULATE([Sales Count], 'Time Variants'[Name] = \"YTD\")",
          "hidden member")
    check(m.measure("Customer Count YTD").expression.strip().startswith("CALCULATE([Customer Count], 'Time Variants'"),
          "multi-line member not rewritten")
    check(m.measure("Customer Count PY").node.props.get("lineageTag", "").strip() == f"{tag}07", "member lineageTag")
    check(consolidate.families(m) == [], "wrappers over a calculation group must not form a new family")
    spec_path = r["verify"]["spec"]
    check(os.path.dirname(spec_path) == ws.mirror and os.path.basename(spec_path) == f"consolidate-{fam['id']}.check.json",
          f"spec path: {spec_path}")
    try:
        spec = json.loads(rd(spec_path))
    except (OSError, ValueError) as e:
        spec = {}
        check(False, f"verify spec is not JSON: {e}")
    checks = spec.get("checks", [])
    check(len(checks) == 6 and all(c["kind"] == "equal" and c["right"].startswith("[") and c["by"] == ["'Date'[Year]", "'Date'[Month]"]
                                   for c in checks), f"verify checks: {checks}")
    left = {c["right"]: c["left"] for c in checks}
    check(left.get("[Total Sales YTD]") == "CALCULATE([Total Sales], DATESYTD('Date'[Date]))"
          and "SAMEPERIODLASTYEAR" in left.get("[Sales Count PY]", ""), f"old expressions in the spec: {left}")
    check(len(synthtest._spec_checks(spec_path)) == 6, "synth check refuses the spec")
    check(any("synth check --spec" in c for c in r["verify"]["commands"]) and
          any(c.endswith("pbz check pbz-workspace/" + os.path.basename(spec_path)) for c in r["verify"]["commands"]),
          f"verify commands: {r['verify']['commands']}")
    clean_mirror(ws, project, "calc group")
    pushed(ws, project, "calc group")

    blob = json.dumps(outputs, ensure_ascii=False)
    for s in test_parity.SECRETS:
        check(s not in blob, f"secret in verb output: {s!r}")
    for t in (tmp1, tmp2, tmp3):
        shutil.rmtree(t, ignore_errors=True)
    for f in fails:
        print("FAIL", f)
    print("ok" if not fails else f"{len(fails)} failure(s)")
    return 1 if fails else 0


def live() -> int:
    """Desktop: calculation-group wrappers return what the old measures returned on fake data, including PY over a
    base measure that calls another measure (a calculation item must not apply twice). Run with --live."""
    import subprocess
    from pbz import desktop, synth, synthtest, tmdl
    from test_synth import _kill
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq PBIDesktop.exe", "/NH"], capture_output=True, text=True).stdout
    if "PBIDesktop.exe" in out:
        print("SKIP live: another Power BI Desktop is running")
        return 0
    fails: list[str] = []
    tmp = tempfile.mkdtemp(prefix="pbzcglive-")
    os.environ["PBZ_SYNTH_ROOT"] = os.path.join(tmp, "synth")
    root = os.path.join(tmp, "ws")
    test_parity.build(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    mirror.pull(ws, project)
    mdef = os.path.join(project.mirror_model, "definition")
    path = os.path.join(mdef, "tables", "Sales.tmdl")
    text, bom = common.read_text(path)
    extra = ["\n\tmeasure 'Sales Count' = COUNTROWS(Sales)\n\t\tlineageTag: 40000000-0000-0000-0000-000000000001\n"]
    n = 2
    for base in ("Total Sales", "Base Unused", "Sales Count"):    # [Base Unused] = [Total Sales] * 2 (nested)
        for item, fn in (("YTD", "DATESYTD('Date'[Date])"), ("PY", "SAMEPERIODLASTYEAR('Date'[Date])")):
            extra.append(f"\n\tmeasure '{base} {item}' = CALCULATE([{base}], {fn})\n"
                         f"\t\tlineageTag: 40000000-0000-0000-0000-0000000000{n:02d}\n")
            n += 1
    common.write_text(path, text.rstrip("\n") + "\n" + "".join(extra), bom)
    fam = next(f for f in consolidate.families(tmdl.load_model(mdef)) if "Base Unused" in f["bases"])
    r = consolidate.calc_group(ws, project, family=fam["id"], name="Time Intel")
    spec_path = os.path.join(ws.mirror, os.path.basename(r["verify"]["spec"]))
    with open(spec_path, encoding="utf-8") as f:
        spec = json.load(f)
    spec["checks"].append({"id": "nested:base_unused_py", "kind": "equal",
                           "left": "CALCULATE([Base Unused], SAMEPERIODLASTYEAR('Date'[Date]))",
                           "right": "CALCULATE([Base Unused], 'Time Intel'[Name] = \"PY\")",
                           "by": ["'Date'[Year]", "'Date'[Month]"], "tol": 1e-9})
    sp = synth.synth_project(ws, project)
    try:
        b = synth.build(ws, project, rows=300, seed=11)
        if not b.get("valid"):
            fails.append(f"synth build: {b}")
        op = synth.open_(ws, project, wait=480)
        if not (op.get("open") and op.get("engine") == "READY"):
            fails.append(f"synth open: {op}")
        else:
            res = synthtest.check(ws, project, spec)
            bad = [(x["id"], x["status"]) for x in res["results"] if x["status"] != "PASS"]
            if bad or len(res["results"]) != 7:
                fails.append(f"wrappers differ from the old measures on fake data: {bad}")
    finally:
        if desktop.find_instance(sp):
            desktop.close(sp, wait=30)
        if desktop.find_instance(sp):
            _kill(tmp)
    shutil.rmtree(tmp, ignore_errors=True)
    for f in fails:
        print("FAIL live", f)
    print("live ok" if not fails else f"{len(fails)} live failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    rc = main()
    if "--live" in sys.argv:
        rc = live() or rc
    sys.exit(rc)
