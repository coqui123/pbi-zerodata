"""Offline checks that page and visual copies commit as one edit.

Run: python tests/test_components_atomic.py
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

from pbz import common, components  # noqa: E402
from pbz.common import PbzError  # noqa: E402


def _visual(name, y=0):
    return {"name": name, "position": {"x": 0, "y": y, "z": 0, "width": 100, "height": 40, "tabOrder": 0},
            "visual": {"visualType": "card"}}


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


class ComponentsAtomicTests(unittest.TestCase):
    def _project(self, *, broken=False, mobile=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        ws = common.Workspace(root)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        project = common.Project(ws, "Mini")
        base = os.path.join(project.mirror_report, "definition", "pages")
        _write(os.path.join(base, "pages.json"), '{"pageOrder": ["p1"]}\n')
        _write(os.path.join(base, "p1", "page.json"),
               '{"name": "p1", "displayName": "Page", "width": 1280, "height": 720}\n')
        _write(os.path.join(base, "p1", "visuals", "v_ok", "visual.json"), json.dumps(_visual("v_ok", 0)))
        _write(os.path.join(base, "p1", "visuals", "v_next", "visual.json"),
               "{" if broken else json.dumps(_visual("v_next", 80)))
        if mobile:
            _write(os.path.join(base, "p1", "visuals", "v_ok", "mobile.json"), '{"position": {"x": 0, "y": 0}}\n')
        return ws, project

    def test_copy_page_write_failure_leaves_no_destination_files(self):
        ws, project = self._project()
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
                components.copy_page(ws, project, "Page", "Copy")
            self.assertEqual(caught.exception.code, "FAILED")
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])

    def test_broken_source_visual_refuses(self):
        ws, project = self._project(broken=True)
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            components.copy_page(ws, project, "Page", "Copy")
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertIn("v_next/visual.json", caught.exception.detail.replace("\\", "/"))
        self.assertNotIn("Expecting", caught.exception.detail)
        self.assertEqual(_bytes(ws.mirror), before)

    def test_copy_visual_failure_restores_pages_json_and_children(self):
        ws, project = self._project(mobile=True)
        before = _bytes(ws.mirror)
        pages = os.path.join(project.mirror_report, "definition", "pages", "pages.json")
        pages_before = Path(pages).read_bytes()
        real = common.write_text
        calls = {"n": 0}

        def boom(path, text, bom=b"", *, root=None, **_kw):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("disk")
            return real(path, text, bom, root=root)

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                components.copy_visual(ws, project, "Page", "v_ok")
            self.assertEqual(caught.exception.code, "FAILED")
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(Path(pages).read_bytes(), pages_before)
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])


if __name__ == "__main__":
    unittest.main()
