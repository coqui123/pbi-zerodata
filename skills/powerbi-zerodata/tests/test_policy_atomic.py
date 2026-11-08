"""Policy writes use the hardened writer and the workspace root, not .pbz as the trust root.

Run: python tests/test_policy_atomic.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz.common import PbzError, Workspace, write_text  # noqa: E402
from pbz import policy  # noqa: E402


class PolicyAtomicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(self.tmp.name)
        open(os.path.join(self.tmp.name, "Canary.pbip"), "w", encoding="utf-8").close()

    def _junction(self, link: str, target: str) -> None:
        made = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True)
        if made.returncode != 0:
            self.skipTest("creating a junction is unavailable")

    def test_save_writes_policy_under_the_workspace(self):
        policy.save(self.ws, {"version": 1, "disclosure": "strict"})
        body = json.loads(open(self.ws.policy_path, encoding="utf-8").read())
        self.assertEqual(body["disclosure"], "strict")
        self.assertEqual(list(Path(self.ws.pbz_dir).glob("*.pbz-tmp")), [])

    def test_pbz_junction_is_refused_and_external_bytes_stay(self):
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        outside = external.name
        secret = os.path.join(outside, "policy.json")
        with open(secret, "wb") as f:
            f.write(b"EXTERNAL")
        self._junction(self.ws.pbz_dir, outside)
        with self.assertRaises(PbzError) as caught:
            policy.save(self.ws, {"version": 1})
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(open(secret, "rb").read(), b"EXTERNAL")
        self.assertEqual(list(Path(outside).glob("*.pbz-tmp")), [])

    def test_policy_leaf_link_is_refused(self):
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        secret = os.path.join(external.name, "policy.json")
        with open(secret, "wb") as f:
            f.write(b"EXTERNAL-LEAF")
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        try:
            os.symlink(secret, self.ws.policy_path)
        except OSError:
            self.skipTest("file symlink is unavailable")
        with self.assertRaises(PbzError) as caught:
            policy.save(self.ws, {"version": 1, "disclosure": "strict"})
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(open(secret, "rb").read(), b"EXTERNAL-LEAF")
        self.assertEqual(list(Path(self.ws.pbz_dir).glob("*.pbz-tmp")), [])

    def test_failed_replace_keeps_old_policy_bytes(self):
        policy.save(self.ws, {"version": 1, "disclosure": "shape"})
        before = open(self.ws.policy_path, "rb").read()
        real = os.replace

        def boom(src, dst):
            if str(src).endswith(".pbz-tmp"):
                raise OSError("replace")
            return real(src, dst)

        with patch("os.replace", boom):
            with self.assertRaises(OSError):
                policy.save(self.ws, {"version": 1, "disclosure": "strict"})
        self.assertEqual(open(self.ws.policy_path, "rb").read(), before)
        self.assertEqual(list(Path(self.ws.pbz_dir).glob("*.pbz-tmp")), [])

    def test_zero_progress_write_leaves_no_owned_temp(self):
        path = os.path.join(self.tmp.name, "note.txt")
        real = os.write

        def stall(fd, data):
            if getattr(stall, "once", False):
                return real(fd, data)
            stall.once = True
            return 0

        with patch("os.write", stall):
            with self.assertRaises(OSError):
                write_text(path, "NEW", root=self.ws.root)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(list(Path(self.tmp.name).glob("*.pbz-tmp")), [])

    def test_tighten_round_trip(self):
        policy.save(self.ws, dict(policy.DEFAULT))
        added = policy.tighten(self.ws, deny=["**/*.secret"])
        self.assertEqual(added["deny"], ["**/*.secret"])
        body = json.loads(open(self.ws.policy_path, encoding="utf-8").read())
        self.assertIn("**/*.secret", body["deny"])
        again = policy.tighten(self.ws, deny=["**/*.secret"])
        self.assertEqual(again["deny"], [])
        body = json.loads(open(self.ws.policy_path, encoding="utf-8").read())
        self.assertEqual(body["deny"].count("**/*.secret"), 1)

    def test_workspace_via_junction_still_writes(self):
        real = os.path.join(self.tmp.name, "real-ws")
        os.makedirs(real)
        link = os.path.join(self.tmp.name, "linked-ws")
        self.assertFalse(os.path.exists(os.path.join(real, ".pbz")))
        self._junction(link, real)
        ws = Workspace(link)
        open(os.path.join(link, "Canary.pbip"), "w", encoding="utf-8").close()
        policy.save(ws, {"version": 1, "disclosure": "strict"})
        body = json.loads(open(ws.policy_path, encoding="utf-8").read())
        self.assertEqual(body["disclosure"], "strict")


if __name__ == "__main__":
    unittest.main()
