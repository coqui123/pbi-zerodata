"""Visual formatting on the synthetic parity project: typed values from Microsoft's formatting catalog,
container vs visual objects, series and state selectors, bulk edits by type and page, unset cleanup,
refusals (types, members, unknown names, tokens, complex values), the formatting checks in validate,
and a push of the formatted report.

Run: python tests/test_style.py
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
from pbz import author, common, mirror, pbircheck, style, validate  # noqa: E402
from pbz.common import PbzError  # noqa: E402


def lit(v: str) -> dict:
    return {"expr": {"Literal": {"Value": v}}}


def main() -> int:
    fails: list[str] = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    def refused(fn, *args, code, **kw):
        try:
            fn(*args, **kw)
            check(False, f"{fn.__name__} {args[2:4]} {kw} should fail with {code}")
        except PbzError as e:
            check(e.code == code, f"{fn.__name__}: {e.code} {e.detail} (want {code})")

    tmp = tempfile.mkdtemp(prefix="pbzstyle-")
    root = os.path.join(tmp, "ws")
    test_parity.build(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    mirror.pull(ws, project)
    validate.report_author = lambda *a, **k: []  # schema validator needs a Desktop-made report; TOM still runs
    pg = author.add_page(ws, project, "Style Lab")["page"]
    ch = author.add_visual(ws, project, "clusteredColumnChart", pg, ["'Customer'[Segment]", "[Total Sales]"])
    card = author.add_visual(ws, project, "card", pg, ["[Total Sales]"])
    btn = author.add_visual(ws, project, "actionButton", pg, [])
    path = lambda v: os.path.join(ws.mirror, v["files"][0])  # noqa: E731
    load = lambda v: json.load(open(path(v), encoding="utf-8"))["visual"]  # noqa: E731

    # ---------------------------------------------------------------- typed values
    r = style.run(ws, project, pg, ch["id"], sets=[
        "title.text=Sales by segment", "title.fontSize=14", "labels.show=on", "labels.labelDisplayUnits=thousands",
        "labels.labelPrecision=1", "legend.position=top", "dataPoint.fill=theme:2:-25", "container.background.color=#fff",
        "categoryAxis.fontFamily=Segoe UI", "title.alignment=Center"])
    check(len(r["changed"]) == 1 and len(r["changed"][0]["changes"]) == 10, f"set: {r}")
    v = load(ch)
    check(v["visualContainerObjects"]["title"][0]["properties"]["text"] == lit("'Sales by segment'"), "title text")
    check(v["visualContainerObjects"]["title"][0]["properties"]["fontSize"] == lit("14D"), "font size")
    check(v["visualContainerObjects"]["title"][0]["properties"]["alignment"] == lit("'center'"), "alignment")
    check(v["objects"]["labels"][0]["properties"]["show"] == lit("true"), "bool")
    check(v["objects"]["labels"][0]["properties"]["labelDisplayUnits"] == lit("1000D"), "display units")
    check(v["objects"]["labels"][0]["properties"]["labelPrecision"] == lit("1L"), "integer")
    check(v["objects"]["legend"][0]["properties"]["position"] == lit("'Top'"), "enum member case")
    check(v["objects"]["dataPoint"][0]["properties"]["fill"] ==
          {"solid": {"color": {"expr": {"ThemeDataColor": {"ColorId": 2, "Percent": -0.25}}}}}, "theme color shade")
    check(v["visualContainerObjects"]["background"][0]["properties"]["color"] == {"solid": {"color": lit("'#FFFFFF'")}},
          "background resolves to the container, hex normalized")

    # ---------------------------------------------------------------- selectors
    style.run(ws, project, pg, ch["id"], sets=["dataPoint.fill=#118DFF"], series="[Total Sales]")
    entries = load(ch)["objects"]["dataPoint"]
    check(len(entries) == 2 and entries[1]["selector"] == {"metadata": "Sales.Total Sales"}, f"series selector: {entries}")
    style.run(ws, project, pg, btn["id"], sets=["fill.fillColor=#333333"], state="hover")
    check(load(btn)["objects"]["fill"][0]["selector"] == {"id": "hover"}, "state selector")
    refused(style.run, ws, project, pg, btn["id"], sets=["fill.fillColor=#333333"], state="dancing", code="BAD_REQUEST")
    refused(style.run, ws, project, pg, ch["id"], sets=["dataPoint.fill=#111111"], series="[Ship Sales]", code="NOT_FOUND")
    style.run(ws, project, pg, ch["id"], sets=["dataPoint.fill=measure:[Total Sales]"], series="[Total Sales]")
    check(load(ch)["objects"]["dataPoint"][1]["properties"]["fill"]["solid"]["color"]["expr"]["Measure"]["Property"] == "Total Sales",
          "field value color")

    # ---------------------------------------------------------------- show, unset, bulk
    rows = style.run(ws, project, pg, ch["id"], show=True)["formatting"][0]["properties"]
    check({"labels.show = true", "labels.labelDisplayUnits = 1000", "container.title.text = Sales by segment",
           "dataPoint.fill = theme:2:-0.25", "dataPoint.fill [metadata=Sales.Total Sales] = measure:[Total Sales]"} <= set(rows),
          f"show: {rows}")
    r = style.run(ws, project, pg, ch["id"], unsets=["labels.show", "labels.labelDisplayUnits", "labels.labelPrecision",
                                                     "container.background.color"])
    v = load(ch)
    check("labels" not in v["objects"] and "background" not in v["visualContainerObjects"], f"unset cleanup: {v.get('objects')}")
    r = style.run(ws, project, None, None, sets=["labels.show=true"], vtype="clusteredColumnChart")
    check(len(r["changed"]) >= 2, f"report-wide by type: {r}")
    r = style.run(ws, project, pg, None, sets=["labels.show=true", "container.border.show=true"], all_=True)
    kinds = {c["type"]: c["changes"] for c in r["changed"]}
    check(kinds.get("actionButton") == ["container.border.show"] and kinds.get("card") == ["container.border.show"]
          and kinds.get("clusteredColumnChart") == ["labels.show", "container.border.show"], f"page bulk: {r}")
    check({s["type"] for s in r.get("skipped", [])} == {"actionButton", "card"}, f"inapplicable property not reported: {r}")

    # ---------------------------------------------------------------- refusals
    refused(style.run, ws, project, pg, card["id"], sets=["labels.show=true"], code="BAD_REQUEST")
    refused(style.run, ws, project, pg, ch["id"], sets=["labels.show=maybe"], code="BAD_REQUEST")
    refused(style.run, ws, project, pg, ch["id"], sets=["legend.position=Sideways"], code="BAD_REQUEST")
    refused(style.run, ws, project, pg, ch["id"], sets=["labels.colour=#000000"], code="BAD_REQUEST")
    refused(style.run, ws, project, pg, ch["id"], sets=["title.text=«lit#1»"], code="BAD_REQUEST")
    refused(style.run, ws, project, pg, ch["id"], sets=["dataPoint.fill=measure:[Nope]"], code="NOT_FOUND")
    refused(style.run, ws, project, pg, ch["id"], sets=["dataPoint.fillRule=x"], code="UNSUPPORTED")
    refused(style.run, ws, project, pg, None, sets=["labels.show=true"], code="BAD_REQUEST")
    refused(style.run, ws, project, pg, "zz-not-a-visual", sets=["labels.show=true"], code="NOT_FOUND")
    try:
        style.run(ws, project, pg, ch["id"], sets=["labels.colour=#000000"])
    except PbzError as e:
        check("labels.color" in e.detail, f"no close match offered: {e.detail}")
    lst = style.list_(ws, project, "KPI", "indicator")
    check(lst["type"] == "kpi" and lst["visual"]["showIcon"] == "bool", f"list: {lst}")
    check("title" in style.list_(ws, project, "card")["container_objects"], "container objects not listed")

    # ---------------------------------------------------------------- checks and push
    check(not pbircheck.run(project.mirror_report), f"formatted report fails checks: {pbircheck.run(project.mirror_report)}")
    doc = json.load(open(path(card), encoding="utf-8"))
    saved = json.dumps(doc, indent=2)
    doc["visual"].setdefault("objects", {})["wobble"] = [{"properties": {"x": lit("true")}}]
    doc["visual"]["objects"]["labels"] = [{"properties": {"fontSize": lit("'big'"), "glow": lit("true")}}]
    open(path(card), "w", encoding="utf-8").write(json.dumps(doc, indent=2))
    codes = {f["code"] for f in pbircheck.run(project.mirror_report)}
    check({"PBIR_OBJECT_UNKNOWN", "PBIR_PROPERTY_UNKNOWN", "PBIR_PROPERTY_VALUE"} <= codes, f"formatting checks: {codes}")
    open(path(card), "w", encoding="utf-8").write(saved)
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
