"""Evidence regression runner: python tests/test_evidence.py."""
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

    def test_pending_authentication_precedes_all_deletion(self):
        a = self._record("a")
        b = self._record("b", self._mac("a"))
        paths = self.pending([a, b], self._mac("b"))
        for bad in (b.replace('"fixture": "b"', '"fixture": "forged"'), self._record("fork")):
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

    def test_malformed_index_never_deletes_or_raises_on_verify(self):
        path = self.pending([self._record("a")], self._mac("a"))[0]
        for raw in ('{', '{"anchor": 12}', json.dumps({"anchor": evidence.GENESIS, "pending": {}}),
                    json.dumps({"anchor": evidence.GENESIS, "pending": [{}]})):
            with self.subTest(raw=raw):
                self._write(self.anchor, raw)
                evidence.verify_chain(self.ws)
                self.assertTrue(os.path.exists(path))
                with self.assertRaises(PbzError) as caught:
                    evidence._append_lines(self.ws, [{"next": True}])
                self.assertEqual(caught.exception.code, "REFUSED")

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
