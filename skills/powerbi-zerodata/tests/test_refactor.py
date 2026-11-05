"""Refactoring and layout verbs on the synthetic parity project: rename table, move measure, layout map
and lint. Each edit is pushed through the full staged validation (TOM, PBIR integrity, field refs).

Run: python tests/test_refactor.py
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
from pbz import common, layout, mirror, move, rename, usage, validate  # noqa: E402
from pbz.common import PbzError  # noqa: E402


def states(ws, project, swap=None):
    g = usage.build(ws, project)
    out = set()
    for k, o in g.objects.items():
        name, table = o.name, o.table
        if swap:
            name = swap.get(name, name)
            table = swap.get(table, table)
        out.add((o.kind, table, name, g.state[k]))
    return out


def main() -> int:
    fails: list[str] = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    tmp = tempfile.mkdtemp(prefix="pbzrefactor-")
    root = os.path.join(tmp, "ws")
    test_parity.build(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    mirror.pull(ws, project)
    validate.report_author = lambda *a, **k: []  # schema validator needs a Desktop-made report; TOM still runs
    st0 = states(ws, project)

    # ---------------------------------------------------------------- rename table (quoted new name)
    r = rename.rename(ws, project, "table", "Customer", "Client Accounts")
    check(r.get("file_renamed") == {"from": "tables/Customer.tmdl", "to": "tables/Client Accounts.tmdl"}, f"file rename: {r}")
    mt = mirror.mirror_texts(ws, project)
    blob = "\n".join(t for rel, t in mt.items())
    plain = re.sub(r"«[^»]*»", "", blob)  # token labels keep the old name until the next pull
    check(not re.search(r"\bCustomer\b(?!Key)", plain), "old table name still referenced")
    model_t = mt["Parity.SemanticModel/definition/model.tmdl"]
    check("ref table 'Client Accounts'" in model_t, "model.tmdl ref not renamed")
    rels = mt["Parity.SemanticModel/definition/relationships.tmdl"]
    check("toColumn: 'Client Accounts'.CustomerKey" in rels, "relationship endpoint not renamed")
    check("tablePermission 'Client Accounts'" in mt["Parity.SemanticModel/definition/roles/Region Reader.tmdl"], "RLS table")
    check("partition 'Client Accounts' = m" in mt["Parity.SemanticModel/definition/tables/Client Accounts.tmdl"], "partition name")
    check("'Client Accounts'[Name]" in mt["Parity.SemanticModel/definition/tables/Sales.tmdl"], "DAX reference")
    check('"Entity": "Client Accounts"' in mt["Parity.Report/definition/pages/p1/visuals/v_bar/visual.json"]
          and '"Entity": "Client Accounts"' in mt["Parity.Report/definition/pages/p1/page.json"], "PBIR entity / From alias")
    check(states(ws, project, {"Client Accounts": "Customer"}) == st0, "usage states changed by the rename")
    check(not [f for f in validate.run_mirror(ws, project) if f["severity"] == "error"], "mirror invalid after rename")
    res = mirror.push(ws, project)
    check(res["result"] == "PUSHED" and not any(f["severity"] == "error" for f in res.get("findings", [])),
          f"push after rename: {res.get('result')} {res.get('findings')}")

    # ---------------------------------------------------------------- move measure (with a qualified reference)
    sp = os.path.join(ws.mirror, "Parity.SemanticModel", "definition", "tables", "Sales.tmdl")
    text = open(sp, encoding="utf-8", newline="").read()
    text = text.replace("measure 'Base Unused' = [Total Sales] * 2", "measure 'Base Unused' = Sales[Ship Sales] * 2")
    open(sp, "w", encoding="utf-8", newline="").write(text)
    r = move.move_measure(ws, project, "Ship Sales", "Client Accounts")
    mt = mirror.mirror_texts(ws, project)
    sales = mt["Parity.SemanticModel/definition/tables/Sales.tmdl"]
    client = mt["Parity.SemanticModel/definition/tables/Client Accounts.tmdl"]
    check("measure 'Ship Sales'" not in sales and "measure 'Ship Sales'" in client, "measure block not moved")
    check("'Client Accounts'[Ship Sales] * 2" in sales, "qualified measure reference not updated")
    bar = json.loads(mt["Parity.Report/definition/pages/p1/visuals/v_bar/visual.json"])
    y = bar["visual"]["query"]["queryState"]["Y"]["projections"][0]["field"]["Measure"]["Expression"]["SourceRef"]["Entity"]
    check(y == "Client Accounts", f"visual entity after move: {y}")
    g = usage.build(ws, project)
    check(g.state.get(usage.key("measure", "Client Accounts", "Ship Sales")) == "used", "moved measure lost its usage")
    res = mirror.push(ws, project)
    check(res["result"] == "PUSHED" and not any(f["severity"] == "error" for f in res.get("findings", [])),
          f"push after move: {res.get('result')} {res.get('findings')}")
    for bad, code in ((("Nope", "Sales"), "NOT_FOUND"), (("Total Sales", "Sales"), "BAD_REQUEST"),
                      (("Total Sales", "Missing"), "NOT_FOUND")):
        try:
            move.move_measure(ws, project, *bad)
            check(False, f"move {bad} should fail")
        except PbzError as e:
            check(e.code == code, f"move {bad}: {e.code}")
    try:
        rename.rename(ws, project, "table", "Sales", "Orphan")
        check(False, "rename onto an existing table should fail")
    except PbzError as e:
        check(e.code == "NAME_TAKEN", f"rename collision: {e.code}")

    # ---------------------------------------------------------------- layout map and lint
    lay = layout.run(ws, project, "Overview")
    page = lay["pages"][0]
    check(page["map"][0].startswith("+") and len(page["map"]) == layout.ROWS + 2, "layout map shape")
    check(any(f["code"] == "OVERLAP" for f in lay["findings"]), "overlap not detected")
    vp = os.path.join(ws.mirror, "Parity.Report", "definition", "pages", "p1", "visuals", "v_card", "visual.json")
    v = json.load(open(vp, encoding="utf-8"))
    v["position"].update(x=1250, width=200)
    json.dump(v, open(vp, "w", encoding="utf-8"), indent=2)
    lay = layout.run(ws, project, "p1", show_map=False)
    check(any(f["code"] == "OFF_CANVAS" and f["visual"] == "v_card" for f in lay["findings"]), "off-canvas not detected")
    check("map" not in lay["pages"][0], "--no-map still drew the map")

    # ---------------------------------------------------------------- bpa --fix (property-only fixes)
    from pbz import bpa, fix
    fired = {f["rule"] for f in bpa.run(ws, project, "model")["findings"]}
    want = sorted(set(fix.FIXABLE) & fired)
    check({"HIDE_FOREIGN_KEYS", "NUMERIC_COLUMN_SUMMARIZE_BY"} <= set(want), f"fixable rules not firing: {want}")
    before = mirror.mirror_texts(ws, project)
    dry = fix.run(ws, project, want, dry_run=True)
    check(dry["result"] == "DRY_RUN" and dry["fixed"] and mirror.mirror_texts(ws, project) == before, "fix dry run wrote files")
    res = fix.run(ws, project, want)
    check(res["result"] == "FIXED" and len(res["fixed"]) == len(dry["fixed"]), f"fix: {res.get('result')}")
    again = {f["rule"] for f in bpa.run(ws, project, "model")["findings"]}
    check(not ({"HIDE_FOREIGN_KEYS", "NUMERIC_COLUMN_SUMMARIZE_BY"} & again), "rules still fire after the fix")
    check(fix.run(ws, project, want)["result"] == "NOTHING_TO_FIX", "fix is not idempotent")
    try:
        fix.run(ws, project, ["USE_THE_DIVIDE_FUNCTION_FOR_DIVISION"])
        check(False, "a DAX rewrite rule must not be auto-fixable")
    except PbzError as e:
        check(e.code == "UNSUPPORTED", f"unfixable rule: {e.code}")
    res = mirror.push(ws, project)
    check(res["result"] == "PUSHED" and not any(f["severity"] == "error" for f in res.get("findings", [])),
          f"push after fix: {res.get('result')} {res.get('findings')}")

    shutil.rmtree(tmp, ignore_errors=True)
    for f in fails:
        print("FAIL", f)
    print("ok" if not fails else f"{len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
