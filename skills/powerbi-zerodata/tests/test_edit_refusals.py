"""Offline checks that invalid filter and relationship requests write nothing.

Run: python tests/test_edit_refusals.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import cli, common, filters, tmdl  # noqa: E402
from pbz.common import PbzError  # noqa: E402

SALES = "table Sales\n\n\tcolumn Region\n\t\tdataType: string\n\n\tmeasure Amount = 1\n\n"
MODEL = "model Model\n\tculture: en-US\n\nref table Sales\n"
DATABASE = "database\n\tcompatibilityLevel: 1400\n"


def _bytes(root: str) -> dict[str, bytes]:
    out = {}
    for cur, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(cur, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, root)] = f.read()
    return out


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


class EditRefusalTests(unittest.TestCase):
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
        _write(os.path.join(mdef, "model.tmdl"), MODEL)
        _write(os.path.join(mdef, "database.tmdl"), DATABASE)
        _write(os.path.join(self.project.mirror_report, "definition", "report.json"), "{}\n")
        self.model = tmdl.load_model(mdef)
        self.before = _bytes(self.ws.mirror)

    def _refuse(self, fn):
        with patch("pbz.common.write_text", side_effect=AssertionError("wrote")):
            with self.assertRaises(PbzError) as caught:
                fn()
        self.assertEqual(_bytes(self.ws.mirror), self.before)
        return caught.exception

    def test_top_n_and_ranking_refuse(self):
        err = self._refuse(lambda: filters.add(
            self.ws, self.project, "'Sales'[Region]", report=True, top=0, by="[Amount]"))
        self.assertEqual(err.code, "BAD_REQUEST")
        self.assertNotIn("0", err.detail)
        err = self._refuse(lambda: filters.build(self.model, "'Sales'[Region]", top=-3, by="[Amount]"))
        self.assertNotIn("-3", err.detail)
        err = self._refuse(lambda: filters.build(self.model, "'Sales'[Region]", by="[Amount]"))
        self.assertIn("ranking", err.detail)
        err = self._refuse(lambda: filters.build(self.model, "'Sales'[Region]", bottom=True))
        self.assertIn("ranking", err.detail)

    def test_non_finite_and_bad_datetime_refuse(self):
        err = self._refuse(lambda: filters.literal("nan", "double"))
        self.assertNotIn("nan", err.detail.lower())
        err = self._refuse(lambda: filters.literal("inf", "decimal"))
        self.assertNotIn("inf", err.detail.lower())
        err = self._refuse(lambda: filters.literal("2020-02-31", "datetime"))
        self.assertNotIn("2020-02-31", err.detail)
        err = self._refuse(lambda: filters.build(self.model, "'Sales'[Region]", where=["nope"]))
        self.assertNotIn("nope", err.detail)

    def test_valid_values_still_work(self):
        self.assertEqual(filters.literal("«lit#1»", "int64"), "«lit#1»")
        self.assertEqual(filters.literal("1.5", "double"), "1.5D")
        self.assertTrue(filters.literal("2020-01-02", "datetime").startswith("datetime'"))
        built = filters.build(self.model, "'Sales'[Region]", top=3, by="[Amount]")
        self.assertEqual(built["type"], "TopN")
        added = filters.add(self.ws, self.project, "'Sales'[Region]", report=True, values_in=["East"])
        self.assertEqual(added["action"], "add")
        self.assertTrue(added["files"])

    def test_opposing_relationship_flags(self):
        parser = cli.argument_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["relationship", "set", "'A'[A]", "'B'[B]", "--active", "--inactive"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["relationship", "set", "'A'[A]", "'B'[B]", "--both", "--single"])
        args = parser.parse_args(["relationship", "set", "'A'[A]", "'B'[B]", "--active", "--single"])
        self.assertTrue(args.active)
        self.assertTrue(args.single)


if __name__ == "__main__":
    unittest.main()
