"""Checked withheld absence is clean for status, pull, and push."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pbz import mirror
from mirror_helpers import make_workspace, make_project


class WithheldFilesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = make_workspace(tmp.name)
        self.project = make_project(self.ws)
        self.withheld = "Canary.SemanticModel/definition/tables/T.tmdl"
        self.report = "Canary.Report/definition/report.json"
        self.real = Path(self.ws.root, self.withheld)
        self.mine = Path(self.ws.mirror, self.withheld)
        # An unknown token in an unclassified property fails real detokenization.
        with self.real.open("a", encoding="utf-8") as handle:
            handle.write("\tlineageTag: «unmapped#1»\n")
        self.original = self.real.read_bytes()
        pulled = mirror.pull(self.ws, self.project)
        self.assertEqual(pulled["result"], "PULLED")
        self.assertEqual(pulled["lossy_files"], [self.withheld])
        self.entry = self.manifest()["files"][self.withheld]
        self.assertIsNone(self.entry["mirror"])
        self.assertIs(self.entry["lossy"], True)
        self.assertFalse(self.mine.exists())
        self.enterContext(patch.object(mirror.desktop, "find_instance", return_value=None))
        # The tiny fixture is intentionally not a full Desktop report.
        self.enterContext(patch.object(mirror.validate, "run_stage", return_value=[]))

    def manifest(self):
        return mirror.load_manifest(self.ws, self.project)

    def pending(self):
        return mirror.mirror_changes(self.ws, self.project, self.manifest())

    def test_clean_pull_status_is_clean_and_push_is_noop(self):
        self.assertEqual(self.pending(), {
            "changed": [], "added": [], "deleted": []})
        before = mirror.manifest_path(self.ws, self.project)
        manifest_bytes = Path(before).read_bytes()
        out = mirror.push(self.ws, self.project)
        self.assertEqual(out["result"], "NOTHING_TO_PUSH", out)
        self.assertEqual(Path(before).read_bytes(), manifest_bytes)
        self.assertEqual(self.real.read_bytes(), self.original)

    def test_unrelated_unfiltered_push_preserves_withheld(self):
        edited = b'{"layoutOptimization": 1}\n'
        Path(self.ws.mirror, self.report).write_bytes(edited)
        self.assertEqual(self.pending()["changed"], [self.report])
        out = mirror.push(self.ws, self.project)
        self.assertEqual(out["result"], "PUSHED", out)
        self.assertNotIn(self.withheld, out.get("files", []))
        self.assertNotIn(self.withheld, out.get("deleted", []))
        self.assertNotIn(self.withheld, out.get("held_back", []))
        self.assertEqual(Path(self.ws.root, self.report).read_bytes(), edited)
        self.assertEqual(self.real.read_bytes(), self.original)
        self.assertEqual(self.manifest()["files"][self.withheld], self.entry)
        self.assertFalse(self.mine.exists())
        self.assertEqual(self.pending()["deleted"], [])

    def test_recreated_withheld_is_changed_and_refused(self):
        edited = b"table T\n\tmeasure M = 2\n"
        self.mine.parent.mkdir(parents=True, exist_ok=True)
        self.mine.write_bytes(edited)
        self.assertEqual(self.pending(), {
            "changed": [self.withheld], "added": [], "deleted": []})
        out = mirror.push(self.ws, self.project)
        self.assertEqual(out["result"], "CONFLICT", out)
        self.assertEqual(out["files"], [
            self.withheld + " (not round-trippable, edit in Desktop)"])
        self.assertEqual(self.real.read_bytes(), self.original)
        self.assertEqual(self.mine.read_bytes(), edited)
        self.assertEqual(self.manifest()["files"][self.withheld], self.entry)

    def test_normal_deletion_and_nonwithheld_null_are_selected(self):
        Path(self.ws.mirror, self.report).unlink()
        self.assertEqual(self.pending()["deleted"], [self.report])
        # Filter out the withheld path so this guard passes even on the base.
        for null_mirror in (False, True):
            with self.subTest(null_mirror=null_mirror):
                if null_mirror:
                    man = self.manifest()
                    man["files"][self.report].update(mirror=None, lossy=False)
                    mirror.save_manifest(self.ws, self.project, man)
                out = mirror.push(self.ws, self.project, only=[self.report], dry_run=True)
                self.assertEqual(out["result"], "DRY_RUN_OK", out)
                self.assertIn(self.report, out["objects"])
                self.assertIn(self.report, self.pending()["deleted"])

    def test_upstream_only_change_pulls_and_refreshes_withheld_hash(self):
        upstream = self.original.replace(b"measure M = 1", b"measure M = 3")
        self.real.write_bytes(upstream)
        out = mirror.pull(self.ws, self.project)
        self.assertEqual(out["result"], "PULLED", out)
        entry = self.manifest()["files"][self.withheld]
        self.assertNotEqual(entry["real"], self.entry["real"])
        self.assertEqual(entry["real"], hashlib.sha256(upstream).hexdigest())
        self.assertIsNone(entry["mirror"])
        self.assertIs(entry["lossy"], True)
        self.assertFalse(self.mine.exists())
        self.assertEqual(self.real.read_bytes(), upstream)

    def test_upstream_drift_and_recreated_withheld_preserve_both_guards(self):
        upstream = self.original.replace(b"measure M = 1", b"measure M = 3")
        self.real.write_bytes(upstream)
        self.mine.parent.mkdir(parents=True, exist_ok=True)
        self.mine.write_bytes(b"table T\n\tmeasure M = 2\n")
        out = mirror.push(self.ws, self.project)
        self.assertEqual(out["result"], "CONFLICT", out)
        self.assertEqual(out["files"], [self.withheld,
            self.withheld + " (not round-trippable, edit in Desktop)"])
        self.assertEqual(self.real.read_bytes(), upstream)
        self.assertEqual(self.manifest()["files"][self.withheld], self.entry)


if __name__ == "__main__":
    unittest.main()
