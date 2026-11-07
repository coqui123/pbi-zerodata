"""Format expressions and function renames commit once, after validation.

Run: python tests/test_modeling_atomic.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import common, modeling  # noqa: E402
from pbz.common import PbzError  # noqa: E402


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _bytes(root: str) -> dict[str, bytes]:
    out = {}
    for cur, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(cur, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, root)] = f.read()
    return out


class ModelingAtomicTests(unittest.TestCase):
    def _project(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        ws = common.Workspace(root)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        project = common.Project(ws, "Mini")
        mdef = os.path.join(project.mirror_model, "definition")
        _write(os.path.join(mdef, "model.tmdl"), "model Model\n\tculture: en-US\n\nref table Sales\n")
        _write(os.path.join(mdef, "database.tmdl"), "database\n\tcompatibilityLevel: 1400\n")
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tmeasure Keep = AddOne(1)\n\tmeasure Other = 1\n"
               "\tpartition Sales = m\n\t\tmode: import\n\t\tsource = let x = 1 in x\n")
        _write(os.path.join(mdef, "functions.tmdl"),
               "function AddOne = (x : int64) => x + 1\n\tlineageTag: t1\n")
        return ws, project, mdef

    def test_unknown_ref_with_upgrade_leaves_database(self):
        ws, project, mdef = self._project()
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            modeling.format_expression(ws, project, "[Keep]", "[Nope]", upgrade=True)
        self.assertEqual(caught.exception.code, "UNKNOWN_REF")
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertIn("1400", Path(os.path.join(mdef, "database.tmdl")).read_text(encoding="utf-8"))

    def test_format_upgrade_mid_write_rolls_back(self):
        ws, project, _mdef = self._project()
        before = _bytes(ws.mirror)
        real = common.write_text
        calls = {"n": 0}

        def boom(path, text, bom=b""):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("disk")
            return real(path, text, bom)

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                modeling.format_expression(ws, project, "[Keep]", '"0"', upgrade=True)
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])

    def test_format_files_include_database(self):
        ws, project, _mdef = self._project()
        out = modeling.format_expression(ws, project, "[Keep]", '"0"', upgrade=True)
        self.assertTrue(any(f.endswith("database.tmdl") for f in out["files"]))
        self.assertTrue(any(f.endswith("Sales.tmdl") for f in out["files"]))

    def test_cross_file_rename_rolls_back(self):
        ws, project, mdef = self._project()
        before = _bytes(ws.mirror)
        real = common.write_text
        calls = {"n": 0}

        def boom(path, text, bom=b""):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("disk")
            return real(path, text, bom)

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                modeling.function_rename(ws, project, "AddOne", "AddTwo")
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(_bytes(ws.mirror), before)
        text = Path(os.path.join(mdef, "functions.tmdl")).read_text(encoding="utf-8")
        sales = Path(os.path.join(mdef, "tables", "Sales.tmdl")).read_text(encoding="utf-8")
        self.assertIn("AddOne", text)
        self.assertIn("AddOne", sales)
        self.assertNotIn("AddTwo", text + sales)


if __name__ == "__main__":
    unittest.main()
