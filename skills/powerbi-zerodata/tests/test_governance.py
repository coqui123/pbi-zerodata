"""Governance and safety verbs on the synthetic parity project: mirrored cultures (translations kept in
step by rename / move / remove), translate, perspective and role editors, incremental refresh policy,
selective push and revert, the strict literal policy and the disclosure ledger.

Run: python tests/test_governance.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import checks, common, governance as gv, mirror, move, refreshpolicy as rp, rename, tmdl, unused, validate  # noqa: E402
from pbz import guard  # noqa: E402
from pbz.common import PbzError  # noqa: E402

SECRET_LING = "Zelda Quartermain"


def main() -> int:
    fails: list[str] = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    def refused(fn, *args, code, **kw):
        try:
            fn(*args, **kw)
            check(False, f"{fn.__name__} should fail with {code}")
        except PbzError as e:
            check(e.code == code, f"{fn.__name__}: {e.code} (want {code})")

    def pushed(label, **kw):
        res = mirror.push(ws, project, **kw)
        errs = [f for f in res.get("findings", []) if f["severity"] == "error"]
        check(res["result"] == "PUSHED" and not errs, f"push after {label}: {res.get('result')} {errs}")
        check(not any(f["code"].startswith("TOM_") for f in res.get("findings", [])), f"TOM did not run after {label}")
        return res

    tmp = tempfile.mkdtemp(prefix="pbzgov-")
    root = os.path.join(tmp, "ws")
    test_parity.build(root)
    real_def = os.path.join(root, "Parity.SemanticModel", "definition")
    with open(os.path.join(real_def, "cultures", "en-US.tmdl"), "w", encoding="utf-8", newline="") as f:
        f.write("cultureInfo en-US\n\n\tlinguisticMetadata =\n\t\t\t{\n\t\t\t  \"Version\": \"1.0.0\",\n"
                f"\t\t\t  \"Entities\": {{\"{SECRET_LING}\": {{}}}}\n\t\t\t}}\n\t\tcontentType: json\n\n")
    with open(os.path.join(real_def, "model.tmdl"), "a", encoding="utf-8", newline="") as f:
        f.write("ref cultureInfo en-US\n")
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    res = mirror.pull(ws, project)
    check(not res["lossy_files"], f"lossy: {res['lossy_files']}")
    validate.report_author = lambda *a, **k: []  # schema validator needs a Desktop-made report; TOM still runs
    mdef = os.path.join(ws.mirror, "Parity.SemanticModel", "definition")
    cdir = os.path.join(mdef, "cultures")

    # ---------------------------------------------------------------- cultures in the mirror
    en = open(os.path.join(cdir, "en-US.tmdl"), encoding="utf-8").read()
    check(SECRET_LING not in en and "«payload:linguisticMetadata#" in en, "linguistic schema not a payload")
    check(gv.cultures_mirrored(project), "cultures not mirrored")
    gv.culture_add(ws, project, "fr-FR")
    refused(gv.culture_add, ws, project, "fr-FR", code="NAME_TAKEN")
    refused(gv.culture_add, ws, project, "nope!", code="BAD_REQUEST")
    for tgt, kw in (("Sales", {"caption": "Ventes"}), ("[Total Sales]", {"caption": "Ventes totales", "folder": "Mesures"}),
                    ("'Sales'[Region]", {"caption": "Region FR"}), ("'Date'[Calendar]", {"caption": "Calendrier"}),
                    ("[Ship Sales]", {"caption": "Ventes expediees"})):
        gv.translate(ws, project, "fr-FR", tgt, **kw)
    gv.translate(ws, project, "fr-FR", "[Total Sales]", description="Somme des montants")
    refused(gv.translate, ws, project, "fr-FR", "[Nope]", caption="x", code="NOT_FOUND")
    refused(gv.translate, ws, project, "fr-FR", "Sales", caption="«lit#1»", code="BAD_REQUEST")
    lst = {c["culture"]: c for c in gv.culture_list(ws, project)["cultures"]}
    check(lst["fr-FR"]["captions"] == 5 and lst["en-US"]["linguistic_schema"], f"culture list: {lst}")
    miss = gv.culture_missing(ws, project, "fr-FR")
    check("Sales" not in miss["objects"] and "Customer" in miss["objects"], "culture missing list")
    pushed("translations")
    # rename / move keep translations in step
    rename.rename(ws, project, "measure", "Total Sales", "Total Revenue")
    rename.rename(ws, project, "table", "Sales", "Fact Sales")
    move.move_measure(ws, project, "Ship Sales", "Customer")
    fr = open(os.path.join(cdir, "fr-FR.tmdl"), encoding="utf-8").read()
    check("measure 'Total Revenue'" in fr and "table 'Fact Sales'" in fr and "Total Sales" not in fr, "culture rename")
    doc = tmdl.parse("cultures/fr-FR.tmdl", fr)
    ship = next(n for n in doc.nodes if n.kind == "measure" and n.name == "Ship Sales")
    check(ship.parent.name == "Customer", f"translation not moved with the measure: {ship.parent.name}")
    pushed("rename and move with translations")
    # removal prunes translations
    gv.translate(ws, project, "fr-FR", "[Sales Copy]", caption="Copie")
    out = unused.remove(ws, project, only=["measure"])
    check("measure [Sales Copy]" in out["removed"] and "Sales Copy" not in open(os.path.join(cdir, "fr-FR.tmdl"), encoding="utf-8").read(),
          "removal did not prune the translation")
    pushed("removal with translations")

    # ---------------------------------------------------------------- perspectives and roles
    gv.perspective_add(ws, project, "Exec")
    r = gv.perspective_edit(ws, project, "Exec", ["Customer", "[Total Revenue]", "Date.Calendar"], True)
    check("+ measure Fact Sales.Total Revenue" in r["changes"] and any(c.startswith("+ column Customer.") for c in r["changes"]),
          f"perspective include: {r['changes']}")
    r = gv.perspective_edit(ws, project, "Exec", ["'Customer'[Segment]"], False)
    check(r["changes"] == ["- column Customer.Segment"], f"perspective exclude: {r['changes']}")
    gv.role_add(ws, project, "Segment Reader")
    refused(gv.role_add, ws, project, "Segment Reader", code="NAME_TAKEN")
    check(gv.role_filter(ws, project, "Segment Reader", "Customer", '[Segment] = "Retail"')["references"] == ["Customer[Segment]"],
          "role filter")
    refused(gv.role_filter, ws, project, "Segment Reader", "Customer", "[Nope] = 1", code="UNKNOWN_REF")
    gv.role_member(ws, project, "Segment Reader", "add", "someone@example.invalid")
    pushed("perspective and role")
    mirror.pull(ws, project)
    role = open(os.path.join(mdef, "roles", "Segment Reader.tmdl"), encoding="utf-8").read()
    check("someone@example.invalid" not in role and "«pii#" in role, "role member not tokenized after pull")
    tok = next(x for x in role.split() if x.startswith("«pii#"))
    check(gv.role_member(ws, project, "Segment Reader", "remove", tok)["members"] == 0, "member removal by token")
    gv.role_filter(ws, project, "Segment Reader", "Customer", None)
    gv.perspective_remove(ws, project, "Exec")
    pushed("unfilter and removals")

    # ---------------------------------------------------------------- incremental refresh
    r = rp.set_policy(ws, project, "Fact Sales", "DateKey", 3, "year", 10, "day", detect="ShipDateKey", complete=True)
    check(r["filter_step"] == "ADDED" and r["parameters_created"] == ["RangeStart", "RangeEnd"], f"refresh policy: {r}")
    refused(rp.set_policy, ws, project, "Fact Sales", "DateKey", 3, "year", 10, "day", code="NAME_TAKEN")
    refused(rp.set_policy, ws, project, "Customer", "Name", 1, "year", 1, "day", code="UNSUPPORTED")
    shown = rp.show(ws, project)["policies"]
    check(shown and shown[0]["rollingWindowPeriods"] == "3" and shown[0]["change_detection"], f"show: {shown}")
    pushed("refresh policy")
    check(rp.remove_policy(ws, project, "Fact Sales")["action"] == "removed", "remove policy")
    pushed("refresh policy removal")

    # ---------------------------------------------------------------- selective push and revert
    sp = os.path.join(mdef, "tables", "Fact Sales.tmdl")
    text = open(sp, encoding="utf-8", newline="").read()
    open(sp, "w", encoding="utf-8", newline="").write(text.replace("SUM('Fact Sales'[Amount])", "SUM('Fact Sales'[Amount]) * 1"))
    gv.culture_add(ws, project, "es-ES")
    res = pushed("selective push", only=["*/cultures/*", "*/model.tmdl"])
    check(any("Fact Sales.tmdl" in h for h in res.get("held_back", [])), f"held back: {res.get('held_back')}")
    check(mirror.diff(ws, project)["changed"] == ["Parity.SemanticModel/definition/tables/Fact Sales.tmdl"], "held file not pending")
    check(mirror.revert(ws, project, obj="measure 'Fact Sales'[Total Revenue]")["result"] == "REVERTED", "object revert")
    check(not any(mirror.diff(ws, project)[k] for k in ("changed", "added", "deleted")), "revert left changes")

    # ---------------------------------------------------------------- strict literal policy
    check(guard.policy_loosened({"literals": "strict"}, {}) and not guard.policy_loosened({}, {"literals": "strict"}),
          "strict literal policy must only be removable by the user")
    root2 = os.path.join(tmp, "ws2")
    test_parity.build(root2)
    os.makedirs(os.path.join(root2, ".pbz"), exist_ok=True)
    json.dump({"literals": "strict"}, open(os.path.join(root2, ".pbz", "policy.json"), "w"))
    ws2 = common.Workspace(root2)
    ws2.private = os.path.join(tmp, "private2")
    p2 = ws2.project()
    res = mirror.pull(ws2, p2)
    blob = "\n".join(mirror.mirror_texts(ws2, p2).values())
    check(not res["lossy_files"] and "SalesDbSecret" not in blob and "FactSales" not in blob and "secret-host" not in blob,
          "strict policy left source literals visible")
    check('"MMM"' in blob and "\"Amount\"" in blob, "strict policy hid format codes or source column names")
    check(mirror.push(ws2, p2)["result"] == "NOTHING_TO_PUSH", "strict round trip not exact")

    # ---------------------------------------------------------------- disclosure ledger
    text = open(sp, encoding="utf-8", newline="").read()
    text = text.replace("measure 'Base Unused' = [Total Revenue] * 2",
                        "measure 'Base Unused' = IF(SELECTEDVALUE(Customer[Segment]) = \"GuessOne\", 1, BLANK())")
    open(sp, "w", encoding="utf-8", newline="").write(text)
    pushed("agent literal")
    st = checks._load_state(ws)
    check(checks._h("GuessOne") in st["ledger"]["authored"], "pushed literal not recorded in the ledger")
    pol_path = os.path.join(root, ".pbz", "policy.json")
    os.makedirs(os.path.dirname(pol_path), exist_ok=True)
    json.dump({"budgets": {"constants_per_target": 0}}, open(pol_path, "w"))
    check("GuessOne" in open(sp, encoding="utf-8").read(), "test setup: agent literal not in the mirror")
    # [Wrapped] reaches [Base Unused], where the pushed literal lives
    out = checks.run_checks(ws, project, {"checks": [{"id": "a", "kind": "nonblank", "expr": "[Base Unused] + 0"}]})
    check(out["results"][0]["status"] == "BUDGET", f"a literal reached through a measure was not charged: {out['results']}")
    json.dump({"budgets": {"probes_per_day": 1}}, open(pol_path, "w"))
    check(checks.spend(ws, "probes") and not checks.spend(ws, "probes"), "daily probe budget")

    # ---------------------------------------------------------------- MCP argv for list positionals
    from pbz import mcp
    argv, _cwd = mcp.build_argv(mcp.BY_NAME["perspective"], {"action": "include", "name": "Exec",
                                                             "targets": ["[Total Revenue]", "Customer"]}, root, None)
    check(argv[-2:] == ["[Total Revenue]", "Customer"], f"MCP list positional: {argv}")
    argv, _cwd = mcp.build_argv(mcp.BY_NAME["role"], {"action": "filter", "name": "R", "args": ["Customer", "[Segment] = 1"]},
                                root, None)
    check(argv[-2:] == ["Customer", "[Segment] = 1"], f"MCP role args: {argv}")

    shutil.rmtree(tmp, ignore_errors=True)
    for f in fails:
        print("FAIL", f)
    print("ok" if not fails else f"{len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
