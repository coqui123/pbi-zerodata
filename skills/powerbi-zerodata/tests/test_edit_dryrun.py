"""Filter, relationship and interaction edits: dry-run, one commit, refuse a bad sibling.

Run: python tests/test_edit_dryrun.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import common, filters, relate  # noqa: E402
from pbz.common import PbzError  # noqa: E402

MODEL = "model Model\n\tculture: en-US\n\nref table Sales\n"
DATABASE = "database\n\tcompatibilityLevel: 1400\n"
SALES = (
    "table Sales\n"
    "\tcolumn Name\n\t\tdataType: string\n\t\tsourceColumn: Name\n"
    "\tcolumn When\n\t\tdataType: dateTime\n\t\tsourceColumn: When\n"
    "\tmeasure Keep = 1\n"
    "\tpartition Sales = m\n\t\tmode: import\n\t\tsource = let x = 1 in x\n"
)
DATE = (
    "table Date\n"
    "\tcolumn Date\n\t\tdataType: dateTime\n\t\tisKey\n\t\tsourceColumn: Date\n"
    "\tpartition Date = calculated\n\t\tmode: import\n\t\tsource = CALENDAR(DATE(2020, 1, 1), DATE(2020, 1, 2))\n"
)
RELS = ""


def _write(path: str, data: str | bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(data, bytes):
        with open(path, "wb") as f:
            f.write(data)
        return
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(data)


def _bytes(root: str) -> dict[str, bytes]:
    out = {}
    for cur, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(cur, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, root)] = f.read()
    return out


def _visual(name):
    return {"name": name, "position": {"x": 0, "y": 0, "width": 100, "height": 40},
            "visual": {"visualType": "card"}}


class EditDryRunTests(unittest.TestCase):
    def _project(self, *, bad_visual=False, bad_tmdl=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        ws = common.Workspace(root)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        project = common.Project(ws, "Mini")
        mdef = os.path.join(project.mirror_model, "definition")
        _write(os.path.join(mdef, "model.tmdl"), MODEL + "ref table Date\n")
        _write(os.path.join(mdef, "database.tmdl"), DATABASE)
        _write(os.path.join(mdef, "tables", "Sales.tmdl"), SALES)
        _write(os.path.join(mdef, "tables", "Date.tmdl"), DATE)
        _write(os.path.join(mdef, "relationships.tmdl"), RELS)
        if bad_tmdl:
            _write(os.path.join(mdef, "tables", "bad.tmdl"), b"\xff")
        base = os.path.join(project.mirror_report, "definition")
        _write(os.path.join(base, "report.json"), "{}\n")
        pages = os.path.join(base, "pages")
        _write(os.path.join(pages, "pages.json"), '{"pageOrder": ["p1"]}\n')
        _write(os.path.join(pages, "p1", "page.json"),
               '{"name": "p1", "displayName": "Page", "width": 1280, "height": 720}\n')
        _write(os.path.join(pages, "p1", "visuals", "v1", "visual.json"), json.dumps(_visual("v1")))
        _write(os.path.join(pages, "p1", "visuals", "v2", "visual.json"),
               "{" if bad_visual else json.dumps(_visual("v2")))
        return ws, project

    def test_filter_dry_run_and_apply_files(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        dry = filters.add(ws, project, "'Sales'[Name]", report=True, values_in=["A"], dry_run=True)
        self.assertEqual(dry["result"], "DRY_RUN")
        self.assertTrue(dry["files"])
        self.assertEqual(_bytes(ws.mirror), before)
        applied = filters.add(ws, project, "'Sales'[Name]", report=True, values_in=["A"])
        self.assertEqual(applied["files"], dry["files"])
        self.assertNotEqual(_bytes(ws.mirror), before)

    def test_relationship_dry_run_files_match_apply(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        dry = relate.add(ws, project, "'Sales'[When]", "'Date'[Date]", dry_run=True)
        self.assertEqual(dry["result"], "DRY_RUN")
        self.assertTrue(any(f.endswith("relationships.tmdl") for f in dry["files"]))
        self.assertEqual(_bytes(ws.mirror), before)
        applied = relate.add(ws, project, "'Sales'[When]", "'Date'[Date]")
        self.assertEqual(applied["files"], dry["files"])
        self.assertIn("relationship", Path(os.path.join(project.mirror_model, "definition", "relationships.tmdl")).read_text(encoding="utf-8"))

    def test_interaction_dry_run_unchanged(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        dry = filters.interaction(ws, project, "Page", "v1", "v2", "none", dry_run=True)
        self.assertEqual(dry["result"], "DRY_RUN")
        self.assertTrue(any(f.endswith("page.json") for f in dry["files"]))
        self.assertEqual(_bytes(ws.mirror), before)

    def test_malformed_sibling_refused(self):
        ws, project = self._project(bad_visual=True)
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            filters.add(ws, project, "'Sales'[Name]", report=True, values_in=["A"])
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertIn("visual.json", caught.exception.detail)
        self.assertNotIn("Expecting", caught.exception.detail)
        self.assertEqual(_bytes(ws.mirror), before)
        ws2, project2 = self._project(bad_tmdl=True)
        before2 = _bytes(ws2.mirror)
        with self.assertRaises(PbzError) as caught2:
            relate.add(ws2, project2, "'Sales'[When]", "'Date'[Date]")
        self.assertEqual(caught2.exception.code, "REFUSED")
        self.assertIn("bad.tmdl", caught2.exception.detail)
        self.assertEqual(_bytes(ws2.mirror), before2)

    def test_write_failure_rolls_back(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        real = common.write_text

        def boom(path, text, bom=b"", *, root=None, **_kw):
            raise OSError("disk")

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                filters.add(ws, project, "'Sales'[Name]", report=True, values_in=["A"])
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])
        real  # keep the import used if a later assertion wants the original


if __name__ == "__main__":
    unittest.main()
