"""`pbz synth test`: measure regression tests on the synthetic model. Offline, with a stubbed query runner:
save (spec and measure shortcut), run SAME / DIFF (cells, missing and added rows), float tolerance,
errors, drift (seed or row count, mirror model change, stale synthetic copy), list / remove, refusals
(unknown test, name taken, bad names and specs, truncated results, not synthetic).

Run: python tests/test_synthtest.py
     python tests/test_synthtest.py --live   (also builds and opens the synthetic copy of the fixture in
                                              Desktop and runs a real save / refactor / run round trip)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
import tempfile

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import common, desktop, mirror, synth, synthtest  # noqa: E402
from pbz.common import PbzError  # noqa: E402

FIXTURE = os.path.join(SKILL, "tests", "fixture")


class Stub:
    """Fake synth-query: answers by query shape; `grouped` / `single` / `fail` change between steps."""

    def __init__(self):
        self.grouped = [["Corporate", 10.0], ["Retail", 20.0]]
        self.single = [[30.0]]
        self.fail: dict[str, str] = {}
        self.truncated = False
        self.calls: list[str] = []

    def __call__(self, ws, project, dax, max_rows):
        self.calls.append(dax)
        for needle, code in self.fail.items():
            if needle in dax:
                raise PbzError(code, "stub")
        if "SUMMARIZECOLUMNS" in dax:
            return {"columns": ["Customer[Segment]", "[Total Sales]"], "rows": [list(r) for r in self.grouped],
                    "truncated": self.truncated}
        return {"columns": ["[Total Sales]"], "rows": [list(r) for r in self.single], "truncated": False}


def offline(fails: list) -> None:
    def check(cond, msg):
        if not cond:
            fails.append(msg)

    def refused(fn, *args, code, **kw):
        try:
            fn(*args, **kw)
            check(False, f"{fn.__name__} should fail with {code}")
        except PbzError as e:
            check(e.code == code, f"{fn.__name__}: {e.code} {e.detail} (want {code})")

    tmp = tempfile.mkdtemp(prefix="pbzsynthtest-")
    os.environ["PBZ_SYNTH_ROOT"] = os.path.join(tmp, "synth")
    root = os.path.join(tmp, "ws")
    test_parity.build(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    mirror.pull(ws, project)
    stub = Stub()
    synthtest.QUERY = stub

    refused(synthtest.save, ws, project, "before-build", measures=["[Total Sales]"], code="UNSAFE")
    sroot = synth.synth_project(ws, project).ws.root  # a stand-in synthetic folder (no Desktop offline)
    os.makedirs(os.path.join(sroot, ".pbz"))
    with open(os.path.join(sroot, ".pbz", "policy.json"), "w", encoding="utf-8") as f:
        json.dump({"synthetic": True}, f)
    info = {"source_project": "Parity", "built": dt.datetime.now().isoformat(timespec="seconds"), "rows": 200,
            "seed": 7, "tables": {"Sales": 200}}

    def write_info(**kw):
        with open(os.path.join(sroot, ".pbz", "synth.json"), "w", encoding="utf-8") as f:
            json.dump(dict(info, **kw), f)

    write_info()

    # ---------------------------------------------------------------- save
    q = synthtest.measure_query(project, ["[Total Sales]"], ["'Customer'[Segment]"])
    check("SUMMARIZECOLUMNS" in q["dax"] and "'Customer'[Segment]" in q["dax"] and '"Total Sales", [Total Sales]' in q["dax"]
          and "ORDER BY 'Customer'[Segment]" in q["dax"] and q["keys"] == 1, f"measure query: {q}")
    single = synthtest.measure_query(project, ["Total Sales"])  # bare names work too
    check(single["dax"].startswith("EVALUATE ROW(") and single["keys"] == 0, f"ungrouped measure query: {single}")
    s = synthtest.save(ws, project, "sales", measures=["[Total Sales]"], by=["'Customer'[Segment]"],
                       spec={"queries": [{"id": "total", "dax": "EVALUATE ROW(\"t\", [Total Sales])"}]})
    check(s["queries"] == [{"id": "total", "rows": 1, "columns": 1},
                           {"id": "[Total Sales] by 'Customer'[Segment]", "rows": 2, "columns": 2}]
          and "warning" not in s and os.path.isfile(os.path.join(root, s["file"])), f"save: {s}")
    refused(synthtest.save, ws, project, "sales", measures=["[Total Sales]"], code="NAME_TAKEN")
    refused(synthtest.save, ws, project, "../escape", measures=["[Total Sales]"], code="BAD_REQUEST")
    refused(synthtest.save, ws, project, "x", measures=["[Nope]"], code="NOT_FOUND")
    refused(synthtest.save, ws, project, "x", measures=["[Total Sales]"], by=["'Customer'[Nope]"], code="NOT_FOUND")
    refused(synthtest.save, ws, project, "x", spec='{"queries": [{"id": "a", "dax": "SELECT 1"}]}', code="BAD_REQUEST")
    refused(synthtest.save, ws, project, "x", spec='{"queries": []}', code="BAD_REQUEST")
    refused(synthtest.save, ws, project, "x", by=["'Customer'[Segment]"], code="BAD_REQUEST")
    stub.truncated = True
    refused(synthtest.save, ws, project, "big", measures=["[Total Sales]"], by=["'Customer'[Segment]"], code="TOO_MANY_ROWS")
    stub.truncated = False

    # ---------------------------------------------------------------- run
    r = synthtest.run(ws, project, "sales")
    check(r["result"] == "SAME" and r["summary"] == {"SAME": 2} and "drift" not in r, f"run SAME: {r}")
    stub.grouped = [["Corporate", 10.0 + 1e-12], ["Retail", 20.0]]
    check(synthtest.run(ws, project, "sales")["result"] == "SAME", "float within tolerance reported as DIFF")
    stub.grouped = [["Corporate", 10.5], ["Retail", 20.0]]
    r = synthtest.run(ws, project, "sales")
    g = next(x for x in r["queries"] if x["id"].startswith("[Total Sales]"))
    check(r["result"] == "DIFF" and g["changed_cells"] == 1 and g["cells"] == [
        {"key": ["Corporate"], "column": "[Total Sales]", "was": 10.0, "now": 10.5}], f"cell DIFF: {g}")
    check(synthtest.run(ws, project, "sales", tol=0.1)["result"] == "SAME", "a looser tolerance still DIFF")
    stub.grouped = [["Corporate", 10.0], ["Wholesale", 5.0]]
    g = next(x for x in synthtest.run(ws, project, "sales")["queries"] if x["id"].startswith("[Total Sales]"))
    check(g.get("missing_rows") == [["Retail"]] and g.get("added_rows") == [["Wholesale"]], f"row DIFF: {g}")
    stub.grouped = [["Corporate", 10.0], ["Retail", 20.0]]
    stub.fail = {"ROW(\"t\"": "SYNTAX"}
    r = synthtest.run(ws, project, "sales")
    check(r["result"] == "ERROR" and any(x["result"] == "ERROR:SYNTAX" for x in r["queries"]), f"error: {r}")
    stub.fail = {"ROW(\"t\"": "ENGINE_UNAVAILABLE"}
    refused(synthtest.run, ws, project, "sales", code="ENGINE_UNAVAILABLE")
    stub.fail = {}

    # ---------------------------------------------------------------- drift
    write_info(seed=8)
    r = synthtest.run(ws, project, "sales")
    check(r["result"] == "NOT_COMPARABLE" and "seed 8" in r["drift"]["synth"], f"seed drift: {r}")
    write_info(built=(dt.datetime.now() - dt.timedelta(hours=2)).isoformat(timespec="seconds"))
    mdef = os.path.join(project.mirror_model, "definition", "tables", "Sales.tmdl")
    with open(mdef, "a", encoding="utf-8", newline="") as f:
        f.write("\n\tmeasure 'Synth Test Added' = [Total Sales] * 2\n")
    r = synthtest.run(ws, project, "sales")
    check(r["result"] == "SAME" and "model" in r["drift"] and "synth_stale" in r["drift"]
          and "synth" not in r["drift"], f"model drift: {r}")
    s = synthtest.save(ws, project, "stale", measures=["[Total Sales]"])
    check("older than the mirror model" in s.get("warning", ""), f"stale save not warned: {s}")
    write_info(built=(dt.datetime.now() + dt.timedelta(seconds=5)).isoformat(timespec="seconds"))  # rebuilt after the edit
    r = synthtest.run(ws, project, "sales")
    check("synth_stale" not in r.get("drift", {}) and "model" in r["drift"], f"fresh rebuild: {r}")

    # ---------------------------------------------------------------- keys, list, remove, not synthetic
    stub.single = [[30.0]]
    spec = {"queries": [{"id": "pos", "dax": "EVALUATE ROW(\"a\", 1)"}]}
    synthtest.save(ws, project, "positional", spec=spec)
    stub.single = [[31.0]]
    p = synthtest.run(ws, project, "positional")["queries"][0]
    check(p["result"] == "DIFF" and p["cells"][0]["key"] == 0, f"positional compare: {p}")
    lst = synthtest.list_(ws)
    check({t["name"] for t in lst["tests"]} == {"sales", "stale", "positional"}, f"list: {lst}")
    check(synthtest.remove(ws, "stale")["result"] == "REMOVED", "remove")
    refused(synthtest.remove, ws, "stale", code="NOT_FOUND")
    refused(synthtest.run, ws, project, "nope", code="NOT_FOUND")
    os.remove(os.path.join(sroot, ".pbz", "policy.json"))
    refused(synthtest.run, ws, project, "sales", code="UNSAFE")
    calls = len(stub.calls)
    refused(synthtest.save, ws, project, "again", measures=["[Total Sales]"], code="UNSAFE")
    check(len(stub.calls) == calls, "a query ran although the synthetic folder is not marked")
    synthtest.QUERY = synth.query
    shutil.rmtree(tmp, ignore_errors=True)


def live(fails: list) -> None:
    from test_synth import _kill

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    tmp = tempfile.mkdtemp(prefix="pbzsynthtestlive-")
    os.environ["PBZ_SYNTH_ROOT"] = os.path.join(tmp, "synth")
    ws_dir = os.path.join(tmp, "ws")
    shutil.copytree(FIXTURE, ws_dir, ignore=shutil.ignore_patterns(".pbi", "pbz-workspace", ".pbz", "CANARIES.md"))
    ws = common.Workspace(ws_dir)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    mirror.pull(ws, project)
    sp = synth.synth_project(ws, project)
    try:
        check(synth.build(ws, project, rows=60, seed=11)["valid"], "synthetic build invalid")
        op = synth.open_(ws, project, wait=480)
        check(op.get("open") and op.get("engine") == "READY", f"synth open: {op}")
        s = synthtest.save(ws, project, "hours", measures=["[Total Hours]"], by=["'Project'[Status]"],
                           spec={"queries": [{"id": "count", "dax": "EVALUATE ROW(\"n\", COUNTROWS('Hours'))"}]})
        check(all("rows" in q for q in s["queries"]), f"live save: {s}")
        r = synthtest.run(ws, project, "hours")
        check(r["result"] == "SAME" and "drift" not in r, f"live rerun: {r}")
        # refactor the measure in the mirror, rebuild the synthetic copy with the same settings, rerun
        tdir = os.path.join(project.mirror_model, "definition", "tables")
        hit = None
        for fn in os.listdir(tdir):
            path = os.path.join(tdir, fn)
            text = open(path, encoding="utf-8").read()
            if "measure 'Total Hours'" in text:
                hit = path
                break
        check(hit is not None, "fixture measure [Total Hours] not found")
        if hit:
            text = open(hit, encoding="utf-8").read()
            start = text.index("measure 'Total Hours'")
            eq = text.index("=", start)
            line_end = text.index("\n", eq)
            new = text[:eq + 1] + " 2 * (" + text[eq + 1:line_end].strip() + ")" + text[line_end:]
            open(hit, "w", encoding="utf-8", newline="").write(new)
        r = synthtest.run(ws, project, "hours")
        check("synth_stale" in r.get("drift", {}) and "model" in r.get("drift", {}), f"stale copy not reported: {r}")
        desktop.close(sp, wait=30)
        if desktop.find_instance(sp):  # Desktop asks to save the recalculated copy: it is ours to discard
            _kill(sp.ws.root)
        check(synth.build(ws, project, rows=60, seed=11)["valid"], "rebuild invalid")
        op = synth.open_(ws, project, wait=480)
        check(op.get("open"), f"reopen: {op}")
        r = synthtest.run(ws, project, "hours")
        by = next((q for q in r["queries"] if q["id"].startswith("[Total Hours]")), {})
        cnt = next((q for q in r["queries"] if q["id"] == "count"), {})
        check(r["result"] == "DIFF" and by.get("result") == "DIFF" and by.get("changed_cells", 0) >= 1
              and cnt.get("result") == "SAME" and "synth" not in r.get("drift", {}), f"refactor DIFF: {r}")
        if by.get("cells"):
            c = by["cells"][0]
            check(abs(c["now"] - 2 * c["was"]) <= 1e-6 * max(1.0, abs(c["now"])), f"doubled measure: {c}")
    finally:
        if desktop.find_instance(sp):
            desktop.close(sp, wait=30)
        if desktop.find_instance(sp):
            _kill(sp.ws.root)
    check(not desktop.find_instance(sp), "the synthetic Desktop instance stayed open")
    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    fails: list[str] = []
    offline(fails)
    if "--live" in sys.argv:
        live(fails)
    for f in fails:
        print("FAIL", f)
    print("ok" if not fails else f"{len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
