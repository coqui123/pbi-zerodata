"""Offline checks for the closed `pbz capabilities` matrix.

Run: python tests/test_capabilities.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import capabilities, cli, validate  # noqa: E402


class CapabilitiesTests(unittest.TestCase):
    def test_statuses_are_closed_and_native_interactions_unavailable(self):
        out = capabilities.matrix()
        allowed = set(capabilities.STATUSES)
        self.assertEqual(set(out["statuses"]), allowed)
        seen = set()
        for name, rec in out["features"].items():
            self.assertIn(rec["status"], allowed, name)
            seen.add(rec["status"])
            self.assertIn(name, out["buckets"][rec["status"]])
        self.assertTrue(seen <= allowed)
        native = out["features"]["native_interactions"]
        self.assertEqual(native["status"], "unavailable")
        self.assertEqual(native["source"], "interactiontest")
        self.assertIn("native_interactions", out["buckets"]["unavailable"])
        # Metadata edits, static contracts, and native execution stay different claims.
        self.assertEqual(out["features"]["interaction"]["status"], "supported")
        self.assertEqual(out["features"]["interactions"]["status"], "static_only")
        self.assertEqual(out["features"]["interactions"]["source"], "interactiontest")
        self.assertNotEqual(out["features"]["interaction"], out["features"]["interactions"])
        self.assertEqual(out["features"]["smoke"]["status"], "approximate")
        self.assertEqual(out["features"]["preview"]["status"], "approximate")
        self.assertIn(out["features"]["freshness_publication"]["status"], {"unavailable", "unknown"})
        self.assertEqual(out["features"]["doctor"]["status"], "checklist_only")
        self.assertEqual(out["features"]["isolation"]["status"], "checklist_only")

    def test_report_validator_matches_validate_detection(self):
        present = "available" if os.path.exists(validate.REPORT_AUTHOR) else "unavailable"
        self.assertEqual(capabilities.matrix()["features"]["report_validator"]["status"], present)
        with patch("pbz.capabilities.os.path.exists", return_value=False):
            self.assertEqual(capabilities.matrix()["features"]["report_validator"]["status"], "unavailable")
        with patch("pbz.capabilities.os.path.exists", return_value=True):
            self.assertEqual(capabilities.matrix()["features"]["report_validator"]["status"], "available")

    def test_cli_envelope_without_workspace(self):
        with patch("pbz.cli.find_workspace", side_effect=AssertionError("workspace discovery should not run")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["capabilities"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["verb"], "capabilities")
        self.assertEqual(out["outcome"]["status"], "SUCCEEDED")
        self.assertEqual(out["outcome"]["exit_code"], 0)
        self.assertEqual(out["features"]["native_interactions"]["status"], "unavailable")
        for rec in out["features"].values():
            self.assertIn(rec["status"], capabilities.STATUSES)


if __name__ == "__main__":
    unittest.main()
