"""Handoff refuses a bad cursor or log before writing, and commits the packet once.

Run: python tests/test_handoff_atomic.py
"""
from __future__ import annotations

import hashlib
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

import test_parity  # noqa: E402
from pbz import common, handoff, mirror  # noqa: E402
from pbz.common import PbzError  # noqa: E402


def _files(root: str) -> dict[str, bytes]:
    out = {}
    if not os.path.isdir(root):
        return out
    for cur, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(cur, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, root).replace("\\", "/")] = f.read()
    return out


class HandoffAtomicTests(unittest.TestCase):
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
        self.state = os.path.join(self.ws.pbz_dir, "handoff.json")
        self.log = os.path.join(self.ws.pbz_dir, "push-log.jsonl")

    def _log(self, line="{}"):
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        with open(self.log, "w", encoding="utf-8") as f:
            f.write(line if line.endswith("\n") else line + "\n")

    def test_bad_state_and_log_refuse_before_write(self):
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        with open(self.state, "w", encoding="utf-8") as f:
            f.write("{")
        before = _files(self.ws.mirror)
        with self.assertRaises(PbzError) as caught:
            handoff.build(self.ws, self.project)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(_files(self.ws.mirror), before)
        os.remove(self.state)
        self._log("not-json")
        with self.assertRaises(PbzError) as caught:
            handoff.build(self.ws, self.project)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertFalse(os.path.exists(os.path.join(self.ws.mirror, "HANDOFF.md")))

    def test_mid_commit_restores(self):
        self._log(json.dumps({"project": "Parity", "stamp": "2026-01-01T00:00:00", "objects": ["+ measure Total"]}))
        before = _files(self.ws.root)
        real = common.write_text
        seen = {"n": 0}

        def boom(path, text, bom=b""):
            seen["n"] += 1
            if seen["n"] > 1:
                raise OSError("mid")
            return real(path, text, bom)

        with patch("pbz.evidence.mirror_rev", return_value="m"), patch("pbz.evidence.real_rev", return_value="r"), \
                patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                handoff.build(self.ws, self.project)
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(_files(self.ws.root), before)
        self.assertEqual(list(Path(self.ws.root).rglob("*.pbz-tmp")), [])
        self.assertNotIn("share the specific numbers", open(
            os.path.join(SKILL, "pbz", "handoff.py"), encoding="utf-8").read())

    def test_accept_failure_keeps_cursor(self):
        text = "# checklist\n"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
        packet = os.path.join(self.ws.mirror, "handoffs", digest + ".md")
        os.makedirs(os.path.dirname(packet), exist_ok=True)
        with open(packet, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        cursor = {"version": 2, "projects": {"Parity": {
            "generated": "2026-01-01T00:00:00",
            "packet": f"pbz-workspace/handoffs/{digest}.md",
            "revision": {"mirror": "m", "real": "r"}}}}
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        raw = json.dumps(cursor)
        with open(self.state, "w", encoding="utf-8") as f:
            f.write(raw)
        with patch("pbz.common.write_text", side_effect=OSError("accept")):
            with self.assertRaises(PbzError) as caught:
                handoff.accept_generated(self.ws, self.project, "m", "r")
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(open(self.state, encoding="utf-8").read(), raw)


if __name__ == "__main__":
    unittest.main()
