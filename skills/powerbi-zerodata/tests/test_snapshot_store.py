"""Snapshot store namespace layout, migration, idempotency, shape verification, and fallback tests."""
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

from pbz import checks, evidence, tokens
from pbz.common import PbzError
from mirror_helpers import make_workspace


class SnapshotStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = make_workspace(tmp.name)
        self.snaps = Path(self.ws.private) / "snapshots"
        self.snaps.mkdir(parents=True, exist_ok=True)

    def test_old_layout_fixture_migration_and_sha256_identity(self):
        res_data = {"queries": {"q1": {"rows": ["abc123def456"]}}, "refreshed": "2026-01-01T00:00:00"}
        spec_data = {"queries": [{"id": "q1", "dax": "EVALUATE {1}"}]}

        res_bytes = json.dumps(res_data, indent=2).encode("utf-8")
        spec_bytes = json.dumps(spec_data, indent=2).encode("utf-8")

        res_file = self.snaps / "monthly.json"
        spec_file = self.snaps / "monthly.spec.json"

        res_file.write_bytes(res_bytes)
        spec_file.write_bytes(spec_bytes)

        res_sha = hashlib.sha256(res_bytes).hexdigest()
        spec_sha = hashlib.sha256(spec_bytes).hexdigest()

        res = checks.migrate_snapshot_layout(self.ws)
        self.assertTrue(res["migrated"])
        self.assertEqual(res["results"], ["monthly"])
        self.assertEqual(res["specs"], ["monthly"])

        self.assertFalse(res_file.exists())
        self.assertFalse(spec_file.exists())

        new_res = self.snaps / "results" / "monthly.json"
        new_spec = self.snaps / "specs" / "monthly.json"
        self.assertTrue(new_res.is_file())
        self.assertTrue(new_spec.is_file())

        self.assertEqual(hashlib.sha256(new_res.read_bytes()).hexdigest(), res_sha)
        self.assertEqual(hashlib.sha256(new_spec.read_bytes()).hexdigest(), spec_sha)

    def test_migration_results_shaped_monthly_spec_file(self):
        # A legacy file named monthly.spec.json that holds results (from baseline named monthly.spec)
        res_data = {"queries": {"q1": {"rows": ["111222"]}}, "refreshed": "2026-02-01T00:00:00"}
        legacy_file = self.snaps / "monthly.spec.json"
        legacy_file.write_text(json.dumps(res_data), encoding="utf-8")

        res = checks.migrate_snapshot_layout(self.ws)
        self.assertTrue(res["migrated"])
        self.assertEqual(res["results"], ["monthly.spec"])
        self.assertEqual(res["specs"], [])

        self.assertFalse(legacy_file.exists())
        migrated = self.snaps / "results" / "monthly.spec.json"
        self.assertTrue(migrated.is_file())
        loaded = json.loads(migrated.read_text("utf-8"))
        self.assertEqual(loaded["refreshed"], "2026-02-01T00:00:00")

    def test_migration_corrupt_legacy_file_fail_closed(self):
        # Corrupt / unrecognized shape legacy file
        corrupt_file = self.snaps / "bad.json"
        corrupt_file.write_text('{"random_key": 123}', encoding="utf-8")

        with self.assertRaises(PbzError) as ctx:
            checks.migrate_snapshot_layout(self.ws)
        self.assertEqual(ctx.exception.code, "CORRUPT_STATE")

        # Pass 1 aborted: corrupt file was not removed
        self.assertTrue(corrupt_file.exists())

    def test_migration_two_pass_late_conflict_prevents_partial_migration(self):
        # Setup: candidate a is valid, candidate b conflicts with existing results/b.json
        (self.snaps / "results").mkdir(parents=True, exist_ok=True)
        (self.snaps / "results" / "b.json").write_text('{"queries": {}, "refreshed": "new"}', encoding="utf-8")

        a_data = {"queries": {"q1": {}}, "refreshed": "a"}
        b_data = {"queries": {"q1": {}}, "refreshed": "old_conflicting"}
        (self.snaps / "a.json").write_text(json.dumps(a_data), encoding="utf-8")
        (self.snaps / "b.json").write_text(json.dumps(b_data), encoding="utf-8")

        with self.assertRaises(PbzError) as ctx:
            checks.migrate_snapshot_layout(self.ws)
        self.assertEqual(ctx.exception.code, "CONFLICT")

        # Zero partial mutation: a.json must NOT have been migrated or removed!
        self.assertTrue((self.snaps / "a.json").is_file())
        self.assertFalse((self.snaps / "results" / "a.json").exists())

    def test_migration_crash_recovery_identical_both_exist(self):
        # Both results/c.json and snapshots/c.json exist with identical content
        c_data = {"queries": {"q1": {}}, "refreshed": "identical"}
        c_text = json.dumps(c_data)
        (self.snaps / "results").mkdir(parents=True, exist_ok=True)
        (self.snaps / "results" / "c.json").write_text(c_text, encoding="utf-8")
        (self.snaps / "c.json").write_text(c_text, encoding="utf-8")

        res = checks.migrate_snapshot_layout(self.ws)
        self.assertTrue(res["migrated"])
        self.assertEqual(res["results"], ["c"])
        self.assertFalse((self.snaps / "c.json").exists())
        self.assertTrue((self.snaps / "results" / "c.json").is_file())

    def test_migration_idempotency(self):
        (self.snaps / "results").mkdir(parents=True, exist_ok=True)
        (self.snaps / "specs").mkdir(parents=True, exist_ok=True)
        (self.snaps / "results" / "foo.json").write_text('{"queries": {}, "refreshed": "1"}', encoding="utf-8")
        (self.snaps / "specs" / "foo.json").write_text('{"queries": []}', encoding="utf-8")

        res = checks.migrate_snapshot_layout(self.ws)
        self.assertFalse(res["migrated"])
        self.assertEqual(res["results"], [])
        self.assertEqual(res["specs"], [])

        self.assertTrue((self.snaps / "results" / "foo.json").is_file())
        self.assertTrue((self.snaps / "specs" / "foo.json").is_file())

    def test_migration_under_workspace_lock(self):
        lock_acquired = []

        orig_enter = tokens.WorkspaceLock.__enter__
        def instrumented_enter(lock_self):
            lock_acquired.append(lock_self.directory)
            return orig_enter(lock_self)

        with patch.object(tokens.WorkspaceLock, "__enter__", side_effect=instrumented_enter, autospec=True):
            checks.migrate_snapshot_layout(self.ws)

        self.assertEqual(lock_acquired, [self.ws.private])

    def test_migration_refuses_unsafe_link_paths_before_makedirs(self):
        # A reparse point on snapshots/results or snapshots/specs fails closed with UNSAFE_PATH
        with patch("pbz.checks._reparse_link", side_effect=lambda p: "results" in str(p)):
            (self.snaps / "results").mkdir(parents=True, exist_ok=True)
            with self.assertRaises(PbzError) as ctx:
                checks.migrate_snapshot_layout(self.ws)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")

    def test_snapshot_validates_arguments_before_migration(self):
        # Invalid action or invalid name fails before migration runs
        with patch("pbz.checks.migrate_snapshot_layout") as mock_mig:
            with self.assertRaises(PbzError) as ctx:
                checks.snapshot(self.ws, None, "bad_action", "monthly", None)
            self.assertEqual(ctx.exception.code, "BAD_REQUEST")

            with self.assertRaises(PbzError) as ctx:
                checks.snapshot(self.ws, None, "save", "../escape", None)
            self.assertEqual(ctx.exception.code, "BAD_REQUEST")

            mock_mig.assert_not_called()

    def test_namespace_non_collision_between_monthly_and_monthly_spec(self):
        spec_monthly = checks._snapshot_spec_path(self.ws, "monthly")
        spec_monthly_spec = checks._snapshot_spec_path(self.ws, "monthly.spec")

        self.assertNotEqual(spec_monthly, spec_monthly_spec)
        self.assertTrue(spec_monthly.endswith(os.path.join("snapshots", "specs", "monthly.json")))
        self.assertTrue(spec_monthly_spec.endswith(os.path.join("snapshots", "specs", "monthly.spec.json")))

    def test_saved_spec_legacy_read_only_fallback(self):
        legacy_path = self.snaps / "old.spec.json"
        legacy_data = {"queries": [{"id": "q1", "dax": "EVALUATE {10}"}]}
        legacy_path.write_text(json.dumps(legacy_data), encoding="utf-8")

        spec = checks.saved_spec(self.ws, "old")
        self.assertEqual(spec, legacy_data)

        (self.snaps / "specs").mkdir(parents=True, exist_ok=True)
        new_data = {"queries": [{"id": "q1", "dax": "EVALUATE {20}"}]}
        (self.snaps / "specs" / "old.json").write_text(json.dumps(new_data), encoding="utf-8")

        spec = checks.saved_spec(self.ws, "old")
        self.assertEqual(spec, new_data)

    def test_saved_spec_legacy_fallback_refuses_result_shaped_file(self):
        # A legacy result-shaped file at monthly.spec.json must not be misread as a spec for monthly
        legacy_path = self.snaps / "monthly.spec.json"
        res_data = {"queries": {"q1": {"rows": []}}, "refreshed": "2026-01-01T00:00:00"}
        legacy_path.write_text(json.dumps(res_data), encoding="utf-8")

        spec = checks.saved_spec(self.ws, "monthly")
        self.assertIsNone(spec)

    def test_saved_spec_primary_precedence_invalid_primary_does_not_fall_back(self):
        # When primary specs/foo.json exists but is invalid (queries not a list), do NOT fall back to legacy!
        (self.snaps / "specs").mkdir(parents=True, exist_ok=True)
        (self.snaps / "specs" / "foo.json").write_text('{"queries": {}}', encoding="utf-8")
        legacy_path = self.snaps / "foo.spec.json"
        legacy_path.write_text('{"queries": [{"id": "q1"}]}', encoding="utf-8")

        spec = checks.saved_spec(self.ws, "foo")
        self.assertIsNone(spec)

    def test_migration_dot_spec_result_baseline(self):
        # A legacy result baseline named .spec (file name .spec.json)
        res_data = {"queries": {"q1": {}}, "refreshed": "2026-01-01T00:00:00"}
        legacy_file = self.snaps / ".spec.json"
        legacy_file.write_text(json.dumps(res_data), encoding="utf-8")

        res = checks.migrate_snapshot_layout(self.ws)
        self.assertTrue(res["migrated"])
        self.assertEqual(res["results"], [".spec"])
        self.assertTrue((self.snaps / "results" / ".spec.json").is_file())
        self.assertFalse(legacy_file.exists())

    def test_migration_invalid_refreshed_type_fails_closed(self):
        # A legacy result file with numeric refreshed (e.g. 123 instead of string/null)
        bad_file = self.snaps / "bad_refresh.json"
        bad_file.write_text('{"queries": {}, "refreshed": 123}', encoding="utf-8")

        with self.assertRaises(PbzError) as ctx:
            checks.migrate_snapshot_layout(self.ws)
        self.assertEqual(ctx.exception.code, "CORRUPT_STATE")
        self.assertTrue(bad_file.exists())
        # Destination directories must not have been created on Pass 1 failure
        self.assertFalse((self.snaps / "results").exists())
        self.assertFalse((self.snaps / "specs").exists())

    def test_migration_anomalous_spec_file_fails_closed(self):
        # A spec-shaped file without .spec.json suffix is refused as CORRUPT_STATE
        anomalous = self.snaps / "anomalous.json"
        anomalous.write_text(json.dumps({"queries": [{"id": "q1"}]}), encoding="utf-8")

        with self.assertRaises(PbzError) as ctx:
            checks.migrate_snapshot_layout(self.ws)
        self.assertEqual(ctx.exception.code, "CORRUPT_STATE")
        self.assertTrue(anomalous.exists())
        self.assertFalse((self.snaps / "results").exists())
        self.assertFalse((self.snaps / "specs").exists())

    def test_evidence_state_names_refuses_intermediate_reparse_link(self):
        # If snapshots itself is flagged as a reparse link, looking up snapshots/results must fail closed
        with patch("pbz.evidence._reparse_link", side_effect=lambda p: os.path.normpath(str(p)).endswith("snapshots")):
            with self.assertRaises(PbzError) as ctx:
                evidence._state_names(self.ws, os.path.join("snapshots", "results"), base_dir=self.ws.private)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")

    def test_evidence_suggested_tests_includes_results_monthly_spec_and_unions_legacy(self):
        # In ws.private/snapshots/results/: q1.json
        (self.snaps / "results").mkdir(parents=True, exist_ok=True)
        (self.snaps / "results" / "q1.json").write_text("{}", encoding="utf-8")

        # In legacy ws.private/snapshots/:
        # 1. monthly.spec.json (result-shaped) -> should be suggested as monthly.spec
        (self.snaps / "monthly.spec.json").write_text('{"queries": {}, "refreshed": "2026-01-01"}', encoding="utf-8")
        # 2. legacy.json (result-shaped) -> should be suggested as legacy
        (self.snaps / "legacy.json").write_text('{"queries": {}, "refreshed": "2026-01-01"}', encoding="utf-8")
        # 3. pure_spec.spec.json (spec-shaped) -> should be omitted
        (self.snaps / "pure_spec.spec.json").write_text('{"queries": [{"id": "q1"}]}', encoding="utf-8")

        suggested = evidence._suggested_tests(self.ws, None, [])
        # Must include q1 from results
        self.assertIn("pbz snapshot compare q1", suggested)
        # Must include monthly.spec from result-shaped legacy file
        self.assertIn("pbz snapshot compare monthly.spec", suggested)
        # Must include legacy from legacy result file
        self.assertIn("pbz snapshot compare legacy", suggested)
        # Must NOT include pure_spec.spec (since that was a spec file in the legacy directory)
        self.assertNotIn("pbz snapshot compare pure_spec.spec", suggested)
        self.assertNotIn("pbz snapshot compare pure_spec", suggested)

    def test_evidence_suggested_tests_pbz_state_b_dir_none(self):
        # Under .pbz/snapshots/ (b_dir=None path):
        pbz_snaps = Path(self.ws.mirror).parent / ".pbz" / "snapshots"
        pbz_snaps.mkdir(parents=True, exist_ok=True)
        (pbz_snaps / "shared_res.spec.json").write_text('{"queries": {}, "refreshed": "2026-01-01"}', encoding="utf-8")
        suggested = evidence._suggested_tests(self.ws, None, [])
        self.assertIn("pbz snapshot compare shared_res.spec", suggested)

    def test_evidence_suggested_tests_omits_invalid_refreshed_type(self):
        # Ambiguous legacy file with non-string/non-null refreshed is omitted from suggestions
        (self.snaps / "bad_num_ref.spec.json").write_text('{"queries": {}, "refreshed": 123}', encoding="utf-8")
        suggested = evidence._suggested_tests(self.ws, None, [])
        self.assertNotIn("pbz snapshot compare bad_num_ref.spec", suggested)

    def test_evidence_state_names_refuses_dot_dot_traversal(self):
        with self.assertRaises(PbzError) as ctx:
            evidence._state_names(self.ws, "../escape", base_dir=self.ws.private)
        self.assertEqual(ctx.exception.code, "UNSAFE_PATH")

    def test_first_save_creates_specs_directory_on_fresh_workspace(self):
        # Fresh workspace with no pre-existing snapshots directory
        if self.snaps.exists():
            import shutil
            shutil.rmtree(self.snaps)
        self.assertFalse(self.snaps.exists())
        with patch("pbz.desktop.require_port", return_value=1234), \
             patch("pbz.checks.engine_call", return_value={"ok": True, "class": "OK"}), \
             patch("pbz.checks.spend", return_value=True):
            res = checks.snapshot(self.ws, None, "save", "fresh_snap", {"queries": [{"id": "q1", "dax": "EVALUATE {1}"}]})
            self.assertTrue(res.get("ok"))
            self.assertTrue((self.snaps / "specs" / "fresh_snap.json").is_file())

    def test_snapshot_refuses_empty_queries_in_checks(self):
        # snapshot() must fail closed with BAD_REQUEST when queries list is empty
        # and must not spend budget or invoke engine or write files
        with patch("pbz.desktop.require_port", return_value=1234), \
             patch("pbz.checks.spend") as mock_spend, \
             patch("pbz.checks.engine_call") as mock_engine:
            for action in ("save", "compare"):
                for empty_spec in ({"queries": []}, {}, None):
                    with self.subTest(action=action, empty_spec=empty_spec):
                        with self.assertRaises(PbzError) as ctx:
                            checks.snapshot(self.ws, None, action, "empty_snap", empty_spec)
                        self.assertEqual(ctx.exception.code, "BAD_REQUEST")
                        self.assertIn("queries must not be empty", str(ctx.exception))
            mock_spend.assert_not_called()
            mock_engine.assert_not_called()
            self.assertFalse((self.snaps / "specs" / "empty_snap.json").exists())
            self.assertFalse((self.snaps / "results" / "empty_snap.json").exists())

    def test_engine_refuses_empty_queries_in_save_and_compare(self):
        # Direct engine call with empty queries must return BAD_REQUEST
        for verb in ("snapshot-save", "snapshot-compare"):
            res = checks.engine_call(verb, {
                "private_dir": self.ws.private,
                "port": 9999,
                "name": "empty_queries",
                "queries": [],
            })
            self.assertFalse(res.get("ok"))
            self.assertEqual(res.get("class"), "BAD_REQUEST")


if __name__ == "__main__":
    unittest.main()
