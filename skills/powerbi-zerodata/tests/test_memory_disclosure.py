"""Strict disclosure strips bands, including a shape-mode cache.
Run: python tests/test_memory_disclosure.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import cli, common, mcp, memory, mirror, policy  # noqa: E402

SENTINEL = "SECRET_ENGINE_SENTINEL_9f3a"


def _bands(blob):
    return "size_band" in blob or "share_band" in blob


class MemoryDisclosureTests(unittest.TestCase):
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
        pol = policy.load(self.ws)
        pol["disclosure"] = "strict"
        policy.save(self.ws, pol)

    def _engine(self, verb, payload, timeout=30):
        return {"ok": True, "model": {"size_band": "<1 MiB"},
                "tables": [{"name": "Sales", "size_band": "1-10 MiB", "share_band": ">=20%",
                            "columns": [{"name": "Date", "size_band": "<1 MiB", "share_band": "<1%",
                                         "time_part": True}],
                            "hierarchies": []}],
                "relationships": [], "coverage": "FULL", "predicates": "DONE"}

    def test_strict_live_and_cached_shape_drop_bands(self):
        with patch("pbz.memory.engine_call", self._engine), patch("pbz.memory.desktop.require_port", lambda p: 1), \
                patch("pbz.memory.checks.spend", lambda *a, **k: True):
            live = memory.run(self.ws, self.project)
        blob = json.dumps(live)
        self.assertFalse(_bands(blob))
        self.assertTrue(live["tables"][0]["columns"][0]["time_part"])
        cached = {"verb": "memory", "project": self.project.name, "disclosure": "shape",
                  "saved_at": "2026-01-01T00:00:00", "mirror_rev": "m-old",
                  "model": {"size_band": "<1 MiB"},
                  "tables": [{"name": "Sales", "size_band": "1-10 MiB", "share_band": "<1%",
                              "columns": [{"name": "Date", "time_part": True, "size_band": "<1 MiB",
                                           "share_band": "1-5%"}],
                              "hierarchies": [{"name": "Cal", "size_band": "<1 MiB"}]}],
                  "relationships": [{"name": "r", "size_band": "<1 MiB"}],
                  "coverage": "FULL", "predicates": "DONE"}
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        with open(os.path.join(self.ws.pbz_dir, "memory.json"), "w", encoding="utf-8") as f:
            json.dump(cached, f)
        loaded = memory.load(self.ws, self.project)
        text = json.dumps(loaded)
        self.assertFalse(_bands(text))
        self.assertEqual(loaded["disclosure"], "strict")
        self.assertTrue(loaded["tables"][0]["columns"][0]["time_part"])
        buf = io.StringIO()
        with patch("pbz.memory.engine_call", self._engine), patch("pbz.memory.desktop.require_port", lambda p: 1), \
                patch("pbz.memory.checks.spend", lambda *a, **k: True), redirect_stdout(buf):
            cli.main(["--workspace", self.ws.root, "--project", "Parity", "audit", "memory"])

            def fake_run(argv, **kwargs):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(argv[3:])

                class Proc:
                    returncode = code
                    stdout = out.getvalue().encode("utf-8")
                    stderr = b""
                return Proc()

            with patch("pbz.mcp.subprocess.run", fake_run):
                mcp_out = mcp.call_tool("audit", {"action": "memory", "workspace": self.ws.root,
                                                  "project": "Parity"}, None, None)
        combined = buf.getvalue() + mcp_out["content"][0]["text"]
        self.assertFalse(_bands(combined))
        self.assertIn("time_part", combined)

    def test_unexpected_engine_class_is_closed(self):
        def bad(verb, payload, timeout=30):
            return {"ok": False, "class": SENTINEL, "detail": SENTINEL}

        buf = io.StringIO()
        with patch("pbz.memory.engine_call", bad), patch("pbz.memory.desktop.require_port", lambda p: 1), \
                patch("pbz.memory.checks.spend", lambda *a, **k: True), redirect_stdout(buf):
            rc = cli.main(["--workspace", self.ws.root, "--project", "Parity", "audit", "memory"])

            def fake_run(argv, **kwargs):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(argv[3:])

                class Proc:
                    returncode = code
                    stdout = out.getvalue().encode("utf-8")
                    stderr = b""
                return Proc()

            with patch("pbz.mcp.subprocess.run", fake_run):
                mcp_out = mcp.call_tool("audit", {"action": "memory", "workspace": self.ws.root,
                                                  "project": "Parity"}, None, None)
        blob = buf.getvalue() + mcp_out["content"][0]["text"]
        self.assertNotEqual(rc, 0)
        self.assertTrue(mcp_out["isError"])
        self.assertIn("ENGINE_UNAVAILABLE", blob)
        self.assertNotIn(SENTINEL, blob)


if __name__ == "__main__":
    unittest.main()
