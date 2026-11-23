"""Rebase preserves intentional mirror deletions without authorizing upstream drift."""
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
from pbz.common import PbzError
from mirror_helpers import make_workspace, make_project


class RebaseDeletesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = make_workspace(tmp.name)
        self.project = make_project(self.ws)
        self.report = "Canary.Report/definition/report.json"
        self.table = "Canary.SemanticModel/definition/tables/T.tmdl"
        self.assertEqual(mirror.pull(self.ws, self.project)["result"], "PULLED")
        self.enterContext(patch.object(mirror.desktop, "find_instance", return_value=None))
        # The tiny fixture is intentionally not a full Desktop report.
        self.enterContext(patch.object(mirror.validate, "run_stage", return_value=[]))

    def manifest(self):
        return mirror.load_manifest(self.ws, self.project)

    def paths(self, rel):
        return (Path(self.ws.root, rel), Path(self.ws.mirror, rel),
                Path(mirror.base_dir(self.ws, self.project), rel))

    def test_unchanged_upstream_keeps_deletion(self):
        entries = {}
        for rel in (self.report, self.table):
            with self.subTest(rel=rel):
                real, mine, base = self.paths(rel)
                entries[rel] = self.manifest()["files"][rel]
                before_real, before_base = real.read_bytes(), base.read_bytes()
                mine.unlink()
                out = mirror.pull(self.ws, self.project, rebase=True)
                self.assertEqual(out["result"], "PULLED", out)
                self.assertEqual(out["conflicts"], [])
                self.assertFalse(mine.exists(), "rebase republished the deleted mirror file")
                self.assertEqual(self.manifest()["files"].get(rel), entries[rel])
                self.assertIn(rel, mirror.mirror_changes(self.ws, self.project, self.manifest())["deleted"])
                self.assertEqual(real.read_bytes(), before_real)
                self.assertEqual(base.read_bytes(), before_base)
                for kept, entry in entries.items():
                    self.assertFalse(self.paths(kept)[1].exists())
                    self.assertEqual(self.manifest()["files"].get(kept), entry)
                    self.assertIn(kept, mirror.mirror_changes(self.ws, self.project, self.manifest())["deleted"])
                with self.assertRaises(PbzError) as caught:
                    mirror.pull(self.ws, self.project)
                self.assertEqual(caught.exception.code, "UNPUSHED_EDITS")
        for rel in (self.report, self.table):
            with self.subTest(push=rel):
                pushed = mirror.push(self.ws, self.project, only=[rel])
                self.assertEqual(pushed["result"], "PUSHED", pushed)
                self.assertFalse(self.paths(rel)[0].exists())
                self.assertFalse(self.paths(rel)[1].exists())
                self.assertNotIn(rel, self.manifest()["files"])
                self.assertNotIn(rel, mirror.mirror_changes(self.ws, self.project, self.manifest())["deleted"])

    def test_both_deleted_rebase_resolves_cleanly(self):
        for rel in (self.report, self.table):
            with self.subTest(rel=rel):
                real, mine, _ = self.paths(rel)
                self.assertIn(rel, self.manifest()["files"])
                mine.unlink()
                real.unlink()
                out = mirror.pull(self.ws, self.project, rebase=True)
                self.assertEqual(out["result"], "PULLED", out)
                self.assertEqual(out["conflicts"], [])
                self.assertFalse(mine.exists())
                self.assertFalse(real.exists())
                self.assertNotIn(rel, self.manifest()["files"])
                self.assertNotIn(rel, mirror.mirror_changes(self.ws, self.project, self.manifest())["deleted"])

    def test_changed_upstream_conflicts_without_advancing(self):
        for rel, upstream in ((self.report, b'{"layoutOptimization": 1}\n'),
                              (self.table, b"table T\n\tmeasure M = 3\n")):
            with self.subTest(rel=rel):
                real, mine, base = self.paths(rel)
                entry = self.manifest()["files"][rel]
                before_base = base.read_bytes()
                mine.unlink()
                real.write_bytes(upstream)
                for attempt in range(2):
                    with self.subTest(attempt=attempt):
                        with patch.object(mirror, "_read_mine", wraps=mirror._read_mine) as read_mine:
                            out = mirror.pull(self.ws, self.project, rebase=True)
                        self.assertEqual(out["result"], "PULLED", out)
                        self.assertFalse(mine.exists(), "rebase republished the deleted mirror file")
                        self.assertIn(rel, out["conflicts"])
                        read_mine.assert_not_called()
                        self.assertEqual(self.manifest()["files"][rel], entry)
                        self.assertEqual(base.read_bytes(), before_base)
                        pushed = mirror.push(self.ws, self.project, only=[rel])
                        self.assertEqual(pushed["result"], "CONFLICT", pushed)
                        self.assertEqual(pushed["files"], [rel])
                        self.assertEqual(real.read_bytes(), upstream)
                # Resolve this subtest so the next path is tested independently.
                mirror.pull(self.ws, self.project, force=True)

    def test_push_dry_run_before_rebase_summarizes_deletion(self):
        real, mine, base = self.paths(self.table)
        before_real, before_base = real.read_bytes(), base.read_bytes()
        before_manifest = self.manifest()
        mine.unlink()
        out = mirror.push(self.ws, self.project, dry_run=True)
        self.assertEqual(out["result"], "DRY_RUN_OK", out)
        self.assertIn("- table T", out["objects"])
        self.assertIn("- measure M", out["objects"])
        self.assertFalse(mine.exists())
        self.assertEqual(real.read_bytes(), before_real)
        self.assertEqual(base.read_bytes(), before_base)
        self.assertEqual(self.manifest(), before_manifest)

    def test_changed_mirror_with_unchanged_upstream_is_kept(self):
        real, mine, base = self.paths(self.table)
        before_real, before_base = real.read_bytes(), base.read_bytes()
        entry = self.manifest()["files"][self.table]
        edited = b"table T\n\tmeasure M = 2\n"
        mine.write_bytes(edited)
        out = mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(out["conflicts"], [])
        self.assertEqual(mine.read_bytes(), edited)
        self.assertEqual(real.read_bytes(), before_real)
        self.assertEqual(base.read_bytes(), before_base)
        self.assertEqual(self.manifest()["files"][self.table], entry)

    def test_added_mirror_is_kept(self):
        rel = "Canary.Report/definition/extra.json"
        real, mine, base = self.paths(rel)
        edited = b"{}\n"
        mine.write_bytes(edited)
        out = mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(out["conflicts"], [])
        self.assertEqual(mine.read_bytes(), edited)
        self.assertFalse(real.exists())
        self.assertFalse(base.exists())
        self.assertNotIn(rel, self.manifest()["files"])
        self.assertEqual(mirror.mirror_changes(self.ws, self.project, self.manifest())["added"], [rel])

    def test_changed_upstream_and_mirror_text_still_merge(self):
        real, mine, base = self.paths(self.table)
        original = b"table T\n\tmeasure M = 1\n\n\tmeasure N = 1\n"
        real.write_bytes(original)
        mirror.pull(self.ws, self.project)
        mine.write_bytes(original.replace(b"measure M = 1", b"measure M = 2"))
        upstream = original.replace(b"measure N = 1", b"measure N = 3")
        real.write_bytes(upstream)
        out = mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(out["conflicts"], [])
        self.assertEqual(mine.read_bytes(), b"table T\n\tmeasure M = 2\n\n\tmeasure N = 3\n")
        self.assertEqual(base.read_bytes(), upstream)
        self.assertEqual(self.manifest()["files"][self.table]["real"], hashlib.sha256(upstream).hexdigest())

    def test_plain_pull_refuses_and_forced_pull_republishes(self):
        real, mine, base = self.paths(self.report)
        before_manifest = self.manifest()
        mine.unlink()
        with self.assertRaises(PbzError) as caught:
            mirror.pull(self.ws, self.project)
        self.assertEqual(caught.exception.code, "UNPUSHED_EDITS")
        self.assertFalse(mine.exists())
        self.assertEqual(self.manifest(), before_manifest)
        out = mirror.pull(self.ws, self.project, force=True)
        self.assertEqual(out["result"], "PULLED", out)
        self.assertEqual(out["conflicts"], [])
        self.assertEqual(mine.read_bytes(), real.read_bytes())
        self.assertEqual(base.read_bytes(), real.read_bytes())
        self.assertIn(self.report, self.manifest()["files"])

    def test_withheld_absence_is_not_a_deletion_on_rebase(self):
        real, mine, base = self.paths(self.table)
        real.write_text("table T\n\tmeasure M = 1\n\tlineageTag: «unmapped#1»\n", encoding="utf-8")
        out = mirror.pull(self.ws, self.project)
        self.assertEqual(out["lossy_files"], [self.table])
        entry = self.manifest()["files"][self.table]
        before_base = base.read_bytes()
        self.assertIsNone(entry["mirror"])
        self.assertIs(entry["lossy"], True)
        self.assertFalse(mine.exists())
        self.assertEqual(mirror.mirror_changes(self.ws, self.project, self.manifest())["deleted"], [])
        upstream = real.read_bytes().replace(b"measure M = 1", b"measure M = 3")
        real.write_bytes(upstream)
        out = mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(out["result"], "PULLED", out)
        self.assertEqual(out["conflicts"], [])
        self.assertEqual(out["lossy_files"], [self.table])
        updated = self.manifest()["files"][self.table]
        self.assertNotEqual(updated["real"], entry["real"])
        self.assertEqual(updated["real"], hashlib.sha256(upstream).hexdigest())
        self.assertIsNone(updated["mirror"])
        self.assertIs(updated["lossy"], True)
        self.assertFalse(mine.exists())
        self.assertEqual(base.read_bytes(), before_base)
        self.assertEqual(mirror.mirror_changes(self.ws, self.project, self.manifest())["deleted"], [])


if __name__ == "__main__":
    unittest.main()
