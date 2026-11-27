"""Rebase preserves intentional mirror deletions without authorizing upstream drift."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pbz import mirror
from pbz.common import PbzError, file_sha
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



class RebasePresenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = make_workspace(tmp.name)
        self.project = make_project(self.ws)
        self.report = "Canary.Report/definition/report.json"
        self.table = "Canary.SemanticModel/definition/tables/T.tmdl"
        self.version = "Canary.Report/definition/version.json"
        self.assertEqual(mirror.pull(self.ws, self.project)["result"], "PULLED")
        self.enterContext(patch.object(mirror.desktop, "find_instance", return_value=None))
        # The tiny fixture is intentionally not a full Desktop report.
        self.enterContext(patch.object(mirror.validate, "run_stage", return_value=[]))

    def manifest(self):
        return mirror.load_manifest(self.ws, self.project)

    def paths(self, rel):
        return (Path(self.ws.root, rel), Path(self.ws.mirror, rel),
                Path(mirror.base_dir(self.ws, self.project), rel))

    def _fresh_push(self, rel):
        tests_dir = os.path.dirname(os.path.abspath(__file__))
        program = f"""
import json
import sys
from unittest.mock import patch

sys.path.insert(0, {SKILL!r})
sys.path.insert(0, {tests_dir!r})
from pbz import mirror
from pbz.common import Workspace

ws = Workspace({self.ws.root!r})
ws.private = {self.ws.private!r}
proj = ws.project()
with patch.object(mirror.desktop, "find_instance", return_value=None), patch.object(
    mirror.validate, "run_stage", return_value=[]
):
    out = mirror.push(ws, proj, only=[{rel!r}])
