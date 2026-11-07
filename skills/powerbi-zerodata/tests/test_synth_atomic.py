"""Synthetic build publishes only a validated candidate and never deletes an unowned tree.

Run: python tests/test_synth_atomic.py
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
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import common, mirror, synth  # noqa: E402
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


def _ok(verb, request, timeout=120):
    if verb == "tmdl-validate":
        return {"ok": True, "errors": []}
    return {"ok": False, "class": "ENGINE_UNAVAILABLE"}


class SynthAtomicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["PBZ_SYNTH_ROOT"] = os.path.join(self.tmp.name, "synth")
        root = os.path.join(self.tmp.name, "ws")
        test_parity.build(root)
        self.ws = common.Workspace(root)
        self.ws.private = os.path.join(self.tmp.name, "private")
        os.makedirs(self.ws.private)
        self.project = self.ws.project()
        mirror.pull(self.ws, self.project)
        self.dest = synth.synth_project(self.ws, self.project).ws.root

    def _tmps(self):
        return list(Path(self.tmp.name).rglob("*.pbz-tmp"))

    def test_mid_build_failure_restores_previous_tree(self):
        with patch("pbz.synth.engine_call", _ok):
            built = synth.build(self.ws, self.project, rows=20, seed=1)
        self.assertTrue(built["valid"])
        before = _files(self.dest)
        real = common.write_text

        def boom(path, text, bom=b""):
            raise OSError("mid")

        with patch("pbz.synth.engine_call", _ok), patch("pbz.synth.write_text", boom):
            with self.assertRaises(OSError):
                synth.build(self.ws, self.project, rows=20, seed=1)
        self.assertIs(real, common.write_text)
        self.assertEqual(_files(self.dest), before)
        self.assertFalse(os.path.exists(self.dest + ".pbz-stage"))
        self.assertEqual(self._tmps(), [])

    def test_unavailable_then_success_can_retry(self):
        def missing(verb, request, timeout=120):
            return {"ok": False, "class": "ENGINE_UNAVAILABLE"}

        with patch("pbz.synth.engine_call", missing):
            with self.assertRaises(PbzError) as caught:
                synth.build(self.ws, self.project, rows=20, seed=1)
        self.assertEqual(caught.exception.code, "ENGINE_UNAVAILABLE")
        self.assertFalse(os.path.exists(self.dest))
        self.assertFalse(os.path.exists(self.dest + ".pbz-stage"))
        with patch("pbz.synth.engine_call", _ok):
            built = synth.build(self.ws, self.project, rows=20, seed=1)
        self.assertTrue(built["valid"])
        self.assertTrue(synth._is_synthetic_root(self.dest))
        self.assertNotIn("expressions.tmdl", _files(self.dest))

    def test_unrelated_destination_is_untouched(self):
        os.makedirs(self.dest)
        keep = os.path.join(self.dest, "keep.txt")
        with open(keep, "wb") as f:
            f.write(b"FOREIGN")
        with patch("pbz.synth.engine_call", _ok):
            with self.assertRaises(PbzError) as caught:
                synth.build(self.ws, self.project, rows=20, seed=1)
        self.assertEqual(caught.exception.code, "UNSAFE")
        self.assertEqual(open(keep, "rb").read(), b"FOREIGN")
        self.assertFalse(os.path.exists(self.dest + ".pbz-stage"))
        with self.assertRaises(PbzError) as status:
            synth.status(self.ws, self.project)
        self.assertEqual(status.exception.code, "REFUSED")
        with self.assertRaises(PbzError) as removed:
            synth.remove(self.ws, self.project)
        self.assertEqual(removed.exception.code, "UNSAFE")
        self.assertEqual(open(keep, "rb").read(), b"FOREIGN")

    def test_rejected_or_unavailable_validation_does_not_publish(self):
        with patch("pbz.synth.engine_call", _ok):
            synth.build(self.ws, self.project, rows=20, seed=1)
        before = _files(self.dest)

        def reject(verb, request, timeout=120):
            return {"ok": False, "errors": [{"file": "model.tmdl", "line": 1, "class": "X"}]}

        with patch("pbz.synth.engine_call", reject):
            with self.assertRaises(PbzError) as caught:
                synth.build(self.ws, self.project, rows=20, seed=2)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(_files(self.dest), before)

        def down(verb, request, timeout=120):
            return {"ok": False, "class": "ENGINE_UNAVAILABLE"}

        with patch("pbz.synth.engine_call", down):
            with self.assertRaises(PbzError) as caught:
                synth.build(self.ws, self.project, rows=20, seed=3)
        self.assertEqual(caught.exception.code, "ENGINE_UNAVAILABLE")
        self.assertEqual(_files(self.dest), before)
        self.assertFalse(os.path.exists(self.dest + ".pbz-stage"))
        self.assertEqual(self._tmps(), [])

    def test_failed_first_publish_can_be_retried(self):
        def fail_publish(ops, private=False):
            os.makedirs(self.dest, exist_ok=True)
            with open(os.path.join(self.dest, "partial.txt"), "wb") as f:
                f.write(b"partial")
            raise PbzError("FAILED", "publish")

        with patch("pbz.synth.engine_call", _ok), patch("pbz.synth.commit_bytes", fail_publish):
            with self.assertRaises(PbzError) as caught:
                synth.build(self.ws, self.project, rows=20, seed=1)
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertFalse(os.path.exists(self.dest))
        with patch("pbz.synth.engine_call", _ok):
            built = synth.build(self.ws, self.project, rows=20, seed=1)
        self.assertTrue(built["valid"])
        self.assertTrue(synth._is_synthetic_root(self.dest))


if __name__ == "__main__":
    unittest.main()
