"""Offline checks that page and visual adds commit as one edit.

Run: python tests/test_author_atomic.py
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

from pbz import author, common  # noqa: E402
from pbz.common import PbzError  # noqa: E402

SALES = "table Sales\n\n\tmeasure M = 1\n\n"
MODEL = "model Model\n\tculture: en-US\n\nref table Sales\n"
DATABASE = "database\n\tcompatibilityLevel: 1400\n"
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


def _new_files(before, after):
    return [k for k in after if k not in before]


class AuthorAtomicTests(unittest.TestCase):
    def _project(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        ws = common.Workspace(root)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        project = common.Project(ws, "Mini")
        mdef = os.path.join(project.mirror_model, "definition")
        _write(os.path.join(mdef, "tables", "Sales.tmdl"), SALES)
        _write(os.path.join(mdef, "model.tmdl"), MODEL)
        _write(os.path.join(mdef, "database.tmdl"), DATABASE)
        base = os.path.join(project.mirror_report, "definition")
        _write(os.path.join(base, "report.json"), "{}\n")
        _write(os.path.join(base, "pages", "pages.json"), '{"pageOrder": ["p1"]}\n')
        _write(os.path.join(base, "pages", "p1", "page.json"), PAGE)
        contract = {"guid": "VizAbc", "register_as": "public",
                    "roles": [{"name": "Values", "kind": "measure"}]}
        _write(os.path.join(ws.root, ".pbz", "visuals", "VizAbc.json"), json.dumps(contract))
        return ws, project

    def test_unknown_after_refuses(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            author.add_page(ws, project, "Later", after="Missing")
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        self.assertEqual(_new_files(before, _bytes(ws.mirror)), [])
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])

    def test_page_write_failure_leaves_no_orphan_files(self):
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
                author.add_page(ws, project, "Later")
            self.assertEqual(caught.exception.code, "FAILED")
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(_new_files(before, _bytes(ws.mirror)), [])
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])

    def test_registration_failure_leaves_report_unchanged(self):
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
                author.add_visual(ws, project, "VizAbc", "Page", ["[M]"])
            self.assertEqual(caught.exception.code, "FAILED")
        self.assertGreaterEqual(calls["n"], 2)
        after = _bytes(ws.mirror)
        self.assertEqual(after, before)
        self.assertFalse(any(k.replace("\\", "/").endswith("/visual.json") and "visuals" in k.replace("\\", "/")
                             and "p1" not in k.replace("\\", "/") for k in after))
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])


if __name__ == "__main__":
    unittest.main()
