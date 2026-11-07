"""bpa --fix plans, then one commit. Dry-run uses that plan. Run: python tests/test_bpafix_atomic.py
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
from pbz import common, fix, mirror  # noqa: E402


def file_map(root):
    out = {}
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace("\\", "/")
            with open(path, "rb") as f:
                out[rel] = f.read()
    return out


class BpaFixAtomicTests(unittest.TestCase):
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
        self.rule = "NUMERIC_COLUMN_SUMMARIZE_BY"
        # Sales[Amount] is the only visible summed column. A second table makes the commit two writes.
        customer = os.path.join(self.project.mirror_model, "definition", "tables", "Customer.tmdl")
        with open(customer, encoding="utf-8") as f:
            text = f.read()
        old = "\t\tisHidden\n\t\tlineageTag: 20000000-0000-0000-0000-000000000005\n\t\tsummarizeBy: none"
        self.assertIn(old, text)
        with open(customer, "w", encoding="utf-8", newline="") as f:
            f.write(text.replace(old, "\t\tlineageTag: 20000000-0000-0000-0000-000000000005\n\t\tsummarizeBy: sum", 1))

    def test_dry_run_unchanged_and_second_write_rolls_back(self):
        before = file_map(self.project.mirror_model)
        dry = fix.run(self.ws, self.project, [self.rule], dry_run=True)
        self.assertEqual(dry["result"], "DRY_RUN")
        self.assertGreaterEqual(len(dry["files"]), 2)
        self.assertEqual(file_map(self.project.mirror_model), before)
        real = common.write_text
        seen = {"n": 0}

        def wrapped(path, text, bom=b""):
            seen["n"] += 1
            if seen["n"] >= 2:
                raise OSError("second write")
            return real(path, text, bom)

        with patch("pbz.common.write_text", wrapped):
            with self.assertRaises(common.PbzError) as caught:
                fix.run(self.ws, self.project, [self.rule])
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(file_map(self.project.mirror_model), before)
        for dirpath, _dirs, names in os.walk(self.ws.root):
            for name in names:
                self.assertFalse(name.endswith(".pbz-tmp"))

    def test_unreadable_target_refuses_before_write(self):
        before = file_map(self.project.mirror_model)
        def wrapped(path):
            raise UnicodeError("secret-bytes")

        with patch("pbz.fix.read_text", wrapped), patch("pbz.common.commit_bytes", side_effect=AssertionError("commit")):
            with self.assertRaises(common.PbzError) as caught:
                fix.run(self.ws, self.project, [self.rule], dry_run=True)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertNotIn("secret-bytes", str(caught.exception))
        self.assertEqual(file_map(self.project.mirror_model), before)


if __name__ == "__main__":
    unittest.main()
