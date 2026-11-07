"""Unreadable history does not fail a pull, and a snapshot replace drops its temp.
Run: python tests/test_history_honesty.py
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


class HistoryHonestyTests(unittest.TestCase):
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

    def test_truncated_snapshot_does_not_fail_pull(self):
        snaps = os.path.join(mirror.history_dir(self.ws, self.project), "snapshots")
        with open(os.path.join(snaps, "19990101_000000.json"), "w", encoding="utf-8") as f:
            f.write("{")
        out = mirror.pull(self.ws, self.project, force=True)
        self.assertEqual(out["result"], "PULLED")
        listed = mirror.history_list(self.ws, self.project)
        self.assertTrue(any(row["event"] == "UNREADABLE" for row in listed))

    def test_snapshot_replace_failure_cleans_tmp(self):
        real = os.replace

        def boom(src, dst):
            if "snapshots" in str(dst).replace("\\", "/") and str(dst).endswith(".json"):
                raise OSError("replace")
            return real(src, dst)

        with patch("os.replace", boom):
            with self.assertRaises(OSError):
                mirror.snapshot(self.ws, self.project, "manual")
        root = mirror.history_dir(self.ws, self.project)
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                self.assertFalse(name.endswith(".pbz-tmp"))

    def test_post_pull_history_warning_keeps_pulled(self):
        with patch("pbz.mirror.snapshot", side_effect=OSError("history")):
            out = mirror.pull(self.ws, self.project, force=True)
        self.assertEqual(out["result"], "PULLED")
        self.assertEqual(out["warnings"][0]["code"], "POST_PULL_HISTORY")


if __name__ == "__main__":
    unittest.main()
