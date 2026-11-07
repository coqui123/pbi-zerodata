"""Offline checks that repair and property edits commit as one edit.

Run: python tests/test_repair_props_atomic.py
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

from pbz import common, props, repair  # noqa: E402
from pbz.common import PbzError  # noqa: E402

TAG = "11111111-1111-1111-1111-111111111111"
SALES = (
    "table Sales\n\n"
    "\tmeasure Keep = 1\n"
    "\t\tformatString: #,0\n"
    "\t\tlineageTag: " + TAG + "\n\n"
    "\t\tformatStringDefinition = \"#,0\"\n\n"
    "\tmeasure Copy = 1\n"
    "\t\tlineageTag: " + TAG + "\n\n"
)
TAG_B = "22222222-2222-2222-2222-222222222222"
OTHER = (
    "table Other\n\n"
    "\tmeasure Extra = 1\n"
    "\t\tlineageTag: " + TAG_B + "\n\n"
    "\tmeasure Extra2 = 2\n"
    "\t\tlineageTag: " + TAG_B + "\n\n"
)
MODEL = "model Model\n\tculture: en-US\n\nref table Sales\nref table Other\n"
DATABASE = "database\n\tcompatibilityLevel: 1601\n"
PAGE = '{"name": "p1", "displayName": "Page", "width": 1280, "height": 720}\n'


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


def _boom():
    real = common.write_text
    calls = {"n": 0}

    def wrapped(path, text, bom=b""):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("disk")
        return real(path, text, bom)

    return calls, wrapped


class RepairPropsAtomicTests(unittest.TestCase):
    def _project(self, *, broken_page=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        ws = common.Workspace(root)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        project = common.Project(ws, "Mini")
        mdef = os.path.join(project.mirror_model, "definition")
        _write(os.path.join(mdef, "tables", "Sales.tmdl"), SALES)
        _write(os.path.join(mdef, "tables", "Other.tmdl"), OTHER)
        _write(os.path.join(mdef, "model.tmdl"), MODEL)
        _write(os.path.join(mdef, "database.tmdl"), DATABASE)
        page = os.path.join(project.mirror_report, "definition", "pages", "p1", "page.json")
        _write(page, "{" if broken_page else PAGE)
        _write(os.path.join(project.mirror_report, "definition", "pages", "pages.json"),
               '{"pageOrder": ["p1"]}\n')
        return ws, project

    def test_props_second_write_rolls_back(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        calls, boom = _boom()
        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                props.run(ws, project, kind="measure", sets=["displayFolder=Pack"])
            self.assertEqual(caught.exception.code, "FAILED")
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])

    def test_repair_second_write_rolls_back(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        calls, boom = _boom()
        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                repair.run(ws, project, lineage=True)
            self.assertEqual(caught.exception.code, "FAILED")
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])

    def test_repair_dry_run_writes_nothing(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        out = repair.run(ws, project, dry_run=True, lineage=True)
        self.assertEqual(out["result"], "DRY_RUN")
        self.assertEqual(_bytes(ws.mirror), before)

    def test_repair_lineage_and_format_together(self):
        ws, project = self._project()
        out = repair.run(ws, project, lineage=True)
        codes = {f["code"] for f in out["fixes"]}
        self.assertIn("DUPLICATE_LINEAGE_TAG", codes)
        self.assertIn("FORMAT_STRING_CONFLICT", codes)
        text = Path(os.path.join(project.mirror_model, "definition", "tables", "Sales.tmdl")).read_text(encoding="utf-8")
        self.assertNotIn("formatString:", text)
        self.assertIn("formatStringDefinition", text)
        self.assertEqual(text.lower().count(TAG), 1)
        other = Path(os.path.join(project.mirror_model, "definition", "tables", "Other.tmdl")).read_text(encoding="utf-8")
        self.assertEqual(other.lower().count(TAG_B), 1)

    def test_malformed_page_refuses_apply_and_dry_run(self):
        ws, project = self._project(broken_page=True)
        before = _bytes(ws.mirror)
        for dry in (False, True):
            with self.assertRaises(PbzError) as caught:
                repair.run(ws, project, dry_run=dry, lineage=True)
            self.assertEqual(caught.exception.code, "REFUSED")
            self.assertIn("page.json", caught.exception.detail)
            self.assertNotIn("Expecting", caught.exception.detail)
        self.assertEqual(_bytes(ws.mirror), before)


if __name__ == "__main__":
    unittest.main()
