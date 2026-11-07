"""Pending recovery blocks every pull, and a bad journal is not echoed.
Run: python tests/test_mirror_recovery_honesty.py
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
from pbz import common, mirror  # noqa: E402

SECRET = "SECRET_JOURNAL_PAYLOAD_9f3a"


def file_map(root):
    out = {}
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace("\\", "/")
            with open(path, "rb") as f:
                out[rel] = f.read()
    return out


class RecoveryHonestyTests(unittest.TestCase):
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

    def _journal(self, body):
        directory = os.path.join(mirror._transaction_dir(self.ws, self.project), "tx1")
        os.makedirs(directory)
        with open(os.path.join(directory, "journal.json"), "w", encoding="utf-8") as f:
            json.dump(body, f)

    def test_pending_recovery_blocks_every_pull(self):
        self._journal({"version": 1, "state": "APPLYING", "entries": []})
        before = file_map(self.ws.mirror)
        for kwargs in ({}, {"force": True}, {"rebase": True}):
            out = mirror.pull(self.ws, self.project, **kwargs)
            self.assertEqual(out["result"], "RECOVERY_REQUIRED")
            self.assertEqual(file_map(self.ws.mirror), before)

    def test_malformed_journal_refuses_restore_without_payload(self):
        self._journal({"version": 1, "state": SECRET, "entries": [{"note": SECRET}]})
        before = file_map(self.ws.root)
        pending = mirror.pending_recovery(self.ws, self.project)
        self.assertEqual(pending, [{"id": "tx1", "state": "INVALID"}])
        self.assertNotIn(SECRET, json.dumps(pending))
        out = mirror.recover(self.ws, self.project, "tx1")
        blob = json.dumps(out)
        self.assertEqual(out["result"], "RECOVERY_REQUIRED")
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("note", blob)
        self.assertEqual(file_map(self.ws.root), before)


if __name__ == "__main__":
    unittest.main()
