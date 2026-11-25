"""Unit tests for Phase 6 F1: pbz doctor ci read-only diagnostics."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))

from pbz import doctor, outcomes
from pbz.cli import main
from pbz.common import PbzError

# Labeled replay of an authenticated artifact. Not hosted evidence.
_REPLAY_PROVENANCE = "authenticated"


def _checks_then_statuses(checks_stdout: str, statuses_stdout: str = "[]"):
    def run(cmd, **kwargs):
        joined = " ".join(str(part) for part in cmd)
        if "/statuses" in joined:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=statuses_stdout, stderr="")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=checks_stdout, stderr="")
    return run


class DoctorCiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.test_root = Path(self.tmp.name)

    def test_doctor_isolation_checklist_unchanged(self):
        """pbz doctor (without ci) returns standard OS-isolation checklist."""
        out = doctor.cmd_doctor()
        self.assertEqual(out["verb"], "doctor")
        self.assertIn("isolation", out)
        self.assertEqual(len(out["isolation"]), 5)
        wrapped, rc = outcomes.envelope(out)
        self.assertEqual(rc, 0)
        self.assertEqual(wrapped["outcome"]["status"], "SUCCEEDED")
        self.assertEqual(wrapped["outcome"]["code"], "OK")

    def test_doctor_ci_all_compliant(self):
        """When all CI evidence is known, clean, and passing, status is SUCCEEDED (exit 0)."""
        ci_data = {
            "sha": "a" * 40,
            "sha_reason": None,
            "dirty": False,
            "dirty_reason": None,
            "hook_mode": "full",
            "hosted_checks": {
                "checked_sha": "a" * 40,
                "status": "success",
                "reason": None,
                "checks": [{"name": "tests", "status": "completed", "conclusion": "success"}],
            },
            "branch_protection": {
                "branch": "main",
                "status": "protected",
                "reason": None,
                "ruleset": "classic",
            },
            "skips": {
                "approved_count": 12,
                "observed_count": 12,
                "status": "match",
                "reason": None,
                "unexpected": [],
                "missing": [],
            },
            "desktop_evidence": False,
            "desktop_evidence_reason": "ABSENT",
            "artifact_provenance": _REPLAY_PROVENANCE,
        }
        res = doctor.evaluate_ci_outcome(ci_data)
        self.assertEqual(res, "SUCCEEDED")

        out = {"verb": "doctor", "target": "ci", "status": res, "ci": ci_data}
        wrapped, rc = outcomes.envelope(out)
        self.assertEqual(rc, 0)
        self.assertEqual(wrapped["outcome"]["status"], "SUCCEEDED")
        self.assertEqual(wrapped["outcome"]["code"], "SUCCEEDED")

    def test_doctor_ci_fail_closed_allow_list(self):
        """Missing or unrecognized statuses must fail closed to INCOMPLETE, not SUCCEEDED."""
        ci_empty = {
            "sha": "a" * 40,
            "dirty": False,
            "hook_mode": "full",
            "hosted_checks": {},
            "branch_protection": {},
            "skips": {},
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci_empty), "INCOMPLETE")

        ci_unrecognized = {
            "sha": "a" * 40,
            "dirty": False,
            "hook_mode": "full",
            "hosted_checks": {"status": "cancelled", "checked_sha": "a" * 40},
            "branch_protection": {"status": "protected"},
            "skips": {"status": "match"},
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci_unrecognized), "INCOMPLETE")

    def test_doctor_ci_sha_trailing_newline_refused(self):
        """SHA with trailing newline must be rejected and fail closed to INCOMPLETE."""
        ci_trailing_newline = {
            "sha": ("a" * 40) + "\n",
            "dirty": False,
            "hook_mode": "full",
            "hosted_checks": {"status": "success", "checked_sha": "a" * 40},
            "branch_protection": {"status": "protected"},
            "skips": {"status": "match"},
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci_trailing_newline), "INCOMPLETE")

    def test_doctor_ci_sha_mismatch_refused(self):
        """Mismatched checked_sha vs head sha must fail closed to INCOMPLETE."""
        ci_mismatch_sha = {
            "sha": "a" * 40,
            "dirty": False,
            "hook_mode": "full",
            "hosted_checks": {"status": "success", "checked_sha": "b" * 40},
            "branch_protection": {"status": "protected"},
            "skips": {"status": "match"},
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci_mismatch_sha), "INCOMPLETE")

    def test_doctor_ci_malformed_nested_categories(self):
        """Explicit None or non-dict category values must not raise and fail closed to INCOMPLETE."""
        ci_malformed = {
            "sha": "a" * 40,
            "dirty": False,
            "hook_mode": "full",
            "hosted_checks": None,
            "branch_protection": "not_a_dict",
            "skips": ["invalid_list"],
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci_malformed), "INCOMPLETE")

        ci_malformed_with_failure = {
            "sha": "a" * 40,
            "dirty": False,
            "hook_mode": "full",
            "hosted_checks": {"status": "failure"},
            "branch_protection": None,
            "skips": None,
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci_malformed_with_failure), "FAILED")

    def test_doctor_ci_check_failure(self):
        """When hosted check fails, status is FAILED (exit 1)."""
        ci_data = {
            "sha": "b" * 40,
            "sha_reason": None,
            "dirty": False,
            "dirty_reason": None,
            "hook_mode": "full",
            "hosted_checks": {
                "checked_sha": "b" * 40,
                "status": "failure",
                "reason": None,
                "checks": [{"name": "tests", "status": "completed", "conclusion": "failure"}],
            },
            "branch_protection": {
                "branch": "main",
                "status": "protected",
                "reason": None,
            },
            "skips": {
                "status": "match",
            },
        }
        res = doctor.evaluate_ci_outcome(ci_data)
        self.assertEqual(res, "FAILED")

        out = {"verb": "doctor", "target": "ci", "status": res, "ci": ci_data}
        wrapped, rc = outcomes.envelope(out)
        self.assertEqual(rc, 1)
        self.assertEqual(wrapped["outcome"]["status"], "FAILED")
        self.assertEqual(wrapped["outcome"]["code"], "FAILED")

    def test_doctor_ci_mixed_failure_and_incomplete(self):
        """Definitive failure takes precedence over incomplete / unrun evidence."""
        ci_data = {
            "sha": "b" * 40,
            "dirty": True,
            "hook_mode": "unknown",
            "hosted_checks": {"status": "failure", "checked_sha": "b" * 40},
            "branch_protection": {"status": "unknown"},
            "skips": {"status": "unknown", "reason": "NOT_RUN"},
        }
        res = doctor.evaluate_ci_outcome(ci_data)
        self.assertEqual(res, "FAILED")
        wrapped, rc = outcomes.envelope({"verb": "doctor", "target": "ci", "status": res, "ci": ci_data})
        self.assertEqual(rc, 1)
        self.assertEqual(wrapped["outcome"]["status"], "FAILED")

    def test_doctor_ci_branch_unprotected(self):
        """When branch is explicitly unprotected, status is FAILED (exit 1)."""
        ci_data = {
            "sha": "c" * 40,
            "dirty": False,
            "hook_mode": "full",
            "hosted_checks": {"status": "success", "checked_sha": "c" * 40},
            "branch_protection": {"branch": "main", "status": "unprotected", "reason": None},
            "skips": {"status": "match"},
        }
        res = doctor.evaluate_ci_outcome(ci_data)
        self.assertEqual(res, "FAILED")
        wrapped, rc = outcomes.envelope({"verb": "doctor", "target": "ci", "status": res, "ci": ci_data})
        self.assertEqual(rc, 1)

    def test_doctor_ci_skip_mismatch(self):
        """When observed skips do not match approved baseline, status is FAILED (exit 1)."""
        ci_data = {
            "sha": "d" * 40,
            "dirty": False,
            "hook_mode": "full",
            "hosted_checks": {"status": "success", "checked_sha": "d" * 40},
            "branch_protection": {"status": "protected"},
            "skips": {"status": "mismatch", "reason": "UNEXPECTED_SKIPS"},
        }
        res = doctor.evaluate_ci_outcome(ci_data)
        self.assertEqual(res, "FAILED")
        wrapped, rc = outcomes.envelope({"verb": "doctor", "target": "ci", "status": res, "ci": ci_data})
        self.assertEqual(rc, 1)

    def test_doctor_ci_offline_mode(self):
        """Offline mode reports unknown with OFFLINE reason and INCOMPLETE outcome (exit 3)."""
        with patch.object(doctor, "get_current_branch", return_value=("main", None)):
            out = doctor.cmd_doctor_ci(ws_path=self.test_root, offline=True)
            self.assertEqual(out["verb"], "doctor")
            self.assertEqual(out["target"], "ci")
            self.assertEqual(out["status"], "INCOMPLETE")
            self.assertEqual(out["ci"]["hosted_checks"]["reason"], "OFFLINE")
            self.assertEqual(out["ci"]["branch_protection"]["reason"], "OFFLINE")

            wrapped, rc = outcomes.envelope(out)
            self.assertEqual(rc, 3)
            self.assertEqual(wrapped["outcome"]["status"], "BLOCKED")
            self.assertEqual(wrapped["outcome"]["code"], "INCOMPLETE")

    def test_doctor_ci_desktop_evidence_informational(self):
        """Desktop evidence is informational and does not prevent SUCCEEDED when absent."""
        for ev, reason in [(True, None), (False, "ABSENT"), (None, "UNAVAILABLE")]:
            ci_data = {
                "sha": "a" * 40,
                "dirty": False,
                "hook_mode": "full",
                "hosted_checks": {
                    "status": "success",
                    "checked_sha": "a" * 40,
                    "checks": [{"name": "tests", "status": "completed", "conclusion": "success"}],
                },
                "branch_protection": {"status": "protected"},
                "skips": {"status": "match"},
                "desktop_evidence": ev,
                "desktop_evidence_reason": reason,
                "artifact_provenance": _REPLAY_PROVENANCE,
            }
            res = doctor.evaluate_ci_outcome(ci_data)
            self.assertEqual(res, "SUCCEEDED")

    def test_doctor_ci_unborn_repo(self):
        """Unborn repo or missing HEAD produces nullable sha and sha_reason UNBORN."""
        with patch.object(doctor, "_run_git", return_value=(1, "")):
            sha, reason = doctor.get_head_sha(self.test_root)
            self.assertIsNone(sha)
            self.assertEqual(reason, "UNBORN")

    def test_doctor_ci_git_unavailable(self):
        """When git is unavailable, nullable fields are None with closed reasons."""
        sha, sha_r = doctor.get_head_sha(None)
        dirty, dirty_r = doctor.get_dirty_status(None)
        hook_mode = doctor.get_hook_mode(None)
        branch, branch_r = doctor.get_current_branch(None)
        self.assertIsNone(sha)
        self.assertEqual(sha_r, "GIT_UNAVAILABLE")
        self.assertIsNone(dirty)
        self.assertEqual(dirty_r, "GIT_UNAVAILABLE")
        self.assertEqual(hook_mode, "unknown")
        self.assertIsNone(branch)
        self.assertEqual(branch_r, "NO_BRANCH")

    def test_doctor_ci_detached_head(self):
        """Detached HEAD reports branch None with DETACHED_HEAD reason."""
        with patch.object(doctor, "_run_git", side_effect=[(1, ""), (0, "HEAD")]):
            branch, reason = doctor.get_current_branch(self.test_root)
            self.assertIsNone(branch)
            self.assertEqual(reason, "DETACHED_HEAD")

    def test_doctor_ci_dirty_working_tree(self):
        """Dirty working tree reports dirty=True and maps to INCOMPLETE."""
        ci_data = {
            "sha": "e" * 40,
            "dirty": True,
            "hook_mode": "full",
            "hosted_checks": {"status": "success", "checked_sha": "e" * 40},
            "branch_protection": {"status": "protected"},
            "skips": {"status": "match"},
        }
        res = doctor.evaluate_ci_outcome(ci_data)
        self.assertEqual(res, "INCOMPLETE")
        wrapped, rc = outcomes.envelope({"verb": "doctor", "target": "ci", "status": res, "ci": ci_data})
        self.assertEqual(rc, 3)

    def test_doctor_ci_hook_modes(self):
        """Hook modes: not_installed, foreign, fast, full, unknown."""
        # 1. Not installed
        with patch.object(doctor, "_run_git", side_effect=[(0, "hooks/pre-push"), (1, "")]):
            mode = doctor.get_hook_mode(self.test_root)
            self.assertEqual(mode, "not_installed")

        # 2. Foreign
        hook_dir = self.test_root / ".git" / "hooks"
        hook_dir.mkdir(parents=True, exist_ok=True)
        hook_file = hook_dir / "pre-push"
        hook_file.write_text("#!/bin/sh\necho foreign hook\n", encoding="utf-8")
        with patch.object(doctor, "_run_git", side_effect=[(0, ".git/hooks/pre-push")]):
            mode = doctor.get_hook_mode(self.test_root)
            self.assertEqual(mode, "foreign")

        # 3. Comment-only hook without shebang returns foreign
        hook_file.write_text("# pbz: pre-push hook\n", encoding="utf-8")
        with patch.object(doctor, "_run_git", side_effect=[(0, ".git/hooks/pre-push")]):
            mode = doctor.get_hook_mode(self.test_root)
            self.assertEqual(mode, "foreign")

        # 3b. Shebang-bearing marker-only no-op hook without execution returns foreign
        hook_file.write_text("#!/bin/sh\nset -e\n# pbz: pre-push hook\nexit 0\n", encoding="utf-8")
        with patch.object(doctor, "_run_git", side_effect=[(0, ".git/hooks/pre-push")]):
            mode = doctor.get_hook_mode(self.test_root)
            self.assertEqual(mode, "foreign")

        # 3c. Compound commands and shell manipulation bypasses return foreign
        for bypass in [
            "#!/bin/sh\nset -e\n# pbz:\necho run_offline\nexit 0\n",
            "#!/bin/sh\nset -e\necho scripts/git-hooks/pre-push\nexit 0\n",
            "#!/bin/sh\nset -e\n# pbz:\nexit 0; exec python tests/run_offline.py\n",
            "#!/bin/sh\nset -e\n# pbz:\nfalse && exec python tests/run_offline.py\n",
            "#!/bin/sh\nset -e\n# pbz:\nexec python tests/run_offline.py || true\n",
            "#!/bin/sh\nset -e\n# pbz:\ntrue # exec python tests/run_offline.py\n",
            "#!/bin/sh\nset -e\n# pbz:\nexit 0\nexec python tests/run_offline.py\n",
            "#!/bin/sh\n# pbz:\nexec python tests/run_offline.py\n",  # missing set -e
        ]:
            hook_file.write_text(bypass, encoding="utf-8")
            with patch.object(doctor, "_run_git", side_effect=[(0, ".git/hooks/pre-push")]):
                mode = doctor.get_hook_mode(self.test_root)
                self.assertEqual(mode, "foreign")

        # 4. Arbitrary pbz.requiredCI string resolves to unknown
        hook_file.write_text("#!/bin/sh\nset -e\nexec python tests/run_offline.py\n", encoding="utf-8")
        with patch.object(doctor, "_run_git", side_effect=[(0, ".git/hooks/pre-push"), (0, "invalid_custom_mode")]):
            mode = doctor.get_hook_mode(self.test_root)
            self.assertEqual(mode, "unknown")

        # 5. Explicit empty pbz.requiredCI resolves to unknown
        with patch.object(doctor, "_run_git", side_effect=[(0, ".git/hooks/pre-push"), (0, "   ")]):
            mode = doctor.get_hook_mode(self.test_root)
            self.assertEqual(mode, "unknown")

        # 6. Fast mode
        with patch.object(doctor, "_run_git", side_effect=[(0, ".git/hooks/pre-push"), (0, "fast")]):
            mode = doctor.get_hook_mode(self.test_root)
            self.assertEqual(mode, "fast")

        # 7. Full mode (default when cleanly unset rc=1)
        with patch.object(doctor, "_run_git", side_effect=[(0, ".git/hooks/pre-push"), (1, "")]):
            mode = doctor.get_hook_mode(self.test_root)
            self.assertEqual(mode, "full")

        # 8. Leading whitespace/blank line before shebang is strictly rejected
        for bad_prefix in (" ", "\t", "\n", "  "):
            hook_file.write_text(bad_prefix + "#!/bin/sh\nset -e\nexec python tests/run_offline.py\n", encoding="utf-8")
            with patch.object(doctor, "_run_git", side_effect=[(0, ".git/hooks/pre-push")]):
                self.assertEqual(doctor.get_hook_mode(self.test_root), "foreign")

        # 8b. Byte-exact mutations (extra blank lines, trailing whitespace) are rejected as foreign
        for mutated in [
            "#!/bin/sh\n\nset -e\nexec python tests/run_offline.py\n",  # extra blank line after shebang
            "#!/bin/sh\nset -e\nexec python tests/run_offline.py \n",   # extra trailing space
            "#!/bin/sh\nset -e\nexec python tests/run_offline.py\n\n", # extra trailing blank line
        ]:
            hook_file.write_text(mutated, encoding="utf-8")
            with patch.object(doctor, "_run_git", side_effect=[(0, ".git/hooks/pre-push")]):
                self.assertEqual(doctor.get_hook_mode(self.test_root), "foreign")

        # 9. Delegating wrapper transitive verification:
        delegating_text = """#!/bin/sh
