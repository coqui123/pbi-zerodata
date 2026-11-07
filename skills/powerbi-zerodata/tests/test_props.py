"""Typed model property edits (`pbz set` / `pbz get`) on the synthetic parity project: descriptions
(/// lines), display folders, format strings, hidden and key flags, data category, summarize-by,
sort-by with loop refusal, bulk selection, unset, refusals, and a push that TOM validates.

Run: python tests/test_props.py
"""
from __future__ import annotations

import os
import sys
import tempfile

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import common, mirror, props, tmdl, validate  # noqa: E402
from pbz.common import PbzError  # noqa: E402


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

    tmp = tempfile.mkdtemp(prefix="pbzprops-")
    root = os.path.join(tmp, "ws")
    test_parity.build(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    mirror.pull(ws, project)
    validate.report_author = lambda *a, **k: []
    mdef = os.path.join(project.mirror_model, "definition")
    model = lambda: tmdl.load_model(mdef)  # noqa: E731

    r = props.run(ws, project, "[Total Sales]", ["description=Sales amount\nexcluding returns", "formatString=#,0.00",
                                                 "displayFolder=KPIs\\Sales"])
    check(r["result"] == "CHANGED" and r["changed"][0]["changes"] == ["description", "formatString", "displayFolder"], f"set: {r}")
    g = props.get(ws, project, "[Total Sales]")["objects"][0]
    check(g.get("description") == "Sales amount\nexcluding returns" and g.get("formatString") == "#,0.00"
          and g.get("displayFolder") == "KPIs\\Sales", f"get: {g}")
    check(model().measure("Total Sales").format_string == "#,0.00", "format string not parsed back")
    r = props.run(ws, project, "[Total Sales]", ["description=One line now"])
    check(props.get(ws, project, "[Total Sales]")["objects"][0].get("description") == "One line now", "description replace")
    r = props.run(ws, project, None, ["displayFolder=Base"], kind="measure", table="Sales", match="Unused")
    check({c["object"] for c in r["changed"]} == {"[Base Unused]", "[Top Unused]"}, f"bulk by match: {r}")
    r = props.run(ws, project, None, ["isHidden=true"], kind="column", table="Sales", match="Key$")
    check(len(r["changed"]) >= 3 and all(c["changes"] == ["isHidden"] for c in r["changed"]), f"bulk hide keys: {r}")
    check(model().column("Sales", "CustomerKey").hidden, "isHidden not written")
    r = props.run(ws, project, "'Sales'[CustomerKey]", ["isHidden=false"])
    check(not model().column("Sales", "CustomerKey").hidden, f"unhide: {r}")
    props.run(ws, project, "'Sales'[Amount]", ["summarizeBy=sum", "dataCategory=Uncategorized"])
    check(props.get(ws, project, "'Sales'[Amount]")["objects"][0].get("summarizeBy") == "sum", "summarizeBy")
    props.run(ws, project, "'Date'[Month]", ["sortByColumn=MonthNo"])
    check(model().column("Date", "Month").sort_by == "MonthNo", "sortByColumn")
    refused(props.run, ws, project, "'Date'[MonthNo]", ["sortByColumn=Month"], code="BAD_REQUEST")
    refused(props.run, ws, project, "'Date'[Month]", ["sortByColumn=Month"], code="BAD_REQUEST")
    refused(props.run, ws, project, "'Date'[Month]", ["sortByColumn=Nope"], code="NOT_FOUND")
    refused(props.run, ws, project, "[Total Sales]", ["summarizeBy=sum"], code="BAD_REQUEST")
    refused(props.run, ws, project, "'Sales'[Amount]", ["summarizeBy=median"], code="BAD_REQUEST")
    refused(props.run, ws, project, "'Sales'[Amount]", ["dataType=string"], code="BAD_REQUEST")
    refused(props.run, ws, project, "[Total Sales]", ["description=has «lit#1» token"], code="BAD_REQUEST")
    refused(props.run, ws, project, "'Sales'", ["dataCategory=City"], code="BAD_REQUEST")
    refused(props.run, ws, project, None, ["isHidden=true"], code="BAD_REQUEST")
    refused(props.run, ws, project, "[Nope]", ["isHidden=true"], code="NOT_FOUND")
    r = props.run(ws, project, "[Total Sales]", [], unsets=["displayFolder", "description"])
    g = props.get(ws, project, "[Total Sales]")["objects"][0]
    check("displayFolder" not in g and "description" not in g, f"unset: {g}")
    r = props.run(ws, project, "[Total Sales]", ["formatString=#,0.00"])
    check(r["result"] == "NOTHING_TO_CHANGE", f"idempotent: {r}")
    props.run(ws, project, "'Date'", ["dataCategory=Time"])
    check(props.get(ws, project, "'Date'")["objects"][0].get("dataCategory") == "Time", "mark date table category")
    res = mirror.push(ws, project)
    errs = [f for f in res.get("findings", []) if f["severity"] == "error"]
    check(res["result"] == "PUSHED" and not errs and not any(f["code"].startswith("TOM_") and f["code"] != "TOM_PASSED" for f in res.get("findings", [])),
          f"push (TOM validates the TMDL): {res.get('result')} {errs}")

    if fails:
        print("\n".join("FAIL " + f for f in fails))
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
