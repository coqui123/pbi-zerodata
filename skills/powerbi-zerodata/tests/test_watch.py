"""Tests for pbz.watch change-aware validation runner: safe traversal, boundary checks, and zero-data invariants."""
from __future__ import annotations

import getpass
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import bpa, cli, common, guard, validate, watch
from pbz.common import PbzError


class DummyWorkspace:
    def __init__(self, root: str | None):
        self.root = root
        if root:
            self.mirror = os.path.join(root, "mirror")
            self.policy_path = os.path.join(root, ".pbz", "policy.json")
            self.pbz_dir = os.path.join(root, ".pbz")
            self.private = os.path.join(root, ".private")
        else:
            self.mirror = None
            self.policy_path = None
            self.pbz_dir = None
            self.private = None


class DummyProject:
    def __init__(self, root: str):
        self.mirror_model = os.path.join(root, "mirror", "model")
        self.mirror_report = os.path.join(root, "mirror", "report")


def _fake_stat(st: os.stat_result, **overrides) -> types.SimpleNamespace:
    attrs = {n: getattr(st, n) for n in dir(st) if n.startswith("st_")}
    attrs.update(overrides)
    return types.SimpleNamespace(**attrs)


class TestWatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "ws")
        os.makedirs(self.root, exist_ok=True)
        self.ws = DummyWorkspace(self.root)
        self.project = DummyProject(self.root)

        # Create basic directory structure
        os.makedirs(self.project.mirror_model, exist_ok=True)
        os.makedirs(self.project.mirror_report, exist_ok=True)
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        os.makedirs(self.ws.private, exist_ok=True)

        with open(os.path.join(self.project.mirror_model, "a.tmdl"), "wb") as f:
            f.write(b"table A\n")
        with open(self.ws.policy_path, "wb") as f:
            f.write(b'{"version": 1}\n')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Part 1: Core Watch Execution, Change Detection & CLI Options
    # -------------------------------------------------------------------------

    def test_watch_once(self):
        with patch.object(validate, "run_mirror", return_value=[{"severity": "warning", "message": "w"}]), \
             patch.object(bpa, "run", return_value={"model": {"findings": 2}, "report": {"findings": 1}}):
            out = watch.run(self.ws, self.project, once=True)
            self.assertEqual(out["verb"], "watch")
            self.assertTrue(out["once"])
            self.assertEqual(len(out["passes"]), 1)
            p = out["passes"][0]
            self.assertIsInstance(p["validate"], int)
            self.assertEqual(p["validate"], 1)
            self.assertIsInstance(p["validate_errors"], int)
            self.assertEqual(p["validate_errors"], 0)
            self.assertIsInstance(p["bpa"], int)
            self.assertEqual(p["bpa"], 3)

    @patch("time.sleep", return_value=None)
    def test_unchanged_tree_across_three_intervals_means_one_run(self, mock_sleep):
        val_calls = [0]
        def mock_val(ws, proj):
            val_calls[0] += 1
            return []

        with patch.object(validate, "run_mirror", side_effect=mock_val), \
             patch.object(bpa, "run", return_value={"model": {"findings": 0}}):
            out = watch.run(self.ws, self.project, seconds=1, intervals=3)
            self.assertEqual(val_calls[0], 1)
            self.assertEqual(len(out["passes"]), 1)
            self.assertEqual(mock_sleep.call_count, 2)

    @patch("time.sleep", return_value=None)
    def test_change_in_one_file_means_rerun(self, mock_sleep):
        val_calls = [0]
        def mock_val(ws, proj):
            val_calls[0] += 1
            return []

        def sleep_side_effect(sec):
            if mock_sleep.call_count == 1:
                with open(os.path.join(self.project.mirror_model, "a.tmdl"), "wb") as f:
                    f.write(b"table A_modified\n")

        mock_sleep.side_effect = sleep_side_effect

        with patch.object(validate, "run_mirror", side_effect=mock_val), \
             patch.object(bpa, "run", return_value={"model": {"findings": 0}}):
            out = watch.run(self.ws, self.project, seconds=1, intervals=2)
            self.assertEqual(val_calls[0], 2)
            self.assertEqual(len(out["passes"]), 2)

    @patch("time.sleep", return_value=None)
    def test_file_creation_and_deletion_mean_rerun(self, mock_sleep):
        val_calls = [0]
        def mock_val(ws, proj):
            val_calls[0] += 1
            return []

        new_file = os.path.join(self.project.mirror_model, "new.tmdl")

        def sleep_side_effect(sec):
            if mock_sleep.call_count == 1:
                with open(new_file, "wb") as f:
                    f.write(b"table New\n")
            elif mock_sleep.call_count == 2:
                os.remove(new_file)

        mock_sleep.side_effect = sleep_side_effect

        with patch.object(validate, "run_mirror", side_effect=mock_val), \
             patch.object(bpa, "run", return_value={"model": {"findings": 0}}):
            out = watch.run(self.ws, self.project, seconds=1, intervals=3)
            self.assertEqual(val_calls[0], 3)
            self.assertEqual(len(out["passes"]), 3)

    @patch("time.sleep", return_value=None)
    def test_empty_root_creation_and_deletion_mean_rerun(self, mock_sleep):
        val_calls = [0]
        def mock_val(ws, proj):
            val_calls[0] += 1
            return []

        new_dir = os.path.join(self.project.mirror_model, "empty_subdir")

        def sleep_side_effect(sec):
            if mock_sleep.call_count == 1:
                os.makedirs(new_dir, exist_ok=True)
            elif mock_sleep.call_count == 2:
                os.rmdir(new_dir)

        mock_sleep.side_effect = sleep_side_effect

        with patch.object(validate, "run_mirror", side_effect=mock_val), \
             patch.object(bpa, "run", return_value={"model": {"findings": 0}}):
            out = watch.run(self.ws, self.project, seconds=1, intervals=3)
            self.assertEqual(val_calls[0], 3)
            self.assertEqual(len(out["passes"]), 3)

    @patch("time.sleep", return_value=None)
    def test_change_in_policy_means_rerun(self, mock_sleep):
        val_calls = [0]
        def mock_val(ws, proj):
            val_calls[0] += 1
            return []

        def sleep_side_effect(sec):
            if mock_sleep.call_count == 1:
                with open(self.ws.policy_path, "wb") as f:
                    f.write(b'{"version": 2}\n')

        mock_sleep.side_effect = sleep_side_effect

        with patch.object(validate, "run_mirror", side_effect=mock_val), \
             patch.object(bpa, "run", return_value={"model": {"findings": 0}}):
            out = watch.run(self.ws, self.project, seconds=1, intervals=2)
            self.assertEqual(val_calls[0], 2)
            self.assertEqual(len(out["passes"]), 2)

    @patch("time.sleep", return_value=None)
    def test_change_in_rules_means_rerun(self, mock_sleep):
        val_calls = [0]
        def mock_val(ws, proj):
            val_calls[0] += 1
            return []

        rules_dir = os.path.join(self.root, "rules")
        os.makedirs(rules_dir, exist_ok=True)

        def sleep_side_effect(sec):
            if mock_sleep.call_count == 1:
                with open(os.path.join(rules_dir, "custom.json"), "wb") as f:
                    f.write(b'{"rule": "r1"}\n')

        mock_sleep.side_effect = sleep_side_effect

        with patch.object(validate, "run_mirror", side_effect=mock_val), \
             patch.object(bpa, "run", return_value={"model": {"findings": 0}}):
            out = watch.run(self.ws, self.project, seconds=1, intervals=2)
            self.assertEqual(val_calls[0], 2)
            self.assertEqual(len(out["passes"]), 2)

    @patch("time.sleep", return_value=None)
    def test_rescan_forces_rerun_even_if_tree_unchanged(self, mock_sleep):
        val_calls = [0]
        def mock_val(ws, proj):
            val_calls[0] += 1
            return []

        with patch.object(validate, "run_mirror", side_effect=mock_val), \
             patch.object(bpa, "run", return_value={"model": {"findings": 0}}):
            out = watch.run(self.ws, self.project, seconds=1, rescan=True, intervals=3)
            self.assertEqual(val_calls[0], 3)
            self.assertEqual(len(out["passes"]), 3)

    @patch("time.sleep", return_value=None)
    def test_history_stays_at_most_n_and_retains_newest(self, mock_sleep):
        counter = [0]
        def mock_val(ws, proj):
            counter[0] += 1
            return [{"severity": "info"}] * counter[0]

        def sleep_side_effect(sec):
            with open(os.path.join(self.project.mirror_model, "a.tmdl"), "wb") as f:
                f.write(f"table A_{mock_sleep.call_count}\n".encode("utf-8"))

        mock_sleep.side_effect = sleep_side_effect

        with patch.object(validate, "run_mirror", side_effect=mock_val), \
             patch.object(bpa, "run", return_value={"model": {"findings": 0}}):
            out = watch.run(self.ws, self.project, seconds=1, max_passes=3, intervals=5)
            self.assertEqual(len(out["passes"]), 3)
            self.assertEqual([p["validate"] for p in out["passes"]], [3, 4, 5])

    @patch("time.sleep", return_value=None)
    def test_transient_scan_failed_in_polling_skips_interval(self, mock_sleep):
        calls = [0]
        real_tree_digest = watch.tree_digest

        def flaking_digest(ws, proj):
            calls[0] += 1
            if calls[0] == 3:
                # Pre-scan of interval 2 fails transiently
                raise PbzError("SCAN_FAILED", "filesystem scan failed")
            return real_tree_digest(ws, proj)

        with patch.object(watch, "tree_digest", side_effect=flaking_digest), \
             patch.object(validate, "run_mirror", return_value=[]) as mock_val, \
             patch.object(bpa, "run", return_value={"model": {"findings": 0}}):
            out = watch.run(self.ws, self.project, seconds=1, intervals=3)
            self.assertEqual(len(out["passes"]), 1)
            mock_val.assert_called_once()
            self.assertEqual(mock_sleep.call_count, 2)

    @patch("time.sleep", return_value=None)
    def test_persistent_scan_failure_escalates_after_max_retries(self, mock_sleep):
        with patch.object(watch, "tree_digest", side_effect=PbzError("SCAN_FAILED", "filesystem scan failed")):
            with self.assertRaises(PbzError) as ctx:
                watch.run(self.ws, self.project, seconds=1, intervals=12)
            self.assertEqual(ctx.exception.code, "SCAN_FAILED")
            self.assertEqual(ctx.exception.detail, "exceeded maximum consecutive scan failures")
            self.assertEqual(mock_sleep.call_count, 9)

    @patch("time.sleep", return_value=None)
    def test_persistent_post_validation_scan_failure_escalates_after_max_retries(self, mock_sleep):
        calls = [0]
        real_tree_digest = watch.tree_digest

        def flaking_digest(ws, proj):
            calls[0] += 1
            if calls[0] % 2 == 0:
                # Every post-validation scan fails
                raise PbzError("SCAN_FAILED", "filesystem scan failed")
            # Every pre-validation scan succeeds
            return real_tree_digest(ws, proj)

        with patch.object(watch, "tree_digest", side_effect=flaking_digest), \
             patch.object(validate, "run_mirror", return_value=[]) as mock_val, \
             patch.object(bpa, "run", return_value={"model": {"findings": 0}}):
            with self.assertRaises(PbzError) as ctx:
                watch.run(self.ws, self.project, seconds=1, intervals=12)
            self.assertEqual(ctx.exception.code, "SCAN_FAILED")
            self.assertEqual(ctx.exception.detail, "exceeded maximum consecutive scan failures")
            self.assertEqual(mock_val.call_count, 10)
            self.assertEqual(mock_sleep.call_count, 9)

    def test_keyboard_interrupt_propagates_in_once_mode(self):
        with patch.object(watch, "tree_digest", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                watch.run(self.ws, self.project, once=True)

    @patch("time.sleep", return_value=None)
    def test_keyboard_interrupt_returns_collected_passes(self, mock_sleep):
        with patch.object(validate, "run_mirror", return_value=[]), \
             patch.object(bpa, "run", return_value={"model": {"findings": 0}}), \
             patch("time.sleep", side_effect=KeyboardInterrupt):
            out = watch.run(self.ws, self.project, seconds=1)
            self.assertEqual(len(out["passes"]), 1)

    @patch("time.sleep", return_value=None)
    def test_unsafe_path_fatal_in_polling_mode(self, mock_sleep):
        with patch.object(watch, "tree_digest", side_effect=PbzError("UNSAFE_PATH", "refusing to scan link")), \
             patch.object(validate, "run_mirror") as mock_val:
            with self.assertRaises(PbzError) as ctx:
                watch.run(self.ws, self.project, seconds=1)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            mock_val.assert_not_called()
            mock_sleep.assert_not_called()

    @patch("time.sleep", return_value=None)
    def test_unsafe_path_fatal_post_validation_in_polling_mode(self, mock_sleep):
        digest_calls = [0]
        real_digest = watch.tree_digest

        def flaking_digest(ws, proj):
            digest_calls[0] += 1
            if digest_calls[0] == 2:
                # Post-validation digest check raises UNSAFE_PATH
                raise PbzError("UNSAFE_PATH", "intermediate ancestor replaced during scan")
            return real_digest(ws, proj)

        with patch.object(watch, "tree_digest", side_effect=flaking_digest), \
             patch.object(validate, "run_mirror", return_value=[]), \
             patch.object(bpa, "run", return_value={"model": {"findings": 0}}):
            with self.assertRaises(PbzError) as ctx:
                watch.run(self.ws, self.project, seconds=1, intervals=2)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            mock_sleep.assert_not_called()

    @patch("time.sleep", return_value=None)
    def test_validation_pass_consistency_discard_on_input_mismatch(self, mock_sleep):
        # 1. Once mode: raises SCAN_FAILED on post-validation mismatch
        val_calls = [0]
        def mutating_val(ws, proj):
            val_calls[0] += 1
            with open(os.path.join(self.project.mirror_model, "a.tmdl"), "wb") as f:
                f.write(f"table Mutated_{val_calls[0]}\n".encode("utf-8"))
            return []

        with patch.object(validate, "run_mirror", side_effect=mutating_val), \
             patch.object(bpa, "run", return_value={"model": {"findings": 0}}):
            with self.assertRaises(PbzError) as ctx:
                watch.run(self.ws, self.project, once=True)
            self.assertEqual(ctx.exception.code, "SCAN_FAILED")
            self.assertEqual(ctx.exception.detail, "workspace modified during validation pass")

        # 2. Polling multi-interval test (3 intervals with populated baseline):
        # Interval 1: Workspace has state A. poll_val leaves A unchanged.
        #             Pass 1 committed with validate=1. last_digest is populated with digest(A).
        # First sleep: Changes workspace to state B.
        # Interval 2: Pre-scan sees B. poll_val mutates B to state C during validation.
        #             Post-scan sees mismatch digest(C) != digest(B). Candidate discarded!
        #             last_digest is explicitly cleared to None!
        # Second sleep: Restores workspace to state A.
        # Interval 3: Pre-scan sees A.
        #             CRITICAL CONTROL: If last_digest had NOT been cleared (retaining digest(A)),
        #             interval 3 would see curr_digest == last_digest and SKIP validation!
        #             Because last_digest = None was executed, interval 3 runs poll_val and commits pass 2.
        poll_calls = [0]
        a_path = os.path.join(self.project.mirror_model, "a.tmdl")
        with open(a_path, "wb") as f:
            f.write(b"table State_A\n")

        def poll_val(ws, proj):
            poll_calls[0] += 1
            if poll_calls[0] == 2:
                # During interval 2 validation, mutate B to C to trigger mismatch
                with open(a_path, "wb") as f:
                    f.write(b"table State_C\n")
            return [{"severity": "info"}] * poll_calls[0]

        def sleep_manager(sec):
            if mock_sleep.call_count == 1:
                # First sleep: introduce state B
                with open(a_path, "wb") as f:
                    f.write(b"table State_B\n")
            elif mock_sleep.call_count == 2:
                # Second sleep: restore state A
                with open(a_path, "wb") as f:
                    f.write(b"table State_A\n")

        mock_sleep.side_effect = sleep_manager

        with patch.object(validate, "run_mirror", side_effect=poll_val), \
             patch.object(bpa, "run", return_value={"model": {"findings": 0}}):
            out = watch.run(self.ws, self.project, intervals=3)
            # Exactly 2 passes committed (from call 1 and call 3; call 2 discarded)
            self.assertEqual(len(out["passes"]), 2)
            self.assertEqual(poll_calls[0], 3)
            self.assertEqual([p["validate"] for p in out["passes"]], [1, 3])
            self.assertEqual(mock_sleep.call_count, 2)

    def test_relative_path_encoding_no_collision(self):
        enc1 = watch._encode_rel_path(os.path.join("a", "b:c"))
        enc2 = watch._encode_rel_path(os.path.join("a:b", "c"))
        self.assertNotEqual(enc1, enc2)

    @patch("time.sleep", return_value=None)
    def test_cli_options_validation_and_dispatch(self, mock_sleep):
        # 1. Invalid seconds bounds
        with self.assertRaises(SystemExit) as ctx:
            cli.main(["watch", "--seconds", "0"])
        self.assertEqual(ctx.exception.code, 2)

        with self.assertRaises(SystemExit) as ctx:
            cli.main(["watch", "--seconds", "3601"])
        self.assertEqual(ctx.exception.code, 2)

        # 2. Invalid history bounds
        with self.assertRaises(SystemExit) as ctx:
            cli.main(["watch", "--history", "0"])
        self.assertEqual(ctx.exception.code, 2)

        with self.assertRaises(SystemExit) as ctx:
            cli.main(["watch", "--history", "1001"])
        self.assertEqual(ctx.exception.code, 2)

        # 3. Non-integer and out-of-range input must never echo supplied sensitive argument
        buf_err = io.StringIO()
        with redirect_stderr(buf_err), self.assertRaises(SystemExit) as ctx:
            cli.main(["watch", "--history", "SECRET_SENTINEL_XYZ"])
        self.assertEqual(ctx.exception.code, 2)
        err_msg = buf_err.getvalue()
        self.assertIn("expected an integer", err_msg)
        self.assertNotIn("SECRET_SENTINEL_XYZ", err_msg)

        buf_err2 = io.StringIO()
        with redirect_stderr(buf_err2), self.assertRaises(SystemExit) as ctx:
            cli.main(["watch", "--history", "987654321"])
        self.assertEqual(ctx.exception.code, 2)
        err_msg2 = buf_err2.getvalue()
        self.assertIn("value must be between 1 and 1000", err_msg2)
        self.assertNotIn("987654321", err_msg2)

        # Create dummy .pbip in root so cli discovery succeeds
        dummy_pbip = os.path.join(self.root, "Model.pbip")
        with open(dummy_pbip, "w") as f:
            f.write("{}")

        # 4. Argument forwarding to watch.run (and boundary acceptance 1, 1000, 3600)
        with patch.object(watch, "run", return_value={"verb": "watch", "once": True, "passes": []}) as mock_run:
            rc = cli.main(["--workspace", self.root, "watch", "--once", "--rescan", "--history", "1000", "--seconds", "3600"])
            self.assertEqual(rc, 0)
            mock_run.assert_called_once()
            _, kwargs = mock_run.call_args
            self.assertTrue(kwargs["once"])
            self.assertTrue(kwargs["rescan"])
            self.assertEqual(kwargs["max_passes"], 1000)
            self.assertEqual(kwargs["seconds"], 3600)

        with patch.object(watch, "run", return_value={"verb": "watch", "once": True, "passes": []}) as mock_run2:
            rc = cli.main(["--workspace", self.root, "watch", "--once", "--history", "1", "--seconds", "1"])
            self.assertEqual(rc, 0)
            _, kwargs = mock_run2.call_args
            self.assertEqual(kwargs["max_passes"], 1)
            self.assertEqual(kwargs["seconds"], 1)

    # -------------------------------------------------------------------------
    # Part 2: Safe Traversal, Reparse Rejection, Ancestor Pinning & Zero-Data
    # -------------------------------------------------------------------------

    def test_containment_valueerror_maps_to_unsafe_path_without_chaining(self):
        with patch.object(os.path, "relpath", side_effect=ValueError("mixed drives")), \
             patch.object(validate, "run_mirror") as mock_val:
            with self.assertRaises(PbzError) as ctx:
                watch.run(self.ws, self.project, once=True)
        self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
        self.assertEqual(ctx.exception.detail, "path outside workspace")
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)
        mock_val.assert_not_called()

    def test_fail_closed_incomplete_scan_does_not_call_validation(self):
        with patch.object(common, "read_checked", side_effect=OSError("disk read error")), \
             patch.object(validate, "run_mirror") as mock_val:
            with self.assertRaises(PbzError) as ctx:
                watch.run(self.ws, self.project, once=True)
            self.assertEqual(ctx.exception.code, "SCAN_FAILED")
            self.assertEqual(ctx.exception.detail, "filesystem scan failed")
            self.assertIsNone(ctx.exception.__cause__)
            self.assertTrue(ctx.exception.__suppress_context__)
            mock_val.assert_not_called()

    def test_zero_data_error_tokens_never_leak_paths_or_raw_errors(self):
        # Create .pbip and mirror model so cli discovery and project initialization succeed
        pbip_path = os.path.join(self.root, "Model.pbip")
        with open(pbip_path, "w", encoding="utf-8") as f:
            f.write("{}")

        mirror_model = os.path.join(self.root, common.MIRROR_DIR, "Model.SemanticModel")
        os.makedirs(mirror_model, exist_ok=True)
        tmdl_file = os.path.join(mirror_model, "model.tmdl")
        with open(tmdl_file, "wb") as f:
            f.write(b"model Model\n")

        # 1. Force SCAN_FAILED via read error
        with patch.object(common, "read_checked", side_effect=OSError("private/secret_path/leak.txt: Permission denied")) as mock_rc:
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with redirect_stdout(buf_out), redirect_stderr(buf_err):
                rc = cli.main(["--workspace", self.root, "watch", "--once"])
            self.assertEqual(rc, 2)
            mock_rc.assert_called()
            out_str = buf_out.getvalue() + buf_err.getvalue()
            # Assert exact error code and constant detail token are emitted
            self.assertIn('"code": "SCAN_FAILED"', out_str)
            self.assertIn('"detail": "filesystem scan failed"', out_str)
            # Assert no path, username, or raw error text is emitted
            self.assertNotIn("secret_path", out_str)
            self.assertNotIn("Permission denied", out_str)
            self.assertNotIn(self.root, out_str)
            self.assertNotIn(os.path.basename(self.tmp), out_str)
            current_user = getpass.getuser()
            if current_user and len(current_user) > 2:
                self.assertNotIn(current_user, out_str)

        # 2. Force UNSAFE_PATH via link on specific mirror target
        real_islink = watch._is_any_link
        def link_target(p):
            if os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(tmdl_file)):
                return True
            return real_islink(p)

        with patch.object(watch, "_is_any_link", side_effect=link_target):
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with redirect_stdout(buf_out), redirect_stderr(buf_err):
                rc = cli.main(["--workspace", self.root, "watch", "--once"])
            self.assertEqual(rc, 2)
            out_str = buf_out.getvalue() + buf_err.getvalue()
            self.assertIn('"code": "UNSAFE_PATH"', out_str)
            self.assertIn('"detail": "refusing to scan link"', out_str)
            self.assertNotIn(self.root, out_str)
            self.assertNotIn(os.path.basename(self.tmp), out_str)
            if current_user and len(current_user) > 2:
                self.assertNotIn(current_user, out_str)

    def test_zero_data_contract_digest_and_contents_not_leaked_or_persisted(self):
        secret_sentinel = b"CONFIDENTIAL_ROW_DATA_SENTINEL_xyz123"
        secret_file = os.path.join(self.project.mirror_model, "secret.tmdl")
        with open(secret_file, "wb") as f:
            f.write(secret_sentinel)

        def _dir_snapshot(d: str) -> dict[str, tuple[int, str]]:
            snapshot: dict[str, tuple[int, str]] = {}
            for r, dirs, files in os.walk(d):
                for f in files:
                    p = os.path.join(r, f)
                    rel = os.path.relpath(p, d)
                    with open(p, "rb") as fp:
                        h = hashlib.sha256(fp.read()).hexdigest()
                    snapshot[rel] = (os.path.getsize(p), h)
                for dir_name in dirs:
                    rel_dir = os.path.relpath(os.path.join(r, dir_name), d)
                    snapshot[rel_dir + os.sep] = (0, "DIR")
            return snapshot

        ws_before = _dir_snapshot(self.root)
        priv_before = _dir_snapshot(self.ws.private)

        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err), \
             patch.object(validate, "run_mirror", return_value=[]), \
             patch.object(bpa, "run", return_value={"model": {"findings": 0}}):
            out = watch.run(self.ws, self.project, once=True)

        # Take after-snapshot immediately after watch.run()
        ws_after = _dir_snapshot(self.root)
        priv_after = _dir_snapshot(self.ws.private)

        # 1. Zero persistent file creations, deletions, or modifications
        self.assertEqual(ws_before, ws_after)
        self.assertEqual(priv_before, priv_after)

        # 2. File contents never leaked into returned data, stdout, or stderr
        out_str = json.dumps(out) + buf_out.getvalue() + buf_err.getvalue()
        self.assertNotIn("CONFIDENTIAL_ROW_DATA", out_str)
        self.assertNotIn("xyz123", out_str)

        # 3. Workspace tree digest never leaked into returned data, stdout, or stderr
        computed_digest = watch.tree_digest(self.ws, self.project)
        self.assertNotIn(computed_digest, out_str)

    def test_dangling_policy_symlink_is_rejected(self):
        dangling_policy = os.path.join(self.ws.pbz_dir, "policy.json")
        try:
            os.symlink(os.path.join(self.tmp, "nonexistent"), dangling_policy)
        except OSError:
            norm_policy = os.path.normcase(os.path.abspath(dangling_policy))
            checked_paths = []
            real_islink = watch._is_any_link
            real_guard_link = guard._is_link

            def mock_islink(p, *a, **k):
                checked_paths.append(os.path.normcase(os.path.abspath(p)))
                if os.path.normcase(os.path.abspath(p)) == norm_policy:
                    return True
                return real_islink(p, *a, **k)

            def mock_guard_link(p, *a, **k):
                checked_paths.append(os.path.normcase(os.path.abspath(p)))
                if os.path.normcase(os.path.abspath(p)) == norm_policy:
                    return True
                return real_guard_link(p, *a, **k)

            with patch.object(watch, "_is_any_link", side_effect=mock_islink), \
                 patch.object(guard, "_is_link", side_effect=mock_guard_link):
                with self.assertRaises(PbzError) as ctx:
                    watch.tree_digest(self.ws, self.project)
                self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
                self.assertEqual(ctx.exception.detail, "refusing to scan link")
                self.assertIn(norm_policy, checked_paths)
                return

        with self.assertRaises(PbzError) as ctx:
            watch.tree_digest(self.ws, self.project)
        self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
        self.assertEqual(ctx.exception.detail, "refusing to scan link")

    def test_ancestor_symlink_within_workspace_is_rejected(self):
        nested_dir = os.path.join(self.root, "dir_a", "dir_b")
        os.makedirs(nested_dir, exist_ok=True)
        leaf = os.path.join(nested_dir, "leaf.tmdl")
        with open(leaf, "wb") as f:
            f.write(b"content")

        dir_a = os.path.join(self.root, "dir_a")
        real_islink = watch._is_any_link
        def mock_islink(p, *a, **k):
            if os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(dir_a)):
                return True
            return real_islink(p)

        with patch.object(watch, "_is_any_link", side_effect=mock_islink):
            # 1. Existing leaf rejected
            with self.assertRaises(PbzError) as ctx:
                watch._check_containment_and_ancestors(leaf, stop_dir=self.root)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "refusing to scan link")

            # 2. Optional non-existent leaf rejected when ancestor is a link
            opt_leaf = os.path.join(nested_dir, "missing.tmdl")
            with self.assertRaises(PbzError) as ctx:
                watch._check_containment_and_ancestors(opt_leaf, stop_dir=self.root, optional=True)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "refusing to scan link")

    def test_optional_missing_path_with_linked_ancestor_is_rejected(self):
        # GPT Astra Blocker 1: Existing link at redirected, with missing_parent/model absent.
        # Top-down ancestor walk verifies redirected is a link and raises UNSAFE_PATH.
        redirected = os.path.join(self.root, "redirected")
        os.makedirs(redirected, exist_ok=True)
        missing_target = os.path.join(redirected, "missing_parent", "model")

        real_islink = watch._is_any_link
        def mock_islink(p, *a, **k):
            if os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(redirected)):
                return True
            return real_islink(p)

        with patch.object(watch, "_is_any_link", side_effect=mock_islink):
            with self.assertRaises(PbzError) as ctx:
                watch._check_containment_and_ancestors(missing_target, stop_dir=self.root, optional=True)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "refusing to scan link")

    def test_parent_above_workspace_link_does_not_block_workspace(self):
        # Claude Fable B2: build norm_parent from self.ws.root, spy verifies parent is never queried
        norm_parent = os.path.normcase(os.path.dirname(os.path.abspath(self.ws.root)))
        queried_paths: list[str] = []
        real_islink = watch._is_any_link

        def spy_islink(p, *a, **k):
            abs_p = os.path.normcase(os.path.abspath(p))
            queried_paths.append(abs_p)
            if abs_p == norm_parent:
                return True
            return real_islink(p)

        with patch.object(watch, "_is_any_link", side_effect=spy_islink):
            leaf = os.path.join(self.project.mirror_model, "a.tmdl")
            # stop_dir=self.ws.root must not check above workspace
            checked = watch._check_containment_and_ancestors(leaf, stop_dir=self.ws.root)
            self.assertEqual(checked, os.path.abspath(leaf))
            self.assertNotIn(norm_parent, queried_paths)

            # Positive control: if stop_dir is above the link, UNSAFE_PATH is raised!
            higher_stop = os.path.dirname(norm_parent)
            with self.assertRaises(PbzError) as ctx:
                watch._check_containment_and_ancestors(leaf, stop_dir=higher_stop)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "refusing to scan link")

    def test_filesystem_root_containment(self):
        # Claude Fable B1: cross-platform drive-relative root with guard._is_link patched to False
        descendant = os.path.abspath(self.root)
        root_dir = os.path.splitdrive(descendant)[0] + os.sep
        with patch.object(guard, "_is_link", return_value=False), \
             patch.object(watch, "_is_any_link", return_value=False):
            checked = watch._check_containment_and_ancestors(descendant, stop_dir=root_dir)
            self.assertEqual(checked, descendant)

    def test_traversal_root_replacement_is_rejected(self):
        root_file = os.path.join(self.project.mirror_model, "a.tmdl")
        with open(root_file, "wb") as f:
            f.write(b"table RootTest\n")

        armed = [False]
        real_read = common.read_checked
        real_lstat = os.lstat

        def spy_read(p, *a, **k):
            data = real_read(p, *a, **k)
            if os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(root_file)):
                armed[0] = True
            return data

        def spy_lstat(path, *a, **k):
            res = real_lstat(path, *a, **k)
            if armed[0] and os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(self.project.mirror_model)):
                return _fake_stat(res, st_ino=res.st_ino + 9999)
            return res

        with patch.object(common, "read_checked", side_effect=spy_read), \
             patch("os.lstat", side_effect=spy_lstat):
            with self.assertRaises(PbzError) as ctx:
                watch.tree_digest(self.ws, self.project)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "directory modified or replaced during traversal")

    def test_traversal_child_directory_replacement_is_rejected(self):
        child_dir = os.path.join(self.project.mirror_model, "child")
        os.makedirs(child_dir, exist_ok=True)
        with open(os.path.join(child_dir, "b.tmdl"), "wb") as f:
            f.write(b"table B\n")

        real_lstat = os.lstat
        stat_calls = [0]

        def spy_lstat(path, *a, **k):
            res = real_lstat(path)
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(child_dir)):
                stat_calls[0] += 1
                if stat_calls[0] > 1:
                    return _fake_stat(res, st_ino=res.st_ino + 9999)
            return res

        with patch("os.lstat", side_effect=spy_lstat):
            with self.assertRaises(PbzError) as ctx:
                watch.tree_digest(self.ws, self.project)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "directory modified or replaced during traversal")

    def test_child_directory_replacement_after_visit_is_rejected(self):
        dir_a = os.path.join(self.project.mirror_model, "dir_a")
        dir_z = os.path.join(self.project.mirror_model, "dir_z")
        os.makedirs(dir_a, exist_ok=True)
        os.makedirs(dir_z, exist_ok=True)
        file_a = os.path.join(dir_a, "a.tmdl")
        file_z = os.path.join(dir_z, "z.tmdl")
        with open(file_a, "wb") as f:
            f.write(b"table A\n")
        with open(file_z, "wb") as f:
            f.write(b"table Z\n")

        armed = [False]
        reads_completed: list[str] = []
        real_read_checked = common.read_checked
        real_lstat = os.lstat

        def spy_read_checked(p, *a, **k):
            data = real_read_checked(p, *a, **k)
            if os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(file_a)):
                reads_completed.append("read_a")
            elif os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(file_z)):
                reads_completed.append("read_z")
            if len(reads_completed) == 2:
                # Arm mutation only after both expected directory reads have completed
                armed[0] = True
            return data

        def spy_lstat(path, *a, **k):
            st = real_lstat(path)
            if armed[0] and os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(dir_a)):
                return _fake_stat(st, st_ino=st.st_ino + 7777)
            return st

        with patch.object(common, "read_checked", side_effect=spy_read_checked), \
             patch("os.lstat", side_effect=spy_lstat):
            with self.assertRaises(PbzError) as ctx:
                watch.tree_digest(self.ws, self.project)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "directory modified or replaced during traversal")
            self.assertEqual(sorted(reads_completed), ["read_a", "read_z"])

    def test_workspace_root_junction_allowed_but_replacement_rejected(self):
        # 1. Allowed at entry
        real_islink = watch._is_any_link
        def mock_islink_root(p, *a, **k):
            if os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(self.root)):
                return True
            return real_islink(p)

        with patch.object(watch, "_is_any_link", side_effect=mock_islink_root):
            d = watch.tree_digest(self.ws, self.project)
            self.assertIsInstance(d, str)

        # 2. Mid-scan inode replacement rejected
        real_lstat = os.lstat
        stat_calls = [0]
        def spy_lstat(path, *a, **k):
            res = real_lstat(path)
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(self.root)):
                stat_calls[0] += 1
                if stat_calls[0] > 1:
                    return _fake_stat(res, st_ino=res.st_ino + 8888)
            return res

        with patch("os.lstat", side_effect=spy_lstat):
            with self.assertRaises(PbzError) as ctx:
                watch.tree_digest(self.ws, self.project)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "workspace root replaced during scan")

    def test_workspace_root_link_status_change_rejected(self):
        # Link status changes mid-scan while dev/ino remains identical
        real_islink = watch._is_any_link
        islink_calls = [0]
        def flipping_islink(p, *a, **k):
            if os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(self.root)):
                islink_calls[0] += 1
                # First check at entry returns False, later check at sweep returns True
                if islink_calls[0] > 1:
                    return True
            return real_islink(p)

        with patch.object(watch, "_is_any_link", side_effect=flipping_islink):
            with self.assertRaises(PbzError) as ctx:
                watch.tree_digest(self.ws, self.project)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "workspace root replaced during scan")

    def test_intermediate_ancestor_replacement_during_scan_is_rejected(self):
        nested_model = os.path.join(self.root, "subgroup", "model")
        os.makedirs(nested_model, exist_ok=True)
        self.project.mirror_model = nested_model
        sub_file = os.path.join(nested_model, "sub.tmdl")
        with open(sub_file, "wb") as f:
            f.write(b"table Sub\n")

        subgroup = os.path.join(self.root, "subgroup")
        armed = [False]
        real_read = common.read_checked
        real_lstat = os.lstat

        def spy_read(p, *a, **k):
            data = real_read(p, *a, **k)
            if os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(sub_file)):
                armed[0] = True
            return data

        def spy_lstat(path, *a, **k):
            res = real_lstat(path)
            if armed[0] and os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(subgroup)):
                return _fake_stat(res, st_ino=res.st_ino + 5555)
            return res

        with patch.object(common, "read_checked", side_effect=spy_read), \
             patch("os.lstat", side_effect=spy_lstat):
            with self.assertRaises(PbzError) as ctx:
                watch.tree_digest(self.ws, self.project)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "intermediate ancestor replaced during scan")

    def test_shared_intermediate_ancestor_replacement_before_subsequent_input_is_rejected(self):
        # GPT Astra Blocker 2 & Gemini Neon Blocker:
        # shared/model and shared/report share intermediate ancestor 'shared'.
        # Replace 'shared' after model traversal has fully completed, at the exact
        # boundary where report inspection begins.
        # Must raise UNSAFE_PATH immediately without overwriting the original pin!
        shared_dir = os.path.join(self.root, "shared")
        model_dir = os.path.join(shared_dir, "model")
        report_dir = os.path.join(shared_dir, "report")
        os.makedirs(model_dir, exist_ok=True)
        os.makedirs(report_dir, exist_ok=True)
        self.project.mirror_model = model_dir
        self.project.mirror_report = report_dir

        file_m = os.path.join(model_dir, "m.tmdl")
        file_r = os.path.join(report_dir, "r.json")
        with open(file_m, "wb") as f:
            f.write(b"table M\n")
        with open(file_r, "wb") as f:
            f.write(b'{"report": 1}\n')

        model_completed = [False]
        report_reached = [False]
        real_inspect = watch._inspect_path
        real_read = common.read_checked
        real_lstat = os.lstat
        read_files = []

        def spy_read(p, *a, **k):
            read_files.append(os.path.normcase(os.path.abspath(p)))
            return real_read(p, *a, **k)

        def spy_inspect(p, expect_dir, optional=False, stop_dir=None, ancestor_identities=None, **kwargs):
            if os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(report_dir)):
                # Verified boundary: model scan has completed all its checks and report inspection has been reached
                model_completed[0] = True
                report_reached[0] = True
            return real_inspect(p, expect_dir, optional=optional, stop_dir=stop_dir, ancestor_identities=ancestor_identities, **kwargs)

        def spy_lstat(path, *a, **k):
            st = real_lstat(path, *a, **k)
            # Mutate shared_dir ONLY after model traversal has completed and report inspection starts
            if model_completed[0] and os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(shared_dir)):
                return _fake_stat(st, st_ino=st.st_ino + 4321)
            return st

        with patch.object(watch, "_inspect_path", side_effect=spy_inspect), \
             patch.object(common, "read_checked", side_effect=spy_read), \
             patch("os.lstat", side_effect=spy_lstat):
            with self.assertRaises(PbzError) as ctx:
                watch.tree_digest(self.ws, self.project)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "intermediate ancestor replaced during scan")
            self.assertTrue(model_completed[0])
            self.assertTrue(report_reached[0])
            self.assertIn(os.path.normcase(os.path.abspath(file_m)), read_files)
            # Report file must never be read because shared ancestor replacement fails closed first
            self.assertNotIn(os.path.normcase(os.path.abspath(file_r)), read_files)

    def test_traversed_mirror_directory_replacement_before_later_input_is_rejected(self):
        # GPT Astra Blocker 1: Model directory passes initial traversal sweep,
        # but is replaced before report inspection finishes. Whole-scan final sweep rejects it!
        shared_dir = os.path.join(self.root, "dir_holder")
        model_dir = os.path.join(shared_dir, "model")
        report_dir = os.path.join(shared_dir, "report")
        os.makedirs(model_dir, exist_ok=True)
        os.makedirs(report_dir, exist_ok=True)
        self.project.mirror_model = model_dir
        self.project.mirror_report = report_dir

        with open(os.path.join(model_dir, "m.tmdl"), "wb") as f:
            f.write(b"table M\n")
        with open(os.path.join(report_dir, "r.json"), "wb") as f:
            f.write(b'{"report": 1}\n')

        armed = [False]
        real_read = common.read_checked
        real_lstat = os.lstat

        def spy_read(p, *a, **k):
            data = real_read(p, *a, **k)
            # Arm after reading report file r.json
            if os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(os.path.join(report_dir, "r.json"))):
                armed[0] = True
            return data

        def spy_lstat(path, *a, **k):
            st = real_lstat(path, *a, **k)
            if armed[0] and os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(model_dir)):
                return _fake_stat(st, st_ino=st.st_ino + 8888)
            return st

        with patch.object(common, "read_checked", side_effect=spy_read), \
             patch("os.lstat", side_effect=spy_lstat):
            with self.assertRaises(PbzError) as ctx:
                watch.tree_digest(self.ws, self.project)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "directory modified or replaced during traversal")

    def test_dotdot_path_refusal_via_common(self):
        target = os.path.join(self.root, "dir", "..", "other")
        with patch.object(common, "_refuse_dotdot_through_link") as mock_refuse:
            watch._check_containment_and_ancestors(target, stop_dir=self.root)
            mock_refuse.assert_called_once_with(target)

        # Assert refusal propagates unchanged
        with patch.object(common, "_refuse_dotdot_through_link",
                          side_effect=PbzError("UNSAFE_PATH", "refusing traversal containing parent reference")):
            with self.assertRaises(PbzError) as ctx:
                watch._check_containment_and_ancestors(target, stop_dir=self.root)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "refusing traversal containing parent reference")

    def test_catalog_rules_symlink_is_rejected(self):
        fake_cat = os.path.join(self.tmp, "cat_root", "rules_catalog")
        os.makedirs(fake_cat, exist_ok=True)
        catalog_path = os.path.normcase(os.path.abspath(fake_cat))
        real_islink = watch._is_any_link

        def mock_islink(p, *a, **k):
            if os.path.normcase(os.path.abspath(p)) == catalog_path:
                return True
            return real_islink(p)

        with patch.object(bpa, "RULES_DIR", fake_cat), \
             patch.object(watch, "_is_any_link", side_effect=mock_islink):
            with self.assertRaises(PbzError) as ctx:
                watch.tree_digest(self.ws, self.project)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "refusing to scan link")

    def test_catalog_parent_symlink_is_rejected(self):
        fake_cat = os.path.join(self.tmp, "cat_root", "rules_catalog")
        fake_cat_parent = os.path.dirname(fake_cat)
        os.makedirs(fake_cat, exist_ok=True)
        real_islink = watch._is_any_link
        def mock_islink(p, *a, **k):
            if os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(fake_cat_parent)):
                return True
            return real_islink(p)

        with patch.object(bpa, "RULES_DIR", fake_cat), \
             patch.object(watch, "_is_any_link", side_effect=mock_islink):
            with self.assertRaises(PbzError) as ctx:
                watch.tree_digest(self.ws, self.project)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "refusing to scan link")

    def test_catalog_parent_replacement_during_scan_is_rejected(self):
        fake_cat = os.path.join(self.tmp, "cat_root", "rules_catalog")
        fake_cat_parent = os.path.dirname(fake_cat)
        os.makedirs(fake_cat, exist_ok=True)
        rule_file = os.path.join(fake_cat, "rule.json")
        with open(rule_file, "wb") as f:
            f.write(b'{"rule": 1}\n')

        armed = [False]
        real_read = common.read_checked
        real_lstat = os.lstat

        def spy_read(p, *a, **k):
            data = real_read(p, *a, **k)
            if os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(rule_file)):
                armed[0] = True
            return data

        def spy_lstat(path, *a, **k):
            res = real_lstat(path)
            if armed[0] and os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(fake_cat_parent)):
                return _fake_stat(res, st_ino=res.st_ino + 6666)
            return res

        with patch.object(bpa, "RULES_DIR", fake_cat), \
             patch.object(common, "read_checked", side_effect=spy_read), \
             patch("os.lstat", side_effect=spy_lstat):
            with self.assertRaises(PbzError) as ctx:
                watch.tree_digest(self.ws, self.project)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "catalog parent replaced during scan")

    def test_child_directory_symlink_swap_during_traversal_is_rejected(self):
        swap_dir = os.path.join(self.project.mirror_model, "swap_child")
        os.makedirs(swap_dir, exist_ok=True)
        with open(os.path.join(swap_dir, "x.tmdl"), "wb") as f:
            f.write(b"table X\n")

        real_walk = os.walk
        real_islink = watch._is_any_link
        armed = [False]

        def spy_walk(root, *a, **k):
            for cur, dirs, files in real_walk(root, *a, **k):
                yield cur, dirs, files
                if os.path.normcase(os.path.abspath(cur)) == os.path.normcase(os.path.abspath(swap_dir)):
                    armed[0] = True

        def spy_islink(path, *a, **k):
            if armed[0] and os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(swap_dir)):
                return True
            return real_islink(path)

        with patch("os.walk", side_effect=spy_walk), \
             patch.object(watch, "_is_any_link", side_effect=spy_islink):
            with self.assertRaises(PbzError) as ctx:
                watch.tree_digest(self.ws, self.project)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "directory modified or replaced during traversal")
            self.assertNotIn("directory not visited during traversal", ctx.exception.detail)

    def test_scanned_file_in_place_rewrite_before_later_input_is_rejected(self):
        report_file = os.path.join(self.project.mirror_report, "report.json")
        with open(report_file, "wb") as f:
            f.write(b'{"sections": []}\n')

        model_file = os.path.join(self.project.mirror_model, "a.tmdl")
        initial_payload = b"table A1\n"  # 9 bytes
        with open(model_file, "wb") as f:
            f.write(initial_payload)

        st_before = os.lstat(model_file)
        self.assertEqual(st_before.st_size, len(initial_payload))

        model_read_done = [False]
        mutation_done = [False]
        real_read = common.read_checked

        def spy_read(path, *args, **kwargs):
            norm_p = os.path.normcase(os.path.abspath(path))
            if norm_p == os.path.normcase(os.path.abspath(model_file)):
                model_read_done[0] = True
            elif norm_p == os.path.normcase(os.path.abspath(report_file)):
                self.assertTrue(model_read_done[0])
                replacement_payload = b"table A2\n"  # 9 bytes
                self.assertEqual(len(replacement_payload), len(initial_payload))
                with open(model_file, "r+b") as mf:
                    mf.write(replacement_payload)
                    mf.flush()
                new_mtime_ns = st_before.st_mtime_ns + 10_000_000_000
                os.utime(model_file, ns=(st_before.st_atime_ns, new_mtime_ns))
                st_after = os.lstat(model_file)
                self.assertEqual(st_after.st_dev, st_before.st_dev)
                self.assertEqual(st_after.st_ino, st_before.st_ino)
                self.assertEqual(st_after.st_size, st_before.st_size)
                self.assertNotEqual(st_after.st_mtime_ns, st_before.st_mtime_ns)
                mutation_done[0] = True
            return real_read(path, *args, **kwargs)

        with patch.object(common, "read_checked", side_effect=spy_read):
            with self.assertRaises(PbzError) as ctx:
                watch.tree_digest(self.ws, self.project)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "file modified or replaced during scan")

        self.assertTrue(model_read_done[0])
        self.assertTrue(mutation_done[0])

    def test_optional_absent_policy_appearance_during_scan_is_rejected(self):
        root_policy = os.path.join(self.root, "policy.json")
        if os.path.exists(root_policy):
            os.remove(root_policy)

        real_inspect = watch._inspect_path
        observed_absent = [False]
        hook_ran = [False]

        def spy_inspect(path, *args, **kwargs):
            res = real_inspect(path, *args, **kwargs)
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(root_policy)):
                self.assertIsNone(res)
                observed_absent[0] = True
                with open(root_policy, "wb") as pf:
                    pf.write(b'{"version": 1}\n')
                hook_ran[0] = True
            return res

        with patch.object(watch, "_inspect_path", side_effect=spy_inspect):
            with self.assertRaises(PbzError) as ctx:
                watch.tree_digest(self.ws, self.project)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "file modified or replaced during scan")

        self.assertTrue(observed_absent[0])
        self.assertTrue(hook_ran[0])

    def test_directory_membership_conflict_across_overlapping_walks_is_rejected(self):
        self.project.mirror_report = self.project.mirror_model
        new_file = os.path.join(self.project.mirror_model, "b.tmdl")

        inspect_count = [0]
        real_inspect = watch._inspect_path

        def spy_inspect(path, *args, **kwargs):
            norm_p = os.path.normcase(os.path.abspath(path))
            if norm_p == os.path.normcase(os.path.abspath(self.project.mirror_model)):
                inspect_count[0] += 1
                if inspect_count[0] == 2:
                    # First walk (mirror:model) completed. Before second walk starts, create b.tmdl on real disk!
                    with open(new_file, "wb") as f:
                        f.write(b"table B\n")
            return real_inspect(path, *args, **kwargs)

        read_files = []
        real_read = common.read_checked

        def spy_read(path, *args, **kwargs):
            read_files.append(os.path.normcase(os.path.abspath(path)))
            return real_read(path, *args, **kwargs)

        with patch.object(watch, "_inspect_path", side_effect=spy_inspect), \
             patch.object(common, "read_checked", side_effect=spy_read):
            with self.assertRaises(PbzError) as ctx:
                watch.tree_digest(self.ws, self.project)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "directory modified or replaced during traversal")

        self.assertEqual(inspect_count[0], 2)
        self.assertNotIn(os.path.normcase(os.path.abspath(new_file)), read_files)

    def test_standalone_file_replaced_between_inspection_and_hashing_is_rejected(self):
        self.assertTrue(os.path.exists(self.ws.policy_path))
        real_inspect = watch._inspect_path
        inspected = [False]
        replaced = [False]

        def spy_inspect(path, *args, **kwargs):
            res = real_inspect(path, *args, **kwargs)
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(self.ws.policy_path)):
                inspected[0] = True
                os.remove(self.ws.policy_path)
                with open(self.ws.policy_path, "wb") as f:
                    f.write(b'{"version": 999}\n')
                replaced[0] = True
            return res

        with patch.object(watch, "_inspect_path", side_effect=spy_inspect):
            with self.assertRaises(PbzError) as ctx:
                watch.tree_digest(self.ws, self.project)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "file modified or replaced during scan")

        self.assertTrue(inspected[0])
        self.assertTrue(replaced[0])

    def test_missing_optional_ancestor_becoming_symlink_is_rejected(self):
        shutil.rmtree(self.ws.pbz_dir, ignore_errors=True)
        self.assertFalse(os.path.exists(self.ws.pbz_dir))

        real_inspect = watch._inspect_path
        observed_absent = [False]
        armed = [False]

        def spy_inspect(path, *args, **kwargs):
            res = real_inspect(path, *args, **kwargs)
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(self.ws.policy_path)):
                if res is None:
                    observed_absent[0] = True
                    armed[0] = True
            return res

        real_islink = watch._is_any_link

        def spy_islink(path, *args, **kwargs):
            if armed[0] and os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(self.ws.pbz_dir)):
                return True
            return real_islink(path, *args, **kwargs)

        with patch.object(watch, "_inspect_path", side_effect=spy_inspect), \
             patch.object(watch, "_is_any_link", side_effect=spy_islink):
            with self.assertRaises(PbzError) as ctx:
                watch.tree_digest(self.ws, self.project)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "refusing to scan link")

        self.assertTrue(observed_absent[0])
        self.assertTrue(armed[0])

    def test_missing_optional_ancestor_becoming_directory_is_rejected(self):
        shutil.rmtree(self.ws.pbz_dir, ignore_errors=True)
        self.assertFalse(os.path.exists(self.ws.pbz_dir))

        real_inspect = watch._inspect_path
        observed_absent = [False]
        armed = [False]

        def spy_inspect(path, *args, **kwargs):
            res = real_inspect(path, *args, **kwargs)
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(self.ws.policy_path)):
                if res is None:
                    observed_absent[0] = True
                    os.makedirs(self.ws.pbz_dir, exist_ok=True)
                    armed[0] = True
            return res

        with patch.object(watch, "_inspect_path", side_effect=spy_inspect):
            with self.assertRaises(PbzError) as ctx:
                watch.tree_digest(self.ws, self.project)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "directory modified or replaced during traversal")

        self.assertTrue(observed_absent[0])
        self.assertTrue(armed[0])

    def test_unvisited_child_directory_fails_scan(self):
        omitted_dir = os.path.join(self.project.mirror_model, "omitted")
        os.makedirs(omitted_dir, exist_ok=True)
        with open(os.path.join(omitted_dir, "o.tmdl"), "wb") as f:
            f.write(b"table O\n")

        real_walk = os.walk
        def spy_walk(root, *a, **k):
            for cur, dirs, files in real_walk(root, *a, **k):
                if os.path.normcase(os.path.abspath(cur)) == os.path.normcase(os.path.abspath(self.project.mirror_model)):
                    yield cur, ["omitted"], files
                elif os.path.normcase(os.path.abspath(cur)) == os.path.normcase(os.path.abspath(omitted_dir)):
                    continue
                else:
                    yield cur, dirs, files

        with patch("os.walk", side_effect=spy_walk):
            with self.assertRaises(PbzError) as ctx:
                watch.tree_digest(self.ws, self.project)
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
            self.assertEqual(ctx.exception.detail, "directory not visited during traversal")

    def test_boundary_escape_is_rejected(self):
        outside_path = os.path.join(self.tmp, "outside", "file.tmdl")
        os.makedirs(os.path.dirname(outside_path), exist_ok=True)
        with open(outside_path, "wb") as f:
            f.write(b"table Outside\n")
        with self.assertRaises(PbzError) as ctx:
            watch._check_containment_and_ancestors(outside_path, stop_dir=self.root)
        self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
        self.assertEqual(ctx.exception.detail, "path outside workspace")

    def test_expected_directory_replaced_by_file_is_rejected(self):
        file_as_dir = os.path.join(self.root, "not_a_dir")
        with open(file_as_dir, "wb") as f:
            f.write(b"i am a file\n")
        with self.assertRaises(PbzError) as ctx:
            watch._inspect_path(file_as_dir, expect_dir=True, stop_dir=self.root)
        self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
        self.assertEqual(ctx.exception.detail, "unrecognized file type during scan")

    def test_missing_workspace_root_is_rejected(self):
        ws_empty = DummyWorkspace(None)
        with self.assertRaises(PbzError) as ctx:
            watch.tree_digest(ws_empty, self.project)
        self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
        self.assertEqual(ctx.exception.detail, "workspace root is required for watch")


if __name__ == "__main__":
    unittest.main()
