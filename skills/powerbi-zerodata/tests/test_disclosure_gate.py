"""Offline checks for pbz doctor and the narrow fail-closed input gate."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))

from pbz import cli, tokens  # noqa: E402
from pbz.outcomes import envelope  # noqa: E402


class _Heuristic:
    """Approves every free-text span as visible. The gate must not trust that for unknown slots."""

    mode = "heuristic"

    def free_text(self, text, numbers=False):
        return []

    def literal(self, value, vtype, column):
        return None

    def label(self, table, column):
        return f"{table}[{column}]"

    def column_class(self, table, column):
        return "unknown"

    def _specific(self, value):
        return False


class DoctorTests(unittest.TestCase):
    def test_five_closed_statuses(self):
        out, rc = envelope(cli.cmd_doctor())
        self.assertEqual(out["verb"], "doctor")
        self.assertEqual(rc, 0)
        self.assertEqual(len(out["isolation"]), 5)
        ids = [item["id"] for item in out["isolation"]]
        self.assertEqual(ids, ["account", "ntfs", "broker", "network", "disposable"])
        self.assertTrue(all(item["status"] in {"configured", "unverified", "failed"} for item in out["isolation"]))
        self.assertIn("heuristic mode is not OS-isolated", out["note"])
        self.assertEqual(out["outcome"]["status"], "SUCCEEDED")


class InputGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tm = tokens.TokenMap(self.tmp.name)
        self.clf = _Heuristic()

    def test_unknown_tmdl_property_is_not_left_visible(self):
        text = "\ttable Sales\n\t\tdataType: string\n\t\tnote: salary note for the team\n"
        out = tokens.tokenize_tmdl(text, "tables/Sales.tmdl", None, self.clf, self.tm)
        self.assertIn("dataType: string", out)
        self.assertNotIn("salary note", out)
        self.assertIn("«lit#", out)
        back, unknown = tokens.detokenize_tmdl(out, self.tm)
        self.assertEqual(unknown, [])
        self.assertEqual(back, text)

    def test_unknown_pbir_schema_tokenizes_payload(self):
        body = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/9.9.9/schema.json",
            "name": "v1",
            "visual": {"visualType": "card", "title": "SECRET PAYLOAD"},
        }
        text = json.dumps(body)
        out = tokens.tokenize_pbir(text, "definition/pages/p/visuals/v/visual.json", None, self.clf, self.tm)
        self.assertNotIn("SECRET PAYLOAD", out)
        self.assertNotIn('"card"', out)
        self.assertNotIn('"v1"', out)
        self.assertIn("$schema", out)
        self.assertIn("«payload:schema#", out)
        back, unknown = tokens.detokenize_pbir(out, self.tm)
        self.assertEqual(unknown, [])
        self.assertEqual(json.loads(back)["visual"]["title"], "SECRET PAYLOAD")

    def test_unknown_tmdl_short_and_word_values_are_tokenized(self):
        text = "\ttable Sales\n\t\tnote: Confidential\n\t\tcode: 123456\n\t\tcc: US\n"
        out = tokens.tokenize_tmdl(text, "tables/Sales.tmdl", None, self.clf, self.tm)
        self.assertNotIn("Confidential", out)
        self.assertNotIn("123456", out)
        self.assertNotIn(": US", out)
        back, unknown = tokens.detokenize_tmdl(out, self.tm)
        self.assertEqual(unknown, [])
        self.assertEqual(back, text)

    def test_known_pbir_schema_keeps_structural_text(self):
        body = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.1.0/schema.json",
            "name": "p1",
            "displayName": "Canary Page",
            "displayOption": "FitToPage",
        }
        text = json.dumps(body)
        out = tokens.tokenize_pbir(text, "definition/pages/p1/page.json", None, self.clf, self.tm)
        self.assertIn("Canary Page", out)
        self.assertNotIn("payload:schema", out)

    def test_spoofed_pbir_schema_host_is_fail_closed(self):
        body = {
            "$schema": "https://example.invalid/definition/page/2.1.0/schema.json",
            "name": "p1",
            "displayName": "SECRET PAGE",
        }
        text = json.dumps(body)
        out = tokens.tokenize_pbir(text, "definition/pages/p1/page.json", None, self.clf, self.tm)
        self.assertNotIn("SECRET PAGE", out)
        self.assertIn("«payload:schema#", out)


if __name__ == "__main__":
    unittest.main()
