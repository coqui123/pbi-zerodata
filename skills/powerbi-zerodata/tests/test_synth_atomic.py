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

        def boom(path, text, bom=b"", *, root=None, **_kw):
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
        self.assertIn("SYNTH_VALIDATE_REJECTED", str(caught.exception))
        self.assertNotIn("model.tmdl", str(caught.exception))
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

    def test_validation_diagnostic_drops_raw_engine_text(self):
        with patch("pbz.synth.engine_call", _ok):
            synth.build(self.ws, self.project, rows=20, seed=1)
        canary = "CANARY_TOM_MESSAGE"
        secret = "C:/synthetic-private/model.tmdl"

        def reject(verb, request, timeout=120):
            return {"ok": False, "errors": [
                {"file": secret, "line": 4, "class": "SYNTAX", "message": canary},
                {"file": secret, "line": 9, "class": "not-a-class", "message": canary},
            ], "missing_refs": [secret]}

        with patch("pbz.synth.engine_call", reject):
            with self.assertRaises(PbzError) as caught:
                synth.build(self.ws, self.project, rows=20, seed=2)
        text = str(caught.exception)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertIn("SYNTH_VALIDATE_REJECTED", text)
        self.assertNotIn("count=", text)
        self.assertNotIn("classes=", text)
        self.assertNotIn(canary, text)
        self.assertNotIn(secret, text)
        self.assertNotIn("not-a-class", text)

    def test_failed_first_publish_can_be_retried(self):
        def fail_publish(ops, private=False, *, root=None, roots=None, **_kw):
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

    def test_hardlinked_source_is_refused_before_read(self):
        target = next(Path(self.project.mirror_model).rglob("*.tmdl"))
        canary = target.with_name("CANARY-HARD.tmdl")
        with open(canary, "wb") as f:
            f.write(b"CANARY-HARD")
        os.link(canary, target.with_name("linked-" + canary.name))
        before = _files(self.dest)
        with self.assertRaises(PbzError) as caught:
            synth.build(self.ws, self.project, rows=20, seed=1)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertNotIn("CANARY-HARD", str(caught.exception))
        self.assertEqual(_files(self.dest), before)
        self.assertFalse(os.path.exists(self.dest + ".pbz-stage"))

    def test_replacement_between_check_and_read_is_refused(self):
        real_open = open
        swapped = {"done": False}

        def swapping(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if not swapped["done"] and "rb" in str(mode) and str(path).lower().endswith(".tmdl"):
                swapped["done"] = True
                with real_open(path, "wb") as out:
                    out.write(b"CANARY-SWAP")
            return real_open(path, *args, **kwargs)

        before = _files(self.dest)
        with patch("builtins.open", swapping):
            with self.assertRaises(PbzError) as caught:
                synth.build(self.ws, self.project, rows=20, seed=1)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertNotIn("CANARY-SWAP", str(caught.exception))
        self.assertEqual(_files(self.dest), before)
        self.assertFalse(os.path.exists(self.dest + ".pbz-stage"))

    def test_excluded_cache_never_reaches_destination(self):
        cache = os.path.join(self.project.mirror_model, ".pbi")
        os.makedirs(cache, exist_ok=True)
        with open(os.path.join(cache, "cache.txt"), "wb") as f:
            f.write(b"CANARY-CACHE")
        with patch("pbz.synth.engine_call", _ok):
            synth.build(self.ws, self.project, rows=20, seed=1)
        published = _files(self.dest)
        self.assertFalse(any("CANARY-CACHE" in rel or b"CANARY-CACHE" in data for rel, data in published.items()))
        self.assertFalse(os.path.exists(self.dest + ".pbz-stage"))

    def test_malformed_validation_envelope_is_closed(self):
        def bad(verb, request, timeout=120):
            return {"ok": "yes", "errors": "x"}

        before = _files(self.dest)
        with patch("pbz.synth.engine_call", bad):
            with self.assertRaises(PbzError) as caught:
                synth.build(self.ws, self.project, rows=20, seed=1)
        text = str(caught.exception)
        self.assertEqual(caught.exception.code, "ENGINE_UNAVAILABLE")
        self.assertNotIn("yes", text)
        self.assertNotIn("count=", text)
        self.assertNotIn("classes=", text)
        self.assertEqual(_files(self.dest), before)
        self.assertFalse(os.path.exists(self.dest + ".pbz-stage"))
        self.assertEqual(self._tmps(), [])

    def test_null_collections_do_not_publish(self):
        published = {"n": 0}
        real_publish = synth._publish_candidate

        def watch(stage, dest, owned):
            published["n"] += 1
            return real_publish(stage, dest, owned)

        for payload in (
            {"ok": True, "errors": None, "missing_refs": []},
            {"ok": True, "errors": [], "missing_refs": None},
            {"ok": True, "errors": "x", "missing_refs": []},
        ):
            published["n"] = 0

            def bad(verb, request, timeout=120, body=payload):
                return body

            before = _files(self.dest)
            with patch("pbz.synth.engine_call", bad), patch("pbz.synth._publish_candidate", watch):
                with self.assertRaises(PbzError) as caught:
                    synth.build(self.ws, self.project, rows=20, seed=1)
            self.assertEqual(caught.exception.code, "ENGINE_UNAVAILABLE")
            self.assertEqual(published["n"], 0)
            self.assertEqual(_files(self.dest), before)
            self.assertFalse(os.path.exists(self.dest + ".pbz-stage"))
            self.assertEqual(self._tmps(), [])

    def test_open_swaps_to_external_file_and_restores_path(self):
        external = os.path.join(self.tmp.name, "external.tmdl")
        canary = b"CANARY-OPEN-SWAP"
        with open(external, "wb") as f:
            f.write(canary)
        real_open = open
        swapped = {"done": False}
        captured = []

        def swapping(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            text = str(path)
            if (not swapped["done"] and "rb" in str(mode) and text.lower().endswith(".tmdl")
                    and os.path.abspath(text) != os.path.abspath(external)):
                swapped["done"] = True
                held = text + ".held"
                os.replace(text, held)
                handle = real_open(external, "rb")
                os.replace(held, text)

                def refuse(*_a, **_k):
                    raise AssertionError("capture read the swapped handle")

                handle.read = refuse
                return handle
            return real_open(path, *args, **kwargs)

        real_capture = synth._capture_source

        def watch(root, boundary):
            data = real_capture(root, boundary)
            captured.append(data)
            return data

        before = _files(self.dest)
        with patch("builtins.open", swapping), patch("pbz.synth._capture_source", watch):
            with self.assertRaises(PbzError) as caught:
                synth.build(self.ws, self.project, rows=20, seed=1)
        self.assertTrue(swapped["done"])
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertNotIn("CANARY-OPEN-SWAP", str(caught.exception))
        self.assertFalse(any(canary in blob for data in captured for blob in data.values()))
        self.assertEqual(_files(self.dest), before)
        self.assertFalse(os.path.exists(self.dest + ".pbz-stage"))

    def test_unknown_top_level_class_does_not_publish(self):
        published = {"n": 0}
        real_publish = synth._publish_candidate

        def watch(stage, dest, owned):
            published["n"] += 1
            return real_publish(stage, dest, owned)

        def bad(verb, request, timeout=120):
            return {"ok": True, "class": "CANARY-INVALID-CLASS", "errors": [], "missing_refs": []}

        before = _files(self.dest)
        with patch("pbz.synth.engine_call", bad), patch("pbz.synth._publish_candidate", watch):
            with self.assertRaises(PbzError) as caught:
                synth.build(self.ws, self.project, rows=20, seed=1)
        self.assertEqual(caught.exception.code, "ENGINE_UNAVAILABLE")
        self.assertNotIn("CANARY-INVALID-CLASS", str(caught.exception))
        self.assertEqual(published["n"], 0)
        self.assertEqual(_files(self.dest), before)
        self.assertFalse(os.path.exists(self.dest + ".pbz-stage"))

    def test_mirror_ancestor_junction_is_refused(self):
        outside = os.path.join(self.tmp.name, "external-mirror")
        os.makedirs(os.path.join(outside, "definition"))
        with open(os.path.join(outside, "definition", "model.tmdl"), "wb") as f:
            f.write(b"CANARY-ANCESTOR")
        link = os.path.join(self.ws.root, "_redirected")
        try:
            made = __import__("subprocess").run(
                ["cmd", "/c", "mklink", "/J", link, outside], capture_output=True)
        except OSError:
            self.skipTest("creating a junction is unavailable")
        if made.returncode != 0:
            self.skipTest("creating a junction is unavailable")
        before = _files(self.dest)
        with self.assertRaises(PbzError) as caught:
            synth._capture_source(link, self.ws.root)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertNotIn("CANARY-ANCESTOR", str(caught.exception))
        self.assertEqual(_files(self.dest), before)

    def test_workspace_via_junction_still_builds(self):
        real = self.ws.root
        via = os.path.join(self.tmp.name, "via-ws")
        try:
            made = __import__("subprocess").run(
                ["cmd", "/c", "mklink", "/J", via, real], capture_output=True)
        except OSError:
            self.skipTest("creating a junction is unavailable")
        if made.returncode != 0:
            self.skipTest("creating a junction is unavailable")
        linked = common.Workspace(via)
        linked.private = self.ws.private
        project = linked.project()
        with patch("pbz.synth.engine_call", _ok):
            out = synth.build(linked, project, rows=20, seed=1)
        self.assertTrue(out.get("valid"))
        self.assertTrue(synth._is_synthetic_root(self.dest) or os.path.isdir(out["root"]))
        published = _files(out["root"])
        self.assertFalse(any(b"CANARY" in blob for blob in published.values()))


if __name__ == "__main__":
    unittest.main()
