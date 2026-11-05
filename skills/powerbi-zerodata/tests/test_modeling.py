"""Hierarchies, dynamic format strings, DAX user-defined functions and object-level security on the
synthetic parity project, each pushed through TOM validation.

Run: python tests/test_modeling.py
"""
from __future__ import annotations

import os
import sys
import tempfile

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import common, governance as gv, mirror, modeling as md, tmdl, validate  # noqa: E402
from pbz.common import PbzError  # noqa: E402


def main() -> int:
    fails: list[str] = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    def refused(fn, *args, code, **kw):
        try:
            fn(*args, **kw)
            check(False, f"{fn.__name__} {args[2:]} should fail with {code}")
        except PbzError as e:
            check(e.code == code, f"{fn.__name__}: {e.code} {e.detail} (want {code})")

    def pushed(label):
        res = mirror.push(ws, project)
        errs = [f for f in res.get("findings", []) if f["severity"] == "error"]
        check(res["result"] == "PUSHED" and not errs and not any(f["code"].startswith("TOM_") for f in res.get("findings", [])),
              f"push after {label}: {res.get('result')} {errs}")

    tmp = tempfile.mkdtemp(prefix="pbzmodel-")
    root = os.path.join(tmp, "ws")
    test_parity.build(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    mirror.pull(ws, project)
    validate.report_author = lambda *a, **k: []
    mdef = os.path.join(project.mirror_model, "definition")
    model = lambda: tmdl.load_model(mdef)  # noqa: E731

    # ---------------------------------------------------------------- hierarchies
    r = md.hierarchy_add(ws, project, "Customer", "Geo", ["Segment", "Name"], names=["Segment", "Customer"])
    check(model().tables["Customer"].hierarchies.get("Geo") == {"Segment": "Segment", "Customer": "Name"}, f"add: {r}")
    tags = {lv.name: lv.props.get("lineageTag") for lv in next(n for n in model().tables["Customer"].node.children
                                                               if n.kind == "hierarchy" and n.name == "Geo").children}
    r = md.hierarchy_levels(ws, project, "Customer", "Geo", ["Segment", "SortOrder", "Name"], names=["Segment", "Order", "Customer"])
    h = next(n for n in model().tables["Customer"].node.children if n.kind == "hierarchy" and n.name == "Geo")
    new_tags = {lv.name: lv.props.get("lineageTag") for lv in h.children}
    check(r["kept_identity"] == 2 and new_tags["Segment"] == tags["Segment"] and new_tags["Customer"] == tags["Customer"]
          and [lv.name for lv in h.children] == ["Segment", "Order", "Customer"], f"relevel keeps identity: {r} {new_tags}")
    refused(md.hierarchy_add, ws, project, "Customer", "Geo", ["Segment"], code="NAME_TAKEN")
    refused(md.hierarchy_add, ws, project, "Customer", "Bad", ["Segment", "Segment"], code="BAD_REQUEST")
    refused(md.hierarchy_add, ws, project, "Customer", "Bad", ["Nope"], code="NOT_FOUND")
    pushed("hierarchies")
    refused(md.hierarchy_remove, ws, project, "Date", "Calendar", code="IN_USE")
    r = md.hierarchy_remove(ws, project, "Customer", "Geo")
    check("Geo" not in model().tables["Customer"].hierarchies, f"remove: {r}")

    # ---------------------------------------------------------------- dynamic format strings
    refused(md.format_expression, ws, project, "[Total Sales]", '"#,0"', code="COMPAT_LEVEL")
    r = md.format_expression(ws, project, "[Total Sales]", 'IF([Total Sales] >= 1000, "#,0,K", "#,0")', upgrade=True)
    check("1601" in r.get("note", ""), f"compat upgrade note: {r}")
    m = model().measure("Total Sales")
    check(any(c.kind == "formatStringDefinition" for c in m.node.children), f"format expr: {r}")
    r = md.format_expression(ws, project, "[Total Sales]", 'SWITCH(TRUE(),\n[Total Sales] >= 1000000, "#,0,,M",\n"#,0")')
    check(sum(1 for c in model().measure("Total Sales").node.children if c.kind == "formatStringDefinition") == 1,
          "format expr replaced, not duplicated")
    check("formatString" not in model().measure("Total Sales").node.props,
          "the dynamic format replaces the static formatString (Desktop refuses both)")
    # a hand-made conflict: lint error (push refuses it), set refuses, repair drops the static format
    from pbz import lint, props, repair
    from pbz.tokens import apply_edits
    mt = model().measure("Total Sales")
    doc = model().docs[mt.node.file]
    path = os.path.join(project.mirror_model, "definition", *mt.node.file.split("/"))
    common.write_text(path, apply_edits(doc.text, tmdl.prop_edit(doc, mt.node, "formatString", "#,0")),
                      common.read_text(path)[1])
    check(any(f["code"] == "FORMAT_STRING_CONFLICT" for f in lint.run(model())), "lint flags formatString + definition")
    refused(props.run, ws, project, "[Total Sales]", ["formatString=#,0.0"], code="FORMAT_STRING_CONFLICT")
    r = repair.run(ws, project)
    check(any(f["code"] == "FORMAT_STRING_CONFLICT" for f in r["fixes"])
          and "formatString" not in model().measure("Total Sales").node.props
          and any(c.kind == "formatStringDefinition" for c in model().measure("Total Sales").node.children),
          f"repair drops the static format: {r}")
    refused(md.format_expression, ws, project, "[Total Sales]", "[Nope] & \"x\"", code="UNKNOWN_REF")
    pushed("dynamic format string")
    r = md.format_expression(ws, project, "[Total Sales]", None)
    check(r["result"] == "REMOVED" and not any(c.kind == "formatStringDefinition" for c in model().measure("Total Sales").node.children),
          "format expr removed")

    # ---------------------------------------------------------------- functions
    refused(md.function_add, ws, project, "AddTax", "amount : NUMERIC", "amount * 1.1", code="COMPAT_LEVEL")
    r = md.function_add(ws, project, "AddTax", "amount : NUMERIC", "amount * 1.1", upgrade=True)
    check("AddTax" in model().functions, f"function add: {r}")
    r = md.function_add(ws, project, "SafeRatio", "(numerator : NUMERIC, denominator : NUMERIC)",
                        "IF(denominator = 0,\nBLANK(),\nnumerator / denominator)")
    check("SafeRatio" in model().functions, f"multi-line function: {r}")
    refused(md.function_add, ws, project, "AddTax", "x", "x", code="NAME_TAKEN")
    refused(md.function_add, ws, project, "Bad", "x : WIDGET", "x", code="BAD_REQUEST")
    refused(md.function_add, ws, project, "Bad2", "x", "x + [Nope]", code="UNKNOWN_REF")
    sales = model().tables["Sales"]
    doc = model().docs[sales.file]
    eol = "\r\n" if "\r\n" in doc.text else "\n"
    with open(os.path.join(mdef, *sales.file.split("/")), "a", encoding="utf-8", newline="") as f:
        f.write(f"{eol}\tmeasure 'Taxed Sales' = AddTax([Total Sales]){eol}\t\tlineageTag: 50000000-0000-0000-0000-00000000aaaa{eol}")
    pushed("functions")
    lst = md.function_list(ws, project)
    check({f["function"]: f["calls"] for f in lst["functions"]}.get("AddTax") == 1, f"list: {lst}")
    r = md.function_update(ws, project, "AddTax", None, "amount * 1.2")
    check("1.2" in "".join(model().docs[model().functions['AddTax'].file].text.splitlines()), f"update: {r}")
    r = md.function_rename(ws, project, "AddTax", "WithTax")
    check(r["calls_fixed"] == 1 and "WithTax" in model().functions and "WithTax(" in model().measure("Taxed Sales").expression,
          f"rename fixes calls: {r}")
    refused(md.function_remove, ws, project, "WithTax", code="IN_USE")
    check(md.function_remove(ws, project, "SafeRatio")["action"] == "remove" and "SafeRatio" not in model().functions,
          "remove unused function")
    pushed("function update and rename")

    # ---------------------------------------------------------------- OLS
    gv.role_add(ws, project, "Limited")
    r = md.role_ols(ws, project, "Limited", "'Customer'[Name]", "none")
    r2 = md.role_ols(ws, project, "Limited", "Orphan", "none")
    rn = model().role_nodes["Limited"]
    tps = {c.name: c for c in rn.children if c.kind == "tablePermission"}
    check("Customer" in tps and any(c.kind == "columnPermission" and c.name == "Name" for c in tps["Customer"].children)
          and tps["Orphan"].props.get("metadataPermission", "").strip() == "none", f"ols: {r} {r2}")
    r = md.role_ols(ws, project, "Limited", "'Customer'[SortOrder]", "none")
    tps = {c.name: c for c in model().role_nodes["Limited"].children if c.kind == "tablePermission"}
    check(sum(1 for c in tps["Customer"].children if c.kind == "columnPermission") == 2, f"second column: {r}")
    gv.role_filter(ws, project, "Limited", "Sales", "[Region] <> \"North\"")
    pushed("object-level security")
    r = md.role_ols(ws, project, "Limited", "'Customer'[SortOrder]", "default")
    tps = {c.name: c for c in model().role_nodes["Limited"].children if c.kind == "tablePermission"}
    check(sum(1 for c in tps["Customer"].children if c.kind == "columnPermission") == 1, f"default removes: {r}")
    refused(md.role_ols, ws, project, "Limited", "'Customer'[Nope]", "none", code="NOT_FOUND")
    refused(md.role_ols, ws, project, "Limited", "Orphan", "hidden", code="BAD_REQUEST")
    pushed("ols removal")

    if fails:
        print("\n".join("FAIL " + f for f in fails))
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
