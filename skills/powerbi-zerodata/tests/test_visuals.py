"""Visual catalog on the synthetic parity project: every built-in type from Microsoft's core visual
capabilities (roles, kinds, mapping conditions), and custom visuals: .pbiviz contracts, AppSource,
organizational and private registration (the package travels with `pbz push`), registration and role
checks, untrusted zip handling, and package code never reaching the mirror or any output.

Run: python tests/test_visuals.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import zipfile

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import author, common, mirror, pbircheck, validate, visuals  # noqa: E402
from pbz.common import PbzError  # noqa: E402

GUID = "pbzTestVisual1234567890ABCDEF"
CANARY_JS = "PBZ_CANARY_VISUAL_CODE"


def make_pbiviz(path: str, guid: str = GUID, evil: bool = False) -> None:
    payload = {"visual": {"guid": guid, "name": "pbzTestVisual", "displayName": "PBZ Test Visual", "version": "1.2.3"},
               "apiVersion": "5.11.0",
               "capabilities": {"dataRoles": [{"name": "category", "kind": "Grouping", "displayName": "Category"},
                                              {"name": "measure", "kind": "Measure", "displayName": "Values"}],
                                "dataViewMappings": [{"conditions": [{"category": {"max": 1}, "measure": {"max": 2}}],
                                                      "categorical": {}}],
                                "objects": {"colors": {"properties": {"fill": {"type": {"fill": {"solid": {"color": True}}}}}}}},
               "content": {"js": f"var x = '{CANARY_JS}';", "css": "", "iconBase64": ""}}
    manifest = {"visual": payload["visual"], "resources": [{"resourceId": "rId0", "sourceType": 5,
                                                            "file": f"resources/{guid}.pbiviz.json"}],
                "version": "1.2.3"}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("package.json", json.dumps(manifest))
        z.writestr(f"resources/{guid}.pbiviz.json", json.dumps(payload))
        if evil:
            z.writestr("../escape.txt", "x")


def main() -> int:
    fails: list[str] = []
    outputs: list = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    def keep(res):
        outputs.append(res)
        return res

    def refused(fn, *args, code, **kw):
        try:
            keep(fn(*args, **kw))
            check(False, f"{fn.__name__} should fail with {code}")
        except PbzError as e:
            check(e.code == code, f"{fn.__name__}: {e.code} {e.detail} (want {code})")

    tmp = tempfile.mkdtemp(prefix="pbzvis-")
    root = os.path.join(tmp, "ws")
    test_parity.build(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    mirror.pull(ws, project)
    validate.report_author = lambda *a, **k: []  # schema validator needs a Desktop-made report; TOM still runs
    page = author.add_page(ws, project, "Visual Lab")["page"]
    rj = os.path.join(project.mirror_report, "definition", "report.json")

    # ---------------------------------------------------------------- built-in catalog
    core = visuals.core()
    check(len(core) >= 50 and {"kpi", "gauge", "cardVisual", "listSlicer", "matrix", "tableEx"} <= set(core),
          f"core catalog: {len(core)}")
    kpi = keep(visuals.roles(ws, project, "KPI"))
    check(kpi["visual_type"] == "kpi" and [r["name"] for r in kpi["roles"]] == ["Indicator", "TrendLine", "Goal"],
          f"kpi roles: {kpi}")
    v = keep(author.add_visual(ws, project, "kpi", page, ["[Total Sales]", "TrendLine='Date'[Year]"]))
    check(v["roles"] == {"Indicator": 1, "TrendLine": 1} and "custom" not in v, f"kpi: {v}")
    v = keep(author.add_visual(ws, project, "gauge", page, ["[Total Sales]", "TargetValue=[Ship Sales]"]))
    check(v["roles"] == {"Y": 1, "TargetValue": 1}, f"gauge: {v}")
    v = keep(author.add_visual(ws, project, "cardVisual", page, ["[Total Sales]"]))
    check(v["roles"] == {"Data": 1}, f"new card: {v}")
    refused(author.add_visual, ws, project, "card", page, ["[Total Sales]", "[Ship Sales]"], code="BAD_REQUEST")
    refused(author.add_visual, ws, project, "kpi", page, ["Nope=[Total Sales]"], code="BAD_REQUEST")
    refused(author.add_visual, ws, project, "listSlicer", page, ["[Total Sales]"], code="BAD_REQUEST")
    refused(author.add_visual, ws, project, "image", page, [], code="BAD_REQUEST")
    refused(author.add_visual, ws, project, "notAVisual", page, ["[Total Sales]"], code="BAD_REQUEST")
    check(not [f for f in pbircheck.run(project.mirror_report) if f["severity"] == "error"], "built-in visuals fail checks")

    # ---------------------------------------------------------------- .pbiviz contract and private import
    pv = os.path.join(tmp, "test.pbiviz")
    make_pbiviz(pv)
    imp = keep(visuals.import_pbiviz(ws, project, pv))
    check(imp["guid"] == GUID and imp["roles"] == ["category:grouping", "measure:measure"] and "registered" not in imp,
          f"contract import: {imp}")
    check(imp["conditions"] == ["category<=1 measure<=2"], f"conditions: {imp['conditions']}")
    refused(author.add_visual, ws, project, "PBZ Test Visual", page, ["[Total Sales]"], code="NOT_REGISTERED")
    before = json.load(open(rj, encoding="utf-8"))
    imp = keep(visuals.import_pbiviz(ws, project, pv, "private"))
    check(imp["registered"] == "private" and imp["report_json"] == "added", f"private import: {imp}")
    data = json.load(open(rj, encoding="utf-8"))
    new_pkg = {"name": GUID, "type": "CustomVisual",
               "items": [{"name": f"{GUID}.pbiviz.json", "path": f"{GUID}.pbiviz.json", "type": "CustomVisualMetadata"}]}
    check(data["resourcePackages"] == (before.get("resourcePackages") or []) + [new_pkg]
          and {k: v for k, v in data.items() if k != "resourcePackages"} == {k: v for k, v in before.items() if k != "resourcePackages"},
          f"report.json edit: {data.get('resourcePackages')}")
    mf = validate.run_mirror(ws, project)
    check(not any(f["code"] == "PBIR_PRIVATE_VISUAL_PACKAGE_MISSING" for f in mf), f"staged package not seen: {mf}")
    v = keep(author.add_visual(ws, project, "PBZ Test Visual", page, ["'Customer'[Segment]", "[Total Sales]", "[Ship Sales]"]))
    check(v["type"] == GUID and v["roles"] == {"category": 1, "measure": 2} and v["custom"]["roles_from"] == "contract",
          f"custom visual: {v}")
    vj = json.load(open(os.path.join(ws.mirror, v["files"][0]), encoding="utf-8"))
    check(set(vj["visual"]["query"]["queryState"]) == {"category", "measure"}, "queryState keys are not the internal role names")
    refused(author.add_visual, ws, project, GUID, page, ["'Customer'[Segment]", "'Customer'[Name]"], code="BAD_REQUEST")
    res = keep(mirror.push(ws, project))
    errs = [f for f in res.get("findings", []) if f["severity"] == "error"]
    check(res["result"] == "PUSHED" and not errs and res.get("custom_visuals_installed") == [GUID], f"push: {res.get('result')} {errs}")
    real_pkg = os.path.join(project.report_dir, "CustomVisuals", GUID)
    check(os.path.isfile(os.path.join(real_pkg, "package.json")) and
          os.path.isfile(os.path.join(real_pkg, "resources", f"{GUID}.pbiviz.json")), "package not installed in the report")
    check(not os.path.isdir(os.path.join(ws.private, visuals.PENDING, GUID)), "pending package not cleared")
    lst = keep(visuals.list_visuals(ws, project))
    me = next((c for c in lst["custom"] if c["visual_type"] == GUID), {})
    check(me.get("registered") == "private" and me.get("package") == "present" and me.get("instances") == 1, f"list: {me}")
    refused(visuals.import_pbiviz, ws, project, pv, "private", code="CONFLICT")
    evil = os.path.join(tmp, "evil.pbiviz")
    make_pbiviz(evil, guid="pbzEvilVisual99", evil=True)
    refused(visuals.import_pbiviz, ws, project, evil, code="UNSAFE_PACKAGE")

    # ---------------------------------------------------------------- AppSource, organizational, observed roles
    deneb = "deneb7E15AEF80B9E4D4F8E12924291ECE89A"
    r = keep(visuals.register(ws, project, deneb, "public", ["dataset:either"]))
    check(r["registered"] == "public" and r["report_json"] == "added", f"public register: {r}")
    check(deneb in json.load(open(rj, encoding="utf-8")).get("publicCustomVisuals", []), "publicCustomVisuals not written")
    v = keep(author.add_visual(ws, project, deneb, page, ["'Customer'[Segment]", "[Total Sales]"]))
    check(v["roles"] == {"dataset": 2} and "setup" in v["custom"], f"deneb: {v}")
    r = keep(visuals.register(ws, project, "orgVisualABC", "organizational", ["values:measure"]))
    check(r["visual_type"] == "orgVisualABC_OrgStore", f"org register: {r}")
    v = keep(author.add_visual(ws, project, "orgVisualABC_OrgStore", page, ["[Total Sales]"]))
    check(v["type"] == "orgVisualABC_OrgStore" and v["roles"] == {"values": 1}, f"org visual: {v}")
    keep(visuals.register(ws, project, "observedVis123", "public"))
    refused(author.add_visual, ws, project, "observedVis123", page, ["[Total Sales]"], code="NO_CONTRACT")
    seed = keep(author.add_visual(ws, project, "tableEx", page, ["[Total Sales]"]))
    sp = os.path.join(ws.mirror, seed["files"][0])
    sj = json.load(open(sp, encoding="utf-8"))
    sj["visual"]["visualType"] = "observedVis123"
    sj["visual"]["query"]["queryState"] = {"foo": sj["visual"]["query"]["queryState"]["Values"]}
    json.dump(sj, open(sp, "w", encoding="utf-8"), indent=2)
    v = keep(author.add_visual(ws, project, "observedVis123", page, ["foo=[Ship Sales]"]))
    check(v["custom"]["roles_from"] == "observed", f"observed roles: {v}")
    check(not [f for f in pbircheck.run(project.mirror_report) if f["severity"] == "error"], "registered custom visuals fail checks")
    pv2 = os.path.join(tmp, "second.pbiviz")
    make_pbiviz(pv2, guid="pbzSecondVisual77")
    keep(visuals.import_pbiviz(ws, project, pv2, "public"))
    text = open(rj, encoding="utf-8").read()
    data = json.loads(text)
    data["publicCustomVisuals"] = [g for g in data["publicCustomVisuals"] if g != "pbzSecondVisual77"]
    open(rj, "w", encoding="utf-8").write(json.dumps(data, indent=2))
    refused(author.add_visual, ws, project, "pbzSecondVisual77", page, ["'Customer'[Segment]", "'Customer'[Name]"],
            code="BAD_REQUEST")
    check("pbzSecondVisual77" not in open(rj, encoding="utf-8").read(), "a refused visual still registered its type")
    v = keep(author.add_visual(ws, project, "pbzSecondVisual77", page, ["[Total Sales]"]))
    check(any(n.startswith("registered pbzSecondVisual77") for n in v["custom"].get("notes", []))
          and "pbzSecondVisual77" in json.load(open(rj, encoding="utf-8"))["publicCustomVisuals"]
          and len(v["files"]) == 2, f"deferred registration: {v}")

    # ---------------------------------------------------------------- registration and role checks
    sj["visual"]["visualType"] = "mysteryVisual123"
    json.dump(sj, open(sp, "w", encoding="utf-8"), indent=2)
    codes = {f["code"] for f in pbircheck.run(project.mirror_report)}
    check("PBIR_VISUAL_TYPE_UNREGISTERED" in codes, f"unregistered type not flagged: {codes}")
    sj["visual"]["visualType"] = "tableEx"
    json.dump(sj, open(sp, "w", encoding="utf-8"), indent=2)
    codes = {f["code"] for f in pbircheck.run(project.mirror_report)}
    check("PBIR_ROLE_UNKNOWN" in codes, f"unknown role of a built-in type not flagged: {codes}")
    os.remove(sp)
    os.rmdir(os.path.dirname(sp))
    text = open(rj, encoding="utf-8").read()
    bad = text.replace("orgVisualABC_OrgStore", "orgVisualABC")
    open(rj, "w", encoding="utf-8").write(bad)
    codes = {f["code"] for f in pbircheck.run(project.mirror_report)}
    check("PBIR_ORGSTORE_NAME" in codes, f"org package without suffix not flagged: {codes}")
    open(rj, "w", encoding="utf-8").write(text)
    res = keep(mirror.push(ws, project))
    errs = [f for f in res.get("findings", []) if f["severity"] == "error"]
    check(res["result"] == "PUSHED" and not errs, f"second push: {res.get('result')} {errs}")

    # ---------------------------------------------------------------- package code stays out
    blobs = [json.dumps(outputs, ensure_ascii=False)]
    for base in (ws.mirror, os.path.join(root, ".pbz")):
        for cur, _d, fs in os.walk(base):
            for fn in fs:
                with open(os.path.join(cur, fn), encoding="utf-8", errors="replace") as h:
                    blobs.append(h.read())
    check(CANARY_JS not in "\n".join(blobs), "custom visual code reached the mirror or an output")
    pulled = keep(mirror.pull(ws, project, rebase=True))
    check(not os.path.isdir(os.path.join(project.mirror_report, "CustomVisuals")), f"CustomVisuals mirrored: {pulled}")

    if fails:
        print("\n".join("FAIL " + f for f in fails))
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
