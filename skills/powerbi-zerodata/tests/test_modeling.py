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


def test_region_index(check):
    import statistics
    import time
    from unittest.mock import patch
    from pbz import consolidate, deps, describe
    from pbz.tokens import apply_edits

    text = ("table T\n\tcolumn Plain\n\t\tdataType: int64\n"
            "\t/// calculated description\n\tcolumn Calc = 1 + 2\n\t\tdataType: int64\n"
            "\tmeasure Multiple = 10\n\t\tdetailRowsExpression = ROW(\"x\", 3)\n"
            "\t/// measure description\n\tmeasure M = 7\n\t\tformatStringDefinition = \"0.00\"\n"
            "\t/// M description\n\tpartition Import = m\n\t\tsource = Table.FromRows({{1}})\n"
            "\t/// calculated description\n\tpartition Calc = calculated\n\t\tsource = ROW(\"x\", 2)\n"
            "\tmeasure Fenced = ```\n\t\t8 + 9\n\t\t```\n\t\tformatString: 0\n")
    texts = {"mixed.tmdl": text, "expression.tmdl": "expression Query = 42\n"}
    model = tmdl.load_model("", texts)
    def snapshot(m):
        tables = [(t.name, t.calculated, t.partitions,
                 [(c.name, c.calculated, c.data_type) for c in t.columns.values()],
                 [(v.name, v.expression, v.format_string) for v in t.measures.values()])
                for t in m.tables.values()]
        expressions = [(name, m.docs[n.file].text[n.expr.start:n.expr.end])
                       for name, n in m.expressions.items()]
        return tables, expressions
    # Corrected baseline: the source-property rule remains in force, with legacy full scans.
    with patch.object(tmdl.TmdlDoc, "regions_for", lambda d, n: [r for r in d.regions if r.node is n]):
        baseline = tmdl.load_model("", texts)
    check(snapshot(model) == snapshot(baseline), "indexed model equals corrected scanning baseline")
    check(snapshot(model)[1] == [("Query", "42")], "expected shared expression source")
    t = model.tables["T"]
    check(t.partitions == [("Import", "m", "Table.FromRows({{1}})"),
                           ("Calc", "calculated", 'ROW("x", 2)')], "expected partition text")
    check(t.calculated and not t.columns["Plain"].calculated and t.columns["Calc"].calculated,
          "expected calculated flags")
    check(t.measures["Multiple"].expression == "10", "first DAX region wins among multiple own regions")
    check(t.measures["M"].expression == "7" and t.measures["Fenced"].expression == "\t\t8 + 9\n",
          "expected measure expressions, excluding descriptions and formats")
    doc = model.docs["mixed.tmdl"]
    m = t.measures["M"].node
    check(m.expr is not None and doc.text[m.expr.start:m.expr.end] == "7", "parser assigns expression")
    for n in doc.nodes:
        ids = {id(n), *(id(d) for d in n.descendants())}
        check(tmdl.node_regions(doc, n) == [r for r in doc.regions if id(r.node) in ids], "deep global order")
        check(tmdl.node_regions(doc, n, False) == [r for r in doc.regions if r.node is n], "own global order")
    with patch.object(tmdl, "_lines", wraps=tmdl._lines) as line_table:
        parsed = tmdl.parse("mixed.tmdl", text)
        check(line_table.call_count == 1, "parse computes line table once")
        direct = tmdl.TmdlDoc("mixed.tmdl", text, parsed.nodes, parsed.regions)
        check(line_table.call_count == 2 and direct.lines == parsed.lines,
              "direct document construction computes line table")
    for ending, other in (("\n", "\r\n"), ("\r\n", "\n")):
        mixed = "table Mixed" + other + "\tmeasure M = 7" + ending + "\tmeasure Next = 8" + other
        mixed_doc = tmdl.parse("mixed.tmdl", mixed)
        measure = next(n for n in mixed_doc.nodes if n.kind == "measure")
        result = apply_edits(mixed, tmdl.prop_edit(mixed_doc, measure, "displayFolder", "KPIs"))
        check(result == mixed.replace("\tmeasure M = 7" + ending,
                                     "\tmeasure M = 7" + ending + "\t\tdisplayFolder: KPIs" + ending),
              "insert uses local line ending " + repr(ending))
    # Interleaved ownership, including a parent region after a descendant's region.
    regs = [doc.regions_for(m)[0], doc.regions_for(m.children[0])[0], doc.regions_for(m)[1]]
    interleaved = tmdl.TmdlDoc("", text, doc.nodes, regs)
    check(tmdl.node_regions(interleaved, m) == regs, "deep order is global, not concatenated per owner")

    class NoScan(list):
        attempted = False
        def __iter__(self):
            self.attempted = True
            raise AssertionError("consumer rescanned all document regions")
    doc.regions = NoScan(doc.regions)
    check(len(tmdl.node_regions(doc, m)) == 3, "indexed deep lookup avoids full traversal")
    edited = apply_edits(text, tmdl.prop_edit(doc, m, "displayFolder", "KPIs"))
    check("measure M = 7\n\t\tdisplayFolder: KPIs\n\t\tformatStringDefinition" in edited, "property insertion span")
    fenced = t.measures["Fenced"].node
    check(apply_edits(text, tmdl.prop_edit(doc, fenced, "formatString", "0.00")) ==
          text.replace("formatString: 0", "formatString: 0.00"), "property replacement span")
    a, b = consolidate._expr_span(doc, m)
    check(text[a:b] == " 7", "inline consolidation span")
    a, b = consolidate._expr_span(doc, fenced)
    check(text[a:b] == " ```\n\t\t8 + 9\n\t\t```", "fenced consolidation span")
    a, b = tmdl.block_span(doc, m)
    check(text[a:b] == "\t/// measure description\n\tmeasure M = 7\n\t\tformatStringDefinition = \"0.00\"\n", "block excludes next description")
    fmt = consolidate._format_of(model, t.measures["M"], deps.Resolver(model))
    check(fmt == ("", ("LIT:S0.00",)), "dynamic format uses indexed regions")
    check(mirror._object_summary("mixed.tmdl", text, text.replace("measure M = 7", "measure M = 6")) ==
          ["~ measure M"], "mirror object summary")

    # Every load_model member lookup must avoid iterating the global region list.
    original_parse = tmdl.parse
    def guarded_parse(*args):
        d = original_parse(*args)
        d.regions = NoScan(d.regions)
        return d
    with patch.object(tmdl, "parse", guarded_parse):
        guarded_model = tmdl.load_model("", texts)
        check(snapshot(guarded_model) == snapshot(baseline), "zero global traversals in load_model")
        check(mirror._object_summary("mixed.tmdl", text, text.replace("measure M = 7", "measure M = 6")) ==
              ["~ measure M"], "zero global traversals in summary")

    check(not doc.regions.attempted and not any(d.regions.attempted for d in guarded_model.docs.values()),
          "no traversal attempts in indexed consumers")

    # Table describe must use the same parsed model for its classifier.
    from types import SimpleNamespace
    with tempfile.TemporaryDirectory() as directory:
        project = SimpleNamespace(mirror_model=directory, name="fixture")
        os.mkdir(os.path.join(directory, "definition"))
        ws = SimpleNamespace(private=directory)
        with patch.object(tmdl, "load_model", return_value=model) as load, \
             patch.object(describe, "Classifier") as classifier, \
             patch.object(describe.policy_mod, "load", return_value={}), \
             patch.object(describe, "_index_summary", return_value={}), \
             patch.object(describe, "field_usage", return_value={}):
            classifier.return_value.column_class.return_value = "high"
            classifier.return_value.category_values.return_value = None
            result = describe.describe(ws, project, "T")
            check(load.call_count == 1 and classifier.call_args.args[0] is model, "describe loads directory once")
            check(result["partitions"][0]["functions"] == ["Table.FromRows"], "describe sees source expression")

    # One large document, repeated measurements; setup is outside each measurement.
    timings = {}
    for size in (4000, 8000):
        large = {"large.tmdl": "table Large\n" + "".join(f"\tmeasure M{i} = {i}\n" for i in range(size))}
        elapsed = []
        for _ in range(5):
            start = time.perf_counter()
            with patch.object(tmdl, "parse", guarded_parse):
                loaded = tmdl.load_model("", large)
            elapsed.append(time.perf_counter() - start)
            check(len(loaded.tables["Large"].measures) == size, "benchmark member count")
            check(not loaded.docs["large.tmdl"].regions.attempted, "zero global traversals per large document")
        timings[size] = statistics.median(elapsed)
    ratio = timings[8000] / timings[4000]
    print(f"region benchmark: median of 5, 8k/4k = {ratio:.3f}")
    check(ratio < 2.5, f"region scaling ratio {ratio:.3f} exceeds 2.5")


