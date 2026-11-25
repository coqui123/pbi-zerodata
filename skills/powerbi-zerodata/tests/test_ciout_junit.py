"""Tests for PR 5: JUnit fingerprinting, versioning, legacy detection, and boundary conditions."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

from pbz import ciout, common  # noqa: E402


class CioutJUnitFingerprintingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = common.Workspace(self.tmp.name)
        os.makedirs(os.path.join(self.ws.root, ".pbz", "baselines"), exist_ok=True)

    def _sample_findings(self, count: int) -> list[dict]:
        return [
            {
                "rule": f"RULE_{i:04d}",
                "code": f"C_{i:04d}",
                "object": f"Table[Measure_{i:04d}]",
                "where": f"tables/Table_{i:04d}.tmdl",
                "severity": 2,
            }
            for i in range(count)
        ]

    def test_boundary_matrix_counts(self):
        """Mandatory boundary counts: 0, 1, 499, 500, 501, 502, 700."""
        cases = [
            (0, 1, 0, False, 0, 0),
            (1, 1, 1, False, 1, 0),
            (499, 499, 499, False, 499, 0),
            (500, 500, 500, False, 500, 0),
            (501, 501, 501, True, 500, 1),
            (502, 501, 501, True, 500, 2),
            (700, 501, 501, True, 500, 200),
        ]
        for count, exp_tests, exp_fails, exp_trunc, exp_emitted, exp_omitted in cases:
            with self.subTest(count=count):
                report_path = os.path.join(self.ws.root, f"report_{count}.xml")
                findings = self._sample_findings(count)
                ciout._write_report("junit", report_path, findings, self.ws.root)
                tree = ET.parse(report_path)
                root = tree.getroot()
                self.assertEqual(root.tag, "testsuite")
                self.assertEqual(root.get("tests"), str(exp_tests))
                self.assertEqual(root.get("failures"), str(exp_fails))

                testcases = root.findall("testcase")
                self.assertEqual(len(testcases), exp_tests)
                failures = root.findall("testcase/failure")
                self.assertEqual(len(failures), exp_fails)

                # Suite properties
                suite_props = {p.get("name"): p.get("value") for p in root.findall("properties/property")}
                self.assertEqual(suite_props.get("pbz.fingerprint_version"), "1")
                self.assertEqual(suite_props.get("pbz.truncated"), "true" if exp_trunc else "false")
                self.assertEqual(suite_props.get("pbz.total_findings"), str(count))
                self.assertEqual(suite_props.get("pbz.emitted_findings"), str(exp_emitted))
                self.assertEqual(suite_props.get("pbz.omitted_findings"), str(exp_omitted))

                # Sentinel check
                sentinels = [tc for tc in testcases if tc.get("name") == "pbz.truncated"]
                if exp_trunc:
                    self.assertEqual(len(sentinels), 1)
                    fail = sentinels[0].find("failure")
                    self.assertIsNotNone(fail)
                    self.assertEqual(fail.get("type"), "TRUNCATED")
                else:
                    self.assertEqual(len(sentinels), 0)

    def test_fingerprint_properties_in_junit(self):
        findings = self._sample_findings(5)
        report_path = os.path.join(self.ws.root, "report.xml")
        ciout._write_report("junit", report_path, findings, self.ws.root)
        tree = ET.parse(report_path)
        root = tree.getroot()

        pattern = re.compile(r"^v1:[0-9a-f]{64}\Z")
        for tc in root.findall("testcase"):
            fp_prop = tc.find("properties/property[@name='pbz.fingerprint']")
            self.assertIsNotNone(fp_prop, f"testcase {tc.get('name')} missing pbz.fingerprint")
            val = fp_prop.get("value")
            self.assertIsNotNone(pattern.fullmatch(val), f"invalid fingerprint format: {val}")

    def test_collision_resistance_beyond_300_and_pipe_injection(self):
        """Fixes 300-char truncation collisions and pipe delimiter collisions."""
        long_prefix = "A" * 320
        f1 = {"rule": "R", "code": "C", "object": long_prefix + "1", "where": "w"}
        f2 = {"rule": "R", "code": "C", "object": long_prefix + "2", "where": "w"}
        self.assertNotEqual(ciout._fp(f1), ciout._fp(f2))

        # Pipe injection
        f3 = {"rule": "a|b", "code": "c", "object": "o", "where": "w"}
        f4 = {"rule": "a", "code": "b|c", "object": "o", "where": "w"}
        self.assertNotEqual(ciout._fp(f3), ciout._fp(f4))

    def test_file_fallback_when_where_missing(self):
        """A missing where uses file. where wins when both are set."""
        left = {"rule": "R", "code": "C", "object": "O", "file": "tables/" + ("A" * 320) + ".tmdl"}
        right = {"rule": "R", "code": "C", "object": "O", "file": "tables/" + ("A" * 320) + "Z.tmdl"}
        self.assertNotEqual(ciout._fp(left), ciout._fp(right))
        via_where = {"rule": "R", "code": "C", "object": "O", "where": left["file"]}
        self.assertEqual(ciout._fp(left), ciout._fp(via_where))
        prefers_where = {"rule": "R", "code": "C", "object": "O", "where": left["file"], "file": "other.tmdl"}
        self.assertEqual(ciout._fp(prefers_where), ciout._fp(via_where))

    def test_golden_vector(self):
        """Pin canonical golden vector so fingerprint computation cannot drift."""
        finding = {
            "rule": "R",
            "code": "C",
            "object": "T[M]",
            "where": "tables/T.tmdl",
        }
        expected = "v1:d34ccbce7de762925f8ed2b412b0067e33b438e6897686a3eabafc3123806f8c"
        self.assertEqual(ciout._fp(finding), expected)

    def test_xml_safety_round_trip(self):
        """XML special characters, valid supplementary characters, and stripped invalid XML chars."""
        raw_finding = {
            "rule": "RULE <&> \"quote\" 'single' \n\t\r 🚀 \x00\x0b\x1f\ud800\ufffe\uffff",
            "code": "C_XML",
            "object": "Table[Col < 5 & > 1 \"x\" 'y' \x00\x0b\x1f\ud800\ufffe\uffff]",
            "where": "tables/T.tmdl",
            "severity": 2,
        }
        report_path = os.path.join(self.ws.root, "xml_safe_report.xml")
        ciout._write_report("junit", report_path, [raw_finding], self.ws.root)

        # File must parse cleanly with standard XML parser
        tree = ET.parse(report_path)
        root = tree.getroot()
        case = root.find("testcase")
        self.assertIsNotNone(case)

        case_name = case.get("name")
        case_class = case.get("classname")
        failure_msg = case.find("failure").get("message")

        # Invalid XML chars were stripped
        for bad_ch in ("\x00", "\x0b", "\x1f", "\ud800", "\ufffe", "\uffff"):
            self.assertNotIn(bad_ch, case_name)
            self.assertNotIn(bad_ch, case_class)
            self.assertNotIn(bad_ch, failure_msg)

        # Valid characters and non-BMP survived
        self.assertIn("🚀", case_name)
        self.assertIn("RULE", case_name)

        # Fingerprint in XML must match raw un-sanitized finding, and differ from sanitized finding
        xml_fp = case.find("properties/property[@name='pbz.fingerprint']").get("value")
        self.assertEqual(xml_fp, ciout._fp_v1(raw_finding))
        sanitized_finding = {**raw_finding, "rule": case_name, "object": case_class}
        self.assertNotEqual(xml_fp, ciout._fp_v1(sanitized_finding))

        # Empty-collapsing rule falls back to "pbz"
        bad_rule_finding = {"rule": "\x00\x0b", "code": "", "object": "Obj", "where": "w"}
        bad_report_path = os.path.join(self.ws.root, "bad_rule_report.xml")
        ciout._write_report("junit", bad_report_path, [bad_rule_finding], self.ws.root)
        bad_tree = ET.parse(bad_report_path)
        self.assertEqual(bad_tree.getroot().find("testcase").get("name"), "pbz")

    def test_baseline_size_boundary_and_preservation(self):
        """Save refuses baselines > 1,000,000 bytes and preserves existing baseline on refusal."""
        # Baseline with exactly 14,084 fingerprints serializes to 1,000,000 bytes
        doc_14084 = [{"rule": f"R_{i:05d}", "code": "C", "object": f"O_{i:05d}", "where": "w"} for i in range(14084)]
        ciout.save_baseline(self.ws, "boundary", doc_14084)
        boundary_path = ciout._baseline_path(self.ws, "boundary")
        self.assertEqual(os.path.getsize(boundary_path), ciout.MAX_BASELINE_BYTES)
        loaded = ciout.load_baseline(self.ws, "boundary")
        self.assertEqual(len(loaded), 14084)

        # Baseline with 14,085 fingerprints serializes to 1,000,071 bytes (> 1,000,000)
        doc_14085 = [{"rule": f"R_{i:05d}", "code": "C", "object": f"O_{i:05d}", "where": "w"} for i in range(14085)]
        with self.assertRaises(common.PbzError) as cm:
            ciout.save_baseline(self.ws, "boundary", doc_14085)
        self.assertEqual(cm.exception.code, "REFUSED")

        # Prior existing baseline remains intact and loadable
        reloaded = ciout.load_baseline(self.ws, "boundary")
        self.assertEqual(len(reloaded), 14084)

    def test_baseline_versioning_and_legacy_detection(self):
        findings = self._sample_findings(3)
        saved = ciout.save_baseline(self.ws, "current", findings)
        self.assertEqual(saved["baseline"], "current")
        self.assertEqual(saved["fingerprints"], 3)

        path = ciout._baseline_path(self.ws, "current")
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(doc.get("fingerprint_version"), 1)
        self.assertEqual(len(doc.get("ids", [])), 3)
        self.assertTrue(all(id_.startswith("v1:") for id_ in doc["ids"]))

        # Load current versioned baseline
        loaded_ids = ciout.load_baseline(self.ws, "current")
        self.assertEqual(len(loaded_ids), 3)

        # Legacy baseline (unversioned {"ids": [...]})
        legacy_path = ciout._baseline_path(self.ws, "legacy")
        legacy_fp = ciout._fp_v0(findings[0])
        with open(legacy_path, "w", encoding="utf-8") as f:
            json.dump({"ids": [legacy_fp]}, f)

        # attach() with legacy baseline flags baseline_legacy
        out = {}
        attached = ciout.attach(self.ws, out, findings, fail_on="new", since="legacy")
        self.assertTrue(attached.get("baseline_legacy"))
        # Since finding 0 matched legacy, gate_count should be 2 new findings
        self.assertEqual(attached.get("gate_count"), 2)

    def test_gating_behavior(self):
        """Test gating round-trip, truncation independence, and missing since requirement."""
        findings = self._sample_findings(505)

        # Save baseline with first 500 findings
        ciout.save_baseline(self.ws, "base500", findings[:500])

        # Attach with identical 500 findings -> PASS
        out_pass = {}
        ciout.attach(self.ws, out_pass, findings[:500], fail_on="new", since="base500")
        self.assertEqual(out_pass.get("gate"), "PASS")
        self.assertEqual(out_pass.get("gate_count"), 0)

        # Attach with all 505 findings -> FAIL with 5 new findings (past 500 boundary)
        out_fail = {}
        ciout.attach(self.ws, out_fail, findings, fail_on="new", since="base500")
        self.assertEqual(out_fail.get("gate"), "FAIL")
        self.assertEqual(out_fail.get("gate_count"), 5)

        # --fail-on new without --since raises BAD_REQUEST
        with self.assertRaises(common.PbzError) as cm:
            ciout.attach(self.ws, {}, findings, fail_on="new")
        self.assertEqual(cm.exception.code, "BAD_REQUEST")

    def test_fail_closed_baseline_validation(self):
        """Invalid baseline files must raise REFUSED."""
        valid_hash = "a" * 64
        bad_cases = [
            ("bad_version_str", {"fingerprint_version": "1", "ids": []}),
            ("bad_version_bool", {"fingerprint_version": True, "ids": []}),
            ("bad_version_unknown", {"fingerprint_version": 99, "ids": []}),
            ("bad_ids_type", {"fingerprint_version": 1, "ids": "not_a_list"}),
            ("bad_ids_entry", {"fingerprint_version": 1, "ids": [123]}),
            ("bad_ids_token", {"fingerprint_version": 1, "ids": ["c0-badtoken"]}),
            ("bad_ids_prefix_only", {"fingerprint_version": 1, "ids": ["v1:"]}),
            ("bad_ids_not_a_hash", {"fingerprint_version": 1, "ids": ["v1:not-a-hash"]}),
            ("bad_ids_short_hex", {"fingerprint_version": 1, "ids": ["v1:" + "a" * 63]}),
            ("bad_ids_long_hex", {"fingerprint_version": 1, "ids": ["v1:" + "a" * 65]}),
            ("bad_ids_uppercase_hex", {"fingerprint_version": 1, "ids": ["v1:" + "A" * 64]}),
            ("bad_ids_non_hex", {"fingerprint_version": 1, "ids": ["v1:" + "g" * 64]}),
            ("bad_ids_trailing_space", {"fingerprint_version": 1, "ids": ["v1:" + valid_hash + " "]}),
            ("bad_ids_trailing_newline", {"fingerprint_version": 1, "ids": ["v1:" + valid_hash + "\n"]}),
            ("bad_ids_leading_newline", {"fingerprint_version": 1, "ids": ["\nv1:" + valid_hash]}),
            ("bad_list_doc", ["not_a_dict"]),
            ("bad_null_ids", {"ids": None}),
            ("bad_empty_doc", {}),
        ]
        for name, doc in bad_cases:
            path = ciout._baseline_path(self.ws, name)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc, f)
            with self.subTest(name=name):
                with self.assertRaises(common.PbzError) as cm:
                    ciout.load_baseline(self.ws, name)
                self.assertEqual(cm.exception.code, "REFUSED")

    def test_zero_data_canaries(self):
        """Sensitive non-identity fields never enter XML or alter fingerprints."""
        canary = "CONFIDENTIAL_PAYROLL_2026"
        base_finding = {
            "rule": "RULE_1",
            "code": "C_1",
            "object": "Table[Col]",
            "where": "tables/T.tmdl",
        }
        enriched_finding = {
            **base_finding,
            "value": canary,
            "expression": f"EVALUATE '{canary}'",
            "query": canary,
            "sample": canary,
        }
        # Fingerprints must be completely identical regardless of non-identity data fields
        self.assertEqual(ciout._fp(base_finding), ciout._fp(enriched_finding))

        # Canary text must not appear anywhere in JUnit report XML
        report_path = os.path.join(self.ws.root, "canary_report.xml")
        ciout._write_report("junit", report_path, [enriched_finding], self.ws.root)
        with open(report_path, "r", encoding="utf-8") as f:
            xml_text = f.read()
        self.assertNotIn(canary, xml_text)


if __name__ == "__main__":
    unittest.main()
