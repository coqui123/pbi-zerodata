"""Offline checks that style edits parse every candidate, then commit once.

Run: python tests/test_style_atomic.py
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

from pbz import common, style  # noqa: E402
from pbz.common import PbzError  # noqa: E402

MODEL = "model Model\n\tculture: en-US\n\n"
DATABASE = "database\n\tcompatibilityLevel: 1400\n"


def _visual(name):
    return {"name": name, "position": {"x": 0, "y": 0, "width": 100, "height": 40},
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


class StyleAtomicTests(unittest.TestCase):
    def _project(self, *, broken_visual=False, broken_page=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        ws = common.Workspace(root)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        project = common.Project(ws, "Mini")
        mdef = os.path.join(project.mirror_model, "definition")
        _write(os.path.join(mdef, "model.tmdl"), MODEL)
        _write(os.path.join(mdef, "database.tmdl"), DATABASE)
        base = os.path.join(project.mirror_report, "definition", "pages")
        _write(os.path.join(base, "pages.json"), '{"pageOrder": ["p1"]}\n')
        _write(os.path.join(base, "p1", "page.json"),
               "{" if broken_page else '{"name": "p1", "displayName": "Page", "width": 1280, "height": 720}\n')
        _write(os.path.join(base, "p1", "visuals", "v_ok", "visual.json"), json.dumps(_visual("v_ok")))
        _write(os.path.join(base, "p1", "visuals", "v_next", "visual.json"),
               "{" if broken_visual else json.dumps(_visual("v_next")))
        return ws, project

    def test_later_write_rolls_back(self):
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
                style.run(ws, project, page="Page", all_=True, sets=["title.show=true"])
            self.assertEqual(caught.exception.code, "FAILED")
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])

    def test_malformed_visual_refuses(self):
        ws, project = self._project(broken_visual=True)
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            style.run(ws, project, page="Page", visual="v_ok", sets=["title.show=true"])
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertIn("v_next/visual.json", caught.exception.detail.replace("\\", "/"))
        self.assertNotIn("Expecting", caught.exception.detail)
        self.assertEqual(_bytes(ws.mirror), before)

    def test_malformed_page_canvas_refuses(self):
        ws, project = self._project(broken_page=True)
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            style.run_canvas(ws, project, all_=True, sets=["background.color=#112233"])
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertIn("page.json", caught.exception.detail.replace("\\", "/"))
        self.assertEqual(_bytes(ws.mirror), before)

    def test_show_only_is_read_only(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        out = style.run(ws, project, page="Page", visual="v_ok", show=True)
        self.assertIn("formatting", out)
        self.assertEqual(out["files"], [])
        self.assertEqual(_bytes(ws.mirror), before)


if __name__ == "__main__":
    unittest.main()
