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
    ({"result": "NOT_COMPARABLE"}, "BLOCKED", "NOT_COMPARABLE", 3),
    ({"result": "ERROR:SYNTAX"}, "FAILED", "ERROR:SYNTAX", 1),
    ({"queries": [{"id": "q", "result": "ERROR:TYPE_MISMATCH"}]}, "FAILED", "ASSERTION_FAILED", 1),
    ({"verb": "smoke", "visuals": [{"status": "OK", "coverage": "PARTIAL", "note": "filter"}],
      "summary": {"executed": {"OK": 1}, "not_executed": {"UNSUPPORTED": 1}}},
     "BLOCKED", "INCONCLUSIVE_EVIDENCE", 3),
    ({"ok": False, "error": "LOCAL_STATE_UNAVAILABLE"}, "BLOCKED", "LOCAL_STATE_UNAVAILABLE", 3),
    ({"ok": False, "error": "INTERNAL_ERROR"}, "FAILED", "INTERNAL_ERROR", 1),
]


def _mcp(cmd):
    def fake_run(argv, **kwargs):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(argv[3:])

        class Proc:
            returncode = code
            stdout = out.getvalue().encode("utf-8")
            stderr = b""
        return Proc()
    return fake_run


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
                    with patch("pbz.mcp.subprocess.run", _mcp(None)):
                        mcp_out = mcp.call_tool("recover", {"workspace": self.ws.root, "project": "Parity"}, None, None)
                self.assertEqual(cli_rc, rc)
                self.assertEqual(body["outcome"]["status"], status)
                self.assertEqual(body["outcome"]["exit_code"], rc)
                self.assertEqual(mcp_out["isError"], rc != 0)
                text = mcp_out["content"][0]["text"]
                self.assertEqual(json.loads(text)["outcome"]["exit_code"], rc)

    def test_diagnostic_stays_exit_zero(self):
        payload = {"verb": "check", "results": [{"id": "c1", "status": "FAIL"}]}
        env, rc = outcomes.envelope(payload, diagnostic=True)
        self.assertEqual(rc, 0)
        self.assertEqual(env["outcome"]["status"], "FINDINGS")
        with patch("pbz.checks.run_checks", lambda *a, **k: payload):
            cli_rc, body = self._cli(["check", "--diagnostic", "{}"])
        self.assertEqual(cli_rc, 0)
        self.assertEqual(body["outcome"]["status"], "FINDINGS")

    def test_recover_unknown_id_is_not_found(self):
        out = mirror.recover(self.ws, self.project, "does-not-exist")
        self.assertEqual(out["result"], "NOT_FOUND")
        self.assertNotEqual(out["result"], "NOTHING_TO_RECOVER")
        env, rc = outcomes.envelope(out)
        self.assertEqual(rc, 0)

    def test_validate_only_appends_no_evidence(self):
        self._cli(["check", "--validate-only", "{}"])
        path = os.path.join(self.ws.pbz_dir, "evidence.jsonl")
        text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        self.assertEqual(text.strip(), "")
        self.assertIsNone(evidence.summarize("check", None, {"verb": "check validate", "spec_valid": True}))

    def test_synth_evidence_is_scoped_by_name(self):
        evidence.record(self.ws, self.project, "synth-test", "run", {"name": "alpha", "result": "ERROR:SYNTAX"})
        evidence.record(self.ws, self.project, "synth-test", "run", {"name": "beta", "result": "SAME"})
        lines = open(os.path.join(self.ws.pbz_dir, "evidence.jsonl"), encoding="utf-8").read().splitlines()
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

        with patch("pbz.synthtest.QUERY", query):
            cli_rc, body = self._cli(["synth-test", "run", "err"])
            with patch("pbz.mcp.subprocess.run", _mcp(None)):
                mcp_out = mcp.call_tool("synth_test", {"action": "run", "name": "err",
                                                       "workspace": self.ws.root, "project": "Parity"}, None, None)
        blob = json.dumps(body) + mcp_out["content"][0]["text"]
        self.assertEqual(cli_rc, 1)
        self.assertTrue(mcp_out["isError"])
        self.assertNotIn('"SAME"', blob)
        self.assertNotIn('"PASS"', blob)
        self.assertIn("ERROR:SYNTAX", blob)
        logged = open(os.path.join(self.ws.pbz_dir, "evidence.jsonl"), encoding="utf-8").read()
        self.assertNotIn('"outcome": "PASS"', logged)


if __name__ == "__main__":
    unittest.main()
