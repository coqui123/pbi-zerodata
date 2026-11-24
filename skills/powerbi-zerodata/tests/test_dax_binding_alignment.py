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
        "\tcolumn Qty\n\t\tdataType: int64\n\n"
        "\tcolumn Discount\n\t\tdataType: int64\n",
        "tables/Product.tmdl": "table Product\n\n\tcolumn Price\n\t\tdataType: int64\n\n\tcolumn Other\n\t\tdataType: int64\n",
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

    def test_e2_keeps_home_ref_but_is_uncertain(self):
        # Council round 2 revised the round 1 Product outcome: the existing
        # similar_scope poison pairs pin this shape to the home ref with an
        # uncertain flag, and the brief requires only that no editor acts on
        # an occurrence whose owner cannot be established.
        b = self.price(E2)
        self.assertEqual(b.ref, deps.Ref("column", "Sales", "Price"))
        self.assertTrue(b.uncertain)
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

    def test_composite_sources_keep_base_binding_decisions(self):
        # Composite and projected source provenance is canon's domain (the
        # similar_scope controls depend on it); deps decisions for sources in
        # the table-operand position stay exactly at their base values.
        # Proven ownership for CROSSJOIN is tested separately in
        # test_crossjoin_proven_ownership_qualify_and_rename.
        for source, table in (
            ("VALUES(Sales[Qty])", "Sales"), ("ALL(Sales[Qty])", "Sales"),
            ('SELECTCOLUMNS(Sales, "P", Sales[Price])', "Sales"),
            ("SUMMARIZE(Sales, Sales[Qty])", "Sales"),
            ("TREATAS(VALUES(Product[Price]), Sales[Qty])", "Product"),
            ("UNION(Sales, Sales)", "Sales"), ("GENERATE(Sales, Product)", "Sales"),
        ):
            with self.subTest(source=source):
                b = self.price(f"SUMX({source}, [Price])")
                self.assertEqual(b.ref, deps.Ref("column", table, "Price"))
                self.assertFalse(b.uncertain)

    def test_crossjoin_proven_ownership_qualify_and_rename(self):
        expr = "SUMX(CROSSJOIN(VALUES(Sales[Qty]), VALUES(Product[Price])), [Price])"
        # 1. Price is proven to come from Product, not Sales
        b = self.price(expr)
        self.assertEqual(b.ref, deps.Ref("column", "Product", "Price"))
        self.assertFalse(b.uncertain)
        # 2. Qty is proven to come from Sales
        b_qty = [x for x in deps.dax_bindings(expr, "Sales", self.res).bindings
                 if expr[x.start:x.end] == "[Qty]"][-1]
        self.assertEqual(b_qty.ref, deps.Ref("column", "Sales", "Qty"))
        self.assertFalse(b_qty.uncertain)
        # 3. Qualify leaves [Price] unqualified because it belongs to Product, not home table Sales
        m = self.model.measure("Control")  # measure in Sales
        self.assertEqual(rewrite._qualify(expr, m, self.model), expr)
        # 4. Renaming Product[Price] updates both the projection and the usage
        edits_prod = rename.dax_ref_edits(expr, 0, len(expr), "column", "Product", "Price", "NewPrice",
                                          cur="Sales", res=self.res)
        self.assertEqual(len(edits_prod), 2)
        self.assertEqual([expr[s:e] for s, e, _ in edits_prod], ["[Price]", "[Price]"])
        # 5. Renaming Sales[Price] leaves Product's Price untouched
        edits_sales = rename.dax_ref_edits(expr, 0, len(expr), "column", "Sales", "Price", "NewPrice",
                                           cur="Sales", res=self.res)
        self.assertEqual(edits_sales, [])

        # 6. Usage sites for both [Qty] and [Price] in iterator body
        expr_both = "SUMX(CROSSJOIN(VALUES(Sales[Qty]), VALUES(Product[Price])), [Qty] + [Price])"
        bindings_both = deps.dax_bindings(expr_both, "Sales", self.res).bindings
        b_qty_use = [x for x in bindings_both if expr_both[x.start:x.end] == "[Qty]"][-1]
        b_price_use = [x for x in bindings_both if expr_both[x.start:x.end] == "[Price]"][-1]
        self.assertEqual(b_qty_use.ref, deps.Ref("column", "Sales", "Qty"))
        self.assertFalse(b_qty_use.uncertain)
        self.assertEqual(b_price_use.ref, deps.Ref("column", "Product", "Price"))
        self.assertFalse(b_price_use.uncertain)

        # 7. Outer untaught iterator competing with inner proven CROSSJOIN binding forces uncertainty/refusal
        expr_untaught = "SUMX(VarTable, SUMX(CROSSJOIN(VALUES(Sales[Qty]), VALUES(Product[Price])), [Price]))"
        b_untaught = self.price(expr_untaught)
        self.assertEqual(b_untaught.ref, deps.Ref("column", "Product", "Price"))
        self.assertTrue(b_untaught.uncertain)
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_untaught, 0, len(expr_untaught), "column", "Product", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")

        # 8. Outer iterator resolution when inner CROSSJOIN does not project the column
        expr_outer = "SUMX(Product, SUMX(CROSSJOIN(VALUES(Sales[Qty]), VALUES(Dates[Date])), [Price]))"
        b_outer = self.price(expr_outer)
        self.assertEqual(b_outer.ref, deps.Ref("column", "Product", "Price"))
        self.assertFalse(b_outer.uncertain)

    def test_crossjoin_ambiguous_and_nested_shadowing_controls(self):
        # 1. Ambiguous projected columns: both Sales and Product project Price -> uncertain=True in both orders
        expr_ambig = "SUMX(CROSSJOIN(VALUES(Sales[Price]), VALUES(Product[Price])), [Price])"
        b_ambig = self.price(expr_ambig)
        self.assertTrue(b_ambig.uncertain)
        m = self.model.measure("Control")
        self.assertEqual(rewrite._qualify(expr_ambig, m, self.model), expr_ambig)
        # Both rename targets refuse with AMBIGUOUS
        with self.assertRaises(PbzError) as ctx1:
            rename.dax_ref_edits(expr_ambig, 0, len(expr_ambig), "column", "Sales", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx1.exception.code, "AMBIGUOUS")
        with self.assertRaises(PbzError) as ctx2:
            rename.dax_ref_edits(expr_ambig, 0, len(expr_ambig), "column", "Product", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx2.exception.code, "AMBIGUOUS")

        # Reverse operand order
        expr_ambig_rev = "SUMX(CROSSJOIN(VALUES(Product[Price]), VALUES(Sales[Price])), [Price])"
        self.assertTrue(self.price(expr_ambig_rev).uncertain)
        with self.assertRaises(PbzError) as ctx3:
            rename.dax_ref_edits(expr_ambig_rev, 0, len(expr_ambig_rev), "column", "Sales", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx3.exception.code, "AMBIGUOUS")
        with self.assertRaises(PbzError) as ctx4:
            rename.dax_ref_edits(expr_ambig_rev, 0, len(expr_ambig_rev), "column", "Product", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx4.exception.code, "AMBIGUOUS")

        # 2. Whole-table operands covering Price: ALL(Sales) and VALUES(Product[Price]) in both orders
        expr_all = "SUMX(CROSSJOIN(ALL(Sales), VALUES(Product[Price])), [Price])"
        self.assertTrue(self.price(expr_all).uncertain)
        with self.assertRaises(PbzError) as ctx5:
            rename.dax_ref_edits(expr_all, 0, len(expr_all), "column", "Sales", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx5.exception.code, "AMBIGUOUS")
        with self.assertRaises(PbzError) as ctx6:
            rename.dax_ref_edits(expr_all, 0, len(expr_all), "column", "Product", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx6.exception.code, "AMBIGUOUS")

        expr_all_rev = "SUMX(CROSSJOIN(VALUES(Product[Price]), ALL(Sales)), [Price])"
        self.assertTrue(self.price(expr_all_rev).uncertain)

        # Mixed whole-table and projected column: Sales (whole) and VALUES(Product[Price])
        expr_mixed = "SUMX(CROSSJOIN(Sales, VALUES(Product[Price])), [Price])"
        self.assertTrue(self.price(expr_mixed).uncertain)

        # 3. Non-whitelisted wrapper around CROSSJOIN forces uncertainty
        expr_distinct = "SUMX(DISTINCT(CROSSJOIN(VALUES(Sales[Qty]), VALUES(Product[Price]))), [Price])"
        self.assertTrue(self.price(expr_distinct).uncertain)

        # 4. Nested iterator shadowing: inner iterator over Dates with column Date shadows outer CROSSJOIN
        expr_nested = "SUMX(CROSSJOIN(VALUES(Sales[Qty]), VALUES(Product[Price])), SUMX(Dates, [Date]))"
        b_date = [x for x in deps.dax_bindings(expr_nested, "Sales", self.res).bindings
                  if expr_nested[x.start:x.end] == "[Date]"][-1]
        self.assertEqual(b_date.ref, deps.Ref("column", "Dates", "Date"))
        self.assertFalse(b_date.uncertain)

        # 5. Unsupported/aliased operand alongside known projection
        expr_alias = 'SUMX(CROSSJOIN(SELECTCOLUMNS(Sales, "Price", Sales[Qty]), VALUES(Product[Price])), [Price])'
        b_alias = self.price(expr_alias)
        self.assertTrue(b_alias.uncertain)

        # 6. Same table contributing duplicate column names across operands
        expr_same_tab = "SUMX(CROSSJOIN(VALUES(Sales[Price]), ALL(Sales[Price])), [Price])"
        self.assertTrue(self.price(expr_same_tab).uncertain)

        # 7. Opaque operand inside transparent FILTER wrapper propagates cj_opaque
        expr_wrapped_alias = 'SUMX(FILTER(CROSSJOIN(SELECTCOLUMNS(Sales, "Price", Sales[Qty]), VALUES(Product[Price])), TRUE()), [Price])'
        self.assertTrue(self.price(expr_wrapped_alias).uncertain)

        # 8. Column ref in wrapper predicate after CROSSJOIN popped does not re-teach row_table
        expr_calc_table = "SUMX(CALCULATETABLE(CROSSJOIN(VALUES(Sales[Qty]), VALUES(Dates[Date])), Sales[Price] > 0), [Price])"
        self.assertTrue(self.price(expr_calc_table).uncertain)

        # 9. Nested CROSSJOIN inside a filter predicate does not leak to outer iterator
        expr_nested_cj = "SUMX(FILTER(Sales, COUNTROWS(CROSSJOIN(VALUES(Product[Price]), VALUES(Dates[Date]))) > 0), [Price])"
        b_nested_cj = self.price(expr_nested_cj)
        self.assertEqual(b_nested_cj.ref, deps.Ref("column", "Sales", "Price"))
        self.assertFalse(b_nested_cj.uncertain)

        # 10. Non-whitelisted wrapper inside FILTER operand marks frame opaque
        expr_filter_selcol = 'SUMX(CROSSJOIN(FILTER(SELECTCOLUMNS(Dates, "Price", 1), TRUE()), VALUES(Product[Price])), [Price])'
        self.assertTrue(self.price(expr_filter_selcol).uncertain)

        # 11. VAR definition inside CROSSJOIN operand marks operand opaque
        expr_var_operand = 'SUMX(CROSSJOIN(VAR P = VALUES(Product[Price]) RETURN ROW("Price", 1), VALUES(Sales[Qty])), [Price])'
        self.assertTrue(self.price(expr_var_operand).uncertain)

        # 12. Predicate join with more operands does not pollute outer source join
        expr_pred_join = 'SUMX(FILTER(CROSSJOIN(VALUES(Sales[Qty]), VALUES(Product[Price])), COUNTROWS(CROSSJOIN(ROW("A", 1), ROW("B", 1), ROW("C", 1))) > 0), [Price])'
        b_pred_join = self.price(expr_pred_join)
        self.assertEqual(b_pred_join.ref, deps.Ref("column", "Product", "Price"))
        self.assertFalse(b_pred_join.uncertain)

        # 13. Non-transparent wrappers (TOPN, SAMPLE, GENERATE) around CROSSJOIN force cj_opaque
        expr_topn_cj = "SUMX(TOPN(1, CROSSJOIN(VALUES(Sales[Qty]), VALUES(Product[Price]))), [Price])"
        self.assertTrue(self.price(expr_topn_cj).uncertain)
        expr_topn_order = "SUMX(TOPN(1, CROSSJOIN(VALUES(Sales[Qty]), VALUES(Product[Price])), Sales[Qty]), [Price])"
        self.assertTrue(self.price(expr_topn_order).uncertain)
        expr_generate = "SUMX(GENERATE(Dates, CROSSJOIN(VALUES(Sales[Qty]), VALUES(Product[Price]))), [Price])"
        self.assertTrue(self.price(expr_generate).uncertain)

        # 14. Pop handler sets has_crossjoin on empty ref joins, preventing predicate re-teaching
        expr_var_cj = "SUMX(CALCULATETABLE(CROSSJOIN(VarA, VarB), Sales[Price] > 0), [Price])"
        self.assertTrue(self.price(expr_var_cj).uncertain)

        # 14a. Multi-column ALL operand marks join opaque, refusing ambiguous edit
        expr_all_multi = "SUMX(CROSSJOIN(ALL(Sales[Qty], Sales[Price]), VALUES(Product[Price])), [Price])"
        self.assertTrue(self.price(expr_all_multi).uncertain)
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_all_multi, 0, len(expr_all_multi), "column", "Product", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")

        # 15. VAR definitions in table operand force uncertainty on body resolution
        expr_var_ret_sales = "SUMX(VAR t = CROSSJOIN(VALUES(Dates[Date]), VALUES(Product[Price])) RETURN Sales, [Price])"
        self.assertTrue(self.price(expr_var_ret_sales).uncertain)

        expr_var_ret_alias = 'SUMX(VAR t = CROSSJOIN(VALUES(Sales[Qty]), VALUES(Product[Price])) RETURN SELECTCOLUMNS(t, "Price", 1), [Price])'
        self.assertTrue(self.price(expr_var_ret_alias).uncertain)

        # 16. Source-level VAR expression with constructed ROW output forces uncertainty
        expr_var_j = 'SUMX(VAR J = CROSSJOIN(VALUES(Sales[Qty]), VALUES(Product[Price])) RETURN ROW("Price", 1), [Price])'
        self.assertTrue(self.price(expr_var_j).uncertain)
        self.assertEqual(rewrite._qualify(expr_var_j, m, self.model), expr_var_j)
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_var_j, 0, len(expr_var_j), "column", "Product", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_var_j, 0, len(expr_var_j), "column", "Sales", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")

        # 16a. Parenthesized VAR expression cannot bypass opacity
        expr_wrapped_var = 'SUMX((VAR J = CROSSJOIN(VALUES(Sales[Qty]), VALUES(Product[Price])) RETURN ROW("Price", 1)), [Price])'
        self.assertTrue(self.price(expr_wrapped_var).uncertain)
        self.assertEqual(rewrite._qualify(expr_wrapped_var, m, self.model), expr_wrapped_var)
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_wrapped_var, 0, len(expr_wrapped_var), "column", "Product", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_wrapped_var, 0, len(expr_wrapped_var), "column", "Sales", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")

        # 16b. FILTER-wrapped VAR expression cannot bypass opacity
        expr_filter_var = 'SUMX(FILTER(VAR J = CROSSJOIN(VALUES(Sales[Qty]), VALUES(Product[Price])) RETURN ROW("Price", 1), TRUE()), [Price])'
        self.assertTrue(self.price(expr_filter_var).uncertain)
        self.assertEqual(rewrite._qualify(expr_filter_var, m, self.model), expr_filter_var)
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_filter_var, 0, len(expr_filter_var), "column", "Product", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_filter_var, 0, len(expr_filter_var), "column", "Sales", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")

        # 16c. Nested VAR definition retains opacity through multiple RETURNs
        expr_nested_var = 'SUMX(VAR A = VAR X = 1 RETURN CROSSJOIN(VALUES(Sales[Qty]), VALUES(Product[Price])) RETURN ROW("Price", 1), [Price])'
        self.assertTrue(self.price(expr_nested_var).uncertain)
        self.assertEqual(rewrite._qualify(expr_nested_var, m, self.model), expr_nested_var)
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_nested_var, 0, len(expr_nested_var), "column", "Product", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_nested_var, 0, len(expr_nested_var), "column", "Sales", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")

        # 17. Predicate-local VAR taint stops at predicate boundary and preserves outer provenance
        expr_pred_var = "SUMX(CROSSJOIN(FILTER(Sales, VAR N = 1 RETURN N = 1), VALUES(Dates[Date])), [Price])"
        b_pred_var = self.price(expr_pred_var)
        self.assertEqual(b_pred_var.ref, deps.Ref("column", "Sales", "Price"))
        self.assertFalse(b_pred_var.uncertain)

        # 17a. Operand predicate with real column references does not over-refuse source table
        expr_operand_pred = "SUMX(CROSSJOIN(FILTER(Sales, Sales[Qty] > 0), VALUES(Dates[Date])), [Qty])"
        b_operand_pred = [b for b in deps.dax_bindings(expr_operand_pred, "Sales", self.res).bindings
                          if expr_operand_pred[b.start:b.end] == "[Qty]" and b.qualifier is None][-1]
        self.assertEqual(b_operand_pred.ref, deps.Ref("column", "Sales", "Qty"))
        self.assertFalse(b_operand_pred.uncertain)
        edits_pred = rename.dax_ref_edits(expr_operand_pred, 0, len(expr_operand_pred), "column", "Sales", "Qty", "NewQty",
                                          cur="Sales", res=self.res)
        self.assertEqual(len(edits_pred), 2)

        # 17b. Measure collision with CROSSJOIN column forces uncertainty/refusal
        m_coll = tmdl.load_model("", texts=texts(measures={"Price": "1", "Control": CONTROL}))
        res_coll = deps.Resolver(m_coll)
        m_ctrl = m_coll.measure("Control")
        expr_meas_coll = "SUMX(CROSSJOIN(VALUES(Sales[Qty]), VALUES(Product[Price])), [Price])"
        b_meas_coll = [b for b in deps.dax_bindings(expr_meas_coll, "Sales", res_coll).bindings
                       if expr_meas_coll[b.start:b.end] == "[Price]" and b.qualifier is None][-1]
        self.assertTrue(b_meas_coll.uncertain)
        self.assertEqual(rewrite._qualify(expr_meas_coll, m_ctrl, m_coll), expr_meas_coll)
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_meas_coll, 0, len(expr_meas_coll), "column", "Product", "Price", "NewPrice",
                                 cur="Sales", res=res_coll)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_meas_coll, 0, len(expr_meas_coll), "measure", "Sales", "Price", "NewPrice",
                                 cur="Sales", res=res_coll)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")

        # 18. Extension column inside CROSSJOIN is preserved and not rebound
        expr_ext_inner = 'SUMX(CROSSJOIN(VALUES(Sales[Qty]), VALUES(Product[Price])), SUMX(ADDCOLUMNS(Dates, "Amt", 1), [Amt]))'
        b_ext_inner = [b for b in deps.dax_bindings(expr_ext_inner, "Sales", self.res).bindings
                       if expr_ext_inner[b.start:b.end] == "[Amt]"][-1]
        self.assertEqual(b_ext_inner.ref, deps.Ref("extension", "", "Amt"))
        # Renaming Product[Price] edits only the projection in VALUES, not the inner extension usage
        edits_ext = rename.dax_ref_edits(expr_ext_inner, 0, len(expr_ext_inner), "column", "Product", "Price", "NewPrice",
                                         cur="Sales", res=self.res)
        self.assertEqual(len(edits_ext), 1)

        # 18a. Competing outer extension with same name forces uncertainty regardless of home table
        expr_ext_outer = 'SUMX(ADDCOLUMNS(Dates, "Price", 1), SUMX(CROSSJOIN(VALUES(Sales[Qty]), VALUES(Product[Price])), [Price]))'
        b_ext_sales = self.price(expr_ext_outer, home="Sales")
        self.assertEqual(b_ext_sales.ref, deps.Ref("column", "Product", "Price"))
        self.assertTrue(b_ext_sales.uncertain)
        self.assertEqual(rewrite._qualify(expr_ext_outer, m, self.model), expr_ext_outer)
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_ext_outer, 0, len(expr_ext_outer), "column", "Product", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_ext_outer, 0, len(expr_ext_outer), "column", "Sales", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")

        b_ext_dates = [b for b in deps.dax_bindings(expr_ext_outer, "Dates", self.res).bindings
                       if expr_ext_outer[b.start:b.end] == "[Price]"][-1]
        self.assertEqual(b_ext_dates.ref, deps.Ref("column", "Product", "Price"))
        self.assertTrue(b_ext_dates.uncertain)
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_ext_outer, 0, len(expr_ext_outer), "column", "Product", "Price", "NewPrice",
                                 cur="Dates", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")

        # 18b. Outer model table competing with inner CROSSJOIN forces uncertainty/AMBIGUOUS refusal
        expr_outer_model = 'SUMX(Product, SUMX(CROSSJOIN(VALUES(Sales[Price]), VALUES(Dates[Date])), [Price]))'
        b_outer_model = self.price(expr_outer_model)
        self.assertEqual(b_outer_model.ref, deps.Ref("column", "Sales", "Price"))
        self.assertTrue(b_outer_model.uncertain)
        self.assertEqual(rewrite._qualify(expr_outer_model, m, self.model), expr_outer_model)
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_outer_model, 0, len(expr_outer_model), "column", "Sales", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_outer_model, 0, len(expr_outer_model), "column", "Product", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")

        # 18c. Opaque inner CROSSJOIN with outer extension forces uncertainty/AMBIGUOUS refusal
        expr_opaque_inner_ext = 'SUMX(ADDCOLUMNS(Dates, "Price", 1), SUMX(CROSSJOIN(TOPN(1, Product), VALUES(Sales[Qty])), [Price]))'
        b_opaque_ext = self.price(expr_opaque_inner_ext)
        self.assertTrue(b_opaque_ext.uncertain)
        self.assertEqual(rewrite._qualify(expr_opaque_inner_ext, m, self.model), expr_opaque_inner_ext)
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_opaque_inner_ext, 0, len(expr_opaque_inner_ext), "column", "Product", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")

        # 18d. Bare reference in TOPN and SAMPLE order-by over CROSSJOIN is uncertain and refuses rename
        for fn in ("TOPN", "SAMPLE"):
            expr_fn_orderby = f"{fn}(1, CROSSJOIN(VALUES(Sales[Qty]), VALUES(Product[Price])), [Price])"
            b_fn = [b for b in deps.dax_bindings(expr_fn_orderby, "Sales", self.res).bindings
                    if expr_fn_orderby[b.start:b.end] == "[Price]"][-1]
            self.assertTrue(b_fn.uncertain)
            with self.assertRaises(PbzError) as ctx:
                rename.dax_ref_edits(expr_fn_orderby, 0, len(expr_fn_orderby), "column", "Sales", "Price", "NewPrice",
                                     cur="Sales", res=self.res)
            self.assertEqual(ctx.exception.code, "AMBIGUOUS")
            with self.assertRaises(PbzError) as ctx:
                rename.dax_ref_edits(expr_fn_orderby, 0, len(expr_fn_orderby), "column", "Product", "Price", "NewPrice",
                                     cur="Sales", res=self.res)
            self.assertEqual(ctx.exception.code, "AMBIGUOUS")

        # 18e. Astra counterexample: inner single-column projection does not supply other columns
        expr_astra = "SUMX(CROSSJOIN(VALUES(Product[Price]), VALUES(Dates[Date])), SUMX(VALUES(Sales[Qty]), [Price]))"
        b_astra = self.price(expr_astra)
        self.assertEqual(b_astra.ref, deps.Ref("column", "Product", "Price"))
        self.assertFalse(b_astra.uncertain)
        # Renaming Product[Price] updates both the outer projection and the inner usage
        edits_astra_prod = rename.dax_ref_edits(expr_astra, 0, len(expr_astra), "column", "Product", "Price", "NewPrice",
                                                cur="Sales", res=self.res)
        self.assertEqual(len(edits_astra_prod), 2)
        # Renaming Sales[Price] produces 0 edits because Sales[Qty] does not project Price
        edits_astra_sales = rename.dax_ref_edits(expr_astra, 0, len(expr_astra), "column", "Sales", "Price", "NewPrice",
                                                 cur="Sales", res=self.res)
        self.assertEqual(len(edits_astra_sales), 0)

        # Paired control: whole-table Sales DOES supply Price, competing with outer CROSSJOIN
        expr_astra_ctrl = "SUMX(CROSSJOIN(VALUES(Product[Price]), VALUES(Dates[Date])), SUMX(Sales, [Price]))"
        b_astra_ctrl = self.price(expr_astra_ctrl)
        self.assertEqual(b_astra_ctrl.ref, deps.Ref("column", "Sales", "Price"))
        self.assertTrue(b_astra_ctrl.uncertain)
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_astra_ctrl, 0, len(expr_astra_ctrl), "column", "Sales", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")

        # 18f. Calculated column (row_home=True): implicit home row context competes with inner CROSSJOIN
        expr_calc_col = "SUMX(CROSSJOIN(VALUES(Product[Price]), VALUES(Dates[Date])), [Price])"
        b_calc_col = self.price(expr_calc_col, home="Sales", row_home=True)
        self.assertEqual(b_calc_col.ref, deps.Ref("column", "Product", "Price"))
        self.assertTrue(b_calc_col.uncertain)
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_calc_col, 0, len(expr_calc_col), "column", "Product", "Price", "NewPrice",
                                 cur="Sales", res=self.res, row_home=True)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")

        # When inner CROSSJOIN does not supply the column, outer row_home column is certain
        expr_calc_col_non_colliding = "SUMX(CROSSJOIN(VALUES(Sales[Qty]), VALUES(Dates[Date])), [Price])"
        b_non_colliding = self.price(expr_calc_col_non_colliding, home="Sales", row_home=True)
        self.assertEqual(b_non_colliding.ref, deps.Ref("column", "Sales", "Price"))
        self.assertFalse(b_non_colliding.uncertain)

        # Fable non-compete: when inner CROSSJOIN and outer row_home resolve to the exact same column, it is certain
        expr_calc_col_same = "SUMX(CROSSJOIN(VALUES(Product[Price]), VALUES(Dates[Date])), [Price])"
        b_same = self.price(expr_calc_col_same, home="Product", row_home=True)
        self.assertEqual(b_same.ref, deps.Ref("column", "Product", "Price"))
        self.assertFalse(b_same.uncertain)
        edits_same = rename.dax_ref_edits(expr_calc_col_same, 0, len(expr_calc_col_same), "column", "Product", "Price", "NewPrice",
                                          cur="Product", res=self.res, row_home=True)
        self.assertEqual(len(edits_same), 2)

        # 18g. Outer whole-table supplier correctly replaces stale single-column inner binding
        expr_outer_whole = "SUMX(Product, SUMX(CROSSJOIN(VALUES(Sales[Qty]), VALUES(Dates[Date])), SUMX(VALUES(Sales[Qty]), [Price])))"
        b_outer_whole = self.price(expr_outer_whole)
        self.assertEqual(b_outer_whole.ref, deps.Ref("column", "Product", "Price"))
        self.assertFalse(b_outer_whole.uncertain)
        self.assertEqual(rewrite._qualify(expr_outer_whole, m, self.model), expr_outer_whole)
        # Renaming Product[Price] produces exactly 1 edit, at the final bare [Price]
        edits_whole_prod = rename.dax_ref_edits(expr_outer_whole, 0, len(expr_outer_whole), "column", "Product", "Price", "NewPrice",
                                                cur="Sales", res=self.res)
        self.assertEqual(len(edits_whole_prod), 1)
        self.assertEqual([expr_outer_whole[s:e] for s, e, _ in edits_whole_prod], ["[Price]"])
        # Renaming Sales[Price] produces 0 edits
        edits_whole_sales = rename.dax_ref_edits(expr_outer_whole, 0, len(expr_outer_whole), "column", "Sales", "Price", "NewPrice",
                                                 cur="Sales", res=self.res)
        self.assertEqual(len(edits_whole_sales), 0)

        # Operand-order reversed CROSSJOIN produces identical proven ownership
        expr_outer_whole_rev = "SUMX(Product, SUMX(CROSSJOIN(VALUES(Dates[Date]), VALUES(Sales[Qty])), SUMX(VALUES(Sales[Qty]), [Price])))"
        b_outer_whole_rev = self.price(expr_outer_whole_rev)
        self.assertEqual(b_outer_whole_rev.ref, deps.Ref("column", "Product", "Price"))
        self.assertFalse(b_outer_whole_rev.uncertain)

        # 18h. Multi-column ALL supplies multiple columns, competing with outer CROSSJOIN
        expr_all_multi_inner = "SUMX(CROSSJOIN(VALUES(Product[Price]), VALUES(Dates[Date])), SUMX(ALL(Sales[Qty], Sales[Price]), [Price]))"
        b_all_multi = self.price(expr_all_multi_inner)
        self.assertEqual(b_all_multi.ref, deps.Ref("column", "Sales", "Price"))
        self.assertTrue(b_all_multi.uncertain)
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_all_multi_inner, 0, len(expr_all_multi_inner), "column", "Product", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_all_multi_inner, 0, len(expr_all_multi_inner), "column", "Sales", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")

        # 18i. Ordinary row-table branch marks uncertain when model measure collides
        m_rt_coll = tmdl.load_model("", texts=texts(measures={"Price": "1", "Control": CONTROL}))
        res_rt_coll = deps.Resolver(m_rt_coll)
        expr_rt_meas_coll = "SUMX(CROSSJOIN(VALUES(Sales[Qty]), VALUES(Dates[Date])), SUMX(Product, [Price]))"
        b_rt_meas_coll = [b for b in deps.dax_bindings(expr_rt_meas_coll, "Sales", res_rt_coll).bindings
                          if expr_rt_meas_coll[b.start:b.end] == "[Price]" and b.qualifier is None][-1]
        self.assertTrue(b_rt_meas_coll.uncertain)
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_rt_meas_coll, 0, len(expr_rt_meas_coll), "column", "Product", "Price", "NewPrice",
                                 cur="Sales", res=res_rt_coll)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")

        # 18j. Ordinary row-table branch marks uncertain when extension column collides
        expr_rt_ext_coll = 'SUMX(CROSSJOIN(VALUES(Sales[Qty]), VALUES(Dates[Date])), SUMX(ADDCOLUMNS(Product, "Price", 1), [Price]))'
        b_rt_ext_coll = self.price(expr_rt_ext_coll)
        self.assertTrue(b_rt_ext_coll.uncertain)
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_rt_ext_coll, 0, len(expr_rt_ext_coll), "column", "Product", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")

        # 18k. Astra regression: inner multi-column projection omitting Price does not supply Price
        expr_astra_discount = "SUMX(CROSSJOIN(VALUES(Product[Price]), VALUES(Dates[Date])), SUMX(ALL(Sales[Qty], Sales[Discount]), [Price]))"
        b_astra_disc = self.price(expr_astra_discount)
        self.assertEqual(b_astra_disc.ref, deps.Ref("column", "Product", "Price"))
        self.assertFalse(b_astra_disc.uncertain)
        edits_disc_prod = rename.dax_ref_edits(expr_astra_discount, 0, len(expr_astra_discount), "column", "Product", "Price", "NewPrice",
                                               cur="Sales", res=self.res)
        self.assertEqual(len(edits_disc_prod), 2)
        edits_disc_sales = rename.dax_ref_edits(expr_astra_discount, 0, len(expr_astra_discount), "column", "Sales", "Price", "NewPrice",
                                                cur="Sales", res=self.res)
        self.assertEqual(len(edits_disc_sales), 0)

        # Astra negative control: neither scope supplies Price, cannot select Sales without taint
        expr_astra_other = "SUMX(CROSSJOIN(VALUES(Product[Other]), VALUES(Dates[Date])), SUMX(ALL(Sales[Qty], Sales[Discount]), [Price]))"
        b_astra_other = self.price(expr_astra_other)
        self.assertTrue(b_astra_other.uncertain)
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_astra_other, 0, len(expr_astra_other), "column", "Sales", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")

        # 18l. Explicit schema classification: unproven shapes (SELECTCOLUMNS, DISTINCT) and CALCULATETABLE
        # Fable CALCULATETABLE: whole-table Product in arg 0 is not downgraded by Sales[Qty] in filter arg
        expr_calc_table_whole = "SUMX(CROSSJOIN(VALUES(Sales[Price]), VALUES(Dates[Date])), SUMX(CALCULATETABLE(Product, VALUES(Sales[Qty])), [Price]))"
        b_calc_whole = self.price(expr_calc_table_whole)
        self.assertTrue(b_calc_whole.uncertain)
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_calc_table_whole, 0, len(expr_calc_table_whole), "column", "Sales", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_calc_table_whole, 0, len(expr_calc_table_whole), "column", "Product", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")

        # Astra DISTINCT: unproven wrapper around VALUES cannot authorize lookup of Sales[Price]
        expr_distinct_values = "SUMX(CROSSJOIN(VALUES(Product[Other]), VALUES(Dates[Date])), SUMX(DISTINCT(VALUES(Sales[Qty])), [Price]))"
        b_distinct = self.price(expr_distinct_values)
        self.assertTrue(b_distinct.uncertain)
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_distinct_values, 0, len(expr_distinct_values), "column", "Sales", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")

        # Neon SELECTCOLUMNS: unproven projection replacement cannot authorize lookup of Sales[Price]
        expr_selectcolumns = 'SUMX(CROSSJOIN(VALUES(Product[Other]), VALUES(Dates[Date])), SUMX(SELECTCOLUMNS(Sales, "OnlyQty", Sales[Qty]), [Price]))'
        b_selcols = self.price(expr_selectcolumns)
        self.assertTrue(b_selcols.uncertain)
        with self.assertRaises(PbzError) as ctx:
            rename.dax_ref_edits(expr_selectcolumns, 0, len(expr_selectcolumns), "column", "Sales", "Price", "NewPrice",
                                 cur="Sales", res=self.res)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")


        # 19. Sibling multi-declaration VAR expression does not pollute subsequent CROSSJOIN iterator
        expr_multi_var = "SUMX(Dates, VAR A = 1 VAR B = 2 RETURN A + B) + SUMX(CROSSJOIN(VALUES(Sales[Qty]), VALUES(Product[Price])), [Price])"
        b_multi_var = self.price(expr_multi_var)
        self.assertEqual(b_multi_var.ref, deps.Ref("column", "Product", "Price"))
        self.assertFalse(b_multi_var.uncertain)
        edits_multi = rename.dax_ref_edits(expr_multi_var, 0, len(expr_multi_var), "column", "Product", "Price", "NewPrice",
                                           cur="Sales", res=self.res)
        self.assertEqual(len(edits_multi), 2)

    def test_parentheses_preserve_source_argument_role(self):
        b = self.price("SUMX(TOPN((Sales[Qty]), (Product)), [Price])")
        self.assertEqual(b.ref, deps.Ref("column", "Sales", "Price"))
        self.assertTrue(b.uncertain)
        expr = "VAR v = Product RETURN SUMX(FILTER((v), (Sales[Qty] > 0)), [Price])"
        self.assertTrue(self.price(expr).uncertain)

    def test_unknown_var_and_measure_fallback_are_conservative(self):
        self.assertTrue(self.price("VAR t = Sales RETURN SUMX(t, [Price])").uncertain)
        expr = "VAR t = Product RETURN SUMX(t, [Total Sales])"
        b = deps.dax_bindings(expr, "Sales", self.res).bindings[-1]
        self.assertEqual(b.ref.kind, "measure")
        self.assertTrue(b.uncertain)

    def test_nested_predicate_and_orderby_refs_do_not_teach(self):
        # Council round 2 (Neon): a table ref nested inside a predicate
        # function and a column ref in a TOPN order-by argument are the same
        # leak species as the brief's scalar witnesses.
        b = self.price("VAR v = Product RETURN SUMX(FILTER(v, NOT ISEMPTY(Sales)), [Price])")
        self.assertEqual(b.ref, deps.Ref("column", "Sales", "Price"))
        self.assertTrue(b.uncertain)
        b = self.price("VAR v = Product RETURN SUMX(TOPN(5, v, Sales[Price]), [Price])")
        self.assertEqual(b.ref, deps.Ref("column", "Sales", "Price"))
        self.assertTrue(b.uncertain)

    def test_nested_topn_contest_is_recorded_for_every_frame(self):
        # Diff council (Astra): contest recording must not depend on veto
        # order; the inner TOPN's scalar witness is Sales, so its Product
        # operand must not certify the occurrence.
        b = self.price("SUMX(TOPN(COUNTROWS(Product), TOPN(COUNTROWS(Sales), Product)), [Price])")
        self.assertEqual(b.ref, deps.Ref("column", "Sales", "Price"))
        self.assertTrue(b.uncertain)

    def test_extension_fallback_under_unknown_frame_is_uncertain(self):
        expr = 'VAR t = ADDCOLUMNS(Sales, "Amt", 1) RETURN SUMX(t, [Amt])'
        b = deps.dax_bindings(expr, "Sales", self.res).bindings[-1]
        self.assertEqual(b.ref.kind, "extension")
        self.assertTrue(b.uncertain)

    def test_index_omitted_relation_is_conservative(self):
        # Whether an omitted INDEX relation is defined by its ORDERBY
        # columns is engine semantics pbz does not prove; fail closed.
        b = self.price("MAXX(INDEX(1, , ORDERBY(Product[Price], DESC)), [Price])")
        self.assertEqual(b.ref, deps.Ref("column", "Sales", "Price"))
        self.assertTrue(b.uncertain)

    def test_direct_filter_table_keeps_pinned_decision(self):
        # The similar_scope CalcA poison pair pins a bare table ref used
        # directly as a CALCULATETABLE filter argument as the frame's
        # teacher; canon marks the pair uncertain and merge refuses it.
        # Recorded as a deferred residual in the item report.
        texts_with_other = dict(texts())
        texts_with_other["tables/Other.tmdl"] = (
            "table Other\n\n\tcolumn Price\n\t\tdataType: int64\n")
        res = deps.Resolver(tmdl.load_model("", texts=texts_with_other))
        expr = "SUMX(CALCULATETABLE({ 1, 2 }, Other), [Price])"
        b = [x for x in deps.dax_bindings(expr, "Sales", res).bindings
             if expr[x.start:x.end] == "[Price]" and x.qualifier is None][-1]
        self.assertEqual(b.ref, deps.Ref("column", "Other", "Price"))
        self.assertFalse(b.uncertain)


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
