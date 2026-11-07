"""Offline checks that bookmark add and remove commit as one edit.

Run: python tests/test_bookmarks_atomic.py
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

from pbz import bookmarks, common  # noqa: E402
from pbz.common import PbzError  # noqa: E402

PAGE = '{"name": "p1", "displayName": "Page", "width": 1280, "height": 720}\n'
VISUAL = {
    "name": "v1",
    "position": {"x": 10, "y": 10, "z": 0, "width": 100, "height": 80, "tabOrder": 0},
    "visual": {"visualType": "card"},
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


class BookmarkAtomicTests(unittest.TestCase):
    def _project(self, meta="{}"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        ws = common.Workspace(root)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        project = common.Project(ws, "Mini")
        base = os.path.join(project.mirror_report, "definition")
        _write(os.path.join(base, "pages", "p1", "page.json"), PAGE)
        _write(os.path.join(base, "pages", "pages.json"), '{"pageOrder": ["p1"]}\n')
        _write(os.path.join(base, "pages", "p1", "visuals", "v1", "visual.json"), json.dumps(VISUAL))
        if meta is not None:
            _write(os.path.join(base, "bookmarks", "bookmarks.json"), meta)
        return ws, project

    def test_add_second_write_rolls_back(self):
        ws, project = self._project(meta=None)
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
                bookmarks.add(ws, project, "Shown", "Page", hide=["v1"])
            self.assertEqual(caught.exception.code, "FAILED")
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])

    def test_remove_meta_failure_restores_bookmark(self):
        ws, project = self._project(meta=None)
        made = bookmarks.add(ws, project, "Shown", "Page", hide=["v1"])
        before = _bytes(ws.mirror)
        real = common.write_text

        def boom(path, text, bom=b""):
            raise OSError("disk")

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                bookmarks.remove(ws, project, "Shown")
            self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertTrue(any(made["name"] in k for k in before))
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])

    def test_malformed_meta_refuses(self):
        ws, project = self._project(meta="{")
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            bookmarks.add(ws, project, "Shown", "Page", hide=["v1"])
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertIn("bookmarks.json", caught.exception.detail.replace("\\", "/"))
        self.assertNotIn("Expecting", caught.exception.detail)
        self.assertEqual(_bytes(ws.mirror), before)

    def test_files_match_touched_paths(self):
        ws, project = self._project()
        out = bookmarks.add(ws, project, "Shown", "Page", hide=["v1"])
        self.assertEqual(len(out["files"]), 2)
        for rel in out["files"]:
            self.assertTrue(os.path.isfile(os.path.join(ws.mirror, *rel.split("/"))))
        text = Path(os.path.join(project.mirror_report, "definition", "bookmarks", out["name"] + ".bookmark.json")).read_text(encoding="utf-8")
        self.assertIn('"suppressData": true', text)


if __name__ == "__main__":
    unittest.main()
