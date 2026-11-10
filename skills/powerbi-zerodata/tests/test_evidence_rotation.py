"""Evidence log rotation, HMAC chain, and the 30-day segment prune.

Run: python tests/test_evidence_rotation.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz.common import PbzError, Project, Workspace  # noqa: E402
from pbz.checks import LEDGER_DAYS  # noqa: E402
from pbz import evidence  # noqa: E402


def _entry(ts: str, n: int) -> dict:
    return {"ts": ts, "project": "Canary", "verb": "validate", "mirror_rev": "m" + "a" * 12,
            "real_rev": "r" + "b" * 12, "stage": "PARSED", "scope": "validate",
            "summary": {"errors": 0, "warnings": n, "outcome": "PASS"}}


class EvidenceRotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["PBZ_PRIVATE_ROOT"] = os.path.join(self.tmp.name, "private")
        self.addCleanup(lambda: os.environ.pop("PBZ_PRIVATE_ROOT", None))
        self.root = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.root)
        open(os.path.join(self.root, "Canary.pbip"), "w", encoding="utf-8").close()
        self.ws = Workspace(self.root)
        self.project = Project(self.ws, "Canary")

    def _cap(self, n):
        return patch.object(evidence, "LOG_CAP", n)

    def test_rotation_keeps_the_chain_and_legacy_lines(self):
        with self._cap(700):
            day = (dt.date.today() - dt.timedelta(days=1)).isoformat()
            evidence._append_lines(self.ws, [_entry(day + "T00:00:01", 1)])
            path = os.path.join(self.ws.pbz_dir, "evidence.jsonl")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write('{"legacy": true}\n')
            evidence._append_lines(self.ws, [_entry(day + "T00:00:02", 2)])
            # Fill until the next record cannot fit, which rotates.
            while os.path.getsize(path) + 400 <= evidence.LOG_CAP:
                evidence._append_lines(self.ws, [_entry(day + "T00:00:03", 3)])
            evidence._append_lines(self.ws, [_entry(day + "T00:00:04", 4)])
            self.assertLessEqual(len(os.listdir(os.path.join(self.ws.pbz_dir, "evidence.d"))), 4)
            segment = os.path.join(self.ws.pbz_dir, "evidence.d", "0001.jsonl")
            self.assertTrue(os.path.isfile(segment))
            self.assertLess(os.path.getsize(path), evidence.LOG_CAP)
            self.assertIn(b"legacy", open(segment, "rb").read())
            checked = evidence.verify_chain(self.ws)
            self.assertEqual(checked["chain"], "OK")
            self.assertGreaterEqual(checked["records"], 2)
            # A chained line must not follow a forged previous MAC.
            lines = open(path, encoding="utf-8").read().splitlines()
            forged = json.loads(lines[-1])
            forged["chain_prev"] = "f" * 64
            lines[-1] = json.dumps(forged)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            self.assertEqual(evidence.verify_chain(self.ws)["chain"], "BROKEN")

    def test_over_cap_does_not_rotate(self):
        with self._cap(200):
            os.makedirs(self.ws.pbz_dir, exist_ok=True)
            path = os.path.join(self.ws.pbz_dir, "evidence.jsonl")
            with open(path, "wb") as handle:
                handle.write(b"x" * 201)
            before = open(path, "rb").read()
            with self.assertRaises(PbzError) as caught:
                evidence._append_lines(self.ws, [_entry("2026-11-08T00:00:01", 1)])
            self.assertEqual(caught.exception.detail, evidence.OVER_CAP)
            self.assertEqual(open(path, "rb").read(), before)
            self.assertFalse(os.path.lexists(os.path.join(self.ws.pbz_dir, "evidence.d")))

    def test_prune_drops_only_an_old_prefix_and_keeps_the_anchor(self):
        # 2026-01-02 is outside the live 30-day window, so an append dated that day is pruned
        # immediately. Hold the clock on a day when that record is still inside the window long
        # enough to rotate it, and date the segment that must survive as yesterday.
        yesterday = dt.date.today() - dt.timedelta(days=1)
        held = dt.date(2026, 1, 2) + dt.timedelta(days=LEDGER_DAYS - 1)

        class _HeldDate(dt.date):
            @classmethod
            def today(cls):
                return cls(held.year, held.month, held.day)

        def near_cap(path):
            slack = evidence.LOG_CAP - os.path.getsize(path) - 30
            self.assertGreater(slack, 0)
            with open(path, "ab") as handle:
                handle.write(b"x" * slack)

        with self._cap(4000), patch("pbz.evidence.dt.date", _HeldDate):
            evidence._append_lines(self.ws, [_entry("2026-01-02T00:00:01", 1)])
            path = os.path.join(self.ws.pbz_dir, "evidence.jsonl")
            near_cap(path)
            evidence._append_lines(self.ws, [_entry(yesterday.isoformat() + "T00:00:03", 2)])
            old_segment = os.path.join(self.ws.pbz_dir, "evidence.d", "0001.jsonl")
            self.assertTrue(os.path.isfile(old_segment))
            near_cap(path)
            evidence._append_lines(self.ws, [_entry(yesterday.isoformat() + "T00:00:04", 3)])
            keep_segment = os.path.join(self.ws.pbz_dir, "evidence.d", "0002.jsonl")
            self.assertTrue(os.path.isfile(keep_segment))
            with open(old_segment, encoding="utf-8") as handle:
                old_text = handle.read()

        self.assertIn("2026-01-02", old_text)
        pruned = evidence.prune(self.ws)
        self.assertGreaterEqual(pruned["pruned_segments"], 1)
        self.assertFalse(os.path.lexists(old_segment))
        self.assertTrue(os.path.isfile(keep_segment))
        self.assertEqual(evidence._segment_paths(self.ws), [keep_segment])
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(evidence.verify_chain(self.ws)["chain"], "OK")
        with open(os.path.join(self.ws.pbz_dir, "evidence.chain.json"), encoding="utf-8") as handle:
            anchor = json.loads(handle.read())
        with open(keep_segment, encoding="utf-8") as handle:
            kept_text = handle.read()
        kept = next(json.loads(line) for line in kept_text.splitlines() if line.startswith("{"))
        self.assertEqual(kept["chain_prev"], anchor["anchor"])
        self.assertEqual(anchor["anchor"], evidence._last_mac(old_text))
        self.assertIn(yesterday.isoformat(), kept_text)

    def test_cr_is_normalized_before_the_mac(self):
        evidence._append_lines(self.ws, [{"k": "a\r\nb"}])
        body = open(os.path.join(self.ws.pbz_dir, "evidence.jsonl"), "rb").read()
        self.assertNotIn(b"\r", body)
        self.assertEqual(evidence.verify_chain(self.ws)["chain"], "OK")


if __name__ == "__main__":
    unittest.main()
