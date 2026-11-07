"""memscrub --apply is human-only and one private commit. A sandbox user name is not isolation.

Run: python tests/test_memscrub_atomic.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import cli, common, memscrub  # noqa: E402
from pbz.common import PbzError  # noqa: E402


class MemscrubAtomicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = common.Workspace(self.tmp.name)
        self.ws.private = os.path.join(self.tmp.name, "private")
        os.makedirs(self.ws.private)
        self.notes = os.path.join(self.tmp.name, "notes")
        os.makedirs(self.notes)
        self.a = os.path.join(self.notes, "a.md")
        self.b = os.path.join(self.notes, "b.md")
        for path in (self.a, self.b):
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write("spent 1000 hours\n")

    def test_nonhuman_apply_writes_nothing(self):
        before = {p: open(p, "rb").read() for p in (self.a, self.b)}
        with self.assertRaises(PbzError) as caught:
            memscrub.run(self.ws, None, self.notes, apply=True, human=False)
        self.assertEqual(caught.exception.code, "HUMAN_ONLY")
        self.assertEqual({p: open(p, "rb").read() for p in before}, before)
        self.assertFalse(os.path.isdir(os.path.join(self.ws.private, "memory-backup")))

    def test_mid_write_restores_notes(self):
        before = {p: open(p, "rb").read() for p in (self.a, self.b)}
        real = common.write_text
        seen = {"n": 0}

        def boom(path, text, bom=b""):
            if path.replace("\\", "/").endswith(".md") and "memory-backup" not in path.replace("\\", "/"):
                seen["n"] += 1
                if seen["n"] > 1:
                    raise OSError("mid")
            return real(path, text, bom)

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                memscrub.run(self.ws, None, self.notes, apply=True, human=True)
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual({p: open(p, "rb").read() for p in before}, before)

    def test_username_alone_does_not_configure_isolation(self):
        with patch.dict(os.environ, {"USERNAME": "WDAGUtilityAccount"}):
            out = cli.cmd_doctor()
        self.assertTrue(all(item["status"] == "unverified" for item in out["isolation"]))
        flags = {spec[0] for spec in __import__("pbz.mcp", fromlist=["mcp"]).BY_NAME["memscrub"].opts.values()}
        self.assertNotIn("--apply", flags)


if __name__ == "__main__":
    unittest.main()
