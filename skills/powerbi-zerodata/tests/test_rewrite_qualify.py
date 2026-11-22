"""Offline regression checks for DAX qualification and candidate validation.

Run: python3 tests/test_rewrite_qualify.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import common, mirror, rewrite, tmdl  # noqa: E402
from pbz.common import PbzError  # noqa: E402


class QualifyUnitTests(unittest.TestCase):
    def setUp(self):
        self.model = tmdl.load_model("", texts={
            "tables/Sales.tmdl": (
                "table Sales\n\n"
                "\tmeasure M = SUM([Amount])\n\n"
                "\tmeasure Margin = SUM([Amount]) * 2\n\n"
                "\tcolumn Amount\n\t\tdataType: double\n\n"
                "\tcolumn K\n\t\tdataType: int64\n"
            ),
            "tables/Other.tmdl": (
                "table Other\n\n"
                "\tcolumn Amount\n\t\tdataType: double\n\n"
                "\tcolumn Margin\n\t\tdataType: double\n\n"
                "\tcolumn K\n\t\tdataType: int64\n"
            ),
            "tables/BobsSales.tmdl": (
                "table 'Bob''s Sales'\n\n"
                "\tmeasure 'Bob Margin' = SUM([Amount])\n\n"
                "\tcolumn Amount\n\t\tdataType: double\n"
            ),
        })
        self.assertEqual(self.model.measure("Bob Margin").table, "Bob's Sales")
        self.measure_m = self.model.measure("M")

    def q(self, expr):
        return rewrite._qualify(expr, self.measure_m, self.model)

    def test_bare_home_column_gains_qualifier(self):
        self.assertEqual(self.q("SUM([Amount])"), "SUM(Sales[Amount])")

    def test_already_qualified_home_unchanged(self):
        self.assertEqual(self.q("SUM(Sales[Amount])"), "SUM(Sales[Amount])")

    def test_already_qualified_other_unchanged(self):
        self.assertEqual(self.q("SUM(Other[Amount])"), "SUM(Other[Amount])")

    def test_quoted_qualified_with_trivia_unchanged(self):
        self.assertEqual(self.q("SUM('Sales' [Amount])"), "SUM('Sales' [Amount])")

    def test_qualify_is_idempotent(self):
        self.assertEqual(self.q(self.q("SUM([Amount])")), self.q("SUM([Amount])"))
        self.assertEqual(self.q(self.q("SUM(Sales[Amount])")), "SUM(Sales[Amount])")

    def test_uncertain_binding_unchanged(self):
        self.assertEqual(self.q("SUM(Missing[Amount])"), "SUM(Missing[Amount])")

    def test_constructed_table_row_contexts_unchanged(self):
        for expr in (
            'SUMX(ADDCOLUMNS(VALUES(Sales[K]), "X", 1), [Amount])',
            'VAR t = SELECTCOLUMNS(Other, "Amount", Other[K]) RETURN SUMX(t, [Amount])',
            'SUMX(ADDCOLUMNS(VALUES(Other[K]), "Amount", 1), [Amount])',
        ):
            with self.subTest(expr=expr):
                self.assertEqual(self.q(expr), expr)

    def test_qualified_measure_reference_is_stripped(self):
        self.assertEqual(self.q("'Sales'[Margin]"), "[Margin]")

    def test_unquoted_measure_qualifier_is_not_stripped(self):
        expr = "SUMX(Other, Sales[Margin])"
        self.assertEqual(self.q(expr), expr)

    def test_strip_decodes_escaped_apostrophes(self):
        self.assertEqual(self.q("'Bob''s Sales'[Bob Margin]"), "[Bob Margin]")

    def test_strip_preserves_intervening_comment(self):
        self.assertEqual(self.q("'Sales' /* why */ [Margin]"), " /* why */ [Margin]")

    def test_constructed_name_without_call_still_qualifies(self):
        self.assertEqual(self.q("SUM([Amount]) + ADDCOLUMNS"), "SUM(Sales[Amount]) + ADDCOLUMNS")


class QualifyRunValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pbzqualify-")
        self.addCleanup(self.tmp.cleanup)
        private = os.path.join(self.tmp.name, "private")
        os.environ["PBZ_PRIVATE_ROOT"] = private
        self.addCleanup(lambda: os.environ.pop("PBZ_PRIVATE_ROOT", None))
        self.root = os.path.join(self.tmp.name, "ws")
        test_parity.build(self.root)
        self.ws = common.Workspace(self.root)
        self.ws.private = private
        self.project = self.ws.project()
        mirror.pull(self.ws, self.project)

    def _model(self):
        return tmdl.load_model(os.path.join(self.project.mirror_model, "definition"))

    def _tree_bytes(self, root):
        found = {}
        for dirpath, dirnames, names in os.walk(root):
            dirnames.sort()
            for name in sorted(names):
                path = os.path.join(dirpath, name)
                with open(path, "rb") as handle:
                    found[os.path.relpath(path, root)] = handle.read()
        return found

    def _insert_measure(self, name, expression, tag):
        path = os.path.join(self.project.mirror_model, "definition", "tables", "Sales.tmdl")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        block = f"\tmeasure '{name}' = {expression}\n\t\tlineageTag: {tag}\n\n"
        marker = "\tpartition Sales = m\n"
        self.assertIn(marker, text)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text.replace(marker, block + marker, 1))

    def _closed_desktop(self):
        return patch("pbz.desktop.require_port", side_effect=PbzError("ENGINE_UNAVAILABLE", "closed"))

    def test_failed_candidate_validation_leaves_bytes_untouched(self):
        self._insert_measure("Qual Target", "SUM([Amount])", "10000000-0000-0000-0000-0000000001a1")
        before = self._tree_bytes(self.ws.mirror)
        calls = []

        def fail_candidate(*args):
            calls.append(args)
            return {"status": "FAIL", "errors": 1}

        with self._closed_desktop(), patch.object(
            rewrite, "compile_candidate", side_effect=fail_candidate, create=True
        ):
            with self.assertRaises(PbzError) as caught:
                rewrite.run(self.ws, self.project, "qualify", "[Qual Target]")
            self.assertEqual(caught.exception.code, "REFUSED")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][:2], (self.ws, self.project))
            self.assertEqual(calls[0][2].name, "Qual Target")
            self.assertEqual(calls[0][3], "SUM(Sales[Amount])")
        self.assertEqual(self._tree_bytes(self.ws.mirror), before)

    def test_passing_candidate_validation_writes(self):
        self._insert_measure("Qual Target", "SUM([Amount])", "10000000-0000-0000-0000-0000000001a3")
        with self._closed_desktop(), patch.object(
            rewrite, "compile_candidate", return_value={"status": "PASS"}, create=True
        ):
            out = rewrite.run(self.ws, self.project, "qualify", "[Qual Target]")
        self.assertEqual(out["result"], "CHANGED")
        self.assertEqual(self._model().measure("Qual Target").expression.strip(), "SUM(Sales[Amount])")

    def test_unavailable_validation_proceeds_and_reports_not_compiled(self):
        self._insert_measure("Qual Target", "SUM([Amount])", "10000000-0000-0000-0000-0000000001a2")
        with self._closed_desktop(), patch.object(
            rewrite, "compile_candidate", return_value={"status": "NOT_RUN"}, create=True
        ):
            out = rewrite.run(self.ws, self.project, "qualify", "[Qual Target]")
        self.assertEqual(out["result"], "CHANGED")
        self.assertEqual(self._model().measure("Qual Target").expression.strip(), "SUM(Sales[Amount])")


if __name__ == "__main__":
    unittest.main()
