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

        def boom(path, text, bom=b"", *, root=None, **_kw):
            seen["n"] += 1
            if seen["n"] > 1:
                raise OSError("mid")
            return real(path, text, bom, root=root)

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

    def test_page_segment_skips_escape_and_reraises_checked_read(self):
        outside = os.path.join(self.tmp.name, "outside")
        os.makedirs(outside)
        outside_page = os.path.join(outside, "page.json")
        payload = b'{"displayName":"OUTSIDE"}'
        with open(outside_page, "wb") as f:
            f.write(payload)
        pages = os.path.join(self.project.mirror_report, "definition", "pages")
        rel = os.path.relpath(outside, pages)
        self.assertNotIn("/", rel)
        self.assertIn("\\", rel)
        self.assertIn("..", rel)
        self.assertEqual(self.project.name, "Parity")
        self._log(json.dumps({
            "project": "Parity",
            "stamp": "2026-01-01T00:00:00",
            "objects": ["/pages/" + rel + "/"],
        }))
        with patch("pbz.evidence.mirror_rev", return_value="m"), patch("pbz.evidence.real_rev", return_value="r"):
            out = handoff.build(self.ws, self.project)
        self.assertIn("checklist", out)
        self.assertNotIn("OUTSIDE", out["checklist"])
        self.assertNotIn("OUTSIDE", json.dumps(out))
        for path in Path(self.tmp.name).rglob("HANDOFF.md"):
            self.assertNotIn("OUTSIDE", path.read_text(encoding="utf-8"))
            path.unlink()
        self.assertEqual(Path(outside_page).read_bytes(), payload)

        self._log(json.dumps({
            "project": "Parity",
            "stamp": "2026-01-02T00:00:00",
            "objects": ["/pages/p1/visuals/v1/"],
        }))
        original = handoff.read_checked

        def refuse(path, *, root=None, max_bytes=None):
            if os.path.basename(path) == "page.json":
                raise PbzError("UNSAFE_PATH", "refusing to write through a link")
            return original(path, root=root, max_bytes=max_bytes)

        with patch("pbz.handoff.read_checked", side_effect=refuse):
            with self.assertRaises(PbzError) as caught:
                handoff.build(self.ws, self.project)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(list(Path(self.tmp.name).rglob("HANDOFF.md")), [])
        self.assertEqual(Path(outside_page).read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
