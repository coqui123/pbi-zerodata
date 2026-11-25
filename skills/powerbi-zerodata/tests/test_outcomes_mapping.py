"""Outcome, evidence and recover mapping. Run: python tests/test_outcomes_mapping.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import cli, common, evidence, mcp, mirror, outcomes, synth, synthtest  # noqa: E402

CASES = [
    # SUCCEEDED (rc=0)
    ({"result": "OK"}, "SUCCEEDED", "OK", 0),
    ({"status": "PASS"}, "SUCCEEDED", "PASS", 0),
    ({"action": "pulled"}, "SUCCEEDED", "pulled", 0),
    ({"verb": "snapshot", "queries": [{"id": "q", "result": "SAME"}]}, "SUCCEEDED", "OK", 0),

    # PARTIAL (rc=5)
    ({"result": "PUSHED", "reload": "FAILED"}, "PARTIAL", "APPLIED_RELOAD_FAILED", 5),
    ({"result": "PARTIAL"}, "PARTIAL", "PARTIAL", 5),

    # FAILED (rc=1: assertions, internal errors, engine catch-alls)
    ({"ok": False, "error": "INTERNAL_ERROR"}, "FAILED", "INTERNAL_ERROR", 1),
    ({"result": "OTHER"}, "FAILED", "OTHER", 1),
    ({"result": "ROLLED_BACK"}, "FAILED", "ROLLED_BACK", 1),
    ({"result": "FAILED"}, "FAILED", "FAILED", 1),
    ({"result": "ERROR:SYNTAX"}, "FAILED", "ERROR:SYNTAX", 1),
    ({"queries": [{"id": "q", "result": "ERROR:TYPE_MISMATCH"}]}, "FAILED", "ASSERTION_FAILED", 1),
    ({"status": "PROTOCOL_ERROR"}, "FAILED", "ASSERTION_FAILED", 1),
    ({"gate": "FAIL"}, "FAILED", "GATE_FAILED", 1),
    ({"status": "FAIL"}, "FAILED", "ASSERTION_FAILED", 1),
    ({"result": "DIFF"}, "FAILED", "ASSERTION_FAILED", 1),
    ({"results": [{"status": "FAIL"}]}, "FAILED", "ASSERTION_FAILED", 1),
    ({"verb": "health", "ok": True, "errors": [{"kind": "measure", "class": "SYNTAX"}]},
     "FAILED", "ERROR:SYNTAX", 1),

    # FAILED (rc=2: unhandled/generic error payload)
    # Known deviation / legacy behavior: unhandled error strings fall through to line 71-72
    # which yields status "FAILED" with rc=2; tracked for future normalization.
    ({"ok": False, "error": "UNKNOWN_ERROR"}, "FAILED", "UNKNOWN_ERROR", 2),

    # INVALID (rc=2: bad request, bad arguments, invalid spec, validation failed)
    ({"ok": False, "error": "BAD_REQUEST"}, "INVALID", "BAD_REQUEST", 2),
    ({"result": "BAD_ARGUMENTS"}, "INVALID", "BAD_ARGUMENTS", 2),
    ({"result": "UNKNOWN_TOKEN"}, "INVALID", "UNKNOWN_TOKEN", 2),
    ({"result": "SPEC_INVALID"}, "INVALID", "SPEC_INVALID", 2),
    ({"result": "INVALID"}, "INVALID", "INVALID", 2),
    ({"spec_valid": False}, "INVALID", "SPEC_INVALID", 2),
    ({"findings": [{"severity": "error"}]}, "INVALID", "VALIDATION_FAILED", 2),

    # BLOCKED (rc=3: locks, state unknown, inconclusive evidence, budget)
    # All 20 canonical BLOCKED codes:
    ({"result": "DESKTOP_OPEN"}, "BLOCKED", "DESKTOP_OPEN", 3),
    ({"result": "DESKTOP_DIRTY"}, "BLOCKED", "DESKTOP_DIRTY", 3),
    ({"result": "DESKTOP_STATE_UNKNOWN"}, "BLOCKED", "DESKTOP_STATE_UNKNOWN", 3),
    ({"result": "WAITING_ON_DIALOG"}, "BLOCKED", "WAITING_ON_DIALOG", 3),
    ({"result": "CONFLICT"}, "BLOCKED", "CONFLICT", 3),
    ({"result": "CONFLICT_RISK"}, "BLOCKED", "CONFLICT_RISK", 3),
    ({"result": "PENDING_PQ_CHANGES"}, "BLOCKED", "PENDING_PQ_CHANGES", 3),
    ({"status": "BUDGET"}, "BLOCKED", "BUDGET", 3),
    ({"result": "RECOVERY_REQUIRED"}, "BLOCKED", "RECOVERY_REQUIRED", 3),
    ({"result": "INCOMPARABLE"}, "BLOCKED", "INCOMPARABLE", 3),
    ({"result": "PENDING_PUSH"}, "BLOCKED", "PENDING_PUSH", 3),
    ({"result": "INCOMPLETE"}, "BLOCKED", "INCOMPLETE", 3),
    ({"result": "HUMAN_REVIEW_REQUIRED"}, "BLOCKED", "HUMAN_REVIEW_REQUIRED", 3),
    ({"result": "DEPENDENCY_PENDING"}, "BLOCKED", "DEPENDENCY_PENDING", 3),
    ({"result": "STALE_EVIDENCE"}, "BLOCKED", "STALE_EVIDENCE", 3),
    ({"result": "RECOVERY_CONFLICT"}, "BLOCKED", "RECOVERY_CONFLICT", 3),
    ({"result": "COMMIT_BUSY"}, "BLOCKED", "COMMIT_BUSY", 3),
    ({"result": "AMBIGUOUS_DESKTOP"}, "BLOCKED", "AMBIGUOUS_DESKTOP", 3),
    ({"result": "NOT_COMPARABLE"}, "BLOCKED", "NOT_COMPARABLE", 3),
    ({"ok": False, "error": "LOCAL_STATE_UNAVAILABLE"}, "BLOCKED", "LOCAL_STATE_UNAVAILABLE", 3),
    # Inconclusive evidence rules:
    ({"verb": "smoke", "visuals": [{"status": "OK", "coverage": "PARTIAL", "note": "filter"}],
      "summary": {"executed": {"OK": 1}, "not_executed": {"UNSUPPORTED": 1}}},
     "BLOCKED", "INCONCLUSIVE_EVIDENCE", 3),
    ({"results": [{"status": "EMPTY"}]}, "BLOCKED", "INCONCLUSIVE_EVIDENCE", 3),
    ({"results": [{"result": "STALE_DATA"}]}, "BLOCKED", "INCONCLUSIVE_EVIDENCE", 3),
    ({"verb": "snapshot", "queries": [{"id": "q", "result": "NEW"}]},
     "BLOCKED", "INCONCLUSIVE_EVIDENCE", 3),
    ({"verb": "snapshot", "queries": [{"id": "q", "result": "MISSING"}]},
     "BLOCKED", "INCONCLUSIVE_EVIDENCE", 3),
    ({"verb": "health", "ok": True, "errors": [], "unprocessed_tables": ["Sales"]},
     "BLOCKED", "INCOMPLETE", 3),

    # UNAVAILABLE (rc=4: engine down, timeout, unsupported)
    ({"ok": False, "error": "ENGINE_UNAVAILABLE"}, "UNAVAILABLE", "ENGINE_UNAVAILABLE", 4),
    ({"result": "TIMEOUT"}, "UNAVAILABLE", "TIMEOUT", 4),
    ({"result": "UNSUPPORTED"}, "UNAVAILABLE", "UNSUPPORTED", 4),
    ({"result": "NOT_AVAILABLE"}, "UNAVAILABLE", "NOT_AVAILABLE", 4),
    ({"result": "NOT_READY"}, "UNAVAILABLE", "NOT_READY", 4),
    ({"result": "CLI_UNAVAILABLE"}, "UNAVAILABLE", "CLI_UNAVAILABLE", 4),
]


def _mcp(cmd):
    def fake_bounded(command, cwd=None, env=None, timeout=None, mid=0):
        fake_bounded.calls.append(command)
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(command[3:])
        return code, out.getvalue().encode("utf-8"), None
    fake_bounded.calls = []
    return fake_bounded


class OutcomeMappingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "ws")
        test_parity.build(root)
        self.ws = common.Workspace(root)
        self.ws.private = os.path.join(self.tmp.name, "private")
        os.makedirs(self.ws.private)
        self.project = self.ws.project()
        mirror.pull(self.ws, self.project)

    def _cli(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--workspace", self.ws.root, "--project", "Parity", *argv])
        return rc, json.loads(buf.getvalue())

    def test_envelope_cli_and_mcp_agree(self):
        for payload, status, code, rc in CASES:
            with self.subTest(payload=payload):
                env, got = outcomes.envelope(payload)
                self.assertEqual((env["outcome"]["status"], env["outcome"]["code"], got), (status, code, rc))
                with patch("pbz.mirror.recover", lambda *a, **k: dict(payload)):
                    cli_rc, body = self._cli(["recover"])
                    fake_bounded = _mcp(None)
                    with patch("pbz.mcp._bounded_output", fake_bounded):
                        mcp_out = mcp.call_tool("recover", {"workspace": self.ws.root, "project": "Parity"}, self.ws.root, None)
                self.assertTrue(fake_bounded.calls)
                self.assertEqual(cli_rc, rc)
                self.assertEqual(body["outcome"]["status"], status)
                self.assertEqual(body["outcome"]["code"], code)
                self.assertEqual(body["outcome"]["exit_code"], rc)
                self.assertEqual(mcp_out["isError"], rc != 0)
                text = mcp_out["content"][0]["text"]
                mcp_outcome = json.loads(text)["outcome"]
                self.assertEqual(mcp_outcome["status"], status)
                self.assertEqual(mcp_outcome["code"], code)
                self.assertEqual(mcp_outcome["exit_code"], rc)

    def test_diagnostic_stays_exit_zero(self):
        payload = {"verb": "check", "results": [{"id": "c1", "status": "FAIL"}]}
        env, rc = outcomes.envelope(payload, diagnostic=True)
        self.assertEqual(rc, 0)
        self.assertEqual(env["outcome"]["status"], "FINDINGS")
        self.assertEqual(env["outcome"]["code"], "DIAGNOSTIC_FINDINGS")
        with patch("pbz.checks.run_checks", lambda *a, **k: payload):
            cli_rc, body = self._cli(["check", "--diagnostic", "{}"])
        self.assertEqual(cli_rc, 0)
        self.assertEqual(body["outcome"]["status"], "FINDINGS")
        self.assertEqual(body["outcome"]["code"], "DIAGNOSTIC_FINDINGS")

        # Execution error under diagnostic also produces FINDINGS / DIAGNOSTIC_FINDINGS / 0
        payload_err = {"result": "ERROR:SYNTAX"}
        env_err, rc_err = outcomes.envelope(payload_err, diagnostic=True)
        self.assertEqual(rc_err, 0)
        self.assertEqual(env_err["outcome"]["status"], "FINDINGS")
        self.assertEqual(env_err["outcome"]["code"], "DIAGNOSTIC_FINDINGS")

    def test_all_canonical_category_codes_covered(self):
        """Ensure all canonical BLOCKED, UNAVAILABLE, and INVALID set members map cleanly."""
        for code in outcomes.BLOCKED:
            env, rc = outcomes.envelope({"ok": False, "error": code})
            self.assertEqual(env["outcome"]["status"], "BLOCKED")
            self.assertEqual(env["outcome"]["code"], code)
            self.assertEqual(rc, 3)
            self.assertEqual(env["outcome"]["exit_code"], 3)

        for code in outcomes.UNAVAILABLE:
            env, rc = outcomes.envelope({"ok": False, "error": code})
            self.assertEqual(env["outcome"]["status"], "UNAVAILABLE")
            self.assertEqual(env["outcome"]["code"], code)
            self.assertEqual(rc, 4)
            self.assertEqual(env["outcome"]["exit_code"], 4)

        for code in outcomes.INVALID:
            env, rc = outcomes.envelope({"ok": False, "error": code})
            self.assertEqual(env["outcome"]["status"], "INVALID")
            self.assertEqual(env["outcome"]["code"], code)
            self.assertEqual(rc, 2)
            self.assertEqual(env["outcome"]["exit_code"], 2)

    def test_recover_unknown_id_is_not_found(self):
        out = mirror.recover(self.ws, self.project, "does-not-exist")
        self.assertEqual(out["result"], "NOT_FOUND")
        self.assertNotEqual(out["result"], "NOTHING_TO_RECOVER")
        env, rc = outcomes.envelope(out)
        self.assertEqual(rc, 0)

    def test_validate_only_appends_no_evidence(self):
        self._cli(["check", "--validate-only", "{}"])
        path = os.path.join(self.ws.pbz_dir, "evidence.jsonl")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                text = f.read()
        else:
            text = ""
        self.assertEqual(text.strip(), "")
        self.assertIsNone(evidence.summarize("check", None, {"verb": "check validate", "spec_valid": True}))

    def test_synth_evidence_is_scoped_by_name(self):
        evidence.record(self.ws, self.project, "synth-test", "run", {"name": "alpha", "result": "ERROR:SYNTAX"})
        evidence.record(self.ws, self.project, "synth-test", "run", {"name": "beta", "result": "SAME"})
        with open(os.path.join(self.ws.pbz_dir, "evidence.jsonl"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        scopes = [json.loads(line)["scope"] for line in lines]
        self.assertIn("synth-test:alpha", scopes)
        self.assertIn("synth-test:beta", scopes)
        alpha = json.loads([line for line in lines if "alpha" in line][0])
        self.assertNotEqual(alpha["summary"]["outcome"], "PASS")

    def test_identical_errors_never_same(self):
        gold = {"error": "SYNTAX", "columns": [], "rows": []}
        now = {"error": "SYNTAX", "columns": [], "rows": []}
        compared = synthtest.compare(gold, now)
        self.assertNotEqual(compared["result"], "SAME")
        self.assertTrue(str(compared["result"]).startswith("ERROR"))
        os.environ["PBZ_SYNTH_ROOT"] = os.path.join(self.tmp.name, "synth")
        root = synth.synth_project(self.ws, self.project).ws.root
        os.makedirs(os.path.join(root, ".pbz"), exist_ok=True)
        built = (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
        with open(os.path.join(root, ".pbz", "policy.json"), "w", encoding="utf-8") as f:
            json.dump({"synthetic": True}, f)
        with open(os.path.join(root, ".pbz", "synth.json"), "w", encoding="utf-8") as f:
            json.dump({"source_project": "Parity", "built": built, "seed": 1, "rows": 20,
                       "tables": {}, "profile": "standard"}, f)
        path = synthtest._path(self.ws, "err")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        doc = {"name": "err", "model_hash": synthtest.model_hash(self.project),
               "synth": {"seed": 1, "rows": 20, "profile": "standard", "built": built},
               "queries": [{"id": "q", "dax": "EVALUATE ROW()", "error": "SYNTAX"}]}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)

        def query(*a, **k):
            return {"error": "SYNTAX"}

        with patch("pbz.synthtest.QUERY", query), patch("pbz.cli.find_workspace", return_value=self.ws):
            cli_rc, body = self._cli(["synth-test", "run", "err"])
            fake_bounded = _mcp(None)
            with patch("pbz.mcp._bounded_output", fake_bounded):
                mcp_out = mcp.call_tool("synth_test", {"action": "run", "name": "err",
                                                       "workspace": self.ws.root, "project": "Parity"}, self.ws.root, None)
        self.assertTrue(fake_bounded.calls)
        blob = json.dumps(body) + mcp_out["content"][0]["text"]
        self.assertEqual(cli_rc, 1)
        self.assertTrue(mcp_out["isError"])
        self.assertNotIn('"SAME"', blob)
        self.assertNotIn('"PASS"', blob)
        self.assertIn("ERROR:SYNTAX", blob)
        with open(os.path.join(self.ws.pbz_dir, "evidence.jsonl"), encoding="utf-8") as f:
            logged = f.read()
        self.assertNotIn('"outcome": "PASS"', logged)


if __name__ == "__main__":
    unittest.main()
