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

from pbz import cli, dpapi, tokens  # noqa: E402
from pbz.classify import Classifier, looks_like_phone  # noqa: E402
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

    def test_schema_members_are_independent(self):
        text = '{"$schema":918273,"$schema":true,"title":"CANARY-NAME"}'
        out = tokens.tokenize_pbir(text, "definition/pages/p1/page.json", None, self.clf, self.tm)
        self.assertNotIn("918273", out)
        self.assertNotIn("true", out)
        self.assertNotIn("CANARY-NAME", out)
        self.assertIn("«payload:schema#", out)
        parsed = json.loads(out)
        self.assertIsInstance(parsed, dict)
        back, unknown = tokens.detokenize_pbir(out, self.tm)
        self.assertEqual(unknown, [])
        self.assertEqual(back, text)

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

    def test_malformed_deneb_is_opaque(self):
        raw = "'{\"data\":{\"values\":[{\"name\":\"CANARY-DENEB\"}'"
        body = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.4.0/schema.json",
            "visual": {"objects": {"vc": [{"properties": {"spec": {"expr": {"Literal": {"Value": raw}}}}}]}}
        }
        text = json.dumps(body)
        out = tokens.tokenize_pbir(text, "definition/pages/p/visuals/v/visual.json", None, self.clf, self.tm)
        self.assertNotIn("CANARY-DENEB", out)
        self.assertIn("payload:deneb", out)


class IndexMatchTests(unittest.TestCase):
    def setUp(self):
        self.clf = Classifier(None, None, {})
        self.clf.key = b"k" * 32
        self.clf.mode = "index"
        self.col = "People[Owner]"
        self.clf.col_kind[self.col] = "person"

    def _index(self, value):
        self.clf.hmac_to_col[dpapi.hmac_hex(self.clf.key, value)] = self.col

    def test_nonlatin_exact_match_and_phone_shape(self):
        cyr = "Иван Петров"
        cjk = "山田太郎"
        self._index(cyr)
        self._index(cjk)
        self.assertEqual(self.clf.hmac_column(cyr), self.col)
        self.assertEqual(self.clf.hmac_column(cjk), self.col)
        self.assertEqual(self.clf.hmac_column("иван петров"), self.col)
        self.assertIsNone(self.clf.hmac_column("обычная метка"))
        note = f"note {cyr} end"
        spans = self.clf.free_text(note)
        self.assertTrue(any(note[s:e] == cyr for s, e, _lab in spans))
        self.assertTrue(looks_like_phone("+1 (415) 555-1212"))
        self.assertTrue(looks_like_phone("415.555.1212"))
        self.assertFalse(looks_like_phone("2024-01-01"))
        self.assertFalse(looks_like_phone("1234567890.12"))
        self.assertIsNone(self.clf.literal("2024-01-01", "string", None))
        self.assertEqual(self.clf.literal("+1 (415) 555-1212", "string", None), "pii")


_VISUAL_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
    "visualContainer/2.4.0/schema.json")


class Round15ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tm = tokens.TokenMap(self.tmp.name)
        self.clf = Classifier(None, None, {})

    def test_long_digits_phones_dates_decimals(self):
        self.assertEqual(self.clf.literal("4111111111111111", "string", None), "pii")
        self.assertEqual(self.clf.literal("4111 1111 1111 1111", "string", None), "pii")
        self.assertTrue(looks_like_phone("4155551212"))
        self.assertIsNone(self.clf.literal("2024-01-01", "string", None))
        self.assertIsNone(self.clf.literal("12/31/2024", "string", None))
        self.assertIsNone(self.clf.literal("1234567890.12", "string", None))
        self.assertIsNone(self.clf.literal("1.25", "string", None))
        note = "card 4111 1111 1111 1111 on 2024-01-01"
        hidden = [note[s:e] for s, e, _lab in self.clf.free_text(note)]
        self.assertIn("4111 1111 1111 1111", hidden)
        self.assertNotIn("2024-01-01", hidden)
        phone = "tel:\n415 555 1212"
        exact = [phone[s:e] for s, e, _lab in self.clf.free_text(phone)]
        self.assertEqual([span for span in exact if "415" in span], ["415 555 1212"])

    def test_sentence_phones_and_emails_in_tmdl_and_pbir(self):
        sentence = "reach ada@example.com or +1 (415) 555-1212 today"
        spans = [sentence[s:e] for s, e, _lab in self.clf.free_text(sentence)]
        self.assertIn("ada@example.com", spans)
        self.assertIn("+1 (415) 555-1212", spans)
        text = "\ttable Sales\n\t\tmeasure M = 1\n\t\t\tdescription: " + sentence + "\n"
        out = tokens.tokenize_tmdl(text, "tables/Sales.tmdl", None, self.clf, self.tm)
        self.assertNotIn("ada@example.com", out)
        self.assertNotIn("415) 555-1212", out)
        back, unknown = tokens.detokenize_tmdl(out, self.tm)
        self.assertEqual(unknown, [])
        self.assertEqual(back, text)

        raw = "'reach o''brien@example.com or 415-555-1212'"
        body = {
            "$schema": _VISUAL_SCHEMA,
            "visual": {"objects": {"vc": [{"properties": {"t": {"expr": {"Literal": {"Value": raw}}}}}]}}
        }
        pbir = json.dumps(body)
        # Force a JSON unicode escape so the offset map cannot assume 1:1 bytes.
        pbir = pbir.replace("brien@example", "brien\\u0040example")
        pout = tokens.tokenize_pbir(pbir, "definition/pages/p/visuals/v/visual.json", None, self.clf, self.tm)
        self.assertNotIn("brien@example.com", pout)
        self.assertNotIn("brien\\u0040example.com", pout)
        self.assertNotIn("415-555-1212", pout)
        pback, unknown = tokens.detokenize_pbir(pout, self.tm)
        self.assertEqual(unknown, [])
        self.assertEqual(pback, pbir)

    def test_short_nonlatin_exact_index_only(self):
        self.clf.key = b"k" * 32
        self.clf.mode = "index"
        col = "People[Owner]"
        self.clf.col_kind[col] = "person"
        self.clf.hmac_to_col[dpapi.hmac_hex(self.clf.key, "李明")] = col
        self.assertEqual(self.clf.hmac_column("李明"), col)
        self.assertIsNone(self.clf.hmac_column("普通"))
        note = "see 李明 here"
        self.assertTrue(any(note[s:e] == "李明" for s, e, _lab in self.clf.free_text(note)))
        plain = "label 普通 stays"
        self.assertFalse(any("普通" == plain[s:e] for s, e, _lab in self.clf.free_text(plain)))

    def test_non_object_pbir_roots_are_refused(self):
        from pbz.common import PbzError
        for raw, rel in (
            ("[918273, true]", "definition/pages/p1/page.json"),
            ("918273", "definition/pages/p/visuals/v/visual.json"),
            ("true", "definition/report.json"),
        ):
            with self.assertRaises(PbzError) as caught:
                tokens.tokenize_pbir(raw, rel, None, self.clf, self.tm)
            self.assertEqual(caught.exception.code, "REFUSED")
            self.assertNotIn("918273", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
