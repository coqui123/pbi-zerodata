"""Offline checks for pbz doctor and the narrow fail-closed input gate."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
import subprocess
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))

from pbz import audit, checks, cli, common, describe, dpapi, index_store, lexers, mirror, pbir, tmdl, tokens, unused  # noqa: E402
from pbz.common import PbzError  # noqa: E402
from pbz.classify import Classifier, looks_like_phone  # noqa: E402
from _junction import make_junction  # noqa: E402
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
        with self.assertRaises(PbzError) as caught:
            tokens.tokenize_pbir(text, "definition/pages/p1/page.json", None, self.clf, self.tm)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertNotIn("CANARY-NAME", str(caught.exception))

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

    def test_overlapping_number_does_not_uncover_indexed_tail(self):
        token = "12345-ABC"
        self._index(token)
        text = "-" + token
        hidden = [text[s:e] for s, e, _lab in self.clf.free_text(text, numbers=True)]
        self.assertTrue(any("ABC" in piece for piece in hidden), hidden)


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


class IndexLinkTests(unittest.TestCase):
    def test_index_junction_is_unsafe_and_missing_stays_heuristic(self):
        private = tempfile.TemporaryDirectory()
        outside = tempfile.TemporaryDirectory()
        bare = tempfile.TemporaryDirectory()
        self.addCleanup(private.cleanup)
        self.addCleanup(outside.cleanup)
        self.addCleanup(bare.cleanup)
        link = os.path.join(private.name, "index.json")
        if not make_junction(None, link, outside.name):
            self.skipTest("creating a junction is unavailable")
        self.addCleanup(os.rmdir, link)
        with self.assertRaises(PbzError) as caught:
            Classifier(None, private.name, {})
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        heuristic = Classifier(None, bare.name, {})
        self.assertEqual(heuristic.mode, "heuristic")
        self.assertIsNone(heuristic.index)


class IndexGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = SimpleNamespace(private=self.tmp.name)
        self.a = SimpleNamespace(name="A", mirror_model=self.tmp.name, mirror_report=self.tmp.name)
        self.b = SimpleNamespace(name="B", mirror_model=self.tmp.name)
        self.pol = {"disclosure": "shape"}
        self.cls = "category"
        self.model = tmdl.Model()
        definition = Path(self.tmp.name) / "definition"
        (definition / "tables").mkdir(parents=True)
        (definition / "tables" / "T.tmdl").write_text(
            "table T\n\tcolumn Status\n\t\tdataType: string\n\tcolumn Owner\n\t\tdataType: string\n")
        self.model = tmdl.load_model(str(definition))

    def engine(self, verb, payload, timeout=30):
        cols = {"T[Status]": {"type": "string", "class": self.cls},
                "T[Owner]": {"type": "string", "class": "category"}}
        (Path(self.ws.private) / "index.json").write_text(json.dumps({"columns": cols}))
        dpapi.load_key(self.ws.private)
        cats = {"T[Owner]": ["Ada Jones"]}
        if self.cls == "category":
            cats["T[Status]"] = ["Shared", "Member"]
        dpapi.write_protected(str(Path(self.ws.private) / "categories.bin"), json.dumps(cats).encode())
        return {"ok": True, "tables": [{"table": "T", "rows_bucket": "1-9", "columns": [
            {"column": "Status", "class": self.cls, "card_bucket": "1-9"},
            {"column": "Owner", "class": "category", "card_bucket": "1-9"}]}]}

    def run_index(self, project):
        with patch("pbz.checks.policy_mod.load", return_value=self.pol), \
                patch("pbz.checks.desktop.require_port", return_value=1), \
                patch("pbz.checks.engine_call", self.engine):
            return checks.index(self.ws, project)

    def test_project_b_after_index_a_last_and_membership(self):
        self.cls = "high"
        self.run_index(self.b)
        self.cls = "category"
        self.run_index(self.a)  # Pin the order: A overwrites the shared staging store last.
        a = Classifier(self.model, self.ws.private, {}, "A")
        b = Classifier(self.model, self.ws.private, {}, "B")
        self.assertEqual(a.category_values("T", "Status"), ["Shared", "Member"])
        self.assertIsNone(a.literal("Member", "string", ("T", "Status")))
        self.assertEqual(a.literal("New", "string", ("T", "Status")), "T[Status]")
        self.assertIsNone(b.category_values("T", "Status"))
        # Shared is a member in A: rejecting it in B requires identity, not membership.
        self.assertEqual(b.literal("Shared", "string", ("T", "Status")), "T[Status]")
        self.assertEqual(b.literal("Other", "string", ("T", "Status")), "T[Status]")
        with patch("pbz.describe.policy_mod.load", return_value={}):
            self.assertEqual(describe.values(self.ws, self.a, "T[Status]")["values"], ["Shared", "Member"])
            labels_b = describe.values(self.ws, self.b, "T[Status]")
        self.assertNotIn("values", labels_b)
        self.assertNotIn("Shared", json.dumps(labels_b))


    def test_removed_column_cannot_use_stored_membership(self):
        self.run_index(self.a)
        missing = tmdl.Model()
        clf = Classifier(missing, self.ws.private, {}, "A")
        self.assertIsNone(clf.category_values("T", "Status"))
        self.assertEqual(clf.column_class("T", "Status"), "sensitive")
        self.assertEqual(Classifier(None, self.ws.private, {}, "A").category_values("T", "Status"),
                         ["Shared", "Member"])

    def test_publish_hash_mismatch_keeps_previous_generation(self):
        self.run_index(self.a)
        before = index_store.load(self.ws.private, "A")
        self.assertIsNotNone(before)
        manifest = (Path(self.ws.private) / "index-current.json").read_bytes()
        directories = sorted(Path(self.ws.private).glob("index-*"))
        for name in ("index.json", "categories.bin"):
            with self.subTest(file=name), self.assertRaises(PbzError) as raised:
                index_store.publish(self.ws.private, "A", {}, expected={name: "0" * 64})
            self.assertEqual(raised.exception.code, "REFUSED")
            self.assertEqual((Path(self.ws.private) / "index-current.json").read_bytes(), manifest)
            self.assertEqual(sorted(Path(self.ws.private).glob("index-*")), directories)
            self.assertEqual(index_store.load(self.ws.private, "A"), before)

    def test_index_pins_artifacts_before_rebuilding_summary(self):
        rebuild = checks._rebuild_summary
        for name in ("categories.bin", "index.json"):
            with self.subTest(file=name):
                snapshot = {}

                def replace_staging(private_dir, tables, idx=None):
                    for artifact in ("index.json", "categories.bin"):
                        snapshot[artifact] = (Path(private_dir) / artifact).read_bytes()
                    # Replace before rebuilding: both classes and published bytes must be pinned.
                    (Path(private_dir) / name).write_bytes(b'{"columns": {"T[Owner]": {"class": "category"}}}')
                    return rebuild(private_dir, tables, idx=idx)

                with patch("pbz.checks._rebuild_summary", replace_staging):
                    self.run_index(self.a)
                stored = index_store.load(self.ws.private, "A")
                self.assertIsNotNone(stored)
                manifest = json.loads((Path(self.ws.private) / "index-current.json").read_bytes())
                for artifact, raw in snapshot.items():
                    self.assertEqual(stored[artifact], raw)
                    self.assertEqual(manifest["sha256"][artifact], hashlib.sha256(raw).hexdigest())
                summary = json.loads(stored["index-summary.json"])
                self.assertEqual(summary["tables"][0]["columns"][1]["class"], "high")
                self.assertEqual(json.loads(stored["index.json"])["columns"]["T[Owner]"]["class"], "high")
                self.assertNotIn("T[Owner]", json.loads(dpapi.unprotect(stored["categories.bin"])))

    def test_index_publishes_hardener_bytes_after_staging_replacement(self):
        harden = checks.harden_index
        snapshot = {}

        def replace_staging(ws, pol):
            result = harden(ws, pol)
            snapshot.update(result[1])
            path = Path(ws.private) / "index.json"
            idx = json.loads(path.read_bytes())
            idx["columns"]["T[Owner]"]["class"] = "category"
            path.write_bytes(json.dumps(idx).encode("utf-8"))
            return result

        with patch("pbz.checks.harden_index", replace_staging):
            self.run_index(self.a)
        stored = index_store.load(self.ws.private, "A")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["index.json"], snapshot["index.json"])
        self.assertEqual(json.loads(stored["index.json"])["columns"]["T[Owner]"]["class"], "high")
        manifest = json.loads((Path(self.ws.private) / "index-current.json").read_bytes())
        self.assertEqual((Path(self.ws.private) / manifest["generation"] / "index.json").read_bytes(),
                         snapshot["index.json"])

    def test_prune_enumeration_failure_keeps_committed_publication(self):
        for mid_iteration in (False, True):
            with self.subTest(mid_iteration=mid_iteration):
                self.cls = "category"
                self.run_index(self.a)
                path = Path(self.ws.private) / "index-current.json"
                first = json.loads(path.read_bytes())["generation"]
                self.cls = "high"

                def broken_scan(*args):
                    if mid_iteration:
                        yield SimpleNamespace(name="unrelated")
                    raise OSError("enumeration failed")

                kwargs = ({"side_effect": broken_scan} if mid_iteration else
                          {"side_effect": OSError("enumeration failed")})
                with patch("pbz.index_store.os.scandir", **kwargs):
                    self.run_index(self.a)
                manifest = json.loads(path.read_bytes())
                self.assertNotEqual(manifest["generation"], first)
                stored = index_store.load(self.ws.private, "A")
                self.assertIsNotNone(stored)
                self.assertEqual(json.loads(stored["index.json"])["columns"]["T[Status]"]["class"], "high")
                for name, raw in stored.items():
                    self.assertEqual((Path(self.ws.private) / manifest["generation"] / name).read_bytes(), raw)

    def test_category_values_require_matching_known_type(self):
        self.run_index(self.a)
        col = self.model.column("T", "Status")
        for dtype, expected in (("STRING", ["Shared", "Member"]), ("binary", None),
                                ("", ["Shared", "Member"])):
            with self.subTest(dtype=dtype):
                col.data_type = dtype
                clf = Classifier(self.model, self.ws.private, {}, "A")
                self.assertEqual(clf.category_values("T", "Status"), expected)

    def test_prune_failure_does_not_undo_committed_publication(self):
        self.run_index(self.a)
        path = Path(self.ws.private) / "index-current.json"
        first = json.loads(path.read_bytes())["generation"]
        self.cls = "high"
        with patch("pbz.index_store.shutil.rmtree", side_effect=OSError("open handle")):
            self.run_index(self.a)
        manifest = json.loads(path.read_bytes())
        self.assertNotEqual(manifest["generation"], first)
        self.assertTrue((Path(self.ws.private) / first).is_dir())
        stored = index_store.load(self.ws.private, "A")
        self.assertEqual(json.loads(stored["index.json"])["columns"]["T[Status]"]["class"], "high")
        self.assertEqual((Path(self.ws.private) / manifest["generation"] / "index.json").read_bytes(),
                         stored["index.json"])

    def test_prune_reparse_point_is_unlinked_and_unlink_failure_is_harmless(self):
        self.run_index(self.a)
        path = Path(self.ws.private) / "index-current.json"
        first = json.loads(path.read_bytes())["generation"]
        old = Path(self.ws.private) / first
        real_reparse = index_store._reparse_link

        def reparse(filename):
            return Path(filename) == old or real_reparse(filename)

        with patch("pbz.index_store._reparse_link", reparse), \
                patch("pbz.index_store.os.unlink", side_effect=OSError("open junction")) as unlink, \
                patch("pbz.index_store.shutil.rmtree", side_effect=AssertionError("must not traverse junction")):
            self.run_index(self.a)
        unlink.assert_called_once_with(str(old))
        self.assertNotEqual(json.loads(path.read_bytes())["generation"], first)
        self.assertIsNotNone(index_store.load(self.ws.private, "A"))

    def test_load_retries_if_manifest_generation_was_pruned(self):
        self.run_index(self.a)
        path = Path(self.ws.private) / "index-current.json"
        stale = path.read_bytes()
        self.cls = "high"
        self.run_index(self.a)
        expected = index_store.load(self.ws.private, "A")
        real_read = index_store.read_checked
        reads = []

        def racing_read(filename, **kwargs):
            if Path(filename) == path:
                reads.append(filename)
                if len(reads) == 1:
                    return stale
            return real_read(filename, **kwargs)

        with patch("pbz.index_store.read_checked", racing_read):
            self.assertEqual(index_store.load(self.ws.private, "A"), expected)
        self.assertEqual(len(reads), 2)

    def test_strict_load_refuses_damaged_project_identity(self):
        self.run_index(self.a)
        self.assertIsNone(index_store.load_strict(self.ws.private, "B"))
        self.assertIsNone(Classifier(self.model, self.ws.private, {}, "B").index)
        for damaged in ({}, {"project": 123}):
            (Path(self.ws.private) / "index-current.json").write_text(json.dumps(damaged))
            for read in (lambda: index_store.load_strict(self.ws.private, "A"),
                         lambda: Classifier(self.model, self.ws.private, {}, "A")):
                with self.subTest(manifest=damaged), self.assertRaises(PbzError) as raised:
                    read()
                self.assertEqual(raised.exception.code, "REFUSED")
                self.assertEqual(str(raised.exception), "REFUSED: private index could not be read")

    def test_audit_and_unused_receive_project_summary(self):
        self.run_index(self.a)
        with patch("pbz.describe.policy_mod.load", return_value={"disclosure": "shape"}):
            self.assertEqual(audit._index(self.ws, self.a)["t"]["cols"]["status"], "1-9")
            self.assertEqual(unused._index_summary(self.ws, self.a)["T"]["rows_bucket"], "1-9")
        with patch("pbz.describe._index_summary", return_value={"T": {}, "U": {
                "columns": [None, 4, {}, {"column": "Status", "card_bucket": "1-9"}]}}):
            summary = audit._index(self.ws, self.a)
        self.assertEqual(summary["t"]["cols"], {})
        self.assertEqual(summary["u"]["cols"], {"status": "1-9"})

    def test_publish_prunes_old_generations_and_unlinks_symlinks(self):
        self.run_index(self.a)
        first = json.loads((Path(self.ws.private) / "index-current.json").read_text())["generation"]
        target = Path(self.tmp.name) / "keep"
        target.mkdir()
        (target / "marker").write_text("keep")
        link = Path(self.ws.private) / ("index-" + "f" * 32)
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("file symlink is unavailable")
        self.cls = "high"
        self.run_index(self.a)
        directories = [p for p in Path(self.ws.private).glob("index-*") if p.is_dir()]
        self.assertEqual(len(directories), 1)
        self.assertNotEqual(directories[0].name, first)
        self.assertFalse(link.is_symlink())
        self.assertEqual((target / "marker").read_text(), "keep")
        stored = index_store.load(self.ws.private, "A")
        self.assertIsNotNone(stored)
        self.assertEqual(json.loads(stored["index.json"])["columns"]["T[Status]"]["class"], "high")

    def test_strict_load_refuses_missing_generation_files(self):
        self.assertIsNone(index_store.load_strict(self.ws.private, "A"))
        self.run_index(self.a)
        self.assertIsNone(index_store.load_strict(self.ws.private, "B"))
        manifest = json.loads((Path(self.ws.private) / "index-current.json").read_text())
        (Path(self.ws.private) / manifest["generation"] / "index.json").unlink()
        self.assertIsNone(index_store.load(self.ws.private, "A"))
        with self.assertRaises(PbzError) as raised:
            index_store.load_strict(self.ws.private, "A")
        self.assertEqual(raised.exception.code, "REFUSED")

    def test_describe_readers_pin_one_generation(self):
        self.run_index(self.a)
        stored = index_store.load(self.ws.private, "A")
        for read in (lambda: describe.describe(self.ws, self.a, "T"),
                     lambda: describe.values(self.ws, self.a, "T[Owner]")):
            with self.subTest(read=read), patch("pbz.index_store.load", return_value=stored) as load, \
                    patch("pbz.index_store.load_strict", side_effect=AssertionError("unexpected reload")), \
                    patch("pbz.describe.policy_mod.load", return_value={}):
                read()
                load.assert_called_once_with(self.ws.private, "A")
        with patch("pbz.index_store.load_strict", side_effect=AssertionError("unexpected reload")):
            self.assertIsNone(Classifier(self.model, self.ws.private, {}, "A", stored=None).index)
        with patch("pbz.index_store.load", side_effect=AssertionError("unexpected reload")):
            self.assertEqual(describe._index_summary(self.ws, self.a, stored=None), {})

    def test_hardened_summary_and_all_artifacts_are_one_generation(self):
        out = self.run_index(self.a)
        stored = index_store.load(self.ws.private, "A")
        idx = json.loads(stored["index.json"])
        cats = json.loads(dpapi.unprotect(stored["categories.bin"]))
        summary = json.loads(stored["index-summary.json"])
        self.assertEqual(out["personal_or_id_columns_hashed"], 1)
        self.assertEqual(idx["columns"]["T[Owner]"]["class"], "high")
        self.assertNotIn("T[Owner]", cats)
        self.assertEqual(summary["tables"][0]["columns"][1]["class"], "high")
        manifest = json.loads((Path(self.ws.private) / "index-current.json").read_text())
        path = Path(self.ws.private) / manifest["generation"] / "categories.bin"
        path.write_bytes(b"mixed generation")
        self.assertIsNone(index_store.load(self.ws.private, "A"))
        with self.assertRaises(PbzError) as raised:
            Classifier(self.model, self.ws.private, {}, "A")
        self.assertEqual(raised.exception.code, "REFUSED")
        self.assertEqual(describe._index_summary(self.ws, self.a), {})
        self.assertEqual(audit._index(self.ws, self.a), {})
        self.assertEqual(unused._index_summary(self.ws, self.a), {})

    def test_kill_before_hardening_never_publishes_personal_category(self):
        # A real child dies after the engine writes a low-cardinality PERSONAL column,
        # before hardening. This fixture would move Owner to high if allowed to finish.
        script = r"""
