"""Snapshot store namespace layout, migration, idempotency, shape verification, and fallback tests."""
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

from pbz import checks, evidence, guard, tokens
from pbz.common import PbzError
from mirror_helpers import make_workspace
from test_mirror_writers_root import _tiny

CLI = os.path.join(SKILL, "pbz", "cli.py")
VALID_SPEC = '{"queries":[{"id":"q1","dax":"EVALUATE {1}"}]}'
LEGACY_RESULT = {"queries": {"q1": {"rows": ["abc123def456"]}}, "refreshed": "2026-01-01T00:00:00"}


def _tree(root: Path) -> tuple:
    """Relative path, entry type, and file byte hash for the whole snapshot directory."""
    if not root.exists():
        return (("ABSENT", "absent", ""),)
    rows = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        for name in dirnames:
            path = Path(dirpath) / name
            rows.append((path.relative_to(root).as_posix(), "dir", ""))
        for name in filenames:
            path = Path(dirpath) / name
            rows.append((path.relative_to(root).as_posix(), "file", hashlib.sha256(path.read_bytes()).hexdigest()))
    return tuple(sorted(rows))


def _tree_with_pending_lock(before, name):
    lock = (f"pending/{name}.lock", "file", hashlib.sha256(b"").hexdigest())
    return tuple(sorted((*before, ("pending", "dir", ""), lock)))


def _public_cli(workspace: str, private_root: str, args: list):
    env = os.environ.copy()
    env["PBZ_PRIVATE_ROOT"] = private_root
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PBZ_SKIP_OUTPUT", None)
    return subprocess.run(
        [sys.executable, "-I", CLI, *args],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )


def _public_snapshot(workspace: str, private_root: str, action: str, name: str, spec: str):
    return _public_cli(workspace, private_root, ["snapshot", action, name, "--spec", spec])


