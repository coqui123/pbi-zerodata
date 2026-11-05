"""Report authoring beyond visuals on the synthetic parity project: filters (basic, not-in, advanced
with AND, Top N, measure filters, empty), interactions, tooltip and drillthrough pages, button
actions and report-page tooltips, the link checks in validate, and a push of the result.

Run: python tests/test_reportedit.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import author, common, filters, mirror, pbircheck, usage, validate  # noqa: E402
from pbz.common import PbzError  # noqa: E402


def main() -> int:
    fails: list[str] = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    def refused(fn, *args, code, **kw):
        try:
            fn(*args, **kw)
            check(False, f"{fn.__name__} {kw} should fail with {code}")
        except PbzError as e:
            check(e.code == code, f"{fn.__name__}: {e.code} {e.detail} (want {code})")

    tmp = tempfile.mkdtemp(prefix="pbzrep-")
    root = os.path.join(tmp, "ws")
    test_parity.build(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    mirror.pull(ws, project)
    validate.report_author = lambda *a, **k: []  # schema validator needs a Desktop-made report; TOM still runs
    pg = author.add_page(ws, project, "Lab")["page"]
    ch = author.add_visual(ws, project, "clusteredColumnChart", pg, ["'Customer'[Segment]", "[Total Sales]"])
    tb = author.add_visual(ws, project, "tableEx", pg, ["'Customer'[Name]", "[Total Sales]"])
    page_json = os.path.join(ws.mirror, ch["files"][0].rsplit("/visuals/", 1)[0], "page.json")
    pj = lambda: json.load(open(page_json, encoding="utf-8"))  # noqa: E731

    # ---------------------------------------------------------------- filters
    r = filters.add(ws, project, "'Customer'[Segment]", page=pg, values_in=["Retail", "Corporate"])
    check(r["type"] == "Categorical" and r["condition"] == "in ('Retail', 'Corporate')", f"basic: {r}")
    flt = pj()["filterConfig"]["filters"][0]
    check(flt["filter"]["From"] == [{"Name": "c", "Entity": "Customer", "Type": 0}]
          and flt["filter"]["Where"][0]["Condition"]["In"]["Expressions"][0]["Column"]["Expression"]["SourceRef"] == {"Source": "c"}
          and flt["howCreated"] == "User", f"basic shape: {flt}")
    r = filters.add(ws, project, "'Sales'[Region]", page=pg, values_out=["North"])
    check(r["condition"] == "not in ('North')", f"not in: {r}")
    r = filters.add(ws, project, "'Date'[Year]", report=True, where=[">= 2020", "<= 2030"])
    check(r["scope"] == "report" and r["condition"] == ">= 2020L and <= 2030L", f"advanced AND: {r}")
    r = filters.add(ws, project, "'Customer'[Name]", page=pg, visual=tb["id"], top=5, by="[Total Sales]")
    check(r["type"] == "TopN" and r["condition"] == "top 5 by [Total Sales]", f"top n: {r}")
    r = filters.add(ws, project, "[Total Sales]", page=pg, visual=ch["id"], where=["> 0"])
    check(r["type"] == "Advanced" and r["field"] == "[Total Sales]", f"measure filter: {r}")
    r = filters.add(ws, project, "'Sales'[Comment]", page=pg)
    check(r["condition"] == "no condition", f"empty filter: {r}")
    r = filters.add(ws, project, "'Customer'[Segment]", page=pg, values_in=["«Customer[Segment]#1»"])
    lit = json.load(open(os.path.join(ws.mirror, r["files"][0]), encoding="utf-8"))["filterConfig"]["filters"][-1]
    check(lit["filter"]["Where"][0]["Condition"]["In"]["Values"][0][0]["Literal"]["Value"] == "«Customer[Segment]#1»",
          "a mirror token must stand for the whole literal")
    filters.remove(ws, project, r["name"], page=pg)  # an invented token: push would refuse it (UNKNOWN_TOKEN)
    refused(filters.add, ws, project, "[Total Sales]", page=pg, where=["> 0"], code="BAD_REQUEST")
    refused(filters.add, ws, project, "'Customer'[Segment]", page=pg, values_in=["a"], where=["> 1"], code="BAD_REQUEST")
    refused(filters.add, ws, project, "'Date'[Year]", page=pg, values_in=["twenty"], code="BAD_REQUEST")
    refused(filters.add, ws, project, "'Customer'[Name]", page=pg, top=3, by="'Sales'[Amount]", code="BAD_REQUEST")
    refused(filters.add, ws, project, "'Customer'[Segment]", page=pg, values_in=["x «lit#1»"], code="BAD_REQUEST")
    lst = filters.list_(ws, project)
    names = [f["name"] for s in lst["scopes"] for f in s["filters"]]
    ours = [f for s in lst["scopes"] for f in s["filters"] if f.get("how") == "User"]
    check(len(names) == len(set(names)) and len(ours) >= 6, f"filter names unique across the report: {names}")
    r = filters.remove(ws, project, "'Sales'[Comment]", page=pg)
    check(r["removed"] == 1, f"remove: {r}")
    refused(filters.remove, ws, project, "'Sales'[Ghost]", page=pg, code="NOT_FOUND")
    g = usage.build(ws, project)
    check(g.state[usage.key("column", "Sales", "Region")] == "used", "filter fields count as used")

    # ---------------------------------------------------------------- interactions
    r = filters.interaction(ws, project, pg, ch["id"], tb["id"], "highlight")
    check(pj()["visualInteractions"] == [{"source": ch["id"], "target": tb["id"], "type": "HighlightFilter"}], f"interaction: {r}")
    filters.interaction(ws, project, pg, ch["id"], "all", "none")
    check(pj()["visualInteractions"] == [{"source": ch["id"], "target": tb["id"], "type": "NoFilter"}], "interaction replaced")
    filters.interaction(ws, project, pg, ch["id"], tb["id"], "default")
    check("visualInteractions" not in pj(), "default removes the entry")
    refused(filters.interaction, ws, project, pg, ch["id"], ch["id"], "none", code="BAD_REQUEST")

    # ---------------------------------------------------------------- tooltip and drillthrough pages, links
    tip = author.add_page(ws, project, "Hover Tip", tooltip=True)
    tpj = json.load(open(os.path.join(ws.mirror, tip["files"][0]), encoding="utf-8"))
    check(tpj["width"] == 320 and tpj["height"] == 240 and tpj["visibility"] == "HiddenInViewMode"
          and tpj["pageBinding"]["type"] == "Tooltip" and tpj["type"] == "Tooltip", f"tooltip page: {tpj}")
    dt = author.add_page(ws, project, "Detail", drillthrough=["'Customer'[Segment]"])
    dpj = json.load(open(os.path.join(ws.mirror, dt["files"][0]), encoding="utf-8"))
    par = dpj["pageBinding"]["parameters"][0]
    dflt = dpj["filterConfig"]["filters"][0]
    check(dpj["pageBinding"]["type"] == "Drillthrough" and par["boundFilter"] == dflt["name"]
          and par["fieldExpr"] == dflt["field"] and dflt["howCreated"] == "Drillthrough" and dt.get("back_button"),
          f"drillthrough page: {dpj}")
    refused(author.add_page, ws, project, "Both", tooltip=True, drillthrough=["'Customer'[Segment]"], code="BAD_REQUEST")
    refused(author.add_page, ws, project, "Bad", drillthrough=["[Total Sales]"], code="BAD_REQUEST")
    btn = author.add_visual(ws, project, "actionButton", pg, [])
    r = author.link(ws, project, pg, btn["id"], to_page="Detail")
    check(r["set"] == ["page Detail"], f"page link: {r}")
    r = author.link(ws, project, pg, btn["id"], drillthrough="Detail")
    vl = json.load(open(os.path.join(ws.mirror, btn["files"][0]), encoding="utf-8"))["visual"]["visualContainerObjects"]["visualLink"]
    check(vl[0]["properties"]["drillthroughSection"] == {"expr": {"Literal": {"Value": f"'{dt['id']}'"}}}, f"drillthrough link: {vl}")
    r = author.link(ws, project, pg, btn["id"], url="https://example.com/help")
    check(r["set"] == ["web link"], f"url: {r}")
    r = author.link(ws, project, pg, ch["id"], tooltip="Hover Tip")
    vt = json.load(open(os.path.join(ws.mirror, ch["files"][0]), encoding="utf-8"))["visual"]["visualContainerObjects"]["visualTooltip"]
    check(vt[0]["properties"]["type"] == {"expr": {"Literal": {"Value": "'Canvas'"}}}, f"tooltip link: {vt}")
    refused(author.link, ws, project, pg, btn["id"], drillthrough="Lab", code="BAD_REQUEST")
    refused(author.link, ws, project, pg, btn["id"], to_page="Nowhere", code="NOT_FOUND")
    refused(author.link, ws, project, pg, ch["id"], back=True, code="BAD_REQUEST")
    refused(author.link, ws, project, pg, btn["id"], back=True, to_page="Hover Tip", code="BAD_REQUEST")
    refused(author.link, ws, project, pg, ch["id"], tooltip="Lab", code="BAD_REQUEST")
    refused(author.link, ws, project, pg, btn["id"], url="javascript:alert(1)", code="BAD_REQUEST")

    # ---------------------------------------------------------------- bookmarks
    from pbz import bookmarks
    bm = bookmarks.add(ws, project, "Chart only", pg, hide=[tb["id"]])
    bj = json.load(open(os.path.join(ws.mirror, bm["files"][0]), encoding="utf-8"))
    cont = bj["explorationState"]["sections"][next(iter(bj["explorationState"]["sections"]))]["visualContainers"]
    check(bm["hidden"] == [tb["id"]] and cont[tb["id"]]["singleVisual"]["display"] == {"mode": "hidden"}
          and "display" not in cont[ch["id"]]["singleVisual"] and bj["options"]["suppressData"] is True, f"bookmark: {bj}")
    meta = json.load(open(os.path.join(ws.mirror, bm["files"][1]), encoding="utf-8"))
    check({"name": bm["name"]} in meta["items"], "bookmark not listed in bookmarks.json")
    r = author.link(ws, project, pg, btn["id"], bookmark="Chart only")
    check(r["set"] == ["bookmark Chart only"], f"bookmark link: {r}")
    only = bookmarks.add(ws, project, "Table off", pg, hide=[tb["id"]], only=[tb["id"]])
    check(json.load(open(os.path.join(ws.mirror, only["files"][0]), encoding="utf-8"))["options"]["targetVisualNames"] == [tb["id"]],
          "bookmark scope")
    refused(bookmarks.add, ws, project, "Chart only", pg, code="NAME_TAKEN")
    refused(bookmarks.add, ws, project, "Bad", pg, hide=["nope"], code="NOT_FOUND")
    refused(bookmarks.add, ws, project, "Both", pg, hide=[tb["id"]], show=[tb["id"]], code="BAD_REQUEST")
    check(any(b["display_name"] == "Chart only" for b in bookmarks.list_(ws, project)["bookmarks"]), "bookmark list")
    bookmarks.remove(ws, project, "Table off")
    check(not any(b["display_name"] == "Table off" for b in bookmarks.list_(ws, project)["bookmarks"]), "bookmark remove")
    g = usage.build(ws, project)
    bk = next((o for o in g.report_objects if o["kind"] == "bookmark" and o.get("name") == "Chart only"), {})
    check(bk.get("referenced"), f"a bookmark a button points at counts as referenced: {bk}")
    check(not [f for f in pbircheck.run(project.mirror_report) if f["severity"] == "error"],
          f"authored report fails checks: {pbircheck.run(project.mirror_report)}")
    author.link(ws, project, pg, btn["id"], to_page="Detail")
    bp = os.path.join(ws.mirror, btn["files"][0])
    doc = json.load(open(bp, encoding="utf-8"))
    doc["visual"]["visualContainerObjects"]["visualLink"][0]["properties"]["navigationSection"] = {"expr": {"Literal": {"Value": "'gone'"}}}
    saved = open(bp, encoding="utf-8").read()
    open(bp, "w", encoding="utf-8").write(json.dumps(doc, indent=2))
    check("PBIR_LINK_TARGET_MISSING" in {f["code"] for f in pbircheck.run(project.mirror_report)}, "dangling link not flagged")
    open(bp, "w", encoding="utf-8").write(saved)
    res = mirror.push(ws, project)
    errs = [f for f in res.get("findings", []) if f["severity"] == "error"]
    check(res["result"] == "PUSHED" and not errs, f"push: {res.get('result')} {errs}")

    if fails:
        print("\n".join("FAIL " + f for f in fails))
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