import json, os, signal, sys
from types import SimpleNamespace
from unittest.mock import patch
from pbz import checks, dpapi
ws = SimpleNamespace(private=sys.argv[1])
def engine(*args, **kwargs):
    with open(os.path.join(ws.private, "index.json"), "w") as f:
        json.dump({"columns": {"T[Owner]": {"type": "string", "class": "category"}}}, f)
    dpapi.load_key(ws.private)
    dpapi.write_protected(os.path.join(ws.private, "categories.bin"), b'{"T[Owner]":["Ada Jones"]}')
    return {"ok": True, "tables": [{"table": "T", "columns": [{"column": "Owner", "class": "category"}]}]}
def kill(*args):
    with open(sys.argv[2], "w") as marker:
        marker.write("harden hook reached")
    os.kill(os.getpid(), signal.SIGTERM)
with patch("pbz.checks.policy_mod.load", return_value={}), patch("pbz.checks.desktop.require_port", return_value=1), patch("pbz.checks.engine_call", engine), patch("pbz.checks.harden_index", kill):
    checks.index(ws, SimpleNamespace(name="A"))
"""
        marker = Path(self.tmp.name) / "harden-reached"
        proc = subprocess.run([sys.executable, "-c", script, self.ws.private, str(marker)], cwd=SKILL,
                              capture_output=True, timeout=30)
        self.assertNotEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertTrue(marker.is_file())
        staged = json.loads((Path(self.ws.private) / "index.json").read_text())
        self.assertEqual(staged["columns"]["T[Owner]"]["class"], "category")
        self.assertIsNone(index_store.load(self.ws.private, "A"))
        self.assertIsNone(Classifier(self.model, self.ws.private, {}, "A").category_values("T", "Owner"))
        self.assertEqual(describe._index_summary(self.ws, self.a), {})
        self.assertEqual(audit._index(self.ws, self.a), {})
        self.assertEqual(unused._index_summary(self.ws, self.a), {})

    def test_failed_reindex_keeps_previous_completed_generation(self):
        self.run_index(self.a)
        before = index_store.load(self.ws.private, "A")
        self.assertIsNotNone(before)
        with patch("pbz.checks.harden_index", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                self.run_index(self.b)
        self.assertEqual(index_store.load(self.ws.private, "A"), before)
        self.assertIsNone(index_store.load(self.ws.private, "B"))


class TypedSensitivityTests(unittest.TestCase):
    _m2_page_path = Path("Mini.Report/definition/pages/p1/page.json")
    _m2_visual_path = _m2_page_path.parent / "visuals/v1/visual.json"
    _m2_definition = Path("Mini.SemanticModel/definition")
    _m2_policy = {
        "sensitive_columns": [
            "Events[SecretDate]", "Events[SecretStamp]", "Events[SecretFlag]", "Events[Name]",
        ],
        "category_columns": ["Events[KindFlag]"],
    }
    _m2_sites = (
        ("SecretDate", "datetime'2024-03-05T00:00:00'"),
        ("SecretDate", "datetime'2024-04-01T00:00:00'", True),
        ("SecretStamp", "datetime'2024-03-05T06:30:00'"),
        ("SecretStamp", "datetimeoffset'2024-03-05T06:30:00'"),
        ("SecretFlag", "true"),
        ("SecretFlag", "false"),
        ("Name", "'Canary Person'"),
        ("OpenDate", "datetime'2025-01-15T00:00:00'"),
        ("OpenFlag", "false"),
        ("KindFlag", "true"),
        ("OpenFlag", "datetime'2025-02-02T00:00:00'"),
        ("OpenDate", "false"),
    )
    _m2_titles = ("true", "datetime'2025-06-01T00:00:00'")

    def _m2_page(self, sites):
        filters = []
        for index, (column, value, *in_site) in enumerate(sites, 1):
            field = {"Column": {
                "Expression": {"SourceRef": {"Entity": "Events"}}, "Property": column,
            }}
            left = {"Column": {
                "Expression": {"SourceRef": {"Source": "e"}}, "Property": column,
            }}
            literal = {"Literal": {"Value": value}}
            if in_site:
                condition = {"In": {"Expressions": [left], "Values": [[literal]]}}
            else:
                condition = {"Comparison": {"ComparisonKind": 0, "Left": left, "Right": literal}}
            filters.append({
                "name": f"f{index}", "field": field, "type": "Categorical",
                "filter": {
                    "Version": 2, "From": [{"Name": "e", "Entity": "Events", "Type": 0}],
                    "Where": [{"Condition": condition}],
                },
            })
        return {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.1.0/schema.json",
            "name": "p1", "displayName": "Overview", "filterConfig": {"filters": filters},
        }

    def _m2_values(self, page):
        sites = []
        for entry in page["filterConfig"]["filters"]:
            column = entry["field"]["Column"]["Property"]
            condition = entry["filter"]["Where"][0]["Condition"]
            if "Comparison" in condition:
                literal = condition["Comparison"]["Right"]
            else:
                literal = condition["In"]["Values"][0][0]
            sites.append((column, literal["Literal"]["Value"]))
        return sites

    def _m2_workspace(self, root, private, policy):
        columns = {
            "SecretDate": "dateTime", "SecretStamp": "dateTime", "SecretFlag": "boolean",
            "Name": "string", "OpenDate": "dateTime", "OpenFlag": "boolean",
            "KindFlag": "boolean", "Count": "int64", "Amount": "double",
        }
        table = "table Events\n"
        for index, (column, dtype) in enumerate(columns.items(), 1):
            table += (
                f"\n\tcolumn {column}\n\t\tlineageTag: 00000000-0000-0000-0000-{index:012d}\n"
                f"\t\tdataType: {dtype}\n\t\tsourceColumn: {column}\n"
            )
        visual = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.10.0/schema.json",
            "name": "v1",
            "visual": {"visualType": "card", "objects": {"general": [{"properties": {
                "title": {"expr": {"Literal": {"Value": self._m2_titles[0]}}},
                "title2": {"expr": {"Literal": {"Value": self._m2_titles[1]}}},
            }}]}},
        }
        files = {
            Path("Mini.pbip"): json.dumps({
                "version": "1.0", "artifacts": [
                    {"report": {"path": "Mini.Report"}}, {"dataset": {"path": "Mini.SemanticModel"}},
                ],
            }),
            Path("Mini.SemanticModel/model.tmdl"): "database\n\nref table Events\n",
            self._m2_definition / "tables/Events.tmdl": table,
            Path("Mini.SemanticModel/definition.pbism"): json.dumps({"version": "4.0", "settings": {}}),
            Path("Mini.Report/definition.pbir"): json.dumps({
                "version": "4.0", "datasetReference": {"byPath": {"path": "../Mini.SemanticModel"}},
            }),
            self._m2_page_path: json.dumps(self._m2_page(self._m2_sites), indent=2),
            self._m2_visual_path: json.dumps(visual, indent=2),
            Path(".pbz/policy.json"): json.dumps({**self._m2_policy, **policy}),
        }
        for relative, content in files.items():
            path = Path(root) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("utf-8"))
        ws = common.Workspace(root)
        ws.private = private
        return ws, ws.project()

    def _m2_pull(self, ws, project):
        mirror.pull(ws, project)
        page = Path(ws.mirror) / self._m2_page_path
        return self._m2_values(json.loads(page.read_text(encoding="utf-8")))

    def _m2_assert_token(self, column, value):
        self.assertTrue(value.startswith("«"), (column, value))
        self.assertIn(f"Events[{column}]", value)

    def _m2_assert_edit_restored(self, root, ws, project, authored):
        real_page = Path(root) / self._m2_page_path
        published = Path(ws.mirror) / self._m2_page_path
        text = published.read_bytes()
        before, after = b'"Overview"', b'"Overview Edited"'
        self.assertEqual(text.count(before), 1)
        self.assertEqual(authored.count(before), 1)
        published.write_bytes(text.replace(before, after, 1))
        # The synthetic report lacks the metadata needed by the Desktop validators.
        with patch("pbz.mirror.desktop.processes", return_value=[]), patch(
            "pbz.mirror.validate.run_stage", return_value=[],
        ):
            result = mirror.push(ws, project)
        self.assertEqual(result["result"], "PUSHED", result)
        self.assertEqual(real_page.read_bytes(), authored.replace(before, after, 1))

    def test_sensitive_datetime_hidden(self):
        for policy in ({}, {"literals": "strict"}):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as private:
                ws, project = self._m2_workspace(root, private, policy)
                sites = self._m2_pull(ws, project)
                for index in (0, 1):
                    column, value = sites[index]
                    self.assertEqual(column, "SecretDate")
                    self._m2_assert_token(column, value)
                    self.assertNotIn(self._m2_sites[index][1], [value for _, value in sites])

    def test_sensitive_offset_hidden(self):
        for policy in ({}, {"literals": "strict"}):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as private:
                ws, project = self._m2_workspace(root, private, policy)
                sites = self._m2_pull(ws, project)
                column, value = sites[3]
                self.assertEqual(column, "SecretStamp")
                self._m2_assert_token(column, value)
                self.assertNotIn(self._m2_sites[3][1], [value for _, value in sites])

    def test_sensitive_bool_hidden(self):
        for policy in ({}, {"literals": "strict"}):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as private:
                ws, project = self._m2_workspace(root, private, policy)
                sites = self._m2_pull(ws, project)
                for column, value in sites[4:6]:
                    self.assertEqual(column, "SecretFlag")
                    self._m2_assert_token(column, value)
                    self.assertNotIn(value, ("true", "false"))
                self.assertNotEqual(sites[4][1], sites[5][1])

    def test_controls_unchanged(self):
        def snapshot(root, published):
            return {path.relative_to(root): path.read_bytes() for path in root.rglob("*")
                    if path.is_file() and not path.is_relative_to(published)}

        for policy in ({}, {"literals": "strict"}):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as private:
                ws, project = self._m2_workspace(root, private, policy)
                before = snapshot(Path(root), Path(ws.mirror))
                sites = self._m2_pull(ws, project)
                self.assertEqual(sites[7:], list(self._m2_sites[7:]))
                column, value = sites[6]
                self.assertEqual(column, "Name")
                self._m2_assert_token(column, value)
                visual_path = Path(ws.mirror) / self._m2_visual_path
                visual = json.loads(visual_path.read_text(encoding="utf-8"))
                properties = visual["visual"]["objects"]["general"][0]["properties"]
                self.assertEqual(
                    tuple(properties[title]["expr"]["Literal"]["Value"] for title in ("title", "title2")),
                    self._m2_titles,
                )
                self.assertEqual(mirror.push(ws, project)["result"], "NOTHING_TO_PUSH")
                self.assertEqual(snapshot(Path(root), Path(ws.mirror)), before)

    def test_restoration_after_edit(self):
        for policy in ({}, {"literals": "strict"}):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as private:
                ws, project = self._m2_workspace(root, private, policy)
                authored = (Path(root) / self._m2_page_path).read_bytes()
                sites = self._m2_pull(ws, project)
                self.assertEqual(
                    [column for column, _ in sites[:6]],
                    [site[0] for site in self._m2_sites[:6]],
                )
                for column, value in sites[:6]:
                    self._m2_assert_token(column, value)
                self._m2_assert_edit_restored(root, ws, project, authored)

    def test_collision_pair_distinct_tokens(self):
        for policy in ({}, {"literals": "strict"}):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as private:
                ws, project = self._m2_workspace(root, private, policy)
                authored = (Path(root) / self._m2_page_path).read_bytes()
                sites = self._m2_pull(ws, project)
                for column, value in sites[2:4]:
                    self.assertEqual(column, "SecretStamp")
                    self._m2_assert_token(column, value)
                self.assertNotEqual(sites[2][1], sites[3][1])
                self._m2_assert_edit_restored(root, ws, project, authored)

    def test_unit_pins(self):
        stamp = "2024-03-05T06:30:00"
        offset = "datetimeoffset'2024-03-05T06:30:00'"
        with self.subTest(pin="encode datetimeoffset"):
            self.assertEqual(pbir.encode_literal(stamp, "datetimeoffset"), offset)

        with tempfile.TemporaryDirectory() as tmp:
            tm = tokens.TokenMap(tmp)
            for label, value, vtype, expected in (
                ("Events[SecretStamp]", stamp, "datetimeoffset", offset),
                ("Events[SecretFlag]", True, "bool", "true"),
                ("Events[SecretFlag]", False, "bool", "false"),
                ("Events[OpenDate]", "2025-01-15T00:00:00", "datetime", "datetime'2025-01-15T00:00:00'"),
            ):
                with self.subTest(pin="fallback", value=value, vtype=vtype):
                    tok = tm.lit(label, value, vtype, "other_ctx", "ignored")
                    self.assertEqual(tokens.detokenize_pbir(json.dumps(tok), tm), (json.dumps(expected), []))

            self.assertEqual(pbir.decode_literal(offset), (stamp, "datetime"))
            self.assertEqual(pbir.decode_literal("true"), (True, "bool"))
            for policy in ({}, {"literals": "strict"}):
                with self.subTest(pin="index category", policy=policy):
                    clf = Classifier(None, None, policy)
                    clf.mode = "index"
                    clf.index = {"columns": {"T[Kind]": {"class": "category", "type": "string"}}}
                    clf.categories = {"T[Kind]": ["2024-01-01T00:00:00"]}
                    self.assertEqual(clf.literal(True, "bool", ("T", "Kind")), "T[Kind]")
                    self.assertEqual(clf.literal("2025-05-05T00:00:00", "datetime", ("T", "Kind")), "T[Kind]")
                    self.assertIsNone(clf.literal("2024-01-01T00:00:00", "datetime", ("T", "Kind")))

            definition = Path(tmp) / "definition"
            (definition / "tables").mkdir(parents=True)
            (definition / "model.tmdl").write_text("model Model\n\nref table T\n", encoding="utf-8")
            (definition / "tables/T.tmdl").write_text(
                "table T\n\tcolumn A\n\t\tdataType: string\n", encoding="utf-8",
            )
            model = tmdl.load_model(str(definition))
            clf = Classifier(model, None, {"category_columns": ["T[Gone]"]})
            clf.index = {"columns": {}}
            self.assertEqual(clf.column_class("T", "Gone"), "sensitive")

            tm2 = tokens.TokenMap(str(Path(tmp) / "hmac"))
            tok = tm2.lit("T[B]", True, "bool", "pbir_full", '"true"')
            key = b"k" * 32
            digest = dpapi.hmac_hex(key, "true")
            self.assertEqual(tm2.find_by_hmac(key, "T[B]", digest), tok)
            self.assertIsNone(tm2.find_by_hmac(key, "T[Other]", digest))

    def test_dispatch_arguments(self):
        class RecordingClassifier(Classifier):
            def __init__(self, model, policy):
                super().__init__(model, None, policy)
                self.calls = []

            def literal(self, value, vtype, column):
                self.calls.append((value, vtype, column))
                return super().literal(value, vtype, column)

        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as private:
            self._m2_workspace(root, private, {})
            model = tmdl.load_model(str(Path(root) / self._m2_definition))
            clf = RecordingClassifier(model, self._m2_policy)
            tm = tokens.TokenMap(private)
            page = (Path(root) / self._m2_page_path).read_text(encoding="utf-8")
            relative = "definition/pages/p1/page.json"
            tokens.tokenize_pbir(page, relative, model, clf, tm)
            extra = self._m2_page((
                ("Count", "5L"), ("Amount", "1.5D"), ("OpenFlag", "datetime'2025-02-02T00:00:00'"),
            ))
            tokens.tokenize_pbir(json.dumps(extra), relative, model, clf, tm)
            visual = (Path(root) / self._m2_visual_path).read_text(encoding="utf-8")
            tokens.tokenize_pbir(
                visual, "definition/pages/p1/visuals/v1/visual.json", model, clf, tm,
            )

        for call in (
            (True, "bool", ("Events", "SecretFlag")),
            (False, "bool", ("Events", "SecretFlag")),
            ("2024-03-05T00:00:00", "datetime", ("Events", "SecretDate")),
            ("Canary Person", "string", ("Events", "Name")),
            (5, "number", ("Events", "Count")),
            (1.5, "number", ("Events", "Amount")),
            (True, "bool", None),
            ("2025-06-01T00:00:00", "datetime", None),
            ("2025-02-02T00:00:00", "datetime", ("Events", "OpenFlag")),
            (False, "bool", ("Events", "OpenDate")),
        ):
            with self.subTest(call=call):
                self.assertIn(call, clf.calls)
        with self.subTest(column="SecretStamp"):
            stamp = ("2024-03-05T06:30:00", "datetime", ("Events", "SecretStamp"))
            self.assertGreaterEqual(clf.calls.count(stamp), 2)
        for value, vtype, column in clf.calls:
            if isinstance(value, bool):
                self.assertEqual(vtype, "bool")
            if isinstance(value, str) and column in (("Events", "SecretDate"), ("Events", "SecretStamp")):
                self.assertEqual(vtype, "datetime")

    def test_raw_sharing_limit(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as private:
            self._m2_workspace(root, private, {})
            model = tmdl.load_model(str(Path(root) / self._m2_definition))
            clf = Classifier(model, None, self._m2_policy)
            tm = tokens.TokenMap(private)
            page = self._m2_page((("SecretFlag", "true"), ("SecretFlag", "true\n")))
            output = tokens.tokenize_pbir(
                json.dumps(page), "definition/pages/p1/page.json", model, clf, tm,
            )
            sites = self._m2_values(json.loads(output))
            self.assertTrue(sites[0][1].startswith("«"), sites)
            self.assertEqual(sites[0][1], sites[1][1])
            restored, unknown = tokens.detokenize_pbir(output, tm)
            self.assertEqual(unknown, [])
            self.assertEqual(
                self._m2_values(json.loads(restored)), [("SecretFlag", "true"), ("SecretFlag", "true")],
            )


class EmbeddedRowConstructorTests(unittest.TestCase):
    EVENTS = [
        ("Name", "string"), ("Amount", "double"), ("When", "dateTime"),
        ("Kind", "string"), ("R", "string"), ("X", "double"), ("Net Amount", "double"),
    ]
    _m3_modes = ({}, {"literals": "strict"})
    _m3_edit = b"/// harmless m3 edit\n"
    _m3_rows = '{{"qz-4815 widget", 7}, {"qz-90210 gadget", 67890}}'
    _m3_type = "type table [Name = text, Amount = number]"
    _m3_names = '{"Name", "Amount"}'
    _m3_context = 'List.Contains(Table.Column(Table.FromRows({{12345}}, {"Amount"}), "Amount"), [Amount])'
    _m3_context_default = 'List.Contains(Table.FromList({1}, null, {"Amount"}, 12345), [Amount])'
    _m3_comment = ('Table.FromRows({{/* contact a@b.co */ "qz-4815 widget", 7}, '
                   '{Table.FromRecords({424242})}}, SomeHeader)')

    def _m3_tm(self):
        result = object.__new__(tokens.TokenMap)
        result.data = {"version": 1, "next": {}, "tokens": {}, "keys": {}}
        result.dirty = False
        return result

    def _m3_header(self, table, columns):
        header = f"table {table}\n"
        for name, dtype in columns:
            if " " in name:
                name = f"'{name}'"
            header += f"\tcolumn {name}\n\t\tdataType: {dtype}\n"
        return header

    def _m3_model(self, root, spec):
        tables = Path(root) / "tables"
        tables.mkdir()
        for table, columns in spec.items():
            (tables / f"{table}.tmdl").write_text(
                self._m3_source(table, columns, "0"), encoding="utf-8",
            )
        return tmdl.load_model(root)

    def _m3_events(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        return self._m3_model(root, {"Events": self.EVENTS})

    def _m3_source(self, table, columns, expr, kind="m"):
        return (
            self._m3_header(table, columns)
            + f"\tpartition {table} = {kind}\n\t\tmode: import\n\t\tsource = {expr}\n"
        )

    def _m3_tok(self, source, model, policy=None, clf=None):
        tm = self._m3_tm()
        if clf is None:
            clf = Classifier(model, None, policy or {})
        return tokens.tokenize_tmdl(source, "tables/Events.tmdl", model, clf, tm), tm

    def _m3_roundtrip(self, source, model, policy=None, clf=None):
        output, tm = self._m3_tok(source, model, policy, clf)
        self.assertEqual(tokens.detokenize_tmdl(output, tm), (source, []))
        return output

    def _m3_sig(self, expr):
        return lexers.significant(lexers.lex_m(expr, 0))

    def _m3_tokens_in(self, text):
        import re

        return re.findall(r"«[^»]*»", text)

    def _m3_source_line(self, text):
        return next(line for line in text.splitlines() if "source =" in line)

    def _m3_line_tokens(self, line):
        return [token.split("#", 1)[0] for token in self._m3_tokens_in(line)]

    def _m3_workspace(self, root, private, tables, policy):
        if os.name != "nt":
            # These synthetic workspaces exercise pull and push on hosts without Windows DPAPI.
            self.enterContext(patch.object(dpapi, "protect", side_effect=lambda data: data))
            self.enterContext(patch.object(dpapi, "unprotect", side_effect=lambda data: data))
        m2 = TypedSensitivityTests()
        visual = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.10.0/schema.json",
            "name": "v1",
            "visual": {"visualType": "card", "objects": {"general": [{"properties": {
                "title": {"expr": {"Literal": {"Value": m2._m2_titles[0]}}},
                "title2": {"expr": {"Literal": {"Value": m2._m2_titles[1]}}},
            }}]}},
        }
        files = {
            Path("Mini.pbip"): json.dumps({
                "version": "1.0", "artifacts": [
                    {"report": {"path": "Mini.Report"}}, {"dataset": {"path": "Mini.SemanticModel"}},
                ],
            }),
            Path("Mini.SemanticModel/model.tmdl"): "database\n\n" + "".join(
                f"ref table {table}\n" for table in tables
            ),
            Path("Mini.SemanticModel/definition.pbism"): json.dumps({"version": "4.0", "settings": {}}),
            Path("Mini.Report/definition.pbir"): json.dumps({
                "version": "4.0", "datasetReference": {"byPath": {"path": "../Mini.SemanticModel"}},
            }),
            m2._m2_page_path: json.dumps(m2._m2_page([]), indent=2),
            m2._m2_visual_path: json.dumps(visual, indent=2),
            Path(".pbz/policy.json"): json.dumps({
                "sensitive_columns": [
                    f"{table}[{column}]" for table, (columns, _, _) in tables.items()
                    for column, _ in columns if column in ("Name", "Amount")
                ],
                **policy,
            }),
        }
        for table_index, (table, (columns, kind, expr)) in enumerate(tables.items(), 1):
            source = self._m3_source(table, columns, expr, kind)
            for index, (column, _) in enumerate(columns, 1):
                source = source.replace(
                    f"\tcolumn {column}\n",
                    f"\tcolumn {column}\n"
                    f"\t\tlineageTag: 00000000-0000-0000-{table_index:04d}-{index:012d}\n",
                    1,
                )
            files[self._m3_table_path(table)] = source
        for relative, content in files.items():
            path = Path(root) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("utf-8"))
        ws = common.Workspace(root)
        ws.private = private
        return ws, ws.project()

    def _m3_table_path(self, table):
        return Path(f"Mini.SemanticModel/definition/tables/{table}.tmdl")

    def _m3_forms(self):
        rows, header, names = self._m3_rows, self._m3_type, self._m3_names
        columns = self.EVENTS[:2]
        amount = [self.EVENTS[1]]
        return {
            "FDataTable": (columns, "calculated", f'DATATABLE("Name", STRING, "Amount", DOUBLE, {rows})'),
            "FHashType": (columns, "m", f"#table({header}, {rows})"),
            "FHashNames": (columns, "m", f"#table({names}, {rows})"),
            "FFromRowsType": (columns, "m", f"Table.FromRows({rows}, {header})"),
            "FFromRowsNames": (columns, "m", f"Table.FromRows({rows}, {names})"),
            "FFromRecords": (columns, "m", 'Table.FromRecords({[Name = "qz-4815 widget", Amount = 7], '
                             '[Name = "qz-90210 gadget", Amount = 67890]})'),
            "FFromColumns": (columns, "m", 'Table.FromColumns({{"qz-4815 widget", "qz-90210 gadget"}, '
                             f"{{7, 67890}}}}, {names})"),
            "FFromList": (amount, "m", 'Table.FromList({7, 67890}, null, {"Amount"})'),
            "FHashAmount": (amount, "m", '#table({"Amount"}, {{7}, {67890}})'),
        }

    def _m3_assert_hidden(self, authored, published, columns):
        spellings = ("67890",)
        if len(columns) == 2:
            spellings = ("qz-4815 widget", "qz-90210 gadget", *spellings)
        for spelling in spellings:
            self.assertIn(spelling, authored)
            self.assertNotIn(spelling, published)

    def _m3_push_restored(self, root, ws, project, paths):
        originals = {}
        for path in paths:
            originals[path] = (Path(root) / path).read_bytes()
            published = Path(ws.mirror) / path
            published.write_bytes(self._m3_edit + published.read_bytes())
        with patch("pbz.mirror.desktop.processes", return_value=[]), patch(
            "pbz.mirror.validate.run_stage", return_value=[],
        ):
            result = mirror.push(ws, project)
        self.assertEqual(result["result"], "PUSHED", result)
        for path, original in originals.items():
            self.assertEqual((Path(root) / path).read_bytes(), self._m3_edit + original)

    def test_public_pull_equivalence(self):
        tables = self._m3_forms()
        expected_labels = {
            "FDataTable": ("Name", "Amount", "Name", "Amount"),
            "FHashType": ("Name", "Amount", "Name", "Amount"),
            "FHashNames": ("Name", "Amount", "Name", "Amount"),
            "FFromRowsType": ("Name", "Amount", "Name", "Amount"),
            "FFromRowsNames": ("Name", "Amount", "Name", "Amount"),
            "FFromRecords": ("Name", "Amount", "Name", "Amount"),
            "FFromColumns": ("Name", "Name", "Amount", "Amount"),
            "FFromList": ("Amount", "Amount"),
            "FHashAmount": ("Amount", "Amount"),
        }
        fragments = ("{7}", "{7,", ", 7}", ", 7,", "Amount = 7]")
        for policy in self._m3_modes:
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as private:
                ws, project = self._m3_workspace(root, private, tables, policy)
                mirror.pull(ws, project)
                paired_lines = {}
                for table, (columns, _, _) in tables.items():
                    with self.subTest(table=table):
                        path = self._m3_table_path(table)
                        authored = (Path(root) / path).read_text(encoding="utf-8")
                        published = (Path(ws.mirror) / path).read_text(encoding="utf-8")
                        line = self._m3_source_line(published)
                        if table in ("FFromList", "FHashAmount"):
                            paired_lines[table] = line
                        self._m3_assert_hidden(authored, published, columns)
                        for column, _ in columns:
                            self.assertEqual(line.count(f"«{table}[{column}]#"), 2)
                        self.assertEqual(
                            self._m3_line_tokens(line),
                            [f"«{table}[{column}]" for column in expected_labels[table]],
                        )
                        authored_line = self._m3_source_line(authored)
                        self.assertTrue(any(fragment in authored_line for fragment in fragments))
                        for fragment in fragments:
                            self.assertNotIn(fragment, line)
                self.assertEqual(
                    [token.replace("FFromList", "T") for token in self._m3_line_tokens(paired_lines["FFromList"])],
                    [token.replace("FHashAmount", "T") for token in self._m3_line_tokens(paired_lines["FHashAmount"])],
                )

    def test_public_pull_push_restores_each_file(self):
        tables = self._m3_forms()
        for policy in self._m3_modes:
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as private:
                ws, project = self._m3_workspace(root, private, tables, policy)
                mirror.pull(ws, project)
                for table, (columns, _, _) in tables.items():
                    path = self._m3_table_path(table)
                    self._m3_assert_hidden(
                        (Path(root) / path).read_text(encoding="utf-8"),
                        (Path(ws.mirror) / path).read_text(encoding="utf-8"), columns,
                    )
                self._m3_push_restored(root, ws, project, [self._m3_table_path(table) for table in tables])

    def test_public_category_text_visible(self):
        tables = {
            "FCatHash": ([("Kind", "string")], "m", '#table({"Kind"}, {{"Internal"}})'),
            "FCatList": ([("Kind", "string")], "m", 'Table.FromList({"Internal"}, null, {"Kind"})'),
        }
        policy = {"category_columns": [f"{table}[Kind]" for table in tables]}
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as private:
            ws, project = self._m3_workspace(root, private, tables, policy)
            mirror.pull(ws, project)
            for table in tables:
                path = Path(ws.mirror) / self._m3_table_path(table)
                self.assertIn("Internal", path.read_text(encoding="utf-8"))

    def test_public_uncertain_fromrows_withheld(self):
        columns = self.EVENTS[:2]
        tables = {"FUncertain": (columns, "m", f"Table.FromRows({self._m3_rows})")}
        path = self._m3_table_path("FUncertain")
        fragments = ("{7}", "{7,", ", 7}", ", 7,")
        for policy in self._m3_modes:
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as private:
                ws, project = self._m3_workspace(root, private, tables, policy)
                mirror.pull(ws, project)
                authored = (Path(root) / path).read_text(encoding="utf-8")
                published = (Path(ws.mirror) / path).read_text(encoding="utf-8")
                self._m3_assert_hidden(authored, published, columns)
                authored_line = self._m3_source_line(authored)
                published_line = self._m3_source_line(published)
                self.assertEqual(len(self._m3_tokens_in(published_line)), 1)
                self.assertTrue(any(fragment in authored_line for fragment in fragments))
                for fragment in fragments:
                    self.assertNotIn(fragment, published_line)
                self._m3_push_restored(root, ws, project, [path])

    def test_unit_equivalence_eight_forms(self):
        model = self._m3_events()
        forms = self._m3_forms()
        del forms["FHashAmount"]
        forms["FFromList"] = (
            [self.EVENTS[0]], "m",
            'Table.FromList({"qz-4815 widget", "qz-90210 gadget"}, null, {"Name"})',
        )
        for policy in self._m3_modes:
            for form, (_, kind, expr) in forms.items():
                with self.subTest(policy=policy, form=form):
                    source = self._m3_source("Events", self.EVENTS, expr, kind)
                    line = self._m3_source_line(self._m3_roundtrip(source, model, policy))
                    for spelling in ("qz-4815 widget", "qz-90210 gadget", "67890", "7"):
                        self.assertNotIn(spelling, line)

    def test_unit_category_pairing_states(self):
        model = self._m3_events()
        forms = ('#table({"Kind"}, {{"Internal"}})', 'Table.FromList({"Internal"}, null, {"Kind"})')
        for policy in self._m3_modes:
            for state in ("heuristic", "index"):
                with self.subTest(policy=policy, state=state):
                    pol = {"category_columns": ["Events[Kind]"], **policy}
                    clf = Classifier(model, None, pol)
                    if state == "index":
                        clf.index = {"columns": {"Events[Kind]": {}}}
                        clf.categories = {"Events[Kind]": ["Internal", "External"]}
                        clf.mode = "index"
                        clf.key = b"m3-test-key"
                    expected = "«Events[Kind]#" if policy and state == "heuristic" else "Internal"
                    for expr in forms:
                        source = self._m3_source("Events", self.EVENTS, expr)
                        self.assertIn(expected, self._m3_roundtrip(source, model, clf=clf))

    def test_explicit_policy_string_hidden(self):
        model = self._m3_events()
        forms = (
            '#table({"Name", "Amount"}, {{"qz-4815 widget", 7}})',
            'Table.FromRows({{"qz-4815 widget", 7}}, {"Name", "Amount"})',
            'Table.FromRecords({[Name = "qz-4815 widget", Amount = 7]})',
        )
        for policy in self._m3_modes:
            pol = {"sensitive_columns": ["Events[Name]", "Events[Amount]"], **policy}
            for expr in forms:
                with self.subTest(policy=policy, expr=expr):
                    source = self._m3_source("Events", self.EVENTS, expr)
                    self.assertNotIn("qz-4815 widget", self._m3_roundtrip(source, model, pol))

    def test_uncertain_shapes_withheld(self):
        import re

        model = self._m3_events()
        cases = (
            ("a", f"Table.FromRows({self._m3_rows})", ("qz-4815 widget", "qz-90210 gadget", "67890"), 1),
            ("b", 'Table.FromRows({{"qz-4815 widget", 7}}, SomeHeader("a@b.co"))',
             ("qz-4815 widget", "a@b.co"), 1),
            ("c", '#table({"Name", "Amount"}, {{"qz-4815 widget", 7, 99999}})', ("99999",), 1),
            ("d", 'Table.FromRecords({[Name = "qz-4815 widget", Amount = 7], 424242})', ("424242",), 1),
            ("e", '#table({"Name", "Amount"}, {{"qz-4815 widget", 12345}})', ("12345",), 1),
            ("f", 'Table.FromRows({List.Combine({{12345}, {67890}})}, {"Amount"})', ("12345", "67890"), 1),
            ("g", '#table({"Amount"}, {{1, 2} & {3}})', (), 1),
            ("h", '#table({"Amount"}, {{731}} & {{842}})', (), 1),
            ("i", 'DATATABLE("Amount", "Amount2", {{731}})', ("731",), 1),
            ("j", '#table(type table [Amount = number] extra, {{731}})', ("731",), 1),
            ("k", '#table({"Amount"}, {{731})', ("731",), 1),
            ("l", 'Table.FromList({731, 842}, MySplitter, {"Amount"})', ("731", "842"), 1),
            ("m", 'Table.FromList({731}, null, {"Amount", "Name"})', ("731",), 1),
            ("n", 'Table.FromList({731, 842}, null, {"Amount"}, 1 + 2)', ("731", "842"), 1),
            ("o", 'Table.FromList({731}, MySplitter, {"Amount"}, 1 + 2)', ("731",), 2),
            ("p", 'Table.FromList({}, null, {"Amount"}, Number.FromText("67890"))', ("67890",), 1),
            ("q", 'Table.FromList({}, null, {"Amount", "Name"}, 12345)', ("12345",), 1),
            ("r", '#table({"Amount"}, {{731}}', ("731",), 1),
            ("s", 'DATATABLE("Amount", DOUBLE, {{731}}', ("731",), 1),
            ("t", '#table({"Amount"}, {{[731}})', ("731",), 1),
            ("u", '#table({"Amount"}, {{731]}})', ("731",), 1),
            ("v", 'Table.FromRows({{731]}}, {"Amount"})', ("731",), 1),
            ("w", '#table({"Amount"}, {{({731)}}})', ("731",), 1),
            ("x", 'Table.FromRows({{731}}, , {"Amount"})', ("731",), 1),
            ("y", '#table(, {{731}})', ("731",), 1),
            ("extraValues", 'Table.FromList({731}, null, {"Amount"}, null, 5)', (), 0),
        )
        for shape, expr, spellings, payload_count in cases:
            with self.subTest(shape=shape):
                if shape == "e":
                    source = f"expression Shared = {expr}\n"
                    tm = self._m3_tm()
                    empty = tmdl.Model()
                    output = tokens.tokenize_tmdl(source, "model.tmdl", empty, Classifier(empty, None, {}), tm)
                    line = output
                else:
                    kind = "calculated" if shape in ("i", "s") else "m"
                    source = self._m3_source("Events", self.EVENTS, expr, kind)
                    output, tm = self._m3_tok(source, model)
                    line = self._m3_source_line(output)
                for spelling in spellings:
                    self.assertNotIn(spelling, output)
                self.assertEqual(line.count("«payload:"), payload_count)
                if shape == "b":
                    self.assertIn("«pii#", output)
                if shape in ("g", "h", "r", "s"):
                    self.assertNotIn("«Events[Amount]#", line)
                if shape == "n":
                    self.assertEqual(line.count("«Events[Amount]#"), 2)
                    self.assertIn('"Amount"', output)
                    self.assertTrue(re.search(
                        r'Table\.FromList\(\{«Events\[Amount\]#\d+», «Events\[Amount\]#\d+»\}, null, \{"Amount"\}, «payload:',
                        line,
                    ))
                if shape == "o":
                    self.assertTrue(re.search(
                        r'Table\.FromList\(«payload:[^»]*», MySplitter, \{"Amount"\}, «payload:', line,
                    ))
                if shape == "p":
                    self.assertIn("{}", output)
                    self.assertTrue(re.search(r'Table\.FromList\(\{\}, null, \{"Amount"\}, «payload:', line))
                if shape == "q":
                    self.assertTrue(re.search(r'Table\.FromList\(\{\}, null, \{"Amount", "Name"\}, «payload:', line))
                if shape == "r":
                    self.assertTrue(re.search(r'#table\(\{"Amount"\}, «payload:', line))
                    self.assertTrue(line.rstrip().endswith("»"))
                if shape == "s":
                    self.assertTrue(re.search(r'DATATABLE\("Amount", DOUBLE, «payload:', line))
                    self.assertTrue(line.rstrip().endswith("»"))
                if shape == "extraValues":
                    self.assertEqual(line.count("«Events[Amount]#"), 1)
                    self.assertIn(", null, 5)", line)
                self.assertEqual(tokens.detokenize_tmdl(output, tm), (source, []))

    def _m3_row_bindings(self, expr, model, expected):
        sig = self._m3_sig(expr)
        result = tokens.bind_literals(sig, "m", "Events", model)
        self.assertIsInstance(result, tuple)
        binding, _ = result
        actual = [
            (tok.value if tok.kind == "str" else tok.text, binding.get(index))
            for index, tok in enumerate(sig)
            if (tok.kind == "str" and tok.value in expected)
            or (tok.kind == "num" and tok.text in expected)
        ]
        self.assertEqual(actual, [(value, ("Events", column, "row")) for value, column in expected.items()])

    def test_attribution_correctness(self):
        import re

        model = self._m3_events()
        with self.subTest(item="a"):
            expr = '#table(type table [Name = text, Amount = number], {{Text.Repeat("x", 3), 67890}})'
            output = self._m3_roundtrip(self._m3_source("Events", self.EVENTS, expr), model)
            self.assertEqual(
                [token.split("#", 1)[0] for token in self._m3_tokens_in(output)],
                ["«Events[Name]", "«Events[Name]", "«Events[Amount]"],
            )
        with self.subTest(item="b"):
            expr = '#table({"Amount"}, {{Number.FromText("123.45", "en-US")}})'
            self._m3_row_bindings(expr, model, {"123.45": "Amount", "en-US": "Amount"})
            result = tokens.bind_literals(self._m3_sig(expr), "m", "Events", model)
            self.assertIsInstance(result, tuple)
            self.assertEqual(result[1], [])
        date_rows = '{{"qz-4815 widget", #date(2024, 1, 15), 67890}}'
        date_type = "type table [Name = text, When = date, Amount = number]"
        date_names = '{"Name", "When", "Amount"}'
        date_forms = (
            ("m", f"#table({date_type}, {date_rows})"),
            ("m", f"#table({date_names}, {date_rows})"),
            ("m", f"Table.FromRows({date_rows}, {date_type})"),
            ("m", f"Table.FromRows({date_rows}, {date_names})"),
            ("m", 'Table.FromRecords({[Name = "qz-4815 widget", When = #date(2024, 1, 15), Amount = 67890]})'),
            ("calculated", 'DATATABLE("Name", STRING, "When", DATETIME, "Amount", DOUBLE, '
             '{{"qz-4815 widget", DATE(2024, 1, 15), 67890}})'),
        )
        for kind, expr in date_forms:
            with self.subTest(item="c", expr=expr):
                output = self._m3_roundtrip(self._m3_source("Events", self.EVENTS, expr, kind), model)
                self.assertEqual(
                    self._m3_line_tokens(output),
                    ["«Events[Name]", "«Events[When]", "«Events[When]", "«Events[When]", "«Events[Amount]"],
                )
        with self.subTest(item="d"):
            expr = 'Table.FromColumns({{Number.FromText("123.45", "en-US")}, {67890}}, {"Amount", "X"})'
            self._m3_row_bindings(expr, model, {"123.45": "Amount", "en-US": "Amount", "67890": "X"})
        with self.subTest(item="e"):
            expr = '#table(type table [#"Net Amount" = number], {{12345}})'
            output = self._m3_roundtrip(self._m3_source("Events", self.EVENTS, expr), model)
            self.assertNotIn("12345", output)
            self.assertIn("«Events[Net Amount]#", output)
        with self.subTest(item="f"):
            expr = '#table(type table [R = [X = number], Amount = number], {{"a", 2}})'
            output = self._m3_roundtrip(self._m3_source("Events", self.EVENTS, expr), model)
            self.assertNotIn("«Events[X]#", output)
            self.assertIn("«Events[Amount]#", output)
        with self.subTest(item="g"):
            expr = '#table(type table [Amount = number], {{{12345}}})'
            output = self._m3_roundtrip(self._m3_source("Events", self.EVENTS, expr), model)
            self.assertIn("«Events[Amount]#", output)
        with self.subTest(item="h"):
            expr = 'Table.FromList({731}, null, {"Amount"}, 7)'
            output = self._m3_roundtrip(self._m3_source("Events", self.EVENTS, expr), model)
            self.assertEqual(output.count("«Events[Amount]#"), 2)
        with self.subTest(item="i"):
            expr = 'Table.FromList({}, null, {"Amount"}, 12345)'
            output = self._m3_roundtrip(self._m3_source("Events", self.EVENTS, expr), model)
            self.assertNotIn("12345", output)
            self.assertEqual(output.count("«Events[Amount]#"), 1)
        with self.subTest(item="j"):
            expr = 'Table.FromList({731, 842}, MySplitter, {"Amount"}, 7)'
            output = self._m3_roundtrip(self._m3_source("Events", self.EVENTS, expr), model)
            self.assertEqual(output.count("«payload:"), 1)
            self.assertEqual(output.count("«Events[Amount]#"), 1)
            line = self._m3_source_line(output)
            self.assertTrue(re.search(
                r'Table\.FromList\(«payload:[^»]*», MySplitter, \{"Amount"\}, «Events\[Amount\]#\d+»\)', line))
        with self.subTest(item="k"):
            expr = (
                'Table.FromRecords({[Name = "qz-4815 widget", Amount = 7]}, '
                '{"Name", "Amount"}, {"x", "a@b.co"})'
            )
            output = self._m3_roundtrip(self._m3_source("Events", self.EVENTS, expr), model)
            self.assertIn("«Events[Name]#", output)
            self.assertNotIn("qz-4815 widget", output)
            self.assertNotIn("a@b.co", output)
            self.assertIn("«pii#", output)
            self.assertIn('"Name"', output)
            self.assertIn('"Amount"', output)

    def test_fromrecords_optional_args_binder(self):
        model = self._m3_events()
        expr = ('Table.FromRecords({[Name = "qz-4815 widget", Amount = 7]}, '
                '{"Name", "Amount"}, {"x", "a@b.co"})')
        sig = self._m3_sig(expr)
        result = tokens.bind_literals(sig, "m", "Events", model)
        self.assertIsInstance(result, tuple)
        binding, spans = result
        self.assertEqual(spans, [])
        for index, tok in enumerate(sig):
            if tok.kind == "str" and tok.value in ("Name", "Amount"):
                self.assertEqual(binding.get(index), "skip", (index, tok))
            if tok.kind == "str" and tok.value == "qz-4815 widget":
                self.assertEqual(binding.get(index), ("Events", "Name", "row"))
            if tok.kind == "num" and tok.text == "7":
                self.assertEqual(binding.get(index), ("Events", "Amount", "row"))
            if tok.kind == "str" and tok.value == "a@b.co":
                self.assertIsNone(binding.get(index))

    def test_contextual_ownership_tokenize(self):
        model = self._m3_events()
        for expr in (self._m3_context, self._m3_context_default):
            with self.subTest(expr=expr):
                source = self._m3_source("Events", self.EVENTS, expr)
                output, _ = self._m3_tok(source, model)
                self.assertNotIn("12345", output)

    def test_bare_function_reference_binds(self):
        model = self._m3_events()
        expr = '#table({"Amount", "Kind"}, {{731, Table.FromRows}})'
        output = self._m3_roundtrip(self._m3_source("Events", self.EVENTS, expr), model)
        line = self._m3_source_line(output)
        self.assertNotIn("731", line)
        self.assertNotIn("«payload:", line)
        self.assertIn("«Events[Amount]#1»", line)
        self.assertIn("Table.FromRows", line)

    def test_contextual_ownership_binder(self):
        model = self._m3_events()
        for expr in (self._m3_context, self._m3_context_default):
            with self.subTest(expr=expr):
                self._m3_row_bindings(expr, model, {"12345": "Amount"})

    def test_nested_constructor_isolation(self):
        model = self._m3_events()
        expr = 'Table.FromRows({{#table({"Amount"}, {{12345}})}}, {"Amount"})'
        output = self._m3_roundtrip(self._m3_source("Events", self.EVENTS, expr), model)
        self.assertNotIn("12345", output)
        self.assertEqual(self._m3_source_line(output).count("«payload:"), 1)

    def test_nested_containment_comment(self):
        model = self._m3_events()
        source = self._m3_source("Events", self.EVENTS, self._m3_comment)
        output, tm = self._m3_tok(source, model)
        self.assertEqual(output.count("«payload:"), 1)
        self.assertNotIn("a@b.co", output)
        self.assertNotIn("qz-4815 widget", output)
        self.assertNotIn("424242", output)
        records = [record for record in tm.data["tokens"].values() if "a@b.co" in str(record)]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "raw")
        self.assertIn("424242", str(records[0]))
        self.assertEqual(tokens.detokenize_tmdl(output, tm), (source, []))

    def test_nested_containment_binder_spans(self):
        model = self._m3_events()
        result = tokens.bind_literals(self._m3_sig(self._m3_comment), "m", "Events", model)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result[1]), 1)

    def test_spelling_variant_limit_pin(self):
        model = self._m3_events()
        forms = (
            '#table({"Amount"}, {{1.5}, {1.50}})',
            'Table.FromRecords({[Amount = 1.5], [Amount = 1.50]})',
        )
        for expr in forms:
            with self.subTest(expr=expr):
                source = self._m3_source("Events", self.EVENTS, expr)
                output, tm = self._m3_tok(source, model)
                sites = self._m3_tokens_in(self._m3_source_line(output))
                self.assertEqual(len(sites), 2)
                self.assertEqual(sites[0], sites[1])
                restored, unknown = tokens.detokenize_tmdl(output, tm)
                self.assertNotEqual(restored, source)
                self.assertEqual(restored, source.replace("1.50", "1.5"))
                self.assertEqual(unknown, [])

    def test_controls_unchanged(self):
        model = self._m3_events()
        with self.subTest(control="ordinary constant"):
            source = self._m3_source("Events", self.EVENTS, "let Threshold = 12345 in Threshold")
            output, _ = self._m3_tok(source, model)
            self.assertIn("12345", output)
        with self.subTest(control="empty typed table"):
            columns = [*self.EVENTS, ("Note", "string")]
            root = self.enterContext(tempfile.TemporaryDirectory())
            note_model = self._m3_model(root, {"Events": columns})
            source = self._m3_source("Events", columns, '#table(type table [Note = text], {})')
            output, _ = self._m3_tok(source, note_model)
            self.assertEqual(output, source)
        with self.subTest(control="base64"):
            base64 = "QUJD" * 60
            expr = f'Table.FromRows(Json.Document(Binary.Decompress(Binary.FromText("{base64}"))))'
            source = self._m3_source("Events", self.EVENTS, expr)
            output, _ = self._m3_tok(source, model)
            self.assertEqual(output.count("«payload:"), 1)
            self.assertIn("«payload:Events#", output)
            self.assertNotIn(base64, output)
            self.assertNotIn("«Events[", output)
        with self.subTest(control="category"):
            source = self._m3_source("Events", self.EVENTS, '#table({"Kind"}, {{"Internal"}})')
            output, _ = self._m3_tok(source, model, {"category_columns": ["Events[Kind]"]})
            self.assertIn("Internal", output)
        with self.subTest(control="rename pairs"):
            expr = 'Table.RenameColumns(#table({"Amount"}, {{7}}), {{"Amount", "Total"}})'
            source = self._m3_source("Events", self.EVENTS, expr)
            output, _ = self._m3_tok(source, model)
            self.assertIn("Total", output)
        with self.subTest(control="empty rows"):
            source = self._m3_source("Events", self.EVENTS, 'Table.FromRows({}, {"Amount"})')
            output, _ = self._m3_tok(source, model)
            self.assertEqual(output, source)
        with self.subTest(control="literal-free nested"):
            source = self._m3_source(
                "Events", self.EVENTS, 'Table.FromRows({{#table(type table [Amount = number], {})}}, {"Amount"})',
            )
            output, _ = self._m3_tok(source, model)
            self.assertEqual(output, source)
        with self.subTest(control="over-width null"):
            source = self._m3_source("Events", self.EVENTS, '#table({"Amount"}, {{731, null}})')
            output = self._m3_roundtrip(source, model)
            self.assertNotIn("731", output)
            self.assertNotIn("«payload:", output)
        with self.subTest(control="unmatched new constructor"):
            source = self._m3_source("Events", self.EVENTS, 'Table.FromRows({{12345}}, {"Amount"}')
            output, _ = self._m3_tok(source, model)
            self.assertIn("12345", output)

    def test_fixture_guard_zero_opaque_spans(self):
        root = Path(__file__).resolve().parent / "fixture" / "Canary.SemanticModel"
        model = tmdl.load_model(str(root))
        self.assertTrue(model.tables)
        for path in sorted(root.rglob("*.tmdl")):
            relpath = path.relative_to(root).as_posix()
            text = path.read_text(encoding="utf-8")
            for region in tmdl.parse(relpath, text).regions:
                if region.kind in ("dax", "m"):
                    with self.subTest(path=relpath, start=region.start):
                        segment = text[region.start:region.end]
                        lex = lexers.lex_dax if region.kind == "dax" else lexers.lex_m
                        sig = lexers.significant(lex(segment, region.start))
                        result = tokens.bind_literals(sig, region.kind, tokens._table_of(region.node), model)
                        self.assertIsInstance(result, tuple)
                        self.assertEqual(result[1], [])


if __name__ == "__main__":
    unittest.main()
