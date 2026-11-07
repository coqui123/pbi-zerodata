"""Offline checks that tab order and phone layout commit as one edit.

Run: python tests/test_a11y_atomic.py
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

from pbz import a11y, common  # noqa: E402
from pbz.common import PbzError  # noqa: E402


def _visual(name, y, tab):
    return {
        "name": name,
        "position": {"x": 10, "y": y, "z": 0, "width": 100, "height": 40, "tabOrder": tab},
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


class A11yAtomicTests(unittest.TestCase):
    def _project(self, *, broken=False, mobile=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        ws = common.Workspace(root)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        project = common.Project(ws, "Mini")
        base = os.path.join(project.mirror_report, "definition", "pages", "p1")
        _write(os.path.join(base, "page.json"), '{"name": "p1", "displayName": "Page", "width": 1280, "height": 720}\n')
        _write(os.path.join(project.mirror_report, "definition", "pages", "pages.json"), '{"pageOrder": ["p1"]}\n')
        _write(os.path.join(base, "visuals", "v_ok", "visual.json"), json.dumps(_visual("v_ok", 10, 9000)))
        _write(os.path.join(base, "visuals", "v_next", "visual.json"),
               "{" if broken else json.dumps(_visual("v_next", 80, 9000)))
        if mobile:
            for vid, text in mobile.items():
                _write(os.path.join(base, "visuals", vid, "mobile.json"), text)
        return ws, project

    def test_tab_order_write_failure_rolls_back(self):
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
                a11y.fix_tab_order(ws, project, "Page")
            self.assertEqual(caught.exception.code, "FAILED")
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])

    def test_malformed_candidate_refuses(self):
        ws, project = self._project(broken=True)
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            a11y.fix_tab_order(ws, project, "Page")
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertIn("v_next/visual.json", caught.exception.detail.replace("\\", "/"))
        self.assertNotIn("Expecting", caught.exception.detail)
        self.assertEqual(_bytes(ws.mirror), before)

    def test_malformed_mobile_refuses(self):
        ws, project = self._project(mobile={"v_ok": "{"})
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            a11y.mobile(ws, project, "Page", "v_ok", pos="0,0,100,40")
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertIn("mobile.json", caught.exception.detail.replace("\\", "/"))
        self.assertEqual(_bytes(ws.mirror), before)

    def test_nonfinite_pos_refuses_before_write(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            a11y.mobile(ws, project, "Page", "v_ok", pos="nan,0,100,40")
        self.assertEqual(caught.exception.code, "BAD_REQUEST")
        self.assertEqual(_bytes(ws.mirror), before)

    def test_remove_failure_restores_deletes(self):
        doc = '{"position": {"x": 0, "y": 0, "width": 10, "height": 10}}\n'
        ws, project = self._project(mobile={"v_ok": doc, "v_next": doc})
        before = _bytes(ws.mirror)
        real = os.remove
        calls = {"n": 0}

        def boom(path):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("disk")
            return real(path)

        with patch("pbz.common.os.remove", boom):
            with self.assertRaises(PbzError) as caught:
                a11y.mobile(ws, project, "Page", "all", remove=True)
            self.assertEqual(caught.exception.code, "FAILED")
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])


if __name__ == "__main__":
    unittest.main()
