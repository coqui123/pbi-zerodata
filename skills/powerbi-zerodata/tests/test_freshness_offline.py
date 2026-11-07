"""Offline provenance labels. Engine payloads are mocked; timestamps stay out of closed fields."""
from __future__ import annotations

import datetime as dt
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))

from pbz import freshness  # noqa: E402
from pbz.common import PbzError  # noqa: E402


class FreshnessOfflineTests(unittest.TestCase):
    def setUp(self):
        self.ws = SimpleNamespace(private="synthetic-private")
        self.project = SimpleNamespace(name="Synthetic")
        self.now = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.timezone.utc)

    def capture(self, payload):
        with patch("pbz.freshness.desktop.require_port", return_value=1), \
             patch("pbz.freshness.engine_call", return_value=payload):
            return freshness.capture(self.ws, self.project, now=self.now)

    def test_missing_partitions_are_unknown(self):
        secret = "1999-01-01T00:00:00Z"
        out = self.capture({"ok": True, "partitions": [], "refresh": "FRESH", "captured": secret,
                            "table": "SecretTable"})
        self.assertEqual(out["refresh"], "UNKNOWN")
        self.assertEqual(out["coverage"], "PARTIAL")
        self.assertEqual(out["source_publication"], "UNKNOWN")
        self.assertNotIn(secret, json.dumps(out))
        self.assertNotIn("SecretTable", json.dumps(out))
        missing = self.capture({"ok": True, "partitions": [{"table": "SecretTable", "refreshed": None}]})
        self.assertEqual(missing["refresh"], "UNKNOWN")
        self.assertEqual(missing["coverage"], "PARTIAL")
        self.assertNotIn("SecretTable", json.dumps(missing))

    def test_boundary_timestamp_is_fresh_and_not_echoed(self):
        edge = (self.now - dt.timedelta(hours=24)).isoformat()
        over = (self.now - dt.timedelta(hours=24, seconds=1)).isoformat()
        fresh = self.capture({"ok": True, "partitions": [{"partition": "SecretPart", "refreshed": edge}],
                              "refreshed": edge})
        self.assertEqual(fresh["refresh"], "FRESH")
        self.assertEqual(fresh["coverage"], "COMPLETE")
        self.assertNotIn(edge, json.dumps(fresh))
        self.assertNotIn("SecretPart", json.dumps(fresh))
        stale = self.capture({"ok": True, "partitions": [{"refreshed": over}]})
        self.assertEqual(stale["refresh"], "STALE")
        self.assertEqual(stale["coverage"], "COMPLETE")
        self.assertNotIn(over, json.dumps(stale))

    def test_engine_failure_stays_closed(self):
        secret = "1999-01-01T00:00:00Z"
        with self.assertRaises(PbzError) as caught:
            self.capture({"ok": False, "class": "ENGINE_UNAVAILABLE", "message": secret})
        self.assertEqual(caught.exception.code, "ENGINE_UNAVAILABLE")
        self.assertNotIn(secret, caught.exception.detail)
        self.assertNotIn(secret, str(caught.exception))
        with self.assertRaises(PbzError) as caught:
            self.capture({"ok": False, "class": secret})
        self.assertEqual(caught.exception.code, "PROTOCOL_ERROR")
        self.assertNotIn(secret, caught.exception.detail)

    def test_publication_stays_unknown(self):
        secret = "1999-01-01T00:00:00Z"
        out = self.capture({"ok": True, "refresh": "FRESH", "coverage": "COMPLETE",
                            "source_publication": secret})
        self.assertEqual(out["refresh"], "FRESH")
        self.assertEqual(out["coverage"], "COMPLETE")
        self.assertEqual(out["source_publication"], "UNKNOWN")
        self.assertNotIn(secret, json.dumps(out))
        self.assertEqual(set(out), {"ok", "verb", "refresh", "source_publication", "coverage", "note"})

    def test_malformed_closed_fields_stay_closed(self):
        out = self.capture({"ok": True, "refresh": [], "coverage": "COMPLETE"})
        self.assertEqual(out["refresh"], "UNKNOWN")
        self.assertEqual(out["coverage"], "PARTIAL")
        self.assertEqual(out["source_publication"], "UNKNOWN")
        bad_cov = self.capture({"ok": True, "refresh": "FRESH", "coverage": {"x": 1}})
        self.assertEqual(bad_cov["refresh"], "FRESH")
        self.assertEqual(bad_cov["coverage"], "PARTIAL")


if __name__ == "__main__":
    unittest.main()
