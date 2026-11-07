"""Authoring verbs on the synthetic parity project: `new` scaffolds, `relationship`, `add page|visual`
and `repair`. Every result is pushed through the full staged validation (TOM, PBIR integrity, fields).

Run: python tests/test_authoring.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import author, common, layout, mirror, pbircheck, relate, repair, scaffold, tmdl, usage, validate  # noqa: E402
from pbz.common import PbzError  # noqa: E402


def main() -> int:
    fails: list[str] = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    def refused(fn, *args, code, **kw):
        try:
            fn(*args, **kw)
            check(False, f"{fn.__name__}{args[2:]} should fail with {code}")
        except PbzError as e:
            check(e.code == code, f"{fn.__name__}{args[2:]}: {e.code} (want {code})")

    def pushed(label):
        res = mirror.push(ws, project)
        errs = [f for f in res.get("findings", []) if f["severity"] == "error"]
        check(res["result"] == "PUSHED" and not errs, f"push after {label}: {res.get('result')} {errs}")
        check(not any(f["code"].startswith("TOM_") and f["code"] != "TOM_PASSED" for f in res.get("findings", [])), f"TOM did not run after {label}")

    tmp = tempfile.mkdtemp(prefix="pbzauthor-")
    root = os.path.join(tmp, "ws")
    test_parity.build(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    mirror.pull(ws, project)
    validate.report_author = lambda *a, **k: []  # schema validator needs a Desktop-made report; TOM still runs
    mdef = os.path.join(ws.mirror, "Parity.SemanticModel", "definition")

    # ---------------------------------------------------------------- scaffolds
    scaffold.measure_table(ws, project, "_Measures")
    mp = os.path.join(mdef, "model.tmdl")
    mtext = open(mp, encoding="utf-8", newline="").read()
    open(mp, "w", encoding="utf-8", newline="").write(mtext.replace("\tdiscourageImplicitMeasures\n", ""))
    cg = scaffold.calc_group(ws, project, "Time Intel", "'Date'[Date]", ["Current", "YTD", "PY", "YoY %"])
    check(cg["precedence"] == 1 and "model_setting" in cg, f"calc group: {cg}")
    fp = scaffold.field_parameter(ws, project, "Metric Picker", ["[Total Sales]", "'Sales'[Amount]"], ["Sales", "Amount"])
    check(fp["fields"] == 2, f"field parameter: {fp}")
    model = tmdl.load_model(mdef)
    check(model.model_node.flag("discourageImplicitMeasures"), "discourageImplicitMeasures not set")
    for t in ("_Measures", "Time Intel", "Metric Picker"):
        check(model.table(t) is not None, f"scaffold table {t} missing")
    check(set(model.table("Time Intel").calc_items) == {"Current", "YTD", "PY", "YoY %"}, "calc items")
    fpt = open(os.path.join(mdef, "tables", "Metric Picker.tmdl"), encoding="utf-8").read()
    check("NAMEOF('Sales'[Total Sales])" in fpt and "ParameterMetadata" in fpt and "groupByColumn: 'Metric Picker Fields'" in fpt,
          "field parameter shape")
    g = usage.build(ws, project)
    check(g.state[usage.key("calc_item", "Time Intel", "YTD")] in ("unused", "used_by_unused"), "new calc group state")
    refused(scaffold.date_table, ws, project, "Calendar", 2024, 2026, code="AMBIGUOUS")  # 'Date' is marked
    refused(scaffold.calc_group, ws, project, "TI2", "'Date'[Date]", ["Nope"], code="BAD_REQUEST")
    refused(scaffold.calc_group, ws, project, "TI3", "'Date'[Date]", ["YTD"], precedence=1, code="NAME_TAKEN")
    refused(scaffold.field_parameter, ws, project, "FP2", ["[Missing]"], code="NOT_FOUND")
    refused(scaffold.field_parameter, ws, project, "FP3", ["[Total Sales]"], ["«lit#1»"], code="BAD_REQUEST")
    refused(scaffold.measure_table, ws, project, "Sales", code="NAME_TAKEN")
    pushed("scaffolds")
    # unmark the existing date table, then scaffold a new one related to a fact column
    dp = os.path.join(mdef, "tables", "Date.tmdl")
    text = open(dp, encoding="utf-8", newline="").read()
    open(dp, "w", encoding="utf-8", newline="").write(text.replace("\tdataCategory: Time\n", ""))
    dt = scaffold.date_table(ws, project, "Calendar", 2024, 2026, ["'Sales'[ShipDateKey]"])
    check(dt.get("relationships") == ["Sales[ShipDateKey] -> Calendar[Date]"], f"date table relate: {dt}")
    cal = tmdl.load_model(mdef).table("Calendar")
    check(cal is not None and cal.node.props.get("dataCategory", "").strip() == "Time" and cal.columns["Date"].node.flag("isKey"),
          "date table not marked")
    check(cal.columns["Month"].sort_by == "Month Number" and "Calendar" in cal.hierarchies, "date table sort / hierarchy")
    pushed("date table")

    # ---------------------------------------------------------------- relationships
    refused(relate.add, ws, project, "'Sales'[CustomerKey]", "'Customer'[CustomerKey]", code="DUPLICATE")
    refused(relate.add, ws, project, "'Sales'[Region]", "'Date'[Date]", code="TYPE_MISMATCH")
    refused(relate.add, ws, project, "'Sales'[Amount]", "'Sales'[Amount]", code="BAD_REQUEST")
    refused(relate.set_, ws, project, "'Sales'[DueDateKey]", "'Date'[Date]", active=True, code="AMBIGUOUS_PATH")
    check(relate.remove(ws, project, "'Sales'[DueDateKey]", "'Date'[Date]")["action"] == "removed", "remove")
    refused(relate.add, ws, project, "'Sales'[DueDateKey]", "'Date'[Date]", code="AMBIGUOUS_PATH")
    r = relate.add(ws, project, "'Sales'[DueDateKey]", "'Date'[Date]", inactive=True)
    check(r["action"] == "added" and r["checks"]["active"] is False, f"add inactive: {r}")
    check(relate.set_(ws, project, "'Sales'[DueDateKey]", "'Date'[Date]", both=True)["action"] == "updated", "set both")
    rel = [x for x in tmdl.load_model(mdef).relationships if x.from_column == "DueDateKey"]
    check(len(rel) == 1 and not rel[0].active and rel[0].both, "relationship properties")
    check(relate.set_(ws, project, "'Sales'[DueDateKey]", "'Date'[Date]", both=False)["action"] == "updated", "set single")
    pushed("relationships")

    # ---------------------------------------------------------------- pages and visuals
    pg = author.add_page(ws, project, "Sales Review", after="Overview")
    check(pg["position"] == 1, f"page position: {pg}")
    made = {}
    for vt, fields in (("card", ["[Total Sales]"]), ("clusteredColumnChart", ["'Customer'[Segment]", "'Sales'[Amount]"]),
                       ("pivotTable", ["Rows='Sales'[Region]", "Columns='Calendar'[Year]", "[Total Sales]"]),
                       ("slicer", ["'Metric Picker'[Metric Picker]"]), ("textbox", [])):
        v = author.add_visual(ws, project, vt, "Sales Review", fields, title=None if vt == "textbox" else "T",
                              text="Notes" if vt == "textbox" else None)
        made[vt] = v
    card = json.load(open(os.path.join(ws.mirror, made["card"]["files"][0]), encoding="utf-8"))
    check("visualContainerObjects" in card["visual"] and "visualContainerObjects" not in card,
          "title must sit in visual.visualContainerObjects (Microsoft's schema rejects it at the root)")
    card_path = os.path.join(ws.mirror, made["card"]["files"][0])
    bad = dict(card, visualContainerObjects=card["visual"]["visualContainerObjects"])
    json.dump(bad, open(card_path, "w", encoding="utf-8"), indent=2)
    check(any(f["code"] == "PBIR_MISPLACED_PROPERTY" for f in pbircheck.run(project.mirror_report)), "misplaced root key not flagged")
    json.dump(card, open(card_path, "w", encoding="utf-8"), indent=2)
    check(made["clusteredColumnChart"]["roles"] == {"Category": 1, "Y": 1}, f"chart roles: {made['clusteredColumnChart']}")
    check(made["pivotTable"]["roles"] == {"Rows": 1, "Columns": 1, "Values": 1}, "matrix roles")
    check(all(v["placement"] == "free_spot" for v in made.values()), "placement")
    refused(author.add_visual, ws, project, "slicer", "Sales Review", ["[Total Sales]"], code="BAD_REQUEST")
    refused(author.add_visual, ws, project, "card", "Nope", ["[Total Sales]"], code="NOT_FOUND")
    refused(author.add_visual, ws, project, "card", "Sales Review", ["[Missing]"], code="NOT_FOUND")
    refused(author.add_page, ws, project, "Overview", code="NAME_TAKEN")
    check(not layout.run(ws, project, "Sales Review", show_map=False)["findings"], "new visuals overlap")
    check(not pbircheck.run(project.mirror_report), "new page fails PBIR checks")
    check(not [f for f in validate.run_mirror(ws, project) if f["severity"] == "error"], "new visuals fail field refs")
    g = usage.build(ws, project)
    check(g.state[usage.key("table", "", "Metric Picker")] == "used" and
          g.state[usage.key("measure", "Sales", "Total Sales")] == "used", "new bindings not seen by the usage graph")
    pushed("pages and visuals")

    # ---------------------------------------------------------------- repair
    d = os.path.join(project.mirror_report, "definition")
    pj = os.path.join(d, "pages", "pages.json")
    meta = json.load(open(pj, encoding="utf-8"))
    meta["pageOrder"].append("ghost")
    meta["activePageName"] = "ghost"
    json.dump(meta, open(pj, "w", encoding="utf-8"), indent=2)
    os.remove(os.path.join(d, "bookmarks", "BmOrphan.bookmark.json"))
    check(any(f["severity"] == "error" for f in pbircheck.run(project.mirror_report)), "breakage not detected")
    r = repair.run(ws, project)
    check(r["result"] == "REPAIRED" and {"PAGE_ORDER", "ACTIVE_PAGE_MISSING", "BOOKMARK_LIST"} <= {f["code"] for f in r["fixes"]},
          f"repair: {r}")
    check(not pbircheck.run(project.mirror_report) and repair.run(ws, project)["result"] == "NOTHING_TO_REPAIR", "repair not clean")
    sp = os.path.join(mdef, "tables", "Sales.tmdl")
    text = open(sp, encoding="utf-8", newline="").read()
    open(sp, "w", encoding="utf-8", newline="").write(
        text.replace("10000000-0000-0000-0000-000000000003", "10000000-0000-0000-0000-000000000002"))  # copied tag
    dup = [f for f in validate.run_mirror(ws, project) if f["code"] == "DUPLICATE_LINEAGE_TAG"]
    check(dup and dup[0]["severity"] == "warning", f"duplicate lineageTag not reported as a warning: {dup}")
    r = repair.run(ws, project, lineage=True)
    check([f["code"] for f in r["fixes"]] == ["DUPLICATE_LINEAGE_TAG"], f"lineage repair: {r['fixes']}")
    check(not [f for f in validate.run_mirror(ws, project) if f["code"] == "DUPLICATE_LINEAGE_TAG"], "lineage still duplicated")
    check("10000000-0000-0000-0000-000000000002" in open(sp, encoding="utf-8").read(), "the first object lost its tag")
    pushed("repair")

    shutil.rmtree(tmp, ignore_errors=True)
    for f in fails:
        print("FAIL", f)
    print("ok" if not fails else f"{len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