def _public_budget(private_root: str, workspace: str) -> int:
    wid = hashlib.sha256(guard.norm(os.path.abspath(workspace)).encode()).hexdigest()[:16]
    path = Path(private_root) / wid / "state.json"
    if not path.is_file():
        return 0
    doc = json.loads(path.read_text(encoding="utf-8"))
    return int(doc.get("snapshot_queries") or 0)


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
        def fake_engine(verb, payload, timeout=1800, **kwargs):
            results = self.snaps / "results"
            results.mkdir()
            (results / (payload["name"] + ".json")).write_text(json.dumps(LEGACY_RESULT), encoding="utf-8")
            return {"ok": True, "class": "OK", "payload_state": "may_have_been_sent",
                    "queries": [{"id": "q1", "result": "SAVED"}]}

        with patch("pbz.desktop.require_port", return_value=1234), \
             patch("pbz.checks.engine_call", side_effect=fake_engine), \
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
                        message = ("--spec required" if action == "compare" and empty_spec is None
                                   else "queries must not be empty")
                        self.assertIn(message, str(ctx.exception))
            mock_spend.assert_not_called()
            mock_engine.assert_not_called()
            self.assertFalse((self.snaps / "specs" / "empty_snap.json").exists())
            self.assertFalse((self.snaps / "results" / "empty_snap.json").exists())

    def _seed_public_tree(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "ws"
        root.mkdir()
        _tiny(str(root))
        private = Path(tmp.name) / "private"
        private.mkdir()
        wid = hashlib.sha256(guard.norm(os.path.abspath(root)).encode()).hexdigest()[:16]
        snaps = private / wid / "snapshots"
        snaps.mkdir(parents=True)
        (snaps / "earlier.json").write_text(json.dumps(LEGACY_RESULT), encoding="utf-8")
        (snaps / "notes.txt").write_text("keep\n", encoding="utf-8")
        return str(root), str(private), snaps

    def test_public_cli_empty_query_leaves_snapshot_tree(self):
        # Public launcher. Desktop is not required. Empty queries are invalid before any port probe.
        for action in ("compare", "save"):
            with self.subTest(action=action):
                workspace, private, snaps = self._seed_public_tree()
                before = _tree(snaps)
                proc = _public_snapshot(workspace, private, action, "different", '{"queries":[]}')
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                payload = json.loads(proc.stdout)
                self.assertEqual(payload.get("error"), "BAD_REQUEST")
                self.assertEqual(payload["outcome"]["status"], "INVALID")
                self.assertEqual(payload["outcome"]["exit_code"], 2)
                self.assertEqual(_tree(snaps), before)

    def test_public_cli_valid_query_desktop_down_leaves_snapshot_tree(self):
        for action in ("compare", "save"):
            with self.subTest(action=action):
                workspace, private, snaps = self._seed_public_tree()
                before = _tree(snaps)
                proc = _public_snapshot(workspace, private, action, "different", VALID_SPEC)
                self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
                payload = json.loads(proc.stdout)
                self.assertEqual(payload.get("error"), "ENGINE_UNAVAILABLE")
                self.assertEqual(payload["outcome"]["status"], "UNAVAILABLE")
                self.assertEqual(payload["outcome"]["exit_code"], 4)
                self.assertEqual(_tree(snaps), _tree_with_pending_lock(before, "different"))

    def test_public_cli_cold_snapshot_dir_stays_absent(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "ws"
        root.mkdir()
        _tiny(str(root))
        private = Path(tmp.name) / "private"
        private.mkdir()
        wid = hashlib.sha256(guard.norm(os.path.abspath(root)).encode()).hexdigest()[:16]
        snaps = private / wid / "snapshots"
        self.assertFalse(snaps.exists())
        proc = _public_snapshot(str(root), str(private), "save", "different", '{"queries":[]}')
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertFalse(snaps.exists())

    def test_empty_queries_do_not_probe_port_or_spend(self):
        with patch("pbz.desktop.require_port") as port, \
             patch("pbz.checks.spend") as spend, \
             patch("pbz.checks.engine_call") as engine:
            for action in ("save", "compare"):
                with self.assertRaises(PbzError) as ctx:
                    checks.snapshot(self.ws, None, action, "different", {"queries": []})
                self.assertEqual(ctx.exception.code, "BAD_REQUEST")
            port.assert_not_called()
            spend.assert_not_called()
            engine.assert_not_called()

    def test_unrelated_malformed_legacy_does_not_reject_another_name(self):
        (self.snaps / "bad.json").write_text("{not-json", encoding="utf-8")
        (self.snaps / "notes.txt").write_text("keep\n", encoding="utf-8")
        before = _tree(self.snaps)
        with patch("pbz.desktop.require_port", side_effect=PbzError("ENGINE_UNAVAILABLE", "desktop down")):
            with self.assertRaises(PbzError) as ctx:
                checks.snapshot(self.ws, None, "save", "different", {"queries": [{"id": "q1", "dax": "EVALUATE {1}"}]})
            self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")
        self.assertEqual(_tree(self.snaps), _tree_with_pending_lock(before, "different"))

    def test_malformed_requested_baseline_fails_before_port(self):
        (self.snaps / "different.json").write_text("{not-json", encoding="utf-8")
        (self.snaps / "notes.txt").write_text("keep\n", encoding="utf-8")
        before = _tree(self.snaps)
        with patch("pbz.desktop.require_port") as port, \
             patch("pbz.checks.engine_call") as engine, \
             patch("pbz.checks.spend") as spend:
            with self.assertRaises(PbzError) as ctx:
                checks.snapshot(self.ws, None, "compare", "different", {"queries": [{"id": "q1", "dax": "EVALUATE {1}"}]})
            self.assertEqual(ctx.exception.code, "CORRUPT_STATE")
            port.assert_not_called()
            engine.assert_not_called()
            spend.assert_not_called()
        self.assertEqual(_tree(self.snaps), _tree_with_pending_lock(before, "different"))

    def test_malformed_namespaced_result_does_not_fall_back_to_legacy(self):
        (self.snaps / "results").mkdir()
        (self.snaps / "results" / "different.json").write_text("{not-json", encoding="utf-8")
        (self.snaps / "different.json").write_text(json.dumps(LEGACY_RESULT), encoding="utf-8")
        before = _tree(self.snaps)
        with patch("pbz.desktop.require_port") as port, \
             patch("pbz.checks.engine_call") as engine, \
             patch("pbz.checks.spend") as spend:
            with self.assertRaises(PbzError) as ctx:
                checks.snapshot(
                    self.ws, None, "compare", "different", {"queries": [{"id": "q1", "dax": "EVALUATE {1}"}]}
                )
            self.assertEqual(ctx.exception.code, "CORRUPT_STATE")
            port.assert_not_called()
            engine.assert_not_called()
            spend.assert_not_called()
        self.assertEqual(_tree(self.snaps), _tree_with_pending_lock(before, "different"))

    def test_namespaced_result_wins_and_legacy_bytes_stay(self):
        (self.snaps / "results").mkdir()
        namespaced = {"queries": {"q1": {"rows": ["aa"]}}, "refreshed": "2026-02-01T00:00:00"}
        legacy = {"queries": {"q1": {"rows": ["bb"]}}, "refreshed": "2026-01-01T00:00:00"}
        (self.snaps / "results" / "different.json").write_text(json.dumps(namespaced), encoding="utf-8")
        (self.snaps / "different.json").write_text(json.dumps(legacy), encoding="utf-8")
        (self.snaps / "notes.txt").write_text("keep\n", encoding="utf-8")
        legacy_bytes = (self.snaps / "different.json").read_bytes()
        notes_bytes = (self.snaps / "notes.txt").read_bytes()
        with patch("pbz.desktop.require_port", return_value=1), \
             patch("pbz.checks.engine_call", return_value={"ok": True, "queries": [{"id": "q1", "result": "SAME"}]}) as engine, \
             patch("pbz.checks.spend", return_value=True):
            res = checks.snapshot(self.ws, None, "compare", "different", {"queries": [{"id": "q1", "dax": "EVALUATE {1}"}]})
        self.assertTrue(res.get("ok"))
        engine.assert_called_once()
        self.assertEqual((self.snaps / "different.json").read_bytes(), legacy_bytes)
        self.assertEqual((self.snaps / "notes.txt").read_bytes(), notes_bytes)
        self.assertEqual(json.loads((self.snaps / "results" / "different.json").read_text("utf-8")), namespaced)

    def test_adapter_save_writes_only_the_named_snapshot(self):
        # Adapter, not a live engine run. The stand-in writes only the namespaced result.
        (self.snaps / "earlier.json").write_text(json.dumps(LEGACY_RESULT), encoding="utf-8")
        (self.snaps / "notes.txt").write_text("keep\n", encoding="utf-8")
        earlier_bytes = (self.snaps / "earlier.json").read_bytes()
        notes_bytes = (self.snaps / "notes.txt").read_bytes()

        def fake_engine(verb, payload, timeout=1800, **kwargs):
            self.assertEqual(verb, "snapshot-save")
            self.assertEqual(payload["name"], "different")
            results = self.snaps / "results"
            results.mkdir(parents=True, exist_ok=True)
            (results / "different.json").write_text(
                json.dumps({"queries": {"q1": {}}, "refreshed": "2026-03-01T00:00:00"}),
                encoding="utf-8",
            )
            return {"ok": True, "payload_state": "may_have_been_sent", "queries": [{"id": "q1", "result": "SAVED"}]}

        with patch("pbz.desktop.require_port", return_value=1), \
             patch("pbz.checks.engine_call", side_effect=fake_engine), \
             patch("pbz.checks.spend", return_value=True):
            res = checks.snapshot(
                self.ws, None, "save", "different", {"queries": [{"id": "q1", "dax": "EVALUATE {1}"}]}
            )
        self.assertTrue(res.get("ok"))
        self.assertEqual((self.snaps / "earlier.json").read_bytes(), earlier_bytes)
        self.assertEqual((self.snaps / "notes.txt").read_bytes(), notes_bytes)
        self.assertTrue((self.snaps / "results" / "different.json").is_file())
        self.assertTrue((self.snaps / "specs" / "different.json").is_file())
        self.assertFalse((self.snaps / "results" / "earlier.json").exists())

    def test_adapter_compare_reads_legacy_result_in_place(self):
        # Adapter, not a live engine run. Legacy result and spec stay at their original paths.
        (self.snaps / "earlier.json").write_text(json.dumps(LEGACY_RESULT), encoding="utf-8")
        spec = {"queries": [{"id": "q1", "dax": "EVALUATE {1}"}]}
        (self.snaps / "earlier.spec.json").write_text(json.dumps(spec), encoding="utf-8")
        (self.snaps / "notes.txt").write_text("keep\n", encoding="utf-8")
        before = _tree(self.snaps)

        def fake_engine(verb, payload, timeout=1800, **kwargs):
            self.assertEqual(verb, "snapshot-compare")
            self.assertTrue((self.snaps / "earlier.json").is_file())
            self.assertFalse((self.snaps / "results" / "earlier.json").exists())
            return {"ok": True, "payload_state": "may_have_been_sent", "queries": [{"id": "q1", "result": "SAME"}]}

        with patch("pbz.desktop.require_port", return_value=1), \
             patch("pbz.checks.engine_call", side_effect=fake_engine), \
             patch("pbz.checks.spend", return_value=True):
            res = checks.snapshot(self.ws, None, "compare", "earlier", spec)
        self.assertTrue(res.get("ok"))
        self.assertEqual(_tree(self.snaps), _tree_with_pending_lock(before, "earlier"))

    def test_adapter_monthly_names_do_not_share_files(self):
        # Adapter, not a live engine run. monthly and monthly.spec stay distinct.
        def fake_engine(verb, payload, timeout=1800, **kwargs):
            results = self.snaps / "results"
            results.mkdir(parents=True, exist_ok=True)
            (results / (payload["name"] + ".json")).write_text(
                json.dumps({"queries": {"q1": {"name": payload["name"]}}, "refreshed": "2026-01-01T00:00:00"}),
                encoding="utf-8",
            )
            return {"ok": True, "payload_state": "may_have_been_sent", "queries": [{"id": "q1", "result": "SAVED"}]}

        with patch("pbz.desktop.require_port", return_value=1), \
             patch("pbz.checks.engine_call", side_effect=fake_engine), \
             patch("pbz.checks.spend", return_value=True):
            checks.snapshot(self.ws, None, "save", "monthly", {"queries": [{"id": "q1", "dax": "EVALUATE {1}"}]})
            first_result = (self.snaps / "results" / "monthly.json").read_bytes()
            first_spec = (self.snaps / "specs" / "monthly.json").read_bytes()
            checks.snapshot(self.ws, None, "save", "monthly.spec", {"queries": [{"id": "q1", "dax": "EVALUATE {2}"}]})
        self.assertEqual((self.snaps / "results" / "monthly.json").read_bytes(), first_result)
        self.assertEqual((self.snaps / "specs" / "monthly.json").read_bytes(), first_spec)
        self.assertTrue((self.snaps / "results" / "monthly.spec.json").is_file())
        self.assertTrue((self.snaps / "specs" / "monthly.spec.json").is_file())
        self.assertNotEqual(
            (self.snaps / "results" / "monthly.json").read_bytes(),
            (self.snaps / "results" / "monthly.spec.json").read_bytes(),
        )

    def test_spec_shaped_legacy_file_stays_put(self):
        # Adapter. A spec-shaped legacy file is not deleted or moved. The live engine
        # case that reports NEW for this file is unchanged and is not run here.
        doc = {"queries": [{"id": "q1", "dax": "EVALUATE {1}"}]}
        (self.snaps / "unmigrated.spec.json").write_text(json.dumps(doc), encoding="utf-8")
        before = _tree(self.snaps)
        with patch("pbz.desktop.require_port", return_value=1), \
             patch("pbz.checks.engine_call", return_value={"ok": True, "queries": [{"id": "q1", "result": "NEW"}]}), \
             patch("pbz.checks.spend", return_value=True):
            res = checks.snapshot(
                self.ws, None, "compare", "unmigrated.spec", {"queries": [{"id": "q1", "dax": "EVALUATE {1}"}]}
            )
        self.assertTrue(res.get("ok"))
        self.assertEqual(_tree(self.snaps), _tree_with_pending_lock(before, "unmigrated.spec"))

    def _state_bytes(self):
        path = checks._state_path(self.ws)
        if not os.path.lexists(path):
            return None
        return Path(path).read_bytes()

    def _refuse_before_port(self, spec, message):
        before = _tree(self.snaps)
        state = self._state_bytes()
        with patch("pbz.desktop.require_port") as port, \
             patch("pbz.checks.engine_call") as engine:
            with self.assertRaises(PbzError) as ctx:
                checks.snapshot(self.ws, None, "save", "different", spec)
            self.assertEqual(ctx.exception.code, "BAD_REQUEST")
            self.assertIn(message, str(ctx.exception))
            port.assert_not_called()
            engine.assert_not_called()
        self.assertEqual(_tree(self.snaps), before)
        self.assertEqual(self._state_bytes(), state)

    def _reaches_port(self, spec, *, creates_lock=False):
        before = _tree(self.snaps)
        if creates_lock:
            before = _tree_with_pending_lock(before, "different")
        state = self._state_bytes()
        with patch("pbz.desktop.require_port", side_effect=PbzError("ENGINE_UNAVAILABLE", "desktop down")) as port, \
             patch("pbz.checks.engine_call") as engine:
            with self.assertRaises(PbzError) as ctx:
                checks.snapshot(self.ws, None, "save", "different", spec)
            self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")
            port.assert_called_once()
            engine.assert_not_called()
        self.assertEqual(_tree(self.snaps), before)
        self.assertEqual(self._state_bytes(), state)

    def test_query_shape_is_rejected_before_port_or_spend(self):
        bad_id = [
            {"queries": [{"dax": "EVALUATE {1}"}]},
            {"queries": [{"id": "", "dax": "EVALUATE {1}"}]},
            {"queries": [{"id": "a" * 129, "dax": "EVALUATE {1}"}]},
            {"queries": [{"id": "q1!", "dax": "EVALUATE {1}"}]},
            {"queries": [{"id": 1, "dax": "EVALUATE {1}"}]},
        ]
        for spec in bad_id:
            with self.subTest(spec=spec):
                self._refuse_before_port(spec, "query id is invalid")
        self._refuse_before_port(
            {"queries": [{"id": "q1", "dax": "EVALUATE {1}"}, {"id": "q1", "dax": "EVALUATE {2}"}]},
            "duplicate query id",
        )
        for spec in (
            {"queries": [{"id": "q1"}]},
            {"queries": [{"id": "q1", "dax": ""}]},
            {"queries": [{"id": "q1", "dax": "   "}]},
            {"queries": [{"id": "q1", "dax": 1}]},
        ):
            with self.subTest(spec=spec):
                self._refuse_before_port(spec, "dax is required")
        for spec in (
            {"queries": [{"id": "q1", "dax": "EVALUATE {1}", "keys": False}]},
            {"queries": [{"id": "q1", "dax": "EVALUATE {1}", "keys": ["ok", 1]}]},
            {"queries": [{"id": "q1", "dax": "EVALUATE {1}", "keys": {"a": "b"}}]},
        ):
            with self.subTest(spec=spec):
                self._refuse_before_port(spec, "keys must be a list of strings")
        boundary = {"queries": [{"id": "a" * 128, "dax": "EVALUATE {1}"}]}
        with self.subTest(spec=boundary):
            self._reaches_port(boundary, creates_lock=True)
        accepted = [
            {"queries": [{"id": "q1", "dax": "EVALUATE {1}"}]},
            {"queries": [{"id": "q1", "dax": "EVALUATE {1}", "keys": None}]},
            {"queries": [{"id": "q1", "dax": "EVALUATE {1}", "keys": "Person"}]},
            {"queries": [{"id": "q1", "dax": "EVALUATE {1}", "keys": ["Person"]}]},
            {"queries": [{"id": "q1", "dax": "EVALUATE {1}", "keys": []}]},
            {"queries": [{"id": "A", "dax": "EVALUATE {1}"}, {"id": "a", "dax": "EVALUATE {2}"}]},
        ]
        for spec in accepted:
            with self.subTest(spec=spec):
                self._reaches_port(spec)

    def test_specs_file_does_not_spend_or_call_engine(self):
        (self.snaps / "results").mkdir()
        result = self.snaps / "results" / "different.json"
        original = b'{"queries":{"q1":{"rows":["kept"]}},"refreshed":"2026-01-01T00:00:00"}'
        result.write_bytes(original)
        (self.snaps / "specs").write_bytes(b"not-a-directory")
        (self.snaps / "earlier.json").write_text(json.dumps(LEGACY_RESULT), encoding="utf-8")
        before = _tree(self.snaps)
        state = self._state_bytes()

        def overwrite(verb, payload, timeout=1800, **kwargs):
            result.write_bytes(b"changed-by-engine")
            return {"ok": True, "payload_state": "may_have_been_sent"}

        with patch("pbz.desktop.require_port", return_value=1) as port, \
             patch("pbz.checks.engine_call", side_effect=overwrite) as engine:
            with self.assertRaises(PbzError) as ctx:
                checks.snapshot(
                    self.ws, None, "save", "different",
                    {"queries": [{"id": "q1", "dax": "EVALUATE {1}"}]},
                )
            self.assertEqual(ctx.exception.code, "CORRUPT_STATE")
            port.assert_not_called()
            engine.assert_not_called()
        self.assertEqual(result.read_bytes(), original)
        self.assertTrue((self.snaps / "specs").is_file())
        self.assertEqual(_tree(self.snaps), before)
        self.assertEqual(self._state_bytes(), state)

    def test_result_directory_file_and_result_path_directory_are_corrupt(self):
        (self.snaps / "results").write_bytes(b"not-a-directory")
        before = _tree(self.snaps)
        with patch("pbz.desktop.require_port") as port, patch("pbz.checks.engine_call") as engine:
            with self.assertRaises(PbzError) as ctx:
                checks.snapshot(
                    self.ws, None, "save", "different",
                    {"queries": [{"id": "q1", "dax": "EVALUATE {1}"}]},
                )
            self.assertEqual(ctx.exception.code, "CORRUPT_STATE")
            port.assert_not_called()
            engine.assert_not_called()
        self.assertEqual(_tree(self.snaps), before)
        (self.snaps / "results").unlink()
        (self.snaps / "results").mkdir()
        (self.snaps / "results" / "different.json").mkdir()
        with patch("pbz.desktop.require_port") as port, patch("pbz.checks.engine_call") as engine:
            with self.assertRaises(PbzError) as ctx:
                checks.snapshot(
                    self.ws, None, "save", "different",
                    {"queries": [{"id": "q1", "dax": "EVALUATE {1}"}]},
                )
            self.assertEqual(ctx.exception.code, "CORRUPT_STATE")
            port.assert_not_called()
            engine.assert_not_called()

    def test_private_root_file_is_corrupt_before_port(self):
        parent = Path(self.ws.private).parent
        name = Path(self.ws.private).name
        if self.snaps.exists():
            import shutil
            shutil.rmtree(self.ws.private)
        (parent / name).write_bytes(b"not-a-directory")
        self.ws.private = str(parent / name)
        with patch("pbz.desktop.require_port") as port, patch("pbz.checks.engine_call") as engine:
            with self.assertRaises(PbzError) as ctx:
                checks.snapshot(
                    self.ws, None, "save", "different",
                    {"queries": [{"id": "q1", "dax": "EVALUATE {1}"}]},
                )
            self.assertEqual(ctx.exception.code, "CORRUPT_STATE")
            port.assert_not_called()
            engine.assert_not_called()
        self.assertEqual((parent / name).read_bytes(), b"not-a-directory")

    def test_snapshot_link_is_unsafe_before_port(self):
        from _junction import make_junction
        external = self.snaps.parent / "outside-snapshots"
        external.mkdir()
        make_junction(self, str(self.snaps / "specs"), str(external))
        before = _tree(self.snaps)
        with patch("pbz.desktop.require_port") as port, patch("pbz.checks.engine_call") as engine:
            with self.assertRaises(PbzError) as ctx:
                checks.snapshot(
                    self.ws, None, "save", "different",
                    {"queries": [{"id": "q1", "dax": "EVALUATE {1}"}]},
                )
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            port.assert_not_called()
            engine.assert_not_called()
        self.assertEqual(_tree(self.snaps), before)

    def test_spec_write_failure_does_not_refund_or_restore(self):
        # Recovery is required. The engine write is not rolled back and the query stays spent.
        written = b'{"queries":{"q1":{"rows":["engine"]}},"refreshed":"2026-01-01T00:00:00"}'

        def fake_engine(verb, payload, timeout=1800, **kwargs):
            dest = os.path.join(payload["private_dir"], "snapshots", "results", payload["name"] + ".json")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as handle:
                handle.write(written)
            return {"ok": True, "payload_state": "may_have_been_sent", "class": "OK", "queries": [{"id": "q1", "result": "SAVED"}]}

        real_replace = os.replace

        def fail_spec(src, dst):
            parent = os.path.basename(os.path.dirname(dst))
            if parent == "specs":
                raise OSError("disk")
            return real_replace(src, dst)

        with patch("pbz.desktop.require_port", return_value=1), \
             patch("pbz.checks.engine_call", side_effect=fake_engine), \
             patch("pbz.checks.os.replace", side_effect=fail_spec):
            with self.assertRaises(PbzError) as ctx:
                checks.snapshot(
                    self.ws, None, "save", "different",
                    {"queries": [{"id": "q1", "dax": "EVALUATE {1}"}]},
                )
        self.assertEqual(ctx.exception.code, "RECOVERY_REQUIRED")
        result = self.snaps / "results" / "different.json"
        self.assertEqual(result.read_bytes(), written)
        self.assertFalse((self.snaps / "specs" / "different.json").exists())
        self.assertEqual(checks._load_state(self.ws)["snapshot_queries"], 1)

    def test_public_cli_specs_file_leaves_tree_and_budget(self):
        workspace, private, snaps = self._seed_public_tree()
        (snaps / "results").mkdir()
        (snaps / "results" / "different.json").write_bytes(
            b'{"queries":{"q1":{"rows":["kept"]}},"refreshed":"2026-01-01T00:00:00"}'
        )
        (snaps / "specs").write_bytes(b"not-a-directory")
        before = _tree(snaps)
        self.assertEqual(_public_budget(private, workspace), 0)
        proc = _public_snapshot(workspace, private, "save", "different", VALID_SPEC)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload.get("error"), "CORRUPT_STATE")
        self.assertEqual(payload["outcome"]["status"], "FAILED")
        self.assertEqual(payload["outcome"]["exit_code"], 2)
        self.assertEqual(_tree(snaps), before)
        self.assertEqual(_public_budget(private, workspace), 0)

    def test_public_cli_explicit_empty_spec_does_not_use_saved_spec(self):
        workspace, private, snaps = self._seed_public_tree()
        (snaps / "specs").mkdir()
        (snaps / "specs" / "different.json").write_text(VALID_SPEC, encoding="utf-8")
        before = _tree(snaps)
        for spec in ("{}", "null", '{"queries":[]}'):
            with self.subTest(spec=spec):
                proc = _public_snapshot(workspace, private, "compare", "different", spec)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                payload = json.loads(proc.stdout)
                self.assertEqual(payload.get("error"), "BAD_REQUEST")
                self.assertEqual(payload["outcome"]["status"], "INVALID")
                self.assertEqual(_tree(snaps), before)
                self.assertEqual(_public_budget(private, workspace), 0)
        omitted = _public_cli(workspace, private, ["snapshot", "compare", "different"])
        self.assertEqual(omitted.returncode, 4, omitted.stdout + omitted.stderr)
        payload = json.loads(omitted.stdout)
        self.assertEqual(payload.get("error"), "ENGINE_UNAVAILABLE")
        self.assertEqual(payload["outcome"]["status"], "UNAVAILABLE")
        self.assertEqual(_tree(snaps), _tree_with_pending_lock(before, "different"))
        self.assertEqual(_public_budget(private, workspace), 0)

    # PR A red tests: a save where no query succeeds must not publish.
    # The adapter half is proven here with a response-only fake engine (it
    # returns the engine verdicts and writes nothing itself, like the other
    # adapter tests in this file). The result-file half is proven against the
    # real engine in tests/test_engine.py, because a fake that writes the
    # result file cannot be turned green by any adapter change.

    _GOOD_RESULT = b'{"queries":{"q1":{"rows":["good"]}},"refreshed":"2026-01-01T00:00:00"}'
    _OLD_SPEC = {"queries": [{"id": "q1", "dax": "EVALUATE {1}"}]}
    _NEW_SPEC = {"queries": [{"id": "q1", "dax": "EVALUATE {2}"}]}

    def _seed_good_pair(self):
        (self.snaps / "results").mkdir(exist_ok=True)
        (self.snaps / "specs").mkdir(exist_ok=True)
        (self.snaps / "results" / "different.json").write_bytes(self._GOOD_RESULT)
        spec_path = self.snaps / "specs" / "different.json"
        spec_path.write_text(json.dumps(self._OLD_SPEC), encoding="utf-8")
        return spec_path, spec_path.read_bytes()

    def test_all_failed_save_preserves_spec_and_charge(self):
        spec_path, spec_bytes = self._seed_good_pair()

        def fake_engine(verb, payload, timeout=1800, **kwargs):
            return {"ok": True, "payload_state": "may_have_been_sent", "verb": "snapshot-save", "name": "different",
                    "queries": [{"id": "q1", "result": "ERROR", "class": "OTHER"}]}

        with patch("pbz.desktop.require_port", return_value=1), \
             patch("pbz.checks.engine_call", side_effect=fake_engine):
            res = checks.snapshot(self.ws, None, "save", "different", self._NEW_SPEC)
        self.assertTrue(res.get("ok"))
        self.assertEqual(res["queries"][0]["result"], "ERROR")
        self.assertEqual(checks._load_state(self.ws)["snapshot_queries"], 1)
        self.assertEqual(spec_path.read_bytes(), spec_bytes)

    def test_all_failed_save_preserves_spec_for_several_queries(self):
        spec_path, spec_bytes = self._seed_good_pair()
        spec = {"queries": [{"id": f"q{i}", "dax": f"EVALUATE {{{i}}}"} for i in (1, 2, 3)]}

        def fake_engine(verb, payload, timeout=1800, **kwargs):
            return {"ok": True, "payload_state": "may_have_been_sent", "verb": "snapshot-save", "name": "different",
                    "queries": [{"id": q["id"], "result": "ERROR", "class": "OTHER"}
                                for q in payload["queries"]]}

        with patch("pbz.desktop.require_port", return_value=1), \
             patch("pbz.checks.engine_call", side_effect=fake_engine):
            res = checks.snapshot(self.ws, None, "save", "different", spec)
        self.assertEqual([q["result"] for q in res["queries"]], ["ERROR"] * 3)
        self.assertEqual(checks._load_state(self.ws)["snapshot_queries"], 3)
        self.assertEqual(spec_path.read_bytes(), spec_bytes)

    def test_first_all_failed_save_creates_no_spec_and_no_temp(self):
        def fake_engine(verb, payload, timeout=1800, **kwargs):
            return {"ok": True, "payload_state": "may_have_been_sent", "verb": "snapshot-save", "name": "different",
                    "queries": [{"id": "q1", "result": "ERROR", "class": "OTHER"}]}

        with patch("pbz.desktop.require_port", return_value=1), \
             patch("pbz.checks.engine_call", side_effect=fake_engine):
            res = checks.snapshot(self.ws, None, "save", "different", self._NEW_SPEC)
        self.assertEqual(res["queries"][0]["result"], "ERROR")
        self.assertFalse((self.snaps / "specs" / "different.json").exists())
        leftovers = [p for p in self.snaps.rglob("*") if "snapshot-spec." in p.name]
        self.assertEqual(leftovers, [])

    def test_save_with_no_success_signal_does_not_write_spec(self):
        for response in ({"ok": True, "verb": "snapshot-save", "name": "different", "queries": []},
                         {"ok": True, "verb": "snapshot-save", "name": "different"}):
            with self.subTest(response=response):
                # Reseed inside the subtest so each response demonstrates its
                # own failure, independent of what the previous case wrote.
                spec_path, spec_bytes = self._seed_good_pair()
                with patch("pbz.desktop.require_port", return_value=1), \
                     patch("pbz.checks.engine_call", return_value=dict(response, payload_state="may_have_been_sent")):
                    checks.snapshot(self.ws, None, "save", "different", self._NEW_SPEC)
                self.assertEqual(spec_path.read_bytes(), spec_bytes)

    def test_zero_row_success_still_publishes(self):
        # Response-only control: the fake reports SAVED for a query whose
        # result set is empty, and the adapter must still publish the spec.
        # Result-file publication for a zero-row success is asserted against
        # the real engine in tests/test_engine.py.
        spec_path, _spec_bytes = self._seed_good_pair()

        def fake_engine(verb, payload, timeout=1800, **kwargs):
            return {"ok": True, "payload_state": "may_have_been_sent", "verb": "snapshot-save", "name": "different",
                    "queries": [{"id": "q1", "result": "SAVED"}]}

        with patch("pbz.desktop.require_port", return_value=1), \
             patch("pbz.checks.engine_call", side_effect=fake_engine):
            res = checks.snapshot(self.ws, None, "save", "different", self._NEW_SPEC)
        self.assertEqual(res["queries"][0]["result"], "SAVED")
        self.assertEqual(json.loads(spec_path.read_text(encoding="utf-8")), self._NEW_SPEC)

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


class SnapshotPairPublicationTests(unittest.TestCase):
    _OLD_SPEC = SnapshotStoreTests._OLD_SPEC
    _NEW_SPEC = SnapshotStoreTests._NEW_SPEC
    _OLD_RESULT = SnapshotStoreTests._GOOD_RESULT
    _NEW_RESULT = _OLD_RESULT.replace(b'"good"', b'"new"')

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = make_workspace(tmp.name)
        _tiny(self.ws.root)
        self.private_root = Path(self.ws.private)
        wid = hashlib.sha256(guard.norm(os.path.abspath(self.ws.root)).encode()).hexdigest()[:16]
        self.ws.private = str(self.private_root / wid)
        Path(self.ws.private).mkdir()
        self.snaps = Path(self.ws.private) / "snapshots"
        self.result = self.snaps / "results" / "different.json"
        self.spec = self.snaps / "specs" / "different.json"
        self.pending = self.snaps / "pending"
        self.journal = self.pending / "different.journal.json"
        self.staged = self.pending / "different.spec.next"
        self.old_spec_bytes = json.dumps(self._OLD_SPEC).encode()
        self.new_spec_bytes = json.dumps(self._NEW_SPEC).encode()
        self.port = self.enterContext(patch("pbz.desktop.require_port", return_value=1))
        self.engine = self.enterContext(patch("pbz.checks.engine_call", side_effect=self._engine))

    def _seed_pair(self):
        for path, data in ((self.result, self._OLD_RESULT), (self.spec, self.old_spec_bytes)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def _engine(self, verb, payload, timeout=1800, **kwargs):
        if verb == "snapshot-save":
            self.result.parent.mkdir(parents=True, exist_ok=True)
            self.result.write_bytes(self._NEW_RESULT)
            verdict = "SAVED"
        else:
            dax = payload["queries"][0]["dax"]
            expected = "EVALUATE {2}" if self.result.read_bytes() == self._NEW_RESULT else "EVALUATE {1}"
            verdict = "MATCH" if dax == expected else "DRIFT"
        response = {"ok": True, "class": "OK", "queries": [{"id": "q1", "result": verdict}]}
        if verb == "snapshot-save":
            response["payload_state"] = "may_have_been_sent"
        return response

    def _save(self):
        return checks.snapshot(self.ws, None, "save", "different", self._NEW_SPEC)

    def _compare(self):
        return checks.snapshot(self.ws, None, "compare", "different", self._OLD_SPEC)

    def _public_child(self, action, interruption=None, **options):
        command, env = self._public_child_command(action, interruption, **options)
        return subprocess.run(
            command, cwd=self.ws.root, env=env, capture_output=True, text=True, timeout=20,
        )

    def _public_child_command(self, action, interruption=None, *, explicit=False,
                              engine_failure=None, spec=None, result=None):
        script = """
import os
from contextlib import ExitStack
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, sys.argv[1])
from test_snapshot_store import SnapshotPairPublicationTests as Suite
from pbz import cli

action, interruption, explicit, engine_failure, valid_spec, result = sys.argv[2:]
replace = os.replace

def interrupted_replace(src, dst):
    destination = Path(dst)
    if interruption == "before-spec" and destination.parent.name == "specs":
        os._exit(71)
    if interruption == "before-stage" and destination.name.endswith(".spec.next"):
        os._exit(71)
    replace(src, dst)
    if interruption == "after-stage" and destination.name.endswith(".spec.next"):
        os._exit(71)

def fake_engine(verb, payload, timeout=1800, **kwargs):
    if interruption == "hold-at-engine":
        print("ready", file=sys.stderr, flush=True)
        if not sys.stdin.readline():
            os._exit(72)
    if interruption == "watch-lock":
        print("dispatched", file=sys.stderr, flush=True)
    baseline = Path(payload["private_dir"]) / "snapshots" / "results" / (payload["name"] + ".json")
    if engine_failure:
        return {"ok": False, "class": engine_failure, "payload_state": "may_have_been_sent"}
    if verb == "snapshot-save":
        if interruption == "before-engine-write":
            os._exit(71)
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_bytes(result.encode() if result else Suite._NEW_RESULT)
        if interruption == "inside-engine":
            os._exit(71)
        verdict = "SAVED"
    else:
        if not baseline.is_file():
            baseline = baseline.parent.parent / baseline.name
        expected = "EVALUATE {2}" if baseline.read_bytes() == Suite._NEW_RESULT else "EVALUATE {1}"
        verdict = "MATCH" if payload["queries"][0]["dax"] == expected else "DRIFT"
    return {"ok": True, "class": "OK", "payload_state": "may_have_been_sent",
            "queries": [{"id": "q1", "result": verdict}]}

args = ["snapshot", action, "different"]
if action == "save" or explicit:
    args += ["--spec", valid_spec]
with ExitStack() as stack:
    stack.enter_context(patch("pbz.desktop.require_port", return_value=1))
    stack.enter_context(patch("pbz.evidence.record"))
    stack.enter_context(patch("pbz.checks.engine_call", side_effect=fake_engine))
    stack.enter_context(patch("pbz.checks.os.replace", side_effect=interrupted_replace))
    if interruption == "watch-lock":
        import fcntl
        lock = Path(os.environ["PBZ_TEST_PAIR_LOCK"])
        flock = fcntl.flock

        def observe_contention(fd, operation):
            if lock.exists() and os.path.samestat(os.fstat(fd), lock.stat()) and operation & fcntl.LOCK_EX:
                try:
                    return flock(fd, operation | fcntl.LOCK_NB)
                except BlockingIOError:
                    print("contended", file=sys.stderr, flush=True)
                    if operation & fcntl.LOCK_NB:
                        raise
            return flock(fd, operation)

        stack.enter_context(patch("fcntl.flock", side_effect=observe_contention))
    sys.exit(cli.main(args))
"""
        env = os.environ.copy()
        env["PBZ_PRIVATE_ROOT"] = str(self.private_root)
        env["PBZ_TEST_PAIR_LOCK"] = str(self.pending / "different.lock")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("PBZ_SKIP_OUTPUT", None)
        command = [
            sys.executable, "-I", "-c", script, str(Path(__file__).resolve().parent), action,
            interruption or "", str(int(explicit)) if explicit else "", engine_failure or "",
            json.dumps(spec or self._NEW_SPEC), result.decode() if result else "",
        ]
        return command, env

    def _assert_public_compare(self, verdict):
        proc = self._public_child("compare")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["queries"][0]["result"], verdict)

    def _assert_public_block(self):
        proc = self._public_child("compare")
        self.assertEqual(json.loads(proc.stdout).get("error"), "RECOVERY_REQUIRED", proc.stdout + proc.stderr)

    def _assert_class(self, code, operation):
        with self.assertRaises(PbzError) as ctx:
            operation()
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def _assert_completed(self):
        self.assertEqual(self.result.read_bytes(), self._NEW_RESULT)
        self.assertTrue(self.spec.is_file(), "recovery did not publish a spec")
        self.assertEqual(self.spec.read_bytes(), self.new_spec_bytes)
        self.assertFalse(self.journal.exists())
        self.assertFalse(self.staged.exists())

    def _write_journal(self, state="RESULT_PUBLISHED", **fields):
        self.pending.mkdir(parents=True, exist_ok=True)
        (self.pending / "different.lock").touch()
        self.staged.write_bytes(self.new_spec_bytes)
        doc = {
            "version": 1,
            "state": state,
            "before_result": hashlib.sha256(self._OLD_RESULT).hexdigest(),
            "before_spec": hashlib.sha256(self.old_spec_bytes).hexdigest(),
            "before_coherent": True,
            "staged_spec": hashlib.sha256(self.new_spec_bytes).hexdigest(),
            "new_result": hashlib.sha256(self._NEW_RESULT).hexdigest()
            if state == "RESULT_PUBLISHED" else None,
        }
        doc.update(fields)
        data = json.dumps(doc).encode()
        self.journal.write_bytes(data)
        return data

    def _interrupt_spec_replace(self, error):
        replace = os.replace

        def fault(src, dst):
            if Path(dst) == self.spec:
                raise error
            return replace(src, dst)

        with patch("pbz.checks.os.replace", side_effect=fault):
            try:
                self._save()
            except (OSError, PbzError, KeyboardInterrupt) as exc:
                return exc
        self.fail("save did not reach the spec replacement")

    def test_case_01_spec_replace_failure_recovers_pair(self):
        self._seed_pair()
        error = self._interrupt_spec_replace(OSError("disk"))
        was_published = self.journal.is_file() and json.loads(self.journal.read_bytes())["state"] == "RESULT_PUBLISHED"
        self._assert_public_compare("MATCH")
        self._assert_completed()
        self.assertTrue(was_published)
        self.assertEqual(error.code, "RECOVERY_REQUIRED")

    def test_case_02_fresh_public_compare_recovers_interrupted_save(self):
        self._seed_pair()
        child = self._public_child("save", "before-spec")
        self.assertEqual(child.returncode, 71, child.stdout + child.stderr)
        self.assertEqual(self.result.read_bytes(), self._NEW_RESULT)
        self.assertEqual(self.spec.read_bytes(), self.old_spec_bytes)
        self._assert_public_compare("MATCH")
        self._assert_completed()

    def test_case_03_exit_inside_engine_blocks_then_save_supersedes(self):
        self._seed_pair()
        child = self._public_child("save", "inside-engine")
        self.assertEqual(child.returncode, 71, child.stdout + child.stderr)
        pair = (self.result.read_bytes(), self.spec.read_bytes())
        self._assert_public_block()
        self.assertEqual((self.result.read_bytes(), self.spec.read_bytes()), pair)
        self._save()
        self._assert_completed()

    def test_case_04_all_failed_supersede_preserves_prepared(self):
        self._seed_pair()
        self.result.write_bytes(self._NEW_RESULT)
        journal = self._write_journal("PREPARED")
        pair = (self.result.read_bytes(), self.spec.read_bytes())
        with patch("pbz.checks.engine_call", return_value={
            "ok": True, "class": "OK", "payload_state": "may_have_been_sent",
            "queries": [{"id": "q1", "result": "ERROR", "class": "OTHER"}],
        }):
            response = self._save()
        self.assertEqual(response["queries"][0]["result"], "ERROR")
        self._assert_public_block()
        self.assertEqual((self.result.read_bytes(), self.spec.read_bytes()), pair)
        self.assertEqual(self.journal.read_bytes(), journal)
        self._save()
        self._assert_completed()

    def test_case_04_interrupted_supersede_keeps_original_journal(self):
        for state, interruption in (
            ("PREPARED", "before-engine-write"),
            ("PREPARED", "inside-engine"),
            ("RESULT_PUBLISHED", "inside-engine"),
        ):
            with self.subTest(state=state, interruption=interruption):
                self._seed_pair()
                self.result.write_bytes(self._NEW_RESULT)
                original = self._write_journal(state)
                self.staged.unlink()
                newest = self._NEW_RESULT + b" "
                proc = self._public_child("save", interruption, result=newest)
                self.assertEqual(proc.returncode, 71, proc.stdout + proc.stderr)
                self._assert_public_block()
                expected = self._NEW_RESULT if interruption == "before-engine-write" else newest
                self.assertEqual(self.result.read_bytes(), expected)
                self.assertEqual(self.spec.read_bytes(), self.old_spec_bytes)
                if state == "RESULT_PUBLISHED":
                    record = json.loads(self.journal.read_bytes())
                    self.assertTrue(record.pop("superseding"))
                    self.assertEqual(record, json.loads(original))
                else:
                    self.assertEqual(self.journal.read_bytes(), original)
                self._save()
                self._assert_completed()

    def test_case_04_interrupted_restage_checks_both_files(self):
        for staging, publication in (("different", "same"), ("same", "same"), ("same", "different")):
            with self.subTest(staging=staging, publication=publication):
                self._seed_pair()
                self.result.write_bytes(self._NEW_RESULT)
                # Keep the original journal blocking until the supersede stages.
                original = self._write_journal()
                self.staged.unlink()
                next_spec = self._NEW_SPEC if staging == "same" else {
                    "queries": [{"id": "q1", "dax": "EVALUATE {3}"}],
                }
                next_result = self._NEW_RESULT if publication == "same" else self._NEW_RESULT + b" "
                proc = self._public_child("save", "after-stage", spec=next_spec, result=next_result)
                # The base completes and changes the live spec because it never stages.
                self.assertEqual(self.spec.read_bytes(), self.old_spec_bytes)
                self.assertEqual(proc.returncode, 71, proc.stdout + proc.stderr)
                if staging == publication == "same":
                    self._assert_public_compare("MATCH")
                    self._assert_completed()
                else:
                    self._assert_public_block()
                    self.assertEqual(self.result.read_bytes(), next_result)
                    self.assertEqual(self.spec.read_bytes(), self.old_spec_bytes)
                    record = json.loads(self.journal.read_bytes())
                    self.assertEqual(record.pop("superseding", False), publication == "different")
                    self.assertEqual(record, json.loads(original))

    def test_case_05_engine_and_port_refusals_preserve_pair(self):
        self._seed_pair()
        before = _tree(self.snaps)
        self.engine.side_effect = None
        self.engine.return_value = {"ok": False, "class": "ENGINE_UNAVAILABLE", "payload_state": "not_sent"}
        self._assert_class("ENGINE_UNAVAILABLE", self._save)
        self.assertEqual(checks._load_state(self.ws)["snapshot_queries"], 1)
        self.assertEqual(self.spec.read_bytes(), self.old_spec_bytes)
        self.assertEqual(self.result.read_bytes(), self._OLD_RESULT)
        self.assertFalse(self.journal.exists())
        self.assertFalse(self.staged.exists())

        self.engine.reset_mock()
        self.port.side_effect = PbzError("ENGINE_UNAVAILABLE")
        self._assert_class("ENGINE_UNAVAILABLE", self._save)
        self.engine.assert_not_called()
        self.assertEqual(checks._load_state(self.ws)["snapshot_queries"], 1)
        self.assertEqual(tuple(row for row in _tree(self.snaps) if not row[0].startswith("pending")), before)
        self.assertFalse(self.journal.exists())
        self.assertFalse(self.staged.exists())

    @unittest.skipUnless(os.name == "nt", "Windows Job Object setup")
    def test_case_05_job_creation_failure_precedes_budget_charge(self):
        self._seed_pair()
        self.pending.mkdir()
        with patch("pbz.checks.create_kill_on_close_job", side_effect=PbzError("ENGINE_UNAVAILABLE")):
            self._assert_class("ENGINE_UNAVAILABLE", self._save)
        self.engine.assert_not_called()
        self.assertEqual(checks._load_state(self.ws)["snapshot_queries"], 0)
        self.assertEqual(self.result.read_bytes(), self._OLD_RESULT)
        self.assertEqual(self.spec.read_bytes(), self.old_spec_bytes)
        self.assertFalse(self.journal.exists())
        self.assertFalse(self.staged.exists())

    def test_case_06_second_recovery_is_idempotent(self):
        self._seed_pair()
        self._interrupt_spec_replace(OSError("disk"))
        self._assert_public_compare("MATCH")
        self._assert_completed()
        before = _tree(self.snaps)
        self._assert_public_compare("MATCH")
        self.assertEqual(_tree(self.snaps), before)

    def test_case_07_first_save_recovers_missing_spec(self):
        child = self._public_child("save", "before-spec")
        self.assertEqual(child.returncode, 71, child.stdout + child.stderr)
        self.assertEqual(self.result.read_bytes(), self._NEW_RESULT)
        self.assertFalse(self.spec.exists())
        self._assert_public_compare("MATCH")
        self._assert_completed()

    def test_case_08_external_edits_and_invalid_staged_bytes(self):
        for edited in (self.spec, self.result, self.staged):
            with self.subTest(path=edited.name, directory=edited.parent.name):
                self._seed_pair()
                self.result.write_bytes(self._NEW_RESULT)
                self._write_journal()
                data = self.new_spec_bytes + b" " if edited != self.result else self._NEW_RESULT + b" "
                edited.write_bytes(data)
                before = _tree(self.snaps)
                self._assert_class("RECOVERY_REQUIRED", self._compare)
                if edited != self.staged:
                    self._assert_class("RECOVERY_REQUIRED", self._save)
                self.assertEqual(_tree(self.snaps), before)

    def test_case_08_spec_edited_inside_successful_dispatch(self):
        self._seed_pair()
        foreign = self.old_spec_bytes + b" "

        def engine(*args, **kwargs):
            response = self._engine(*args, **kwargs)
            self.spec.write_bytes(foreign)
            return response

        self.engine.side_effect = engine
        self._assert_class("RECOVERY_REQUIRED", self._save)
        self.assertEqual(self.spec.read_bytes(), foreign)
        self.assertEqual(json.loads(self.journal.read_bytes())["state"], "RESULT_PUBLISHED")

    def test_case_08_readback_records_foreign_result_as_generation(self):
        self._seed_pair()
        foreign = self._NEW_RESULT + b" "

        def engine(*args, **kwargs):
            response = self._engine(*args, **kwargs)
            self.result.write_bytes(foreign)
            return response

        self.engine.side_effect = engine
        self._interrupt_spec_replace(OSError("record journal before installing spec"))
        self.assertTrue(self.journal.is_file())
        record = json.loads(self.journal.read_bytes())
        self.assertEqual(record["state"], "RESULT_PUBLISHED")
        self.assertEqual(record["new_result"], hashlib.sha256(foreign).hexdigest())
        self.assertEqual(record["staged_spec"], hashlib.sha256(self.new_spec_bytes).hexdigest())
        self._compare()
        self.assertEqual(self.result.read_bytes(), foreign)
        self.assertEqual(self.spec.read_bytes(), self.new_spec_bytes)
        self.assertFalse(self.journal.exists())

    def test_case_08_result_edited_immediately_after_spec_replace(self):
        self._seed_pair()
        replace = os.replace
        foreign = self._NEW_RESULT + b" "

        def edit_after_replace(src, dst):
            replace(src, dst)
            if Path(dst) == self.spec:
                self.result.write_bytes(foreign)

        with patch("os.replace", side_effect=edit_after_replace):
            self._assert_class("RECOVERY_REQUIRED", self._save)
        self.assertEqual(self.spec.read_bytes(), self.new_spec_bytes)
        self.assertEqual(self.result.read_bytes(), foreign)
        self.assertEqual(json.loads(self.journal.read_bytes())["state"], "RESULT_PUBLISHED")
        self._assert_public_block()

    def test_case_08_pair_moved_during_failed_dispatch(self):
        for path, original in ((self.result, self._OLD_RESULT), (self.spec, self.old_spec_bytes)):
            with self.subTest(path=path.parent.name):
                self.journal.unlink(missing_ok=True)
                self.staged.unlink(missing_ok=True)
                self._seed_pair()
                foreign = original + b" "

                def engine(*args, **kwargs):
                    path.write_bytes(foreign)
                    return {"ok": False, "class": "ENGINE_UNAVAILABLE", "payload_state": "may_have_been_sent"}

                self.engine.side_effect = engine
                error = self._assert_class("RECOVERY_REQUIRED", self._save)
                self.assertEqual(error.__cause__.code, "ENGINE_UNAVAILABLE")
                self.assertEqual(path.read_bytes(), foreign)
                self.assertEqual(json.loads(self.journal.read_bytes())["state"], "PREPARED")
                self._assert_class("RECOVERY_REQUIRED", self._compare)

    def test_case_09_incoherent_before_pair_cannot_be_used(self):
        self._seed_pair()
        journal = self._write_journal(before_coherent=False)
        before = (self.result.read_bytes(), self.spec.read_bytes())
        self._assert_class("RECOVERY_REQUIRED", self._compare)
        self.assertEqual((self.result.read_bytes(), self.spec.read_bytes()), before)
        self.assertEqual(self.journal.read_bytes(), journal)

    def test_case_12_public_compare_without_a_saved_or_explicit_spec(self):
        proc = self._public_child("compare")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        response = json.loads(proc.stdout)
        self.assertEqual(response["error"], "BAD_REQUEST")
        self.assertEqual(response["detail"], "--spec required")

    def test_case_12_public_compare_with_explicit_spec_and_no_saved_spec(self):
        self.result.parent.mkdir(parents=True)
        self.result.write_bytes(self._NEW_RESULT)
        proc = self._public_child("compare", explicit=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["queries"][0]["result"], "MATCH")
        self.assertFalse(self.spec.exists())

    def test_case_12_public_compare_with_only_legacy_result(self):
        self.snaps.mkdir()
        (self.snaps / self.result.name).write_bytes(self._NEW_RESULT)
        proc = self._public_child("compare", explicit=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["queries"][0]["result"], "MATCH")
        self.assertFalse(self.result.exists())

    def test_case_12_public_save_does_not_expose_payload_state(self):
        for failure in (None, "TIMEOUT"):
            with self.subTest(failure=failure):
                proc = self._public_child("save", engine_failure=failure)
                response = json.loads(proc.stdout)
                if failure:
                    self.assertEqual(response.get("error"), failure)
                else:
                    self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertNotIn("payload_state", response)

    def test_case_14_supersede_revalidates_pair_after_port(self):
        self._seed_pair()
        self.result.write_bytes(self._NEW_RESULT)
        journal = self._write_journal("PREPARED")
        foreign = self.old_spec_bytes + b" "

        def edit_at_port(project):
            self.spec.write_bytes(foreign)
            return 1

        with patch("pbz.desktop.require_port", side_effect=edit_at_port):
            self._assert_class("RECOVERY_REQUIRED", self._save)
        self.engine.assert_not_called()
        self.assertEqual(checks._load_state(self.ws)["snapshot_queries"], 0)
        self.assertEqual(self.spec.read_bytes(), foreign)
        self.assertEqual(self.journal.read_bytes(), journal)

    def test_case_14_superseding_refusals_preserve_original_journal(self):
        for state in ("PREPARED", "RESULT_PUBLISHED"):
            for refusal in ("port", "budget", "engine"):
                with self.subTest(state=state, refusal=refusal):
                    self._seed_pair()
                    self.result.write_bytes(self._NEW_RESULT)
                    journal = self._write_journal(state)
                    self.staged.unlink()
                    before = _tree(self.snaps)
                    if refusal == "budget":
                        with patch("pbz.checks.spend", return_value=False):
                            self.assertEqual(self._save()["status"], "BUDGET")
                    elif refusal == "port":
                        with patch("pbz.desktop.require_port", side_effect=PbzError("ENGINE_UNAVAILABLE")):
                            self._assert_class("ENGINE_UNAVAILABLE", self._save)
                    else:
                        with patch("pbz.checks.engine_call", return_value={
                            "ok": False, "class": "ENGINE_UNAVAILABLE", "payload_state": "not_sent",
                        }):
                            self._assert_class("ENGINE_UNAVAILABLE", self._save)
                    self._assert_public_block()
                    self.assertEqual(_tree(self.snaps), before)
                    self.assertEqual(self.journal.read_bytes(), journal)

    def test_case_15_malformed_journal_refuses_save_and_compare(self):
        self._seed_pair()
        for journal in (b"{", b'{"version":2}'):
            for operation in (self._save, self._compare):
                with self.subTest(journal=journal, action=operation.__name__):
                    self._seed_pair()
                    self.pending.mkdir(exist_ok=True)
                    (self.pending / "different.lock").touch()
                    self.journal.write_bytes(journal)
                    self.engine.reset_mock()
                    before = _tree(self.snaps)
                    self._assert_class("CORRUPT_STATE", operation)
                    self.assertEqual(_tree(self.snaps), before)
                    self.engine.assert_not_called()

    def test_case_15_prepared_with_unknown_spec_refuses_save(self):
        self._seed_pair()
        self._write_journal("PREPARED")
        self.spec.write_bytes(self.old_spec_bytes + b" ")
        before = _tree(self.snaps)
        self._assert_class("RECOVERY_REQUIRED", self._save)
        self.assertEqual(_tree(self.snaps), before)
        self.engine.assert_not_called()

    def test_case_16_absent_pair_directories_allow_cleanup(self):
        with patch("pbz.checks.spend", return_value=False):
            self.assertEqual(self._save()["status"], "BUDGET")
        self.engine.assert_not_called()
        self.assertFalse(self.journal.exists())
        self.assertFalse(self.staged.exists())

        interrupted = self._start_snapshot_script(
            '    stack.enter_context(patch("pbz.checks.spend", side_effect=lambda *args: os._exit(71)))'
        )
        _, stderr = interrupted.communicate(timeout=10)
        self.assertEqual(interrupted.returncode, 71, stderr)
        self.assertTrue(self.journal.exists())
        completed = self._public_child("compare")
        self.assertEqual(json.loads(completed.stdout)["error"], "BAD_REQUEST")
        self.assertFalse(self.journal.exists())
        self.assertFalse(self.staged.exists())
        self.assertFalse(self.result.parent.exists())
        self.assertFalse(self.spec.parent.exists())

    def test_case_18_supersede_prepared_foreign_result(self):
        self._seed_pair()
        child = self._public_child("save", "inside-engine")
        self.assertEqual(child.returncode, 71, child.stdout + child.stderr)
        self.result.write_bytes(self._OLD_RESULT + b" ")
        proc = self._public_child("compare")
        with self.subTest("governing journal"):
            self.assertTrue(self.journal.is_file())
        with self.subTest("omitted-spec compare"):
            self.assertEqual(json.loads(proc.stdout).get("error"), "RECOVERY_REQUIRED")
        self._save()
        self._assert_completed()

    def _overwrite_during_dispatch(self, expected_state):
        def engine(*args, **kwargs):
            self.assertTrue(self.journal.is_file(), "no journal at engine dispatch")
            journal = json.loads(self.journal.read_bytes())
            self.assertEqual(journal["state"], expected_state)
            if expected_state == "RESULT_PUBLISHED":
                self.assertTrue(journal.get("superseding"))
            self.result.write_bytes(self._OLD_RESULT + b" ")
            return self._engine(*args, **kwargs)

        self.engine.side_effect = engine
        self._save()
        self._assert_completed()

    def test_case_19_fresh_save_overwrites_edit_inside_dispatch(self):
        self._seed_pair()
        self._overwrite_during_dispatch("PREPARED")

    def test_case_20_supersede_overwrites_edit_inside_dispatch(self):
        self._seed_pair()
        self.result.write_bytes(self._NEW_RESULT)
        self._write_journal()
        self.staged.unlink()
        self._overwrite_during_dispatch("RESULT_PUBLISHED")

    def test_case_21_compare_preflight_rejects_malformed_specs(self):
        self.result.parent.mkdir(parents=True)
        self.result.write_bytes(self._OLD_RESULT)
        self.spec.parent.write_bytes(b"not a directory")
        before = _tree(self.snaps)
        self._assert_class("CORRUPT_STATE", lambda: checks.snapshot(
            self.ws, None, "compare", "different", self._OLD_SPEC,
        ))
        self.assertEqual(_tree(self.snaps), before)
        self.port.assert_not_called()
        self.engine.assert_not_called()

    def _assert_pending_refused(self, code):
        before = _tree(self.snaps)
        for operation in (self._save, self._compare):
            with self.subTest(action=operation.__name__):
                self.engine.reset_mock()
                self._assert_class(code, operation)
                self.engine.assert_not_called()
                self.assertEqual(checks._load_state(self.ws)["snapshot_queries"], 0)
                self.assertEqual(_tree(self.snaps), before)

    def test_case_23_pending_directory_is_a_file(self):
        self._seed_pair()
        self.pending.write_bytes(b"not a directory")
        self._assert_pending_refused("CORRUPT_STATE")

    def test_case_23_pending_journal_is_a_directory(self):
        self._seed_pair()
        self.journal.mkdir(parents=True)
        self._assert_pending_refused("CORRUPT_STATE")

    def test_case_23_pending_directory_link(self):
        from _junction import make_junction
        self._seed_pair()
        outside = self.snaps.parent / "outside-pending"
        outside.mkdir()
        if os.name == "nt":
            make_junction(self, str(self.pending), str(outside))
        else:
            self.pending.symlink_to(outside, target_is_directory=True)
        self._assert_pending_refused("UNSAFE_PATH")
        self.assertEqual(list(outside.iterdir()), [])

    def test_case_23_pending_journal_link(self):
        self._seed_pair()
        self.pending.mkdir()
        outside = self.snaps.parent / "outside-journal"
        outside.write_bytes(b"target survives")
        try:
            self.journal.symlink_to(outside)
        except OSError as exc:
            if os.name != "nt":
                raise
            self.skipTest(f"creating a file symlink is unavailable: {exc}")
        self._assert_pending_refused("UNSAFE_PATH")
        self.assertEqual(outside.read_bytes(), b"target survives")

    def test_case_29_interrupt_at_port_preserves_annotations(self):
        self._seed_pair()
        self.result.write_bytes(self._NEW_RESULT + b" ")
        journal = self._write_journal(superseding=True)
        with patch("pbz.desktop.require_port", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self._save()
        self.assertEqual(self.journal.read_bytes(), journal)
        self._assert_public_block()

    def test_case_29_interrupt_during_completion_leaves_journal(self):
        self._seed_pair()
        error = self._interrupt_spec_replace(KeyboardInterrupt())
        self.assertIsInstance(error, KeyboardInterrupt)
        self.assertTrue(self.journal.is_file())
        self.assertEqual(json.loads(self.journal.read_bytes())["state"], "RESULT_PUBLISHED")
        self._assert_public_compare("MATCH")
        self._assert_completed()


    @unittest.skipUnless(os.name == "posix", "POSIX flock observer")
    def test_case_10_same_name_saves_wait_for_compare_and_other_saves(self):
        for first in ("compare", "save"):
            with self.subTest(first=first):
                self._seed_pair()
                first_process = self._start_public_child(first, "hold-at-engine")
                try:
                    self.assertEqual(self._child_signal(first_process), "ready")
                    newest = self._NEW_RESULT + b" "
                    second = self._start_public_child("save", "watch-lock", result=newest)
                    try:
                        signal = self._child_signal(second)
                        self.assertEqual(signal, "contended")
                        self.assertEqual(self.result.read_bytes(), self._OLD_RESULT)
                        self.assertEqual(self.spec.read_bytes(), self.old_spec_bytes)
                        out, err = first_process.communicate("\n", timeout=10)
                        self.assertEqual(first_process.returncode, 0, out + err)
                        out, err = second.communicate(timeout=10)
                        self.assertEqual(second.returncode, 0, out + err)
                        self.assertEqual(self.result.read_bytes(), newest)
                        self.assertEqual(self.spec.read_bytes(), self.new_spec_bytes)
                        self.assertFalse(self.journal.exists())
                    finally:
                        self._stop_child(second)
                finally:
                    self._stop_child(first_process)

    def _start_public_child(self, action, interruption, **options):
        command, env = self._public_child_command(action, interruption, **options)
        return subprocess.Popen(
            command, cwd=self.ws.root, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def _child_signal(self, child):
        from queue import Empty, Queue
        from threading import Thread

        lines = Queue()
        Thread(target=lambda: lines.put(child.stderr.readline()), daemon=True).start()
        try:
            return lines.get(timeout=10).strip()
        except Empty:
            self.fail("child did not report lock or engine progress")

    @staticmethod
    def _stop_child(child):
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)

    @unittest.skipUnless(os.name == "posix", "POSIX lock open")
    def test_case_10_denied_lock_open_refuses_reader(self):
        import builtins
        import errno

        self._seed_pair()
        lock = self.pending / "different.lock"
        open_file = builtins.open
        open_fd = os.open

        def deny(opener, path, *args, **kwargs):
            if not isinstance(path, int) and os.path.normcase(os.fspath(path)) == os.path.normcase(str(lock)):
                raise PermissionError(errno.EACCES, "denied lock open", path)
            return opener(path, *args, **kwargs)

        for pending, expected in ((False, "LOCAL_STATE_UNAVAILABLE"), (True, "RECOVERY_REQUIRED")):
            with self.subTest(pending=pending):
                if pending:
                    self._write_journal("PREPARED")
                self.engine.reset_mock()
                with patch("builtins.open", side_effect=lambda *a, **kw: deny(open_file, *a, **kw)), \
                     patch("os.open", side_effect=lambda *a, **kw: deny(open_fd, *a, **kw)):
                    self._assert_class(expected, self._compare)
                self.engine.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX flock failure")
    def test_case_10_flock_enolck_falls_through_both_open_modes(self):
        import errno
        import fcntl

        self._seed_pair()
        self._write_journal("PREPARED")
        lock_info = (self.pending / "different.lock").stat()
        real_flock = fcntl.flock
        attempts = []

        def fail_lock(fd, operation):
            if os.path.samestat(os.fstat(fd), lock_info):
                attempts.append(operation)
                raise OSError(errno.ENOLCK, "flock unavailable")
            return real_flock(fd, operation)

        with patch("fcntl.flock", side_effect=fail_lock):
            self._assert_class("RECOVERY_REQUIRED", self._compare)
        self.assertEqual(len(attempts), 2)
        self.engine.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX file descriptor inheritance")
    def test_case_11_posix_timed_out_child_is_reaped(self):
        from pbz import engine

        with tempfile.TemporaryFile() as inherited:
            self._assert_timed_out_child_reaped(engine, pass_fds=(inherited.fileno(),))

    @unittest.skipUnless(os.name == "nt", "Windows handle inheritance")
    def test_case_11_windows_timed_out_child_is_reaped(self):
        from pbz import engine

        import msvcrt

        with tempfile.TemporaryFile() as inherited:
            handle = msvcrt.get_osfhandle(inherited.fileno())
            self._assert_timed_out_child_reaped(engine, inherit_handles=(handle,))

    def _assert_timed_out_child_reaped(self, engine, **call_options):
        popen = subprocess.Popen
        children = []

        def stub(*args, **kwargs):
            child = popen([sys.executable, "-c", "import threading; threading.Event().wait()"], **kwargs)
            children.append(child)

            def reap():
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

            self.addCleanup(reap)
            return child

        request = {"private_dir": self.ws.private, "port": 1, "name": "different",
                   "queries": [{"id": "q1", "dax": "EVALUATE {2}", "keys": []}]}
        with patch("pbz.engine.ENGINE_EXE", sys.executable), \
             patch("pbz.engine.subprocess.Popen", side_effect=stub):
            response = engine.call("snapshot-save", request, timeout=0.1, **call_options)
        self.assertEqual(response["class"], "TIMEOUT")
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())

    @unittest.skipUnless(os.name == "posix", "POSIX directory fsync")
    def test_case_13_failed_specs_sync_retains_journal(self):
        self._seed_pair()
        self.result.write_bytes(self._NEW_RESULT)
        journal = self._write_journal()
        sync = os.fsync
        specs_stat = self.spec.parent.stat()

        def fail_specs(fd):
            if os.path.samestat(os.fstat(fd), specs_stat):
                raise OSError("specs directory sync failed")
            return sync(fd)

        with patch("os.fsync", side_effect=fail_specs):
            self._assert_class("RECOVERY_REQUIRED", self._compare)
            self.assertEqual(self.journal.read_bytes(), journal)
            self._assert_class("RECOVERY_REQUIRED", self._compare)
            self.assertEqual(self.journal.read_bytes(), journal)
        self._assert_public_compare("MATCH")
        self._assert_completed()

    @unittest.skipUnless(os.name == "posix", "POSIX journal durability")
    def test_case_13_staged_unlink_failure_leaves_ignored_orphan(self):
        self._seed_pair()
        self.result.write_bytes(self._NEW_RESULT)
        self._write_journal()
        unlink = os.unlink
        replace = os.replace
        unlinks = []
        spec_replaces = []

        def fail_stage_unlink(path, *args, **kwargs):
            if Path(path) in (self.journal, self.staged):
                unlinks.append(Path(path))
            if Path(path) == self.staged:
                raise PermissionError("staged unlink denied")
            return unlink(path, *args, **kwargs)

        def record_replace(src, dst):
            if Path(dst) == self.spec:
                spec_replaces.append(Path(dst))
            return replace(src, dst)

        with patch("os.unlink", side_effect=fail_stage_unlink), \
             patch("os.replace", side_effect=record_replace):
            self._compare()
            self.assertEqual(self.spec.read_bytes(), self.new_spec_bytes)
            self.assertEqual(unlinks, [self.journal, self.staged])
            self.assertEqual(self.staged.read_bytes(), self.new_spec_bytes)
            self.assertFalse(self.journal.exists())
            self._compare()
        self.assertEqual(spec_replaces, [self.spec])
        self.assertEqual(self.result.read_bytes(), self._NEW_RESULT)
        self.assertEqual(self.staged.read_bytes(), self.new_spec_bytes)

    def test_case_10_denied_journal_unlink_does_not_republish_spec(self):
        self._seed_pair()
        self.result.write_bytes(self._NEW_RESULT)
        journal = self._write_journal()
        unlink = os.unlink
        replace = os.replace
        spec_replaces = []

        def deny_unlink(path, *args, **kwargs):
            if Path(path) == self.journal:
                raise PermissionError("journal unlink denied")
            return unlink(path, *args, **kwargs)

        def record_replace(src, dst):
            if Path(dst) == self.spec:
                spec_replaces.append(Path(dst))
            return replace(src, dst)

        with patch("os.replace", side_effect=record_replace):
            with patch("os.unlink", side_effect=deny_unlink):
                self._assert_class("RECOVERY_REQUIRED", self._compare)
            self.assertEqual(self.result.read_bytes(), self._NEW_RESULT)
            self.assertEqual(self.spec.read_bytes(), self.new_spec_bytes)
            self.assertEqual(self.journal.read_bytes(), journal)
            self._compare()
        self._assert_completed()
        self.assertEqual(spec_replaces, [self.spec])

    @unittest.skipUnless(os.name == "posix", "POSIX directory fsync")
    def test_case_17_result_directory_entry_synced_before_advancing_journal(self):
        self.spec.parent.mkdir(parents=True)
        self.spec.write_bytes(self.old_spec_bytes)
        snapshots_stat = self.snaps.stat()
        real_sync = os.fsync
        engine_returned = False
        post_publication_syncs = []

        def engine(*args, **kwargs):
            nonlocal engine_returned
            response = self._engine(*args, **kwargs)
            engine_returned = True
            return response

        def fail_parent(fd):
            if engine_returned and os.path.samestat(os.fstat(fd), snapshots_stat):
                post_publication_syncs.append(fd)
                raise OSError("snapshot directory sync failed")
            return real_sync(fd)

        self.engine.side_effect = engine
        with patch("os.fsync", side_effect=fail_parent):
            self._assert_class("RECOVERY_REQUIRED", self._save)
        self.assertTrue(post_publication_syncs)
        self.assertEqual(json.loads(self.journal.read_bytes())["state"], "PREPARED")

    def test_case_22_pre_and_post_spend_revalidation_preserve_edits(self):
        for after_spend in (False, True):
            for changed in (self.result, self.spec):
                with self.subTest(after_spend=after_spend, changed=changed.parent.name):
                    self._seed_pair()
                    self.journal.unlink(missing_ok=True)
                    self.staged.unlink(missing_ok=True)
                    self.engine.reset_mock()
                    before = changed.read_bytes()
                    foreign = before + b" "
                    spend = checks.spend
                    replace = os.replace
                    spent = checks._load_state(self.ws)["snapshot_queries"]

                    def edit_after_spend(*args):
                        allowed = spend(*args)
                        if after_spend:
                            changed.write_bytes(foreign)
                        return allowed

                    def edit_after_prepare(src, dst):
                        replace(src, dst)
                        if not after_spend and Path(dst) == self.journal:
                            if json.loads(self.journal.read_bytes())["state"] == "PREPARED":
                                changed.write_bytes(foreign)

                    with patch("pbz.checks.spend", side_effect=edit_after_spend), \
                         patch("os.replace", side_effect=edit_after_prepare):
                        self._assert_class("RECOVERY_REQUIRED", self._save)
                    self.engine.assert_not_called()
                    self.assertEqual(checks._load_state(self.ws)["snapshot_queries"], spent + int(after_spend))
                    self.assertEqual(changed.read_bytes(), foreign)
                    journal = json.loads(self.journal.read_bytes())
                    self.assertEqual(journal["state"], "PREPARED")
                    self.assertEqual(bool(journal.get("result_foreign")), changed == self.result)
                    self._assert_public_block()
                    if changed == self.result:
                        self._assert_class("RECOVERY_REQUIRED", self._save)
                        self.assertEqual(changed.read_bytes(), foreign)
                    changed.write_bytes(before)
                    self._assert_public_compare("MATCH")
                    self.assertFalse(self.journal.exists())

    def test_case_24_known_result_clears_flag_even_with_unknown_spec(self):
        self._seed_pair()
        self.result.write_bytes(self._NEW_RESULT)
        cleared = self._write_journal()
        self._write_journal(superseding=True)
        self.spec.write_bytes(self.old_spec_bytes + b" ")
        self._assert_public_block()
        self.assertEqual(self.journal.read_bytes(), cleared)
        self.spec.write_bytes(self.old_spec_bytes)
        self.result.write_bytes(self._NEW_RESULT + b" ")
        self._assert_class("RECOVERY_REQUIRED", self._save)
        self.engine.assert_not_called()

    def test_case_25_moved_result_refuses_two_superseding_saves(self):
        for state, flagged, after_spend in (
            ("PREPARED", False, False),
            ("RESULT_PUBLISHED", False, False),
            ("RESULT_PUBLISHED", False, True),
            ("RESULT_PUBLISHED", True, False),
            ("RESULT_PUBLISHED", True, True),
        ):
            with self.subTest(state=state, flagged=flagged, after_spend=after_spend):
                self._seed_pair()
                initial = self._NEW_RESULT + b" " if flagged else self._NEW_RESULT
                self.result.write_bytes(initial)
                annotations = {"superseding": True} if flagged else {}
                original = json.loads(self._write_journal(state, **annotations))
                self.staged.unlink()
                foreign = self._NEW_RESULT + b"  "
                self.engine.reset_mock()
                spend = checks.spend

                def edit_at_port(project):
                    if not after_spend:
                        self.result.write_bytes(foreign)
                    return 1

                def edit_after_spend(*args):
                    allowed = spend(*args)
                    if after_spend:
                        self.result.write_bytes(foreign)
                    return allowed

                with patch("pbz.desktop.require_port", side_effect=edit_at_port), \
                     patch("pbz.checks.spend", side_effect=edit_after_spend):
                    self._assert_class("RECOVERY_REQUIRED", self._save)
                journal = self.journal.read_bytes()
                marked = json.loads(journal)
                self.assertTrue(marked.pop("result_foreign"))
                self.assertEqual(marked, original)
                self._assert_class("RECOVERY_REQUIRED", self._save)
                self.engine.assert_not_called()
                self.assertEqual(self.result.read_bytes(), foreign)
                self.assertEqual(self.journal.read_bytes(), journal)

    def test_case_26_flag_exit_distinguishes_payload_delivery(self):
        for delivery in ("not_sent", "may_have_been_sent"):
            with self.subTest(delivery=delivery):
                self._seed_pair()
                self.result.write_bytes(self._NEW_RESULT)
                journal = self._write_journal()
                self.staged.unlink()
                foreign = self._NEW_RESULT + b" "

                def fail_engine(*args, **kwargs):
                    self.result.write_bytes(foreign)
                    return {"ok": False, "class": "ENGINE_UNAVAILABLE" if delivery == "not_sent" else "TIMEOUT",
                            "payload_state": delivery}

                self.engine.side_effect = fail_engine
                if delivery == "not_sent":
                    self._assert_class("RECOVERY_REQUIRED", self._save)
                    self.assertEqual(self.journal.read_bytes(), journal)
                    self.engine.reset_mock()
                    self._assert_class("RECOVERY_REQUIRED", self._save)
                    self.engine.assert_not_called()
                else:
                    self._assert_class("TIMEOUT", self._save)
                    self.assertTrue(json.loads(self.journal.read_bytes()).get("superseding"))
                self.assertEqual(self.result.read_bytes(), foreign)
                self._assert_public_block()

    def test_case_27_restored_result_clears_marker_before_supersede(self):
        self._seed_pair()
        self.result.write_bytes(self._NEW_RESULT)
        journal = self._write_journal()
        self._write_journal(result_foreign=True)
        self.staged.unlink()
        self._assert_public_block()
        self.assertEqual(self.journal.read_bytes(), journal)
        child = self._public_child("save", "inside-engine", result=self._NEW_RESULT + b" ")
        self.assertEqual(child.returncode, 71, child.stdout + child.stderr)
        self._assert_public_block()
        self._save()
        self._assert_completed()

    def test_case_28_port_and_budget_refusal_after_external_result_edit(self):
        for refusal in ("port", "budget"):
            with self.subTest(refusal=refusal):
                self._seed_pair()
                self.result.write_bytes(self._NEW_RESULT)
                self._write_journal("PREPARED")
                foreign = self._NEW_RESULT + b" "

                def edit_and_refuse(*args):
                    self.result.write_bytes(foreign)
                    if refusal == "port":
                        raise PbzError("ENGINE_UNAVAILABLE")
                    return False

                target = "pbz.desktop.require_port" if refusal == "port" else "pbz.checks.spend"
                with patch(target, side_effect=edit_and_refuse):
                    self._assert_class("RECOVERY_REQUIRED", self._save)
                self.assertTrue(json.loads(self.journal.read_bytes()).get("result_foreign"))
                self._assert_class("RECOVERY_REQUIRED", self._save)
                self.engine.assert_not_called()
                self.assertEqual(self.result.read_bytes(), foreign)

    @unittest.skipUnless(os.name == "posix", "POSIX child cleanup")
    def test_case_30_unconfirmed_timeout_retains_prepared_journal(self):
        self._assert_native_timeout_kill_retry(superseding=False, fail_twice=True)

    def test_case_31_payload_state_on_engine_return_paths(self):
        from pbz import common, engine
        from unittest.mock import Mock

        options = {"pass_fds": (1,)}
        verb = "snapshot-save"
        child = Mock()
        with patch.object(engine, "ENGINE_EXE", sys.executable), \
             patch.object(engine.subprocess, "Popen", return_value=child) as start:
            for queries in ([{"id": "q1", "result": "SAVED"}], []):
                raw = {"ok": True, "verb": verb, "name": "different", "queries": queries,
                       "payload_state": "spoofed"}
                child.communicate.return_value = json.dumps(raw).encode(), None
                response = engine.call(verb, {}, **options)
                self.assertTrue(response["ok"])
                self.assertEqual(response["queries"], queries)
                self.assertEqual(response["payload_state"], "may_have_been_sent")
            child.communicate.side_effect = subprocess.TimeoutExpired(verb, 1)
            response = engine.call(verb, {}, **options)
            self.assertEqual((response["class"], response["payload_state"]),
                             ("TIMEOUT", "may_have_been_sent"))

            start.side_effect = OSError("spawn failure")
            response = engine.call(verb, {}, **options)
            self.assertEqual((response["class"], response["payload_state"]),
                             ("ENGINE_UNAVAILABLE", "not_sent"))
            start.reset_mock()
            response = engine.call(verb, {"bad": object()}, **options)
            self.assertEqual((response["class"], response["payload_state"]), ("BAD_REQUEST", "not_sent"))
            start.assert_not_called()

        with patch.object(engine, "ENGINE_EXE", sys.executable), \
             patch.object(engine.ctypes, "WinDLL", create=True, side_effect=OSError("setup failure")), \
             patch.object(engine.subprocess, "Popen") as start:
            response = engine.call(verb, {}, inherit_handles=(1,))
        self.assertEqual((response["class"], response["payload_state"]), ("OTHER", "not_sent"))
        start.assert_not_called()

        missing = Path(self.ws.root) / "missing-engine"
        with patch.object(engine, "ENGINE_EXE", missing):
            response = engine.call(verb, {}, **options)
        self.assertEqual((response["class"], response["payload_state"]), ("ENGINE_UNAVAILABLE", "not_sent"))

        real_import = __import__

        def fail_engine_import(name, globals=None, locals=None, fromlist=(), level=0):
            if level == 1 and "engine" in fromlist:
                raise ImportError("injected engine import failure")
            return real_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=fail_engine_import):
            response = common.engine_call(verb, {}, **options)
        self.assertEqual((response["class"], response["payload_state"]), ("ENGINE_UNAVAILABLE", "not_sent"))

    def test_case_32_fresh_stage_hard_link_is_safe_across_interruption(self):
        for interruption in ("before-stage", "after-stage"):
            with self.subTest(interruption=interruption):
                self._seed_pair()
                self.pending.mkdir(exist_ok=True)
                self.staged.unlink(missing_ok=True)
                os.link(self.spec, self.staged)
                proc = self._public_child("save", interruption)
                self.assertEqual(self.spec.read_bytes(), self.old_spec_bytes)
                self.assertEqual(proc.returncode, 71, proc.stdout + proc.stderr)
                self.assertEqual(self.result.read_bytes(), self._OLD_RESULT)
                self.assertFalse(self.journal.exists())
                if interruption == "after-stage":
                    self.assertFalse(os.path.samefile(self.spec, self.staged))

    def test_case_32_supersede_stage_replaces_hard_link_without_rewriting_spec(self):
        self._seed_pair()
        self.result.write_bytes(self._NEW_RESULT)
        journal = self._write_journal("PREPARED")
        self.staged.unlink()
        os.link(self.spec, self.staged)
        newest = self._NEW_RESULT + b" "
        proc = self._public_child("save", "after-stage", result=newest)
        self.assertEqual(self.spec.read_bytes(), self.old_spec_bytes)
        self.assertEqual(proc.returncode, 71, proc.stdout + proc.stderr)
        self.assertEqual(self.result.read_bytes(), newest)
        self.assertFalse(os.path.samefile(self.spec, self.staged))
        self.assertEqual(self.journal.read_bytes(), journal)

    def test_case_33_recovery_of_a_preserves_a_journal_files(self):
        self._seed_pair()
        self.result.write_bytes(self._NEW_RESULT)
        self._write_journal()
        for path in (self.result, self.spec, self.journal, self.staged, self.pending / "different.lock"):
            path.rename(path.with_name(path.name.replace("different", "a", 1)))
        other = self.pending / "a.journal.journal.json"
        other.write_bytes(b"{")
        other_staged = self.pending / "a.journal.spec.next"
        other_staged.write_bytes(b"blocking name")
        other_lock = self.pending / "a.journal.lock"
        other_lock.touch()
        leftover = self.pending / "a.journal.leftover"
        leftover.write_bytes(b"ignored")
        before = {path: path.read_bytes() for path in (other, other_staged, other_lock, leftover)}
        with patch("pbz.checks.engine_call", return_value={"ok": True, "class": "OK", "queries": []}):
            checks.snapshot(self.ws, None, "compare", "a", self._NEW_SPEC)
        self.assertEqual((self.spec.parent / "a.json").read_bytes(), self.new_spec_bytes)
        self.assertFalse((self.pending / "a.journal.json").exists())
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    @unittest.skipUnless(os.name == "posix", "POSIX shell drain probe")
    def test_case_33_posix_drain_probe_excludes_contender(self):
        import shutil

        if not shutil.which("flock"):
            self.skipTest("the documented drain probe requires flock")
        self._seed_pair()
        child = self._public_child("save", "inside-engine")
        self.assertEqual(child.returncode, 71, child.stdout + child.stderr)
        lock = self.pending / "different.lock"
        # Keep the documented descriptor open in the shell until stdin closes.
        probe = 'exec 9<"$1" && flock -n 9'
        with subprocess.Popen(
            ["sh", "-c", probe + ' && { printf "held\n"; read release; }', "drain", str(lock)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as holder:
            ready = holder.stdout.readline()
            try:
                self.assertEqual(ready, "held\n")
                contender = subprocess.run(
                    ["sh", "-c", probe, "drain", str(lock)], capture_output=True, timeout=5,
                )
                self.assertNotEqual(contender.returncode, 0)
            finally:
                holder.communicate("\n", timeout=5)
        contender = subprocess.run(
            ["sh", "-c", probe, "drain", str(lock)], capture_output=True, timeout=5,
        )
        self.assertEqual(contender.returncode, 0, contender.stderr)

    def test_case_34_superseding_save_cannot_stage_after_result_publication(self):
        import errno

        mkstemp = tempfile.mkstemp
        replace = os.replace
        for state, fault, identical in (
            ("PREPARED", "mkstemp", False),
            ("RESULT_PUBLISHED", "mkstemp", False),
            ("RESULT_PUBLISHED", "mkstemp", True),
            ("PREPARED", "replace", False),
            ("RESULT_PUBLISHED", "replace", False),
        ):
            with self.subTest(state=state, fault=fault, identical=identical):
                self._seed_pair()
                self.result.write_bytes(self._NEW_RESULT)
                journal = self._write_journal(state)
                self.staged.unlink()
                newest = self._NEW_RESULT if identical else self._NEW_RESULT + b" "

                def engine(*args, **kwargs):
                    response = self._engine(*args, **kwargs)
                    self.result.write_bytes(newest)
                    return response

                def fail_stage(*args, **kwargs):
                    if fault == "mkstemp" and kwargs.get("prefix", "").startswith("different.spec."):
                        raise OSError(errno.ENOSPC, "staging full")
                    return mkstemp(*args, **kwargs)

                def fail_replace(src, dst):
                    if fault == "replace" and Path(dst) == self.staged:
                        raise OSError(errno.ENOSPC, "cannot replace staged spec")
                    return replace(src, dst)

                self.engine.side_effect = engine
                spent = checks._load_state(self.ws)["snapshot_queries"]
                with patch("tempfile.mkstemp", side_effect=fail_stage), \
                     patch("os.replace", side_effect=fail_replace):
                    error = self._assert_class("RECOVERY_REQUIRED", self._save)
                self.assertIsInstance(error.__cause__, OSError)
                self.assertEqual(checks._load_state(self.ws)["snapshot_queries"], spent + 1)
                self.assertEqual(self.spec.read_bytes(), self.old_spec_bytes)
                self.assertEqual(self.result.read_bytes(), newest)
                self.assertEqual(list(self.pending.glob("different.spec.*")), [])
                if state == "PREPARED" or identical:
                    self.assertEqual(self.journal.read_bytes(), journal)
                else:
                    flagged = json.loads(self.journal.read_bytes())
                    self.assertTrue(flagged.pop("superseding"))
                    self.assertEqual(flagged, json.loads(journal))
                self._assert_public_block()


    def _blocked_publication(self, state="RESULT_PUBLISHED", **annotations):
        self._seed_pair()
        self.result.write_bytes(self._NEW_RESULT)
        journal = self._write_journal(state, **annotations)
        self.staged.unlink()
        return journal

    def _assert_annotations(self, original, **annotations):
        document = json.loads(original)
        document.update(annotations)
        self.assertEqual(json.loads(self.journal.read_bytes()), document)

    def test_case_04_superseding_timeout_with_moved_file(self):
        for state, moved in (("PREPARED", self.result), ("RESULT_PUBLISHED", self.result),
                             ("RESULT_PUBLISHED", self.spec)):
            with self.subTest(state=state, moved=moved.name, directory=moved.parent.name):
                journal = self._blocked_publication(state)
                foreign = moved.read_bytes() + b" "
                other = self.spec if moved == self.result else self.result
                other_bytes = other.read_bytes()

                def timeout(*args, **kwargs):
                    moved.write_bytes(foreign)
                    return {"ok": False, "class": "TIMEOUT", "payload_state": "may_have_been_sent"}

                self.engine.side_effect = timeout
                if moved == self.spec:
                    error = self._assert_class("RECOVERY_REQUIRED", self._save)
                    self.assertEqual(error.__cause__.code, "TIMEOUT")
                else:
                    self._assert_class("TIMEOUT", self._save)
                if state == "PREPARED":
                    self.assertEqual(self.journal.read_bytes(), journal)
                else:
                    self._assert_annotations(journal, superseding=True)
                self._assert_public_block()
                self.assertEqual(moved.read_bytes(), foreign)
                self.assertEqual(other.read_bytes(), other_bytes)

    def test_case_06_recovery_retry_after_subprocess_death(self):
        self._seed_pair()
        child = self._public_child("save", "before-spec")
        self.assertEqual(child.returncode, 71, child.stdout + child.stderr)
        self._assert_public_compare("MATCH")
        self._assert_completed()
        before = {path: path.read_bytes() for path in self.snaps.rglob("*") if path.is_file()}
        self._assert_public_compare("MATCH")
        self.assertEqual({path: path.read_bytes() for path in self.snaps.rglob("*")
                          if path.is_file()}, before)

    def test_case_08_edit_after_recovery_reads_staged_spec(self):
        self._seed_pair()
        self.result.write_bytes(self._NEW_RESULT)
        journal = self._write_journal()
        foreign = self.old_spec_bytes + b" "
        read_bytes = Path.read_bytes
        reads = []

        def edit_after_read(path):
            data = read_bytes(path)
            if path == self.staged and not reads:
                reads.append(path)
                self._external_snapshot_edit(self.spec, foreign)
            return data

        with patch.object(Path, "read_bytes", edit_after_read):
            self._assert_class("RECOVERY_REQUIRED", self._compare)
        self.assertEqual(reads, [self.staged])
        self.assertEqual(self.spec.read_bytes(), foreign)
        self.assertEqual(self.result.read_bytes(), self._NEW_RESULT)
        self.assertEqual(self.journal.read_bytes(), journal)
        self.engine.assert_not_called()

    @staticmethod
    def _external_snapshot_edit(path, data):
        subprocess.run(
            [sys.executable, "-c",
             "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(bytes.fromhex(sys.argv[2]))",
             str(path), data.hex()],
            check=True, timeout=10,
        )

    def _record_snapshot_syncs(self, sync_path, callback):
        real_sync = os.fsync
        inode = sync_path.stat()

        def sync(fd):
            if os.path.samestat(os.fstat(fd), inode):
                callback()
            return real_sync(fd)

        return patch("os.fsync", side_effect=sync)

    @staticmethod
    def _fail_snapshot_sync():
        raise OSError("injected snapshot fsync failure")

    @unittest.skipUnless(os.name == "posix", "POSIX directory fsync")
    def test_case_13_save_specs_sync_failure_retains_journal(self):
        self._seed_pair()
        with self._record_snapshot_syncs(self.spec.parent, self._fail_snapshot_sync):
            self._assert_class("RECOVERY_REQUIRED", self._save)
            self.assertEqual(json.loads(self.journal.read_bytes())["state"], "RESULT_PUBLISHED")
            retained = self.journal.read_bytes()
            self._assert_class("RECOVERY_REQUIRED", self._compare)
            self.assertEqual(self.journal.read_bytes(), retained)
        self._assert_public_compare("MATCH")
        self._assert_completed()

    @unittest.skipUnless(os.name == "posix", "POSIX recovery fsync")
    def test_case_13_live_sync_failure_blocks_cleanup(self):
        self._seed_pair()
        self.result.write_bytes(self._NEW_RESULT)
        self.spec.write_bytes(self.new_spec_bytes)
        journal = self._write_journal()
        with self._record_snapshot_syncs(self.result, self._fail_snapshot_sync):
            self._assert_class("RECOVERY_REQUIRED", self._compare)
        self.assertEqual(self.journal.read_bytes(), journal)
        self.assertEqual(self.spec.read_bytes(), self.new_spec_bytes)
        self.assertEqual(self.result.read_bytes(), self._NEW_RESULT)
        self._assert_public_compare("MATCH")
        self._assert_completed()

    @unittest.skipUnless(os.name == "posix", "POSIX recovery fsync")
    def test_case_13_cleanup_rechecks_live_spec_after_sync(self):
        self._seed_pair()
        self.result.write_bytes(self._NEW_RESULT)
        self.spec.write_bytes(self.new_spec_bytes)
        journal = self._write_journal()
        foreign = self.new_spec_bytes + b" "
        with self._record_snapshot_syncs(self.spec.parent, lambda: self._external_snapshot_edit(self.spec, foreign)):
            self._assert_class("RECOVERY_REQUIRED", self._compare)
        self.assertEqual(self.spec.read_bytes(), foreign)
        self.assertEqual(self.result.read_bytes(), self._NEW_RESULT)
        self.assertEqual(self.journal.read_bytes(), journal)
        self.engine.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX recovery fsync")
    def test_case_13_clear_requires_successful_result_sync(self):
        journal = self._blocked_publication(superseding=True, result_foreign=True)
        with self._record_snapshot_syncs(self.result, self._fail_snapshot_sync):
            self._assert_class("RECOVERY_REQUIRED", self._compare)
        self.assertEqual(self.journal.read_bytes(), journal)
        self.assertEqual(self.result.read_bytes(), self._NEW_RESULT)
        self.assertEqual(self.spec.read_bytes(), self.old_spec_bytes)

    def test_case_14_budget_refusal_then_edit_and_operator_restoration(self):
        journal = self._blocked_publication()
        replace = os.replace
        journal_writes = []

        def record_replace(src, dst):
            if Path(dst) == self.journal:
                journal_writes.append(Path(dst))
            return replace(src, dst)

        with patch("pbz.checks.spend", return_value=False), patch("os.replace", side_effect=record_replace):
            self.assertEqual(self._save()["status"], "BUDGET")
        self.assertEqual(journal_writes, [])
        self.assertEqual(self.journal.read_bytes(), journal)
        self._assert_public_block()
        foreign = self._NEW_RESULT + b" "
        self.result.write_bytes(foreign)
        self._assert_class("RECOVERY_REQUIRED", self._save)
        self.engine.assert_not_called()
        self.assertEqual(self.result.read_bytes(), foreign)
        self.assertEqual(self.journal.read_bytes(), journal)
        self.result.write_bytes(self._NEW_RESULT)
        self.staged.write_bytes(self.new_spec_bytes)
        self._assert_public_compare("MATCH")
        self._assert_completed()

    def test_case_24_process_death_leaves_flag_for_recovery_to_clear(self):
        journal = self._blocked_publication()
        script = f"""
    import json
    journal = Path({str(self.journal)!r})
    pending = journal.parent
    def crash_if_flagged():
        if json.loads(journal.read_bytes()).get("superseding"):
            os._exit(73)
    if os.name == "nt":
        replace_flag = os.replace
        def crash_after_replace(src, dst):
            replace_flag(src, dst)
            if Path(dst) == journal:
                crash_if_flagged()
        stack.enter_context(patch("os.replace", side_effect=crash_after_replace))
    else:
        sync = os.fsync
        def crash_after_sync(fd):
            sync(fd)
            if os.path.samestat(os.fstat(fd), pending.stat()):
                crash_if_flagged()
        stack.enter_context(patch("os.fsync", side_effect=crash_after_sync))
"""
        child = self._start_snapshot_script(script, interruption="before-engine-write")
        out, err = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 73, out + err)
        self._assert_annotations(journal, superseding=True)
        self._assert_public_block()
        self.assertEqual(self.journal.read_bytes(), journal)
        foreign = self._NEW_RESULT + b" "
        self.result.write_bytes(foreign)
        self._assert_class("RECOVERY_REQUIRED", self._save)
        self.engine.assert_not_called()
        self.assertEqual(self.result.read_bytes(), foreign)

    def test_case_24_clearing_replace_failure_retains_annotations(self):
        journal = self._blocked_publication(superseding=True, result_foreign=True)
        replace = os.replace

        def deny_journal_replace(src, dst):
            if Path(dst) == self.journal:
                raise OSError("cannot install cleared journal")
            return replace(src, dst)

        with patch("os.replace", side_effect=deny_journal_replace):
            self._assert_class("RECOVERY_REQUIRED", self._compare)
        self.assertEqual(self.journal.read_bytes(), journal)
        self.engine.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX journal directory fsync")
    def test_case_24_clearing_post_replace_sync_failure_uses_installed_bytes(self):
        cleared = self._blocked_publication()
        self._blocked_publication(superseding=True, result_foreign=True)

        def fail_cleared_sync():
            if self.journal.read_bytes() == cleared:
                self._fail_snapshot_sync()

        with self._record_snapshot_syncs(self.pending, fail_cleared_sync):
            self._assert_class("RECOVERY_REQUIRED", self._compare)
        self.assertEqual(self.journal.read_bytes(), cleared)
        foreign = self._NEW_RESULT + b" "
        self.result.write_bytes(foreign)
        self._assert_class("RECOVERY_REQUIRED", self._save)
        self.engine.assert_not_called()
        self.assertEqual(self.result.read_bytes(), foreign)

    @unittest.skipUnless(os.name == "posix", "POSIX annotation prerequisite fsync")
    def test_case_24_clearing_rechecks_result_after_sync(self):
        journal = self._blocked_publication(superseding=True, result_foreign=True)
        foreign = self._NEW_RESULT + b" "
        with self._record_snapshot_syncs(self.result, lambda: self._external_snapshot_edit(self.result, foreign)):
            self._assert_class("RECOVERY_REQUIRED", self._compare)
        self.assertEqual(self.journal.read_bytes(), journal)
        self.assertEqual(self.result.read_bytes(), foreign)
        self.engine.assert_not_called()

    def test_case_32_published_original_survives_superseding_stage_replace(self):
        journal = self._blocked_publication()
        os.link(self.spec, self.staged)
        newest = self._NEW_RESULT + b" "
        child = self._public_child("save", "after-stage", result=newest)
        self.assertEqual(child.returncode, 71, child.stdout + child.stderr)
        self.assertEqual(self.result.read_bytes(), newest)
        self.assertEqual(self.spec.read_bytes(), self.old_spec_bytes)
        self.assertEqual(self.staged.read_bytes(), self.new_spec_bytes)
        self.assertFalse(os.path.samefile(self.spec, self.staged))
        self._assert_annotations(journal, superseding=True)

    def test_case_33_equals_before_cleanup_isolates_dotted_name(self):
        self._seed_pair()
        self._write_journal("PREPARED")
        for path in (self.result, self.spec, self.journal, self.staged, self.pending / "different.lock"):
            path.rename(path.with_name(path.name.replace("different", "a", 1)))
        other_journal = self.pending / "a.journal.journal.json"
        other_journal.write_bytes(b"{")
        other_stage = self.pending / "a.journal.spec.next"
        other_stage.write_bytes(b"blocking name")
        other_lock = self.pending / "a.journal.lock"
        other_lock.touch()
        orphan = self.pending / "a.journal.leftover"
        orphan.write_bytes(b"ignored")
        before = {path: path.read_bytes() for path in (other_journal, other_stage, other_lock, orphan)}
        self.engine.return_value = {"ok": True, "class": "OK", "queries": []}
        self.engine.side_effect = None
        checks.snapshot(self.ws, None, "compare", "a", self._OLD_SPEC)
        self.assertFalse((self.pending / "a.journal.json").exists())
        self.assertFalse((self.pending / "a.spec.next").exists())
        self.assertEqual((self.result.parent / "a.json").read_bytes(), self._OLD_RESULT)
        self.assertEqual((self.spec.parent / "a.json").read_bytes(), self.old_spec_bytes)
        self.assertEqual({path: path.read_bytes() for path in before}, before)


    def test_case_27_prepared_marker_clears_with_before_result_and_staged_live_spec(self):
        self._seed_pair()
        self.spec.write_bytes(self.new_spec_bytes)
        journal = self._write_journal("PREPARED")
        self._write_journal("PREPARED", result_foreign=True)
        self._assert_public_block()
        self.assertEqual(self.journal.read_bytes(), journal)
        child = self._public_child("save", "inside-engine")
        self.assertEqual(child.returncode, 71, child.stdout + child.stderr)
        self._assert_public_block()
        self._save()
        self._assert_completed()

    @unittest.skipUnless(os.name == "posix", "POSIX clearing prerequisite fsync")
    def test_case_27_absent_before_result_syncs_snapshots_parent(self):
        self._assert_case_27_results_directory(replaced=False)

    @unittest.skipUnless(os.name == "posix", "POSIX clearing prerequisite fsync")
    def test_case_27_replaced_results_directory_requires_parent_sync(self):
        self._assert_case_27_results_directory(replaced=True)

    def _assert_case_27_results_directory(self, *, replaced):
        self.result.parent.mkdir(parents=True)
        self.result.write_bytes(self._NEW_RESULT)
        fields = {"before_result": None, "before_spec": None}
        journal = self._write_journal("PREPARED", result_foreign=True, **fields)
        if replaced:
            aside = self.snaps / "results-aside"
            self.result.parent.rename(aside)
            self.result.parent.mkdir()
        else:
            self.result.unlink()
            self.result.parent.rmdir()

        def fail_if_marked():
            if self.journal.exists() and json.loads(self.journal.read_bytes()).get("result_foreign"):
                self._fail_snapshot_sync()

        if replaced:
            with self._record_snapshot_syncs(self.snaps, fail_if_marked):
                self._assert_class("RECOVERY_REQUIRED", self._save)
            self.assertEqual(self.journal.read_bytes(), journal)
            self.engine.assert_not_called()
            self.assertEqual((aside / self.result.name).read_bytes(), self._NEW_RESULT)

        syncs = []
        clears = []
        replace = os.replace

        def record_if_marked():
            if self.journal.exists() and json.loads(self.journal.read_bytes()).get("result_foreign"):
                syncs.append(self.snaps)

        def record_clear(src, dst):
            if Path(dst) == self.journal and self.journal.exists():
                before = json.loads(self.journal.read_bytes())
                if before.pop("result_foreign", False):
                    clears.append((list(syncs), Path(src).read_bytes(), json.dumps(before).encode()))
            return replace(src, dst)

        with (
            self._record_snapshot_syncs(self.snaps, record_if_marked),
            patch("os.replace", side_effect=record_clear),
        ):
            self._save()
        self.assertEqual(len(clears), 1, "recovery must clear the existing marker")
        syncs_before_clear, rewritten, expected = clears[0]
        self.assertIn(self.snaps, syncs_before_clear)
        self.assertEqual(rewritten, expected)
        self._assert_completed()

    def test_case_28_spec_moved_during_superseding_refusal(self):
        for refusal in ("ENGINE_UNAVAILABLE", "BUDGET"):
            with self.subTest(refusal=refusal):
                journal = self._blocked_publication("PREPARED")
                foreign = self.old_spec_bytes + b" "

                def edit_and_refuse(*args):
                    self.spec.write_bytes(foreign)
                    if refusal == "ENGINE_UNAVAILABLE":
                        raise PbzError(refusal)
                    return False

                target = "pbz.desktop.require_port" if refusal == "ENGINE_UNAVAILABLE" else "pbz.checks.spend"
                with patch(target, side_effect=edit_and_refuse):
                    error = self._assert_class("RECOVERY_REQUIRED", self._save)
                self.assertIn(refusal.lower(), str(error).lower() + str(error.__cause__).lower())
                self.assertEqual(self.journal.read_bytes(), journal)
                self._assert_public_block()
                self._assert_class("RECOVERY_REQUIRED", self._save)
                self.engine.assert_not_called()
                self.assertEqual(self.spec.read_bytes(), foreign)

    def test_case_28_journal_known_spec_edit_blocks_only_current_attempt(self):
        self._seed_pair()
        self.result.write_bytes(self._NEW_RESULT)
        journal = self._write_journal("PREPARED")

        def edit_and_refuse(*args):
            self.spec.write_bytes(self.new_spec_bytes)
            raise PbzError("ENGINE_UNAVAILABLE")

        with patch("pbz.desktop.require_port", side_effect=edit_and_refuse):
            self._assert_class("RECOVERY_REQUIRED", self._save)
        self.engine.assert_not_called()
        self.assertEqual(self.spec.read_bytes(), self.new_spec_bytes)
        self.assertEqual(self.journal.read_bytes(), journal)
        self._save()
        self._assert_completed()

    def test_case_28_inherited_flag_marker_failure_preserves_supersede_permission(self):
        self._blocked_publication(superseding=True)
        self.result.write_bytes(self._NEW_RESULT + b" ")
        original = self.journal.read_bytes()
        foreign = self._NEW_RESULT + b"  "
        replace = os.replace
        denied = []

        def edit_and_refuse(*args):
            self.result.write_bytes(foreign)
            return False

        def deny_marker(src, dst):
            if Path(dst) == self.journal and json.loads(Path(src).read_bytes()).get("result_foreign"):
                denied.append(Path(dst))
                raise OSError("cannot install marker")
            return replace(src, dst)

        with (
            patch("pbz.checks.spend", side_effect=edit_and_refuse),
            patch("os.replace", side_effect=deny_marker),
        ):
            self._assert_class("RECOVERY_REQUIRED", self._save)
        self.assertEqual(denied, [self.journal])
        self.engine.assert_not_called()
        self.assertEqual(self.journal.read_bytes(), original)
        self.assertEqual(self.result.read_bytes(), foreign)
        self._save()
        self._assert_completed()

    @unittest.skipUnless(os.name == "posix", "POSIX directory fsync")
    def test_case_17_snapshots_sync_precedes_journal_unlink_after_engine_creates_results(self):
        self.spec.parent.mkdir(parents=True)
        self.spec.write_bytes(self.old_spec_bytes)
        events = []
        unlink = os.unlink
        replace = os.replace

        def engine(*args, **kwargs):
            response = self._engine(*args, **kwargs)
            events.append("engine-published")
            return response

        def record_unlink(path, *args, **kwargs):
            if Path(path) == self.journal:
                events.append("journal-unlink")
            return unlink(path, *args, **kwargs)

        def record_spec_replace(src, dst):
            result = replace(src, dst)
            if Path(dst) == self.spec:
                events.append("spec-published")
            return result

        self.engine.side_effect = engine
        with (
            self._record_snapshot_syncs(self.snaps, lambda: events.append("snapshots-sync")),
            patch("os.unlink", side_effect=record_unlink),
            patch("os.replace", side_effect=record_spec_replace),
        ):
            self._save()
        self.assertIn("journal-unlink", events)
        self.assertLess(events.index("engine-published"), events.index("spec-published"))
        self.assertIn("snapshots-sync", events[events.index("spec-published"):events.index("journal-unlink")])
        self._assert_completed()

    @unittest.skipUnless(os.name == "posix", "POSIX directory fsync")
    def test_case_17_superseding_sync_failure_preserves_governing_journal(self):
        for state in ("PREPARED", "RESULT_PUBLISHED"):
            with self.subTest(state=state):
                journal = self._blocked_publication(state)
                newest = self._NEW_RESULT + b" "
                events = []
                replace = os.replace

                def engine(*args, **kwargs):
                    response = self._engine(*args, **kwargs)
                    self.result.write_bytes(newest)
                    events.append("engine-published")
                    return response

                def fail_after_publication():
                    if "engine-published" in events:
                        events.append("snapshots-sync")
                        self._fail_snapshot_sync()

                def record_replace(src, dst):
                    if Path(dst) == self.journal and "engine-published" in events:
                        events.append("journal-replace")
                    return replace(src, dst)

                self.engine.side_effect = engine
                with (
                    self._record_snapshot_syncs(self.snaps, fail_after_publication),
                    patch("os.replace", side_effect=record_replace),
                ):
                    self._assert_class("RECOVERY_REQUIRED", self._save)
                self.assertEqual(events, ["engine-published", "snapshots-sync"])
                if state == "PREPARED":
                    self.assertEqual(self.journal.read_bytes(), journal)
                else:
                    self._assert_annotations(journal, superseding=True)
                self.assertEqual(self.result.read_bytes(), newest)
                self.assertEqual(self.spec.read_bytes(), self.old_spec_bytes)


    def test_case_26_all_failed_save_marks_moved_result_or_clears_self_set_flag(self):
        for state in (None, "PREPARED", "RESULT_PUBLISHED", "inherited"):
            with self.subTest(state=state):
                self.journal.unlink(missing_ok=True)
                self.staged.unlink(missing_ok=True)
                self._seed_pair()
                if state == "inherited":
                    original = self._blocked_publication(superseding=True)
                    self.result.write_bytes(self._NEW_RESULT + b" ")
                elif state:
                    original = self._blocked_publication(state)
                foreign = self._NEW_RESULT + b"  "

                def fail_all(*args, **kwargs):
                    self.result.write_bytes(foreign)
                    return {"ok": True, "class": "OK", "payload_state": "may_have_been_sent",
                            "queries": [{"id": "q1", "result": "FAILED"}]}

                self.engine.side_effect = fail_all
                self.engine.reset_mock()
                self._assert_class("RECOVERY_REQUIRED", self._save)
                self.engine.assert_called_once()
                if state == "RESULT_PUBLISHED":
                    self.assertEqual(self.journal.read_bytes(), original)
                elif state is None:
                    document = json.loads(self.journal.read_bytes())
                    self.assertEqual(document["state"], "PREPARED")
                    self.assertIs(document.get("result_foreign"), True)
                else:
                    self._assert_annotations(original, result_foreign=True)
                self.engine.reset_mock()
                self._assert_class("RECOVERY_REQUIRED", self._save)
                self.engine.assert_not_called()
                self.assertEqual(self.result.read_bytes(), foreign)

    def test_case_26_clear_failure_falls_back_to_marker_or_retains_flag(self):
        if os.name != "posix":
            self.skipTest("POSIX annotation directory fsync")
        for fault in ("clear", "both", "after-marker-replace"):
            with self.subTest(fault=fault):
                original = self._blocked_publication()
                foreign = self._NEW_RESULT + b" "
                replace = os.replace
                writes = []

                def fail_dispatch(*args, **kwargs):
                    self.result.write_bytes(foreign)
                    return {"ok": False, "class": "ENGINE_UNAVAILABLE", "payload_state": "not_sent"}

                def annotate(src, dst):
                    if Path(dst) == self.journal:
                        document = json.loads(Path(src).read_bytes())
                        if document.get("result_foreign"):
                            writes.append("mark")
                            if fault == "both":
                                raise OSError("cannot install marker")
                        elif not document.get("superseding"):
                            writes.append("clear")
                            raise OSError("cannot clear flag")
                    return replace(src, dst)

                def fail_after_marker():
                    if self.journal.exists() and json.loads(self.journal.read_bytes()).get("result_foreign"):
                        self._fail_snapshot_sync()

                self.engine.side_effect = fail_dispatch
                from contextlib import ExitStack
                with ExitStack() as stack:
                    stack.enter_context(patch("os.replace", side_effect=annotate))
                    if fault == "after-marker-replace":
                        stack.enter_context(self._record_snapshot_syncs(self.pending, fail_after_marker))
                    error = self._assert_class("RECOVERY_REQUIRED", self._save)
                self.assertEqual(error.__cause__.code, "ENGINE_UNAVAILABLE")
                self.assertEqual(writes, ["clear", "mark"])
                self.assertEqual(self.result.read_bytes(), foreign)
                if fault == "both":
                    self._assert_annotations(original, superseding=True)
                    self.engine.side_effect = self._engine
                    self._save()
                    self._assert_completed()
                else:
                    self._assert_annotations(original, superseding=True, result_foreign=True)
                    self.engine.reset_mock()
                    self._assert_class("RECOVERY_REQUIRED", self._save)
                    self.engine.assert_not_called()
                    self.assertEqual(self.result.read_bytes(), foreign)

    def _native_engine_patch(self, popen, responses):
        from contextlib import ExitStack
        from pbz import common, engine

        def observe(*args, **kwargs):
            response = common.engine_call(*args, **kwargs)
            responses.append(response)
            return response

        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(engine, "ENGINE_EXE", sys.executable))
        stack.enter_context(patch.object(engine.subprocess, "Popen", side_effect=popen))
        stack.enter_context(patch("pbz.checks.engine_call", side_effect=observe))
        return stack

    @staticmethod
    def _reap_snapshot_engine(child):
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()

    @unittest.skipUnless(os.name == "posix", "POSIX spawn and timeout")
    def test_case_26_native_spawn_failure_and_timeout_distinguish_payload_state(self):
        popen = subprocess.Popen
        for spawn_failure in (True, False):
            with self.subTest(spawn_failure=spawn_failure):
                original = self._blocked_publication()
                foreign = self._NEW_RESULT + b" "
                responses = []
                children = []

                def start(*args, **kwargs):
                    self.result.write_bytes(foreign)
                    if spawn_failure:
                        return popen([str(self.pending / "nonexistent-engine")], **kwargs)
                    child = popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
                    children.append(child)
                    communicate = child.communicate
                    child.communicate = lambda input=None, timeout=None: communicate(input, timeout=0.2)
                    self.addCleanup(self._reap_snapshot_engine, child)
                    return child

                with self._native_engine_patch(start, responses):
                    error = self._assert_class("RECOVERY_REQUIRED" if spawn_failure else "TIMEOUT",
                                               self._save)
                self.assertEqual(responses[0].get("payload_state"),
                                 "not_sent" if spawn_failure else "may_have_been_sent")
                if spawn_failure:
                    self.assertEqual(error.__cause__.code, "ENGINE_UNAVAILABLE")
                    self.assertEqual(self.journal.read_bytes(), original)
                    self._assert_class("RECOVERY_REQUIRED", self._save)
                    self.engine.assert_not_called()
                else:
                    self.assertEqual(len(children), 1)
                    self.assertIsNotNone(children[0].returncode, "timeout must reap the child")
                    self._assert_annotations(original, superseding=True)
                    self._assert_public_block()
                self.assertEqual(self.result.read_bytes(), foreign)

    @unittest.skipUnless(os.name == "posix", "POSIX native communicate faults")
    def test_case_26_native_communicate_exceptions(self):
        for fault in (RuntimeError, OSError, KeyboardInterrupt):
            with self.subTest(fault=fault.__name__):
                self._assert_native_communicate_exception(fault, moved=True)

    @unittest.skipUnless(os.name == "posix", "POSIX native communicate fault")
    def test_case_30_unmoved_supersede_runtime_error_leaves_flag(self):
        self._assert_native_communicate_exception(RuntimeError, moved=False)

    def _assert_native_communicate_exception(self, fault, *, moved):
        original = self._blocked_publication()
        foreign = self._NEW_RESULT + b" " if moved else self._NEW_RESULT
        responses = []
        children = []
        popen = subprocess.Popen

        def start(*args, **kwargs):
            child = popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            children.append(child)
            self.addCleanup(self._reap_snapshot_engine, child)
            communicate = child.communicate

            def fail_once(*args, **kwargs):
                child.communicate = communicate
                self.result.write_bytes(foreign)
                raise fault("injected communicate failure")

            child.communicate = fail_once
            return child

        with self._native_engine_patch(start, responses):
            if fault is KeyboardInterrupt:
                with self.assertRaises(KeyboardInterrupt):
                    self._save()
            else:
                error = self._assert_class("RECOVERY_REQUIRED" if fault is RuntimeError
                                           else "ENGINE_UNAVAILABLE", self._save)
                if fault is RuntimeError:
                    self.assertEqual(error.__cause__.code, "OTHER")
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)
        if fault is RuntimeError:
            self.assertEqual(responses[0]["class"], "OTHER")
            self.assertNotIn("payload_state", responses[0])
        elif fault is OSError:
            self.assertEqual(responses[0].get("payload_state"), "may_have_been_sent")
        else:
            self.assertEqual(responses, [])
        self._assert_annotations(original, superseding=True)
        self.assertEqual(self.result.read_bytes(), foreign)
        if moved:
            self._assert_public_block()

    @unittest.skipUnless(os.name == "posix", "POSIX native cleanup faults")
    def test_case_30_timeout_retries_child_kill_before_confirming_exit(self):
        for superseding, fail_twice in ((False, True), (False, False), (True, True)):
            with self.subTest(superseding=superseding, fail_twice=fail_twice):
                self._assert_native_timeout_kill_retry(superseding, fail_twice)

    def _assert_native_timeout_kill_retry(self, superseding, fail_twice):
        self.journal.unlink(missing_ok=True)
        self.staged.unlink(missing_ok=True)
        self._seed_pair()
        if superseding:
            original = self._blocked_publication()
        original_result = self.result.read_bytes()
        release = Path(self.ws.root) / "release-engine"
        release.unlink(missing_ok=True)
        popen = subprocess.Popen
        responses = []
        children = []
        kills = []
        script = """
import sys, time
from pathlib import Path
release, result = map(Path, sys.argv[1:])
while not release.exists():
    time.sleep(0.01)
result.write_bytes(bytes.fromhex(release.read_text()))
"""

        def start(*args, **kwargs):
            child = popen([sys.executable, "-c", script, str(release), str(self.result)], **kwargs)
            children.append(child)
            kill, wait, communicate = child.kill, child.wait, child.communicate

            def cleanup():
                child.kill = kill
                child.wait = wait
                self._reap_snapshot_engine(child)

            self.addCleanup(cleanup)

            def kill_with_fault():
                kills.append(child.pid)
                if len(kills) == 1 or fail_twice:
                    raise OSError("injected child kill failure")
                return kill()

            child.kill = kill_with_fault
            # The base's subprocess.run context manager otherwise waits forever
            # when its single kill attempt raises before terminating this stub.
            child.wait = lambda timeout=None: wait(timeout=1 if timeout is None else timeout)
            child.communicate = lambda input=None, timeout=None: communicate(input, timeout=0.2)
            return child

        spent = checks._load_state(self.ws)["snapshot_queries"]
        with self._native_engine_patch(start, responses):
            error = self._assert_class("RECOVERY_REQUIRED" if fail_twice else "TIMEOUT", self._save)
        self.assertEqual(checks._load_state(self.ws)["snapshot_queries"], spent + 1)
        self.assertEqual(self.result.read_bytes(), original_result)
        child, = children
        self.assertEqual(kills, [child.pid, child.pid])
        if not fail_twice:
            self.assertIsNotNone(child.returncode)
            self.assertEqual(responses[0]["payload_state"], "may_have_been_sent")
            self.assertFalse(self.journal.exists())
            self.assertFalse(self.staged.exists())
            return
        self.assertIsNone(child.poll())
        self.assertEqual(responses[0]["payload_state"], "unconfirmed")
        self.assertEqual(error.__cause__.code, "TIMEOUT")
        if superseding:
            self._assert_annotations(original, superseding=True)
        else:
            self.assertEqual(json.loads(self.journal.read_bytes())["state"], "PREPARED")
        release_tmp = release.with_suffix(".tmp")
        release_tmp.write_text((self._NEW_RESULT + b" ").hex(), encoding="ascii")
        os.replace(release_tmp, release)
        self.assertEqual(child.wait(timeout=10), 0)
        self.assertEqual(self.result.read_bytes(), self._NEW_RESULT + b" ")
        self._assert_public_block()


    def _start_snapshot_script(self, script, action="save", interruption=None, *, bootstrap="", **options):
        command, env = self._public_child_command(action, interruption, **options)
        command[3] = command[3].replace("    sys.exit(cli.main(args))",
                                        script + "\n    sys.exit(cli.main(args))")
        command[3] = bootstrap + "\n" + command[3]
        child = subprocess.Popen(command, cwd=self.ws.root, env=env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._stop_child, child)
        return child

    def _posix_read_only_lock_open(self):
        import errno

        real_open = os.open
        lock = self.pending / "different.lock"
        attempts = []

        def deny_write(path, flags, *args, **kwargs):
            if isinstance(path, (str, os.PathLike)) and Path(path) == lock:
                if flags & (os.O_CREAT | os.O_WRONLY | os.O_RDWR):
                    attempts.append("write-denied")
                    raise PermissionError(errno.EACCES, "read-only workspace", path)
                attempts.append("read-open")
            return real_open(path, flags, *args, **kwargs)

        return patch("os.open", side_effect=deny_write), attempts

    def _start_writer_for_denied_reader(self, *, pending):
        if pending:
            child = self._start_public_child("save", "hold-at-engine")
            self.addCleanup(self._stop_child, child)
            return child
        return self._start_snapshot_script("""
    def hold_at_port(project):
        print("ready", file=sys.stderr, flush=True)
        if not sys.stdin.readline():
            os._exit(72)
        return 1
    stack.enter_context(patch("pbz.desktop.require_port", side_effect=hold_at_port))
""")

    @unittest.skipUnless(os.name == "posix", "POSIX lock open")
    def test_case_10_denied_reader_refuses_while_live_writer_holds_name(self):
        import builtins
        import errno
        from contextlib import ExitStack

        self._seed_pair()
        lock = self.pending / "different.lock"
        openers = ((builtins, "open"), (os, "open"))

        def deny_lock(real_open):
            def open_file(path, *args, **kwargs):
                if isinstance(path, (str, os.PathLike)) and Path(path) == lock:
                    raise PermissionError(errno.EACCES, "injected lock open denial", path)
                return real_open(path, *args, **kwargs)
            return open_file

        for pending in (False, True):
            with self.subTest(pending=pending):
                self._seed_pair()
                child = self._start_writer_for_denied_reader(pending=pending)
                try:
                    self.assertEqual(self._child_signal(child), "ready")
                    before = _tree(self.snaps)
                    with ExitStack() as stack:
                        for owner, name in openers:
                            stack.enter_context(patch.object(owner, name, side_effect=deny_lock(getattr(owner, name))))
                        self._assert_class("RECOVERY_REQUIRED" if pending else "LOCAL_STATE_UNAVAILABLE",
                                           self._compare)
                    self.engine.assert_not_called()
                    self.assertEqual(_tree(self.snaps), before)
                    self.assertEqual(self.journal.exists(), pending)
                finally:
                    out, err = child.communicate("\n", timeout=10)
                self.assertEqual(child.returncode, 0, out + err)

    @unittest.skipUnless(os.name == "posix", "POSIX read-only flock")
    def test_case_10_step_two_reader_waits_for_writer(self):
        import fcntl
        from queue import Queue
        from threading import Thread

        self._seed_pair()
        child = self._start_public_child("save", "hold-at-engine")
        self.addCleanup(self._stop_child, child)
        self.assertEqual(self._child_signal(child), "ready")
        lock = self.pending / "different.lock"
        open_patch, attempts = self._posix_read_only_lock_open()
        progress = Queue()
        real_flock = fcntl.flock

        def observe(fd, operation):
            if lock.exists() and os.path.samestat(os.fstat(fd), lock.stat()) and operation & fcntl.LOCK_EX:
                try:
                    return real_flock(fd, operation | fcntl.LOCK_NB)
                except BlockingIOError:
                    progress.put("contended")
            return real_flock(fd, operation)

        def compare():
            try:
                self._compare()
            except Exception as error:
                progress.put(error)
            else:
                progress.put("compared")

        with open_patch, patch("fcntl.flock", side_effect=observe):
            reader = Thread(target=compare, daemon=True)
            reader.start()
            try:
                self.assertEqual(progress.get(timeout=10), "contended")
                self.assertEqual(attempts, ["write-denied", "read-open"])
                self.engine.assert_not_called()
            finally:
                out, err = child.communicate("\n", timeout=10)
                reader.join(timeout=10)
        self.assertEqual(child.returncode, 0, out + err)
        self.assertFalse(reader.is_alive())
        self.assertEqual(progress.get(timeout=10), "compared")
        self.engine.assert_called_once()

    @unittest.skipUnless(os.name == "posix", "POSIX read-only flock")
    def test_case_10_read_only_recovery_write_failure_preserves_tree(self):
        import errno

        self._seed_pair()
        self.result.write_bytes(self._NEW_RESULT)
        self._write_journal()
        open_patch, attempts = self._posix_read_only_lock_open()
        before = _tree(self.snaps)
        mkstemp = tempfile.mkstemp
        denied = []

        def deny_recovery_write(*args, **kwargs):
            if kwargs.get("dir") and Path(kwargs["dir"]) in (self.pending, self.spec.parent):
                denied.append(Path(kwargs["dir"]))
                raise OSError(errno.EROFS, "injected read-only recovery write")
            return mkstemp(*args, **kwargs)

        with open_patch, patch("tempfile.mkstemp", side_effect=deny_recovery_write):
            self._assert_class("RECOVERY_REQUIRED", self._compare)
        self.assertEqual(attempts, ["write-denied", "read-open"])
        self.assertTrue(denied)
        self.assertEqual(_tree(self.snaps), before)
        self.engine.assert_not_called()

    def test_case_10_flag_clear_survives_later_spec_replace_denial(self):
        self._seed_pair()
        self.result.write_bytes(self._NEW_RESULT)
        cleared = self._write_journal()
        self._write_journal(superseding=True)
        replace = os.replace
        denied = []

        def deny_spec_replace(src, dst):
            if Path(dst) == self.spec:
                denied.append(self.journal.read_bytes())
                raise PermissionError("cannot install spec")
            return replace(src, dst)

        with patch("os.replace", side_effect=deny_spec_replace):
            self._assert_class("RECOVERY_REQUIRED", self._compare)
        self.assertEqual(denied, [cleared])
        self.assertEqual(self.journal.read_bytes(), cleared)
        self.assertEqual(self.result.read_bytes(), self._NEW_RESULT)
        self.assertEqual(self.spec.read_bytes(), self.old_spec_bytes)
        self._assert_public_compare("MATCH")
        self._assert_completed()

    @unittest.skipUnless(os.name == "posix", "POSIX creation-race directory fsync")
    def test_case_17_other_name_syncs_directory_chain_before_dispatch(self):
        import shutil

        for directory in (self.pending, self.snaps):
            with self.subTest(directory=directory.name):
                shutil.rmtree(self.snaps, ignore_errors=True)
                if directory == self.pending:
                    self.snaps.mkdir()
                script = f"""
    mkdir = os.mkdir
    target = Path({str(directory)!r})
    def pause_after_mkdir(path, *args, **kwargs):
        mkdir(path, *args, **kwargs)
        if not isinstance(path, int) and Path(path) == target:
            print("created", file=sys.stderr, flush=True)
            if not sys.stdin.readline():
                os._exit(72)
    stack.enter_context(patch("os.mkdir", side_effect=pause_after_mkdir))
"""
                child = self._start_snapshot_script(script, interruption="watch-lock")
                try:
                    self.assertEqual(self._child_signal(child), "created")
                    self.assertTrue(directory.is_dir())
                    recorded = []
                    parent = directory.parent
                    parent_synced_at_dispatch = False

                    def dispatch(verb, payload, **kwargs):
                        nonlocal parent_synced_at_dispatch
                        parent_synced_at_dispatch = parent in recorded
                        other = self.result.with_name("other.json")
                        other.parent.mkdir(exist_ok=True)
                        other.write_bytes(self._NEW_RESULT)
                        return {"ok": True, "class": "OK", "payload_state": "may_have_been_sent",
                                "queries": [{"id": "q1", "result": "SAVED"}]}

                    self.engine.reset_mock()
                    self.engine.side_effect = dispatch
                    with self._record_snapshot_syncs(parent, lambda: recorded.append(parent)):
                        result = checks.snapshot(self.ws, None, "save", "other", self._NEW_SPEC)
                    self.engine.assert_called_once()
                    self.assertEqual(self.engine.call_args.args[1]["name"], "other")
                    self.assertTrue(parent_synced_at_dispatch, "observing an existing directory still requires syncing its parent")
                    self.assertEqual(result["queries"], [{"id": "q1", "result": "SAVED"}])
                finally:
                    self._stop_child(child)


    @unittest.skipUnless(os.name == "posix", "POSIX inherited flock")
    def test_case_11_engine_child_keeps_lock_after_holder_death(self):
        self._assert_inherited_lock_lifetime("kill")

    @unittest.skipUnless(os.name == "posix", "POSIX inherited flock")
    def test_case_11_double_interrupt_closes_holder_but_preserves_child_lock(self):
        self._assert_inherited_lock_lifetime("double-interrupt")

    def _start_lifetime_holder(self, mode, *, bootstrap=""):
        ready = Path(self.ws.root) / "engine-ready"
        done = Path(self.ws.root) / "engine-done"
        release = Path(self.ws.root) / "engine-release"
        script = """
import json, os, sys, time
from pathlib import Path
ready, done, release, result = map(Path, sys.argv[1:5])
mode = sys.argv[5]
ready.write_text(str(os.getpid()))
try:
    request = sys.stdin.buffer.read()
    if request:
        json.loads(request)
        done.with_name("engine-received").touch()
        while not release.exists():
            time.sleep(0.01)
        if mode != "default-path":
            result.write_bytes(b'{"queries": {"q1": {"rows": ["engine-child"]}}}')
finally:
    done.touch()
"""
        holder = f"""
    import subprocess, time
    from pbz import checks, common, engine
    mode = {mode!r}
    real_popen = subprocess.Popen
    ready = Path({str(ready)!r})
    def start(argv, **kwargs):
        child = real_popen([sys.executable, "-c", {script!r}, {str(ready)!r}, {str(done)!r},
                            {str(release)!r}, {str(self.result)!r}, mode], **kwargs)
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not ready.exists():
            raise RuntimeError("engine never reached stdin")
        print("spawned", file=sys.stderr, flush=True)
        if mode == "spawn-window":
            os._exit(71)
        if mode == "double-interrupt":
            def interrupt(input=None, timeout=None):
                child.stdin.write(input)
                child.stdin.close()
                raise KeyboardInterrupt("communicate interrupted")
            def interrupt_kill():
                print("kill-interrupted", file=sys.stderr, flush=True)
                raise KeyboardInterrupt("kill interrupted")
            child.communicate = interrupt
            child.kill = interrupt_kill
        return child
    stack.enter_context(patch.object(engine, "ENGINE_EXE", sys.executable))
    stack.enter_context(patch.object(engine.subprocess, "Popen", side_effect=start))
    stack.enter_context(patch.object(checks, "engine_call", side_effect=common.engine_call))
    if mode == "default-path":
        def launch_default(project):
            engine.call("snapshot-compare", {{}})
            return 1
        stack.enter_context(patch("pbz.desktop.require_port", side_effect=launch_default))
"""
        child = self._start_snapshot_script(holder, bootstrap=bootstrap)
        self.addCleanup(release.touch)
        return child, ready, done, release

    @staticmethod
    def _wait_for_snapshot_path(path):
        import time

        deadline = time.monotonic() + 10
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return path.exists()

    def _assert_inherited_lock_lifetime(self, mode):
        self._seed_pair()
        holder, _, done, release = self._start_lifetime_holder(mode)
        self.assertEqual(self._child_signal(holder), "spawned")
        if mode == "kill":
            # The new Popen path writes the request only after returning from spawn.
            # Observe delivery in a file before killing the holder.
            received = done.with_name("engine-received")
            self.assertTrue(self._wait_for_snapshot_path(received), "engine never received a complete request")
            holder.kill()
        else:
            self.assertEqual(self._child_signal(holder), "kill-interrupted")
        self.assertNotEqual(holder.wait(timeout=10), 0)
        contender = self._start_public_child("compare", "watch-lock")
        self.addCleanup(self._stop_child, contender)
        self.assertEqual(self._child_signal(contender), "contended")
        self.assertEqual(self.result.read_bytes(), self._OLD_RESULT)
        self.assertEqual(json.loads(self.journal.read_bytes())["state"], "PREPARED")
        release.touch()
        out, err = contender.communicate(timeout=10)
        self.assertEqual(json.loads(out).get("error"), "RECOVERY_REQUIRED", out + err)
        self.assertTrue(self._wait_for_snapshot_path(done))
        self.assertNotEqual(self.result.read_bytes(), self._OLD_RESULT)

    @unittest.skipUnless(os.name == "posix", "POSIX timeout cleanup")
    def test_case_11_timeout_names_class_after_kill_and_wait(self):
        from pbz import engine

        children = []
        operations = []
        popen = subprocess.Popen

        def start(*args, **kwargs):
            child = popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            children.append(child)
            kill, wait = child.kill, child.wait
            self.addCleanup(self._reap_snapshot_engine, child)

            def record_kill():
                operations.append("kill")
                return kill()

            def record_wait(*args, **kwargs):
                status = wait(*args, **kwargs)
                operations.append("wait")
                return status

            child.kill, child.wait = record_kill, record_wait
            return child

        with tempfile.TemporaryFile() as inherited, \
             patch.object(engine, "ENGINE_EXE", sys.executable), \
             patch("pbz.engine.subprocess.Popen", side_effect=start):
            response = engine.call("snapshot-save", {}, timeout=0.2, pass_fds=(inherited.fileno(),))
        self.assertEqual(response["class"], "TIMEOUT")
        self.assertEqual(response["payload_state"], "may_have_been_sent")
        self.assertEqual(operations[:2], ["kill", "wait"])
        self.assertIsNotNone(children[0].returncode)


    _WINDOWS_DRAIN = '''
import ctypes, sys
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.CreateFileW.restype = ctypes.c_void_p
invalid = ctypes.c_void_p(-1).value
h = k.CreateFileW(sys.argv[1], 0x80000000, 0, None, 3, 0x80, None)
if not h or h == invalid:
    raise SystemExit("lock not acquired (error %d)" % ctypes.get_last_error())
input("lock held; press Enter to release")
k.CloseHandle(ctypes.c_void_p(h))
'''

    def _windows_probe_command(self):
        return [sys.executable, "-c", self._WINDOWS_DRAIN, str(self.pending / "different.lock")]

    def _windows_probe(self):
        return subprocess.run(self._windows_probe_command(), input="\n", text=True,
                              capture_output=True, timeout=10)

    @staticmethod
    def _windows_kernel_filter(hooks):
        # Interpose before imports bind the functions, preserving their ctypes signatures.
        return """
import ctypes
from unittest.mock import patch

original_getitem = ctypes.WinDLL.__getitem__
class HookedFunction:
    def __init__(self, function, hook):
        object.__setattr__(self, "function", function)
        object.__setattr__(self, "hook", hook)
    def __getattr__(self, name):
        return getattr(self.function, name)
    def __setattr__(self, name, value):
        setattr(self.function, name, value)
    def __call__(self, *args):
        return self.hook(self.function, *args)

def bind(dll, name):
    function = original_getitem(dll, name)
    if str(dll._name).lower().removesuffix(".dll") == "kernel32" and name in hooks:
        return HookedFunction(function, hooks[name])
    return function

""" + hooks + "\npatch.object(ctypes.WinDLL, '__getitem__', bind).start()\n"

    @unittest.skipUnless(os.name == "nt", "Windows inherited exclusive-share handle")
    def test_case_11_windows_inherited_handle_outlives_killed_holder(self):
        self._assert_windows_job_lifetime(kill_on_close=False)

    @unittest.skipUnless(os.name == "nt", "Windows kill-on-close Job Object")
    def test_case_11_windows_job_kills_child_when_holder_dies(self):
        self._assert_windows_job_lifetime(kill_on_close=True)

    def _assert_windows_job_lifetime(self, *, kill_on_close):
        self._seed_pair()
        bootstrap = "" if kill_on_close else self._windows_kernel_filter(
            'hooks = {"SetInformationJobObject": lambda fn, *args: 1}'
        )
        holder, ready, done, release = self._start_lifetime_holder("kill", bootstrap=bootstrap)
        self.assertEqual(self._child_signal(holder), "spawned")
        self.assertTrue(self._wait_for_snapshot_path(done.with_name("engine-received")))
        pid = int(ready.read_text())
        self.assertIn("error 32", self._windows_probe().stderr)
        holder.kill()
        holder.wait(timeout=10)
        if kill_on_close:
            self._assert_windows_process_exited(pid)
            self._assert_public_compare("MATCH")
            self.assertEqual(self.result.read_bytes(), self._OLD_RESULT)
            self.assertEqual(self.spec.read_bytes(), self.old_spec_bytes)
            self.assertFalse(self.journal.exists())
        else:
            self.assertIn("error 32", self._windows_probe().stderr)
            contender = self._start_windows_lock_reader(step_two=False)
            self.assertEqual(self._child_signal(contender), "share-retry")
            release.touch()
            out, err = contender.communicate(timeout=10)
            self.assertEqual(json.loads(out).get("error"), "RECOVERY_REQUIRED", out + err)
            self._assert_windows_process_exited(pid)
            self.assertNotEqual(self.result.read_bytes(), self._OLD_RESULT)
            self.assertEqual(json.loads(self.journal.read_bytes())["state"], "PREPARED")

    def _assert_windows_process_exited(self, pid):
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x100000, False, pid)
        if handle:
            try:
                self.assertEqual(kernel.WaitForSingleObject(ctypes.c_void_p(handle), 10000), 0)
            finally:
                kernel.CloseHandle(ctypes.c_void_p(handle))
        else:
            self.assertEqual(ctypes.get_last_error(), 87)

    @unittest.skipUnless(os.name == "nt", "Windows spawn-before-assignment boundary")
    def test_case_11_windows_spawn_window_delivers_no_payload(self):
        self._seed_pair()
        holder, ready, done, _ = self._start_lifetime_holder("spawn-window")
        self.assertEqual(self._child_signal(holder), "spawned")
        self.assertEqual(holder.wait(timeout=10), 71)
        self.assertTrue(self._wait_for_snapshot_path(done), "child must see stdin EOF")
        self._assert_windows_process_exited(int(ready.read_text()))
        self.assertFalse(done.with_name("engine-received").exists())
        self.assertEqual(self.result.read_bytes(), self._OLD_RESULT)
        self.assertEqual(json.loads(self.journal.read_bytes())["state"], "PREPARED")
        self._assert_public_compare("MATCH")
        self.assertFalse(self.journal.exists())

    @unittest.skipUnless(os.name == "nt", "Windows default-path handle inheritability")
    def test_case_11_windows_default_path_child_does_not_keep_save_lock(self):
        self._seed_pair()
        holder, ready, done, release = self._start_lifetime_holder("default-path")
        self.assertEqual(self._child_signal(holder), "spawned")
        self.assertTrue(self._wait_for_snapshot_path(done.with_name("engine-received")))
        self.assertIn("error 32", self._windows_probe().stderr)
        holder.kill()
        holder.wait(timeout=10)
        probe = self._windows_probe()
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertFalse(done.exists(), "unrelated child must still be alive")
        release.touch()
        self._assert_windows_process_exited(int(ready.read_text()))

    def _start_windows_lock_reader(self, *, step_two=False, deny_all=False):
        hooks = f"""
import os, sys
from pathlib import Path
lock = Path({str(self.pending / "different.lock")!r})
printed = False
def create_file(fn, path, access, share, security, disposition, *rest):
    global printed
    is_lock = isinstance(path, (str, os.PathLike)) and Path(path) == lock
    if is_lock and ({deny_all!r} or ({step_two!r} and disposition == 4)):
        print("write-denied", file=sys.stderr, flush=True)
        ctypes.set_last_error(5)
        return ctypes.c_void_p(-1).value
    result = fn(path, access, share, security, disposition, *rest)
    if is_lock and not printed and ctypes.get_last_error() == 32 and result == ctypes.c_void_p(-1).value:
        printed = True
        print("share-retry", file=sys.stderr, flush=True)
    return result
hooks = {{"CreateFileW": create_file}}
"""
        return self._start_snapshot_script("", action="compare", bootstrap=self._windows_kernel_filter(hooks))

    @unittest.skipUnless(os.name == "nt", "Windows share-mode lock acquisition")
    def test_case_10_windows_preflight_and_read_only_open_retry_sharing_violation(self):
        for step_two in (False, True):
            with self.subTest(step_two=step_two):
                self._assert_windows_sharing_retry(step_two=step_two)

    @unittest.skipUnless(os.name == "nt", "Windows read-only share-mode lock acquisition")
    def test_case_33_windows_step_two_retries_sharing_violation(self):
        self._assert_windows_sharing_retry(step_two=True)

    @unittest.skipUnless(os.name == "nt", "Windows access-denied lock opens")
    def test_case_10_windows_denied_reader_refuses_while_writer_holds_name(self):
        for pending in (False, True):
            with self.subTest(pending=pending):
                self._seed_pair()
                holder = self._start_writer_for_denied_reader(pending=pending)
                try:
                    self.assertEqual(self._child_signal(holder), "ready")
                    reader = self._start_windows_lock_reader(deny_all=True)
                    out, err = reader.communicate(timeout=10)
                    self.assertEqual(err.count("write-denied"), 2, err)
                    self.assertEqual(json.loads(out).get("error"),
                                     "RECOVERY_REQUIRED" if pending else "LOCAL_STATE_UNAVAILABLE", out + err)
                    self.assertEqual(self.result.read_bytes(), self._OLD_RESULT)
                    self.assertEqual(self.spec.read_bytes(), self.old_spec_bytes)
                finally:
                    out, err = holder.communicate("\n", timeout=10)
                self.assertEqual(holder.returncode, 0, out + err)

    def _assert_windows_sharing_retry(self, *, step_two):
        self._seed_pair()
        holder = self._start_public_child("save", "hold-at-engine")
        self.addCleanup(self._stop_child, holder)
        self.assertEqual(self._child_signal(holder), "ready")
        contender = self._start_windows_lock_reader(step_two=step_two)
        if step_two:
            self.assertEqual(self._child_signal(contender), "write-denied")
        self.assertEqual(self._child_signal(contender), "share-retry")
        self.assertEqual(self.result.read_bytes(), self._OLD_RESULT)
        out, err = holder.communicate("\n", timeout=10)
        self.assertEqual(holder.returncode, 0, out + err)
        out, err = contender.communicate(timeout=10)
        self.assertEqual(contender.returncode, 0, out + err)
        self.assertEqual(json.loads(out)["queries"][0]["result"], "MATCH")
        self._assert_completed()

    @unittest.skipUnless(os.name == "nt", "Windows literal drain probe")
    def test_case_33_windows_drain_probe_excludes_contenders(self):
        from queue import Empty, Queue
        from threading import Thread

        self._seed_pair()
        absent = self._windows_probe()
        self.assertNotEqual(absent.returncode, 0)
        self.assertNotIn("press Enter", absent.stdout)
        bootstrap = self._windows_kernel_filter('hooks = {"SetInformationJobObject": lambda fn, *args: 1}')
        holder, ready, done, release = self._start_lifetime_holder("kill", bootstrap=bootstrap)
        self.assertEqual(self._child_signal(holder), "spawned")
        self.assertTrue(self._wait_for_snapshot_path(done.with_name("engine-received")))
        holder.kill()
        holder.wait(timeout=10)
        blocked = self._windows_probe()
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("error 32", blocked.stderr)
        self.assertNotIn("press Enter", blocked.stdout)
        release.touch()
        self._assert_windows_process_exited(int(ready.read_text()))
        probe = subprocess.Popen(self._windows_probe_command(), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._stop_child, probe)
        prompt = "lock held; press Enter to release"
        output = Queue()
        reader = Thread(target=lambda: output.put(probe.stdout.read(len(prompt))), daemon=True)
        reader.start()
        try:
            self.assertEqual(output.get(timeout=10), prompt)
        except Empty:
            probe.kill()
            reader.join(timeout=10)
            self.fail("drain probe did not report holding the lock")
        contender = self._windows_probe()
        self.assertNotEqual(contender.returncode, 0)
        self.assertIn("error 32", contender.stderr)
        out, err = probe.communicate("\n", timeout=10)
        self.assertEqual(probe.returncode, 0, out + err)
        self.assertEqual(self._windows_probe().returncode, 0)


    def _start_windows_engine_fault(self, mode, *, moved=False):
        ready = Path(self.ws.root) / "fault-child"
        eof = Path(self.ws.root) / "fault-eof"
        release = Path(self.ws.root) / "fault-release"
        clear_release = Path(self.ws.root) / "clear-release"
        child_script = """
import json, os, sys, time
from pathlib import Path
ready, eof, release, result = map(Path, sys.argv[1:])
ready.write_text(str(os.getpid()))
payload = sys.stdin.buffer.read()
if payload:
    json.loads(payload)
else:
    eof.touch()
while not release.exists():
    time.sleep(0.01)
if payload:
    result.write_text("unexpected engine write")
"""
        hooks = f"""
import sys, time
from pathlib import Path
mode = {mode!r}
toggles = []
assignments = []
def toggle(fn, handle, mask, flags):
    inheritable = bool(flags)
    toggles.append(inheritable)
    if mode == "toggle-before" and inheritable:
        Path({str(self.result)!r}).write_bytes({self._NEW_RESULT + b" "!r})
        ctypes.set_last_error(5)
        return 0
    if not inheritable and mode.startswith("toggle-clear"):
        clear_count = toggles.count(False)
        if clear_count == 1:
            print("clear-paused", file=sys.stderr, flush=True)
            release = Path({str(clear_release)!r})
            deadline = time.monotonic() + 10
            while not release.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not release.exists():
                raise RuntimeError("test did not release inherit-flag clear")
        if clear_count == 1 or mode == "toggle-clear-twice":
            ctypes.set_last_error(5)
            return 0
    return fn(handle, mask, flags)
def assign(fn, *args):
    assignments.append("attempt")
    if mode.startswith("assignment"):
        if {moved!r}:
            Path({str(self.result)!r}).write_bytes({self._NEW_RESULT + b" "!r})
        ctypes.set_last_error(5)
        return 0
    return fn(*args)
hooks = {{"SetHandleInformation": toggle, "AssignProcessToJobObject": assign}}
"""
        script = f"""
    import json, subprocess
    from pbz import checks, common, engine
    from pbz.common import PbzError, find_workspace
    request_workspace = find_workspace()
    real_popen = subprocess.Popen
    children, kills, communicated, responses = [], [], [], []
    def start(argv, **kwargs):
        child = real_popen([sys.executable, "-c", {child_script!r}, {str(ready)!r},
                            {str(eof)!r}, {str(release)!r}, {str(self.result)!r}], **kwargs)
        children.append(child)
        kill, communicate = child.kill, child.communicate
        def kill_child():
            kills.append(child.pid)
            if mode.endswith("unconfirmed"):
                raise OSError("injected termination fault")
            return kill()
        def send(input=None, timeout=None):
            communicated.append(True)
            return communicate(input, timeout=0.2)
        child.kill = child.terminate = kill_child
        child.communicate = send
        return child
    def observe(*args, **kwargs):
        response = common.engine_call(*args, **kwargs)
        responses.append(response)
        return response
    stack.enter_context(patch.object(engine, "ENGINE_EXE", sys.executable))
    stack.enter_context(patch.object(engine.subprocess, "Popen", side_effect=start))
    stack.enter_context(patch.object(checks, "engine_call", side_effect=observe))
    try:
        checks.snapshot(request_workspace, None, "save", "different", Suite._NEW_SPEC)
    except PbzError as error:
        outcome = [error.code, getattr(error.__cause__, "code", None)]
    else:
        outcome = ["OK", None]
    print(json.dumps({{"outcome": outcome, "responses": responses, "toggles": toggles,
                      "assignments": assignments, "kills": kills, "sent": communicated,
                      "children": [c.pid for c in children],
                      "returncodes": [c.returncode for c in children]}}), file=sys.stderr, flush=True)
    sys.stdin.readline()
    sys.exit(0)
"""
        child = self._start_snapshot_script(script, bootstrap=self._windows_kernel_filter(hooks))
        self.addCleanup(release.touch)
        self.addCleanup(clear_release.touch)
        return child, eof, release

    @unittest.skipUnless(os.name == "nt", "Windows native job assignment failure")
    def test_case_05_windows_assignment_failure_preserves_pair_and_cleans_journal(self):
        self._seed_pair()
        holder, _, _ = self._start_windows_engine_fault("assignment-confirmed")
        report = json.loads(self._child_signal(holder))
        self.assertEqual(report["outcome"][0], "ENGINE_UNAVAILABLE")
        self.assertEqual(report["responses"][0]["payload_state"], "not_sent")
        self.assertEqual(report["sent"], [])
        self.assertEqual(len(report["assignments"]), 1)
        self.assertIsNotNone(report["returncodes"][0])
        self.assertEqual(checks._load_state(self.ws)["snapshot_queries"], 1)
        self.assertEqual(self.result.read_bytes(), self._OLD_RESULT)
        self.assertEqual(self.spec.read_bytes(), self.old_spec_bytes)
        self.assertFalse(self.journal.exists())
        self.assertFalse(self.staged.exists())

    @unittest.skipUnless(os.name == "nt", "Windows not-sent job assignment failure")
    def test_case_26_windows_assignment_failure_clears_flag_for_moved_result(self):
        original = self._blocked_publication()
        holder, _, _ = self._start_windows_engine_fault("assignment-confirmed", moved=True)
        report = json.loads(self._child_signal(holder))
        self.assertEqual(report["outcome"], ["RECOVERY_REQUIRED", "ENGINE_UNAVAILABLE"])
        self.assertEqual(report["responses"][0]["payload_state"], "not_sent")
        self.assertEqual(report["sent"], [])
        self.assertIsNotNone(report["returncodes"][0])
        self.assertEqual(self.journal.read_bytes(), original)
        holder.communicate("\n", timeout=10)
        self._assert_class("RECOVERY_REQUIRED", self._save)
        self.engine.assert_not_called()
        self.assertEqual(self.result.read_bytes(), self._NEW_RESULT + b" ")

    @unittest.skipUnless(os.name == "nt", "Windows SetHandleInformation faults")
    def test_case_26_windows_inherit_toggle_failures(self):
        for mode in ("toggle-before", "toggle-clear-once", "toggle-clear-twice"):
            with self.subTest(mode=mode):
                self._assert_windows_toggle_failure(mode)

    def _assert_windows_toggle_failure(self, mode):
        for name in ("fault-child", "fault-eof", "fault-release", "clear-release"):
            (Path(self.ws.root) / name).unlink(missing_ok=True)
        original = self._blocked_publication()
        holder, _, _ = self._start_windows_engine_fault(mode)
        clear_release = Path(self.ws.root) / "clear-release"
        try:
            if mode != "toggle-before":
                self.assertEqual(self._child_signal(holder), "clear-paused")
                reader = self._start_windows_lock_reader()
                self.assertEqual(self._child_signal(reader), "share-retry")
                clear_release.touch()
            report = json.loads(self._child_signal(holder))
            self.assertEqual(report["responses"][0]["class"], "OTHER")
            self.assertEqual(report["responses"][0]["payload_state"], "not_sent")
            self.assertEqual(report["sent"], [])
            self.assertEqual(report["assignments"], [])
            self.assertEqual(self.journal.read_bytes(), original)
            if mode == "toggle-before":
                self.assertEqual(report["outcome"], ["RECOVERY_REQUIRED", "OTHER"])
                self.assertEqual(report["children"], [])
                self.assertEqual(self.result.read_bytes(), self._NEW_RESULT + b" ")
            else:
                self.assertEqual(report["outcome"][0], "OTHER")
                self.assertEqual(report["toggles"], [True, False, False])
                self.assertEqual(len(report["children"]), 1)
                self.assertIsNotNone(report["returncodes"][0])
                self.assertEqual(self.result.read_bytes(), self._NEW_RESULT)
                out, err = reader.communicate(timeout=10)
                self.assertEqual(json.loads(out).get("error"), "RECOVERY_REQUIRED", out + err)
        finally:
            clear_release.touch()
            holder.communicate("\n", timeout=10)
        if mode == "toggle-before":
            self._assert_class("RECOVERY_REQUIRED", self._save)
            self.engine.assert_not_called()
            self.assertEqual(self.result.read_bytes(), self._NEW_RESULT + b" ")
        else:
            self.assertEqual(self._windows_probe().returncode, 0)

    @unittest.skipUnless(os.name == "nt", "Windows cleanup after successful job assignment")
    def test_case_30_windows_unconfirmed_assigned_child_dies_on_job_close(self):
        self._assert_windows_assignment_cleanup(assigned=True)

    @unittest.skipUnless(os.name == "nt", "Windows cleanup before successful job assignment")
    def test_case_30_windows_unconfirmed_unassigned_child_gets_eof_while_holder_lives(self):
        self._assert_windows_assignment_cleanup(assigned=False)

    def _assert_windows_assignment_cleanup(self, *, assigned):
        original = self._blocked_publication()
        holder, eof, release = self._start_windows_engine_fault(
            "assigned-unconfirmed" if assigned else "assignment-unconfirmed"
        )
        try:
            report = json.loads(self._child_signal(holder))
            self.assertEqual(report["outcome"], ["RECOVERY_REQUIRED",
                                                "TIMEOUT" if assigned else "ENGINE_UNAVAILABLE"])
            self.assertEqual(report["responses"][0]["payload_state"], "unconfirmed")
            self.assertEqual(len(report["kills"]), 2)
            self.assertEqual(report["sent"], [True] if assigned else [])
            self._assert_annotations(original, superseding=True)
            retained = self.journal.read_bytes()
            self.assertEqual(checks._load_state(self.ws)["snapshot_queries"], 1)
            pid, = report["children"]
            if assigned:
                self._assert_windows_process_exited(pid)
            else:
                self.assertTrue(self._wait_for_snapshot_path(eof), "unassigned child must see explicit stdin EOF")
                self.assertIn("error 32", self._windows_probe().stderr)
                reader = self._start_windows_lock_reader()
                self.assertEqual(self._child_signal(reader), "share-retry")
                self.assertEqual(self.journal.read_bytes(), retained)
                release.touch()
                self._assert_windows_process_exited(pid)
                self.assertIsNone(holder.poll(), "parent must remain alive holding the Popen object")
                out, err = reader.communicate(timeout=10)
                self.assertEqual(json.loads(out).get("error"), "RECOVERY_REQUIRED", out + err)
            self.assertEqual(self.journal.read_bytes(), retained if assigned else original)
            self.assertEqual(self.result.read_bytes(), self._NEW_RESULT)
            self.assertEqual(self.spec.read_bytes(), self.old_spec_bytes)
            self.assertEqual(self._windows_probe().returncode, 0)
        finally:
            release.touch()
            holder.communicate("\n", timeout=10)


if __name__ == "__main__":
    unittest.main()
