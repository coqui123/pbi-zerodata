"""Offline checks that consolidate merge and calc-group commit as one edit.

A PBIR parse failure or an injected write failure must leave every mirrored file
byte-identical, including no leftover .pbz-tmp and no orphan calculation-group table.

Run: python tests/test_consolidate_atomic.py
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

from pbz import common, consolidate  # noqa: E402
from pbz.common import PbzError  # noqa: E402

SALES = "table Sales\n\n\tmeasure Keep = 1\n\n\tmeasure Copy = 1\n\n"
FAMILY = (
    "table Sales\n\n"
    "\tmeasure A = 1\n\n"
    "\tmeasure B = 2\n\n"
    "\tmeasure C = 3\n\n"
    "\tmeasure 'A YTD' = CALCULATE([A], 1)\n\n"
    "\tmeasure 'B YTD' = CALCULATE([B], 1)\n\n"
    "\tmeasure 'C YTD' = CALCULATE([C], 1)\n\n"
)
MODEL = "model Model\n\tculture: en-US\n\nref table Sales\n"
DATABASE = "database\n\tcompatibilityLevel: 1400\n"
VISUAL = {
    "visual": {"query": {"queryState": {"Y": {"projections": [
        {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Copy"}},
         "queryRef": "Sales.Copy"}
    ]}}}}
}


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


class ConsolidateAtomicTests(unittest.TestCase):
    def _project(self, *, family=False, broken=False, unreadable=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        ws = common.Workspace(root)
        ws.private = os.path.join(root, "private")
        os.makedirs(ws.private)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        project = common.Project(ws, "Mini")
        mdef = os.path.join(project.mirror_model, "definition")
        _write(os.path.join(mdef, "tables", "Sales.tmdl"), FAMILY if family else SALES)
        _write(os.path.join(mdef, "model.tmdl"), MODEL)
        _write(os.path.join(mdef, "database.tmdl"), DATABASE)
        page = os.path.join(project.mirror_report, "definition", "pages", "p1", "visuals")
        _write(os.path.join(page, "v_ok", "visual.json"), json.dumps(VISUAL))
        if broken:
            _write(os.path.join(page, "v_broken", "visual.json"), "{")
        if unreadable:
            path = os.path.join(page, "v_bad", "visual.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"\xff\xfe not utf-8")
        os.makedirs(project.model_dir, exist_ok=True)
        return ws, project

    def test_merge_write_failure_rolls_back(self):
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
                consolidate.merge(ws, project, "Copy", "Keep")
            self.assertEqual(caught.exception.code, "FAILED")
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])

    def test_merge_broken_pbir_refuses(self):
        ws, project = self._project(broken=True)
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            consolidate.merge(ws, project, "Copy", "Keep")
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertIn("v_broken/visual.json", caught.exception.detail)
        self.assertEqual(_bytes(ws.mirror), before)

    def test_merge_dry_run_unchanged(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        out = consolidate.merge(ws, project, "Copy", "Keep", dry_run=True)
        self.assertEqual(out["result"], "DRY_RUN")
        self.assertEqual(_bytes(ws.mirror), before)

    def test_calc_group_check_spec_failure_rolls_back(self):
        ws, project = self._project(family=True)
        before = _bytes(ws.mirror)
        real = common.write_text

        def boom(path, text, bom=b""):
            if str(path).endswith(".check.json"):
                raise OSError("spec")
            return real(path, text, bom)

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                consolidate.calc_group(ws, project, measures=["[A YTD]", "[B YTD]", "[C YTD]"], name="Time Variants")
            self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertFalse(os.path.exists(os.path.join(
            project.mirror_model, "definition", "tables", "Time Variants.tmdl")))
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])
        self.assertEqual(list(Path(ws.mirror).glob("consolidate-*.check.json")), [])

    def test_calc_group_broken_visual_refuses(self):
        ws, project = self._project(family=True, broken=True)
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            consolidate.calc_group(ws, project, measures=["[A YTD]", "[B YTD]", "[C YTD]"], name="Time Variants")
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertIn("v_broken/visual.json", caught.exception.detail)
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(list(Path(ws.mirror).glob("consolidate-*.check.json")), [])

    def test_calc_group_unreadable_visual_refuses(self):
        ws, project = self._project(family=True, unreadable=True)
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            consolidate.calc_group(ws, project, measures=["[A YTD]", "[B YTD]", "[C YTD]"], name="Time Variants")
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertNotIsInstance(caught.exception, OSError)
        self.assertIn("v_bad/visual.json", caught.exception.detail)
        self.assertEqual(_bytes(ws.mirror), before)

    def test_calc_group_dry_run_unchanged(self):
        ws, project = self._project(family=True)
        before = _bytes(ws.mirror)
        out = consolidate.calc_group(ws, project, measures=["[A YTD]", "[B YTD]", "[C YTD]"],
                                     name="Time Variants", dry_run=True)
        self.assertEqual(out["result"], "DRY_RUN")
        self.assertFalse(out["verify"]["written"])
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertFalse(os.path.exists(out["verify"]["spec"]))


if __name__ == "__main__":
    unittest.main()
