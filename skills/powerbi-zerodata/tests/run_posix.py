"""Run the declared POSIX-only tests and validate their executed IDs and outcomes."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile
import time
import unittest

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))


REQUIRED_IDS = (
    'test_mcp.GitHookTests.test_chain_bytes_args_stdin_and_status',
    'test_mcp.GitHookTests.test_non_executable_host_is_ignored',
    'test_mcp.InstallerTransactionTests.test_atomic_text_preserves_mode_and_symlink',
    'test_mcp.InstallerTransactionTests.test_posix_lock_timeout_is_named_conflict',
    'test_mcp.InstallerTransactionTests.test_absent_noop_holder_removes_parents_before_waiter_retries',
    'test_mcp.InstallerTransactionTests.test_absent_noop_lock_unlinks_before_unlocking',
    'test_mcp.InstallerTransactionTests.test_absent_noop_lock_waiter_retries_replaced_inode',
    'test_mirror_recovery_honesty.MirrorEditJournalTests.test_recovery_guard_excludes_concurrent_recovery',
    'test_mirror_recovery_honesty.RecoveryHonestyTests.test_open_editor_save_during_successful_quarantine_return_is_preserved',
    'test_pbz_state_atomic.StateAtomicTests.test_allocator_path_swapped_during_acquisition_refused',
    'test_pbz_state_atomic.StateAtomicTests.test_allocator_posix_deadline',
    'test_pbz_state_atomic.StateAtomicTests.test_allocator_replaced_path_refused',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_10_denied_lock_open_refuses_reader',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_10_denied_reader_refuses_while_live_writer_holds_name',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_10_flock_enolck_falls_through_both_open_modes',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_10_read_only_recovery_write_failure_preserves_tree',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_10_same_name_saves_wait_for_compare_and_other_saves',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_10_step_two_reader_waits_for_writer',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_11_double_interrupt_closes_holder_but_preserves_child_lock',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_11_engine_child_keeps_lock_after_holder_death',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_11_posix_timed_out_child_is_reaped',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_11_timeout_names_class_after_kill_and_wait',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_13_cleanup_rechecks_live_spec_after_sync',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_13_clear_requires_successful_result_sync',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_13_failed_specs_sync_retains_journal',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_13_live_sync_failure_blocks_cleanup',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_13_save_specs_sync_failure_retains_journal',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_13_staged_unlink_failure_leaves_ignored_orphan',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_17_other_name_syncs_directory_chain_before_dispatch',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_17_result_directory_entry_synced_before_advancing_journal',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_17_snapshots_sync_precedes_journal_unlink_after_engine_creates_results',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_17_superseding_sync_failure_preserves_governing_journal',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_24_clearing_post_replace_sync_failure_uses_installed_bytes',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_24_clearing_rechecks_result_after_sync',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_26_clear_failure_falls_back_to_marker_or_retains_flag',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_26_native_communicate_exceptions',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_26_native_spawn_failure_and_timeout_distinguish_payload_state',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_27_absent_before_result_syncs_snapshots_parent',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_27_replaced_results_directory_requires_parent_sync',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_30_timeout_retries_child_kill_before_confirming_exit',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_30_unconfirmed_timeout_retains_prepared_journal',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_30_unmoved_supersede_runtime_error_leaves_flag',
    'test_snapshot_store.SnapshotPairPublicationTests.test_case_33_posix_drain_probe_excludes_contender',
    'test_snapshot_store.SnapshotPairPublicationTests.test_journal_without_read_permission_is_unreadable',
    'test_doctor_ci.DoctorCiTests.test_doctor_ci_skip_unreadable_evidence_dir_is_not_a_match',
)


class RecordingResult(unittest.TestResult):
    """Record one outcome per invocation, including parent subtest failures."""

    def __init__(self):
        super().__init__()
        self.records = []
        self.outcome = None

    def startTest(self, test):
        super().startTest(test)
        self.outcome = None

    def stopTest(self, test):
        self.records.append((test.id(), self.outcome or "error"))
        super().stopTest(test)

    def addSuccess(self, test):
        super().addSuccess(test)
        if self.outcome is None:
            self.outcome = "passed"

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.outcome = "failure"

    def addError(self, test, err):
        super().addError(test, err)
        self.outcome = "error"

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        if self.outcome is None:
            self.outcome = "skipped"

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.outcome = "error"

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.outcome = "error"

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.outcome = "failure" if issubclass(err[0], test.failureException) else "error"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="ci-results/posix.json")
    parser.add_argument("--tests", default=str(Path(__file__).with_name("posix_tests.json")))
    args = parser.parse_args(argv)
    started = time.monotonic()
    expected = []
    records = []
    error = None
    shim = None
    os.environ["PBZ_FAKE_DPAPI"] = "1"
    os.environ["PBZ_TEST_HARNESS"] = "1"
    try:
        shim = tempfile.mkdtemp(prefix="pbz-posix-pwsh-")
        pwsh = Path(shim, "pwsh")
        pwsh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        pwsh.chmod(0o755)
        os.environ["PATH"] = shim + os.pathsep + os.environ.get("PATH", "")
        try:
            import pbz.cli  # noqa: F401
        except Exception as exc:
            raise RuntimeError(f"pbz.cli import failed: {exc}") from exc
        declared = json.loads(Path(args.tests).read_text(encoding="utf-8"))["tests"]
        if not isinstance(declared, list) or not declared or not all(isinstance(i, str) for i in declared):
            raise ValueError("tests must be a nonempty list of importable test IDs")
        expected = declared
        if Counter(expected) != Counter(REQUIRED_IDS):
            raise ValueError("tests must contain exactly the required POSIX test IDs declared in REQUIRED_IDS, once each")
        for test_id in expected:
            loader = unittest.TestLoader()
            try:
                suite = loader.loadTestsFromName(test_id)
                if loader.errors:
                    records.append((test_id, "error"))
                    continue
                result = RecordingResult()
                suite.run(result)
                records.extend(result.records)
                # Class/module fixture failures may have no startTest/stopTest pair.
                if result.errors and not any(outcome == "error" for _, outcome in result.records):
                    error = "test fixture error"
                if result.skipped:
                    error = "test or fixture skipped"
            except Exception as exc:
                records.append((test_id, "error"))
                error = f"test execution failed: {exc}"
    except Exception as exc:
        error = str(exc)
    finally:
        if shim is not None:
            shutil.rmtree(shim)

    passed = (error is None and bool(expected)
              and Counter(i for i, _ in records) == Counter(expected)
              and all(outcome == "passed" for _, outcome in records))
    outcomes = defaultdict(deque)
    for test_id, outcome in records:
        outcomes[test_id].append(outcome)
    rows = [{"id": test_id, "outcome": outcomes[test_id].popleft() if outcomes[test_id] else "error"}
            for test_id in expected]
    report = {"schema": 1, "lane": "posix", "python": platform.python_version(),
              "os": platform.platform(), "seconds": time.monotonic() - started,
              "tests": rows, "passed": passed}
    if error is not None:
        report["error"] = error
    if Counter(i for i, _ in records) != Counter(expected):
        report["recorded"] = [{"id": i, "outcome": outcome} for i, outcome in records]
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    count = sum(outcome == "passed" for _, outcome in records)
    print(f"POSIX: {count}/{len(expected)} passed; validation {'passed' if passed else 'failed'}; report: {args.report}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
