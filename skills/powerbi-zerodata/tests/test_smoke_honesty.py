"""Offline honesty checks for smoke coverage and multi-hit culture moves."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))

from pbz import common, governance, pbir, smoke, tmdl  # noqa: E402


def _parse(obj):
    return pbir.parse_json(json.dumps(obj))


def _column(table, name):
    return {"Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}}


def _agg(table, name, function):
    return {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}},
                            "Function": function}}


class SmokeHonestyTests(unittest.TestCase):
    def setUp(self):
        self.model = tmdl.Model()
        self.tm = type("TM", (), {"get": lambda self, _tok: None})()

    def test_unknown_aggregation_is_unsupported(self):
        visual = {"visual": {"visualType": "card", "query": {"queryState": {"Values": {"projections": [
            {"field": _agg("Sales", "Amount", 99)}]}}}}}
        dax, note = smoke.build_query(_parse(visual), _parse({}), self.model, self.tm)
        self.assertIsNone(dax)
        self.assertEqual(note, "UNSUPPORTED")
        self.assertNotIn("COUNTA", dax or "")

    def test_empty_in_is_partial(self):
        visual = {"visual": {"visualType": "tableEx", "query": {"queryState": {"Values": {"projections": [
            {"field": _column("Sales", "Region")}]}}}},
            "filterConfig": {"filters": [{"filter": {"From": [{"Name": "s", "Entity": "Sales", "Type": 0}],
                "Where": [{"Condition": {"In": {
                    "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "s"}}, "Property": "Region"}}],
                    "Values": []}}}]}}]}}
        dax, coverage = smoke.build_query(_parse(visual), _parse({}), self.model, self.tm)
        self.assertEqual(coverage, "PARTIAL")
        self.assertIsNotNone(dax)
        self.assertNotIn("TREATAS", dax)

    def test_datetime_time_is_partial(self):
        visual = {"visual": {"visualType": "tableEx", "query": {"queryState": {"Values": {"projections": [
            {"field": _column("Sales", "When")}]}}}},
            "filterConfig": {"filters": [{"filter": {"From": [{"Name": "s", "Entity": "Sales", "Type": 0}],
                "Where": [{"Condition": {"Comparison": {
                    "ComparisonKind": 1,
                    "Left": {"Column": {"Expression": {"SourceRef": {"Source": "s"}}, "Property": "When"}},
                    "Right": {"Literal": {"Value": "datetime'2020-01-02T15:04:05'"}}}}}]}}]}}
        dax, coverage = smoke.build_query(_parse(visual), _parse({}), self.model, self.tm)
        self.assertEqual(coverage, "PARTIAL")
        self.assertIn("DATE(2020, 1, 2)", dax)
        self.assertNotIn("15", dax)

    def test_protocol_error_on_malformed_engine_status(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        ws = common.Workspace(str(root))
        ws.private = str(root / "private")
        (root / "Fix.pbip").write_text("{}")
        project = common.Project(ws, "Fix")
        page = Path(project.mirror_report) / "definition" / "pages" / "p1"
        (page / "visuals" / "v1").mkdir(parents=True)
        (page / "page.json").write_text(json.dumps({"name": "p1", "displayName": "Synthetic"}))
        visual = {"visual": {"visualType": "card", "query": {"queryState": {"Values": {"projections": [
            {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Total"}}}]}}}}}
        (page / "visuals" / "v1" / "visual.json").write_text(json.dumps(visual))
        os.makedirs(os.path.join(project.mirror_model, "definition"), exist_ok=True)
        with patch("pbz.checks.spend", return_value=True), \
             patch("pbz.desktop.require_port", return_value=1), \
             patch("pbz.smoke.engine_call", return_value={"ok": True, "visuals": [{"id": "p1/v1", "status": "SUCCESS"}]}):
            out = smoke.run(ws, project)
        row = out["visuals"][0]
        self.assertEqual(row["status"], "PROTOCOL_ERROR")
        self.assertEqual(row["coverage"], "FULL")
        self.assertEqual(out["summary"]["not_executed"].get("PROTOCOL_ERROR"), 1)
        self.assertEqual(out["summary"]["executed"], {})
        self.assertEqual(out["outcome"]["status"], "FAILED")
        self.assertNotIn("SUCCESS", json.dumps(out))

    def _smoke_fixture(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        ws = common.Workspace(str(root))
        ws.private = str(root / "private")
        (root / "Fix.pbip").write_text("{}")
        project = common.Project(ws, "Fix")
        page = Path(project.mirror_report) / "definition" / "pages" / "p1"
        (page / "visuals" / "v1").mkdir(parents=True)
        (page / "page.json").write_text(json.dumps({"name": "p1", "displayName": "Synthetic"}))
        visual = {"visual": {"visualType": "card", "query": {"queryState": {"Values": {"projections": [
            {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Total"}}}]}}}}}
        (page / "visuals" / "v1" / "visual.json").write_text(json.dumps(visual))
        os.makedirs(os.path.join(project.mirror_model, "definition"), exist_ok=True)
        return ws, project

    def test_missing_engine_row_is_protocol_error(self):
        ws, project = self._smoke_fixture()
        with patch("pbz.checks.spend", return_value=True), \
             patch("pbz.desktop.require_port", return_value=1), \
             patch("pbz.smoke.engine_call", return_value={"ok": True, "visuals": []}):
            out = smoke.run(ws, project)
        self.assertEqual(out["visuals"][0]["status"], "PROTOCOL_ERROR")

    def test_list_status_is_protocol_error(self):
        ws, project = self._smoke_fixture()
        with patch("pbz.checks.spend", return_value=True), \
             patch("pbz.desktop.require_port", return_value=1), \
             patch("pbz.smoke.engine_call",
                   return_value={"ok": True, "visuals": [{"id": "p1/v1", "status": ["OK"]}]}):
            out = smoke.run(ws, project)
        self.assertEqual(out["visuals"][0]["status"], "PROTOCOL_ERROR")

    def test_top_level_failure_class_is_closed(self):
        ws, project = self._smoke_fixture()
        with patch("pbz.checks.spend", return_value=True), \
             patch("pbz.desktop.require_port", return_value=1), \
             patch("pbz.smoke.engine_call",
                   return_value={"ok": False, "class": "SECRET PAYLOAD"}):
            with self.assertRaises(common.PbzError) as caught:
                smoke.run(ws, project)
        self.assertEqual(caught.exception.code, "PROTOCOL_ERROR")
        self.assertNotIn("SECRET", caught.exception.detail)

    def test_duplicate_visual_id_is_protocol_error(self):
        ws, project = self._smoke_fixture()
        with patch("pbz.checks.spend", return_value=True), \
             patch("pbz.desktop.require_port", return_value=1), \
             patch("pbz.smoke.engine_call", return_value={"ok": True, "visuals": [
                 {"id": "p1/v1", "status": "OK"}, {"id": "p1/v1", "status": "EMPTY"}]}):
            out = smoke.run(ws, project)
        self.assertEqual(out["visuals"][0]["status"], "PROTOCOL_ERROR")

    def test_partial_ok_evidence_is_incomplete(self):
        from pbz import evidence
        out = {"verb": "smoke", "summary": {"executed": {"OK": 1}, "not_executed": {}},
               "visuals": [{"page": "A", "status": "OK", "coverage": "PARTIAL"}]}
        stage, summary = evidence.summarize("smoke", None, out)
        self.assertEqual(stage, "EXECUTED")
        self.assertEqual(summary["outcome"], "INCOMPLETE")
        self.assertTrue(summary["partial_filters"])


class CultureMoveTests(unittest.TestCase):
    def test_moves_every_measure_hit(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mdef = os.path.join(tmp.name, "definition")
        os.makedirs(os.path.join(mdef, "cultures"))
        text = (
            "cultureInfo en-US\n\n"
            "\ttranslations\n\n"
            "\t\tmodel Model\n\n"
            "\t\t\ttable Src\n\n"
            "\t\t\t\tmeasure Amount\n"
            "\t\t\t\t\tcaption: One\n\n"
            "\t\t\t\tmeasure Amount\n"
            "\t\t\t\t\tcaption: Two\n\n"
            "\t\t\ttable Keep\n\n"
            "\t\t\t\tmeasure Other\n"
            "\t\t\t\t\tcaption: Stay\n"
        )
        path = os.path.join(mdef, "cultures", "en-US.tmdl")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        changed = governance.culture_edits_move(mdef, "Src", "Dst", "Amount")
        self.assertEqual(changed, ["cultures/en-US.tmdl"])
        doc = tmdl.parse("cultures/en-US.tmdl", common.read_text(path)[0])
        amounts = [n for n in doc.nodes if n.kind == "measure" and n.name == "Amount"]
        self.assertEqual(len(amounts), 2)
        self.assertTrue(all(n.parent is not None and n.parent.name == "Dst" for n in amounts))
        self.assertEqual(sorted(n.props.get("caption") for n in amounts), ["One", "Two"])
        other = next(n for n in doc.nodes if n.kind == "measure" and n.name == "Other")
        self.assertEqual(other.parent.name, "Keep")
        self.assertFalse(any(n.kind == "measure" and n.parent is not None and n.parent.name == "Src" for n in doc.nodes))


if __name__ == "__main__":
    unittest.main()
