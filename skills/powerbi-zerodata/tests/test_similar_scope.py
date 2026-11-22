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
              "VarA", "VarB", "AliasUnres", "AliasOk")},
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

    def test_similar_run_does_not_group_scoped_pair(self):
        groups = similar.run(self.ws, self.project)["identical"]
        self.assertTrue(any({"[CtrlA]", "[CtrlB]"} <= set(group["measures"])
                            for group in groups))
        for group in groups:
            self.assertFalse({"[IterA]", "[IterB]"} <= set(group["measures"]))


if __name__ == "__main__":
    unittest.main()
