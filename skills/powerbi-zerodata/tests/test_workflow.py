"""Review and change workflow on the synthetic parity project: revision-bound evidence and the review
packet (release classes, impact, STALE evidence, human-only acceptance), recipes (transactional
batches with rollback, expectations, limits, parameters, refusals) and the pending Power Query guard.

Run: python tests/test_workflow.py
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
from pbz import cli, common, evidence, mirror, recipes, tmdl, validate  # noqa: E402
from pbz.common import PbzError  # noqa: E402


def main() -> int:
    fails: list[str] = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    tmp = tempfile.mkdtemp(prefix="pbzflow-")
    root = os.path.join(tmp, "ws")
    test_parity.build(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    mirror.pull(ws, project)
    validate.report_author = lambda *a, **k: []
    orig_find = common.find_workspace

    def find(start=None):  # CLI calls in this process use the temp private store
        w = orig_find(start)
        w.private = ws.private
        return w
    cli.find_workspace = find

    def pbz(*args):
        rc, out = recipes._call(ws, project, args[0], list(args[1:]))
        return out

    # ---------------------------------------------------------------- evidence and review
    pbz("validate")
    r = evidence.review(ws, project)
    ev = {e["stage"]: e["status"] for e in r["evidence"]}
    check(not r["pending_edits"] and ev["PARSED"] == "CURRENT" and ev["BOUND"] == "NOT_RUN", f"clean review: {ev}")
    pbz("set", "[Total Sales]", "formatString=#,0.0")
    pbz("set", "[Sales Copy]", "description=Copy for testing")
    sales = tmdl.load_model(os.path.join(project.mirror_model, "definition")).tables["Sales"]
    path = os.path.join(project.mirror_model, "definition", *sales.file.split("/"))
    text = open(path, encoding="utf-8").read().replace("SUM(Sales[Amount])", "SUM(Sales[Amount]) * 1", 1)
    open(path, "w", encoding="utf-8", newline="").write(text)
    pbz("add", "page", "Flow Lab")
    r = evidence.review(ws, project)
    classes = r["classes"]
    ev = {e["stage"]: e["status"] for e in r["evidence"]}
    check(r["pending_edits"] and classes.get("METADATA_ONLY") and classes.get("REPORT_ONLY"), f"classes: {classes}")
    check(ev["PARSED"] == "STALE", f"evidence from before the edits must be STALE: {ev}")
    check(os.path.exists(os.path.join(ws.mirror, "REVIEW.md")) and "Changes by release class" in
          open(os.path.join(ws.mirror, "REVIEW.md"), encoding="utf-8").read(), "REVIEW.md")
    logic = [c for c in r["changes"] if c["class"] == "LOGIC"]
    if "SUM(Sales[Amount]) * 1" in text:
        check(logic and any("compiles" in t for t in r["suggested_tests"]), f"logic change and suggested tests: {r['changes']}")
    pbz("validate")
    res = pbz("push")
    check(res.get("result") == "PUSHED", f"push: {res.get('result')}")
    r = evidence.review(ws, project)
    ev = {e["stage"]: e["status"] for e in r["evidence"]}
    check(not r["pending_edits"] and ev["BOUND"] == "CURRENT", f"after push: {ev}")
    try:
        evidence.review(ws, project, accept=True)
        check(False, "an agent accepted a review")
    except PbzError as e:
        check(e.code == "HUMAN_ONLY", f"accept: {e.code}")
    log = open(os.path.join(ws.pbz_dir, evidence.LOG), encoding="utf-8").read()
    check('"stage": "PARSED"' in log and '"stage": "BOUND"' in log, "evidence log")

    # ---------------------------------------------------------------- recipes
    good = {"recipe": "folders", "version": 1, "params": {"table": "Sales"},
            "steps": [{"verb": "set", "args": ["--table", "{table}", "--kind", "measure", "--match", "Unused", "displayFolder=Cleanup"],
                       "expect": {"result": "CHANGED"}},
                      {"verb": "hierarchy", "args": ["add", "Customer", "Geo", "--level", "Segment", "--level", "Name"]},
                      {"verb": "hierarchy", "args": ["add", "Customer", "Geo", "--level", "Segment"],
                       "on_error": {"NAME_TAKEN": "skip"}}],
            "limits": {"max_files": 10}}
    out = recipes.run(ws, project, json.dumps(good))
    check(out["result"] == "APPLIED" and out["steps"][2].get("skipped"), f"recipe applied: {out}")
    check("Geo" in tmdl.load_model(os.path.join(project.mirror_model, "definition")).tables["Customer"].hierarchies,
          "recipe effect")
    snap = recipes._snapshot(ws, project)
    bad = {"recipe": "breaks", "steps": [{"verb": "set", "args": ["[Total Sales]", "displayFolder=Temp"]},
                                         {"verb": "rename", "args": ["measure", "Nope", "Nope2"]}]}
    out = recipes.run(ws, project, json.dumps(bad))
    check(out["result"] == "ROLLED_BACK" and recipes._snapshot(ws, project) == snap, f"rollback on a failed step: {out}")
    exp = {"recipe": "expects", "steps": [{"verb": "set", "args": ["[Total Sales]", "displayFolder=Temp2"],
                                           "expect": {"result": "NOTHING_TO_CHANGE"}}]}
    out = recipes.run(ws, project, json.dumps(exp))
    check(out["result"] == "ROLLED_BACK" and recipes._snapshot(ws, project) == snap, f"rollback on expectation: {out}")
    lim = {"recipe": "limits", "steps": [{"verb": "add", "args": ["page", "One"]}, {"verb": "add", "args": ["page", "Two"]}],
           "limits": {"max_files": 2}}
    out = recipes.run(ws, project, json.dumps(lim))
    check(out["result"] == "ROLLED_BACK" and recipes._snapshot(ws, project) == snap, f"rollback on limit: {out}")
    out = recipes.run(ws, project, json.dumps({"recipe": "dry", "steps": [{"verb": "add", "args": ["page", "Dry"]}]}), dry_run=True)
    check(out["result"] == "DRY_RUN" and recipes._snapshot(ws, project) == snap, f"dry run: {out}")
    for spec, why in (({"steps": [{"verb": "push", "args": []}]}, "push"),
                      ({"steps": [{"verb": "set", "args": ["--workspace", "x"]}]}, "workspace arg"),
                      ({"steps": [{"verb": "set", "args": ["{nope}", "isHidden=true"]}]}, "missing parameter"),
                      ({"steps": []}, "empty")):
        try:
            recipes.run(ws, project, json.dumps(spec))
            check(False, f"recipe with {why} accepted")
        except PbzError as e:
            check(e.code == "BAD_REQUEST", f"{why}: {e.code}")
    check(recipes._snapshot(ws, project) == snap, "refused recipes changed the mirror")

    # ---------------------------------------------------------------- pending Power Query edits
    pbz("set", "[Total Sales]", "displayFolder=Guarded")
    pq = os.path.join(project.model_dir, ".pbi")
    os.makedirs(pq, exist_ok=True)
    open(os.path.join(pq, "unappliedChanges.json"), "w").write("{}")
    res = mirror.push(ws, project)
    check(res["result"] == "PENDING_PQ_CHANGES", f"push with unapplied Power Query edits: {res.get('result')}")
    os.remove(os.path.join(pq, "unappliedChanges.json"))
    check(mirror.push(ws, project)["result"] == "PUSHED", "push after the user resolved them")

    if fails:
        print("\n".join("FAIL " + f for f in fails))
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
