"""Round 16: mirror writers refuse a junction at the mirror root before work.

Run: python tests/test_mirror_writers_root.py
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pbz.common import PbzError, Workspace  # noqa: E402
from pbz import control, docs, handoff, mirror  # noqa: E402

CANARY = b"CANARY-MIRROR-LINE\n"


def _tiny(root: str) -> None:
    model = os.path.join(root, "Canary.SemanticModel", "definition")
    os.makedirs(os.path.join(model, "tables"))
    open(os.path.join(root, "Canary.pbip"), "w", encoding="utf-8").close()
    with open(os.path.join(root, "Canary.SemanticModel", "definition.pbism"), "w", encoding="utf-8") as handle:
        handle.write("{}\n")
    with open(os.path.join(model, "database.tmdl"), "w", encoding="utf-8") as handle:
        handle.write("database\n\tcompatibilityLevel: 1600\n")
    with open(os.path.join(model, "model.tmdl"), "w", encoding="utf-8") as handle:
        handle.write("model Model\n\tculture: en-US\n\nref table T\n")
    with open(os.path.join(model, "tables", "T.tmdl"), "w", encoding="utf-8") as handle:
        handle.write("table T\n\tmeasure M = 1\n")
    report = os.path.join(root, "Canary.Report", "definition")
    os.makedirs(report)
    with open(os.path.join(root, "Canary.Report", "definition.pbir"), "w", encoding="utf-8") as handle:
        handle.write("{}\n")
    with open(os.path.join(report, "report.json"), "w", encoding="utf-8") as handle:
        handle.write("{}\n")


class MirrorWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        _tiny(self.root)
        self.ws = Workspace(self.root)
        self.project = self.ws.project()

    def _junction(self, link, target):
        from _junction import make_junction
        make_junction(self, link, target)

    def _outside(self):
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        return external.name

    def _map(self, root):
        out = {}
        for cur, dirs, files in os.walk(root):
            for name in files:
                path = os.path.join(cur, name)
                if os.path.isfile(path) and not os.path.islink(path):
                    with open(path, "rb") as handle:
                        out[path] = handle.read()
        return out

    def test_junction_at_mirror_refuses_before_work(self):
        outside = self._outside()
        with open(os.path.join(outside, "canary.txt"), "wb") as handle:
            handle.write(CANARY)
        self._junction(self.ws.mirror, outside)
        before = self._map(self.root)
        external = self._map(outside)
        for call in (
            lambda: docs.run(self.ws, self.project),
            lambda: handoff.write_packet(self.ws, "HANDOFF.md", "checklist\n", "handoffs"),
            lambda: control.output_path(self.ws, "requirements", "ledger.json"),
            lambda: mirror._write_guide(self.ws, "heuristic"),
            lambda: mirror.pull(self.ws, self.project),
        ):
            with self.assertRaises(PbzError) as caught:
                call()
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")
            self.assertNotIn(b"CANARY", str(caught.exception).encode())
        self.assertEqual(self._map(outside), external)
        self.assertEqual(self._map(self.root), before)
        self.assertEqual(list(Path(outside).rglob("*.pbz-tmp")), [])

    def test_junction_at_pulled_mirror_refuses_before_traversal(self):
        import shutil
        mirror.pull(self.ws, self.project)
        outside = self._outside()
        with open(os.path.join(outside, "canary.txt"), "wb") as handle:
            handle.write(CANARY)
        shutil.rmtree(self.ws.mirror)
        self._junction(self.ws.mirror, outside)
        opens = {"n": 0}
        real_open = open
        outside_norm = os.path.normcase(outside)

        def counting(path, *args, **kwargs):
            if os.path.normcase(str(path)).startswith(outside_norm + os.sep) or os.path.normcase(str(path)) == outside_norm:
                opens["n"] += 1
            return real_open(path, *args, **kwargs)

        external = self._map(outside)
        with patch("builtins.open", counting):
            for force in (False, True):
                with self.assertRaises(PbzError) as caught:
                    mirror.pull(self.ws, self.project, force=force)
                self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(opens["n"], 0)
        self.assertEqual(self._map(outside), external)
        self.assertEqual(list(Path(outside).rglob("*.pbz-tmp")), [])

    def test_symlinked_guide_blocks_pull_before_mutation(self):
        outside = self._outside()
        secret = os.path.join(outside, "guide.md")
        with open(secret, "wb") as handle:
            handle.write(CANARY)
        mirror.pull(self.ws, self.project)
        guide = os.path.join(self.ws.mirror, "MIRROR.md")
        os.remove(guide)
        try:
            os.symlink(secret, guide)
        except OSError:
            self.skipTest("file symlink is unavailable")
        before = self._map(self.root)
        external = self._map(outside)
        with self.assertRaises(PbzError) as caught:
            mirror.pull(self.ws, self.project)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(self._map(self.root), before)
        self.assertEqual(self._map(outside), external)
        self.assertEqual(list(Path(self.root).rglob("*.pbz-tmp")), [])

    def test_junction_under_mirror_subdirs(self):
        os.makedirs(self.ws.mirror)
        outside = self._outside()
        with open(os.path.join(outside, "canary.txt"), "wb") as handle:
            handle.write(CANARY)
        for name in ("docs", "requirements", "handoffs"):
            self._junction(os.path.join(self.ws.mirror, name), outside)
        with self.assertRaises(PbzError) as caught:
            docs.run(self.ws, self.project)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        with self.assertRaises(PbzError) as caught:
            control.output_path(self.ws, "requirements", "ledger.json")
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        with self.assertRaises(PbzError) as caught:
            handoff.write_packet(self.ws, "HANDOFF.md", "checklist\n", "handoffs")
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(open(os.path.join(outside, "canary.txt"), "rb").read(), CANARY)
        self.assertEqual(list(Path(outside).rglob("*.pbz-tmp")), [])

    def test_handoff_symlink_is_not_opened(self):
        outside = self._outside()
        secret = os.path.join(outside, "notes.md")
        with open(secret, "wb") as handle:
            handle.write(CANARY)
        os.makedirs(self.ws.mirror)
        link = os.path.join(self.ws.mirror, "HANDOFF.md")
        try:
            os.symlink(secret, link)
        except OSError:
            self.skipTest("file symlink is unavailable")
        reads = {"n": 0}
        real_open = open

        def counting(path, *args, **kwargs):
            if os.path.normcase(str(path)) == os.path.normcase(link):
                reads["n"] += 1
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", counting):
            with self.assertRaises(PbzError) as caught:
                handoff.write_packet(self.ws, "HANDOFF.md", "checklist\n", "handoffs")
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(reads["n"], 0)
        seen = {"n": 0}
        with patch("builtins.open", counting):
            with open(link, "rb") as handle:
                seen["n"] = 1 if handle.read() == CANARY else 0
        self.assertEqual(seen["n"], 1)
        self.assertEqual(open(secret, "rb").read(), CANARY)

    def test_handoff_replace_failure_rolls_back(self):
        os.makedirs(self.ws.mirror)
        real = os.replace
        calls = {"n": 0}

        def boom(src, dst):
            if str(src).endswith(".pbz-tmp"):
                calls["n"] += 1
                if calls["n"] > 1:
                    raise OSError("replace")
            return real(src, dst)

        with patch("os.replace", boom):
            with self.assertRaises(PbzError) as caught:
                handoff.write_packet(self.ws, "HANDOFF.md", "checklist\n", "handoffs")
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertFalse(os.path.exists(os.path.join(self.ws.mirror, "HANDOFF.md")))
        self.assertEqual(list(Path(self.ws.mirror).rglob("*.pbz-tmp")), [])
        self.assertEqual(list(Path(self.ws.mirror).rglob("*.md")), [])

    def test_workspace_junction_still_writes_and_docs_succeed(self):
        real = os.path.join(self.root, "real")
        os.makedirs(real)
        _tiny(real)
        link = os.path.join(self.root, "linked")
        self._junction(link, real)
        ws = Workspace(link)
        project = ws.project()
        pulled = mirror.pull(ws, project)
        self.assertEqual(pulled["result"], "PULLED")
        doc = docs.run(ws, project)
        self.assertTrue(os.path.isfile(os.path.join(real, "pbz-workspace", "docs", "Canary.md")))
        self.assertTrue(doc["file"].endswith("Canary.md"))
        control.save(os.path.join(ws.mirror, "requirements", "ledger.json"), {"version": 1}, root=ws.mirror)
        self.assertTrue(os.path.isfile(os.path.join(real, "pbz-workspace", "requirements", "ledger.json")))


if __name__ == "__main__":
    unittest.main()