print(json.dumps({{"result": out["result"], "files": out.get("files", [])}}))
"""
        env = dict(os.environ)
        env.setdefault("PBZ_FAKE_DPAPI", "1")
        env.setdefault("PBZ_TEST_HARNESS", "1")
        completed = subprocess.run([sys.executable, "-c", program], capture_output=True,
                                   text=True, env=env, timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout.splitlines()[-1])

    def _state(self, rel):
        real, _, base = self.paths(rel)
        entry = json.loads(json.dumps(self.manifest()["files"].get(rel)))
        return (real.read_bytes() if real.exists() else None, entry,
                base.read_bytes() if base.exists() else None)

    def _assert_fresh_push_conflict(self, rel):
        before = self._state(rel)
        pushed = self._fresh_push(rel)
        self.assertEqual(pushed["result"], "CONFLICT", pushed)
        self.assertEqual(self._state(rel), before)

    def _assert_resolution_push(self, rel, edited):
        real, mine, base = self.paths(rel)
        mine.write_bytes(edited)
        pushed = mirror.push(self.ws, self.project, only=[rel])
        self.assertEqual(pushed["result"], "PUSHED", pushed)
        self.assertEqual(real.read_bytes(), edited)
        self.assertEqual(base.read_bytes(), edited)
        self.assertEqual(self.manifest()["files"][rel],
                         {"real": file_sha(str(real)), "mirror": file_sha(str(mine))})

    def test_add_add_with_different_content_conflicts(self):
        real, mine, base = self.paths(self.version)
        edited = b'{"version": "mine"}\n'
        mine.write_bytes(edited)
        real.write_bytes(b'{"version": "theirs"}\n')

        out = mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(out["conflicts"], [self.version])
        self.assertNotIn(self.version, self.manifest()["files"])
        self.assertEqual(mine.read_bytes(), edited)
        self.assertFalse(base.exists())
        self._assert_fresh_push_conflict(self.version)

        mine.unlink()
        self.assertEqual(mirror.pull(self.ws, self.project)["result"], "PULLED")
        self._assert_resolution_push(self.version, b'{"version": "edited"}\n')

    def test_add_add_with_identical_content_adopts(self):
        real, mine, base = self.paths(self.version)
        upstream = b'{"version": "theirs"}\n'
        mine.write_bytes(upstream)
        real.write_bytes(upstream)

        out = mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(out["conflicts"], [])
        self.assertEqual(base.read_bytes(), upstream)
        man = self.manifest()
        self.assertEqual(man["files"][self.version],
                         {"real": file_sha(str(real)), "mirror": file_sha(str(mine))})
        for kind, paths in mirror.mirror_changes(self.ws, self.project, man).items():
            with self.subTest(kind=kind):
                self.assertNotIn(self.version, paths)

        before = self._state(self.version)
        pushed = mirror.push(self.ws, self.project, only=[self.version])
        self.assertEqual(pushed["result"], "NOTHING_TO_PUSH", pushed)
        self.assertEqual(self._state(self.version), before)

    def test_add_add_with_lossy_upstream_conflicts_without_entry(self):
        real, mine, _ = self.paths(self.version)
        mine.write_bytes(b'{"note": "agent version"}\n')
        real.write_bytes('{"note": "«unmapped#1»"}\n'.encode("utf-8"))

        out = mirror.pull(self.ws, self.project, rebase=True)
        self.assertIn(self.version, out["conflicts"])
        self.assertIn(self.version, out["lossy_files"])
        self.assertNotIn(self.version, self.manifest()["files"])
        self._assert_fresh_push_conflict(self.version)

    def test_edit_delete_conflicts_and_stays_blocked(self):
        real, mine, base = self.paths(self.table)
        mine.write_bytes(b"table T\n\tmeasure M = 2\n")
        real.unlink()
        entry = self.manifest()["files"][self.table]
        before_base = base.read_bytes()

        out = mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(out["conflicts"], [self.table])
        self.assertEqual(self.manifest()["files"][self.table], entry)
        self.assertEqual(base.read_bytes(), before_base)
        self._assert_fresh_push_conflict(self.table)

        mine.unlink()
        self.assertEqual(mirror.pull(self.ws, self.project, rebase=True)["result"], "PULLED")
        self.assertNotIn(self.table, self.manifest()["files"])
        self.assertFalse(base.exists())
        self._assert_resolution_push(self.table, b"table T\n\tmeasure M = 5\n")

    def test_both_deleted_removes_base_copy(self):
        real, mine, base = self.paths(self.table)
        mine.unlink()
        real.unlink()

        out = mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(out["conflicts"], [])
        self.assertNotIn(self.table, self.manifest()["files"])
        self.assertFalse(base.exists())

    def test_missing_base_copy_conflicts_instead_of_substituting(self):
        real, mine, base = self.paths(self.table)
        base.unlink()
        edited = b"table T\n\tmeasure M = 2\n"
        mine.write_bytes(edited)
        real.write_bytes(b"table T\n\tmeasure M = 1\n\tmeasure N = 3\n")
        entry = self.manifest()["files"][self.table]

        out = mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(out["conflicts"], [self.table])
        self.assertEqual(self.manifest()["files"][self.table], entry)
        self.assertEqual(mine.read_bytes(), edited)
        self._assert_fresh_push_conflict(self.table)

        self.assertEqual(mirror.pull(self.ws, self.project, force=True)["result"], "PULLED")
        self._assert_resolution_push(self.table, edited)

    def test_merge3_without_ancestor_refuses_prepared(self):
        _, mine, base = self.paths(self.version)
        edited = b'{"version": "mine"}\n'
        mine.write_bytes(edited)
        self.assertFalse(base.exists())

        with self.assertRaises(PbzError) as caught:
            mirror._merge3(self.ws, self.project, self.version,
                           '{"version": "theirs"}\n', '{"version": "mine"}\n', b"", None)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(mine.read_bytes(), edited)

    def test_merge3_without_ancestor_refuses_direct(self):
        _, mine, base = self.paths(self.version)
        edited = b'{"version": "mine"}\n'
        mine.write_bytes(edited)
        self.assertFalse(base.exists())

        with self.assertRaises(PbzError) as caught:
            mirror._merge3(self.ws, self.project, self.version, '{"version": "theirs"}\n')
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(mine.read_bytes(), edited)

    def test_merge3_without_ancestor_refuses_prepared_with_disk_base(self):
        _, mine, base = self.paths(self.table)
        self.assertTrue(base.exists())
        before_base = base.read_bytes()
        edited = "table T\n\tmeasure M = 2\n"
        mine.write_bytes(edited.encode("utf-8"))

        with patch.object(mirror, "read_text_checked") as read_text_checked:
            with self.assertRaises(PbzError) as caught:
                mirror._merge3(self.ws, self.project, self.table,
                               "table T\n\tmeasure M = 3\n", edited, b"", None)
        self.assertEqual(caught.exception.code, "REFUSED")
        read_text_checked.assert_not_called()
        self.assertEqual(mine.read_bytes(), edited.encode("utf-8"))
        self.assertEqual(base.read_bytes(), before_base)

    def test_merge3_aliased_mirror_with_valid_base_refuses(self):
        _, mine, base = self.paths(self.version)
        edited = b'{"version": "mine"}\n'
        mine.write_bytes(edited)
        os.link(mine, mine.with_name("version-alias.json"))
        before_base = b'{"version": "base"}\n'
        base.write_bytes(before_base)

        self.assertTrue(base.exists())
        with self.assertRaises(PbzError) as caught:
            mirror._merge3(self.ws, self.project, self.version, '{"version": "theirs"}\n')
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(mine.read_bytes(), edited)
        self.assertEqual(base.read_bytes(), before_base)

    def _lossy_setup(self):
        real, mine, base = self.paths(self.table)
        real.write_text("table T\n\tmeasure M = 1\n\tlineageTag: «unmapped#1»\n", encoding="utf-8")
        out = mirror.pull(self.ws, self.project)
        self.assertEqual(out["lossy_files"], [self.table])
        entry = self.manifest()["files"][self.table]
        self.assertEqual(entry, {"real": file_sha(str(real)), "mirror": None, "lossy": True})
        self.assertIs(entry["lossy"], True)
        self.assertFalse(mine.exists())
        before_base = base.read_bytes()
        mine.write_bytes(b"table T\n\tmeasure M = 2\n")
        self.assertIn(self.table, mirror.mirror_changes(self.ws, self.project, self.manifest())["changed"])
        return entry, before_base

    def test_lossy_entry_with_recreated_mirror_file_conflicts(self):
        entry, before_base = self._lossy_setup()
        real, mine, base = self.paths(self.table)
        before_mine = mine.read_bytes()
        real.write_bytes(b"table T\n\tmeasure M = 3\n")
        self.assertNotEqual(file_sha(str(real)), entry["real"])

        out = mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(out["conflicts"], [self.table])
        self.assertNotIn(self.table, out["lossy_files"])
        self.assertEqual(self.manifest()["files"][self.table], entry)
        self.assertEqual(mine.read_bytes(), before_mine)
        self.assertEqual(base.read_bytes(), before_base)
        self._assert_fresh_push_conflict(self.table)

    def test_lossy_entry_with_recreated_mirror_file_conflicts_upstream_deleted(self):
        entry, _ = self._lossy_setup()
        real, mine, _ = self.paths(self.table)
        before_mine = mine.read_bytes()
        real.unlink()

        out = mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(out["conflicts"], [self.table])
        self.assertEqual(self.manifest()["files"][self.table], entry)
        self.assertEqual(mine.read_bytes(), before_mine)
        self._assert_fresh_push_conflict(self.table)

    def test_lossy_entry_with_recreated_mirror_file_conflicts_still_lossy(self):
        _, before_base = self._lossy_setup()
        real, mine, base = self.paths(self.table)
        before_mine = mine.read_bytes()
        real.write_text("table T\n\tmeasure M = 3\n\tlineageTag: «unmapped#1»\n", encoding="utf-8")

        out = mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(out["conflicts"], [self.table])
        self.assertEqual(self.manifest()["files"][self.table],
                         {"real": file_sha(str(real)), "mirror": None, "lossy": True})
        self.assertEqual(mine.read_bytes(), before_mine)
        self.assertEqual(base.read_bytes(), before_base)
        self._assert_fresh_push_conflict(self.table)

    def test_upstream_only_edit_rebases_cleanly(self):
        real, mine, _ = self.paths(self.table)
        upstream = b"table T\n\tmeasure M = 3\n"
        real.write_bytes(upstream)

        out = mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(out["conflicts"], [])
        self.assertEqual(mine.read_bytes(), upstream)
        self.assertEqual(self.manifest()["files"][self.table]["real"], file_sha(str(real)))

    def _phantom_setup(self):
        real, _, base = self.paths(self.table)
        real.unlink()
        self.assertEqual(mirror.pull(self.ws, self.project)["result"], "PULLED")
        self.assertNotIn(self.table, self.manifest()["files"])
        self.assertTrue(base.exists())
        return base.read_bytes()

    def test_add_add_with_surviving_phantom_base_conflicts(self):
        phantom = self._phantom_setup()
        real, mine, base = self.paths(self.table)
        edited = b"table T\n\tmeasure M = 2\n"
        mine.write_bytes(edited)
        real.write_bytes(b"table T\n\tmeasure M = 3\n")

        out = mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(out["conflicts"], [self.table])
        self.assertNotIn(self.table, self.manifest()["files"])
        self.assertEqual(base.read_bytes(), phantom)
        self.assertEqual(mine.read_bytes(), edited)
        self._assert_fresh_push_conflict(self.table)

    def test_add_add_with_surviving_phantom_base_adopts(self):
        phantom = self._phantom_setup()
        real, mine, base = self.paths(self.table)
        upstream = b"table T\n\tmeasure M = 2\n"
        self.assertNotEqual(upstream, phantom)
        mine.write_bytes(upstream)
        real.write_bytes(upstream)

        out = mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(out["conflicts"], [])
        self.assertEqual(self.manifest()["files"][self.table],
                         {"real": file_sha(str(real)), "mirror": file_sha(str(mine))})
        self.assertEqual(base.read_bytes(), upstream)

    def test_delete_edit_fresh_push_leaves_baseline_intact(self):
        real, mine, _ = self.paths(self.report)
        mine.unlink()
        real.write_bytes(b'{"layoutOptimization": 1}\n')

        out = mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(out["conflicts"], [self.report])
        self._assert_fresh_push_conflict(self.report)

if __name__ == "__main__":
    unittest.main()