def main() -> int:
    fails: list[str] = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    test_region_index(check)
    if "--regions-only" in sys.argv:
        for failure in fails:
            print("FAIL " + failure)
        print(f"region acceptance: {len(fails)} failed")
        return int(bool(fails))

    def refused(fn, *args, code, **kw):
        try:
            fn(*args, **kw)
            check(False, f"{fn.__name__} {args[2:]} should fail with {code}")
        except PbzError as e:
            check(e.code == code, f"{fn.__name__}: {e.code} {e.detail} (want {code})")

    def pushed(label):
        res = mirror.push(ws, project)
        errs = [f for f in res.get("findings", []) if f["severity"] == "error"]
        check(res["result"] == "PUSHED" and not errs and not any(f["code"].startswith("TOM_") and f["code"] != "TOM_PASSED" for f in res.get("findings", [])),
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

    # Item 30a: source must be selected explicitly, independently of model equality.
    partitions = tmdl.load_model("", {"mixed.tmdl":
        "table Sources\n\t/// M description\n\tpartition Import = m\n"
        "\t\tsource = Table.FromRows({{1}})\n"
        "\t/// Calculated description\n\tpartition Calc = calculated\n"
        "\t\tsource = ROW(\"x\", 2)\n"
        "\t/// Multiline description\n\tpartition Multi = m\n\t\tsource = ```\n"
        "\t\t\tlet\n\t\t\t\tRows = Table.FromRows({{3}})\n\t\t\tin\n\t\t\t\tRows\n\t\t```\n"
        "\tpartition Undocumented = m\n\t\tsource = Table.FromRows({{4}})\n"}).tables["Sources"]
    check(partitions.partitions == [("Import", "m", "Table.FromRows({{1}})"),
                                    ("Calc", "calculated", 'ROW("x", 2)'),
                                    ("Multi", "m", "\t\t\tlet\n\t\t\t\tRows = Table.FromRows({{3}})\n\t\t\tin\n\t\t\t\tRows\n"),
                                    ("Undocumented", "m", "Table.FromRows({{4}})")],
          f"documented partition sources: {partitions.partitions}")
    check(partitions.calculated, "calculated partition sets table flag")

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
