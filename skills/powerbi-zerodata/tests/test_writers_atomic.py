"""Optional writers: refresh policy and DAX format apply commit once.

Run: python tests/test_writers_atomic.py
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

from pbz import common, daxfmt, refreshpolicy  # noqa: E402
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


class WritersAtomicTests(unittest.TestCase):
    def _project(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        ws = common.Workspace(root)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        project = common.Project(ws, "Mini")
        mdef = os.path.join(project.mirror_model, "definition")
        _write(os.path.join(mdef, "model.tmdl"),
               "model Model\n\tculture: en-US\n\nref table Sales\nref table Other\n")
        _write(os.path.join(mdef, "database.tmdl"), "database\n\tcompatibilityLevel: 1400\n")
        for name in ("Sales", "Other"):
            _write(os.path.join(mdef, "tables", f"{name}.tmdl"),
                   f"table {name}\n\tmeasure Keep = 1+2\n"
                   f"\tpartition {name} = m\n\t\tmode: import\n\t\tsource = let x = 1 in x\n")
        return ws, project

    def test_format_apply_rolls_back(self):
        ws, project = self._project()
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
                daxfmt.run(ws, project, apply=True)
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])

    def test_format_apply_writes_both(self):
        ws, project = self._project()
        out = daxfmt.run(ws, project, apply=True)
        self.assertGreaterEqual(out["formatted"], 2)
        for name in ("Sales", "Other"):
            text = Path(os.path.join(project.mirror_model, "definition", "tables", f"{name}.tmdl")).read_text(encoding="utf-8")
            self.assertIn("1 + 2", text)

    def test_refresh_policy_rolls_back_expressions(self):
        ws, project = self._project()
        mdef = os.path.join(project.mirror_model, "definition")
        expr = os.path.join(mdef, "expressions.tmdl")
        _write(expr, "expression Kept = 1\n")
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n"
               "\tcolumn When\n\t\tdataType: dateTime\n\t\tsourceColumn: When\n"
               "\tpartition Sales = m\n\t\tmode: import\n"
               "\t\tsource = let x = 1 in x\n")
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
                refreshpolicy.set_policy(ws, project, "Sales", "When", 1, "year", 1, "month")
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(Path(expr).read_text(encoding="utf-8"), "expression Kept = 1\n")
        self.assertNotIn("refreshPolicy", Path(os.path.join(mdef, "tables", "Sales.tmdl")).read_text(encoding="utf-8"))
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])


if __name__ == "__main__":
    unittest.main()
