"""Date-table scaffolds validate every relationship, then commit once.

Run: python tests/test_scaffold_atomic.py
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

from pbz import common, scaffold  # noqa: E402
from pbz.common import PbzError  # noqa: E402

DATABASE = "database\n\tcompatibilityLevel: 1400\n"


def _table(name: str, columns: list[tuple[str, str]]) -> str:
    cols = "".join(
        f"\tcolumn {col}\n\t\tdataType: {dtype}\n\t\tsourceColumn: {col}\n" for col, dtype in columns)
    return f"table {name}\n{cols}\tpartition {name} = m\n\t\tmode: import\n\t\tsource = let x = 1 in x\n"


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


class ScaffoldAtomicTests(unittest.TestCase):
    def _project(self, *, model=True, tables=("Sales", "Orders")):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        ws = common.Workspace(root)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        project = common.Project(ws, "Mini")
        mdef = os.path.join(project.mirror_model, "definition")
        refs = "".join(f"ref table {name}\n" for name in tables)
        if model:
            _write(os.path.join(mdef, "model.tmdl"), "model Model\n\tculture: en-US\n\n" + refs)
        _write(os.path.join(mdef, "database.tmdl"), DATABASE)
        for name in tables:
            cols = [("When", "dateTime")]
            if name == "Sales":
                cols.append(("Ship", "dateTime"))
            _write(os.path.join(mdef, "tables", f"{name}.tmdl"), _table(name, cols))
        return ws, project

    def test_second_relate_invalid_writes_nothing(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            scaffold.date_table(ws, project, "Calendar", 2020, 2020, ["'Sales'[When]", "'Missing'[When]"])
        self.assertIn(caught.exception.code, {"NOT_FOUND", "BAD_REQUEST"})
        self.assertEqual(_bytes(ws.mirror), before)

    def test_duplicate_endpoint_refused(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            scaffold.date_table(ws, project, "Calendar", 2020, 2020, ["'Sales'[When]", "'Sales'[When]"])
        self.assertEqual(caught.exception.code, "DUPLICATE")
        self.assertEqual(_bytes(ws.mirror), before)

    def test_second_active_path_refused(self):
        ws, project = self._project(tables=("Sales",))
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            scaffold.date_table(ws, project, "Calendar", 2020, 2020, ["'Sales'[When]", "'Sales'[Ship]"])
        self.assertEqual(caught.exception.code, "AMBIGUOUS_PATH")
        self.assertEqual(_bytes(ws.mirror), before)

    def test_two_facts_one_relationships_file(self):
        ws, project = self._project()
        out = scaffold.date_table(ws, project, "Calendar", 2020, 2020, ["'Sales'[When]", "'Orders'[When]"])
        self.assertTrue(any(f.endswith("relationships.tmdl") for f in out["files"]))
        text = Path(os.path.join(project.mirror_model, "definition", "relationships.tmdl")).read_text(encoding="utf-8")
        self.assertEqual(text.count("relationship "), 2)
        self.assertEqual(len(out["relationships"]), 2)

    def test_missing_model_refused(self):
        ws, project = self._project(model=False)
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            scaffold.date_table(ws, project, "Calendar", 2020, 2020)
        self.assertEqual(caught.exception.code, "NO_MIRROR")
        self.assertEqual(_bytes(ws.mirror), before)

    def test_mid_write_rolls_back(self):
        ws, project = self._project(tables=("Sales",))
        before = _bytes(ws.mirror)
        real = common.write_text
        calls = {"n": 0}

        def boom(path, text, bom=b"", *, root=None, **_kw):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("disk")
            return real(path, text, bom, root=root)

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                scaffold.date_table(ws, project, "Calendar", 2020, 2020, ["'Sales'[When]"])
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])


if __name__ == "__main__":
    unittest.main()
