"""Revert plans every file, then one commit. Run: python tests/test_revert_atomic.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import common, mirror  # noqa: E402


def file_map(root):
    out = {}
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace("\\", "/")
            with open(path, "rb") as f:
                out[rel] = f.read()
    return out


class RevertAtomicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "ws")
        test_parity.build(root)
        self.ws = common.Workspace(root)
        self.ws.private = os.path.join(self.tmp.name, "private")
        os.makedirs(self.ws.private)
        self.project = self.ws.project()
        mirror.pull(self.ws, self.project)
        self.paths = []
        for dirpath, _dirs, names in os.walk(self.project.mirror_model):
            for name in names:
                if name.endswith(".tmdl"):
                    self.paths.append(os.path.join(dirpath, name))
        self.paths = sorted(self.paths)[:2]
        self.assertGreaterEqual(len(self.paths), 2)
        for path in self.paths:
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n")

    def test_second_write_rolls_back(self):
        before = file_map(self.ws.mirror)
        real = common.write_text
        seen = {"n": 0}

        def wrapped(path, text, bom=b"", *, root=None, **_kw):
            seen["n"] += 1
            if seen["n"] >= 2:
                raise OSError("second write")
            return real(path, text, bom, root=root)

        with patch("pbz.common.write_text", wrapped):
            with self.assertRaises(common.PbzError) as caught:
                mirror.revert(self.ws, self.project)
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(file_map(self.ws.mirror), before)
        self.assertFalse(any(name.endswith(".pbz-tmp") for name in os.listdir(self.ws.mirror)))
        for dirpath, _dirs, names in os.walk(self.ws.root):
            for name in names:
                self.assertFalse(name.endswith(".pbz-tmp"))

    def test_unreadable_target_refuses_before_write(self):
        before = file_map(self.ws.mirror)
        real = mirror.read_text

        def wrapped(path):
            if path.startswith(mirror.base_dir(self.ws, self.project)):
                raise UnicodeError("bad")
            return real(path)

        with patch("pbz.mirror.read_text", wrapped), patch("pbz.common.write_text", side_effect=AssertionError("write")):
            with self.assertRaises(common.PbzError) as caught:
                mirror.revert(self.ws, self.project)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertNotIn("bad", caught.exception.detail)
        self.assertEqual(file_map(self.ws.mirror), before)


if __name__ == "__main__":
    unittest.main()
