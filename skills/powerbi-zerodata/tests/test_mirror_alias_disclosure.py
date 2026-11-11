"""diff --patch and push refuse aliased or hardlinked mirror files.

Run: python tests/test_mirror_alias_disclosure.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

from pbz.common import PbzError, Project, Workspace  # noqa: E402
from pbz import guard, mirror  # noqa: E402

CANARY = "CANARY-LITERAL-99"


def _reset():
    guard._ROOT.clear()
    guard._SYN.clear()
    guard._POL.clear()


class MirrorAliasDisclosureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.private_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.private_tmp.cleanup)
        os.environ["PBZ_PRIVATE_ROOT"] = self.private_tmp.name
        self.addCleanup(lambda: os.environ.pop("PBZ_PRIVATE_ROOT", None))
        self.ws = Workspace(self.tmp.name)
        open(os.path.join(self.tmp.name, "X.pbip"), "w", encoding="utf-8").close()
        self.project = Project(self.ws, "X")
        os.makedirs(self.project.model_dir + os.sep + "definition", exist_ok=True)
        os.makedirs(self.project.mirror_model + os.sep + "definition", exist_ok=True)
        self.real = os.path.join(self.project.model_dir, "definition", "model.tmdl")
        self.mirror_file = os.path.join(self.project.mirror_model, "definition", "model.tmdl")
        with open(self.real, "w", encoding="utf-8") as handle:
            handle.write(CANARY + "\n")
        self.rel = "X.SemanticModel/definition/model.tmdl"
        os.makedirs(self.ws.private, exist_ok=True)
        _reset()

    def _manifest(self, mirror_hash):
        mirror.save_manifest(self.ws, self.project, {"version": 1, "project": "X", "files": {
            self.rel: {"real": "abc", "mirror": mirror_hash}}})

    def test_hardlink_diff_and_push_refuse_without_canary(self):
        try:
            os.link(self.real, self.mirror_file)
        except OSError:
            self.skipTest("hardlink is unavailable")
        digest = hashlib.sha256(open(self.real, "rb").read()).hexdigest()
        self._manifest("0" * 64)
        before = open(self.real, "rb").read()
        man_before = open(mirror.manifest_path(self.ws, self.project), "rb").read()
        with self.assertRaises(PbzError) as caught:
            mirror.diff(self.ws, self.project, patch=True)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertNotIn(CANARY, str(caught.exception))
        self.assertNotIn(CANARY, caught.exception.detail)
        with self.assertRaises(PbzError) as pushed:
            mirror.push(self.ws, self.project)
        self.assertEqual(pushed.exception.code, "REFUSED")
        self.assertNotIn(CANARY, str(pushed.exception))
        self.assertEqual(open(self.real, "rb").read(), before)
        self.assertEqual(open(mirror.manifest_path(self.ws, self.project), "rb").read(), man_before)
        self.assertEqual(list(__import__("pathlib").Path(self.ws.root).rglob("*.pbz-tmp")), [])
        self.assertNotEqual(digest, "")

    def _swap_open(self, path, secret, reads):
        real_open = open

        class Guard:
            def __init__(self, raw):
                self.raw = raw

            def read(self, *args, **kwargs):
                reads["n"] += 1
                return self.raw.read(*args, **kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.raw.close()

            def fileno(self):
                return self.raw.fileno()

            def close(self):
                self.raw.close()

        def swapped(target, *args, **kwargs):
            if os.path.normcase(str(target)) != os.path.normcase(path):
                return real_open(target, *args, **kwargs)
            return Guard(real_open(secret, *args, **kwargs))

        return swapped

    def test_substituted_handle_during_diff_discovery_refuses(self):
        text = "table T\n"
        with open(self.mirror_file, "w", encoding="utf-8") as handle:
            handle.write(text)
        self._manifest("0" * 64)
        secret = os.path.join(self.tmp.name, "outside-model.tmdl")
        with open(secret, "wb") as handle:
            handle.write(CANARY.encode())
        reads = {"n": 0}
        before = open(self.real, "rb").read()
        man_before = open(mirror.manifest_path(self.ws, self.project), "rb").read()
        with patch("builtins.open", self._swap_open(self.mirror_file, secret, reads)):
            with self.assertRaises(PbzError) as caught:
                mirror.diff(self.ws, self.project, patch=True)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertNotIn(CANARY, str(caught.exception))
        self.assertEqual(reads["n"], 0)
        self.assertEqual(open(self.real, "rb").read(), before)
        self.assertEqual(open(mirror.manifest_path(self.ws, self.project), "rb").read(), man_before)
        self.assertEqual(open(secret, "rb").read(), CANARY.encode())

    def test_substituted_handle_during_push_post_hash_refuses(self):
        text = "table T\n"
        with open(self.mirror_file, "w", encoding="utf-8") as handle:
            handle.write(text)
        digest = hashlib.sha256(text.encode()).hexdigest()
        real_digest = hashlib.sha256(open(self.real, "rb").read()).hexdigest()
        mirror.save_manifest(self.ws, self.project, {"version": 1, "project": "X", "files": {
            self.rel: {"real": real_digest, "mirror": "0" * 64}}})
        secret = os.path.join(self.tmp.name, "swap-model.tmdl")
        with open(secret, "wb") as handle:
            handle.write(CANARY.encode())
        reads = {"n": 0}
        before = open(self.real, "rb").read()
        man_before = open(mirror.manifest_path(self.ws, self.project), "rb").read()
        swap = self._swap_open(self.mirror_file, secret, reads)
        real_open = open
        state = {"phase": "pre"}

        def after_validate(*args, **kwargs):
            state["phase"] = "post"
            return []

        def phased(target, *args, **kwargs):
            if state["phase"] == "post" and os.path.normcase(str(target)) == os.path.normcase(self.mirror_file):
                return swap(target, *args, **kwargs)
            return real_open(target, *args, **kwargs)

        with patch("pbz.mirror.validate.run_stage", side_effect=after_validate), \
             patch("pbz.mirror._select", return_value=({"changed": [self.rel], "added": [], "deleted": []}, [])), \
             patch("pbz.mirror.project_files", return_value=[]), \
             patch("pbz.mirror.excluded_mirror_additions", return_value=[]), \
             patch("pbz.mirror.desktop.find_instance", return_value=None), \
             patch("builtins.open", side_effect=phased):
            with self.assertRaises(PbzError) as caught:
                mirror._push(self.ws, self.project)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertNotIn(CANARY, str(caught.exception))
        self.assertEqual(reads["n"], 0)
        self.assertEqual(open(self.real, "rb").read(), before)
        self.assertEqual(open(mirror.manifest_path(self.ws, self.project), "rb").read(), man_before)
        self.assertEqual(list(__import__("pathlib").Path(self.ws.root).rglob("*.pbz-tmp")), [])
        stage = os.path.join(self.ws.private, "stage")
        if os.path.isdir(stage):
            self.assertEqual(list(__import__("pathlib").Path(stage).iterdir()), [])
        self.assertFalse(os.path.lexists(os.path.join(self.ws.private, "transactions",
                                                      self.project.name, ".commit-lock")))
        self.assertNotEqual(digest, None)

    def test_deleted_and_added_are_not_refused(self):
        real_digest = hashlib.sha256(open(self.real, "rb").read()).hexdigest()
        with open(self.mirror_file, "w", encoding="utf-8") as handle:
            handle.write("table T\n")
        mirror.save_manifest(self.ws, self.project, {"version": 1, "project": "X", "files": {
            self.rel: {"real": real_digest, "mirror": "0" * 64}}})
        out = mirror.diff(self.ws, self.project, patch=True)
        self.assertIn(self.rel, out["changed"] + out["added"])
        self.assertNotIn(CANARY, out.get("patch", ""))
        os.remove(self.mirror_file)
        deleted = mirror.diff(self.ws, self.project, patch=True)
        self.assertIn(self.rel, deleted["deleted"])
        added_rel = "X.SemanticModel/definition/extra.tmdl"
        with open(os.path.join(self.project.mirror_model, "definition", "extra.tmdl"), "w", encoding="utf-8") as handle:
            handle.write("table Extra\n")
        added = mirror.diff(self.ws, self.project, patch=True)
        self.assertIn(added_rel, added["added"])
        validated = {"n": 0}

        def mark(*args, **kwargs):
            validated["n"] += 1
            return []

        with patch("pbz.mirror.validate.run_stage", side_effect=mark), \
             patch("pbz.mirror.desktop.find_instance", return_value=None):
            result = mirror._push(self.ws, self.project, dry_run=True)
        self.assertNotEqual(result.get("result"), "REFUSED")
        self.assertNotEqual(result.get("error"), "REFUSED")
        self.assertIn(result.get("result"), ("DRY_RUN_OK", "CONFLICT", "INVALID", "NOTHING_TO_PUSH"))
        self.assertGreaterEqual(validated["n"], 1)

    def test_post_validation_delete_is_conflict(self):
        text = "table T\n"
        with open(self.mirror_file, "w", encoding="utf-8") as handle:
            handle.write(text)
        digest = hashlib.sha256(text.encode()).hexdigest()
        real_digest = hashlib.sha256(open(self.real, "rb").read()).hexdigest()
        mirror.save_manifest(self.ws, self.project, {"version": 1, "project": "X", "files": {
            self.rel: {"real": real_digest, "mirror": "0" * 64}}})
        before = open(self.real, "rb").read()
        man_before = open(mirror.manifest_path(self.ws, self.project), "rb").read()
        ran = {"n": 0}

        def drop(*args, **kwargs):
            ran["n"] += 1
            os.remove(self.mirror_file)
            return []

        with patch("pbz.mirror.validate.run_stage", side_effect=drop), \
             patch("pbz.mirror._select", return_value=({"changed": [self.rel], "added": [], "deleted": []}, [])), \
             patch("pbz.mirror.project_files", return_value=[]), \
             patch("pbz.mirror.excluded_mirror_additions", return_value=[]), \
             patch("pbz.mirror.desktop.find_instance", return_value=None):
            try:
                result = mirror._push(self.ws, self.project)
            except PbzError as err:
                self.fail(err.code)
        self.assertEqual(ran["n"], 1)
        self.assertFalse(os.path.lexists(self.mirror_file))
        self.assertEqual(result.get("result"), "CONFLICT")
        self.assertEqual(open(self.real, "rb").read(), before)
        self.assertEqual(open(mirror.manifest_path(self.ws, self.project), "rb").read(), man_before)
        stage = os.path.join(self.ws.private, "stage")
        if os.path.isdir(stage):
            self.assertEqual(list(__import__("pathlib").Path(stage).iterdir()), [])
        self.assertFalse(os.path.lexists(os.path.join(self.ws.private, "transactions",
                                                      self.project.name, ".commit-lock")))
        self.assertEqual(list(__import__("pathlib").Path(self.ws.root).rglob("*.pbz-tmp")), [])
        self.assertNotEqual(digest, None)

    def test_guard_denies_hardlinked_mirror_read_and_search(self):
        try:
            os.link(self.real, self.mirror_file)
        except OSError:
            self.skipTest("hardlink is unavailable")
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        with open(self.ws.policy_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"version": 1}))
        _reset()
        event = {"tool_name": "Read", "cwd": self.ws.root, "tool_input": {"file_path": self.mirror_file}}
        self.assertEqual(guard.decide(event), "definition")
        _reset()
        grep = {"tool_name": "Grep", "cwd": self.ws.root, "tool_input": {"path": self.ws.mirror}}
        self.assertEqual(guard.decide(grep), "search")
        _reset()
        self.assertEqual(self._shell("rg secret pbz-workspace", self.ws.root), "search")
        clean = os.path.join(self.ws.mirror, "X.SemanticModel", "definition", "clean.tmdl")
        with open(clean, "w", encoding="utf-8") as handle:
            handle.write("table Clean\n")
        _reset()
        # The hardlinked sibling still makes a directory search denied. A direct read of the clean file is allowed.
        self.assertIsNone(guard.classify_path(guard.norm(clean)))

    def _shell(self, cmd, cwd):
        _reset()
        return guard.check_shell(cmd, cwd)

    def test_status_does_not_use_checked_hash(self):
        with open(self.mirror_file, "w", encoding="utf-8") as handle:
            handle.write("table T\n")
        self._manifest(hashlib.sha256(open(self.mirror_file, "rb").read()).hexdigest())
        changes = mirror.mirror_changes(self.ws, self.project, mirror.load_manifest(self.ws, self.project))
        self.assertEqual(changes["changed"], [])

    def test_checked_missing_never_calls_file_sha(self):
        self._manifest("0" * 64)
        if os.path.lexists(self.mirror_file):
            os.remove(self.mirror_file)
        with patch("pbz.mirror.file_sha", side_effect=AssertionError("file_sha must not run")):
            out = mirror.mirror_changes(self.ws, self.project,
                                        mirror.load_manifest(self.ws, self.project), checked=True)
        self.assertEqual(out["deleted"], [self.rel])
        self.assertEqual(out["changed"], [])

    def test_push_summary_does_not_reread_deleted_mirror(self):
        text = "table T\n"
        with open(self.mirror_file, "w", encoding="utf-8") as handle:
            handle.write(text)
        real_digest = hashlib.sha256(open(self.real, "rb").read()).hexdigest()
        mirror.save_manifest(self.ws, self.project, {"version": 1, "project": "X", "files": {
            self.rel: {"real": real_digest, "mirror": hashlib.sha256(text.encode()).hexdigest()}}})
        os.remove(self.mirror_file)
        secret = os.path.join(self.tmp.name, "alias-target.tmdl")
        with open(secret, "wb") as handle:
            handle.write(CANARY.encode())
        reads = {"n": 0}
        real_open = open

        def watch(target, *args, **kwargs):
            handle = real_open(target, *args, **kwargs)
            if os.path.normcase(str(target)) == os.path.normcase(self.mirror_file):
                raw_read = handle.read

                def counted(*a, **k):
                    reads["n"] += 1
                    return raw_read(*a, **k)

                handle.read = counted
            return handle

        def plant_alias(*args, **kwargs):
            try:
                os.link(secret, self.mirror_file)
            except OSError:
                self.skipTest("hardlink is unavailable")
            return []

        with patch("pbz.mirror.validate.run_stage", side_effect=plant_alias), \
             patch("pbz.mirror._select", return_value=({"changed": [], "added": [], "deleted": [self.rel]}, [])), \
             patch("pbz.mirror.project_files", return_value=[]), \
             patch("pbz.mirror.excluded_mirror_additions", return_value=[]), \
             patch("pbz.mirror.desktop.find_instance", return_value=None), \
             patch("builtins.open", side_effect=watch):
            result = mirror._push(self.ws, self.project, dry_run=True)
        self.assertNotEqual(result.get("result"), "PUSHED")
        self.assertEqual(reads["n"], 0)
        self.assertNotIn(CANARY, json.dumps(result))
        self.assertEqual(open(secret, "rb").read(), CANARY.encode())

    def test_guard_denies_mirror_root_junction_and_page_leaf(self):
        from _junction import make_junction
        import shutil
        outside = os.path.join(self.tmp.name, "outside-mirror")
        os.makedirs(outside)
        with open(os.path.join(outside, "secret.tmdl"), "wb") as handle:
            handle.write(CANARY.encode())
        shutil.rmtree(self.ws.mirror)
        make_junction(self, self.ws.mirror, outside)
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        with open(self.ws.policy_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"version": 1}))
        _reset()
        leaf = os.path.join(self.ws.mirror, "secret.tmdl")
        event = {"tool_name": "Read", "cwd": self.ws.root, "tool_input": {"file_path": leaf}}
        self.assertIsNotNone(guard.decide(event))
        # Separate tree: junctioned pages directory under a fresh mirror root.
        _reset()
        if os.path.isdir(self.ws.mirror) or os.path.islink(self.ws.mirror) or os.path.lexists(self.ws.mirror):
            try:
                os.remove(self.ws.mirror)
            except OSError:
                shutil.rmtree(self.ws.mirror, ignore_errors=True)
        report_def = os.path.join(self.ws.mirror, "X.Report", "definition")
        os.makedirs(report_def, exist_ok=True)
        page_outside = os.path.join(self.tmp.name, "page-out")
        os.makedirs(page_outside)
        pages = os.path.join(report_def, "pages")
        make_junction(self, pages, page_outside)
        visual = os.path.join(pages, "p1", "visuals", "v1", "visual.json")
        os.makedirs(os.path.dirname(visual), exist_ok=True)
        with open(visual, "w", encoding="utf-8") as handle:
            handle.write("{}")
        _reset()
        page_event = {"tool_name": "Read", "cwd": self.ws.root, "tool_input": {"file_path": visual}}
        self.assertIsNotNone(guard.decide(page_event))


if __name__ == "__main__":
    unittest.main()

