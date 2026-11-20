"""Copying report pages and visuals (`pbz copy page|visual`) on the synthetic parity project: new page,
visual, group, binding, parameter and filter names, remapped interactions and group parents, links to
the copied page retargeted, sync groups kept or made independent, drillthrough and tooltip pages,
group copies across pages with consistent offsets, the integrity checks, a push, and Microsoft's
report validator on a copy of the Desktop-made fixture.

Run: python tests/test_components.py
"""
from __future__ import annotations

import json
import io
import hashlib
from contextlib import redirect_stdout
import os
import shutil
import sys
import tempfile
import types
import subprocess
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import author, common, components, filters, mirror, pbircheck, usage, validate  # noqa: E402
from pbz.common import PbzError  # noqa: E402


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(path, doc):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)


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

    tmp = tempfile.mkdtemp(prefix="pbzcomp-")
    root = os.path.join(tmp, "ws")
    test_parity.build(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    mirror.pull(ws, project)
    real_author = validate.report_author
    pdir = os.path.join(project.mirror_report, "definition", "pages")
    vpath = lambda pid, vid: os.path.join(pdir, pid, "visuals", vid, "visual.json")  # noqa: E731

    # ---------------------------------------------------------------- enrich the source page
    group = {"$schema": load(vpath("p1", "v_card"))["$schema"], "name": "g_kpis",
             "position": {"x": 600, "y": 400, "z": 5000, "width": 400, "height": 150, "tabOrder": 5000},
             "visualGroup": {"displayName": "KPIs", "groupMode": "ScaleMode"}}
    os.makedirs(os.path.dirname(vpath("p1", "g_kpis")))
    save(vpath("p1", "g_kpis"), group)
    for vid, x in (("v_card", 610), ("v_rm", 810)):
        d = load(vpath("p1", vid))
        d["parentGroupName"] = "g_kpis"
        d["position"].update(x=x, y=410)
        save(vpath("p1", vid), d)
    d = load(vpath("p1", "v_slicer"))
    d["visual"]["syncGroup"] = {"groupName": "period", "fieldChanges": True, "filterChanges": True}
    save(vpath("p1", "v_slicer"), d)
    filters.interaction(ws, project, "Overview", "v_bar", "v_param", "highlight")
    filters.add(ws, project, "[Total Sales]", page="Overview", visual="v_param", where=["> 0"])
    author.link(ws, project, "Overview", "v_btn", to_page="Overview")  # a self-reference
    dt = author.add_page(ws, project, "Detail", drillthrough=["'Customer'[Segment]"])
    check(not [f for f in pbircheck.run(project.mirror_report) if f["severity"] == "error"], "enriched fixture fails checks")

    # ---------------------------------------------------------------- copy a page
    before_names = filters._all_filter_names(project)
    r = components.copy_page(ws, project, "Overview", "Overview Copy")
    new = r["id"]
    meta = load(os.path.join(pdir, "pages.json"))
    check(meta["pageOrder"][meta["pageOrder"].index("p1") + 1] == new, f"copy not inserted after the source: {meta['pageOrder']}")
    src_v = sorted(os.listdir(os.path.join(pdir, "p1", "visuals")))
    dst_v = sorted(os.listdir(os.path.join(pdir, new, "visuals")))
    check(len(src_v) == len(dst_v) == r["visuals"] and not set(src_v) & set(dst_v), f"visual names: {src_v} {dst_v}")
    cp = load(os.path.join(pdir, new, "page.json"))
    check(cp["name"] == new and cp["displayName"] == "Overview Copy", "page identity")
    check({f["name"] for f in cp["filterConfig"]["filters"]}.isdisjoint(before_names), "page filters kept their names")
    names = []
    for root_, _d, fs in os.walk(os.path.join(project.mirror_report, "definition")):
        for f in fs:
            if f in ("page.json", "visual.json", "report.json"):
                names += [x["name"] for x in (load(os.path.join(root_, f)).get("filterConfig") or {}).get("filters", [])]
    check(len(names) == len(set(names)), f"filter names not unique across the report: {names}")
    copies = {load(os.path.join(pdir, new, "visuals", v, "visual.json")).get("visual", {}).get("visualType", "group"): v
              for v in dst_v}
    gid = next(v for v in dst_v if "visualGroup" in load(os.path.join(pdir, new, "visuals", v, "visual.json")))
    kids = [v for v in dst_v if load(os.path.join(pdir, new, "visuals", v, "visual.json")).get("parentGroupName") == gid]
    check(len(kids) == 2, f"group children not re-parented: {kids}")
    inter = cp.get("visualInteractions") or []
    check(len(inter) == 1 and inter[0]["source"] in dst_v and inter[0]["target"] in dst_v
          and inter[0]["type"] == "HighlightFilter", f"interactions: {inter}")
    btn = load(os.path.join(pdir, new, "visuals", copies["actionButton"], "visual.json"))
    nav = btn["visual"]["visualContainerObjects"]["visualLink"][0]["properties"]["navigationSection"]["expr"]["Literal"]["Value"]
    check(nav == f"'{new}'" and r.get("links_to_copy") == 1, f"self link not retargeted: {nav} {r}")
    orig = load(vpath("p1", "v_btn"))["visual"]["visualContainerObjects"]["visualLink"][0]["properties"]["navigationSection"]
    check(orig["expr"]["Literal"]["Value"] == "'p1'", "the original's link changed")
    sl = load(os.path.join(pdir, new, "visuals", copies["slicer"], "visual.json"))
    check(sl["visual"]["syncGroup"]["groupName"] == "period" and "kept" in r.get("sync_groups", ""), f"sync group kept: {r}")
    tb = load(os.path.join(pdir, new, "visuals", copies["tableEx"], "visual.json"))
    check(tb["filterConfig"]["filters"][0]["name"] not in before_names, "visual filter kept its name")
    check(any("bookmarks are not copied" in n for n in r["notes"]), f"notes: {r}")
    r2 = components.copy_page(ws, project, "Overview", "Overview Solo", independent_slicers=True, after="Detail")
    sl2 = next(load(os.path.join(pdir, r2["id"], "visuals", v, "visual.json")) for v in os.listdir(os.path.join(pdir, r2["id"], "visuals"))
               if load(os.path.join(pdir, r2["id"], "visuals", v, "visual.json")).get("visual", {}).get("visualType") == "slicer")
    meta = load(os.path.join(pdir, "pages.json"))
    check(sl2["visual"]["syncGroup"]["groupName"] != "period" and meta["pageOrder"][meta["pageOrder"].index(dt["id"]) + 1] == r2["id"],
          f"independent slicers / after: {sl2['visual']['syncGroup']}")

    # ---------------------------------------------------------------- drillthrough and tooltip pages
    r3 = components.copy_page(ws, project, "Detail", "Detail Copy")
    src_pj, dst_pj = load(os.path.join(pdir, dt["id"], "page.json")), load(os.path.join(pdir, r3["id"], "page.json"))
    par, flt = dst_pj["pageBinding"]["parameters"][0], dst_pj["filterConfig"]["filters"][0]
    check(dst_pj["pageBinding"]["name"] != src_pj["pageBinding"]["name"] and par["name"] != src_pj["pageBinding"]["parameters"][0]["name"]
          and par["boundFilter"] == flt["name"] != src_pj["filterConfig"]["filters"][0]["name"]
          and flt["howCreated"] == "Drillthrough" and r3.get("binding") == "Drillthrough", f"drillthrough copy: {dst_pj}")
    r4 = components.copy_page(ws, project, "Tip", "Tip Copy")
    tp = load(os.path.join(pdir, r4["id"], "page.json"))
    check(tp["pageBinding"]["type"] == "Tooltip" and tp["pageBinding"]["name"] != "tt", f"tooltip copy: {tp}")
    refused(components.copy_page, ws, project, "Overview", "Detail", code="NAME_TAKEN")
    refused(components.copy_page, ws, project, "Nowhere", "X", code="NOT_FOUND")
    refused(components.copy_page, ws, project, "Overview", "Bad «lit#1»", code="BAD_REQUEST")

    # ---------------------------------------------------------------- copy visuals and groups
    v = components.copy_visual(ws, project, "Overview", "v_card")
    vc = load(vpath("p1", v["id"]))
    check("parentGroupName" not in vc and v["placement"] == "free_spot" and v.get("note"), f"grouped visual copied top-level: {v}")
    g = components.copy_visual(ws, project, "Overview", "g_kpis", to_page="Old Page")
    gdir = os.path.join(pdir, "p2", "visuals")
    gdoc = load(os.path.join(gdir, g["id"], "visual.json"))
    ch = [load(os.path.join(gdir, x, "visual.json")) for x in os.listdir(gdir)
          if load(os.path.join(gdir, x, "visual.json")).get("parentGroupName") == g["id"]]
    offs = sorted((c["position"]["x"] - gdoc["position"]["x"], c["position"]["y"] - gdoc["position"]["y"]) for c in ch)
    check(g["copied"] == 3 and len(ch) == 2 and offs == [(10.0, 10.0), (210.0, 10.0)], f"group copy offsets: {g} {offs}")
    p = components.copy_visual(ws, project, "Overview", "v_bar", pos="20,500,300,200")
    check(p["box"] == [20, 500, 300, 200] and p["placement"] == "given", f"copy at position: {p}")
    refused(components.copy_visual, ws, project, "Overview", "zz", code="NOT_FOUND")
    refused(components.copy_visual, ws, project, "Overview", "v_bar", pos="1,2,3", code="BAD_REQUEST")
    refused(components.copy_visual, ws, project, "Overview", "g_kpis", pos="1,2,3,4", code="BAD_REQUEST")

    errs = [f for f in pbircheck.run(project.mirror_report) if f["severity"] == "error"]
    check(not errs, f"copies fail the integrity checks: {errs}")
    check(not [f for f in validate.run_mirror(ws, project) if f["severity"] == "error"], "copies fail mirror validation")
    gr = usage.build(ws, project)
    check(len([u for us in gr.usages.values() for u in us if u.page == "Overview Copy"]) > 0, "usage graph does not see the copy")

    def project_snapshot():
        snapshot = {}
        for directory in (project.report_dir, project.model_dir):
            for current, _dirs, files in os.walk(directory):
                for name in files:
                    path = os.path.join(current, name)
                    with open(path, "rb") as f:
                        snapshot[os.path.relpath(path, ws.root)] = hashlib.sha256(f.read()).hexdigest()
        return snapshot

    before_push = project_snapshot()
    res = mirror.push(ws, project)
    perr = [f for f in res.get("findings", []) if f["severity"] == "error"]
    if os.path.exists(validate.REPORT_AUTHOR):
        check(res["result"] == "INVALID", f"Parity push should be invalid: {res.get('result')}")
        check(bool(perr) and all(f.get("code") == "PBIR" for f in perr), f"Parity push requires vendor errors: {perr}")
        check(project_snapshot() == before_push, "invalid Parity push changed the real project")
    else:
        check(res["result"] == "PUSHED" and not perr, f"push: {res.get('result')} {perr}")

    # ---------------------------------------------------------------- Microsoft's validator on the Desktop-made fixture
    validate.report_author = real_author
    fxroot = os.path.join(tmp, "fxroot")
    st = os.path.join(fxroot, "pbz-workspace")
    shutil.copytree(os.path.join(SKILL, "tests", "fixture"), st)
    fx = types.SimpleNamespace(name="Canary", mirror_report=os.path.join(st, "Canary.Report"),
                               mirror_model=os.path.join(st, "Canary.SemanticModel"))
    fws = types.SimpleNamespace(root=fxroot, mirror=st)
    base = validate.report_author(st, fx, lambda s: s)
    required = "--require-validator" in sys.argv
    available = validate.validator_ran(base)
    check(validator_gate(base, required), f"required validator cannot run: {base}")
    fdir = os.path.join(fx.mirror_report, "definition", "pages")
    src_pid = next(p for p in sorted(os.listdir(fdir)) if os.path.isdir(os.path.join(fdir, p)))
    fvis = sorted(os.listdir(os.path.join(fdir, src_pid, "visuals")))
    components.copy_page(fws, fx, "Canary Page", "Canary Copy")
    dtp = author.add_page(fws, fx, "Canary Detail", drillthrough=["'Project'[Status]"])
    check(dtp.get("back_button"), "fixture drillthrough page")
    components.copy_page(fws, fx, "Canary Detail", "Canary Detail Copy")
    components.copy_visual(fws, fx, "Canary Page", fvis[0], to_page="Canary Copy")
    # Copy ids need no mapping: this lane requires a clean baseline. Any finding
    # on any source or copied object is a regression, including duplicate findings.
    meaningful = lambda fs: [f for f in fs if f.get("code") not in {"REPORT_SCHEMA_PASSED", "REPORT_SCHEMA_NOT_RUN"}]
    if validate.validator_attempted(base):
        check(not meaningful(base), f"validator baseline must be clean: {base}")
    if available:
        after = validate.report_author(st, fx, lambda s: s)
        check(validate.validator_ran(after), f"validator cannot run after copy: {after}")
        added = validate.diagnostic_counts(meaningful(after)) - validate.diagnostic_counts(meaningful(base))
        check(not added, f"Microsoft's validator rejects the copies: {added}")
        control_path = os.path.join(fdir, src_pid, "page.json")
        control = load(control_path)
        control.pop("displayName")
        save(control_path, control)
        invalid = validate.report_author(st, fx, lambda s: s, schema_only=True)
        check(validator_gate(invalid, required), f"required control validator cannot run: {invalid}")
        check(validate.invalid_control_rejected(invalid, src_pid), f"invalid control missing required displayName diagnostic: {invalid}")
    else:
        print("note: live vendor validation unavailable (required lane fails closed)")

    # Independent real workspace: prove a valid Canary edit survives a staged push.
    canary_root = os.path.join(tmp, "canary-push")
    shutil.copytree(os.path.join(SKILL, "tests", "fixture"), canary_root)
    ws2 = common.Workspace(canary_root)
    ws2.private = os.path.join(tmp, "canary-private")
    cproject = ws2.project()
    mirror.pull(ws2, cproject)
    canary_base = validate.report_author(ws2.mirror, cproject, lambda s: s)
    canary_available = validate.validator_ran(canary_base)
    if required:
        check(validator_gate(canary_base, True), f"required Canary push validator cannot run: {canary_base}")
    canary_pages = os.path.join(cproject.mirror_report, "definition", "pages")
    canary_pid = next(p for p in sorted(os.listdir(canary_pages)) if os.path.isdir(os.path.join(canary_pages, p)))
    canary_page_path = os.path.join(canary_pages, canary_pid, "page.json")
    canary_page = load(canary_page_path)
    canary_name = "Canary Page Renamed"
    canary_page["displayName"] = canary_name
    save(canary_page_path, canary_page)
    staged_findings = []
    real_run_stage = validate.run_stage

    def capture_run_stage(*args, **kwargs):
        findings = real_run_stage(*args, **kwargs)
        staged_findings.extend(findings)
        return findings

    with patch.object(validate, "run_stage", side_effect=capture_run_stage):
        res = mirror.push(ws2, cproject)
    check(res["result"] == "PUSHED", f"Canary push: {res}")
    real_canary_page = os.path.join(cproject.report_dir, "definition", "pages", canary_pid, "page.json")
    check(load(real_canary_page)["displayName"] == canary_name, "Canary push did not update the real page")
    if canary_available:
        check(any(f.get("code") == "REPORT_SCHEMA_PASSED" for f in staged_findings),
              f"Canary push did not run schema validation: {staged_findings}")
    comparison_checks(check)

    if fails:
        print("\n".join("FAIL " + f for f in fails))
        return 1
    print("ok")
    return 0


def validator_gate(findings, required):
    return not required or (validate.validator_attempted(findings)
                            and not any(f.get("code") == "REPORT_SCHEMA_NOT_RUN" for f in findings))


def comparison_checks(check):
    finding = {"severity": "error", "code": "SCHEMA", "where": "a/page.json", "message": "x" * 201}
    check(bool(validate.diagnostic_counts([finding, finding]) - validate.diagnostic_counts([finding])),
          "duplicate diagnostic disappeared")
    for sibling in ({**finding, "where": "b/page.json"}, {**finding, "message": "x" * 200 + "different"}):
        check(len(validate.diagnostic_counts([finding, sibling])) == 2, "full diagnostics collided")
    expected = {"severity": "error", "code": "PBIR", "where": "definition/pages/control/page.json",
                "message": "must have required property 'displayName'"}
    check(validate.invalid_control_rejected([expected], "control"), "expected control diagnostic rejected")
    A = {"severity": "error", "code": "PBIR", "where": "C:\\Users\\RUNNER~1\\AppData\\Local\\Temp\\pbzcomp-r7w0dit7\\fxroot\\pbz-workspace\\Canary.Report\\definition\\pages\\530d248092b44ccf9450\\page.json", "message": "page.json missing \"displayName\": C:\\Users\\RUNNER~1\\AppData\\Local\\Temp\\pbzcomp-r7w0dit7\\fxroot\\pbz-workspace\\Canary.Report\\definition\\pages\\530d248092b44ccf9450\\page.json"}
    B = {"severity": "error", "code": "PBIR", "where": "/", "message": "Schema validation: / must have required property 'displayName': C:\\Users\\RUNNER~1\\AppData\\Local\\Temp\\pbzcomp-r7w0dit7\\fxroot\\pbz-workspace\\Canary.Report\\definition\\pages\\530d248092b44ccf9450\\page.json"}
    check(validate.invalid_control_rejected([A, B], "530d248092b44ccf9450"), "split vendor control diagnostics rejected")
    check(not validate.invalid_control_rejected([A], "530d248092b44ccf9450"), "path diagnostic alone accepted as control")
    check(not validate.invalid_control_rejected([B], "530d248092b44ccf9450"), "required property diagnostic alone accepted as control")
    check(validate.invalid_control_rejected([{**expected, "where": expected["where"].replace("/", "\\")}], "control"),
          "Windows control path rejected")
    check(not validate.invalid_control_rejected([{**expected, "where": expected["where"] + ".bak"}], "control"),
          "backup accepted as exact control file")
    check(validate.validator_attempted([expected]), "dirty validator baseline treated as unavailable")
    check(validator_gate([expected], True), "schema diagnostic failed availability gate")
    check(not validate.validator_attempted([]), "empty validator findings counted as attempted")
    check(not validate.invalid_control_rejected([{**expected, "message": "unrelated failure"}], "control"),
          "unrelated diagnostic accepted as control")
    for code in ("REPORT_VALIDATOR_MISSING", "REPORT_VALIDATOR_FAILED", "REPORT_VALIDATOR_RESPONSE"):
        findings = [{"severity": "error", "code": code}]
        check(not validate.validator_ran(findings), f"{code} counted as a run")
        check(not validate.validator_attempted(findings), f"{code} counted as attempted")
        check(not validate.invalid_control_rejected(findings + [expected], "control"), "crash accepted as invalid control")
    check(not validate.invalid_control_rejected([expected], "other"), "another page accepted as control")
    check(not validator_gate([], True), "empty findings passed required gate")
    for code in ("REPORT_VALIDATOR_MISSING", "REPORT_VALIDATOR_FAILED", "REPORT_SCHEMA_NOT_RUN"):
        check(not validator_gate([{"code": code}], True), f"required gate accepted {code}")
        check(validator_gate([{"code": code}], False), "optional gate failed")
    check(validator_gate([{"code": "REPORT_SCHEMA_PASSED"}], True), "schema-passed gate failed")
    check(not validator_gate([{"code": "REPORT_SCHEMA_PASSED"}, {"code": "REPORT_VALIDATOR_FAILED"}], True),
          "failure overrode gate incorrectly")
    project = types.SimpleNamespace(name="Canary")
    with patch.object(validate.os.path, "isdir", return_value=True), patch.object(validate.os.path, "exists", return_value=False):
        check(not validate.validator_ran(validate.report_author("unused", project, lambda s: s)), "missing validator passed")
        with patch.object(validate, "REPORT_AUTHOR", "missing-validator-binary"):
            findings = validate.report_author("unused", project, lambda s: s, schema_only=True)
        check([f["code"] for f in findings] == ["REPORT_VALIDATOR_FAILED"], "schema-only missing validator did not fail")
    for error in (OSError("crash"), subprocess.TimeoutExpired("validator", 600)):
        with patch.object(validate.os.path, "isdir", return_value=True), \
                patch.object(validate.os.path, "exists", return_value=True), \
                patch.object(validate.subprocess, "run", side_effect=error) as run:
            findings = validate.report_author("unused", project, lambda s: s)
            check(not validate.validator_ran(findings), "crash or timeout passed")
            check(run.call_count == 2, "both validator attempts must run")
            check([f["code"] for f in findings] == ["REPORT_VALIDATOR_FAILED"], "wrong crash finding")
    offline = types.SimpleNamespace(stdout=b'{"data":{"result":"succeeded"}}', returncode=0)
    errors = (OSError("crash"), subprocess.TimeoutExpired("validator", 600),
              types.SimpleNamespace(stdout=b'{"error":"schema unavailable"}', returncode=1),
              types.SimpleNamespace(stdout=b'not json', returncode=1))
    for error in errors:
        for schema_only in (False, True):
            with patch.object(validate.os.path, "isdir", return_value=True), \
                    patch.object(validate, "REPORT_AUTHOR", "validator"), \
                    patch.object(validate.os.path, "exists", return_value=True), \
                    patch.object(validate.subprocess, "run", side_effect=[error, offline]) as run:
                findings = validate.report_author("unused", project, lambda s: s, schema_only=schema_only)
                expected_code = "REPORT_VALIDATOR_FAILED" if schema_only else "REPORT_SCHEMA_NOT_RUN"
                check([f["code"] for f in findings] == [expected_code], "wrong schema-only/fallback findings")
                check(run.call_count == (1 if schema_only else 2), "wrong schema-only/fallback attempts")

    # Exercise the live lane: optional availability must not skip control rejection
    # once a clean baseline and copy have run.
    def report_author(_root, _project, _redact, **kwargs):
        return [{"code": "REPORT_VALIDATOR_FAILED", "severity": "error"}] if kwargs.get("schema_only") else [
            {"code": "REPORT_SCHEMA_PASSED", "severity": "info"}]

    output = io.StringIO()
    with tempfile.TemporaryDirectory(prefix="pbzcomp-scenario-") as scenario, \
            patch.object(tempfile, "mkdtemp", return_value=scenario), \
            patch.object(validate, "report_author", side_effect=report_author) as runner, \
            patch.object(sys.modules[__name__], "comparison_checks"), \
            patch.object(sys, "argv", [sys.argv[0]]), redirect_stdout(output):
        result = main()
    check(result == 1 and "FAIL invalid control missing required displayName diagnostic:" in output.getvalue(),
          "optional failed control escaped the live lane rejection assertion")
    check(any(call.kwargs.get("schema_only") for call in runner.call_args_list), "live lane never ran schema-only control")


if __name__ == "__main__":
    if "--comparison-only" in sys.argv:
        failures = []
        comparison_checks(lambda ok, msg: failures.append(msg) if not ok else None)
        for failure in failures:
            print("FAIL", failure)
        print("ok (validator comparison fixtures)" if not failures else f"{len(failures)} failure(s)")
        raise SystemExit(bool(failures))
    raise SystemExit(main())
