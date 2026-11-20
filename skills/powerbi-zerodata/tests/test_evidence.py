"""Evidence regression runner: python tests/test_evidence.py."""
import datetime as dt
import json
import os
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from test_evidence_rotation import EvidenceRotationTests
from test_evidence_honesty import EvidenceHonestyTests
from test_followup_graph import EvidenceTipTests
from pbz import evidence
from pbz.common import PbzError


class RecoveryTests(EvidenceTipTests):
    def pending(self, texts, anchor):
        names = []
        for i, text in enumerate(texts, 1):
            name = f"{i:04d}.jsonl"
            names.append(name)
            self._write(os.path.join(self.segs, name), text)
        self._write(self.anchor, json.dumps({"anchor": anchor, "pending": names}))
        return [os.path.join(self.segs, n) for n in names]

    def expired_record(self, name, prev=evidence.GENESIS, ts="2000-01-01T00:00:00"):
        entry = {"fixture": name, "chain_prev": prev}
        if ts is not None:
            entry["ts"] = ts
        entry["chain_mac"] = evidence._mac(self.key, prev, entry)
        self.macs[name] = entry["chain_mac"]
        return json.dumps(entry, ensure_ascii=False) + "\n"

    def test_pending_authentication_precedes_all_deletion(self):
        a = self.expired_record("a")
        b = self.expired_record("b", self._mac("a"))
        paths = self.pending([a, b], self._mac("b"))
        for bad in (b.replace('"fixture": "b"', '"fixture": "forged"'), self.expired_record("fork")):
            self._write(paths[1], bad)
            evidence._recover_prune(self.ws)
            self.assertTrue(all(os.path.exists(p) for p in paths))
        self._write(paths[1], b)
        self._write(self.anchor, json.dumps({"anchor": "f" * 64, "pending": ["0001.jsonl", "0002.jsonl"]}))
        evidence._recover_prune(self.ws)
        self.assertTrue(all(os.path.exists(p) for p in paths))
        self._write(self.anchor, json.dumps({"anchor": self._mac("b"), "pending": ["0001.jsonl", "0002.jsonl"]}))
        with patch.object(evidence.os, "remove", side_effect=PermissionError("fixture")):
            evidence._recover_prune(self.ws)
        self.assertTrue(all(os.path.exists(p) for p in paths))
        self.assertIn("pending", json.loads(Path(self.anchor).read_text()))
        evidence._recover_prune(self.ws)
        self.assertFalse(any(os.path.exists(p) for p in paths))
        self.assertNotIn("pending", json.loads(Path(self.anchor).read_text()))

    def test_authentic_but_ineligible_pending_is_preserved(self):
        for ts in (dt.date.today().isoformat() + "T00:00:00", None):
            with self.subTest(ts=ts):
                path = self.pending([self.expired_record("a", ts=ts)], self._mac("a"))[0]
                before = Path(path).read_bytes(), Path(self.anchor).read_bytes()
                evidence._recover_prune(self.ws)
                self.assertEqual((Path(path).read_bytes(), Path(self.anchor).read_bytes()), before)

    def test_interrupted_prune_completes_with_lf_only_records(self):
        for name in ("expired", "literal\u2028separator"):
            with self.subTest(name=name):
                a = self.expired_record(name)
                b = self.expired_record("b", self._mac(name))
                paths = self.pending([a, b], self._mac("b"))
                evidence._recover_prune(self.ws)
                self.assertFalse(any(os.path.exists(p) for p in paths))
                self.assertEqual(json.loads(Path(self.anchor).read_text()), {"anchor": self._mac("b")})

    def test_interrupted_future_prune_recovers_using_supplied_cutoff(self):
        from pbz.checks import LEDGER_DAYS
        today = dt.date.today()
        future = today + dt.timedelta(days=LEDGER_DAYS + 30)
        path = os.path.join(self.segs, "0001.jsonl")
        self._write(path, self.expired_record("a", ts=today.isoformat() + "T00:00:00"))
        with patch.object(evidence.os, "remove", side_effect=PermissionError("interrupted")):
            with self.assertRaises(PbzError):
                evidence._prune(self.ws, today=future)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(json.loads(Path(self.anchor).read_text())["anchor"], self._mac("a"))
        evidence._recover_prune(self.ws)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(json.loads(Path(self.anchor).read_text()), {"anchor": self._mac("a")})

    def test_interrupted_earlier_prune_preserves_retained_segment(self):
        from pbz.checks import LEDGER_DAYS
        wall_cutoff = dt.date.today() - dt.timedelta(days=LEDGER_DAYS)
        earlier = dt.date.today() - dt.timedelta(days=30)
        cutoff = earlier - dt.timedelta(days=LEDGER_DAYS)
        old = os.path.join(self.segs, "0001.jsonl")
        retained = os.path.join(self.segs, "0002.jsonl")
        self._write(old, self.expired_record("a", ts=(cutoff - dt.timedelta(days=1)).isoformat() + "T00:00:00"))
        self._write(retained, self.expired_record("b", self._mac("a"),
                    ts=(wall_cutoff - dt.timedelta(days=1)).isoformat() + "T00:00:00"))
        before = Path(retained).read_bytes()
        with patch.object(evidence.os, "remove", side_effect=PermissionError("interrupted")):
            with self.assertRaises(PbzError):
                evidence._prune(self.ws, today=earlier)
        pending = json.loads(Path(self.anchor).read_text())
        self.assertEqual(pending["pending"], ["0001.jsonl"])
        evidence._recover_prune(self.ws)
        self.assertFalse(os.path.exists(old))
        self.assertEqual(Path(retained).read_bytes(), before)
        self.assertEqual(json.loads(Path(self.anchor).read_text()), {"anchor": self._mac("a")})
        self.assertEqual(pending.get("cutoff"), cutoff.isoformat())

    def test_recorded_earlier_cutoff_refuses_wall_clock_expired_pending(self):
        from pbz.checks import LEDGER_DAYS
        cutoff = dt.date.today() - dt.timedelta(days=LEDGER_DAYS)
        path = self.pending([self.expired_record("a", ts=(cutoff - dt.timedelta(days=1)).isoformat() + "T00:00:00")],
                            self._mac("a"))[0]
        index = json.loads(Path(self.anchor).read_text())
        index["cutoff"] = (cutoff - dt.timedelta(days=30)).isoformat()
        self._write(self.anchor, json.dumps(index))
        before = Path(path).read_bytes(), Path(self.anchor).read_bytes()
        evidence._recover_prune(self.ws)
        self.assertEqual((Path(path).read_bytes(), Path(self.anchor).read_bytes()), before)

    def test_pending_must_be_oldest_prefix(self):
        a = self.expired_record("a")
        b = self.expired_record("b", self._mac("a"))
        paths = self.pending([a, b], self._mac("b"))
        self._write(self.anchor, json.dumps({"anchor": self._mac("b"), "pending": ["0002.jsonl"]}))
        before = [Path(p).read_bytes() for p in [*paths, self.anchor]]
        evidence._recover_prune(self.ws)
        self.assertEqual([Path(p).read_bytes() for p in [*paths, self.anchor]], before)

    def test_missing_leading_pending_allowed_but_later_gap_refused(self):
        a = self.expired_record("a")
        b = self.expired_record("b", self._mac("a"))
        c = self.expired_record("c", self._mac("b"))
        paths = self.pending([a, b, c], self._mac("c"))
        os.remove(paths[1])
        before = Path(self.anchor).read_bytes()
        evidence._recover_prune(self.ws)
        self.assertTrue(os.path.exists(paths[0]))
        self.assertTrue(os.path.exists(paths[2]))
        self.assertEqual(Path(self.anchor).read_bytes(), before)
        os.remove(paths[0])
        evidence._recover_prune(self.ws)
        self.assertFalse(os.path.exists(paths[2]))
        self.assertNotIn("pending", json.loads(Path(self.anchor).read_text()))

    def test_malformed_index_never_deletes_or_raises_on_verify(self):
        path = self.pending([self._record("a")], self._mac("a"))[0]
        for raw in ('{', '{"anchor": 12}', json.dumps({"anchor": evidence.GENESIS, "pending": {}}),
                    json.dumps({"anchor": evidence.GENESIS, "pending": [{}]}),
                    *(json.dumps({"anchor": evidence.GENESIS, "cutoff": bad})
                      for bad in (None, 20260101, "2026-02-30", "20260101", "2026-1-01"))):
            with self.subTest(raw=raw):
                self._write(self.anchor, raw)
                before = Path(path).read_bytes()
                self.assertEqual(evidence.verify_chain(self.ws)["chain"], "BROKEN")
                self.assertEqual(Path(path).read_bytes(), before)
                with self.assertRaises(PbzError) as caught:
                    evidence._append_lines(self.ws, [{"next": True}])
                self.assertEqual(caught.exception.code, "REFUSED")
                self.assertEqual(Path(path).read_bytes(), before)

    def test_torn_active_tip_refuses_append(self):
        self._write(self.active, self._record("a") + self._record("b", self._mac("a"))[:30])
        before = Path(self.active).read_bytes()
        with self.assertRaises(PbzError) as caught:
            evidence._append_lines(self.ws, [{"next": True}])
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(Path(self.active).read_bytes(), before)
        self.assertEqual(evidence.verify_chain(self.ws)["chain"], "BROKEN")


if __name__ == "__main__":
    unittest.main()
