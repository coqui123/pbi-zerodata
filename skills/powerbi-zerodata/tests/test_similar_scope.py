"""Offline checks that scoped canonical keys cannot authorize unsafe merges.

Nested iterator references must preserve their row-table binding, and unresolved
references must block automatic consolidation even when canonical keys match.

Run: python tests/test_similar_scope.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import common, consolidate, deps, similar, tmdl  # noqa: E402
from pbz.common import PbzError  # noqa: E402

SALES = (
    "table Sales\n\n"
    "\tcolumn Price\n\t\tdataType: int64\n\t\tsourceColumn: Price\n\n"
    "\tmeasure IterA = SUMX(Sales, SUMX(Other, [Price]))\n\n"
    "\tmeasure IterB = SUMX(Sales, SUMX(Other, Sales[Price]))\n\n"
    "\tmeasure IterC = SUMX(Sales, [Price] + SUMX(Other, [Price]))\n\n"
    "\tmeasure IterD = SUMX(Sales, [Price] + SUMX(Other, Sales[Price]))\n\n"
    "\tmeasure UnresA = SUMX(Sales, [Nope])\n\n"
    "\tmeasure UnresB = SUMX(Sales, [Nope])\n\n"
    "\tmeasure CtrlA = SUM(Sales[Price])\n\n"
    "\tmeasure CtrlB = SUM(Sales[Price])\n\n"
    "\tmeasure VarA = VAR v = Other RETURN SUMX(v, [Price])\n\n"
    "\tmeasure VarB = VAR v = Other RETURN SUMX(v, Sales[Price])\n\n"
    "\tmeasure AliasUnres = 'Missing'[CtrlA]\n\n"
    "\tmeasure AliasOk = [CtrlA]\n\n"
    "\tmeasure WrapA = VAR v = Other RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), [Price])\n\n"
    "\tmeasure WrapB = VAR v = Other RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), Sales[Price])\n\n"
    "\tmeasure TabA = COUNTROWS('Missing')\n\n"
    "\tmeasure TabB = COUNTROWS('Missing')\n\n"
    "\tmeasure TabCtrlA = COUNTROWS(Sales)\n\n"
    "\tmeasure TabCtrlB = COUNTROWS(Sales)\n\n"
    "\tmeasure TopA = VAR v = Other RETURN SUMX(TOPN(MAX(Sales[Price]), v), [Price])\n\n"
    "\tmeasure TopB = VAR v = Other RETURN SUMX(TOPN(MAX(Sales[Price]), v), Sales[Price])\n\n"
    "\tmeasure ScalarA = VAR n = 0 RETURN SUMX(FILTER(Sales, Sales[Price] > n), [Price])\n\n"
    "\tmeasure ScalarB = VAR n = 0 RETURN SUMX(FILTER(Sales, Sales[Price] > n), [Price])\n\n"
    "\tmeasure ExprA = VAR v = FILTER(Other, TRUE()) RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), [Price])\n\n"
    "\tmeasure ExprB = VAR v = FILTER(Other, TRUE()) RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), Sales[Price])\n\n"
    "\tmeasure ChainA = VAR a = Other VAR v = a RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), [Price])\n\n"
    "\tmeasure ChainB = VAR a = Other VAR v = a RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), Sales[Price])\n\n"
    "\tmeasure ValuesA = VAR v = VALUES(Other[Price]) RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), [Price])\n\n"
    "\tmeasure ValuesB = VAR v = VALUES(Other[Price]) RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), Sales[Price])\n\n"
    "\tmeasure DerivedA = VAR a = Other VAR v = FILTER(a, TRUE()) RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), [Price])\n\n"
    "\tmeasure DerivedB = VAR a = Other VAR v = FILTER(a, TRUE()) RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), Sales[Price])\n\n"
    "\tmeasure ScalarQA = VAR n = MAX('Sales'[Price]) RETURN SUMX(FILTER(Sales, Sales[Price] > n), [Price])\n\n"
    "\tmeasure ScalarQB = VAR n = MAX('Sales'[Price]) RETURN SUMX(FILTER(Sales, Sales[Price] > n), [Price])\n\n"
    "\tmeasure CrossA = VAR src = Other VAR v = CROSSJOIN(src, { 1 }) RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), [Price])\n\n"
    "\tmeasure CrossB = VAR src = Other VAR v = CROSSJOIN(src, { 1 }) RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), Sales[Price])\n\n"
    "\tmeasure CtorA = VAR v = { 1, 2 } RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), [Price])\n\n"
    "\tmeasure CtorB = VAR v = { 1, 2 } RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), Sales[Price])\n\n"
    "\tmeasure CountA = VAR n = COUNTROWS(Other) RETURN SUMX(FILTER(Sales, Sales[Price] > n), [Price])\n\n"
    "\tmeasure CountB = VAR n = COUNTROWS(Other) RETURN SUMX(FILTER(Sales, Sales[Price] > n), [Price])\n\n"
    "\tmeasure ScopeA = (VAR v = Other RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), [Price])) + (VAR v = 1 RETURN v)\n\n"
    "\tmeasure ScopeB = (VAR v = Other RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), Sales[Price])) + (VAR v = 1 RETURN v)\n\n"
    "\tmeasure InlineA = SUMX(TOPN(MAX(Sales[Price]), Other), [Price])\n\n"
    "\tmeasure InlineB = SUMX(TOPN(MAX(Sales[Price]), Other), Sales[Price])\n\n"
)
OTHER = (
    "table Other\n\n"
    "\tcolumn Price\n\t\tdataType: int64\n\t\tsourceColumn: Price\n\n"
)
MODEL = "model Model\n\tculture: en-US\n\nref table Sales\nref table Other\n"
DATABASE = "database\n\tcompatibilityLevel: 1400\n"


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


class SimilarScopeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        self.ws = common.Workspace(root)
        self.ws.private = os.path.join(root, "private")
        os.makedirs(self.ws.private)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        self.project = common.Project(self.ws, "Mini")
        mdef = os.path.join(self.project.mirror_model, "definition")
        _write(os.path.join(mdef, "tables", "Sales.tmdl"), SALES)
        _write(os.path.join(mdef, "tables", "Other.tmdl"), OTHER)
        _write(os.path.join(mdef, "model.tmdl"), MODEL)
        _write(os.path.join(mdef, "database.tmdl"), DATABASE)
        os.makedirs(self.project.model_dir, exist_ok=True)
        self.model = tmdl.load_model(mdef)
        self.resolver = deps.Resolver(self.model)
        self.assertEqual(set(self.model.tables), {"Sales", "Other"})
        for table in ("Sales", "Other"):
            self.assertIsNotNone(self.model.column(table, "Price"))
            self.assertIsNone(self.model.column(table, "Nope"))
        self.assertIsNone(self.model.measure("Price"))
        self.assertIsNone(self.model.measure("Nope"))
        self.assertEqual(
            {(m.table, m.name) for m in self.model.all_measures()},
            {("Sales", name) for name in
             ("IterA", "IterB", "IterC", "IterD", "UnresA", "UnresB", "CtrlA", "CtrlB",
              "VarA", "VarB", "AliasUnres", "AliasOk", "WrapA", "WrapB",
              "TabA", "TabB", "TabCtrlA", "TabCtrlB", "TopA", "TopB", "ScalarA", "ScalarB",
              "ExprA", "ExprB", "ChainA", "ChainB", "ValuesA", "ValuesB",
              "DerivedA", "DerivedB", "ScalarQA", "ScalarQB", "CrossA", "CrossB",
              "CtorA", "CtorB", "CountA", "CountB", "ScopeA", "ScopeB", "InlineA", "InlineB")},
        )

    def _canon(self, name):
        measure = self.model.measure(name)
        self.assertIsNotNone(measure)
        return similar.canon(measure.expression, "Sales", self.resolver)

    def _key(self, name):
        return self._canon(name).words("hash")

    def test_nested_iterator_occurrences_get_different_keys(self):
        key_a, key_b = self._key("IterA"), self._key("IterB")
        self.assertNotEqual(key_a, key_b)
        self.assertIn("other[price]", key_a)
        self.assertIn("sales[price]", key_b)
        self.assertEqual(
            key_a,
            similar.canon("SUMX(Sales, SUMX(Other, Other[Price]))",
                          "Sales", self.resolver).words("hash"),
        )

    def test_each_occurrence_binds_its_own_row_context(self):
        key_c, key_d = self._key("IterC"), self._key("IterD")
        self.assertNotEqual(key_c, key_d)
        self.assertIn("sales[price]", key_c)
        self.assertIn("other[price]", key_c)
        self.assertEqual(
            key_c,
            similar.canon("SUMX(Sales, Sales[Price] + SUMX(Other, Other[Price]))",
                          "Sales", self.resolver).words("hash"),
        )

    def test_merge_preview_refuses_scoped_pair_without_force(self):
        with self.assertRaises(PbzError) as caught:
            consolidate.merge(self.ws, self.project, "IterB", "IterA", dry_run=True)
        self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_unresolved_occurrence_blocks_automatic_merge(self):
        self.assertEqual(self._key("UnresA"), self._key("UnresB"))
        with self.assertRaises(PbzError) as caught:
            consolidate.merge(self.ws, self.project, "UnresB", "UnresA", dry_run=True)
        self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_identical_control_still_previews_identical(self):
        result = consolidate.merge(self.ws, self.project, "CtrlB", "CtrlA", dry_run=True)
        self.assertEqual(result["equivalence"], "identical")
        self.assertEqual(result["result"], "DRY_RUN")

    def test_var_table_iterator_occurrence_is_uncertain(self):
        self.assertEqual(self._key("VarA"), self._key("VarB"))
        self.assertTrue(self._canon("VarA").uncertain)
        with self.assertRaises(PbzError) as caught:
            consolidate.merge(self.ws, self.project, "VarB", "VarA", dry_run=True)
        self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_uncertain_alias_is_refused(self):
        with self.assertRaises(PbzError) as caught:
            consolidate.merge(self.ws, self.project, "AliasUnres", "CtrlA", dry_run=True)
        self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_resolved_alias_still_previews_alias(self):
        result = consolidate.merge(self.ws, self.project, "AliasOk", "CtrlA", dry_run=True)
        self.assertEqual(result["equivalence"], "alias")
        self.assertEqual(result["result"], "DRY_RUN")

    def test_wrapped_var_source_stays_uncertain(self):
        self.assertEqual(self._key("WrapA"), self._key("WrapB"))
        self.assertTrue(self._canon("WrapA").uncertain)
        with self.assertRaises(PbzError) as caught:
            consolidate.merge(self.ws, self.project, "WrapB", "WrapA", dry_run=True)
        self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_unresolved_table_token_blocks_identical_merge(self):
        self.assertEqual(self._key("TabA"), self._key("TabB"))
        self.assertTrue(self._canon("TabA").uncertain)
        with self.assertRaises(PbzError) as caught:
            consolidate.merge(self.ws, self.project, "TabB", "TabA", dry_run=True)
        self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")
        result = consolidate.merge(self.ws, self.project, "TabCtrlB", "TabCtrlA", dry_run=True)
        self.assertEqual(result["equivalence"], "identical")

    def test_table_var_after_reference_still_poisons(self):
        self.assertEqual(self._key("TopA"), self._key("TopB"))
        self.assertTrue(self._canon("TopA").uncertain)
        with self.assertRaises(PbzError) as caught:
            consolidate.merge(self.ws, self.project, "TopB", "TopA", dry_run=True)
        self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_expression_defined_table_var_still_poisons(self):
        for dup, into in (("ExprB", "ExprA"), ("ChainB", "ChainA")):
            with self.subTest(into=into):
                self.assertEqual(self._key(dup), self._key(into))
                self.assertTrue(self._canon(into).uncertain)
                with self.assertRaises(PbzError) as caught:
                    consolidate.merge(self.ws, self.project, dup, into, dry_run=True)
                self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_column_fed_and_derived_table_vars_still_poison(self):
        for dup, into in (("ValuesB", "ValuesA"), ("DerivedB", "DerivedA")):
            with self.subTest(into=into):
                self.assertEqual(self._key(dup), self._key(into))
                self.assertTrue(self._canon(into).uncertain)
                with self.assertRaises(PbzError) as caught:
                    consolidate.merge(self.ws, self.project, dup, into, dry_run=True)
                self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_unlisted_table_forms_still_poison(self):
        for dup, into in (("CrossB", "CrossA"), ("CtorB", "CtorA")):
            with self.subTest(into=into):
                self.assertEqual(self._key(dup), self._key(into))
                self.assertTrue(self._canon(into).uncertain)
                with self.assertRaises(PbzError) as caught:
                    consolidate.merge(self.ws, self.project, dup, into, dry_run=True)
                self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_sibling_scope_and_inline_source_still_poison(self):
        for dup, into in (("ScopeB", "ScopeA"), ("InlineB", "InlineA")):
            with self.subTest(into=into):
                self.assertEqual(self._key(dup), self._key(into))
                self.assertTrue(self._canon(into).uncertain)
                with self.assertRaises(PbzError) as caught:
                    consolidate.merge(self.ws, self.project, dup, into, dry_run=True)
                self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_table_consuming_scalar_var_does_not_poison(self):
        self.assertFalse(self._canon("CountA").uncertain)
        result = consolidate.merge(self.ws, self.project, "CountB", "CountA", dry_run=True)
        self.assertEqual(result["equivalence"], "identical")

    def test_quoted_qualified_scalar_var_does_not_poison(self):
        self.assertFalse(self._canon("ScalarQA").uncertain)
        result = consolidate.merge(self.ws, self.project, "ScalarQB", "ScalarQA", dry_run=True)
        self.assertEqual(result["equivalence"], "identical")

    def test_scalar_var_does_not_block_learned_row_table(self):
        self.assertFalse(self._canon("ScalarA").uncertain)
        result = consolidate.merge(self.ws, self.project, "ScalarB", "ScalarA", dry_run=True)
        self.assertEqual(result["equivalence"], "identical")

    def test_similar_run_does_not_group_scoped_pair(self):
        groups = similar.run(self.ws, self.project)["identical"]
        self.assertTrue(any({"[CtrlA]", "[CtrlB]"} <= set(group["measures"])
                            for group in groups))
        for group in groups:
            self.assertFalse({"[IterA]", "[IterB]"} <= set(group["measures"]))


if __name__ == "__main__":
    unittest.main()
