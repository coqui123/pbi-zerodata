"""Synth-test goldens use anchored state paths and checked IO.

Run: python tests/test_synthtest_state_paths.py
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
sys.path.insert(0, os.path.join(SKILL, "tests"))

from pbz.common import PbzError, Project, Workspace  # noqa: E402
from pbz import synthtest  # noqa: E402

INFO = {"seed": 1, "rows": 2, "built": "2026-01-01T00:00:00", "tables": [], "profile": "standard"}
SPEC = {"queries": [{"id": "q", "dax": "EVALUATE ROW(\"v\", 1)"}]}


class SynthtestStatePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["PBZ_PRIVATE_ROOT"] = os.path.join(self.tmp.name, "private")
        self.addCleanup(lambda: os.environ.pop("PBZ_PRIVATE_ROOT", None))
        root = os.path.join(self.tmp.name, "ws")
        os.makedirs(root)
        self.ws = Workspace(root)
        open(os.path.join(root, "Canary.pbip"), "w", encoding="utf-8").close()
        self.project = Project(self.ws, "Canary")
        self.outside = tempfile.TemporaryDirectory()
        self.addCleanup(self.outside.cleanup)
        self.queries = 0
        self.builds = 0

    def _query(self, ws, project, dax, max_rows):
        self.queries += 1
        return {"columns": ["v"], "rows": [[1]], "truncated": False}

    def _info(self, ws, project):
        self.builds += 1
        return dict(INFO)

    def _save(self, name="golden", replace=False):
        with patch("pbz.synthtest.QUERY", self._query), \
             patch("pbz.synth.build_info", self._info), \
             patch("pbz.synthtest.model_hash", return_value="abcd"), \
             patch("pbz.synthtest._stale", return_value=False):
            return synthtest.save(self.ws, self.project, name, SPEC, replace=replace)

    def test_absent_name_is_not_found(self):
        with self.assertRaises(PbzError) as caught:
            synthtest.run(self.ws, self.project, "missing")
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        with self.assertRaises(PbzError) as removed:
            synthtest.remove(self.ws, "missing")
        self.assertEqual(removed.exception.code, "NOT_FOUND")
        self.assertEqual(self.queries, 0)

    def test_hardlink_refuses_before_build_and_is_unreadable(self):
        os.makedirs(os.path.join(self.ws.pbz_dir, "synth-tests"), exist_ok=True)
        outside = os.path.join(self.outside.name, "golden.json")
        with open(outside, "wb") as handle:
            handle.write(b"CANARY")
        leaf = os.path.join(self.ws.pbz_dir, "synth-tests", "golden.json")
        try:
            os.link(outside, leaf)
        except OSError:
            self.skipTest("hardlink is unavailable")
        opened = []
        real = open

        def watch(path, *args, **kwargs):
            if os.path.normcase(str(path)) == os.path.normcase(leaf):
                opened.append(path)
            return real(path, *args, **kwargs)

        with patch("builtins.open", side_effect=watch):
            with self.assertRaises(PbzError) as caught:
                self._save("golden")
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")
            self.assertEqual(self.builds, 0)
            self.assertEqual(self.queries, 0)
            listed = synthtest.list_(self.ws)
            with self.assertRaises(PbzError):
                synthtest.run(self.ws, self.project, "golden")
            with self.assertRaises(PbzError):
                synthtest.remove(self.ws, "golden")
        self.assertEqual(opened, [])
        self.assertEqual(listed["tests"], [{"name": "golden", "error": "unreadable"}])
        self.assertEqual(open(outside, "rb").read(), b"CANARY")
        self.assertEqual(list(Path(self.ws.pbz_dir).glob("*.pbz-tmp")), [])

    def test_junctioned_pbz_refuses_save(self):
        from _junction import make_junction
        external = self.outside.name
        canary = os.path.join(external, "kept.txt")
        with open(canary, "wb") as handle:
            handle.write(b"OUT")
        make_junction(self, self.ws.pbz_dir, external)
        with self.assertRaises(PbzError) as caught:
            self._save()
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(self.builds, 0)
        self.assertEqual(open(canary, "rb").read(), b"OUT")

    def test_dangling_alias_is_not_not_found(self):
        os.makedirs(os.path.join(self.ws.pbz_dir, "synth-tests"), exist_ok=True)
        leaf = os.path.join(self.ws.pbz_dir, "synth-tests", "dangling.json")
        try:
            os.symlink(os.path.join(self.outside.name, "missing.json"), leaf)
        except OSError:
            self.skipTest("symlink is unavailable")
        for call in (lambda: synthtest.run(self.ws, self.project, "dangling"),
                     lambda: synthtest.remove(self.ws, "dangling")):
            with self.assertRaises(PbzError) as caught:
                call()
            self.assertNotEqual(caught.exception.code, "NOT_FOUND")
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")

    def test_failed_replace_keeps_prior_bytes(self):
        self._save("golden")
        path = os.path.join(self.ws.pbz_dir, "synth-tests", "golden.json")
        before = open(path, "rb").read()
        real = os.replace

        def boom(src, dst):
            if str(src).endswith(".pbz-tmp"):
                raise OSError("replace")
            return real(src, dst)

        with patch("os.replace", side_effect=boom):
            with self.assertRaises(PbzError):
                self._save("golden", replace=True)
        self.assertEqual(open(path, "rb").read(), before)
        self.assertEqual(list(Path(self.ws.pbz_dir).rglob("*.pbz-tmp")), [])

    def test_aliased_leaf_lists_unreadable_without_open(self):
        os.makedirs(os.path.join(self.ws.pbz_dir, "synth-tests"), exist_ok=True)
        outside = os.path.join(self.outside.name, "alias.json")
        with open(outside, "wb") as handle:
            handle.write(b"{}")
        leaf = os.path.join(self.ws.pbz_dir, "synth-tests", "alias.json")
        try:
            os.symlink(outside, leaf)
        except OSError:
            self.skipTest("symlink is unavailable")
        opened = []
        real = open

        def watch(path, *args, **kwargs):
            if os.path.normcase(str(path)) == os.path.normcase(leaf):
                opened.append(path)
            return real(path, *args, **kwargs)

        with patch("builtins.open", side_effect=watch):
            listed = synthtest.list_(self.ws)
        self.assertEqual(listed["tests"][0]["error"], "unreadable")
        self.assertEqual(opened, [])


if __name__ == "__main__":
    unittest.main()
