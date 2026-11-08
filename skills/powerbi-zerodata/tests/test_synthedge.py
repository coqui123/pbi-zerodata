"""Edge-case synthetic data (`pbz synth build --edge`) and property checks with counterexamples on the
synthetic copy (`pbz synth check`). Offline: the edge rows (orphan and blank keys, keys without related
rows, duplicate and blank labels, zero / negative / blank amounts, blank text, boundary dates, a month
with no rows; sort-by columns and one-side keys untouched), the profile recorded and compared as drift,
and every check kind against a stubbed query runner (counterexamples, shrinking, token faking, refusals).

Run: python tests/test_synthedge.py
     python tests/test_synthedge.py --live   (builds the edge profile of the fixture, opens it in Desktop
                                              and runs real checks; skipped while another Desktop runs)
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import common, desktop, mirror, synth, synthtest, tmdl  # noqa: E402
from pbz.common import PbzError  # noqa: E402

FIXTURE = os.path.join(SKILL, "tests", "fixture")


class Stub:
    """Fake synth-query: the most specific matching substring answers; errors raise like synth.query."""

    def __init__(self, rules: list):
        self.rules = rules          # [(substring, rows | PbzError code)]
        self.calls: list[str] = []

    def __call__(self, ws, project, dax, max_rows):
        self.calls.append(dax)
        hits = [(s, r) for s, r in self.rules if s in dax]
        if not hits:
            return {"columns": ["v"], "rows": [[1]], "truncated": False}
        _s, r = max(hits, key=lambda h: len(h[0]))
        if isinstance(r, str):
            raise PbzError(r, "stub")
        return {"columns": [f"c{i}" for i in range(len(r[0]) if r else 1)], "rows": [list(x) for x in r], "truncated": False}


def _col(gen, table, column):
    cols, rows = gen[table]
    i = [n for n, _d in cols].index(column)
    return [r[i] for r in rows]


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

    tmp = tempfile.mkdtemp(prefix="pbzsynthedge-")
    os.environ["PBZ_SYNTH_ROOT"] = os.path.join(tmp, "synth")
    root = os.path.join(tmp, "ws")
    test_parity.build(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    mirror.pull(ws, project)
    model = tmdl.load_model(os.path.join(project.mirror_model, "definition"))
    lo, hi = synth._date_range(model)
    pools = synth._collect_pools(
        model, synth._report_texts(synth._capture_source(project.mirror_report, ws.root)), lo, hi)
    plan = [t for t in model.tables.values() if synth._rewrite_table(t)]

    # ---------------------------------------------------------------- edge rows
    gen = synth._generate(model, pools, 80, 5, plan)
    again = synth._generate(model, pools, 80, 5, plan)
    check(gen == again, "the standard generator is not deterministic")
    frozen = copy.deepcopy(gen)
    edge, notes = synth._edge(model, gen, 5, lo, hi)
    check(gen == frozen, "the edge profile changed the standard rows in place")
    keys = _col(edge, "Customer", "CustomerKey")
    fk = _col(edge, "Sales", "CustomerKey")
    check({8888000, 8888001, 8888002} <= {x for x in fk if x not in keys and x is not None}, f"orphans: {fk}")
    check(None in fk, "no blank foreign key")
    check(not set(keys[-2:]) & set(fk), "the last customer keys still have sales")
    check(len(set(keys)) == len(keys) and None not in keys, "one-side keys were touched")
    names = _col(edge, "Customer", "Name")
    check(names[1] == names[0] and names[2] is None, f"duplicate and blank label: {names[:3]}")
    check(_col(edge, "Customer", "Segment") == _col(frozen, "Customer", "Segment")
          and _col(edge, "Customer", "SortOrder") == _col(frozen, "Customer", "SortOrder"), "a sort-by column was touched")
    amount = _col(edge, "Sales", "Amount")
    check(amount[0] == 0.0 and amount[1] < 0 and amount[2] is None, f"amount edge values: {amount[:3]}")
    region = _col(edge, "Sales", "Region")
    check(region[0] is None and region[1] == "", f"blank and empty text: {region[:2]}")
    dates = _col(edge, "Sales", "DateKey")
    dnote = next((n for n in notes.get("Sales", []) if n.startswith("DateKey:")), "")
    gap = dnote.rsplit("no rows in ", 1)[-1] if "no rows in " in dnote else None
    check(dates[0] is None and lo.isoformat() in dates and hi.isoformat() in dates, f"boundary dates: {dates[:8]}")
    check(gap and not any(isinstance(d, str) and d[:7] == gap for d in dates), f"the sparse month {gap} still has rows: {dnote}")
    check(any("orphan" in n for n in notes.get("Sales", [])) and notes.get("Customer"), f"notes: {notes}")
    check(synth._fmt(None, "double") == "BLANK()" and synth._fmt(None, "string") == "BLANK()", "BLANK() literal")

    # ---------------------------------------------------------------- builds record the profile
    b = synth.build(ws, project, rows=80, seed=5)
    info = synth.build_info(ws, project)
    check(b["profile"] == "standard" and info.get("profile") == "standard" and "edge_cases" not in b, f"standard build: {b}")
    b = synth.build(ws, project, rows=80, seed=5, edge=True)
    check(b["profile"] == "edge" and b.get("edge_cases", {}).get("Sales") and b["valid"], f"edge build: {b}")
    check(synth.build_info(ws, project).get("profile") == "edge", "edge profile not recorded")
    sp = synth.synth_project(ws, project)
    sales = open(os.path.join(sp.model_dir, "definition", "tables", "Sales.tmdl"), encoding="utf-8").read()
    check("BLANK()" in sales and "8888000" in sales, "the edge rows did not reach the synthetic TMDL")

    # ---------------------------------------------------------------- profile drift in synth tests
    stub = Stub([("SUMMARIZECOLUMNS", [["Retail", 1.0]])])
    synthtest.QUERY = stub
    sroot = sp.ws.root
    with open(os.path.join(sroot, ".pbz", "synth.json"), encoding="utf-8") as f:
        sj = json.load(f)
    sj["profile"] = "standard"
    with open(os.path.join(sroot, ".pbz", "synth.json"), "w", encoding="utf-8") as f:
        json.dump(sj, f)
    synthtest.save(ws, project, "p", measures=["[Total Sales]"], by=["'Customer'[Segment]"])
    sj["profile"] = "edge"
    with open(os.path.join(sroot, ".pbz", "synth.json"), "w", encoding="utf-8") as f:
        json.dump(sj, f)
    r = synthtest.run(ws, project, "p")
    check(r["result"] == "NOT_COMPARABLE" and "profile" in r.get("drift", {}), f"profile drift: {r}")

    # ---------------------------------------------------------------- synth check
    tok = "«Sales[Amount]#7»"
    stub = Stub([
        ("SUMMARIZECOLUMNS('Customer'[Segment], 'Date'[Year], \"__pbz_l\"", [["Retail", 2024, 10.0, 10.0], ["Corporate", 2024, 5.0, 6.0]]),
        ("SUMMARIZECOLUMNS('Customer'[Segment], \"__pbz_l\"", [["Retail", 10.0, 10.0], ["Corporate", 5.0, 6.0]]),
        ("SUMMARIZECOLUMNS('Date'[Year], \"__pbz_l\"", [[2024, 15.0, 15.0]]),
        ("ROW(\"v\", [Base Unused])", [[None]]),
        ("ROW(\"v\", NOT", "SYNTAX"),
        ("\"o\", COUNTROWS(FILTER(ADDCOLUMNS(DISTINCT('Customer'[Name])", [[50, 1]]),
        ("TOPN(10, FILTER(ADDCOLUMNS(DISTINCT('Customer'[Name])", [["Name 1", 2]]),
        ("COUNTROWS(EXCEPT(FILTER(DISTINCT('Sales'[CustomerKey])", [[30, 3]]),
        ("TOPN(10, EXCEPT(FILTER(DISTINCT('Sales'[CustomerKey])", [[8888000], [8888001], [8888002]]),
        ("ROW(\"v\", CALCULATE([Total Sales], 'Customer'[Segment] = \"Retail\"))", [[-4.5]]),
        ("DEFINE MEASURE", "UNKNOWN_OBJECT"),
        ("ROW(\"l\", [Total Sales], \"r\", [Total Sales] * 2)", [[10.0, 20.0]]),
        ("ROW(\"t\", [Total Sales])", [[100.0]]),
        ("SUMMARIZECOLUMNS('Customer'[Segment], \"__pbz_v\"", [["Retail", 60.0], ["Corporate", 30.0]]),
        ("CALCULATE(COUNTROWS('Sales'), 'Sales'[Region] = \"\")", [[200, 2]]),
        ("TOPN(5, CALCULATETABLE('Sales', 'Sales'[Region] = \"\"))", [[1, "", 3.0]]),
        ("\"__pbz_r\", CALCULATE([Total Sales], REMOVEFILTERS('Customer'[Segment])))", [["Retail", 10.0, 30.0]]),
        ("ROW(\"n\", COUNTROWS('Orphan'))", [[0]]),
        ("ROW(\"v\", [Ship Sales])", [[12.5]]),
        ("'Sales'[Amount] > ", [[7.0]]),
    ])
    synthtest.QUERY = stub
    spec = {"checks": [
        {"id": "eq", "kind": "equal", "left": "[Total Sales]", "right": "SUM('Sales'[Amount])",
         "by": ["'Customer'[Segment]", "'Date'[Year]"]},
        {"id": "nb", "kind": "nonblank", "expr": "[Base Unused]"},
        {"id": "uq", "kind": "unique", "column": "'Customer'[Name]"},
        {"id": "fk", "kind": "no_orphans", "from": "'Sales'[CustomerKey]", "to": "'Customer'[CustomerKey]"},
        {"id": "sg", "kind": "sign", "expr": "[Total Sales]", "where": ["'Customer'[Segment] = \"Retail\""], "expect": ["POS"]},
        {"id": "cp", "kind": "compiles", "expr": "[Nope] + 1", "table": "Sales"},
        {"id": "df", "kind": "differs", "left": "[Total Sales]", "right": "[Total Sales] * 2"},
        {"id": "ad", "kind": "additive", "expr": "[Total Sales]", "by": ["'Customer'[Segment]"]},
        {"id": "bk", "kind": "no_blank_keys", "column": "'Sales'[Region]"},
        {"id": "fi", "kind": "filter_independent", "expr": "[Total Sales]", "column": "'Customer'[Segment]",
         "by": ["'Customer'[Segment]"]},
        {"id": "ne", "kind": "nonempty", "expr": "'Orphan'"},
        {"id": "ex", "kind": "executes", "expr": "[Ship Sales]"},
        {"id": "tk", "kind": "executes", "expr": "[Total Sales]", "where": [f"'Sales'[Amount] > {tok}"]},
        {"id": "bt", "kind": "between", "expr": "[Total Sales]"},
        {"id": "xx", "kind": "bogus"},
        {"id": "nc", "kind": "unique"}]}
    refused_root = sp.ws.root + "-gone"
    os.rename(sp.ws.root, refused_root)
    refused(synthtest.check, ws, project, spec, code="UNSAFE")
    check(not stub.calls, "a query ran before the synthetic folder was verified")
    os.rename(refused_root, sp.ws.root)
    res = synthtest.check(ws, project, json.dumps(spec))
    by_id = {r["id"]: r for r in res["results"]}
    eq = by_id["eq"]
    check(eq["status"] == "FAIL" and eq["mismatch"] == "ONE" and eq["counterexample"] == [
        {"group": {"'Customer'[Segment]": "Corporate", "'Date'[Year]": 2024}, "left": 5.0, "right": 6.0}]
          and eq.get("fails_at_single_column") == ["'Customer'[Segment]"], f"equal: {eq}")
    check(by_id["nb"]["status"] == "FAIL" and by_id["nb"]["value"] is None, f"nonblank: {by_id['nb']}")
    check(by_id["uq"]["status"] == "FAIL" and by_id["uq"]["counterexample"] == [["Name 1", 2]], f"unique: {by_id['uq']}")
    check(by_id["fk"]["status"] == "FAIL" and by_id["fk"]["counterexample"] == [8888000, 8888001, 8888002], f"orphans: {by_id['fk']}")
    check(by_id["sg"]["status"] == "FAIL" and by_id["sg"]["sign"] == "NEG", f"sign: {by_id['sg']}")
    check(by_id["cp"]["status"] == "ERROR" and by_id["cp"]["class"] == "UNKNOWN_OBJECT", f"compiles: {by_id['cp']}")
    check(by_id["df"]["status"] == "PASS" and by_id["df"]["differences"] == {"left": 10.0, "right": 20.0}, f"differs: {by_id['df']}")
    check(by_id["ad"]["status"] == "FAIL" and by_id["ad"]["total"] == 100.0 and by_id["ad"]["sum_of_groups"] == 90.0, f"additive: {by_id['ad']}")
    check(by_id["bk"]["status"] == "FAIL" and by_id["bk"]["counterexample"]["blank_rows"] == 2, f"blank keys: {by_id['bk']}")
    check(by_id["fi"]["status"] == "FAIL" and by_id["fi"]["counterexample"][0]["right"] == 30.0, f"filter independent: {by_id['fi']}")
    check(by_id["ne"]["status"] == "FAIL" and by_id["ne"]["rows"] == 0, f"nonempty: {by_id['ne']}")
    check(by_id["ex"]["status"] == "PASS" and by_id["ex"]["value"] == 12.5, f"executes: {by_id['ex']}")
    check(by_id["tk"]["status"] == "PASS" and not any(tok in d for d in stub.calls)
          and any(str(synth.fake_int(tok)) in d for d in stub.calls), "a bare mirror token reached the query")
    check(by_id["bt"]["status"] == "UNSUPPORTED" and by_id["xx"]["status"] == "ERROR"
          and by_id["nc"]["status"] == "ERROR" and by_id["nc"]["class"] == "BAD_REQUEST", "refusals inside a spec")
    check(res["data"] == "synthetic" and res["profile"] == "edge" and res["summary"].get("FAIL", 0) >= 8, f"summary: {res['summary']}")
    refused(synthtest.check, ws, project, '{"checks": []}', code="BAD_REQUEST")
    synthtest.QUERY = Stub([("ROW(\"v\", [Ship Sales])", "ENGINE_UNAVAILABLE")])
    refused(synthtest.check, ws, project, {"checks": [{"id": "e", "kind": "executes", "expr": "[Ship Sales]"}]},
            code="ENGINE_UNAVAILABLE")
    synthtest.QUERY = synth.query
    shutil.rmtree(tmp, ignore_errors=True)


def _desktop_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq PBIDesktop.exe", "/NH"], capture_output=True, text=True,
                             timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return "PBIDesktop.exe" in out


def live(fails: list) -> None:
    from test_synth import _kill

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    if _desktop_running():
        print("SKIP live: another Power BI Desktop is running")
        return
    tmp = tempfile.mkdtemp(prefix="pbzsynthedgelive-")
    os.environ["PBZ_SYNTH_ROOT"] = os.path.join(tmp, "synth")
    ws_dir = os.path.join(tmp, "ws")
    shutil.copytree(FIXTURE, ws_dir, ignore=shutil.ignore_patterns(".pbi", "pbz-workspace", ".pbz", "CANARIES.md"))
    ws = common.Workspace(ws_dir)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    mirror.pull(ws, project)
    sp = synth.synth_project(ws, project)
    try:
        b = synth.build(ws, project, rows=60, seed=3, edge=True)
        check(b["valid"] and b["profile"] == "edge" and b.get("edge_cases", {}).get("Hours"), f"edge build: {b}")
        op = synth.open_(ws, project, wait=480)
        check(op.get("open") and op.get("engine") == "READY", f"synth open: {op}")
        q = synth.query(ws, project, "EVALUATE ROW(\"blanks\", COUNTROWS(FILTER('Hours', ISBLANK('Hours'[Hours]))))")
        check(q["rows"] and (q["rows"][0][0] or 0) >= 1, f"BLANK() rows did not load: {q}")
        spec = {"checks": [
            {"id": "fk", "kind": "no_orphans", "from": "'Hours'[ProjectID]", "to": "'Project'[ProjectID]"},
            {"id": "bk", "kind": "no_blank_keys", "column": "'Hours'[ProjectID]"},
            {"id": "pk", "kind": "unique", "column": "'Project'[ProjectID]"},
            {"id": "title", "kind": "unique", "column": "'Project'[Title]"},
            {"id": "sum", "kind": "equal", "left": "SUM('Hours'[Hours])", "right": "SUMX('Hours', 'Hours'[Hours])",
             "by": ["'Project'[Status]"]},
            {"id": "neg", "kind": "sign", "expr": "MIN('Hours'[Hours])", "expect": ["POS", "ZERO"]}]}
        res = synthtest.check(ws, project, spec)
        r = {x["id"]: x for x in res["results"]}
        check(r["fk"]["status"] == "FAIL" and 8888000 in (r["fk"].get("counterexample") or []), f"live orphans: {r['fk']}")
        check(r["bk"]["status"] == "FAIL", f"live blank key: {r['bk']}")
        check(r["pk"]["status"] == "PASS", f"live primary key: {r['pk']}")
        check(r["title"]["status"] == "FAIL" and r["title"].get("counterexample"), f"live duplicate label: {r['title']}")
        check(r["sum"]["status"] == "PASS", f"live equal: {r['sum']}")
        check(r["neg"]["status"] == "FAIL" and r["neg"]["sign"] == "NEG", f"live negative amount: {r['neg']}")
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
