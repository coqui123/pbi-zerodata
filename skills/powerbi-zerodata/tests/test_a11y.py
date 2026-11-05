"""Accessibility audit, tab-order fix and phone layout on the synthetic parity project: alt text
(missing, token-only, too long, set through `pbz style`), hidden titles, reading-order tab order
with duplicates and hidden visuals, small fonts typed by the formatting catalog, WCAG contrast of
static colors, the no-phone-layout note, mobile.json placement / repositioning / removal, and a push.

Run: python tests/test_a11y.py
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
from pbz import a11y, author, common, mirror, pbircheck, style, validate  # noqa: E402
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
            check(False, f"{fn.__name__} {args[2:]} {kw} should fail with {code}")
        except PbzError as e:
            check(e.code == code, f"{fn.__name__}: {e.code} {e.detail} (want {code})")

    tmp = tempfile.mkdtemp(prefix="pbza11y-")
    root = os.path.join(tmp, "ws")
    test_parity.build(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    mirror.pull(ws, project)
    validate.report_author = lambda *a, **k: []
    pg = author.add_page(ws, project, "A11y Lab")["page"]
    ch = author.add_visual(ws, project, "clusteredColumnChart", pg, ["'Customer'[Segment]", "[Total Sales]"], pos="20,20,400,300")
    card = author.add_visual(ws, project, "card", pg, ["[Total Sales]"], pos="440,20,200,120")
    tb = author.add_visual(ws, project, "tableEx", pg, ["'Customer'[Name]", "[Total Sales]"], pos="20,340,620,300")
    txt = author.add_visual(ws, project, "textbox", pg, [], text="Notes", pos="660,20,200,60")
    path = lambda v: os.path.join(ws.mirror, v["files"][0])  # noqa: E731

    def codes(vid=None):
        return {f["code"] for f in a11y.audit(ws, project, pg)["findings"] if vid is None or f["visual"] == vid}

    # ---------------------------------------------------------------- alt text and titles
    r = a11y.audit(ws, project, pg)
    missing = {f["visual"] for f in r["findings"] if f["code"] == "A11Y_ALT_TEXT_MISSING"}
    check(missing == {ch["id"], card["id"], tb["id"]}, f"alt text missing on data visuals only: {missing}")
    check(all("container.general.altText" in f.get("hint", "") for f in r["findings"] if f["code"] == "A11Y_ALT_TEXT_MISSING"),
          "alt-text hint")
    style.run(ws, project, pg, ch["id"], sets=["container.general.altText=Sales by customer segment"])
    gen = json.load(open(path(ch), encoding="utf-8"))["visual"]["visualContainerObjects"]["general"]
    check(gen[0]["properties"]["altText"] == lit("'Sales by customer segment'"), f"style sets alt text: {gen}")
    check("A11Y_ALT_TEXT_MISSING" not in codes(ch["id"]), "alt text set through pbz style still reported")
    doc = json.load(open(path(card), encoding="utf-8"))
    doc["visual"].setdefault("visualContainerObjects", {})["general"] = [{"properties": {"altText": lit("«lit#3»")}}]
    json.dump(doc, open(path(card), "w", encoding="utf-8"), indent=2)
    check("A11Y_ALT_TEXT_TOKEN" in codes(card["id"]), "token-only alt text not flagged")
    style.run(ws, project, pg, tb["id"], sets=["container.general.altText=" + "x" * 300])
    check("A11Y_ALT_TEXT_LONG" in codes(tb["id"]), "long alt text not flagged")
    style.run(ws, project, pg, tb["id"], sets=["container.general.altText=Customers and their sales", "container.title.show=false"])
    check("A11Y_TITLE_HIDDEN" in codes(tb["id"]), "hidden title not flagged")

    # ---------------------------------------------------------------- fonts and contrast
    style.run(ws, project, pg, ch["id"], sets=["labels.fontSize=6", "container.title.fontSize=14"])
    small = [f for f in a11y.audit(ws, project, pg)["findings"] if f["code"] == "A11Y_SMALL_FONT"]
    check(len(small) == 1 and "labels.fontSize" in small[0]["message"], f"small font: {small}")
    style.run(ws, project, pg, ch["id"], sets=["container.title.fontColor=#777777", "container.title.background=#888888"])
    check("A11Y_LOW_CONTRAST" in codes(ch["id"]), "low contrast title not flagged")
    style.run(ws, project, pg, ch["id"], sets=["container.title.fontColor=#000000", "container.title.background=#FFFFFF"])
    check("A11Y_LOW_CONTRAST" not in codes(ch["id"]), "black on white flagged")
    check(round(a11y.contrast("000000", "FFFFFF"), 1) == 21.0 and round(a11y.contrast("777777", "FFFFFF"), 2) == 4.48,
          "WCAG ratio formula")

    # ---------------------------------------------------------------- tab order
    order = [ch["id"], card["id"], txt["id"], tb["id"]]  # reading order: top row left to right, then the table
    check(a11y.reading_order(a11y._visuals(os.path.dirname(os.path.dirname(os.path.dirname(path(ch)))))) == order,
          "reading order")
    for i, v in enumerate((tb, txt, card, ch)):  # reversed keyboard order
        d = json.load(open(path(v), encoding="utf-8"))
        d["position"]["tabOrder"] = i * 10
        json.dump(d, open(path(v), "w", encoding="utf-8"), indent=2)
    check("A11Y_TAB_ORDER" in codes(), "wrong keyboard order not flagged")
    d = json.load(open(path(card), encoding="utf-8"))
    d["position"]["tabOrder"] = 0
    json.dump(d, open(path(card), "w", encoding="utf-8"), indent=2)
    check("A11Y_TAB_ORDER_DUPLICATE" in codes(), "duplicate tab order not flagged")
    r = a11y.fix_tab_order(ws, project, pg)
    check(r["order"] == order and r["changed"], f"fix tab order: {r}")
    check(not {"A11Y_TAB_ORDER", "A11Y_TAB_ORDER_DUPLICATE"} & codes(), "tab order still flagged after the fix")
    check(a11y.fix_tab_order(ws, project, pg)["changed"] == [], "tab order fix is not idempotent")
    d = json.load(open(path(txt), encoding="utf-8"))
    d["isHidden"] = True
    json.dump(d, open(path(txt), "w", encoding="utf-8"), indent=2)
    check("A11Y_HIDDEN_IN_TAB_ORDER" in codes(txt["id"]), "hidden visual in tab order not noted")
    check(a11y.fix_tab_order(ws, project, pg)["order"][-1] == txt["id"], "hidden visuals go last")

    # ---------------------------------------------------------------- phone layout
    check("A11Y_NO_MOBILE_LAYOUT" in codes(), "missing phone layout not noted")
    r = a11y.mobile(ws, project, pg, "all")
    placed = {p["visual"]: p["box"] for p in r["placed"]}
    check(set(placed) == {ch["id"], card["id"], tb["id"]} and all(b[2] == a11y.PHONE_WIDTH for b in placed.values()),
          f"phone layout of the page: {r}")
    mj = json.load(open(os.path.join(os.path.dirname(path(ch)), "mobile.json"), encoding="utf-8"))
    check(mj["$schema"].startswith(a11y.MOBILE_SCHEMA + "/") and set(mj["position"]) >= {"x", "y", "width", "height"},
          f"mobile.json shape: {mj}")
    ys = sorted(b[1] for b in placed.values())
    check(ys[0] == 0 and all(b > a for a, b in zip(ys, ys[1:])), f"stacked placement: {ys}")
    check("A11Y_NO_MOBILE_LAYOUT" not in codes(), "phone layout still reported missing")
    r = a11y.mobile(ws, project, pg, card["id"], pos="10,500,150,90")
    mj = json.load(open(os.path.join(os.path.dirname(path(card)), "mobile.json"), encoding="utf-8"))
    check(mj["position"]["x"] == 10 and mj["position"]["width"] == 150, f"explicit phone position: {mj}")
    refused(a11y.mobile, ws, project, pg, card["id"], pos="10,500,-1,90", code="BAD_REQUEST")
    refused(a11y.mobile, ws, project, pg, "all", pos="0,0,10,10", code="BAD_REQUEST")
    refused(a11y.mobile, ws, project, pg, "nope", code="NOT_FOUND")
    r = a11y.mobile(ws, project, pg, card["id"], remove=True)
    check(r["removed"] == [card["id"]] and not os.path.exists(os.path.join(os.path.dirname(path(card)), "mobile.json")),
          f"remove phone position: {r}")
    check(not [f for f in pbircheck.run(project.mirror_report) if f["severity"] == "error"], "a11y edits fail PBIR checks")
    style.run(ws, project, pg, card["id"], sets=["container.general.altText=Total sales"])  # the planted token is not in the map
    res = mirror.push(ws, project)
    errs = [f for f in res.get("findings", []) if f["severity"] == "error"]
    check(res["result"] == "PUSHED" and not errs, f"push: {res.get('result')} {errs}")
    real = os.path.join(project.report_dir, os.path.relpath(os.path.dirname(path(ch)), project.mirror_report), "mobile.json")
    check(os.path.exists(real), "mobile.json not pushed to the real report")

    if fails:
        print("\n".join("FAIL " + f for f in fails))
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