set -e
root=$(git rev-parse --show-toplevel) || exit 1
exec "$root/scripts/git-hooks/pre-push" "$@"
"""
        hook_file.write_text(delegating_text, encoding="utf-8")
        scripts_dir = self.test_root / "scripts" / "git-hooks"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        target_script = scripts_dir / "pre-push"

        # 9a. Target script missing -> foreign
        if target_script.exists():
            target_script.unlink()
        with patch.object(doctor, "_run_git", side_effect=[(0, ".git/hooks/pre-push")]):
            self.assertEqual(doctor.get_hook_mode(self.test_root), "foreign")

        # 9b. Target script tampered / no-op -> foreign
        target_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        with patch.object(doctor, "_run_git", side_effect=[(0, ".git/hooks/pre-push")]):
            self.assertEqual(doctor.get_hook_mode(self.test_root), "foreign")

        # 9c. Target script valid direct runner -> full
        target_script.write_text("#!/bin/sh\nset -e\nexec python tests/run_offline.py\n", encoding="utf-8")
        with patch.object(doctor, "_run_git", side_effect=[(0, ".git/hooks/pre-push"), (1, "")]):
            self.assertEqual(doctor.get_hook_mode(self.test_root), "full")

        # 9d. When git rev-parse --git-path fails, get_hook_mode returns unknown, not falling back to .git/hooks/pre-push
        with patch.object(doctor, "_run_git", return_value=(1, "")):
            self.assertEqual(doctor.get_hook_mode(self.test_root), "unknown")

    def test_doctor_ci_slug_parsing_strict_host(self):
        """_clean_repo_slug strictly requires github.com host."""
        self.assertEqual(doctor._clean_repo_slug("https://github.com/owner/repo.git"), "owner/repo")
        self.assertEqual(doctor._clean_repo_slug("git@github.com:owner/repo.git"), "owner/repo")
        self.assertIsNone(doctor._clean_repo_slug(" https://github.com/owner/repo.git"))
        self.assertIsNone(doctor._clean_repo_slug("https://github.com/owner/repo.git "))
        self.assertIsNone(doctor._clean_repo_slug("git@github.com/owner/repo"))
        self.assertEqual(doctor._clean_repo_slug("ssh://git@github.com/owner/repo.git"), "owner/repo")
        self.assertIsNone(doctor._clean_repo_slug("https://fake-github.com/owner/repo.git"))
        self.assertIsNone(doctor._clean_repo_slug("https://example.com/github.com/owner/repo"))

        # When remote get-url returns non-github URL, get_repo_slug returns None without falling back to config
        with patch.object(doctor, "_run_git", return_value=(0, "https://gitlab.com/owner/repo.git")):
            self.assertIsNone(doctor.get_repo_slug(self.test_root))

    def test_doctor_ci_parse_api_json_strict(self):
        """_parse_api_json rejects empty bodies, whitespace, and scalar roots."""
        for bad in ["", "  ", "null", "123", '"string"', "true"]:
            c, d, rsn = doctor._parse_api_json(bad)
            self.assertEqual(c, -1)
            self.assertEqual(rsn, "API_ERROR")
            self.assertEqual(d, {})

        c_ok, d_ok, rsn_ok = doctor._parse_api_json('{"key": "val"}')
        self.assertEqual(c_ok, 200)
        self.assertEqual(d_ok, {"key": "val"})
        self.assertIsNone(rsn_ok)
        c_dup, d_dup, rsn_dup = doctor._parse_api_json('{"checks": [{"context": "build"}], "checks": []}')
        self.assertEqual(c_dup, -1)
        self.assertEqual(rsn_dup, "API_ERROR")
        self.assertEqual(d_dup, {})
        c_nest, _, rsn_nest = doctor._parse_api_json(
            '{"required_status_checks": {"checks": [{"context": "build"}], "checks": []}}'
        )
        self.assertEqual(c_nest, -1)
        self.assertEqual(rsn_nest, "API_ERROR")

    def test_doctor_ci_branch_names_with_plus_and_at(self):
        """Branch names containing + and @ are valid ref names and do not trigger BRANCH_MISMATCH."""
        for b in ["release+ci", "team@ci"]:
            with patch.object(doctor, "get_current_branch", return_value=(b, None)), \
                 patch.object(doctor, "get_branch_protection", return_value={"branch": b, "status": "protected"}):
                res = doctor.cmd_doctor_ci(ws_path=self.test_root, offline=True)
                self.assertEqual(res["ci"]["branch_protection"]["status"], "protected")

    def test_doctor_ci_skips_require_provenance_without_head(self):
        """require_provenance=True requires provenance even when expected_sha is None."""
        sidecars_dir = self.test_root / "sidecars_no_prov"
        sidecars_dir.mkdir(exist_ok=True)
        (sidecars_dir / "mcp.skips.jsonl").write_text("", encoding="utf-8")
        sk = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=sidecars_dir, expected_sha=None, require_provenance=True)
        self.assertEqual(sk["status"], "unknown")
        self.assertEqual(sk["reason"], "NOT_RUN")

    def test_doctor_ci_hosted_checks_gh_mock(self):
        """Test hosted checks parsing via gh API with pagination check."""
        mock_payload = json.dumps({
            "total_count": 2,
            "check_runs": [
                {"name": "serial", "status": "completed", "conclusion": "success", "head_sha": "f" * 40, "app": {"slug": "github-actions"}},
                {"name": "offline", "status": "completed", "conclusion": "success", "head_sha": "f" * 40, "app": {"slug": "github-actions"}},
            ]
        })
        proc = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=mock_payload, stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", return_value=proc), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            hc = doctor.get_hosted_checks(self.test_root, "f" * 40, offline=False)
            self.assertEqual(hc["status"], "success")
            self.assertEqual(len(hc["checks"]), 2)
            self.assertIsNone(hc["reason"])

        # Check names containing substrings like "special", "pricing", "checkout" must NOT match CI tokens
        mock_negative_payload = json.dumps({
            "total_count": 3,
            "check_runs": [
                {"name": "special_offer", "status": "completed", "conclusion": "success", "head_sha": "f" * 40},
                {"name": "pricing_matrix", "status": "completed", "conclusion": "success", "head_sha": "f" * 40},
                {"name": "checkout_flow", "status": "completed", "conclusion": "success", "head_sha": "f" * 40},
            ]
        })
        proc_neg = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=mock_negative_payload, stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", return_value=proc_neg), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            hc_neg = doctor.get_hosted_checks(self.test_root, "f" * 40, offline=False)
            self.assertEqual(hc_neg["status"], "unknown")
            self.assertEqual(hc_neg["reason"], "NO_SUCCESSFUL_CHECKS")

        # Third-party app producer or 'docs / build' without tests does not satisfy CI success
        mock_third_party = json.dumps({
            "total_count": 2,
            "check_runs": [
                {"name": "docs / build", "status": "completed", "conclusion": "success", "head_sha": "f" * 40, "app": {"slug": "github-actions"}},
                {"name": "serial", "status": "completed", "conclusion": "success", "head_sha": "f" * 40, "app": {"slug": "external-linter"}},
            ]
        })
        proc_third = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=mock_third_party, stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", return_value=proc_third), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            hc_third = doctor.get_hosted_checks(self.test_root, "f" * 40, offline=False)
            self.assertEqual(hc_third["status"], "unknown")
            self.assertEqual(hc_third["reason"], "NO_SUCCESSFUL_CHECKS")

        # Missing app, non-dict app, empty dict, or empty slug must NOT qualify as CI check run
        for malformed_app in [None, {}, {"slug": ""}, "github-actions"]:
            payload_bad_app = json.dumps({
                "total_count": 1,
                "check_runs": [
                    {"name": "serial", "status": "completed", "conclusion": "success", "head_sha": "f" * 40, "app": malformed_app},
                ]
            })
            proc_bad = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=payload_bad_app, stderr="")
            with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
                 patch.object(doctor.subprocess, "run", return_value=proc_bad), \
                 patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
                res = doctor.get_hosted_checks(self.test_root, "f" * 40, offline=False)
                self.assertEqual(res["status"], "unknown")
                self.assertEqual(res["reason"], "NO_SUCCESSFUL_CHECKS")

        # Skipped or neutral CI check alongside a green CI check must yield pending (PENDING_CHECKS), not success
        mock_skipped_ci = json.dumps({
            "total_count": 2,
            "check_runs": [
                {"name": "serial", "status": "completed", "conclusion": "success", "head_sha": "f" * 40, "app": {"slug": "github-actions"}},
                {"name": "offline", "status": "completed", "conclusion": "skipped", "head_sha": "f" * 40, "app": {"slug": "github-actions"}},
            ]
        })
        proc_skip_ci = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=mock_skipped_ci, stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", return_value=proc_skip_ci), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            hc_skip = doctor.get_hosted_checks(self.test_root, "f" * 40, offline=False)
            self.assertEqual(hc_skip["status"], "pending")
            self.assertEqual(hc_skip["reason"], "PENDING_CHECKS")

    def test_doctor_ci_hosted_checks_pagination_incomplete(self):
        """When total_count exceeds returned items, status is unknown with PAGINATION_INCOMPLETE."""
        mock_payload = json.dumps({
            "total_count": 105,
            "check_runs": [{"name": f"test_{i}", "status": "completed", "conclusion": "success"} for i in range(100)],
        })
        proc = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=mock_payload, stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", return_value=proc), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            hc = doctor.get_hosted_checks(self.test_root, "f" * 40, offline=False)
            self.assertEqual(hc["status"], "unknown")
            self.assertEqual(hc["reason"], "PAGINATION_INCOMPLETE")

    def test_doctor_ci_hosted_checks_malformed_total_count(self):
        """Missing, negative, or boolean total_count returns unknown with API_ERROR."""
        for bad_count in [None, -5, True, "100"]:
            mock_payload = json.dumps({
                "total_count": bad_count,
                "check_runs": [{"name": "test_1", "status": "completed", "conclusion": "success"}],
            })
            proc = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=mock_payload, stderr="")
            with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
                 patch.object(doctor.subprocess, "run", return_value=proc), \
                 patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
                hc = doctor.get_hosted_checks(self.test_root, "f" * 40, offline=False)
                self.assertEqual(hc["status"], "unknown")
                self.assertEqual(hc["reason"], "API_ERROR")

    def test_doctor_ci_hosted_checks_all_skipped_is_not_success(self):
        """When all check-runs are skipped/neutral, status is unknown with NO_SUCCESSFUL_CHECKS."""
        mock_payload = json.dumps({
            "total_count": 1,
            "check_runs": [{"name": "skipped_test", "status": "completed", "conclusion": "skipped", "head_sha": "f" * 40}],
        })
        proc = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=mock_payload, stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", return_value=proc), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            hc = doctor.get_hosted_checks(self.test_root, "f" * 40, offline=False)
            self.assertEqual(hc["status"], "unknown")
            self.assertEqual(hc["reason"], "NO_SUCCESSFUL_CHECKS")

    def test_doctor_ci_hosted_checks_reconciliation_with_required_contexts(self):
        """When branch protection defines required contexts, all must be present and successful."""
        mock_payload = json.dumps({
            "total_count": 2,
            "check_runs": [
                {"name": "serial", "status": "completed", "conclusion": "success", "head_sha": "f" * 40, "app": {"slug": "github-actions"}},
                {"name": "offline", "status": "completed", "conclusion": "success", "head_sha": "f" * 40, "app": {"slug": "github-actions"}},
            ]
        })
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=_checks_then_statuses(mock_payload)), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            # 1. Missing required context "security" -> pending / PENDING_CHECKS
            hc_missing = doctor.get_hosted_checks(self.test_root, "f" * 40, offline=False, required_contexts=["security"])
            self.assertEqual(hc_missing["status"], "pending")
            self.assertEqual(hc_missing["reason"], "PENDING_CHECKS")

            ci_missing = {
                "sha": "f" * 40, "dirty": False, "hook_mode": "full",
                "hosted_checks": hc_missing,
                "branch_protection": {"status": "protected", "required_contexts": ["security"]},
                "skips": {"status": "match"},
            }
            self.assertEqual(doctor.evaluate_ci_outcome(ci_missing), "INCOMPLETE")

            # 2. All required contexts satisfied -> success / None
            hc_satisfied = doctor.get_hosted_checks(self.test_root, "f" * 40, offline=False, required_contexts=["serial", "offline"])
            self.assertEqual(hc_satisfied["status"], "success")
            self.assertIsNone(hc_satisfied["reason"])

            ci_satisfied = {
                "sha": "f" * 40, "dirty": False, "hook_mode": "full",
                "hosted_checks": hc_satisfied,
                "branch_protection": {"status": "protected", "required_contexts": ["serial", "offline"]},
                "skips": {"status": "match"},
                "artifact_provenance": _REPLAY_PROVENANCE,
            }
            self.assertEqual(doctor.evaluate_ci_outcome(ci_satisfied), "SUCCEEDED")

    def test_doctor_ci_branch_override_requires_remote_tracking(self):
        """--branch override requires remote tracking ref (refs/remotes/origin/<branch>); local-only fails to BRANCH_MISMATCH."""
        with patch.object(doctor, "_run_git", return_value=(1, "")):
            res = doctor.cmd_doctor_ci(ws_path=self.test_root, offline=True, branch_override="main")
            self.assertEqual(res["ci"]["branch_protection"]["status"], "unknown")
            self.assertEqual(res["ci"]["branch_protection"]["reason"], "BRANCH_MISMATCH")

    def test_doctor_ci_branch_protection_classic_and_rulesets(self):
        """Test branch protection parsing: classic, rulesets, and inconclusive ruleset."""
        # Classic protected (with active status checks contexts)
        proc_ok = subprocess.CompletedProcess(
            args=["gh"],
            returncode=0,
            stdout='{"url": "https://api.github.com/repos/coqui123/pbi-zerodata/branches/main/protection", "required_status_checks": {"strict": true, "contexts": ["ci/tests"]}}',
            stderr="",
        )
        proc_ruleset_empty = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout='[]', stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_ok, proc_ruleset_empty]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp = doctor.get_branch_protection(self.test_root, "main", offline=False)
            self.assertEqual(bp["status"], "protected")
            self.assertEqual(bp["ruleset"], "classic")

        # Classic with inactive checks (empty contexts) falls through to rulesets (empty -> unprotected)
        proc_inactive = subprocess.CompletedProcess(
            args=["gh"],
            returncode=0,
            stdout='{"url": "https://api.github.com/repos/coqui123/pbi-zerodata/branches/main/protection", "required_status_checks": {"contexts": []}, "enforce_admins": {"enabled": false}, "required_pull_request_reviews": null}',
            stderr="",
        )
        proc_ruleset_empty = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout='[]', stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_inactive, proc_ruleset_empty]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp = doctor.get_branch_protection(self.test_root, "main", offline=False)
            self.assertEqual(bp["status"], "unprotected")

        # Classic with inactive checks falls through to rulesets (active ruleset -> protected)
        proc_ruleset_active = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout='[{"ruleset_id": 5, "type": "pull_request"}]', stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_inactive, proc_ruleset_active]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp = doctor.get_branch_protection(self.test_root, "main", offline=False)
            self.assertEqual(bp["status"], "protected")
            self.assertEqual(bp["ruleset"], "ruleset")

        # Classic with admin enforcement only (enforce_admins: true, no status checks or PR reviews)
        # does not count as active CI/PR protection and falls through to rulesets (empty -> unprotected)
        proc_admin_only = subprocess.CompletedProcess(
            args=["gh"],
            returncode=0,
            stdout='{"url": "https://api.github.com/repos/coqui123/pbi-zerodata/branches/main/protection", "required_status_checks": null, "enforce_admins": {"enabled": true}, "required_pull_request_reviews": null}',
            stderr="",
        )
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_admin_only, proc_ruleset_empty]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp_admin = doctor.get_branch_protection(self.test_root, "main", offline=False)
            self.assertEqual(bp_admin["status"], "unprotected")

        # Classic unprotected, ruleset protected
        proc_classic_unprot = subprocess.CompletedProcess(args=["gh"], returncode=1, stdout="", stderr="HTTP 404: Branch not protected")
        proc_ruleset_ok = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout='[{"ruleset_id": 1, "type": "pull_request"}]', stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_classic_unprot, proc_ruleset_ok]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp = doctor.get_branch_protection(self.test_root, "main", offline=False)
            self.assertEqual(bp["status"], "protected")
            self.assertEqual(bp["ruleset"], "ruleset")

        # Classic unprotected, ruleset confirmed empty list -> unprotected
        proc_ruleset_empty = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout='[]', stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_classic_unprot, proc_ruleset_empty]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp = doctor.get_branch_protection(self.test_root, "main", offline=False)
            self.assertEqual(bp["status"], "unprotected")
            self.assertIsNone(bp["ruleset"])

        # Ruleset with only administrative rule (e.g. deletion) returns unprotected
        proc_ruleset_deletion = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout='[{"ruleset_id": 1, "type": "deletion"}]', stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_classic_unprot, proc_ruleset_deletion]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp = doctor.get_branch_protection(self.test_root, "main", offline=False)
            self.assertEqual(bp["status"], "unprotected")

        # Classic unprotected, ruleset NOT_FOUND / error -> preserves unknown, not unprotected
        proc_ruleset_404 = subprocess.CompletedProcess(args=["gh"], returncode=1, stdout="", stderr="HTTP 404: Not Found")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_classic_unprot, proc_ruleset_404]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp = doctor.get_branch_protection(self.test_root, "main", offline=False)
            self.assertEqual(bp["status"], "unknown")
            self.assertEqual(bp["reason"], "NOT_FOUND")

        # Generic 404 on classic (without "Branch not protected") plus empty rulesets returns unknown, NOT_FOUND
        proc_classic_generic_404 = subprocess.CompletedProcess(args=["gh"], returncode=1, stdout="", stderr="HTTP 404: Not Found")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_classic_generic_404, proc_ruleset_empty]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp_gen404 = doctor.get_branch_protection(self.test_root, "main", offline=False)
            self.assertEqual(bp_gen404["status"], "unknown")
            self.assertEqual(bp_gen404["reason"], "NOT_FOUND")

        # Classic 403 (PERMISSION_DENIED) with active ruleset fails closed to unknown/PERMISSION_DENIED
        # (unreadable classic protection cannot prove absence of classic status checks)
        proc_classic_403 = subprocess.CompletedProcess(args=["gh"], returncode=1, stdout="", stderr="HTTP 403: Must have admin rights")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_classic_403, proc_ruleset_ok]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp_perm_ok = doctor.get_branch_protection(self.test_root, "main", offline=False)
            self.assertEqual(bp_perm_ok["status"], "unknown")
            self.assertEqual(bp_perm_ok["reason"], "PERMISSION_DENIED")

        # Active classic protection with ruleset 403 (PERMISSION_DENIED) fails closed to unknown/PERMISSION_DENIED
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_ok, proc_classic_403]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp_ruleset_perm = doctor.get_branch_protection(self.test_root, "main", offline=False)
            self.assertEqual(bp_ruleset_perm["status"], "unknown")
            self.assertEqual(bp_ruleset_perm["reason"], "PERMISSION_DENIED")

        # Classic 403 (PERMISSION_DENIED) with empty ruleset yields unknown/PERMISSION_DENIED
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_classic_403, proc_ruleset_empty]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp_perm_empty = doctor.get_branch_protection(self.test_root, "main", offline=False)
            self.assertEqual(bp_perm_empty["status"], "unknown")
            self.assertEqual(bp_perm_empty["reason"], "PERMISSION_DENIED")

        # Ruleset with required_status_checks requires non-empty checks parameters; empty, null, or missing parameters fail to API_ERROR
        for bad_rsc in [
            '{"ruleset_id": 2, "type": "required_status_checks", "parameters": {}}',
            '{"ruleset_id": 2, "type": "required_status_checks", "parameters": {"required_status_checks": null}}',
            '{"ruleset_id": 2, "type": "required_status_checks", "parameters": {"required_status_checks": []}}',
        ]:
            # Alone
            proc_bad_rsc = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=f"[{bad_rsc}]", stderr="")
            with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
                 patch.object(doctor.subprocess, "run", side_effect=[proc_classic_unprot, proc_bad_rsc]), \
                 patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
                bp_bad_chk = doctor.get_branch_protection(self.test_root, "main", offline=False)
                self.assertEqual(bp_bad_chk["status"], "unknown")
                self.assertEqual(bp_bad_chk["reason"], "API_ERROR")

            # Beside an active pull_request rule, it still returns API_ERROR
            proc_bad_beside_pr = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=f'[{bad_rsc}, {{"ruleset_id": 1, "type": "pull_request"}}]', stderr="")
            with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
                 patch.object(doctor.subprocess, "run", side_effect=[proc_classic_unprot, proc_bad_beside_pr]), \
                 patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
                bp_beside_pr = doctor.get_branch_protection(self.test_root, "main", offline=False)
                self.assertEqual(bp_beside_pr["status"], "unknown")
                self.assertEqual(bp_beside_pr["reason"], "API_ERROR")

        proc_ruleset_valid_checks = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout='[{"ruleset_id": 2, "type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "ci"}]}}]', stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_classic_unprot, proc_ruleset_valid_checks]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp_valid_chk = doctor.get_branch_protection(self.test_root, "main", offline=False)
            self.assertEqual(bp_valid_chk["status"], "protected")
            self.assertEqual(bp_valid_chk["ruleset"], "ruleset")

        # Malformed check context in ruleset parameters returns API_ERROR
        proc_ruleset_null_chk = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout='[{"ruleset_id": 2, "type": "required_status_checks", "parameters": {"required_status_checks": [null]}}]', stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_classic_unprot, proc_ruleset_null_chk]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp_null_chk = doctor.get_branch_protection(self.test_root, "main", offline=False)
            self.assertEqual(bp_null_chk["status"], "unknown")
            self.assertEqual(bp_null_chk["reason"], "API_ERROR")

        # Saturated ruleset page (100 items) fails closed to PAGINATION_INCOMPLETE
        proc_ruleset_100 = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=json.dumps([{"ruleset_id": i+1, "type": "pull_request"} for i in range(100)]), stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_classic_unprot, proc_ruleset_100]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp_100 = doctor.get_branch_protection(self.test_root, "main", offline=False)
            self.assertEqual(bp_100["status"], "unknown")
            self.assertEqual(bp_100["reason"], "PAGINATION_INCOMPLETE")

    def test_doctor_ci_branch_protection_malformed_classic_and_rulesets(self):
        """Malformed classic protection (e.g. empty dict, missing/null url, boolean checks) or invalid ruleset items yield API_ERROR."""
        for malformed in ["{}", "[]", "null", '{"non_protection_field": 123}', '{"url": null}', '{"required_status_checks": false}']:
            proc_bad = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=malformed, stderr="")
            with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
                 patch.object(doctor.subprocess, "run", return_value=proc_bad), \
                 patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
                bp = doctor.get_branch_protection(self.test_root, "main", offline=False)
                self.assertEqual(bp["status"], "unknown")
                self.assertEqual(bp["reason"], "API_ERROR")

        # Ruleset with malformed items: boolean ruleset_id, non-int, empty type
        proc_classic_unprot = subprocess.CompletedProcess(args=["gh"], returncode=1, stdout="", stderr="HTTP 404: Branch not protected")
        for bad_rules in [
            '[123, "string"]',
            '[{"ruleset_id": true, "type": "pull_request"}]',
            '[{"ruleset_id": 1, "type": ""}]',
            '[{"ruleset_id": -1, "type": "pull_request"}]',
        ]:
            proc_ruleset_bad = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=bad_rules, stderr="")
            with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
                 patch.object(doctor.subprocess, "run", side_effect=[proc_classic_unprot, proc_ruleset_bad]), \
                 patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
                bp = doctor.get_branch_protection(self.test_root, "main", offline=False)
                self.assertEqual(bp["status"], "unknown")
                self.assertEqual(bp["reason"], "API_ERROR")

    def test_doctor_ci_hosted_checks_undercount_and_zero_with_runs(self):
        """Total count less than returned runs, zero with runs, or invalid head_sha yields API_ERROR."""
        target_sha = "f" * 40

        # 1. total_count=0 with len(runs)=1
        mock_0_runs = json.dumps({
            "total_count": 0,
            "check_runs": [{"name": "test_1", "status": "completed", "conclusion": "success", "head_sha": target_sha}],
        })
        proc = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=mock_0_runs, stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", return_value=proc), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            hc = doctor.get_hosted_checks(self.test_root, target_sha, offline=False)
            self.assertEqual(hc["status"], "unknown")
            self.assertEqual(hc["reason"], "API_ERROR")

        # 2. total_count=1 with len(runs)=2 (positive undercount)
        mock_undercount = json.dumps({
            "total_count": 1,
            "check_runs": [
                {"name": "test_1", "status": "completed", "conclusion": "success", "head_sha": target_sha},
                {"name": "test_2", "status": "completed", "conclusion": "success", "head_sha": target_sha},
            ],
        })
        proc2 = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=mock_undercount, stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", return_value=proc2), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            hc2 = doctor.get_hosted_checks(self.test_root, target_sha, offline=False)
            self.assertEqual(hc2["status"], "unknown")
            self.assertEqual(hc2["reason"], "API_ERROR")

        # 3. Malformed head_sha: missing, None, empty, or mismatched
        for bad_sha in [None, "", "wrong" * 8]:
            payload = json.dumps({
                "total_count": 1,
                "check_runs": [{"name": "test_1", "status": "completed", "conclusion": "success", "head_sha": bad_sha}],
            })
            proc_bad_sha = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=payload, stderr="")
            with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
                 patch.object(doctor.subprocess, "run", return_value=proc_bad_sha), \
                 patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
                hc3 = doctor.get_hosted_checks(self.test_root, target_sha, offline=False)
                self.assertEqual(hc3["status"], "unknown")
                self.assertEqual(hc3["reason"], "API_ERROR")

    def test_doctor_ci_skip_status_not_run(self):
        """Doctor is read-only; without prior run sidecars, observed is unknown with NOT_RUN."""
        sk = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=None)
        self.assertEqual(sk["status"], "unknown")
        self.assertEqual(sk["reason"], "NOT_RUN")
        self.assertIsNone(sk["observed_count"])
        if sys.platform == "win32":
            self.assertEqual(sk["approved_count"], 12)

    def test_doctor_ci_skip_status_matching_artifact(self):
        """When prior sidecars match approved manifest per-suite, skips report match."""
        sidecars_dir = self.test_root / "sidecars"
        sidecars_dir.mkdir()
        plat = "windows" if sys.platform == "win32" else "posix"
        manifest_data = json.loads((SKILL / "tests" / "offline_skips.json").read_text(encoding="utf-8"))
        
        for suite_id, lines in manifest_data.get(plat, {}).items():
            (sidecars_dir / f"{suite_id}.skips.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

        sk = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=sidecars_dir)
        self.assertEqual(sk["status"], "match")
        self.assertIsNone(sk["reason"])

    def test_doctor_ci_skip_identity_prefix_collision(self):
        """Two distinct test names sharing first 100 characters must not compare equal."""
        sidecars_dir = self.test_root / "sidecars"
        sidecars_dir.mkdir()
        common_prefix = "a" * 100
        test_a = common_prefix + "_ONE"
        test_b = common_prefix + "_TWO"

        # Construct synthetic manifest
        custom_skill = self.test_root / "skill"
        (custom_skill / "tests").mkdir(parents=True)
        manifest_data = {"windows": {"mcp": [f'PBZ_SKIP {{"test": "{test_a}", "reason": "reason"}}']}}
        (custom_skill / "tests" / "offline_skips.json").write_text(json.dumps(manifest_data), encoding="utf-8")

        # Sidecar with test_b
        (sidecars_dir / "mcp.skips.jsonl").write_text(f'PBZ_SKIP {{"test": "{test_b}", "reason": "reason"}}\n', encoding="utf-8")

        sk = doctor.get_skips_status(skill_dir=custom_skill, observed_sidecars_dir=sidecars_dir)
        self.assertEqual(sk["status"], "mismatch")
        self.assertEqual(sk["reason"], "UNEXPECTED_SKIPS")
        self.assertIn(json.dumps({"reason": "reason", "test": test_b}, sort_keys=True), sk["unexpected"])

    def test_doctor_ci_sidecar_unreadable_or_corrupted_fails_closed(self):
        """Malformed JSON line or unreadable file in sidecar fails closed to mismatch with PARSE_ERROR."""
        sidecars_dir = self.test_root / "sidecars"
        sidecars_dir.mkdir()
        (sidecars_dir / "mcp.skips.jsonl").write_text("NOT_VALID_JSON\n", encoding="utf-8")
        sk = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=sidecars_dir)
        self.assertEqual(sk["status"], "mismatch")
        self.assertEqual(sk["reason"], "PARSE_ERROR")

    def test_doctor_ci_skip_provenance_and_missing_suites(self):
        """Provenance validation: stale SHA or platform mismatch fails to mismatch; missing/unexpected suites fail."""
        sidecars_dir = self.test_root / "sidecars_prov"
        sidecars_dir.mkdir()
        plat = "windows" if sys.platform == "win32" else "posix"
        manifest_data = json.loads((SKILL / "tests" / "offline_skips.json").read_text(encoding="utf-8"))

        for suite_id, lines in manifest_data.get(plat, {}).items():
            (sidecars_dir / f"{suite_id}.skips.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

        # 1. Stale run SHA in provenance.json
        (sidecars_dir / "provenance.json").write_text(json.dumps({"sha": "0" * 40, "platform": plat}), encoding="utf-8")
        sk_stale = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=sidecars_dir, expected_sha="1" * 40)
        self.assertEqual(sk_stale["status"], "mismatch")
        self.assertEqual(sk_stale["reason"], "STALE_RUN")

        # 1b. Stale SHA or platform mismatch fails to STALE_RUN / PLATFORM_MISMATCH even if sidecars contain unexpected skips
        (sidecars_dir / "unexpected.skips.jsonl").write_text(json.dumps({"test": "extra_test", "reason": "extra_reason"}) + "\n", encoding="utf-8")
        sk_stale_unexp = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=sidecars_dir, expected_sha="1" * 40)
        self.assertEqual(sk_stale_unexp["status"], "mismatch")
        self.assertEqual(sk_stale_unexp["reason"], "STALE_RUN")
        (sidecars_dir / "unexpected.skips.jsonl").unlink()

        # 2. Platform mismatch in provenance.json
        other_plat = "posix" if plat == "windows" else "windows"
        (sidecars_dir / "provenance.json").write_text(json.dumps({"sha": "1" * 40, "platform": other_plat}), encoding="utf-8")
        sk_plat = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=sidecars_dir, expected_sha="1" * 40)
        self.assertEqual(sk_plat["status"], "mismatch")
        self.assertEqual(sk_plat["reason"], "PLATFORM_MISMATCH")

        # 2b. Malformed provenance schema yields PARSE_ERROR
        (sidecars_dir / "provenance.json").write_text(json.dumps({"sha": 123, "platform": {}}), encoding="utf-8")
        sk_bad_prov = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=sidecars_dir, expected_sha="1" * 40)
        self.assertEqual(sk_bad_prov["status"], "mismatch")
        self.assertEqual(sk_bad_prov["reason"], "PARSE_ERROR")

        # 2c. Missing provenance when expected_sha supplied yields NOT_RUN
        (sidecars_dir / "provenance.json").unlink()
        sk_no_prov = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=sidecars_dir, expected_sha="1" * 40)
        self.assertEqual(sk_no_prov["status"], "unknown")
        self.assertEqual(sk_no_prov["reason"], "NOT_RUN")

        # 3. Missing suite (only 1 suite provided when multiple required on windows)
        sidecars_missing = self.test_root / "sidecars_missing"
        sidecars_missing.mkdir()
        (sidecars_missing / "mcp.skips.jsonl").write_text("", encoding="utf-8")
        if plat == "windows":
            sk_missing = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=sidecars_missing)
            self.assertEqual(sk_missing["status"], "unknown")
            self.assertEqual(sk_missing["reason"], "MISSING_SKIPS")
            self.assertIn("suite:mirror_recovery_honesty", sk_missing["missing"])

        # 3b. Provenance with non-zero exit code or failed status yields SUITE_FAILED
        (sidecars_dir / "provenance.json").write_text(json.dumps({"sha": "1" * 40, "platform": plat, "exit_code": 1}), encoding="utf-8")
        sk_failed_exit = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=sidecars_dir, expected_sha="1" * 40)
        self.assertEqual(sk_failed_exit["status"], "mismatch")
        self.assertEqual(sk_failed_exit["reason"], "SUITE_FAILED")

        (sidecars_dir / "provenance.json").write_text(json.dumps({"sha": "1" * 40, "platform": plat, "status": "interrupted"}), encoding="utf-8")
        sk_interrupted = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=sidecars_dir, expected_sha="1" * 40)
        self.assertEqual(sk_interrupted["status"], "mismatch")
        self.assertEqual(sk_interrupted["reason"], "SUITE_FAILED")

        # 3c. Provenance missing execution evidence (no exit_code and no status) yields NOT_RUN
        (sidecars_dir / "provenance.json").write_text(json.dumps({"sha": "1" * 40, "platform": plat}), encoding="utf-8")
        sk_no_exec = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=sidecars_dir, expected_sha="1" * 40)
        self.assertEqual(sk_no_exec["status"], "unknown")
        self.assertEqual(sk_no_exec["reason"], "NOT_RUN")

        # 3d. Provenance with unrecognized status yields SUITE_FAILED
        (sidecars_dir / "provenance.json").write_text(json.dumps({"sha": "1" * 40, "platform": plat, "status": "weird"}), encoding="utf-8")
        sk_weird = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=sidecars_dir, expected_sha="1" * 40)
        self.assertEqual(sk_weird["status"], "mismatch")
        self.assertEqual(sk_weird["reason"], "SUITE_FAILED")

        # 3e. Provenance with successful completion yields match
        (sidecars_dir / "provenance.json").write_text(json.dumps({"sha": "1" * 40, "platform": plat, "exit_code": 0, "status": "passed"}), encoding="utf-8")
        sk_ok = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=sidecars_dir, expected_sha="1" * 40)
        self.assertEqual(sk_ok["status"], "match")

        # 3f. Provenance with exit_code: 0 but missing status fails closed to NOT_RUN
        (sidecars_dir / "provenance.json").write_text(json.dumps({"sha": "1" * 40, "platform": plat, "exit_code": 0}), encoding="utf-8")
        sk_exit_no_st = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=sidecars_dir, expected_sha="1" * 40)
        self.assertEqual(sk_exit_no_st["status"], "unknown")
        self.assertEqual(sk_exit_no_st["reason"], "NOT_RUN")

        # 3g. Sidecars directory containing both missing and unexpected suites fails to UNEXPECTED_SKIPS (precedence)
        sidecars_both = self.test_root / "sidecars_both"
        sidecars_both.mkdir()
        (sidecars_both / "unauthorized_extra.skips.jsonl").write_text('PBZ_SKIP {"test": "x", "reason": "y"}\n', encoding="utf-8")
        sk_both = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=sidecars_both)
        self.assertEqual(sk_both["status"], "mismatch")
        self.assertEqual(sk_both["reason"], "UNEXPECTED_SKIPS")
        self.assertIn("suite:unauthorized_extra", sk_both["unexpected"])

        # 3h. Unexpected skip record in an observed suite coexisting with a missing suite triggers UNEXPECTED_SKIPS
        sidecars_unexp_and_missing = self.test_root / "sidecars_unexp_and_missing"
        sidecars_unexp_and_missing.mkdir()
        # Provide only mcp suite, but with an unapproved skip record (while other required suites are absent)
        (sidecars_unexp_and_missing / "mcp.skips.jsonl").write_text('PBZ_SKIP {"test": "unapproved.test", "reason": "rogue"}\n', encoding="utf-8")
        sk_unexp_and_miss = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=sidecars_unexp_and_missing)
        self.assertEqual(sk_unexp_and_miss["status"], "mismatch")
        self.assertEqual(sk_unexp_and_miss["reason"], "UNEXPECTED_SKIPS")

        # 3i. Stale SHA provenance with unexpected skips returns STALE_RUN (SHA validation precedence)
        (sidecars_unexp_and_missing / "provenance.json").write_text(json.dumps({"sha": "0" * 40, "platform": plat}), encoding="utf-8")
        sk_stale_precedence = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=sidecars_unexp_and_missing, expected_sha="1" * 40)
        self.assertEqual(sk_stale_precedence["status"], "mismatch")
        self.assertEqual(sk_stale_precedence["reason"], "STALE_RUN")

        # 4. Unexpected extra suite (including unauthorized suite and 'all.skips.jsonl')
        sidecars_extra = self.test_root / "sidecars_extra"
        sidecars_extra.mkdir()
        for suite_id, lines in manifest_data.get(plat, {}).items():
            (sidecars_extra / f"{suite_id}.skips.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (sidecars_extra / "unauthorized_suite.skips.jsonl").write_text('PBZ_SKIP {"test": "x", "reason": "y"}\n', encoding="utf-8")
        sk_extra = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=sidecars_extra)
        self.assertEqual(sk_extra["status"], "mismatch")
        self.assertEqual(sk_extra["reason"], "UNEXPECTED_SKIPS")
        self.assertIn("suite:unauthorized_suite", sk_extra["unexpected"])

        (sidecars_extra / "unauthorized_suite.skips.jsonl").unlink()
        (sidecars_extra / "all.skips.jsonl").write_text('PBZ_SKIP {"test": "x", "reason": "y"}\n', encoding="utf-8")
        sk_all = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=sidecars_extra)
        self.assertEqual(sk_all["status"], "mismatch")
        self.assertEqual(sk_all["reason"], "UNEXPECTED_SKIPS")
        self.assertIn("suite:all", sk_all["unexpected"])

    def test_doctor_ci_manifest_invalid_unicode_and_types(self):
        """Non-string manifest list entries or malformed structure fail closed to PARSE_ERROR."""
        custom_skill = self.test_root / "skill_bad"
        (custom_skill / "tests").mkdir(parents=True)
        # 1. Non-string list item in manifest
        bad_manifest = {"windows": {"mcp": [123, None, {}]}}
        (custom_skill / "tests" / "offline_skips.json").write_text(json.dumps(bad_manifest), encoding="utf-8")
        sk = doctor.get_skips_status(skill_dir=custom_skill, observed_sidecars_dir=None)
        self.assertEqual(sk["status"], "unknown")
        self.assertEqual(sk["reason"], "PARSE_ERROR")

    def test_doctor_ci_zero_data_canaries(self):
        """Canary check: output never contains auth tokens, full credential URLs, or raw errors."""
        proc_leak = subprocess.CompletedProcess(
            args=["gh"],
            returncode=1,
            stdout="",
            stderr="Failed connecting with token ghp_supersecrettoken123 to https://secret_user:secret_pass@github.com/repo",
        )
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", return_value=proc_leak), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            out = doctor.cmd_doctor_ci(ws_path=self.test_root, offline=False)

        raw_json = json.dumps(out)
        self.assertNotIn("ghp_", raw_json)
        self.assertNotIn("supersecret", raw_json)
        self.assertNotIn("secret_pass", raw_json)

        for key in ("sha_reason", "dirty_reason", "desktop_evidence_reason"):
            r = out["ci"].get(key)
            if r is not None:
                self.assertIn(r, doctor.CLOSED_REASONS)
        for sub_key in ("hosted_checks", "branch_protection", "skips"):
            r = out["ci"][sub_key].get("reason")
            if r is not None:
                self.assertIn(r, doctor.CLOSED_REASONS)

    def test_doctor_ci_read_only_guarantee_including_git_index(self):
        """Doctor CI must not modify working tree files or .git/index."""
        repo = self.test_root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        for key, value in (("user.email", "t@example.com"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(repo), "config", key, value], check=True, capture_output=True)
        test_file = repo / "README.md"
        test_file.write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)

        index_file = repo / ".git" / "index"
        index_bytes_before = index_file.read_bytes()
        before_files = {p: p.stat().st_mtime_ns for p in repo.rglob("*")}

        doctor.cmd_doctor_ci(ws_path=repo, offline=True)

        index_bytes_after = index_file.read_bytes()
        after_files = {p: p.stat().st_mtime_ns for p in repo.rglob("*")}

        self.assertEqual(index_bytes_before, index_bytes_after)
        self.assertEqual(set(before_files.keys()), set(after_files.keys()))

    def test_cli_dispatch_and_exit_codes(self):
        """Full CLI dispatch asserting JSON outcome and exit code triples."""
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = main(["doctor"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["outcome"]["status"], "SUCCEEDED")
        self.assertEqual(data["outcome"]["exit_code"], 0)

        buf2 = io.StringIO()
        with patch("sys.stdout", buf2):
            rc = main(["doctor", "ci", "--offline"])
        self.assertEqual(rc, 3)
        data2 = json.loads(buf2.getvalue())
        self.assertEqual(data2["outcome"]["status"], "BLOCKED")
        self.assertEqual(data2["outcome"]["code"], "INCOMPLETE")
        self.assertEqual(data2["outcome"]["exit_code"], 3)

        buf3 = io.StringIO()
        with patch("sys.stdout", buf3):
            rc = main(["doctor", "--offline"])
        self.assertEqual(rc, 2)
        data3 = json.loads(buf3.getvalue())
        self.assertEqual(data3["outcome"]["status"], "INVALID")
        self.assertEqual(data3["outcome"]["code"], "BAD_ARGUMENTS")
        self.assertEqual(data3["outcome"]["exit_code"], 2)

        buf4 = io.StringIO()
        with patch("sys.stdout", buf4):
            rc = main(["doctor", "--branch", "main"])
        self.assertEqual(rc, 2)
        data4 = json.loads(buf4.getvalue())
        self.assertEqual(data4["outcome"]["status"], "INVALID")
        self.assertEqual(data4["outcome"]["code"], "BAD_ARGUMENTS")
        self.assertEqual(data4["outcome"]["exit_code"], 2)

        buf5 = io.StringIO()
        with patch("sys.stdout", buf5):
            rc = main(["doctor", "ci", "--offline", "--branch", "main"])
        self.assertEqual(rc, 3)
        data5 = json.loads(buf5.getvalue())
        self.assertEqual(data5["outcome"]["status"], "BLOCKED")
        self.assertEqual(data5["outcome"]["code"], "INCOMPLETE")
        self.assertEqual(data5["outcome"]["exit_code"], 3)

        # Empty --branch string rejected
        for empty_args in [["doctor", "--branch", ""], ["doctor", "ci", "--branch", ""]]:
            buf_e = io.StringIO()
            with patch("sys.stdout", buf_e):
                rc = main(empty_args)
            self.assertEqual(rc, 2)
            data_e = json.loads(buf_e.getvalue())
            self.assertEqual(data_e["outcome"]["status"], "INVALID")
            self.assertEqual(data_e["outcome"]["code"], "BAD_ARGUMENTS")
            self.assertEqual(data_e["outcome"]["exit_code"], 2)

    def test_subprocess_exit_codes(self):
        """Subprocess invocation verifying real OS process exit codes."""
        env = {**os.environ, "PYTHONPATH": str(SKILL)}
        # 1. doctor -> exit 0
        p0 = subprocess.run([sys.executable, "-m", "pbz.cli", "doctor"], capture_output=True, text=True, env=env)
        self.assertEqual(p0.returncode, 0)
        self.assertEqual(json.loads(p0.stdout)["outcome"]["status"], "SUCCEEDED")

        # 2. doctor ci --offline -> exit 3 (BLOCKED / INCOMPLETE)
        p3 = subprocess.run([sys.executable, "-m", "pbz.cli", "doctor", "ci", "--offline"], capture_output=True, text=True, env=env)
        self.assertEqual(p3.returncode, 3)
        self.assertEqual(json.loads(p3.stdout)["outcome"]["status"], "BLOCKED")
        self.assertEqual(json.loads(p3.stdout)["outcome"]["code"], "INCOMPLETE")

        # 3. doctor --offline -> exit 2 (INVALID / BAD_ARGUMENTS)
        p2 = subprocess.run([sys.executable, "-m", "pbz.cli", "doctor", "--offline"], capture_output=True, text=True, env=env)
        self.assertEqual(p2.returncode, 2)
        self.assertEqual(json.loads(p2.stdout)["outcome"]["status"], "INVALID")
        self.assertEqual(json.loads(p2.stdout)["outcome"]["code"], "BAD_ARGUMENTS")

        # 4. _is_managed_hook bytes contract
        self.assertTrue(doctor._is_managed_hook(b"#!/bin/sh\nset -e\nexec python tests/run_offline.py\n"))
        self.assertFalse(doctor._is_managed_hook(b"#!/bin/sh\necho foreign\n"))

        # 5. --branch with leading dash fails closed to BRANCH_MISMATCH
        out_dash = doctor.cmd_doctor_ci(ws_path=self.test_root, offline=True, branch_override="-option")
        self.assertEqual(out_dash["ci"]["branch_protection"]["status"], "unknown")
        self.assertEqual(out_dash["ci"]["branch_protection"]["reason"], "BRANCH_MISMATCH")

        # 6. CLI rejects leading dash or invalid characters in --branch as BAD_ARGUMENTS
        buf_dash = io.StringIO()
        with patch("sys.stdout", buf_dash):
            rc_dash = main(["doctor", "ci", "--branch=-invalid"])
        self.assertEqual(rc_dash, 2)
        self.assertEqual(json.loads(buf_dash.getvalue())["outcome"]["code"], "BAD_ARGUMENTS")

        # CLI rejects leading-space dash e.g. ' -invalid' as BAD_ARGUMENTS
        buf_space = io.StringIO()
        with patch("sys.stdout", buf_space):
            rc_space = main(["doctor", "ci", "--branch= -invalid"])
        self.assertEqual(rc_space, 2)
        self.assertEqual(json.loads(buf_space.getvalue())["outcome"]["code"], "BAD_ARGUMENTS")

        # Padded branch is rejected. It is not trimmed into a different name.
        buf_pad = io.StringIO()
        with patch("sys.stdout", buf_pad):
            rc_pad = main(["doctor", "ci", "--branch", " main "])
        self.assertEqual(rc_pad, 2)
        self.assertEqual(json.loads(buf_pad.getvalue())["outcome"]["code"], "BAD_ARGUMENTS")
        out_pad = doctor.cmd_doctor_ci(ws_path=self.test_root, offline=True, branch_override=" main ")
        self.assertEqual(out_pad["ci"]["branch_protection"]["reason"], "BRANCH_MISMATCH")

        # 7. Hook mode reason pairs
        for mode, expected_rsn in [
            ("full", None),
            ("fast", "HOOK_MODE_FAST"),
            ("not_installed", "NOT_INSTALLED"),
            ("foreign", "FOREIGN_HOOK"),
            ("unknown", "GIT_UNAVAILABLE"),
        ]:
            with patch.object(doctor, "get_hook_mode", return_value=mode):
                res = doctor.cmd_doctor_ci(ws_path=self.test_root, offline=True)
                self.assertEqual(res["ci"]["hook_mode_reason"], expected_rsn)

        # 8. CLOSED_REASONS completeness: all observed non-success reasons belong to CLOSED_REASONS
        for rsn in (
            out_dash["ci"]["branch_protection"]["reason"],
            out_dash["ci"]["hosted_checks"]["reason"],
            out_dash["ci"]["skips"]["reason"],
            out_dash["ci"]["hook_mode_reason"],
            out_dash["ci"]["dirty_reason"],
        ):
            if rsn is not None:
                self.assertIn(rsn, doctor.CLOSED_REASONS)

    def test_doctor_ci_classic_pr_plus_ruleset_status_checks(self):
        """Classic PR reviews combined with ruleset required status checks (e.g. security):
        When security check is missing, hosted checks pending and overall INCOMPLETE.
        When security check is satisfied, returns SUCCEEDED.
        """
        proc_classic_pr = subprocess.CompletedProcess(
            args=["gh"], returncode=0,
            stdout='{"url": "https://api.github.com/repos/coqui123/pbi-zerodata/branches/main/protection", "required_pull_request_reviews": {"required_approving_review_count": 1}}',
            stderr="",
        )
        proc_ruleset_sec = subprocess.CompletedProcess(
            args=["gh"], returncode=0,
            stdout='[{"ruleset_id": 10, "type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "security"}]}}]',
            stderr="",
        )
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_classic_pr, proc_ruleset_sec]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp = doctor.get_branch_protection(self.test_root, "main", offline=False)
            self.assertEqual(bp["status"], "protected")
            self.assertEqual(bp["ruleset"], "ruleset+classic")
            self.assertEqual(bp["required_contexts"], ["security"])

        # Overall CI outcome: with only "ci / lint" passing and "security" missing -> INCOMPLETE
        ci_missing_sec = {
            "sha": "a" * 40,
            "dirty": False,
            "hook_mode": "full",
            "branch_protection": bp,
            "hosted_checks": {
                "checked_sha": "a" * 40,
                "status": "pending",
                "reason": "PENDING_CHECKS",
                "checks": [{"name": "ci / lint", "status": "completed", "conclusion": "success"}],
            },
            "skips": {"status": "match"},
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci_missing_sec), "INCOMPLETE")

        # When security check is satisfied alongside CI tests -> SUCCEEDED
        ci_sec_satisfied = {
            "sha": "a" * 40,
            "dirty": False,
            "hook_mode": "full",
            "branch_protection": bp,
            "hosted_checks": {
                "checked_sha": "a" * 40,
                "status": "success",
                "reason": None,
                "checks": [
                    {"name": "tests", "status": "completed", "conclusion": "success"},
                    {"name": "security", "status": "completed", "conclusion": "success"},
                ],
            },
            "skips": {"status": "match"},
            "artifact_provenance": _REPLAY_PROVENANCE,
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci_sec_satisfied), "SUCCEEDED")

    def test_doctor_ci_required_check_app_id_constraint(self):
        """Required check with designated app_id:
        A same-named check from the wrong app does not satisfy the requirement (fails to INCOMPLETE).
        Only the designated app satisfies it (SUCCEEDED).
        """
        bp_with_app = {
            "status": "protected",
            "required_contexts": ["security"],
            "required_checks": [{"context": "security", "app_id": 15368}],
        }
        # Wrong app (app_id 99999)
        ci_wrong_app = {
            "sha": "b" * 40,
            "dirty": False,
            "hook_mode": "full",
            "branch_protection": bp_with_app,
            "hosted_checks": {
                "checked_sha": "b" * 40,
                "status": "success",
                "checks": [
                    {"name": "tests", "status": "completed", "conclusion": "success", "app_id": 15368},
                    {"name": "security", "status": "completed", "conclusion": "success", "app_id": 99999},
                ],
            },
            "skips": {"status": "match"},
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci_wrong_app), "INCOMPLETE")

        # Correct app (app_id 15368)
        ci_correct_app = {
            "sha": "b" * 40,
            "dirty": False,
            "hook_mode": "full",
            "branch_protection": bp_with_app,
            "hosted_checks": {
                "checked_sha": "b" * 40,
                "status": "success",
                "checks": [
                    {"name": "tests", "status": "completed", "conclusion": "success", "app_id": 15368},
                    {"name": "security", "status": "completed", "conclusion": "success", "app_id": 15368},
                ],
            },
            "skips": {"status": "match"},
            "artifact_provenance": _REPLAY_PROVENANCE,
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci_correct_app), "SUCCEEDED")

    def test_doctor_ci_long_check_name_untruncated(self):
        """Check names longer than 100 characters preserve full name and reconcile cleanly."""
        long_name = "ci / test_suite_with_a_very_long_descriptive_name_exceeding_one_hundred_characters_in_total_length_and_more_tokens"
        self.assertGreater(len(long_name), 100)

        mock_payload = json.dumps({
            "total_count": 1,
            "check_runs": [{
                "name": long_name,
                "status": "completed",
                "conclusion": "success",
                "head_sha": "c" * 40,
                "app": {"id": 15368, "slug": "github-actions"},
            }],
        })
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=_checks_then_statuses(mock_payload)), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            hc = doctor.get_hosted_checks(self.test_root, "c" * 40, offline=False, required_contexts=[long_name])
            self.assertEqual(hc["status"], "success")
            self.assertEqual(hc["checks"][0]["name"], long_name)

        ci_long = {
            "sha": "c" * 40,
            "dirty": False,
            "hook_mode": "full",
            "branch_protection": {"status": "protected", "required_contexts": [long_name]},
            "hosted_checks": hc,
            "skips": {"status": "match"},
            "artifact_provenance": _REPLAY_PROVENANCE,
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci_long), "SUCCEEDED")

    def test_doctor_ci_run_git_env_scrubbing(self):
        """_run_git strips all GIT_* environment variables (GIT_DIR, GIT_CONFIG_*, etc.)."""
        seen_env = {}
        def mock_run(cmd, **kwargs):
            nonlocal seen_env
            seen_env = kwargs.get("env", {})
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"", stderr=b"")

        with patch.dict(os.environ, {
            "GIT_CONFIG_PARAMETERS": "remote.origin.url=injected",
            "GIT_DIR": "/tmp/fake.git",
            "GIT_WORK_TREE": "/tmp/fake_wt",
            "GIT_INDEX_FILE": "/tmp/fake_idx",
            "GIT_NAMESPACE": "evil",
            "CUSTOM_VAR": "keep_me",
        }):
            with patch.object(doctor.subprocess, "run", side_effect=mock_run):
                doctor._run_git(["status"])

        for k in seen_env:
            if k != "GIT_OPTIONAL_LOCKS":
                self.assertFalse(k.startswith("GIT_"), f"Environment key {k} was not scrubbed!")
        self.assertEqual(seen_env.get("CUSTOM_VAR"), "keep_me")
        self.assertEqual(seen_env.get("GIT_OPTIONAL_LOCKS"), "0")

    def test_doctor_ci_multi_app_same_context_both_required(self):
        """When classic requires context from app 100 and ruleset requires same context from app 200,
        both are preserved in required_checks.
        A alone -> exit 3 (INCOMPLETE).
        B alone -> exit 3 (INCOMPLETE).
        Both A and B together -> exit 0 (SUCCEEDED).
        """
        proc_classic = subprocess.CompletedProcess(
            args=["gh"], returncode=0,
            stdout='{"url": "https://api.github.com/repos/coqui123/pbi-zerodata/branches/main/protection", "required_status_checks": {"strict": true, "checks": [{"context": "security", "app_id": 100}]}}',
            stderr="",
        )
        proc_ruleset = subprocess.CompletedProcess(
            args=["gh"], returncode=0,
            stdout='[{"ruleset_id": 1, "type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "security", "integration_id": 200}]}}]',
            stderr="",
        )
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_classic, proc_ruleset]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp = doctor.get_branch_protection(self.test_root, "main", offline=False)
            self.assertEqual(bp["status"], "protected")
            self.assertEqual(bp["ruleset"], "ruleset+classic")
            self.assertEqual(len(bp["required_checks"]), 2)
            app_ids = {c["app_id"] for c in bp["required_checks"]}
            self.assertEqual(app_ids, {100, 200})

        # App 100 alone -> INCOMPLETE
        ci_app100_only = {
            "sha": "d" * 40,
            "dirty": False,
            "hook_mode": "full",
            "branch_protection": bp,
            "hosted_checks": {
                "checked_sha": "d" * 40,
                "status": "success",
                "checks": [
                    {"name": "security", "status": "completed", "conclusion": "success", "app_id": 100},
                ],
            },
            "skips": {"status": "match"},
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci_app100_only), "INCOMPLETE")

        # App 200 alone -> INCOMPLETE
        ci_app200_only = {
            "sha": "d" * 40,
            "dirty": False,
            "hook_mode": "full",
            "branch_protection": bp,
            "hosted_checks": {
                "checked_sha": "d" * 40,
                "status": "success",
                "checks": [
                    {"name": "security", "status": "completed", "conclusion": "success", "app_id": 200},
                ],
            },
            "skips": {"status": "match"},
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci_app200_only), "INCOMPLETE")

        # Both App 100 and App 200 together -> SUCCEEDED
        ci_both_apps = {
            "sha": "d" * 40,
            "dirty": False,
            "hook_mode": "full",
            "branch_protection": bp,
            "hosted_checks": {
                "checked_sha": "d" * 40,
                "status": "success",
                "checks": [
                    {"name": "security", "status": "completed", "conclusion": "success", "app_id": 100},
                    {"name": "security", "status": "completed", "conclusion": "success", "app_id": 200},
                ],
            },
            "skips": {"status": "match"},
            "artifact_provenance": _REPLAY_PROVENANCE,
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci_both_apps), "SUCCEEDED")

    def test_doctor_ci_evaluator_shape_hardening(self):
        """Malformed or empty check collections fail closed to INCOMPLETE without raising."""
        # checks=None with PR-only policy -> INCOMPLETE
        ci_none_checks = {
            "sha": "e" * 40,
            "dirty": False,
            "hook_mode": "full",
            "branch_protection": {"status": "protected", "ruleset": "classic", "required_contexts": []},
            "hosted_checks": {"checked_sha": "e" * 40, "status": "success", "checks": None},
            "skips": {"status": "match"},
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci_none_checks), "INCOMPLETE")

        # checks=[] with PR-only policy -> INCOMPLETE (requires at least one completed success)
        ci_empty_checks = {
            "sha": "e" * 40,
            "dirty": False,
            "hook_mode": "full",
            "branch_protection": {"status": "protected", "ruleset": "classic", "required_contexts": []},
            "hosted_checks": {"checked_sha": "e" * 40, "status": "success", "checks": []},
            "skips": {"status": "match"},
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci_empty_checks), "INCOMPLETE")

        # Non-dict item in required_checks -> INCOMPLETE
        ci_bad_req = {
            "sha": "e" * 40,
            "dirty": False,
            "hook_mode": "full",
            "branch_protection": {"status": "protected", "required_checks": [123, "not_dict"]},
            "hosted_checks": {
                "checked_sha": "e" * 40,
                "status": "success",
                "checks": [{"name": "tests", "status": "completed", "conclusion": "success"}],
            },
            "skips": {"status": "match"},
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci_bad_req), "INCOMPLETE")

        # required_contexts containing unhashable item (e.g. [{}]) fails closed to INCOMPLETE without raising TypeError
        ci_unhashable_ctx = {
            "sha": "e" * 40,
            "dirty": False,
            "hook_mode": "full",
            "branch_protection": {"status": "protected", "required_contexts": [{}]},
            "hosted_checks": {
                "checked_sha": "e" * 40,
                "status": "success",
                "checks": [{"name": "tests", "status": "completed", "conclusion": "success"}],
            },
            "skips": {"status": "match"},
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci_unhashable_ctx), "INCOMPLETE")

        # required_contexts containing non-list (e.g. string or dict) fails closed to INCOMPLETE
        ci_nonlist_ctx = {
            "sha": "e" * 40,
            "dirty": False,
            "hook_mode": "full",
            "branch_protection": {"status": "protected", "required_contexts": "not_a_list"},
            "hosted_checks": {
                "checked_sha": "e" * 40,
                "status": "success",
                "checks": [{"name": "tests", "status": "completed", "conclusion": "success"}],
            },
            "skips": {"status": "match"},
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci_nonlist_ctx), "INCOMPLETE")

    def test_doctor_ci_classic_protection_empty_rsc_rejected(self):
        """Classic protection with required_status_checks: {} (neither contexts nor checks) fails to API_ERROR."""
        proc_empty_rsc = subprocess.CompletedProcess(
            args=["gh"], returncode=0,
            stdout='{"url": "https://api.github.com/repos/coqui123/pbi-zerodata/branches/main/protection", "required_status_checks": {}, "required_pull_request_reviews": {"required_approving_review_count": 1}}',
            stderr="",
        )
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", return_value=proc_empty_rsc), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp = doctor.get_branch_protection(self.test_root, "main", offline=False)
            self.assertEqual(bp["status"], "unknown")
            self.assertEqual(bp["reason"], "API_ERROR")

    def test_doctor_ci_hosted_checks_boolean_app_id_and_startup_failure(self):
        """Boolean app.id is rejected (not treated as int 1), and startup_failure marks CHECKS_FAILED."""
        # 1. Boolean app.id
        mock_bool_app = json.dumps({
            "total_count": 1,
            "check_runs": [{
                "name": "tests", "status": "completed", "conclusion": "success",
                "head_sha": "f" * 40,
                "app": {"id": True, "slug": "github-actions"},
            }],
        })
        proc_bool = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=mock_bool_app, stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", return_value=proc_bool), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            hc = doctor.get_hosted_checks(self.test_root, "f" * 40, offline=False)
            self.assertIsNone(hc["checks"][0]["app_id"])

        # 2. startup_failure conclusion fails closed to CHECKS_FAILED
        mock_startup_fail = json.dumps({
            "total_count": 1,
            "check_runs": [{
                "name": "ci / build", "status": "completed", "conclusion": "startup_failure",
                "head_sha": "f" * 40,
                "app": {"id": 15368, "slug": "github-actions"},
            }],
        })
        proc_sf = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=mock_startup_fail, stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", return_value=proc_sf), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            hc_sf = doctor.get_hosted_checks(self.test_root, "f" * 40, offline=False)
            self.assertEqual(hc_sf["status"], "failure")
            self.assertEqual(hc_sf["reason"], "CHECKS_FAILED")



    def test_doctor_ci_legacy_status_failure_blocks_same_context(self):
        """A failing commit status blocks a green check run of the same required context."""
        payload = json.dumps({
            "total_count": 1,
            "check_runs": [{
                "name": "serial", "status": "completed", "conclusion": "success",
                "head_sha": "f" * 40, "app": {"slug": "github-actions"},
            }],
        })
        statuses = json.dumps([{"context": "serial", "state": "failure", "sha": "f" * 40}])
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=_checks_then_statuses(payload, statuses)), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            hc = doctor.get_hosted_checks(self.test_root, "f" * 40, offline=False, required_contexts=["serial"])
        self.assertEqual(hc["status"], "failure")
        self.assertEqual(hc["reason"], "CHECKS_FAILED")

    def test_doctor_ci_legacy_status_context_case_blocks(self):
        """A failing commit status blocks the required context when only the case differs."""
        payload = json.dumps({
            "total_count": 1,
            "check_runs": [{
                "name": "build", "status": "completed", "conclusion": "success",
                "head_sha": "f" * 40, "app": {"slug": "github-actions", "id": 1},
            }],
        })
        statuses = json.dumps([{"context": "Build", "state": "failure", "sha": "f" * 40}])
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=_checks_then_statuses(payload, statuses)), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            hc = doctor.get_hosted_checks(self.test_root, "f" * 40, offline=False, required_contexts=["build"])
        self.assertEqual(hc["status"], "failure")
        self.assertEqual(hc["reason"], "CHECKS_FAILED")

    def test_doctor_ci_skipped_required_context_is_not_hidden(self):
        """A skipped run of a required context is not hidden by another success of that name."""
        payload = json.dumps({
            "total_count": 2,
            "check_runs": [
                {"name": "vendor", "status": "completed", "conclusion": "success", "head_sha": "f" * 40, "app": {"id": 1, "slug": "custom"}},
                {"name": "vendor", "status": "completed", "conclusion": "skipped", "head_sha": "f" * 40, "app": {"id": 1, "slug": "custom"}},
            ],
        })
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=_checks_then_statuses(payload)), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            hc = doctor.get_hosted_checks(self.test_root, "f" * 40, offline=False, required_contexts=["vendor"])
        self.assertEqual(hc["status"], "pending")
        self.assertEqual(hc["reason"], "PENDING_CHECKS")

    def test_doctor_ci_workflows_rule_is_required(self):
        """A workflows ruleset rule stays required. An unrelated green check does not pass it."""
        proc_classic = subprocess.CompletedProcess(args=["gh"], returncode=1, stdout="", stderr="HTTP 404: Branch not protected")
        proc_rules = subprocess.CompletedProcess(
            args=["gh"], returncode=0,
            stdout='[{"ruleset_id": 7, "type": "pull_request"}, {"ruleset_id": 8, "type": "workflows", "parameters": {"workflows": [{"path": ".github/workflows/ci.yml", "ref": "refs/heads/main"}]}}]',
            stderr="",
        )
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_classic, proc_rules]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp = doctor.get_branch_protection(self.test_root, "main", offline=False)
        self.assertEqual(bp["status"], "protected")
        flows = [c for c in bp["required_checks"] if c.get("kind") == "workflow"]
        self.assertEqual(flows, [{
            "context": ".github/workflows/ci.yml",
            "app_id": None,
            "kind": "workflow",
            "ref": "refs/heads/main",
            "repository_id": None,
        }])
        named = json.dumps({
            "total_count": 1,
            "check_runs": [{
                "name": ".github/workflows/ci.yml",
                "status": "completed",
                "conclusion": "success",
                "head_sha": "a" * 40,
                "app": {"slug": "github-actions", "id": 1},
            }],
        })
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=_checks_then_statuses(named)), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            hc_named = doctor.get_hosted_checks(
                self.test_root, "a" * 40, offline=False, required_checks=bp["required_checks"],
            )
        self.assertNotEqual(hc_named["status"], "success")
        ci = {
            "sha": "a" * 40,
            "dirty": False,
            "hook_mode": "full",
            "branch_protection": bp,
            "hosted_checks": {
                "checked_sha": "a" * 40,
                "status": "success",
                "checks": [{"name": "serial", "status": "completed", "conclusion": "success"}],
            },
            "skips": {"status": "match"},
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci), "INCOMPLETE")
        ci["hosted_checks"]["checks"] = [{
            "name": ".github/workflows/ci.yml",
            "status": "completed",
            "conclusion": "success",
        }]
        self.assertEqual(doctor.evaluate_ci_outcome(ci), "INCOMPLETE")


    def test_empty_required_checks_still_honors_contexts(self):
        """An empty required_checks list does not hide required_contexts."""
        payload = json.dumps({
            "total_count": 1,
            "check_runs": [{
                "name": "serial", "status": "completed", "conclusion": "success",
                "head_sha": "f" * 40, "app": {"slug": "github-actions", "id": 1},
            }],
        })
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=_checks_then_statuses(payload)), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            hc = doctor.get_hosted_checks(
                self.test_root, "f" * 40, offline=False,
                required_checks=[], required_contexts=["security"],
            )
        self.assertEqual(hc["status"], "pending")
        self.assertEqual(hc["reason"], "PENDING_CHECKS")

    def test_unproven_ruleset_type_is_not_success(self):
        """code_scanning stays required. A green serial check does not pass it."""
        proc_classic = subprocess.CompletedProcess(args=["gh"], returncode=1, stdout="", stderr="HTTP 404: Branch not protected")
        proc_rules = subprocess.CompletedProcess(
            args=["gh"], returncode=0,
            stdout='[{"ruleset_id": 7, "type": "pull_request"}, {"ruleset_id": 9, "type": "code_scanning"}]',
            stderr="",
        )
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_classic, proc_rules]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp = doctor.get_branch_protection(self.test_root, "main", offline=False)
        self.assertEqual(bp["status"], "protected")
        unproven = [c for c in bp["required_checks"] if c.get("kind") == "unproven"]
        self.assertEqual(unproven, [{"context": "code_scanning", "app_id": None, "kind": "unproven"}])
        payload = json.dumps({
            "total_count": 1,
            "check_runs": [{
                "name": "serial", "status": "completed", "conclusion": "success",
                "head_sha": "a" * 40, "app": {"slug": "github-actions", "id": 1},
            }],
        })
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=_checks_then_statuses(payload)), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            hc = doctor.get_hosted_checks(
                self.test_root, "a" * 40, offline=False, required_checks=bp["required_checks"],
            )
        self.assertEqual(hc["status"], "pending")
        self.assertEqual(hc["reason"], "PENDING_CHECKS")
        ci = {
            "sha": "a" * 40,
            "dirty": False,
            "hook_mode": "full",
            "branch_protection": bp,
            "hosted_checks": {
                "checked_sha": "a" * 40,
                "status": "success",
                "checks": [{"name": "serial", "status": "completed", "conclusion": "success"}],
            },
            "skips": {"status": "match"},
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci), "INCOMPLETE")

    def test_malformed_required_checks_fail_closed(self):
        """A nonempty required_checks list with no string context is an API error."""
        payload = json.dumps({
            "total_count": 1,
            "check_runs": [{
                "name": "serial", "status": "completed", "conclusion": "success",
                "head_sha": "b" * 40, "app": {"slug": "github-actions", "id": 1},
            }],
        })
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=_checks_then_statuses(payload)), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            hc = doctor.get_hosted_checks(
                self.test_root, "b" * 40, offline=False, required_checks=[{}],
            )
        self.assertEqual(hc["status"], "unknown")
        self.assertEqual(hc["reason"], "API_ERROR")

    def test_mixed_required_checks_fail_closed(self):
        """One unreadable required_checks entry fails closed even if another entry is valid."""
        payload = json.dumps({
            "total_count": 1,
            "check_runs": [{
                "name": "serial", "status": "completed", "conclusion": "success",
                "head_sha": "b" * 40, "app": {"slug": "github-actions", "id": 1},
            }],
        })
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=_checks_then_statuses(payload)), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            hc = doctor.get_hosted_checks(
                self.test_root, "b" * 40, offline=False,
                required_checks=[{"context": "serial", "app_id": None}, {}, {"kind": "workflow"}],
            )
        self.assertEqual(hc["status"], "unknown")
        self.assertEqual(hc["reason"], "API_ERROR")

    def test_workflow_survives_same_name_app_check(self):
        """A same-named app check must not drop a workflow or unproven requirement."""
        proc_classic = subprocess.CompletedProcess(args=["gh"], returncode=1, stdout="", stderr="HTTP 404: Branch not protected")
        proc_rules = subprocess.CompletedProcess(
            args=["gh"], returncode=0,
            stdout=(
                '[{"ruleset_id": 9, "type": "code_scanning"},'
                ' {"ruleset_id": 8, "type": "workflows", "parameters": {"workflows": [{"path": ".github/workflows/ci.yml"}]}},'
                ' {"ruleset_id": 2, "type": "required_status_checks", "parameters": {"required_status_checks": ['
                ' {"context": ".github/workflows/ci.yml", "integration_id": 15368},'
                ' {"context": "code_scanning"}]}}]'
            ),
            stderr="",
        )
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_classic, proc_rules]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp = doctor.get_branch_protection(self.test_root, "main", offline=False)
        kinds = {c.get("kind") for c in bp["required_checks"]}
        self.assertIn("workflow", kinds)
        self.assertIn("unproven", kinds)
        payload = json.dumps({
            "total_count": 2,
            "check_runs": [
                {
                    "name": ".github/workflows/ci.yml", "status": "completed", "conclusion": "success",
                    "head_sha": "c" * 40, "app": {"slug": "github-actions", "id": 15368},
                },
                {
                    "name": "code_scanning", "status": "completed", "conclusion": "success",
                    "head_sha": "c" * 40, "app": {"slug": "github-actions", "id": 1},
                },
            ],
        })
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=_checks_then_statuses(payload)), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            hc = doctor.get_hosted_checks(
                self.test_root, "c" * 40, offline=False, required_checks=bp["required_checks"],
            )
        self.assertEqual(hc["status"], "pending")
        self.assertEqual(hc["reason"], "PENDING_CHECKS")
        ci = {
            "sha": "c" * 40,
            "dirty": False,
            "hook_mode": "full",
            "branch_protection": bp,
            "hosted_checks": {
                "checked_sha": "c" * 40,
                "status": "success",
                "checks": [
                    {"name": ".github/workflows/ci.yml", "status": "completed", "conclusion": "success", "app_id": 15368},
                    {"name": "code_scanning", "status": "completed", "conclusion": "success", "app_id": 1},
                ],
            },
            "skips": {"status": "match"},
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci), "INCOMPLETE")

    def test_any_app_requirement_survives_other_source_app_id(self):
        """Classic any-app stays beside a ruleset app id. A skipped other app is not hosted success."""
        proc_classic = subprocess.CompletedProcess(
            args=["gh"], returncode=0,
            stdout='{"url": "https://api.github.com/repos/coqui123/pbi-zerodata/branches/main/protection", "required_status_checks": {"strict": true, "contexts": ["build"], "checks": [{"context": "build", "app_id": -1}]}}',
            stderr="",
        )
        proc_rules = subprocess.CompletedProcess(
            args=["gh"], returncode=0,
            stdout='[{"ruleset_id": 2, "type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "build", "integration_id": 15368}]}}]',
            stderr="",
        )
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_classic, proc_rules]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp = doctor.get_branch_protection(self.test_root, "main", offline=False)
        app_ids = {c.get("app_id") for c in bp["required_checks"] if c.get("context") == "build"}
        self.assertEqual(app_ids, {None, 15368})
        payload = json.dumps({
            "total_count": 2,
            "check_runs": [
                {"name": "build", "status": "completed", "conclusion": "success", "head_sha": "d" * 40, "app": {"slug": "github-actions", "id": 15368}},
                {"name": "build", "status": "completed", "conclusion": "skipped", "head_sha": "d" * 40, "app": {"slug": "custom", "id": 999}},
            ],
        })
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=_checks_then_statuses(payload)), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            hc = doctor.get_hosted_checks(
                self.test_root, "d" * 40, offline=False, required_checks=bp["required_checks"],
            )
        self.assertEqual(hc["status"], "pending")
        self.assertEqual(hc["reason"], "PENDING_CHECKS")
        self.assertEqual(doctor.evaluate_ci_outcome({
            "sha": "d" * 40,
            "dirty": False,
            "hook_mode": "full",
            "branch_protection": bp,
            "hosted_checks": {
                "checked_sha": "d" * 40,
                "status": "success",
                "checks": [
                    {"name": "build", "status": "completed", "conclusion": "success", "app_id": 15368},
                    {"name": "build", "status": "completed", "conclusion": "skipped", "app_id": 999},
                ],
            },
            "skips": {"status": "match"},
        }), "INCOMPLETE")

    def test_evaluate_skipped_other_app_without_contexts(self):
        """A skipped run of a required context blocks SUCCEEDED even with no required_contexts."""
        ci = {
            "sha": "e" * 40,
            "dirty": False,
            "hook_mode": "full",
            "branch_protection": {
                "status": "protected",
                "required_checks": [{"context": "build", "app_id": 15368}],
            },
            "hosted_checks": {
                "checked_sha": "e" * 40,
                "status": "success",
                "checks": [
                    {"name": "build", "status": "completed", "conclusion": "success", "app_id": 15368},
                    {"name": "build", "status": "completed", "conclusion": "skipped", "app_id": 999},
                ],
            },
            "skips": {"status": "match"},
            "artifact_provenance": _REPLAY_PROVENANCE,
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci), "INCOMPLETE")
        ci["branch_protection"]["required_contexts"] = []
        self.assertEqual(doctor.evaluate_ci_outcome(ci), "INCOMPLETE")
        ci["hosted_checks"]["checks"] = [
            {"name": "build", "status": "completed", "conclusion": "success", "app_id": 15368},
        ]
        self.assertEqual(doctor.evaluate_ci_outcome(ci), "SUCCEEDED")

    def test_malformed_review_flag_is_api_error(self):
        """A non-boolean pull request review flag is not branch protection."""
        proc_classic = subprocess.CompletedProcess(
            args=["gh"], returncode=0,
            stdout='{"url": "https://api.github.com/repos/coqui123/pbi-zerodata/branches/main/protection", "required_pull_request_reviews": {"dismiss_stale_reviews": []}}',
            stderr="",
        )
        proc_rules = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout="[]", stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_classic, proc_rules]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp = doctor.get_branch_protection(self.test_root, "main", offline=False)
        self.assertEqual(bp["status"], "unknown")
        self.assertEqual(bp["reason"], "API_ERROR")
        proc_null = subprocess.CompletedProcess(
            args=["gh"], returncode=0,
            stdout='{"url": "https://api.github.com/repos/coqui123/pbi-zerodata/branches/main/protection", "required_pull_request_reviews": {"dismiss_stale_reviews": null}}',
            stderr="",
        )
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_null, proc_rules]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp_null = doctor.get_branch_protection(self.test_root, "main", offline=False)
        self.assertEqual(bp_null["status"], "unknown")
        self.assertEqual(bp_null["reason"], "API_ERROR")
        proc_url = subprocess.CompletedProcess(
            args=["gh"], returncode=0,
            stdout='{"url": "https://api.github.com/repos/coqui123/pbi-zerodata/branches/main/protection", "required_pull_request_reviews": {"url": null}}',
            stderr="",
        )
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_url, proc_rules]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp_url = doctor.get_branch_protection(self.test_root, "main", offline=False)
        self.assertEqual(bp_url["status"], "unknown")
        self.assertEqual(bp_url["reason"], "API_ERROR")

    def test_null_checks_list_is_api_error(self):
        """A present null checks list is not a usable status-check requirement."""
        proc_classic = subprocess.CompletedProcess(
            args=["gh"], returncode=0,
            stdout='{"url": "https://api.github.com/repos/coqui123/pbi-zerodata/branches/main/protection", "required_status_checks": {"contexts": ["build"], "checks": null}}',
            stderr="",
        )
        proc_rules = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout="[]", stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_classic, proc_rules]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp = doctor.get_branch_protection(self.test_root, "main", offline=False)
        self.assertEqual(bp["status"], "unknown")
        self.assertEqual(bp["reason"], "API_ERROR")

    def test_float_app_id_is_api_error(self):
        """A float app id is not the any-app sentinel."""
        proc_classic = subprocess.CompletedProcess(
            args=["gh"], returncode=0,
            stdout='{"url": "https://api.github.com/repos/coqui123/pbi-zerodata/branches/main/protection", "required_status_checks": {"checks": [{"context": "build", "app_id": -1.0}]}}',
            stderr="",
        )
        proc_rules = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout="[]", stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_classic, proc_rules]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp = doctor.get_branch_protection(self.test_root, "main", offline=False)
        self.assertEqual(bp["status"], "unknown")
        self.assertEqual(bp["reason"], "API_ERROR")

    def test_duplicate_checks_key_is_api_error(self):
        """A repeated checks key must not collapse to the last empty list."""
        raw = (
            '{"url": "https://api.github.com/repos/coqui123/pbi-zerodata/branches/main/protection", '
            '"required_status_checks": {"checks": [{"context": "build"}], "checks": []}, '
            '"required_pull_request_reviews": {"required_approving_review_count": 1}}'
        )
        proc_classic = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=raw, stderr="")
        proc_rules = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout="[]", stderr="")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_classic, proc_rules]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp = doctor.get_branch_protection(self.test_root, "main", offline=False)
        self.assertEqual(bp["status"], "unknown")
        self.assertEqual(bp["reason"], "API_ERROR")
        self.assertEqual(bp["required_checks"], [])

    def test_duplicate_skip_keys_are_parse_error(self):
        """A repeated key in skip evidence must not keep the last value."""
        parsed, ok = doctor._parse_skip_records(['{"test": "a", "reason": "x", "test": "b", "reason": "y"}'])
        self.assertFalse(ok)
        self.assertIsNone(parsed)
        skill = self.test_root / "skill_dup_manifest"
        (skill / "tests").mkdir(parents=True)
        (skill / "tests" / "offline_skips.json").write_text(
            '{"windows": {"a": []}, "windows": {}}',
            encoding="utf-8",
        )
        sk_manifest = doctor.get_skips_status(skill_dir=skill)
        self.assertEqual(sk_manifest["status"], "unknown")
        self.assertEqual(sk_manifest["reason"], "PARSE_ERROR")
        sidecars = self.test_root / "sidecars_dup_prov"
        sidecars.mkdir()
        (sidecars / "provenance.json").write_text(
            '{"sha": "' + ("1" * 40) + '", "platform": "windows", "status": "failed", "status": "passed", "exit_code": 0}',
            encoding="utf-8",
        )
        sk_prov = doctor.get_skips_status(skill_dir=SKILL, observed_sidecars_dir=sidecars, expected_sha="1" * 40)
        self.assertEqual(sk_prov["status"], "mismatch")
        self.assertEqual(sk_prov["reason"], "PARSE_ERROR")

    def test_ancestry_uses_captured_sha(self):
        """Membership uses the sha already captured, not a later HEAD."""
        sha = "a" * 40
        tip = "b" * 40
        seen = []

        def fake(args, cwd=None, timeout=5):
            seen.append(list(args))
            if args[:2] == ["rev-parse", "--show-toplevel"]:
                return 0, str(self.test_root)
            if args[:3] == ["rev-parse", "--verify", "-q"] and args[-1] == "HEAD":
                return 0, sha
            if args[0] == "status":
                return 0, ""
            if args[0] == "symbolic-ref":
                return 0, "refs/heads/main"
            if args[0] == "rev-parse" and args[-1] == "refs/remotes/origin/main":
                return 0, tip
            if args[0] == "merge-base":
                return 0, ""
            return 1, ""

        with patch.object(doctor, "_find_repo_root", return_value=self.test_root), \
             patch.object(doctor, "_run_git", side_effect=fake), \
             patch.object(doctor, "get_hook_mode", return_value="full"), \
             patch.object(doctor, "get_branch_protection", return_value={
                 "branch": "main", "status": "unknown", "reason": "OFFLINE",
                 "required_contexts": [], "required_checks": [],
             }), \
             patch.object(doctor, "get_hosted_checks", return_value={
                 "status": "unknown", "reason": "OFFLINE", "checks": [], "checked_sha": None,
             }), \
             patch.object(doctor, "get_skips_status", return_value={
                 "status": "unknown", "reason": "NOT_RUN", "approved_count": None,
                 "observed_count": None, "unexpected": [], "missing": [],
             }), \
             patch.object(doctor, "get_desktop_evidence", return_value=(False, "NOT_INSTALLED")):
            doctor.cmd_doctor_ci(ws_path=self.test_root, offline=True)
        merge_calls = [c for c in seen if c and c[0] == "merge-base"]
        self.assertEqual(merge_calls, [["merge-base", "--is-ancestor", "--end-of-options", sha, tip]])

    def test_duplicate_error_message_is_not_unprotected(self):
        """A repeated message key must not turn a 404 into branch not protected."""
        class _Resp:
            def __init__(self, raw):
                self._raw = raw
            def read(self):
                return self._raw
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        def open_dup(req, timeout=5):
            url = req.full_url
            if url.endswith("/protection"):
                body = b'{"message":"Not Found","message":"Branch not protected"}'
                raise doctor.urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(body))
            return _Resp(b"[]")

        def open_plain(req, timeout=5):
            url = req.full_url
            if url.endswith("/protection"):
                body = b'{"message":"Branch not protected"}'
                raise doctor.urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(body))
            return _Resp(b"[]")

        with patch.object(doctor.shutil, "which", return_value=None), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "ghs_test_token"}), \
             patch.object(doctor.urllib.request, "urlopen", side_effect=open_dup), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp_dup = doctor.get_branch_protection(self.test_root, "main", offline=False)
        self.assertEqual(bp_dup["status"], "unknown")
        self.assertEqual(bp_dup["reason"], "NOT_FOUND")

        with patch.object(doctor.shutil, "which", return_value=None), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "ghs_test_token"}), \
             patch.object(doctor.urllib.request, "urlopen", side_effect=open_plain), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp_plain = doctor.get_branch_protection(self.test_root, "main", offline=False)
        self.assertEqual(bp_plain["status"], "unprotected")
        self.assertEqual(bp_plain["reason"], "UNPROTECTED")

    def test_list_error_message_is_not_unprotected(self):
        """A list message must not match the unprotected phrase."""
        class _Resp:
            def __init__(self, raw):
                self._raw = raw
            def read(self):
                return self._raw
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        rules = b'[{"ruleset_id": 1, "type": "pull_request"}]'

        def open_list(req, timeout=5):
            url = req.full_url
            if url.endswith("/protection"):
                body = b'{"message":["Not Found","Branch not protected"]}'
                raise doctor.urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(body))
            return _Resp(rules)

        def open_text(req, timeout=5):
            url = req.full_url
            if url.endswith("/protection"):
                body = b'{"message":"Branch not protected"}'
                raise doctor.urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(body))
            return _Resp(rules)

        with patch.object(doctor.shutil, "which", return_value=None), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "ghs_test_token"}), \
             patch.object(doctor.urllib.request, "urlopen", side_effect=open_list), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp_list = doctor.get_branch_protection(self.test_root, "main", offline=False)
        self.assertEqual(bp_list["status"], "unknown")
        self.assertEqual(bp_list["reason"], "NOT_FOUND")

        with patch.object(doctor.shutil, "which", return_value=None), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "ghs_test_token"}), \
             patch.object(doctor.urllib.request, "urlopen", side_effect=open_text), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp_text = doctor.get_branch_protection(self.test_root, "main", offline=False)
        self.assertEqual(bp_text["status"], "protected")
        self.assertEqual(bp_text["ruleset"], "ruleset")

    def test_ls_remote_uses_exact_ref(self):
        """A longer ref that merely ends with the branch name is not the tip."""
        sha = "a" * 40
        wrong = "1" * 40
        right = "2" * 40
        seen = []

        def fake(args, cwd=None, timeout=5):
            seen.append(list(args))
            if args[:2] == ["rev-parse", "--show-toplevel"]:
                return 0, str(self.test_root)
            if args[:3] == ["rev-parse", "--verify", "-q"] and args[-1] == "HEAD":
                return 0, sha
            if args[0] == "status":
                return 0, ""
            if args[0] == "symbolic-ref":
                return 0, "refs/heads/main"
            if args[:3] == ["remote", "get-url", "origin"]:
                return 0, "https://github.com/owner/repo.git"
            if args[0] == "rev-parse" and str(args[-1]).startswith("refs/remotes/origin/"):
                return 0, "c" * 40
            if args[0] == "ls-remote" and "--get-url" in args:
                return 0, args[-1]
            if args[0] == "ls-remote":
                return 0, f"{wrong}\trefs/heads/a/refs/heads/main\n{right}\trefs/heads/main"
            if args[0] == "merge-base":
                return 0, ""
            return 1, ""

        with patch.object(doctor, "_find_repo_root", return_value=self.test_root), \
             patch.object(doctor, "_run_git", side_effect=fake), \
             patch.object(doctor, "get_hook_mode", return_value="full"), \
             patch.object(doctor, "get_branch_protection", return_value={
                 "branch": "main", "status": "unknown", "reason": "OFFLINE",
                 "required_contexts": [], "required_checks": [],
             }), \
             patch.object(doctor, "get_hosted_checks", return_value={
                 "status": "unknown", "reason": "OFFLINE", "checks": [], "checked_sha": None,
             }), \
             patch.object(doctor, "get_skips_status", return_value={
                 "status": "unknown", "reason": "NOT_RUN", "approved_count": None,
                 "observed_count": None, "unexpected": [], "missing": [],
             }), \
             patch.object(doctor, "get_desktop_evidence", return_value=(False, "NOT_INSTALLED")):
            doctor.cmd_doctor_ci(ws_path=self.test_root, offline=False)
        merge_calls = [c for c in seen if c and c[0] == "merge-base"]
        self.assertEqual(merge_calls, [["merge-base", "--is-ancestor", "--end-of-options", sha, right]])

        seen.clear()

        def fake_only_suffix(args, cwd=None, timeout=5):
            seen.append(list(args))
            if args[:2] == ["rev-parse", "--show-toplevel"]:
                return 0, str(self.test_root)
            if args[:3] == ["rev-parse", "--verify", "-q"] and args[-1] == "HEAD":
                return 0, sha
            if args[0] == "status":
                return 0, ""
            if args[0] == "symbolic-ref":
                return 0, "refs/heads/main"
            if args[0] == "rev-parse" and str(args[-1]).startswith("refs/remotes/origin/"):
                return 0, "c" * 40
            if args[0] == "ls-remote" and "--get-url" in args:
                return 0, args[-1]
            if args[0] == "ls-remote":
                return 0, f"{wrong}\trefs/heads/a/refs/heads/main"
            if args[0] == "merge-base":
                return 0, ""
            return 1, ""

        with patch.object(doctor, "_find_repo_root", return_value=self.test_root), \
             patch.object(doctor, "_run_git", side_effect=fake_only_suffix), \
             patch.object(doctor, "get_hook_mode", return_value="full"), \
             patch.object(doctor, "get_hosted_checks", return_value={
                 "status": "success", "reason": None, "checks": [], "checked_sha": sha,
             }), \
             patch.object(doctor, "get_skips_status", return_value={
                 "status": "match", "reason": None, "approved_count": 0,
                 "observed_count": 0, "unexpected": [], "missing": [],
             }), \
             patch.object(doctor, "get_desktop_evidence", return_value=(False, "NOT_INSTALLED")):
            out = doctor.cmd_doctor_ci(ws_path=self.test_root, offline=False)
        self.assertEqual(out["ci"]["branch_protection"]["reason"], "BRANCH_MISMATCH")
        self.assertFalse(any(c and c[0] == "merge-base" for c in seen))

    def test_gh_json_error_body_beats_stderr_phrase(self):
        """A JSON error body is classified. Stderr text does not override it."""
        dup = '{"message":"Not Found","message":"Branch not protected"}'
        proc_dup = subprocess.CompletedProcess(
            args=["gh"], returncode=1, stdout=dup, stderr="HTTP 404: Branch not protected",
        )
        proc_rules = subprocess.CompletedProcess(
            args=["gh"], returncode=0,
            stdout='[{"ruleset_id": 1, "type": "pull_request"}]',
            stderr="",
        )
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_dup, proc_rules]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp_dup = doctor.get_branch_protection(self.test_root, "main", offline=False)
        self.assertEqual(bp_dup["status"], "unknown")
        self.assertEqual(bp_dup["reason"], "API_ERROR")

        listed = '{"message":["Not Found","Branch not protected"]}'
        proc_list = subprocess.CompletedProcess(
            args=["gh"], returncode=1, stdout=listed, stderr="HTTP 404: Branch not protected",
        )
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_list, proc_rules]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp_list = doctor.get_branch_protection(self.test_root, "main", offline=False)
        self.assertEqual(bp_list["status"], "unknown")
        self.assertEqual(bp_list["reason"], "NOT_FOUND")

    def test_unprotected_phrase_must_be_exact(self):
        """A 404 that merely contains the phrase is not unprotected."""
        proc_classic = subprocess.CompletedProcess(
            args=["gh"], returncode=1, stdout="",
            stderr="HTTP 404: Unable to determine whether branch not protected",
        )
        proc_rules = subprocess.CompletedProcess(
            args=["gh"], returncode=0,
            stdout='[{"ruleset_id": 1, "type": "pull_request"}]',
            stderr="",
        )
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(doctor.subprocess, "run", side_effect=[proc_classic, proc_rules]), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bp = doctor.get_branch_protection(self.test_root, "main", offline=False)
        self.assertEqual(bp["status"], "unknown")
        self.assertEqual(bp["reason"], "NOT_FOUND")

    def test_dirty_status_includes_ignored_submodules(self):
        """ignore=dirty must not hide a modified submodule."""
        seen = []

        def fake(args, cwd=None, timeout=5):
            seen.append(list(args))
            return 0, ""

        def raw(args, cwd=None, timeout=5):
            return 0, b""

        with patch.object(doctor, "_run_git", side_effect=fake), \
             patch.object(doctor, "_run_git_raw", side_effect=raw):
            dirty, reason = doctor.get_dirty_status(self.test_root)
        self.assertIs(dirty, False)
        self.assertIsNone(reason)
        self.assertIn(
            ["status", "--porcelain", "--untracked-files=normal", "--ignore-submodules=none"],
            seen,
        )

    def _repo_with_file(self, name: str, filename: str, text: bytes) -> Path:
        repo = self.test_root / name
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        for key, value in (("user.email", "t@example.com"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(repo), "config", key, value], check=True, capture_output=True)
        (repo / filename).write_bytes(text)
        subprocess.run(["git", "-C", str(repo), "add", "--", filename], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
        return repo

    def test_assume_unchanged_edit_is_dirty(self):
        """An assume-unchanged edit is dirty and the index bytes stay put."""
        repo = self._repo_with_file("assume-edit", "f.txt", b"one\n")
        subprocess.run(["git", "-C", str(repo), "update-index", "--assume-unchanged", "f.txt"], check=True, capture_output=True)
        (repo / "f.txt").write_bytes(b"two\n")
        index_before = (repo / ".git" / "index").read_bytes()
        dirty, reason = doctor.get_dirty_status(repo)
        index_after = (repo / ".git" / "index").read_bytes()
        self.assertTrue(dirty)
        self.assertEqual(reason, "DIRTY_WORKSPACE")
        self.assertEqual(index_before, index_after)

    def test_assume_unchanged_match_stays_clean(self):
        """The assume-unchanged bit alone is clean, including a CRLF worktree."""
        repo = self._repo_with_file("assume-clean", "f.txt", b"one\n")
        subprocess.run(["git", "-C", str(repo), "update-index", "--assume-unchanged", "f.txt"], check=True, capture_output=True)
        index_before = (repo / ".git" / "index").read_bytes()
        dirty, reason = doctor.get_dirty_status(repo)
        self.assertIs(dirty, False)
        self.assertIsNone(reason)
        self.assertEqual(index_before, (repo / ".git" / "index").read_bytes())

        crlf = self.test_root / "assume-crlf"
        crlf.mkdir()
        subprocess.run(["git", "init", str(crlf)], check=True, capture_output=True)
        for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("core.autocrlf", "true")):
            subprocess.run(["git", "-C", str(crlf), "config", key, value], check=True, capture_output=True)
        (crlf / "c.txt").write_bytes(b"a\r\n")
        subprocess.run(["git", "-C", str(crlf), "add", "--", "c.txt"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(crlf), "commit", "-m", "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(crlf), "update-index", "--assume-unchanged", "c.txt"], check=True, capture_output=True)
        index_before = (crlf / ".git" / "index").read_bytes()
        dirty, reason = doctor.get_dirty_status(crlf)
        self.assertIs(dirty, False)
        self.assertIsNone(reason)
        self.assertEqual(index_before, (crlf / ".git" / "index").read_bytes())

    def test_skip_worktree_edit_or_deletion_is_dirty(self):
        """Skip-worktree hides an edit and a deletion from status. Both are dirty."""
        edited = self._repo_with_file("skip-edit", "s.txt", b"one\n")
        subprocess.run(["git", "-C", str(edited), "update-index", "--skip-worktree", "s.txt"], check=True, capture_output=True)
        (edited / "s.txt").write_bytes(b"two\n")
        index_before = (edited / ".git" / "index").read_bytes()
        dirty, reason = doctor.get_dirty_status(edited)
        self.assertTrue(dirty)
        self.assertEqual(reason, "DIRTY_WORKSPACE")
        self.assertEqual(index_before, (edited / ".git" / "index").read_bytes())

        removed = self._repo_with_file("skip-gone", "s.txt", b"one\n")
        subprocess.run(["git", "-C", str(removed), "update-index", "--skip-worktree", "s.txt"], check=True, capture_output=True)
        (removed / "s.txt").unlink()
        index_before = (removed / ".git" / "index").read_bytes()
        dirty, reason = doctor.get_dirty_status(removed)
        self.assertTrue(dirty)
        self.assertEqual(reason, "DIRTY_WORKSPACE")
        self.assertEqual(index_before, (removed / ".git" / "index").read_bytes())

    def test_ignorestat_edit_is_dirty(self):
        """core.ignoreStat hides an edit from status and still marks the tree dirty."""
        repo = self.test_root / "ignore-stat"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("core.ignoreStat", "true")):
            subprocess.run(["git", "-C", str(repo), "config", key, value], check=True, capture_output=True)
        (repo / "f.txt").write_bytes(b"one\n")
        subprocess.run(["git", "-C", str(repo), "add", "--", "f.txt"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
        (repo / "f.txt").write_bytes(b"two\n")
        status = subprocess.run(
            ["git", "-C", str(repo), "-c", "core.fsmonitor=false", "--no-optional-locks", "status", "--porcelain", "--untracked-files=normal", "--ignore-submodules=none"],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual(status.stdout.strip(), "")
        index_before = (repo / ".git" / "index").read_bytes()
        dirty, reason = doctor.get_dirty_status(repo)
        self.assertTrue(dirty)
        self.assertEqual(reason, "DIRTY_WORKSPACE")
        self.assertEqual(index_before, (repo / ".git" / "index").read_bytes())

    def _porcelain(self, repo: Path) -> str:
        status = subprocess.run(
            ["git", "-C", str(repo), "-c", "core.fsmonitor=false", "--no-optional-locks", "status", "--porcelain", "--untracked-files=normal", "--ignore-submodules=none"],
            check=True, capture_output=True, text=True,
        )
        return status.stdout.strip()

    def _index_bytes(self, repo: Path) -> bytes:
        git_path = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--git-path", "index"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        return (repo / git_path).read_bytes()

    def test_submodule_assume_unchanged_edit_is_dirty(self):
        """A hidden edit inside a checked-out submodule stays dirty."""
        sup = self.test_root / "sup"
        sup.mkdir()
        subprocess.run(["git", "init", str(sup)], check=True, capture_output=True)
        for key, value in (("user.email", "t@example.com"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(sup), "config", key, value], check=True, capture_output=True)
        sub = sup / "sub"
        sub.mkdir()
        subprocess.run(["git", "init", str(sub)], check=True, capture_output=True)
        for key, value in (("user.email", "t@example.com"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(sub), "config", key, value], check=True, capture_output=True)
        (sub / "s.txt").write_bytes(b"one\n")
        subprocess.run(["git", "-C", str(sub), "add", "--", "s.txt"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(sub), "commit", "-m", "s"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(sup), "submodule", "add", "--force", "./sub", "sub"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(sup), "commit", "-m", "addsub"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(sub), "update-index", "--assume-unchanged", "s.txt"], check=True, capture_output=True)
        (sub / "s.txt").write_bytes(b"two\n")
        self.assertEqual(self._porcelain(sup), "")
        self.assertEqual(self._porcelain(sub), "")
        sup_before = self._index_bytes(sup)
        sub_before = self._index_bytes(sub)
        dirty, reason = doctor.get_dirty_status(sup)
        self.assertTrue(dirty)
        self.assertEqual(reason, "DIRTY_WORKSPACE")
        self.assertEqual(sup_before, self._index_bytes(sup))
        self.assertEqual(sub_before, self._index_bytes(sub))

    def test_flagged_gitlink_commit_is_dirty(self):
        """A hidden gitlink whose checkout moved is dirty."""
        for name, flag in (("gitlink-assume", "--assume-unchanged"), ("gitlink-skip", "--skip-worktree")):
            sup = self.test_root / name
            sup.mkdir()
            subprocess.run(["git", "init", str(sup)], check=True, capture_output=True)
            for key, value in (("user.email", "t@example.com"), ("user.name", "t")):
                subprocess.run(["git", "-C", str(sup), "config", key, value], check=True, capture_output=True)
            sub = sup / "sub"
            sub.mkdir()
            subprocess.run(["git", "init", str(sub)], check=True, capture_output=True)
            for key, value in (("user.email", "t@example.com"), ("user.name", "t")):
                subprocess.run(["git", "-C", str(sub), "config", key, value], check=True, capture_output=True)
            (sub / "f.txt").write_bytes(b"one\n")
            subprocess.run(["git", "-C", str(sub), "add", "--", "f.txt"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(sub), "commit", "-m", "c1"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(sup), "submodule", "add", "--force", "./sub", "sub"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(sup), "commit", "-m", "add"], check=True, capture_output=True)
            (sub / "f.txt").write_bytes(b"two\n")
            subprocess.run(["git", "-C", str(sub), "add", "--", "f.txt"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(sub), "commit", "-m", "c2"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(sup), "add", "sub"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(sup), "commit", "-m", "c2link"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(sub), "checkout", "-q", "HEAD~1"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(sup), "update-index", flag, "sub"], check=True, capture_output=True)
            self.assertEqual(self._porcelain(sup), "")
            sup_before = self._index_bytes(sup)
            sub_before = self._index_bytes(sub)
            dirty, reason = doctor.get_dirty_status(sup)
            self.assertTrue(dirty)
            self.assertEqual(reason, "DIRTY_WORKSPACE")
            self.assertEqual(sup_before, self._index_bytes(sup))
            self.assertEqual(sub_before, self._index_bytes(sub))

    def test_symlink_over_matching_gitlink_is_dirty(self):
        """A symlink over a gitlink is dirty even when the target commit matches."""
        sup = self.test_root / "gitlink-link"
        sup.mkdir()
        subprocess.run(["git", "init", str(sup)], check=True, capture_output=True)
        for key, value in (("user.email", "t@example.com"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(sup), "config", key, value], check=True, capture_output=True)
        sub = sup / "sub"
        sub.mkdir()
        subprocess.run(["git", "init", str(sub)], check=True, capture_output=True)
        for key, value in (("user.email", "t@example.com"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(sub), "config", key, value], check=True, capture_output=True)
        (sub / "f.txt").write_bytes(b"one\n")
        subprocess.run(["git", "-C", str(sub), "add", "--", "f.txt"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(sub), "commit", "-m", "c1"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(sup), "submodule", "add", "--force", "./sub", "sub"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(sup), "commit", "-m", "add"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(sup), "update-index", "--skip-worktree", "sub"], check=True, capture_output=True)
        self.assertEqual(self._porcelain(sup), "")
        before = self._index_bytes(sup)
        with patch.object(Path, "is_symlink", return_value=True):
            dirty, reason = doctor.get_dirty_status(sup)
        self.assertTrue(dirty)
        self.assertEqual(reason, "DIRTY_WORKSPACE")
        self.assertEqual(before, self._index_bytes(sup))


    def test_filemode_assume_unchanged_is_dirty(self):
        """A hidden executable-bit change is dirty when Git tracks file mode."""
        repo = self._repo_with_file("filemode", "f.txt", b"one\n")
        subprocess.run(["git", "-C", str(repo), "config", "core.filemode", "true"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "update-index", "--chmod=+x", "f.txt"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "x"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "update-index", "--assume-unchanged", "f.txt"], check=True, capture_output=True)
        self.assertEqual(self._porcelain(repo), "")
        before = self._index_bytes(repo)
        dirty, reason = doctor.get_dirty_status(repo)
        self.assertTrue(dirty)
        self.assertEqual(reason, "DIRTY_WORKSPACE")
        self.assertEqual(before, self._index_bytes(repo))

    def test_symlink_type_assume_unchanged_is_dirty(self):
        """A hidden symlink-type change is dirty when Git tracks symlinks."""
        repo = self.test_root / "link"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("core.symlinks", "true")):
            subprocess.run(["git", "-C", str(repo), "config", key, value], check=True, capture_output=True)
        (repo / "f.txt").write_bytes(b"target.txt")
        subprocess.run(["git", "-C", str(repo), "add", "--", "f.txt"], check=True, capture_output=True)
        sha = subprocess.run(
            ["git", "-C", str(repo), "hash-object", "--", "f.txt"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(["git", "-C", str(repo), "update-index", "--cacheinfo", f"120000,{sha},f.txt"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "link"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "update-index", "--assume-unchanged", "f.txt"], check=True, capture_output=True)
        self.assertEqual(self._porcelain(repo), "")
        self.assertFalse((repo / "f.txt").is_symlink())
        before = self._index_bytes(repo)
        dirty, reason = doctor.get_dirty_status(repo)
        self.assertTrue(dirty)
        self.assertEqual(reason, "DIRTY_WORKSPACE")
        self.assertEqual(before, self._index_bytes(repo))

    def test_symlink_retarget_hashes_link_text(self):
        """A retargeted symlink is dirty even when the new target file has the old link text."""
        repo = self._repo_with_file("symretarget", "f.txt", b"one\n")
        before = (repo / ".git" / "index").read_bytes()
        old = subprocess.run(
            ["git", "-C", str(repo), "hash-object", "--stdin"],
            input=b"prod.yml", check=True, capture_output=True,
        ).stdout.decode().strip()
        with patch.object(Path, "is_symlink", return_value=True), patch.object(doctor.os, "readlink", return_value="dev.yml"):
            dirty, reason = doctor._flagged_worktree(repo, "f.txt", "120000", old, True, False, False, True)
        self.assertTrue(dirty)
        self.assertEqual(reason, "DIRTY_WORKSPACE")
        with patch.object(Path, "is_symlink", return_value=True), patch.object(doctor.os, "readlink", return_value="prod.yml"):
            dirty, reason = doctor._flagged_worktree(repo, "f.txt", "120000", old, True, False, False, True)
        self.assertIs(dirty, False)
        self.assertIsNone(reason)
        self.assertEqual(before, (repo / ".git" / "index").read_bytes())

    def test_worktree_symlink_over_file_is_dirty_when_symlinks_off(self):
        """A symlink over a regular index entry is dirty even when Git does not track symlinks."""
        repo = self._repo_with_file("link-over", "f.txt", b"one\n")
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", ":0:f.txt"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        before = self._index_bytes(repo)

        def boom(*_args, **_kwargs):
            raise AssertionError("hashed")

        with patch.object(Path, "is_symlink", return_value=True), \
             patch.object(doctor, "_worktree_blob", side_effect=boom):
            dirty, reason = doctor._flagged_worktree(repo, "f.txt", "100644", sha, True, False, False, False)
        self.assertTrue(dirty)
        self.assertEqual(reason, "DIRTY_WORKSPACE")
        self.assertEqual(before, self._index_bytes(repo))

    def test_symlink_index_file_stays_clean_when_symlinks_off(self):
        """A 120000 entry checked out as a regular file stays clean when Git does not track symlinks."""
        repo = self.test_root / "link-plain"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("core.symlinks", "false")):
            subprocess.run(["git", "-C", str(repo), "config", key, value], check=True, capture_output=True)
        (repo / "f.txt").write_bytes(b"target.txt")
        subprocess.run(["git", "-C", str(repo), "add", "--", "f.txt"], check=True, capture_output=True)
        sha = subprocess.run(
            ["git", "-C", str(repo), "hash-object", "--", "f.txt"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(["git", "-C", str(repo), "update-index", "--cacheinfo", f"120000,{sha},f.txt"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "link"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "update-index", "--assume-unchanged", "f.txt"], check=True, capture_output=True)
        self.assertEqual(self._porcelain(repo), "")
        self.assertFalse((repo / "f.txt").is_symlink())
        before = self._index_bytes(repo)
        dirty, reason = doctor.get_dirty_status(repo)
        self.assertIs(dirty, False)
        self.assertIsNone(reason)
        self.assertEqual(before, self._index_bytes(repo))

    def test_non_utf8_index_listing_is_not_clean(self):
        """A lossy path spelling is not hashed. Bad UTF-8 is not a clean tree."""
        def fake(args, cwd=None, timeout=5):
            if args and args[0] == "status":
                return 0, ""
            return 1, ""

        def raw(args, cwd=None, timeout=5):
            return 0, b"\xff"

        with patch.object(doctor, "_run_git", side_effect=fake), \
             patch.object(doctor, "_run_git_raw", side_effect=raw):
            dirty, reason = doctor.get_dirty_status(self.test_root)
        self.assertIsNone(dirty)
        self.assertEqual(reason, "DIFF_INDEX_FAILED")

    def test_trailing_space_checkout_does_not_use_sibling(self):
        """A path whose name ends in a space must not select the checkout without that space."""
        sibling = self._repo_with_file("repo", "f.txt", b"one\n")
        spaced = Path(str(sibling) + " ")
        found = doctor._find_repo_root(sibling)
        self.assertEqual(found, sibling.resolve())
        payload = (str(sibling) + " \n").encode()

        def raw(args, cwd=None, timeout=5):
            if args[:2] == ["rev-parse", "--show-toplevel"]:
                return 0, payload
            return 1, b""

        with patch.object(doctor, "_run_git_raw", side_effect=raw):
            collapsed = doctor._find_repo_root(sibling)
        self.assertIsNone(collapsed)
        out = doctor.cmd_doctor_ci(ws_path=spaced, offline=True)
        self.assertIsNone(out["ci"]["sha"])
        self.assertNotEqual(out["status"], "SUCCEEDED")

    def test_trailing_cr_checkout_does_not_use_sibling(self):
        """A toplevel name that ends in CR is not the sibling without that CR."""
        self.assertEqual(doctor._git_text_line(b"/work/repo\r\n"), "/work/repo\r")
        self.assertEqual(doctor._git_text_line(b"/work/repo\n"), "/work/repo")
        sibling = self._repo_with_file("crrepo", "f.txt", b"one\n")
        payload = (str(sibling) + "\r\n").encode()

        def raw(args, cwd=None, timeout=5):
            if args[:2] == ["rev-parse", "--show-toplevel"]:
                return 0, payload
            return 1, b""

        with patch.object(doctor, "_run_git_raw", side_effect=raw):
            found = doctor._find_repo_root(sibling)
        self.assertNotEqual(found, sibling.resolve())
        if os.name != "nt":
            cr_dir = Path(str(sibling) + "\r")
            cr_dir.mkdir()
            with patch.object(doctor, "_run_git_raw", side_effect=raw):
                found = doctor._find_repo_root(sibling)
            self.assertEqual(found, cr_dir.resolve())

    def test_hook_path_keeps_embedded_cr(self):
        """An embedded CR in the hook path is not a different file."""
        def fake_run(cmd, **kwargs):
            self.assertIsNot(kwargs.get("text"), True)
            return subprocess.CompletedProcess(cmd, 0, stdout=b".git/ho\rks/pre-push\n", stderr=b"")

        paths = []

        def spy_is_file(self):
            paths.append(str(self))
            return False

        with patch.object(doctor.subprocess, "run", side_effect=fake_run), \
             patch.object(Path, "is_file", spy_is_file):
            mode = doctor.get_hook_mode(self.test_root)
        self.assertEqual(mode, "not_installed")
        self.assertTrue(any("\r" in path for path in paths))

    def test_hook_path_rejects_non_utf8(self):
        """A non-UTF-8 hook path is not opened under a replacement name."""
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout=b"\xff\n", stderr=b"")

        def boom(self):
            raise AssertionError("opened")

        with patch.object(doctor.subprocess, "run", side_effect=fake_run), \
             patch.object(Path, "is_file", boom):
            mode = doctor.get_hook_mode(self.test_root)
        self.assertEqual(mode, "unknown")

    def test_hook_path_keeps_leading_space(self):
        """A leading space in the hook path is not a different file."""
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout=b" hooks/pre-push\n", stderr=b"")

        paths = []

        def spy_is_file(self):
            paths.append(str(self))
            return False

        with patch.object(doctor.subprocess, "run", side_effect=fake_run), \
             patch.object(Path, "is_file", spy_is_file):
            mode = doctor.get_hook_mode(self.test_root)
        self.assertEqual(mode, "not_installed")
        self.assertTrue(any(path.split("hooks")[0].endswith(" ") for path in paths))

    def test_owner_exec_bit_only(self):
        """Git treats only the owner execute bit as a mode change."""
        repo = self._repo_with_file("owner-exec", "f.txt", b"one\n")
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", ":0:f.txt"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        class Stat:
            def __init__(self, mode):
                self.st_mode = mode

        with patch.object(Path, "lstat", lambda self: Stat(0o100755)):
            dirty, reason = doctor._flagged_worktree(repo, "f.txt", "100755", sha, True, False, True, False)
        self.assertIs(dirty, False)
        self.assertIsNone(reason)
        with patch.object(Path, "lstat", lambda self: Stat(0o100655)):
            dirty, reason = doctor._flagged_worktree(repo, "f.txt", "100755", sha, True, False, True, False)
        self.assertTrue(dirty)
        self.assertEqual(reason, "DIRTY_WORKSPACE")

    def test_head_move_during_dirty_check_is_not_clean(self):
        """A clean tree at a later HEAD does not clear the captured commit."""
        first = "a" * 40
        second = "b" * 40
        with patch.object(doctor, "get_head_sha", side_effect=[(first, None), (second, None)]), \
             patch.object(doctor, "get_dirty_status", return_value=(False, None)), \
             patch.object(doctor, "_find_repo_root", return_value=self.test_root):
            out = doctor.cmd_doctor_ci(ws_path=self.test_root, offline=True)
        self.assertIsNone(out["ci"]["dirty"])
        self.assertEqual(out["ci"]["dirty_reason"], "DIFF_INDEX_FAILED")
        self.assertEqual(out["ci"]["sha"], first)
        self.assertNotEqual(out["status"], "SUCCEEDED")


    def test_hidden_bit_probe_failure_is_not_clean(self):
        """A failed ls-files probe does not report a clean tree."""
        def fake(args, cwd=None, timeout=5):
            if args and args[0] == "status":
                return 0, ""
            return 1, ""

        def raw(args, cwd=None, timeout=5):
            return 1, b""

        with patch.object(doctor, "_run_git", side_effect=fake), \
             patch.object(doctor, "_run_git_raw", side_effect=raw):
            dirty, reason = doctor.get_dirty_status(self.test_root)
        self.assertIsNone(dirty)
        self.assertEqual(reason, "DIFF_INDEX_FAILED")

    def test_hosted_evidence_stays_on_one_origin(self):
        """Same SHA on two forks must not mix one fork's requirements with the other's checks."""
        sha = "a" * 40
        urls = []
        ls_remotes = []
        slugs = []

        def fake(args, cwd=None, timeout=5):
            if args[:3] == ["remote", "get-url", "origin"]:
                url = "https://github.com/owner/a.git" if not urls else "https://github.com/owner/b.git"
                urls.append(url)
                return 0, url
            if args[0] == "symbolic-ref":
                return 0, "refs/heads/main"
            if args[0] == "rev-parse" and str(args[-1]).startswith("refs/remotes/origin/"):
                return 0, sha
            if args[0] == "ls-remote" and "--get-url" in args:
                return 0, args[-1]
            if args[0] == "ls-remote":
                ls_remotes.append(args[2])
                if args[2] == "https://github.com/owner/a.git":
                    return 0, f"{sha}\trefs/heads/main"
                return 0, f"{'b' * 40}\trefs/heads/main"
            if args[0] == "merge-base":
                return 0, ""
            return 1, ""

        def api(slug, path, cwd=None, timeout=5):
            slugs.append(slug)
            if slug == "owner/a" and "/protection" in path:
                return 200, {
                    "url": "https://api.github.com/repos/owner/a/branches/main/protection",
                    "required_status_checks": {"strict": True, "contexts": ["build"]},
                    "enforce_admins": {"enabled": True},
                    "required_pull_request_reviews": None,
                }, None
            if slug == "owner/b" and "/protection" in path:
                return 200, {
                    "url": "https://api.github.com/repos/owner/b/branches/main/protection",
                    "required_status_checks": {"strict": True, "contexts": ["build", "security"]},
                    "enforce_admins": {"enabled": True},
                    "required_pull_request_reviews": None,
                }, None
            if path.startswith("rules/"):
                return 200, [], None
            if slug == "owner/b" and "check-runs" in path:
                return 200, {
                    "total_count": 1,
                    "check_runs": [{
                        "name": "build",
                        "status": "completed",
                        "conclusion": "success",
                        "head_sha": sha,
                        "app": {"slug": "github-actions", "id": 1},
                    }],
                }, None
            if "check-runs" in path:
                return 200, {"total_count": 0, "check_runs": []}, None
            if "statuses" in path:
                return 200, [], None
            return -1, {}, "API_ERROR"

        with patch.object(doctor, "_find_repo_root", return_value=self.test_root), \
             patch.object(doctor, "get_head_sha", return_value=(sha, None)), \
             patch.object(doctor, "get_dirty_status", return_value=(False, None)), \
             patch.object(doctor, "get_hook_mode", return_value="full"), \
             patch.object(doctor, "_run_git", side_effect=fake), \
             patch.object(doctor, "_api_get", side_effect=api), \
             patch.object(doctor, "get_skips_status", return_value={
                 "status": "match", "reason": None, "approved_count": 0,
                 "observed_count": 0, "unexpected": [], "missing": [],
             }), \
             patch.object(doctor, "get_desktop_evidence", return_value=(False, "NOT_INSTALLED")):
            out = doctor.cmd_doctor_ci(ws_path=self.test_root, offline=False)
        self.assertEqual(urls, ["https://github.com/owner/a.git"])
        self.assertEqual(ls_remotes, ["https://github.com/owner/a.git"])
        self.assertEqual(set(slugs), {"owner/a"})
        self.assertEqual(out["ci"]["branch_protection"]["required_contexts"], ["build"])
        self.assertNotEqual(out["ci"]["hosted_checks"]["status"], "success")
        self.assertNotEqual(out["status"], "SUCCEEDED")

    def test_same_name_tag_does_not_retarget_branch(self):
        """A tag named main must not send membership or protection to heads/main."""
        repo = self.test_root / "ambig"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True, capture_output=True)
        (repo / "f.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "tag", "main"], check=True, capture_output=True)
        self.assertEqual(doctor.get_current_branch(repo), ("main", None))
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", sha], check=True, capture_output=True)
        seen = []
        real_run = doctor._run_git

        def spy(args, cwd=None, timeout=5):
            seen.append(list(args))
            return real_run(args, cwd=cwd, timeout=timeout)

        branches = []

        def bp(repo_root, branch, branch_reason=None, offline=False, timeout=5, origin_slug=None):
            branches.append(branch)
            return {
                "branch": branch, "status": "unknown", "reason": "OFFLINE",
                "ruleset": None, "required_contexts": [], "required_checks": [],
            }

        with patch.object(doctor, "_run_git", side_effect=spy), \
             patch.object(doctor, "get_branch_protection", side_effect=bp), \
             patch.object(doctor, "get_hosted_checks", return_value={
                 "status": "unknown", "reason": "OFFLINE", "checks": [], "checked_sha": None,
             }), \
             patch.object(doctor, "get_skips_status", return_value={
                 "status": "unknown", "reason": "NOT_RUN", "approved_count": None,
                 "observed_count": None, "unexpected": [], "missing": [],
             }), \
             patch.object(doctor, "get_desktop_evidence", return_value=(False, "NOT_INSTALLED")):
            doctor.cmd_doctor_ci(ws_path=repo, offline=True)
        refs = [
            c[-1] for c in seen
            if c and c[0] == "rev-parse" and str(c[-1]).startswith("refs/remotes/origin/")
        ]
        self.assertEqual(refs, ["refs/remotes/origin/main"])
        self.assertEqual(branches, ["main"])

    def test_padded_origin_url_is_not_hosted_success(self):
        """A padded origin must not borrow success from the unpadded repository."""
        sha = "a" * 40
        clean = "https://github.com/owner/a.git"

        def api(slug, path, cwd=None, timeout=5):
            if slug == "owner/a" and "/protection" in path:
                return 200, {
                    "url": "https://api.github.com/repos/owner/a/branches/main/protection",
                    "required_status_checks": {"strict": True, "contexts": ["build"]},
                    "enforce_admins": {"enabled": True},
                    "required_pull_request_reviews": None,
                }, None
            if path.startswith("rules/"):
                return 200, [], None
            if "check-runs" in path:
                return 200, {
                    "total_count": 1,
                    "check_runs": [{
                        "name": "build",
                        "status": "completed",
                        "conclusion": "success",
                        "head_sha": sha,
                        "app": {"slug": "github-actions", "id": 1},
                    }],
                }, None
            if "statuses" in path:
                return 200, [], None
            return -1, {}, "API_ERROR"

        def run(url):
            slugs = []
            remotes = []

            def fake(args, cwd=None, timeout=5):
                if args[:3] == ["remote", "get-url", "origin"]:
                    return 0, url
                if args[0] == "symbolic-ref":
                    return 0, "refs/heads/main"
                if args[0] == "rev-parse" and str(args[-1]).startswith("refs/remotes/origin/"):
                    return 0, sha
                if args[0] == "ls-remote" and "--get-url" in args:
                    return 0, args[-1]
                if args[0] == "ls-remote":
                    remotes.append(args[2])
                    if args[2] == clean:
                        return 0, f"{sha}\trefs/heads/main"
                    return 1, ""
                if args[0] == "merge-base":
                    return 0, ""
                return 1, ""

            def wrapped(slug, path, cwd=None, timeout=5):
                slugs.append(slug)
                return api(slug, path, cwd=cwd, timeout=timeout)

            with patch.object(doctor, "_find_repo_root", return_value=self.test_root), \
                 patch.object(doctor, "get_head_sha", return_value=(sha, None)), \
                 patch.object(doctor, "get_dirty_status", return_value=(False, None)), \
                 patch.object(doctor, "get_hook_mode", return_value="full"), \
                 patch.object(doctor, "_run_git", side_effect=fake), \
                 patch.object(doctor, "_api_get", side_effect=wrapped), \
                 patch.object(doctor, "get_skips_status", return_value={
                     "status": "match", "reason": None, "approved_count": 0,
                     "observed_count": 0, "unexpected": [], "missing": [],
                 }), \
                 patch.object(doctor, "get_desktop_evidence", return_value=(False, "NOT_INSTALLED")):
                out = doctor.cmd_doctor_ci(ws_path=self.test_root, offline=False)
            return out, slugs, remotes

        good, good_slugs, good_remotes = run(clean)
        self.assertEqual(good_remotes, [clean])
        self.assertEqual(set(good_slugs), {"owner/a"})
        self.assertEqual(good["ci"]["hosted_checks"]["status"], "success")
        self.assertEqual(good["status"], "INCOMPLETE")
        self.assertEqual(good["ci"]["artifact_provenance_reason"], "RETRIEVAL_UNAVAILABLE")

        for padded in (" " + clean, clean + " "):
            bad, bad_slugs, bad_remotes = run(padded)
            self.assertEqual(bad_slugs, [])
            self.assertEqual(bad_remotes, [])
            self.assertNotEqual(bad["ci"]["hosted_checks"]["status"], "success")
            self.assertNotEqual(bad["status"], "SUCCEEDED")

    def test_repeated_padded_component_is_not_that_checkout(self):
        """A later padded copy must not hide a collapsed leaf."""
        if os.name == "nt":
            original = Path("C:/work/repo /repo ")
            resolved = Path("C:/work/repo /repo")
            self.assertFalse(doctor._root_collapsed(Path("C:/Work/Repo"), Path("C:/work/repo")))
        else:
            original = Path("/work/repo /repo ")
            resolved = Path("/work/repo /repo")
        self.assertTrue(doctor._root_collapsed(original, resolved))
        self.assertFalse(doctor._root_collapsed(resolved, resolved))

    def test_slash_scp_url_is_not_hosted_success(self):
        """git@github.com/owner/repo is a local path, not that GitHub repository."""
        sha = "c" * 40
        slash = "git@github.com/owner/a"
        colon = "git@github.com:owner/a.git"
        with patch.object(doctor, "_run_git", return_value=(0, slash)):
            self.assertEqual(doctor.get_origin(self.test_root), (None, None))
        with patch.object(doctor, "_run_git", return_value=(0, colon)):
            self.assertEqual(doctor.get_origin(self.test_root), ("owner/a", colon))

        def api(slug, path, cwd=None, timeout=5):
            if "/protection" in path:
                return 200, {
                    "url": "https://api.github.com/repos/owner/a/branches/main/protection",
                    "required_status_checks": {"strict": True, "contexts": ["build"]},
                    "enforce_admins": {"enabled": True},
                    "required_pull_request_reviews": None,
                }, None
            if path.startswith("rules/"):
                return 200, [], None
            if "check-runs" in path:
                return 200, {
                    "total_count": 1,
                    "check_runs": [{
                        "name": "build", "status": "completed", "conclusion": "success",
                        "head_sha": sha, "app": {"slug": "github-actions", "id": 1},
                    }],
                }, None
            if "statuses" in path:
                return 200, [], None
            return -1, {}, "API_ERROR"

        def run(url):
            slugs, remotes = [], []

            def fake(args, cwd=None, timeout=5):
                if args[:3] == ["remote", "get-url", "origin"]:
                    return 0, url
                if args[0] == "symbolic-ref":
                    return 0, "refs/heads/main"
                if args[0] == "rev-parse" and str(args[-1]).startswith("refs/remotes/origin/"):
                    return 0, sha
                if args[0] == "ls-remote" and "--get-url" in args:
                    return 0, args[-1]
                if args[0] == "ls-remote":
                    remotes.append(args[2])
                    return 0, sha + "\trefs/heads/main"
                if args[0] == "merge-base":
                    return 0, ""
                return 1, ""

            def wrapped(slug, path, cwd=None, timeout=5):
                slugs.append(slug)
                return api(slug, path, cwd=cwd, timeout=timeout)

            with patch.object(doctor, "_find_repo_root", return_value=self.test_root), \
                 patch.object(doctor, "get_head_sha", return_value=(sha, None)), \
                 patch.object(doctor, "get_dirty_status", return_value=(False, None)), \
                 patch.object(doctor, "get_hook_mode", return_value="full"), \
                 patch.object(doctor, "_run_git", side_effect=fake), \
                 patch.object(doctor, "_api_get", side_effect=wrapped), \
                 patch.object(doctor, "get_skips_status", return_value={
                     "status": "match", "reason": None, "approved_count": 0,
                     "observed_count": 0, "unexpected": [], "missing": [],
                 }), \
                 patch.object(doctor, "get_desktop_evidence", return_value=(False, "NOT_INSTALLED")):
                out = doctor.cmd_doctor_ci(ws_path=self.test_root, offline=False)
            return out, slugs, remotes

        bad, bad_slugs, bad_remotes = run(slash)
        self.assertEqual(bad_slugs, [])
        self.assertEqual(bad_remotes, [])
        self.assertNotEqual(bad["ci"]["hosted_checks"]["status"], "success")
        self.assertNotEqual(bad["status"], "SUCCEEDED")
        good, good_slugs, good_remotes = run(colon)
        self.assertEqual(good_remotes, [colon])
        self.assertEqual(set(good_slugs), {"owner/a"})
        self.assertEqual(good["ci"]["hosted_checks"]["status"], "success")
        self.assertEqual(good["status"], "INCOMPLETE")
        self.assertEqual(good["ci"]["artifact_provenance_reason"], "RETRIEVAL_UNAVAILABLE")

    def test_replace_ref_does_not_invent_ancestry(self):
        """A graft must not make a foreign commit a member of the protected branch."""
        repo = self.test_root / "repl"
        repo.mkdir()

        def git(*args):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

        git("init", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (repo / "f.txt").write_text("a\n", encoding="utf-8")
        git("add", "f.txt")
        git("commit", "-m", "A")
        (repo / "f.txt").write_text("b\n", encoding="utf-8")
        git("add", "f.txt")
        git("commit", "-m", "B")
        tip = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        git("checkout", "-q", "--orphan", "side")
        git("rm", "-q", "-rf", ".")
        (repo / "g.txt").write_text("s\n", encoding="utf-8")
        git("add", "g.txt")
        git("commit", "-m", "S")
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        git("update-ref", "refs/remotes/origin/main", tip)
        git("replace", "--graft", tip, sha)
        plain = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, tip],
            capture_output=True,
        )
        self.assertEqual(plain.returncode, 0)
        out = doctor.cmd_doctor_ci(ws_path=repo, offline=True, branch_override="main")
        self.assertEqual(out["ci"]["sha"], sha)
        self.assertEqual(out["ci"]["branch_protection"]["reason"], "BRANCH_MISMATCH")
        self.assertNotEqual(out["status"], "SUCCEEDED")

    def test_info_grafts_do_not_invent_ancestry(self):
        """A legacy grafts file must not make a foreign commit a branch member."""
        repo = self.test_root / "graftfile"
        repo.mkdir()

        def git(*args):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

        git("init", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (repo / "f.txt").write_text("a\n", encoding="utf-8")
        git("add", "f.txt")
        git("commit", "-m", "A")
        (repo / "f.txt").write_text("b\n", encoding="utf-8")
        git("add", "f.txt")
        git("commit", "-m", "B")
        tip = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        git("checkout", "-q", "--orphan", "side")
        git("rm", "-q", "-rf", ".")
        (repo / "g.txt").write_text("s\n", encoding="utf-8")
        git("add", "g.txt")
        git("commit", "-m", "S")
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        git("update-ref", "refs/remotes/origin/main", tip)
        (repo / ".git" / "info" / "grafts").write_text(tip + " " + sha + "\n", encoding="utf-8")
        plain = subprocess.run(
            ["git", "-C", str(repo), "--no-replace-objects", "merge-base", "--is-ancestor", sha, tip],
            capture_output=True,
        )
        self.assertEqual(plain.returncode, 0)
        out = doctor.cmd_doctor_ci(ws_path=repo, offline=True, branch_override="main")
        self.assertEqual(out["ci"]["sha"], sha)
        self.assertEqual(out["ci"]["branch_protection"]["reason"], "BRANCH_MISMATCH")
        self.assertNotEqual(out["status"], "SUCCEEDED")

    def test_padded_skip_output_is_not_the_neighbor(self):
        """PBZ_SKIP_OUTPUT keeps its spacing, so a neighbor directory is not that evidence."""
        plat = "windows" if sys.platform == "win32" else "posix"
        skill = self.test_root / "skipskill"
        (skill / "tests").mkdir(parents=True)
        (skill / "tests" / "offline_skips.json").write_text(
            json.dumps({plat: {"mcp": []}}),
            encoding="utf-8",
        )
        sha = "ab" * 20
        plain = self.test_root / "observed"
        spaced = self.test_root / " observed"
        plain.mkdir()
        spaced.mkdir()
        passed = json.dumps({"sha": sha, "platform": plat, "exit_code": 0, "status": "passed"})
        failed = json.dumps({"sha": sha, "platform": plat, "exit_code": 1, "status": "failed"})
        (plain / "provenance.json").write_text(passed, encoding="utf-8")
        (plain / "mcp.skips.jsonl").write_text("", encoding="utf-8")
        (spaced / "provenance.json").write_text(failed, encoding="utf-8")
        (spaced / "mcp.skips.jsonl").write_text("", encoding="utf-8")
        with patch.dict(os.environ, {"PBZ_SKIP_OUTPUT": str(spaced)}):
            bad = doctor.get_skips_status(skill_dir=skill, expected_sha=sha, require_provenance=True)
        self.assertEqual(bad["reason"], "SUITE_FAILED")
        with patch.dict(os.environ, {"PBZ_SKIP_OUTPUT": str(plain)}):
            good = doctor.get_skips_status(skill_dir=skill, expected_sha=sha, require_provenance=True)
        self.assertEqual(good["status"], "match")

    def test_gh_stdout_rejects_bad_utf8_but_keeps_replacement_char(self):
        """A damaged gh byte is not a check name. A real replacement character still matches."""
        sha = "f" * 40
        bad = (
            b'{"total_count":1,"check_runs":[{"name":"'
            + bytes([0xFF])
            + b'","status":"completed","conclusion":"success","head_sha":"'
            + sha.encode("ascii")
            + b'","app":{"slug":"github-actions"}}]}'
        )
        seen = {}

        def run_bad(cmd, **kwargs):
            seen["text"] = kwargs.get("text")
            seen["errors"] = kwargs.get("errors")
            seen["encoding"] = kwargs.get("encoding")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=bad, stderr=b"")

        with patch.object(doctor.shutil, "which", return_value="gh"), \
             patch.object(doctor.subprocess, "run", side_effect=run_bad), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bad_hc = doctor.get_hosted_checks(
                self.test_root, sha, offline=False, required_contexts=["\ufffd"],
            )
        self.assertIsNone(seen["text"])
        self.assertIsNone(seen["errors"])
        self.assertIsNone(seen["encoding"])
        self.assertEqual(bad_hc["status"], "unknown")
        self.assertEqual(bad_hc["reason"], "API_ERROR")

        name = "\ufffd"
        good_body = json.dumps({
            "total_count": 1,
            "check_runs": [{
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "head_sha": sha,
                "app": {"slug": "github-actions"},
            }],
        }).encode("utf-8")
        statuses = b"[]"

        def run_good(cmd, **kwargs):
            joined = " ".join(str(part) for part in cmd)
            raw = statuses if "/statuses" in joined else good_body
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=raw, stderr=b"")

        with patch.object(doctor.shutil, "which", return_value="gh"), \
             patch.object(doctor.subprocess, "run", side_effect=run_good), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            good_hc = doctor.get_hosted_checks(
                self.test_root, sha, offline=False, required_contexts=[name],
            )
        self.assertEqual(good_hc["status"], "success")
        self.assertIsNone(good_hc["reason"])


    def test_status_row_sha_must_match_captured_commit(self):
        """A status for another commit cannot satisfy the captured commit."""
        sha = "f" * 40
        other = "e" * 40
        payload = json.dumps({
            "total_count": 1,
            "check_runs": [{
                "name": "build", "status": "completed", "conclusion": "success",
                "head_sha": sha, "app": {"slug": "github-actions", "id": 1},
            }],
        })
        mismatched = json.dumps([
            {"context": "build", "state": "success", "sha": other},
            {"context": "build", "state": "failure", "sha": sha},
        ])
        with patch.object(doctor.shutil, "which", return_value="gh"), \
             patch.object(doctor.subprocess, "run", side_effect=_checks_then_statuses(payload, mismatched)), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            bad = doctor.get_hosted_checks(self.test_root, sha, offline=False, required_contexts=["build"])
        self.assertEqual(bad["status"], "unknown")
        self.assertEqual(bad["reason"], "API_ERROR")
        self.assertEqual(doctor.evaluate_ci_outcome({
            "sha": sha, "dirty": False, "hook_mode": "full",
            "hosted_checks": bad,
            "branch_protection": {"status": "protected", "required_contexts": ["build"]},
            "skips": {"status": "match"},
        }), "INCOMPLETE")
        matched = json.dumps([{"context": "build", "state": "success", "sha": sha}])
        with patch.object(doctor.shutil, "which", return_value="gh"), \
             patch.object(doctor.subprocess, "run", side_effect=_checks_then_statuses(payload, matched)), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            good = doctor.get_hosted_checks(self.test_root, sha, offline=False, required_contexts=["build"])
        self.assertEqual(good["status"], "success")
        self.assertIsNone(good["reason"])

    def test_second_insteadof_is_not_membership(self):
        """A second insteadOf rewrite is not the captured origin URL."""
        repo = self.test_root / "insteadof"
        repo.mkdir()

        def git(*args):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

        git("init", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (repo / "f.txt").write_text("a\n", encoding="utf-8")
        git("add", "f.txt")
        git("commit", "-m", "A")
        git("remote", "add", "origin", "git@github.com:acme/repo.git")
        git("config", "url.https://github.com/acme/.insteadOf", "git@github.com:acme/")
        git("config", "url.https://evil.example/.insteadOf", "https://github.com/acme/")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        shown = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        mapped = subprocess.run(
            ["git", "-C", str(repo), "ls-remote", "--get-url", "--end-of-options", shown],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(shown, "https://github.com/acme/repo.git")
        self.assertEqual(mapped, "https://evil.example/repo.git")
        self.assertEqual(doctor.get_origin(repo), ("acme/repo", shown))
        with patch.object(doctor, "get_hosted_checks", return_value={
            "status": "unknown", "reason": "OFFLINE", "checks": [], "checked_sha": None,
        }), patch.object(doctor, "get_branch_protection", side_effect=AssertionError("protection")):
            out = doctor.cmd_doctor_ci(ws_path=repo, offline=False)
        self.assertEqual(out["ci"]["branch_protection"]["reason"], "BRANCH_MISMATCH")
        self.assertNotEqual(out["status"], "SUCCEEDED")

    def _git(self, repo: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    def test_installer_opt_in_is_recognized_and_not_green(self):
        """Real installer plus the public CLI. The opt-in is not unknown and not a green envelope."""
        import importlib.util
        repo = self.test_root / "installed"
        repo.mkdir()
        self._git(repo, "init", "-b", "main")
        self._git(repo, "config", "user.email", "t@example.com")
        self._git(repo, "config", "user.name", "t")
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "-m", "A")
        root = SKILL.parents[1]
        spec = importlib.util.spec_from_file_location("install_git_hooks", root / "scripts" / "install_git_hooks.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        installer.install(str(repo), str(root / "scripts" / "git-hooks"))
        self._git(repo, "config", "pbz.requiredCI", "main-offline-required")
        self.assertEqual(doctor.get_hook_mode(repo), "main-offline-required")
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.run(
            [sys.executable, "-I", str(SKILL / "pbz" / "cli.py"), "doctor", "ci", "--offline"],
            cwd=repo, env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["ci"]["hook_mode"], "main-offline-required")
        self.assertEqual(payload["ci"]["hook_mode_reason"], "LOCAL_OPT_IN_PRE_COMMIT_SKIPS_PRE_PUSH_FULL")
        self.assertNotEqual(payload["status"], "SUCCEEDED")
        self.assertEqual(payload["ci"]["branch_protection"]["status"], "unknown")
        hook = repo / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")
        self.assertEqual(doctor.get_hook_mode(repo), "foreign")
        self._git(repo, "config", "pbz.requiredCI", "not-the-installer")
        hook.write_bytes((root / "scripts" / "git-hooks" / "pre-push").read_bytes())
        if sys.platform != "win32":
            hook.chmod(hook.stat().st_mode | 0o111)
        self.assertEqual(doctor.get_hook_mode(repo), "unknown")
        green = {
            "sha": "a" * 40,
            "dirty": False,
            "hook_mode": "main-offline-required",
            "hosted_checks": {
                "status": "success",
                "checked_sha": "a" * 40,
                "checks": [{"name": "tests", "status": "completed", "conclusion": "success"}],
            },
            "branch_protection": {"status": "protected"},
            "skips": {"status": "match"},
        }
        self.assertEqual(doctor.evaluate_ci_outcome(green), "INCOMPLETE")

    def test_optional_shard_skip_does_not_force_pending(self):
        sha = "a" * 40
        names = ["offline", "vendor-validator", "posix", "serial"]
        runs = [
            {"name": name, "status": "completed", "conclusion": "success", "head_sha": sha, "app": {"slug": "github-actions", "id": 1}}
            for name in names
        ]
        runs.append({"name": "shard", "status": "completed", "conclusion": "skipped", "head_sha": sha, "app": {"slug": "github-actions", "id": 1}})
        runs.append({"name": "rollout-gate", "status": "completed", "conclusion": "skipped", "head_sha": sha, "app": {"slug": "github-actions", "id": 1}})
        runs.append({"name": "live-synth", "status": "completed", "conclusion": "skipped", "head_sha": sha, "app": {"slug": "github-actions", "id": 1}})
        payload = json.dumps({"total_count": len(runs), "check_runs": runs})
        with patch.object(doctor.shutil, "which", return_value="gh"), \
             patch.object(doctor.subprocess, "run", side_effect=_checks_then_statuses(payload)), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            ok = doctor.get_hosted_checks(self.test_root, sha, offline=False, required_contexts=names)
        self.assertEqual(ok["status"], "success")
        self.assertIsNone(ok["reason"])
        pending = json.loads(payload)
        pending["check_runs"][0]["status"] = "in_progress"
        pending["check_runs"][0]["conclusion"] = None
        with patch.object(doctor.shutil, "which", return_value="gh"), \
             patch.object(doctor.subprocess, "run", side_effect=_checks_then_statuses(json.dumps(pending))), \
             patch.object(doctor, "get_repo_slug", return_value="coqui123/pbi-zerodata"):
            blocked = doctor.get_hosted_checks(self.test_root, sha, offline=False, required_contexts=names)
        self.assertEqual(blocked["status"], "pending")

    def _write_serial(self, folder: Path, body: dict, platform: str) -> None:
        doc = dict(body)
        if "identity" not in doc:
            doc["identity"] = {"os": "Windows-10" if platform == "windows" else "Linux-6"}
        folder.mkdir()
        (folder / "serial.json").write_text(json.dumps(doc), encoding="utf-8")

    def _skip_skill(self, manifest: dict) -> Path:
        skill = self.test_root / "skip-skill"
        (skill / "tests").mkdir(parents=True)
        (skill / "tests" / "offline_skips.json").write_text(json.dumps(manifest), encoding="utf-8")
        return skill

    def test_serial_artifact_multiset_matches(self):
        line = 'PBZ_SKIP {"reason": "because", "test": "case_one"}'
        skill = self._skip_skill({"windows": {"suite_a": [line]}, "posix": {}})
        sha = "a" * 40
        body = {
            "schema": 1, "shard": "serial", "sha": sha, "ok": True,
            "scan": {"completed": True, "ok": True},
            "run": {"id": "9", "attempt": "1", "workflow": "tests"},
            "executed": [
                {"id": "suite_a", "exit_code": 0, "error": None, "skip_reasons": [line], "launched": True},
                {"id": "suite_b", "exit_code": 0, "error": None, "skip_reasons": [], "launched": True},
            ],
        }
        folder = self.test_root / "serial-ok"
        self._write_serial(folder, body, "windows")
        sk = doctor.get_skips_status(skill_dir=skill, observed_sidecars_dir=folder, expected_sha=sha, platform="windows")
        self.assertEqual(sk["status"], "match")
        self.assertIsNone(sk["reason"])
        body["executed"][0]["skip_reasons"] = [line, line]
        dup = self.test_root / "serial-dup"
        self._write_serial(dup, body, "windows")
        dup_sk = doctor.get_skips_status(skill_dir=skill, observed_sidecars_dir=dup, expected_sha=sha, platform="windows")
        self.assertEqual(dup_sk["status"], "mismatch")
        self.assertEqual(dup_sk["reason"], "UNEXPECTED_SKIPS")

    def test_serial_artifact_rejects_false_successes(self):
        skill = self._skip_skill({"windows": {}, "posix": {}})
        sha = "b" * 40
        empty = {"schema": 1, "shard": "serial", "sha": sha, "ok": True, "executed": []}
        folder = self.test_root / "serial-empty"
        self._write_serial(folder, empty, "posix")
        sk = doctor.get_skips_status(skill_dir=skill, observed_sidecars_dir=folder, expected_sha=sha, platform="posix")
        self.assertNotEqual(sk["status"], "match")
        self.assertEqual(sk["reason"], "PARSE_ERROR")
        bare = self.test_root / "serial-bare"
        self._write_serial(bare, {"sha": sha}, "posix")
        bare_sk = doctor.get_skips_status(skill_dir=skill, observed_sidecars_dir=bare, expected_sha=sha, platform="posix")
        self.assertEqual(bare_sk["reason"], "PARSE_ERROR")
        stale = self.test_root / "serial-stale"
        self._write_serial(stale, {"schema": 1, "shard": "serial", "sha": "c" * 40, "ok": True, "executed": [{"id": "s", "exit_code": 0, "error": None, "skip_reasons": []}]}, "posix")
        stale_sk = doctor.get_skips_status(skill_dir=skill, observed_sidecars_dir=stale, expected_sha=sha, platform="posix")
        self.assertEqual(stale_sk["reason"], "STALE_RUN")
        zero = self.test_root / "serial-zero"
        self._write_serial(zero, {"schema": 1, "shard": "serial", "sha": sha, "ok": True, "executed": [{"id": "s", "exit_code": 0, "error": None, "skip_reasons": []}]}, "posix")
        zero_sk = doctor.get_skips_status(skill_dir=skill, observed_sidecars_dir=zero, expected_sha=sha, platform="posix")
        self.assertEqual(zero_sk["status"], "unknown")
        self.assertEqual(zero_sk["reason"], "NOT_LAUNCHED")
        bad_ok = self.test_root / "serial-bad-ok"
        self._write_serial(bad_ok, {"schema": 1, "shard": "serial", "sha": sha, "ok": False, "executed": [{"id": "s", "exit_code": 0, "error": None, "skip_reasons": []}]}, "posix")
        self.assertEqual(doctor.get_skips_status(skill_dir=skill, observed_sidecars_dir=bad_ok, expected_sha=sha, platform="posix")["reason"], "SUITE_FAILED")
        bad_code = self.test_root / "serial-bad-code"
        self._write_serial(bad_code, {"schema": 1, "shard": "serial", "sha": sha, "ok": True, "executed": [{"id": "s", "exit_code": 1, "error": None, "skip_reasons": []}]}, "posix")
        self.assertEqual(doctor.get_skips_status(skill_dir=skill, observed_sidecars_dir=bad_code, expected_sha=sha, platform="posix")["reason"], "SUITE_FAILED")
        amb = self.test_root / "serial-amb"
        self._write_serial(amb, {
            "schema": 1, "shard": "serial", "sha": sha, "ok": True,
            "scan": {"completed": True, "ok": True},
            "run": {"id": "9", "attempt": "1", "workflow": "tests"},
            "executed": [{"id": "s", "exit_code": 0, "error": None, "skip_reasons": [], "launched": True}],
        }, "posix")
        (amb / "s.skips.jsonl").write_text('PBZ_SKIP {"reason": "other", "test": "case_x"}\n', encoding="utf-8")
        amb_sk = doctor.get_skips_status(skill_dir=skill, observed_sidecars_dir=amb, expected_sha=sha, platform="posix")
        self.assertEqual(amb_sk["status"], "unknown")
        self.assertEqual(amb_sk["reason"], "AMBIGUOUS_EVIDENCE")
        wrong = self.test_root / "serial-wrong-os"
        self._write_serial(wrong, {"schema": 1, "shard": "serial", "sha": sha, "ok": True, "identity": {"os": "Windows-10"}, "executed": [{"id": "s", "exit_code": 0, "error": None, "skip_reasons": []}]}, "posix")
        self.assertEqual(doctor.get_skips_status(skill_dir=skill, observed_sidecars_dir=wrong, expected_sha=sha, platform="posix")["reason"], "PLATFORM_MISMATCH")
        (skill / "tests" / "offline_suites.json").write_text(json.dumps([{"id": "s"}, {"id": "other"}]), encoding="utf-8")
        missing = self.test_root / "serial-missing-suite"
        self._write_serial(missing, {
            "schema": 1, "shard": "serial", "sha": sha, "ok": True,
            "scan": {"completed": True, "ok": True},
            "run": {"id": "9", "attempt": "1", "workflow": "tests"},
            "executed": [{"id": "s", "exit_code": 0, "error": None, "skip_reasons": [], "launched": True}],
        }, "posix")
        miss_sk = doctor.get_skips_status(skill_dir=skill, observed_sidecars_dir=missing, expected_sha=sha, platform="posix")
        self.assertEqual(miss_sk["status"], "unknown")
        self.assertEqual(miss_sk["reason"], "MISSING_SUITES")
        bound = {
            "schema": 1, "shard": "serial", "sha": sha, "ok": True,
            "scan": {"completed": True, "ok": True},
            "run": {"id": "9", "attempt": "1", "workflow": "tests"},
            "executed": [{"id": "s", "exit_code": 0, "error": None, "skip_reasons": [], "launched": False}],
        }
        launched = self.test_root / "serial-not-launched"
        self._write_serial(launched, bound, "posix")
        self.assertEqual(doctor.get_skips_status(skill_dir=skill, observed_sidecars_dir=launched, expected_sha=sha, platform="posix")["reason"], "NOT_LAUNCHED")
        for scan, folder_name in (
            (None, "serial-scan-missing"),
            ({"completed": False, "ok": True}, "serial-scan-open"),
            ({"completed": True, "ok": False}, "serial-scan-bad"),
        ):
            body = {
                "schema": 1, "shard": "serial", "sha": sha, "ok": True,
                "run": {"id": "9", "attempt": "1", "workflow": "tests"},
                "executed": [{"id": "s", "exit_code": 0, "error": None, "skip_reasons": [], "launched": True}],
            }
            if scan is not None:
                body["scan"] = scan
            folder = self.test_root / folder_name
            self._write_serial(folder, body, "posix")
            self.assertEqual(
                doctor.get_skips_status(skill_dir=skill, observed_sidecars_dir=folder, expected_sha=sha, platform="posix")["reason"],
                "SCAN_FAILED",
            )
        unbound = self.test_root / "serial-unbound"
        self._write_serial(unbound, {
            "schema": 1, "shard": "serial", "sha": sha, "ok": True,
            "scan": {"completed": True, "ok": True},
            "executed": [{"id": "s", "exit_code": 0, "error": None, "skip_reasons": [], "launched": True}],
        }, "posix")
        self.assertEqual(doctor.get_skips_status(skill_dir=skill, observed_sidecars_dir=unbound, expected_sha=sha, platform="posix")["reason"], "RUN_UNBOUND")
        lane = self.test_root / "serial-lane"
        self._write_serial(lane, {
            "schema": 1, "shard": "serial", "sha": sha, "ok": True, "lane": "posix",
            "executed": [{"id": "s", "exit_code": 0, "error": None, "skip_reasons": []}],
        }, "posix")
        self.assertEqual(doctor.get_skips_status(skill_dir=skill, observed_sidecars_dir=lane, expected_sha=sha, platform="posix")["reason"], "LANE_MISMATCH")
        posix_doc = self.test_root / "serial-posix-report"
        self._write_serial(posix_doc, {"schema": 1, "lane": "posix", "os": "Linux-6", "passed": True, "tests": []}, "posix")
        posix_sk = doctor.get_skips_status(skill_dir=skill, observed_sidecars_dir=posix_doc, expected_sha=sha, platform="posix")
        self.assertEqual(posix_sk["status"], "unknown")
        self.assertEqual(posix_sk["reason"], "LANE_MISMATCH")

    def test_desktop_process_is_not_live_evidence(self):
        with patch.object(doctor.desktop, "processes", return_value=[{"pid": 1}]):
            live, reason = doctor.get_desktop_evidence(self.test_root)
            if sys.platform == "win32":
                self.assertIs(live, False)
                self.assertEqual(reason, "NOT_RUN")
            else:
                self.assertIsNone(live)
                self.assertEqual(reason, "UNAVAILABLE")
            self.assertIs(doctor._desktop_process_available(), True)
        log_dir = self.test_root / ".pbz" / "private"
        log_dir.mkdir(parents=True)
        (log_dir / "evidence.log").write_text('{"stage":"LOADED"}\n', encoding="utf-8")
        live_old, _reason = doctor.get_desktop_evidence(self.test_root)
        self.assertIsNot(live_old, True)
        sha = "d" * 40
        (log_dir / "evidence.log").write_text(
            json.dumps({"revision": sha, "case": "legacy-fallback", "result": "PASS"}) + "\n",
            encoding="utf-8",
        )
        unbound, _unbound_reason = doctor.get_desktop_evidence(self.test_root)
        self.assertIsNot(unbound, True)
        live_ok, live_reason = doctor.get_desktop_evidence(self.test_root, expected_sha=sha)
        self.assertIs(live_ok, True)
        self.assertIsNone(live_reason)
        for first, second in (("PASS", "FAIL"), ("FAIL", "PASS")):
            (log_dir / "evidence.log").write_text(
                json.dumps({"revision": sha, "case": "legacy-fallback", "result": first}) + "\n"
                + json.dumps({"revision": sha, "case": "legacy-fallback", "result": second}) + "\n",
                encoding="utf-8",
            )
            conflict, conflict_reason = doctor.get_desktop_evidence(self.test_root, expected_sha=sha)
            self.assertIs(conflict, False)
            self.assertEqual(conflict_reason, "CONFLICTING_EVIDENCE")
        (log_dir / "evidence.log").write_text(
            json.dumps({"revision": sha, "case": "legacy-fallback", "result": "FAILED"}) + "\n",
            encoding="utf-8",
        )
        live_fail, _fail_reason = doctor.get_desktop_evidence(self.test_root, expected_sha=sha)
        self.assertIsNot(live_fail, True)
        (log_dir / "evidence.log").write_text(
            json.dumps({"revision": "e" * 40, "case": "legacy-fallback", "result": "PASS"}) + "\n",
            encoding="utf-8",
        )
        live_stale, _stale_reason = doctor.get_desktop_evidence(self.test_root, expected_sha=sha)
        self.assertIsNot(live_stale, True)

    def test_public_launcher_writes_no_bytecode(self):
        repo = self.test_root / "launcher"
        repo.mkdir()
        self._git(repo, "init", "-b", "main")
        self._git(repo, "config", "user.email", "t@example.com")
        self._git(repo, "config", "user.name", "t")
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "-m", "A")
        before = subprocess.run(["git", "-C", str(repo), "status", "--porcelain=v1", "-uall"], check=True, capture_output=True, text=True).stdout
        before_cfg = subprocess.run(["git", "-C", str(repo), "config", "--local", "--list"], check=True, capture_output=True, text=True).stdout
        for stale in SKILL.rglob("*.pyc"):
            stale.unlink(missing_ok=True)
        before_pyc = {p.relative_to(SKILL).as_posix() for p in SKILL.rglob("*.pyc")}
        cmd = SKILL / "bin" / "pbz.cmd"
        env = os.environ.copy()
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        env.pop("PYTHONPYCACHEPREFIX", None)
        for _ in range(2):
            proc = subprocess.run(["cmd", "/c", str(cmd), "doctor", "ci", "--offline"], cwd=repo, capture_output=True, text=True, timeout=60, env=env)
            self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        after = subprocess.run(["git", "-C", str(repo), "status", "--porcelain=v1", "-uall"], check=True, capture_output=True, text=True).stdout
        after_cfg = subprocess.run(["git", "-C", str(repo), "config", "--local", "--list"], check=True, capture_output=True, text=True).stdout
        after_pyc = {p.relative_to(SKILL).as_posix() for p in SKILL.rglob("*.pyc")}
        self.assertEqual(after, before)
        self.assertEqual(after_cfg, before_cfg)
        self.assertEqual(after_pyc, before_pyc)
        self.assertEqual(list(repo.rglob("*.pyc")), [])

    def test_labeled_replay_can_succeed_and_local_file_cannot(self):
        # artifact_provenance authenticated is a labeled replay, not hosted evidence.
        base = {
            "sha": "a" * 40,
            "dirty": False,
            "hook_mode": "full",
            "hosted_checks": {
                "checked_sha": "a" * 40,
                "status": "success",
                "checks": [{"name": "tests", "status": "completed", "conclusion": "success"}],
            },
            "branch_protection": {"status": "protected"},
            "skips": {"status": "match"},
        }
        self.assertEqual(doctor.evaluate_ci_outcome(base), "INCOMPLETE")
        replay = dict(base)
        replay["artifact_provenance"] = _REPLAY_PROVENANCE
        self.assertEqual(doctor.evaluate_ci_outcome(replay), "SUCCEEDED")
        local = dict(replay)
        local["artifact_provenance"] = "unavailable"
        self.assertEqual(doctor.evaluate_ci_outcome(local), "INCOMPLETE")

    def test_posix_exemption_is_not_serial_coverage(self):
        ids = json.loads((SKILL / "tests" / "posix_tests.json").read_text(encoding="utf-8"))["tests"]
        rows = [{"id": item, "outcome": "passed"} for item in ids]
        doc = {"schema": 1, "lane": "posix", "os": "Linux-6", "passed": True, "tests": rows}
        got = doctor.posix_exemption_status(doc, ids)
        self.assertEqual(got["status"], "verified")
        self.assertIsNone(got["reason"])
        self.assertEqual(got["lane"], "posix")
        duplicate = dict(doc)
        duplicate["tests"] = rows + [rows[0]]
        self.assertEqual(doctor.posix_exemption_status(duplicate, ids)["reason"], "MISSING_SUITES")
        short = dict(doc)
        short["tests"] = rows[:-1]
        self.assertEqual(doctor.posix_exemption_status(short, ids)["reason"], "MISSING_SUITES")
        failed = dict(doc)
        failed["tests"] = [{**rows[0], "outcome": "failed"}, *rows[1:]]
        self.assertEqual(doctor.posix_exemption_status(failed, ids)["status"], "mismatch")
        self.assertEqual(doctor.posix_exemption_status(failed, ids)["reason"], "SUITE_FAILED")
        flag = dict(doc)
        flag["passed"] = False
        self.assertEqual(doctor.posix_exemption_status(flag, ids)["reason"], "SUITE_FAILED")
        wrong = dict(doc)
        wrong["lane"] = "serial"
        parsed = doctor.posix_exemption_status(wrong, ids)
        self.assertEqual(parsed["reason"], "PARSE_ERROR")
        self.assertIsNone(parsed["lane"])
        ci = {
            "sha": "a" * 40,
            "dirty": False,
            "hook_mode": "full",
            "hosted_checks": {
                "checked_sha": "a" * 40,
                "status": "success",
                "checks": [{"name": "tests", "status": "completed", "conclusion": "success"}],
            },
            "branch_protection": {"status": "protected"},
            "skips": {"status": "unknown", "reason": "NOT_RUN"},
            "artifact_provenance": _REPLAY_PROVENANCE,
            "posix_lane": got,
        }
        self.assertEqual(doctor.evaluate_ci_outcome(ci), "INCOMPLETE")

    def test_official_pre_push_bytes_are_trusted(self):
        raw = (SKILL.parents[1] / "scripts" / "git-hooks" / "pre-push").read_bytes()
        lf = raw.replace(b"\r\n", b"\n")
        self.assertEqual(doctor._check_hook_content(raw), "direct")
        self.assertEqual(doctor._check_hook_content(lf), "direct")
        self.assertIsNone(doctor._check_hook_content(lf.replace(b"set -e", b"set +e", 1)))
        hook = self.test_root / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_bytes(raw)
        with patch.object(doctor.sys, "platform", "linux"), \
             patch.object(doctor.os, "access", return_value=False), \
             patch.object(doctor, "_run_git", return_value=(0, ".git/hooks/pre-push")):
            self.assertEqual(doctor.get_hook_mode(self.test_root), "foreign")
        with patch.object(doctor.sys, "platform", "linux"), \
             patch.object(doctor.os, "access", return_value=True), \
             patch.object(doctor, "_run_git", side_effect=[(0, ".git/hooks/pre-push"), (1, "")]):
            self.assertEqual(doctor.get_hook_mode(self.test_root), "full")


if __name__ == "__main__":
    unittest.main()
