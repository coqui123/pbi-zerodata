"""Round 18 Item 2 boundary: aliased mirror pages refuse. Run: python tests/test_interactions_boundary.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import common, interactiontest, mirror  # noqa: E402
from pbz.common import PbzError  # noqa: E402


class InteractionsBoundary(unittest.TestCase):
    def test_hardlinked_page_refused(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "ws")
        test_parity.build(root)
        ws = common.Workspace(root)
        project = ws.project()
        mirror.pull(ws, project)
        pages = os.path.join(project.mirror_report, "definition", "pages", "p1")
        os.makedirs(os.path.join(pages, "visuals", "v1"), exist_ok=True)
        real = os.path.join(tmp.name, "outside.json")
        with open(real, "w", encoding="utf-8") as handle:
            handle.write('{"displayName":"PBZ_CANARY_PAGE"}')
        page = os.path.join(pages, "page.json")
        if os.path.lexists(page):
            os.remove(page)
        try:
            os.link(real, page)
        except OSError:
            self.skipTest("hardlinks unavailable")
        with self.assertRaises(PbzError) as caught:
            interactiontest.run(ws, project, {
                "version": 1, "cases": [{"id": "b", "action": "back", "page": "p1", "visual": "v1"}]})
        self.assertIn(caught.exception.code, ("UNSAFE_PATH", "REFUSED"))
        self.assertNotIn("PBZ_CANARY_PAGE", str(caught.exception))

    def test_parent_page_id_rejected(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "ws")
        test_parity.build(root)
        ws = common.Workspace(root)
        project = ws.project()
        mirror.pull(ws, project)
        with self.assertRaises(PbzError):
            interactiontest.run(ws, project, {
                "version": 1, "cases": [{"id": "bad", "action": "hover", "page": "../A", "visual": "D"}]})

    def test_hardlinked_spec_refused(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "ws")
        test_parity.build(root)
        ws = common.Workspace(root)
        project = ws.project()
        mirror.pull(ws, project)
        real = os.path.join(tmp.name, "spec.json")
        with open(real, "w", encoding="utf-8") as handle:
            handle.write('{"version":1,"title":"PBZ_CANARY_SPEC","cases":[]}')
        link = os.path.join(tmp.name, "alias.json")
        try:
            os.link(real, link)
        except OSError:
            self.skipTest("hardlinks unavailable")
        with self.assertRaises(PbzError) as caught:
            interactiontest.run(ws, project, link)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertNotIn("PBZ_CANARY_SPEC", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
