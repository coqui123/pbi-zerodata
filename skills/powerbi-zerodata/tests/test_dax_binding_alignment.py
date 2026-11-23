"""Offline controls for conservative shared DAX bindings and duplicate reporting."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))
from pbz import common, consolidate, deps, rename, rewrite, similar, tmdl
from pbz.common import PbzError

E1 = "VAR v = Product RETURN SUMX(Sales, SUMX(v, [Price]))"
E2 = "SUMX(Sales, SUMX(TOPN(MAX(Sales[Qty]), Product), [Price]))"
CONTROL = "SUMX(Sales, [Price])"
WITNESSES = (
    "SUMX(Sales, SUMX(TOPN(Sales[Qty], Product), [Price]))",
    "VAR v = Product RETURN SUMX(Sales, SUMX(FILTER(v, Sales[Qty] > 0), [Price]))",
    "VAR v = Product RETURN SUMX(FILTER(v, [Qty] > 0), [Price])",
    "VAR v = Product RETURN SUMX(CALCULATETABLE(v, Sales[Qty] > 0), [Price])",
)


def texts(measures=None):
    measures = measures or {"Dax A": E1, "Dax B": E1, "Control": CONTROL}
    return {
        "tables/Sales.tmdl": "table Sales\n\n" + "".join(
            f"\tmeasure '{name}' = {expr}\n\n" for name, expr in measures.items()
        ) + "\tmeasure 'Total Sales' = SUM(Sales[Qty])\n\n"
        "\tcolumn Price\n\t\tdataType: int64\n\n"
        "\tcolumn Qty\n\t\tdataType: int64\n",
        "tables/Product.tmdl": "table Product\n\n\tcolumn Price\n\t\tdataType: int64\n",
        "tables/Dates.tmdl": "table Dates\n\n\tcolumn Date\n\t\tdataType: dateTime\n",
    }


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.model = tmdl.load_model("", texts=texts())
        self.res = deps.Resolver(self.model)

    def price(self, expr, home="Sales", row_home=False):
        return [b for b in deps.dax_bindings(expr, home, self.res, row_home).bindings
                if expr[b.start:b.end] == "[Price]" and b.qualifier is None][-1]

    def test_e1_keeps_ref_but_is_uncertain(self):
        b = self.price(E1)
        self.assertEqual(b.ref, deps.Ref("column", "Sales", "Price"))
        self.assertTrue(b.uncertain)

    def test_e2_explicit_source_is_product_and_canon_stays_uncertain(self):
        b = self.price(E2)
        self.assertEqual(b.ref, deps.Ref("column", "Product", "Price"))
        self.assertFalse(b.uncertain)
        self.assertTrue(similar.canon(E2, "Sales", self.res).uncertain)

    def test_qualify_no_ops(self):
        for expr in (E1, E2, *WITNESSES):
            with self.subTest(expr=expr):
                self.assertEqual(rewrite._qualify(expr, self.model.measure("Control"), self.model), expr)

    def test_control_qualifies_and_is_idempotent(self):
        m = self.model.measure("Control")
        result = rewrite._qualify(CONTROL, m, self.model)
        self.assertEqual(result, "SUMX(Sales, Sales[Price])")
        self.assertEqual(rewrite._qualify(result, m, self.model), result)
        self.assertFalse(self.price(CONTROL).uncertain)

    def test_d1_supplier_boundaries(self):
        for expr, table, uncertain in (
            ("VAR v = Sales RETURN SUMX(Product, SUMX(v, [Price]))", "Product", True),
            ("VAR v = Product RETURN SUMX(v, SUMX(Sales, [Price]))", "Sales", False),
            ("SUMX(FILTER(Sales, [Qty] > 0), [Price])", "Sales", False),
            ("SUMX(VALUES(Sales[Price]), [Price])", "Sales", False),
            ("VAR n = MAX(Sales[Qty]) RETURN SUMX(Sales, [Price] * n)", "Sales", False),
            ("[Price] * 2", "Sales", False),
        ):
            with self.subTest(expr=expr):
                b = self.price(expr, row_home=True)
                self.assertEqual(b.ref, deps.Ref("column", table, "Price"))
                self.assertEqual(b.uncertain, uncertain)

    def test_relatedtable_preserves_calculated_column_binding(self):
        b = self.price("SUMX(RELATEDTABLE(Sales), [Price])", "Product", True)
        self.assertEqual(b.ref, deps.Ref("column", "Sales", "Price"))
        self.assertFalse(b.uncertain)

    def test_datesinperiod_measure_control(self):
        expr = "AVERAGEX(DATESINPERIOD(Dates[Date], MAX(Dates[Date]), -7, DAY), [Total Sales])"
        b = deps.dax_bindings(expr, "Sales", self.res).bindings[-1]
        self.assertEqual(b.ref, deps.Ref("measure", "Sales", "Total Sales"))
        self.assertFalse(b.uncertain)

    def test_narrowed_and_composite_sources_do_not_certify_whole_tables(self):
        for source in (
            "VALUES(Sales[Qty])", "ALL(Sales[Qty])",
            'SELECTCOLUMNS(Sales, "P", Sales[Price])',
            'CROSSJOIN(VALUES(Sales[Qty]), VALUES(Product[Price]))',
            "SUMMARIZE(Sales, Sales[Qty])", "TREATAS(VALUES(Product[Price]), Sales[Qty])",
            "UNION(Sales, Sales)", "GENERATE(Sales, Product)",
        ):
            with self.subTest(source=source):
                self.assertTrue(self.price(f"SUMX({source}, [Price])").uncertain)

    def test_parentheses_preserve_source_argument_role(self):
        b = self.price("SUMX(TOPN((Sales[Qty]), (Product)), [Price])")
        self.assertEqual(b.ref.table, "Product")
        self.assertFalse(b.uncertain)
        expr = "VAR v = Product RETURN SUMX(FILTER((v), (Sales[Qty] > 0)), [Price])"
        self.assertTrue(self.price(expr).uncertain)

    def test_unknown_var_and_measure_fallback_are_conservative(self):
        self.assertTrue(self.price("VAR t = Sales RETURN SUMX(t, [Price])").uncertain)
        expr = "VAR t = Product RETURN SUMX(t, [Total Sales])"
        b = deps.dax_bindings(expr, "Sales", self.res).bindings[-1]
        self.assertEqual(b.ref.kind, "measure")
        self.assertTrue(b.uncertain)


class PublicTests(unittest.TestCase):
    def project(self, measures=None):
        tmp = tempfile.TemporaryDirectory(prefix="pbz-binding-")
        self.addCleanup(tmp.cleanup)
        ws = common.Workspace(tmp.name)
        ws.private = os.path.join(tmp.name, "private")
        Path(ws.private).mkdir()
        Path(tmp.name, "Mini.pbip").write_text("{}")
        project = common.Project(ws, "Mini")
        mdef = Path(project.mirror_model, "definition")
        for rel, text in texts(measures).items():
            path = mdef / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (mdef / "model.tmdl").write_text("model Model\n\tculture: en-US\n\nref table Sales\nref table Product\nref table Dates\n")
        (mdef / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1600\n")
        Path(project.model_dir).mkdir(parents=True, exist_ok=True)
        return ws, project

    def snapshot(self, ws):
        return {str(p.relative_to(ws.mirror)): p.read_bytes()
                for p in Path(ws.mirror).rglob("*") if p.is_file()}

    def test_public_qualify_no_op_leaves_bytes(self):
        for expr in (E1, E2, *WITNESSES):
            with self.subTest(expr=expr):
                ws, project = self.project({"Target": expr})
                before = self.snapshot(ws)
                out = rewrite.run(ws, project, "qualify", "[Target]")
                self.assertEqual(out["result"], "NOTHING_TO_CHANGE")
                self.assertEqual(self.snapshot(ws), before)

    def test_public_control_persists_qualified_expression(self):
        ws, project = self.project({"Target": CONTROL})
        with patch("pbz.desktop.require_port", side_effect=PbzError("ENGINE_UNAVAILABLE", "closed")):
            out = rewrite.run(ws, project, "qualify", "[Target]")
        self.assertEqual(out["result"], "CHANGED")
        model = tmdl.load_model(str(Path(project.mirror_model, "definition")))
        self.assertEqual(model.measure("Target").expression.strip(), "SUMX(Sales, Sales[Price])")
        self.assertEqual(rewrite.run(ws, project, "qualify", "[Target]")["result"], "NOTHING_TO_CHANGE")

    def test_uncertain_pair_reported_once(self):
        for expr in (E1, E2):
            with self.subTest(expr=expr):
                ws, project = self.project({"Dax A": expr, "Dax B": expr})
                out = similar.run(ws, project, threshold=0)
                pair = {"[Dax A]", "[Dax B]"}
                for bucket in ("identical", "same_shape"):
                    self.assertFalse(any(pair <= set(g["measures"]) for g in out[bucket]))
                self.assertEqual([set(g["measures"]) for g in out["identical_uncertain"]], [pair])
                self.assertFalse(any({p["a"], p["b"]} == pair for p in out["pairs"]))

    def test_ordinary_uncertain_merge_stays_refused_without_writes(self):
        for expr in (E1, E2):
            with self.subTest(expr=expr):
                ws, project = self.project({"Dax A": expr, "Dax B": expr})
                before = self.snapshot(ws)
                with self.assertRaises(PbzError) as caught:
                    consolidate.merge(ws, project, "Dax B", "Dax A")
                self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")
                self.assertEqual(self.snapshot(ws), before)

    def test_certain_identical_and_different_hash_patterns_preserved(self):
        ws, project = self.project({"A": CONTROL, "B": CONTROL,
                                    "C": CONTROL + " + 1", "D": CONTROL + " + 2"})
        out = similar.run(ws, project)
        self.assertIn({"[A]", "[B]"}, [set(g["measures"]) for g in out["identical"]])
        self.assertEqual(out["identical_uncertain"], [])
        self.assertTrue(any({"[C]", "[D]"} <= set(g["measures"]) for g in out["same_shape"]))

    def test_uncertain_different_hash_pattern_hints_preserved(self):
        ws, project = self.project({"A": E1 + " + 1", "B": E1 + " + 2"})
        out = similar.run(ws, project)
        self.assertTrue(any({"[A]", "[B]"} <= set(g["measures"]) for g in out["same_shape"]))

    def test_mixed_certainty_raw_hash_group_is_wholly_uncertain(self):
        ws, project = self.project({"A": CONTROL, "B": CONTROL})
        real = similar.canon
        calls = []

        def mixed(*args, **kwargs):
            c = real(*args, **kwargs)
            calls.append(c)
            if len(calls) == 1:
                c.uncertain = True
            return c

        with patch.object(similar, "canon", side_effect=mixed):
            out = similar.run(ws, project)
        self.assertEqual(out["identical"], [])
        self.assertEqual([set(g["measures"]) for g in out["identical_uncertain"]], [{"[A]", "[B]"}])
        self.assertEqual(out["same_shape"], [])
        self.assertEqual(out["pairs"], [])

    def test_public_rename_unknown_source_refuses_without_writes(self):
        for expr in (E1, WITNESSES[1], WITNESSES[2], WITNESSES[3],
                     "VAR t = Sales RETURN SUMX(t, [Price])"):
            with self.subTest(expr=expr):
                ws, project = self.project({"Target": expr})
                before = self.snapshot(ws)
                with self.assertRaises(PbzError) as caught:
                    rename.rename(ws, project, "column", "Price", "New Price", table="Sales")
                self.assertEqual(caught.exception.code, "AMBIGUOUS")
                self.assertEqual(self.snapshot(ws), before)


if __name__ == "__main__":
    unittest.main()
