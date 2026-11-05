"""Copying report pages and visuals (`pbz copy page|visual`) on the synthetic parity project: new page,
visual, group, binding, parameter and filter names, remapped interactions and group parents, links to
the copied page retargeted, sync groups kept or made independent, drillthrough and tooltip pages,
group copies across pages with consistent offsets, the integrity checks, a push, and Microsoft's
report validator on a copy of the Desktop-made fixture.

Run: python tests/test_components.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import types

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
    validate.report_author = lambda *a, **k: []  # the parity report is not Desktop-made; checked on the fixture below
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
    res = mirror.push(ws, project)
    perr = [f for f in res.get("findings", []) if f["severity"] == "error"]
    check(res["result"] == "PUSHED" and not perr, f"push: {res.get('result')} {perr}")

    # ---------------------------------------------------------------- Microsoft's validator on the Desktop-made fixture
    validate.report_author = real_author
    st = os.path.join(tmp, "fx")
    shutil.copytree(os.path.join(SKILL, "tests", "fixture"), st)
    fx = types.SimpleNamespace(name="Canary", mirror_report=os.path.join(st, "Canary.Report"),
                               mirror_model=os.path.join(st, "Canary.SemanticModel"))
    fws = types.SimpleNamespace(mirror=st)
    base = validate.report_author(st, fx, lambda s: s)
    if any(f.get("code") == "REPORT_VALIDATOR_MISSING" for f in base):
        print("note: Microsoft's report validator is not installed; fixture comparison skipped")
    else:
        key = lambda f: (f.get("severity"), str(f.get("message"))[:200])  # noqa: E731
        fdir = os.path.join(fx.mirror_report, "definition", "pages")
        src_pid = next(p for p in sorted(os.listdir(fdir)) if os.path.isdir(os.path.join(fdir, p)))
        fvis = sorted(os.listdir(os.path.join(fdir, src_pid, "visuals")))
        components.copy_page(fws, fx, "Canary Page", "Canary Copy")
        dtp = author.add_page(fws, fx, "Canary Detail", drillthrough=["'Project'[Status]"])
        components.copy_page(fws, fx, "Canary Detail", "Canary Detail Copy")
        components.copy_visual(fws, fx, "Canary Page", fvis[0], to_page="Canary Copy")
        after = validate.report_author(st, fx, lambda s: s)
        new_findings = [key(f) for f in after if key(f) not in {key(b) for b in base}]
        check(not new_findings, f"Microsoft's validator rejects the copies: {new_findings[:6]}")
        check(dtp.get("back_button"), "fixture drillthrough page")

    if fails:
        print("\n".join("FAIL " + f for f in fails))
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
