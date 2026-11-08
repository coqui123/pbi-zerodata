"""Saved synth comparisons stay honest, and a private recovery leak stays closed.

Run: python tests/test_synthtest_honesty.py
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
from pbz import cli, common, mcp, mirror, synth, synthtest  # noqa: E402

SECRET = "SECRET_EXC_TEXT_9f3a"
PNG = b"\x89PNG-BYTES-NOT-A-PATH"


def _query_ok(ws, project, dax, max_rows=500):
    return {"columns": ["v"], "rows": [[1]], "truncated": False}


class SynthtestHonestyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["PBZ_SYNTH_ROOT"] = os.path.join(self.tmp.name, "synth")
        root = os.path.join(self.tmp.name, "ws")
        test_parity.build(root)
        self.ws = common.Workspace(root)
        self.ws.private = os.path.join(self.tmp.name, "private")
        os.makedirs(self.ws.private)
        self.project = self.ws.project()
        mirror.pull(self.ws, self.project)
        self.calls = {"query": 0, "info": 0}

    def _save(self, name, doc):
        path = synthtest._path(self.ws, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)

    def _mark(self, built):
        root = synth.synth_project(self.ws, self.project).ws.root
        os.makedirs(os.path.join(root, ".pbz"), exist_ok=True)
        with open(os.path.join(root, ".pbz", "policy.json"), "w", encoding="utf-8") as f:
            json.dump({"synthetic": True}, f)
        with open(os.path.join(root, ".pbz", "synth.json"), "w", encoding="utf-8") as f:
            json.dump({"source_project": "Parity", "built": built, "seed": 1, "rows": 20,
                       "tables": {}, "profile": "standard"}, f)

    def _counted_query(self, ws, project, dax, max_rows=500):
        self.calls["query"] += 1
        return _query_ok(ws, project, dax, max_rows)

    def test_truncation_empty_and_malformed_are_never_same(self):
        gold = {"id": "q", "dax": "EVALUATE ROW()", "columns": ["v"], "rows": [[1]]}
        same = {"columns": ["v"], "rows": [[1]], "truncated": False}
        self.assertEqual(synthtest.compare(dict(gold, truncated=True), same)["result"], "NOT_COMPARABLE")
        self.assertEqual(synthtest.compare(gold, dict(same, truncated=True))["result"], "NOT_COMPARABLE")
        self.assertNotEqual(synthtest.compare(dict(gold, truncated=True), dict(same, truncated=True))["result"], "SAME")

        def info(ws, project):
            self.calls["info"] += 1
            raise AssertionError("build_info")

        self._save("empty", {"queries": [], "synth": {"seed": 1, "rows": 20}})
        self._save("bad", {"queries": [{"id": "q"}]})
        with patch("pbz.synthtest.QUERY", self._counted_query), patch("pbz.synthtest.synth.build_info", info):
            empty = synthtest.run(self.ws, self.project, "empty")
            bad = synthtest.run(self.ws, self.project, "bad")
        self.assertEqual(empty["result"], "NOT_COMPARABLE")
        self.assertNotEqual(empty["result"], "SAME")
        self.assertEqual(bad["result"], "NOT_COMPARABLE")
        self.assertEqual(bad["queries"], [])
        self.assertEqual(self.calls, {"query": 0, "info": 0})

    def test_invalid_and_stale_builds_are_not_comparable(self):
        doc = {"name": "q", "model_hash": synthtest.model_hash(self.project),
               "synth": {"seed": 1, "rows": 20, "profile": "standard"},
               "queries": [{"id": "q", "dax": "EVALUATE ROW()", "columns": ["v"], "rows": [[1]]}]}
        self._save("when", doc)
        self._mark("not-a-timestamp")
        with patch("pbz.synthtest.QUERY", self._counted_query):
            bad = synthtest.run(self.ws, self.project, "when")
        self.assertEqual(bad["result"], "NOT_COMPARABLE")
        self.assertNotEqual(bad["result"], "SAME")
        old = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
        self._mark(old)
        for path in synthtest._model_files(self.project):
            os.utime(path, None)
        with patch("pbz.synthtest.QUERY", self._counted_query):
            stale = synthtest.run(self.ws, self.project, "when")
        self.assertEqual(stale["result"], "NOT_COMPARABLE")
        self.assertIn("synth_stale", stale.get("drift", {}))
        self.assertEqual(self.calls["query"], 0)

    def test_identical_errors_are_not_same(self):
        compared = synthtest.compare({"error": "SYNTAX"}, {"error": "SYNTAX"})
        self.assertNotEqual(compared["result"], "SAME")
        self.assertTrue(str(compared["result"]).startswith("ERROR"))

    def test_private_recovery_cli_and_mcp_redact(self):
        reg = os.path.join(self.ws.pbz_dir, "consumers.json")
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        with open(reg, "w", encoding="utf-8") as f:
            f.write('{"version": 1, "consumers": []}\n')
        argv = ["--workspace", self.ws.root, "--project", "Parity", "consumers", "add",
                "--label", "notes", "--object", "[Total Sales]"]

        def bad_restore(path, data, *, root=None, **_kw):
            raise OSError(SECRET)

        def boom(path, text, bom=b"", *, root=None, **_kw):
            raise OSError(SECRET)

        with patch("pbz.common.write_text", boom), patch("pbz.common.replace_bytes", bad_restore):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(argv)
            text = buf.getvalue()

            def fake_run(cmd, **kwargs):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(cmd[3:])
                class Proc:
                    returncode = code
                    stdout = out.getvalue().encode("utf-8")
                    stderr = (SECRET + self.ws.private + PNG.decode("latin1")).encode("latin1", "replace")
                return Proc()

            with patch("pbz.mcp.subprocess.run", fake_run):
                mcp_out = mcp.call_tool("consumers", {"action": "add", "objects": ["[Total Sales]"],
                                                      "label": "notes", "workspace": self.ws.root,
                                                      "project": "Parity"}, None, None)
        blob = text + json.dumps(mcp_out)
        self.assertNotEqual(rc, 0)
        self.assertTrue(mcp_out["isError"])
        self.assertIn("RECOVERY_REQUIRED", text)
        self.assertIn("RECOVERY_REQUIRED", blob)
        for banned in (SECRET, self.ws.private, self.ws.root, "OSError", ".pbz-restore", PNG.decode("latin1")):
            self.assertNotIn(banned, blob)


if __name__ == "__main__":
    unittest.main()
