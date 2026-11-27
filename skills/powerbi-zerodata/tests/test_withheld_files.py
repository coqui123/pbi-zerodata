"""Checked withheld absence is clean for status, pull, and push."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pbz import changes, mirror
from pbz.common import PbzError
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


class WithholdUnknownTests(unittest.TestCase):
    STR_A = "SYNTH-ROW-ALPHA-7Q2X-4815"
    STR_B = "SYNTH-ROW-BRAVO-3K9P-2260"
    STR_D = "SYNTH-ROW-DELTA-5M8R-9034"
    NUM = "424242.42"
    PATH_A = "Canary.Report/definition/customPayload.json"
    PATH_B = "Canary.Report/StaticResources/CustomResources/data.json"
    PATH_D = "Canary.SemanticModel/definition/extraData.json"
    THEME = "Canary.Report/StaticResources/RegisteredResources/CorpTheme.json"
    REPORT = "Canary.Report/definition/report.json"
    TABLE = "Canary.SemanticModel/definition/tables/T.tmdl"
    CASE_TABLE = "Canary.SemanticModel/Definition/tables/Case.tmdl"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = make_workspace(tmp.name)
        self.project = make_project(self.ws)
        self.root = Path(self.ws.root)
        self.manifest_path = Path(mirror.manifest_path(self.ws, self.project))
        self.enterContext(patch.object(mirror.desktop, "find_instance", return_value=None))
        self.enterContext(patch.object(mirror.validate, "run_stage", return_value=[]))

    def _plant(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        path.write_bytes(data)
        return data

    def _mirror_bytes(self, rel):
        return Path(self.ws.mirror, rel).read_bytes()

    def _scan_mirror_for(self, markers):
        for directory, _, files in os.walk(self.ws.mirror):
            for name in files:
                text = Path(directory, name).read_bytes().decode("utf-8", errors="replace")
                if any(marker in text for marker in markers):
                    return True
        return False

    def _manifest(self):
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def _rows(self, marker):
        return f'{{"rows": [{{"v": "{marker}", "n": {self.NUM}}}]}}'

    def _assert_withheld(self, rel, original):
        self.assertEqual(self._manifest()["files"][rel], {
            "real": hashlib.sha256(original).hexdigest(), "mirror": None, "lossy": True})

    def _seed_published(self, rel, text):
        self.assertEqual(mirror.pull(self.ws, self.project)["result"], "PULLED")
        original = self._plant(rel, text)
        for directory in (self.ws.mirror, mirror.base_dir(self.ws, self.project)):
            path = Path(directory, rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(original)
        digest = hashlib.sha256(original).hexdigest()
        manifest = self._manifest()
        manifest["files"][rel] = {"real": digest, "mirror": digest}
        self.manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        return original

    def test_unsupported_files_are_withheld_on_pull(self):
        planted = {
            self.PATH_A: self._plant(self.PATH_A, self._rows(self.STR_A)),
            self.PATH_B: self._plant(self.PATH_B, self._rows(self.STR_B)),
            self.PATH_D: self._plant(self.PATH_D, self._rows(self.STR_D)),
        }
        result = mirror.pull(self.ws, self.project)
        self.assertEqual(result["result"], "PULLED")
        self.assertEqual(sorted(result["lossy_files"]), sorted(planted))
        self.assertNotIn("warnings", result)
        for rel, original in planted.items():
            self.assertFalse(os.path.exists(Path(self.ws.mirror, rel)))
            self._assert_withheld(rel, original)
            self.assertEqual((self.root / rel).read_bytes(), original)
        markers = (self.STR_A, self.STR_B, self.STR_D, self.NUM)
        self.assertFalse(self._scan_mirror_for(markers))
        rendered = json.dumps(result)
        for marker in markers:
            self.assertNotIn(marker, rendered)
        self.assertEqual(mirror.mirror_changes(self.ws, self.project, self._manifest()), {
            "changed": [], "added": [], "deleted": []})
        self.assertEqual(mirror.diff(self.ws, self.project, patch=True)["patch"], "")

    def test_supported_control_publishes(self):
        theme = '{"name": "CorpTheme", "dataColors": ["#118DFF"]}'
        self._plant(self.THEME, theme)
        result = mirror.pull(self.ws, self.project)
        self.assertEqual(result["result"], "PULLED")
        self.assertEqual(result["lossy_files"], [])
        manifest = self._manifest()
        files = manifest["files"]
        for rel in (self.THEME, self.REPORT, self.TABLE, "Canary.Report/definition.pbir",
                    "Canary.SemanticModel/definition.pbism"):
            self.assertTrue(os.path.exists(Path(self.ws.mirror, rel)))
            self.assertFalse(files[rel].get("lossy", False))
        self.assertEqual(mirror.mirror_changes(self.ws, self.project, manifest), {
            "changed": [], "added": [], "deleted": []})
        self.assertEqual(json.loads(Path(self.ws.mirror, self.THEME).read_text(encoding="utf-8")),
                         json.loads(theme))

    def test_push_preserves_withheld_originals(self):
        original = self._plant(self.PATH_A, self._rows(self.STR_A))
        mirror.pull(self.ws, self.project)
        self._assert_withheld(self.PATH_A, original)
        self.assertEqual(mirror.push(self.ws, self.project)["result"], "NOTHING_TO_PUSH")
        edited = b'{"layoutOptimization": 1}\n'
        Path(self.ws.mirror, self.REPORT).write_bytes(edited)
        self.assertEqual(mirror.push(self.ws, self.project)["result"], "PUSHED")
        self.assertEqual((self.root / self.REPORT).read_bytes(), edited)
        self.assertEqual((self.root / self.PATH_A).read_bytes(), original)
        self._assert_withheld(self.PATH_A, original)

    def test_stale_copy_removed_on_plain_repull(self):
        original = self._seed_published(self.PATH_A, self._rows(self.STR_A))
        mirror.snapshot(self.ws, self.project, "seed")
        seed_stamp = next(entry["stamp"] for entry in mirror.history_list(self.ws, self.project)
                          if entry["event"] == "seed")
        result = mirror.pull(self.ws, self.project)
        self.assertEqual(result["result"], "PULLED")
        mine = Path(self.ws.mirror, self.PATH_A)
        self.assertFalse(os.path.exists(mine))
        self.assertFalse(os.path.exists(Path(mirror.base_dir(self.ws, self.project), self.PATH_A)))
        self._assert_withheld(self.PATH_A, original)
        self.assertNotIn("warnings", result)
        mine.write_bytes(b"{}\n")
        rendered = json.dumps(mirror.diff(self.ws, self.project, patch=True))
        self.assertNotIn(self.STR_A, rendered)
        self.assertNotIn(self.NUM, rendered)
        mirror.revert(self.ws, self.project)
        self.assertFalse(self._scan_mirror_for((self.STR_A, self.NUM)))
        rendered = json.dumps(changes.run(self.ws, self.project, from_=seed_stamp, to="mirror"))
        self.assertNotIn(self.STR_A, rendered)
        self.assertNotIn(self.NUM, rendered)
        latest_stamp = mirror.history_list(self.ws, self.project)[-1]["stamp"]
        self.assertNotIn(self.PATH_A, mirror.history_texts(self.ws, self.project, latest_stamp))

    def test_rebase_edit_and_delete_refuse_then_force_withholds(self):
        original = self._seed_published(self.PATH_A, self._rows(self.STR_A))
        edited = self._rows(self.STR_A + "-EDIT").encode("utf-8")
        mine = Path(self.ws.mirror, self.PATH_A)
        mine.write_bytes(edited)
        manifest = self.manifest_path.read_bytes()
        with self.assertRaises(PbzError) as raised:
            mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(raised.exception.code, "REFUSED")
        self.assertEqual(raised.exception.detail, self.PATH_A)
        self.assertEqual(self._mirror_bytes(self.PATH_A), edited)
        self.assertEqual(self.manifest_path.read_bytes(), manifest)
        self.assertEqual((self.root / self.PATH_A).read_bytes(), original)
        self.assertEqual(mirror.pull(self.ws, self.project, force=True)["result"], "PULLED")
        self.assertFalse(os.path.exists(mine))
        self._assert_withheld(self.PATH_A, original)
        self.assertEqual((self.root / self.PATH_A).read_bytes(), original)

    def test_rebase_delete_refuses_then_force_resolves(self):
        original = self._seed_published(self.PATH_A, self._rows(self.STR_A))
        Path(self.ws.mirror, self.PATH_A).unlink()
        manifest = self.manifest_path.read_bytes()
        with self.assertRaises(PbzError) as raised:
            mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(raised.exception.code, "REFUSED")
        self.assertEqual(raised.exception.detail, self.PATH_A)
        self.assertEqual(self.manifest_path.read_bytes(), manifest)
        self.assertEqual(mirror.pull(self.ws, self.project, force=True)["result"], "PULLED")
        self._assert_withheld(self.PATH_A, original)
        self.assertEqual(mirror.push(self.ws, self.project)["result"], "NOTHING_TO_PUSH")
        self.assertEqual((self.root / self.PATH_A).read_bytes(), original)

    def test_legacy_path_with_real_gone(self):
        original = self._seed_published(self.PATH_A, self._rows(self.STR_A))
        edited = self._rows(self.STR_A + "-EDIT").encode("utf-8")
        mine = Path(self.ws.mirror, self.PATH_A)
        base = Path(mirror.base_dir(self.ws, self.project), self.PATH_A)
        real = self.root / self.PATH_A
        mine.write_bytes(edited)
        real.unlink()
        manifest = self.manifest_path.read_bytes()
        with self.assertRaises(PbzError) as raised:
            mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(raised.exception.code, "REFUSED")
        self.assertEqual(raised.exception.detail, self.PATH_A)
        self.assertEqual(self._mirror_bytes(self.PATH_A), edited)
        self.assertEqual(base.read_bytes(), original)
        self.assertEqual(self.manifest_path.read_bytes(), manifest)
        self.assertEqual(mirror.pull(self.ws, self.project, force=True)["result"], "PULLED")
        self.assertFalse(os.path.exists(mine))
        self.assertFalse(os.path.exists(base))
        self.assertNotIn(self.PATH_A, self._manifest()["files"])
        self.assertFalse(real.exists())

    def test_stale_entry_dropped_when_real_gone(self):
        self._seed_published(self.PATH_A, self._rows(self.STR_A))
        (self.root / self.PATH_A).unlink()
        self.assertEqual(mirror.pull(self.ws, self.project)["result"], "PULLED")
        self.assertNotIn(self.PATH_A, self._manifest()["files"])
        mine = Path(self.ws.mirror, self.PATH_A)
        self.assertFalse(os.path.exists(mine))
        self.assertFalse(os.path.exists(Path(mirror.base_dir(self.ws, self.project), self.PATH_A)))
        mine.write_bytes(b"{}\n")
        rendered = json.dumps(mirror.diff(self.ws, self.project, patch=True))
        self.assertNotIn(self.STR_A, rendered)
        self.assertNotIn(self.NUM, rendered)

    def test_malformed_json_at_unsupported_path_is_withheld(self):
        original = self._plant(self.PATH_A, "{not json")
        result = mirror.pull(self.ws, self.project)
        self.assertEqual(result["result"], "PULLED")
        self.assertIn(self.PATH_A, result["lossy_files"])
        self.assertFalse(os.path.exists(Path(self.ws.mirror, self.PATH_A)))
        self._assert_withheld(self.PATH_A, original)

    def test_predicate_table(self):
        supported = mirror._publication_supported
        for rel in (
            "Canary.SemanticModel/.platform",
            "Canary.Report/.platform",
            "Canary.SemanticModel/definition.pbism",
            "Canary.Report/definition.pbir",
            self.TABLE,
            "Canary.SemanticModel/definition/relationships.tmdl",
            "Canary.SemanticModel/definition/roles/Admin.tmdl",
            "Canary.SemanticModel/definition/cultures/en-US.tmdl",
            self.REPORT,
            "Canary.Report/definition/version.json",
            "Canary.Report/definition/reportExtensions.json",
            "Canary.Report/definition/pages/pages.json",
            "Canary.Report/definition/pages/p1/page.json",
            "Canary.Report/definition/pages/p1/visuals/v1/visual.json",
            "Canary.Report/definition/pages/p1/visuals/v1/mobile.json",
            "Canary.Report/definition/bookmarks/bookmarks.json",
            "Canary.Report/definition/bookmarks/B1.bookmark.json",
            self.THEME,
            "Canary.Report/StaticResources/SharedResources/Theme.json",
            "Canary.Report/StaticResources/SharedResources/BaseThemes/CY26SU09.json",
        ):
            with self.subTest(rel=rel):
                self.assertTrue(supported(rel))
        for rel in (
            self.PATH_A, self.PATH_B, self.PATH_D, self.CASE_TABLE,
            "Canary.Report/definition/pages/p1/notes.json",
            "Canary.Report/definition/bookmarks/notes.json",
            "Canary.Report/StaticResources/RegisteredResources/sub/x.json",
            "Canary.Report/definition/tables/T.tmdl",
            "Canary.Report/nested/.platform",
            "Canary.Report/nested/definition.pbir",
            "Canary.SemanticModel/StaticResources/x.json",
        ):
            with self.subTest(rel=rel):
                self.assertFalse(supported(rel))

    def test_mixed_case_tmdl_withheld_on_disk(self):
        upper = self.root / "CaseProbeAA"
        lower = self.root / "caseprobeaa"
        upper.mkdir()
        try:
            lower.mkdir(exist_ok=True)
        except OSError:
            return
        if os.path.samefile(upper, lower):
            return
        self._plant(self.CASE_TABLE,
                    'model\n\tcultureInfo en-US\n\ntable Case\n\n\tmeasure C = "SYNTH-CASE-6T4W-7751"')
        mirror.pull(self.ws, self.project)
        self.assertFalse(os.path.exists(Path(self.ws.mirror, self.CASE_TABLE)))
        entry = self._manifest()["files"][self.CASE_TABLE]
        self.assertIs(entry["lossy"], True)
        self.assertIsNone(entry["mirror"])
        self.assertTrue(os.path.exists(Path(self.ws.mirror, self.TABLE)))

    def test_base_only_orphan_removed_on_pull(self):
        # Clean pull first, so a manifest exists with no entry for PATH_A.
        self.assertEqual(mirror.pull(self.ws, self.project)["result"], "PULLED")
        self.assertNotIn(self.PATH_A, self._manifest()["files"])
        orphan = self._rows(self.STR_A).encode("utf-8")
        base = Path(mirror.base_dir(self.ws, self.project), self.PATH_A)
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_bytes(orphan)
        # Plain pull sweeps the untracked base copy at the unsupported path.
        self.assertEqual(mirror.pull(self.ws, self.project)["result"], "PULLED")
        self.assertFalse(os.path.exists(base))
        mine = Path(self.ws.mirror, self.PATH_A)
        mine.parent.mkdir(parents=True, exist_ok=True)
        mine.write_bytes(b"{}\n")
        rendered = json.dumps(mirror.diff(self.ws, self.project, patch=True))
        self.assertNotIn(self.STR_A, rendered)
        self.assertNotIn(self.NUM, rendered)

    def test_base_only_orphan_removed_on_rebase_with_agent_file(self):
        self.assertEqual(mirror.pull(self.ws, self.project)["result"], "PULLED")
        self.assertNotIn(self.PATH_A, self._manifest()["files"])
        orphan = self._rows(self.STR_A).encode("utf-8")
        base = Path(mirror.base_dir(self.ws, self.project), self.PATH_A)
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_bytes(orphan)
        agent = b'{"agent": "SYNTH-AGENT-LINE-2W7Q"}\n'
        mine = Path(self.ws.mirror, self.PATH_A)
        mine.parent.mkdir(parents=True, exist_ok=True)
        mine.write_bytes(agent)
        result = mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(result["result"], "PULLED")
        self.assertEqual(mine.read_bytes(), agent)
        self.assertFalse(os.path.exists(base))
        self.assertNotIn(self.PATH_A, self._manifest()["files"])
        rendered = json.dumps(mirror.diff(self.ws, self.project, patch=True))
        self.assertIn("SYNTH-AGENT-LINE-2W7Q", rendered)
        self.assertNotIn(self.STR_A, rendered)
        self.assertNotIn(self.NUM, rendered)

    def test_upstream_drift_refreshes_withheld_entry(self):
        original = self._plant(self.PATH_A, self._rows(self.STR_A))
        mirror.pull(self.ws, self.project)
        self._assert_withheld(self.PATH_A, original)

        upstream = self._rows(self.STR_A + "-V2").encode("utf-8")
        (self.root / self.PATH_A).write_bytes(upstream)
        self.assertEqual(mirror.pull(self.ws, self.project)["result"], "PULLED")
        self._assert_withheld(self.PATH_A, upstream)

        third = self._rows(self.STR_A + "-V3").encode("utf-8")
        (self.root / self.PATH_A).write_bytes(third)
        self.assertEqual(mirror.pull(self.ws, self.project, rebase=True)["result"], "PULLED")
        self._assert_withheld(self.PATH_A, third)

    def test_orphan_sweep_refuses_when_base_tree_unreadable(self):
        self.assertEqual(mirror.pull(self.ws, self.project)["result"], "PULLED")
        orphan = self._rows(self.STR_A).encode("utf-8")
        base = Path(mirror.base_dir(self.ws, self.project), self.PATH_A)
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_bytes(orphan)
        agent = b'{"agent": "SYNTH-AGENT-LINE-2W7Q"}\n'
        mine = Path(self.ws.mirror, self.PATH_A)
        mine.parent.mkdir(parents=True, exist_ok=True)
        mine.write_bytes(agent)
        manifest_before = self.manifest_path.read_bytes()

        target = str(base.parent)
        real_scandir = os.scandir

        def failing_scandir(path):
            if os.fspath(path) == target:
                raise PermissionError("inert enumeration failure")
            return real_scandir(path)

        with patch.object(os, "scandir", failing_scandir):
            with self.assertRaises(PbzError) as raised:
                mirror.pull(self.ws, self.project, rebase=True)
            self.assertEqual(raised.exception.code, "REFUSED")
            self.assertEqual(raised.exception.detail, self.project.name)
            self.assertNotIn(self.STR_A, str(raised.exception))
            self.assertEqual(mine.read_bytes(), agent)
            self.assertEqual(base.read_bytes(), orphan)
            self.assertEqual(self.manifest_path.read_bytes(), manifest_before)

        self.assertEqual(mirror.pull(self.ws, self.project, rebase=True)["result"], "PULLED")
        self.assertFalse(base.exists())
        self.assertEqual(mine.read_bytes(), agent)
        rendered = json.dumps(mirror.diff(self.ws, self.project, patch=True))
        self.assertIn("SYNTH-AGENT-LINE-2W7Q", rendered)
        self.assertNotIn(self.STR_A, rendered)
        self.assertNotIn(self.NUM, rendered)


if __name__ == "__main__":
    unittest.main()
