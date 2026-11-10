"""Offline checks for plan-mode helpers on the synthetic parity project.

Run: python tests/test_plan_pure.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import (  # noqa: E402
    calendars, ciout, common, daxcatalog, expand, findreplace, lexers, mirror, mirror_txn,
    perf, rewrite, ruleset, tmdl, tokens,
)
from pbz.common import PbzError  # noqa: E402

LEAK_EXPR = "SUM(Sales[Amount])"
LEAK_TOKEN = "«lit#1»"


def _tree_bytes(root: str) -> dict[str, bytes]:
    found = {}
    for dirpath, dirnames, names in os.walk(root):
        dirnames.sort()
        for name in sorted(names):
            path = os.path.join(dirpath, name)
            with open(path, "rb") as handle:
                found[os.path.relpath(path, root)] = handle.read()
    return found


def _assert_same_bytes(case: unittest.TestCase, before: dict[str, bytes], after: dict[str, bytes]) -> None:
    case.assertEqual(sorted(before), sorted(after))
    for rel, data in before.items():
        case.assertTrue(after[rel] == data, rel)


class PlanPureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pbzplan-")
        self.addCleanup(self.tmp.cleanup)
        private = os.path.join(self.tmp.name, "private")
        os.environ["PBZ_PRIVATE_ROOT"] = private
        self.addCleanup(lambda: os.environ.pop("PBZ_PRIVATE_ROOT", None))
        self.root = os.path.join(self.tmp.name, "ws")
        test_parity.build(self.root)
        self.ws = common.Workspace(self.root)
        self.ws.private = private
        self.project = self.ws.project()
        mirror.pull(self.ws, self.project)

    def _model(self):
        return tmdl.load_model(os.path.join(self.project.mirror_model, "definition"))

    def _mirror_text(self) -> str:
        parts = []
        for data in _tree_bytes(self.ws.mirror).values():
            parts.append(data.decode("utf-8", "replace"))
        return "\n".join(parts)

    def _insert_measure(self, name: str, expression: str, tag: str) -> None:
        path = os.path.join(self.project.mirror_model, "definition", "tables", "Sales.tmdl")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        block = f"\tmeasure '{name}' = {expression}\n\t\tlineageTag: {tag}\n\n"
        marker = "\tpartition Sales = m\n"
        self.assertIn(marker, text)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text.replace(marker, block + marker, 1))

    def _closed_desktop(self):
        return patch("pbz.desktop.require_port", side_effect=PbzError("ENGINE_UNAVAILABLE", "closed"))

    def test_token_patterns_are_refused_and_dot_is_not(self):
        token = "«lit#1»"
        self.assertIsNotNone(tokens.TOKEN_RE.fullmatch(token))
        before = _tree_bytes(self.ws.mirror)
        for pattern in ("«", "»", "pre«post", token):
            with self.assertRaises(PbzError) as caught:
                findreplace.find(self.ws, self.project, pattern)
            self.assertEqual(caught.exception.code, "REFUSED", pattern)
            with self.assertRaises(PbzError) as caught:
                findreplace.replace(self.ws, self.project, pattern, "X", apply=True)
            self.assertEqual(caught.exception.code, "REFUSED", pattern)
        _assert_same_bytes(self, before, _tree_bytes(self.ws.mirror))
        found = findreplace.find(self.ws, self.project, ".")
        self.assertEqual(found["verb"], "find")
        planned = findreplace.replace(self.ws, self.project, ".", "Z", apply=False)
        self.assertEqual(planned["verb"], "replace")
        self.assertTrue(planned["dry_run"])
        _assert_same_bytes(self, before, _tree_bytes(self.ws.mirror))

    def test_name_match_does_not_also_text_replace(self):
        out = findreplace.replace(self.ws, self.project, "Sales", "SalesData", kind="table", apply=True)
        self.assertEqual(out["result"], "CHANGED")
        self.assertEqual(out["text_hits"], 0)
        self.assertTrue(any(item["from"] == "Sales" and item["to"] == "SalesData" for item in out["renames"]))
        model = self._model()
        self.assertIsNotNone(model.table("SalesData"))
        self.assertIsNone(model.table("Sales"))
        self.assertIsNone(model.table("SalesDataData"))
        self.assertIsNotNone(model.measure("Total Sales"))
        self.assertIsNone(model.measure("Total SalesData"))
        self.assertFalse("SalesDataData" in self._mirror_text(), "renamed Sales was text-replaced")

    def test_rename_match_rolls_back_when_rename_raises(self):
        before = _tree_bytes(self.ws.mirror)
        seen = {}

        def boom(ws, project, kind, old, new, table=None):
            seen["call"] = (kind, old, new, table)
            path = os.path.join(self.project.mirror_model, "definition", "tables", "Sales.tmdl")
            with open(path, "ab") as handle:
                handle.write(b"\nMUTATION\n")
            raise RuntimeError("rename failed")

        with patch("pbz.findreplace.rename_mod.rename", side_effect=boom):
            with self.assertRaises(RuntimeError):
                findreplace.rename_match(self.ws, self.project, "table", "Sales", "SalesData")
        self.assertEqual(seen["call"][0], "table")
        self.assertEqual(seen["call"][1], "Sales")
        self.assertEqual(seen["call"][2], "SalesData")
        _assert_same_bytes(self, before, _tree_bytes(self.ws.mirror))

    def test_ruleset_refuses_statistics_and_evaluates_supported(self):
        for expr in ("RowCount > 0", "Vertipaq", "Cardinality = 1", "DistinctCount", "Min", "Max"):
            status, _reason = ruleset.classify(expr)
            self.assertEqual(status, "REFUSED", expr)
            with self.assertRaises(ruleset.Refused):
                ruleset.parse(expr)
        for expr in ('Name = "X"', "IsHidden = true", 'Regex.IsMatch(Name, "^Total")'):
            status, reason = ruleset.classify(expr)
            self.assertEqual((status, reason), ("SUPPORTED", ""), expr)
            ruleset.parse(expr)
        document = ruleset.load_document(json.dumps({"Rules": [
            {"ID": "NAME_EQ", "Name": "named", "Scope": "Measure", "Expression": 'Name = "X"'},
            {"ID": "HIDDEN", "Name": "hidden", "Scope": "Column", "Expression": "IsHidden = true"},
            {"ID": "NAME_RX", "Name": "rx", "Scope": "Measure", "Expression": 'Regex.IsMatch(Name, "^Total")'},
        ]}))
        self.assertTrue(all(rule["status"] == "SUPPORTED" for rule in document["rules"]))
        result = ruleset.evaluate(self._model(), document)
        self.assertEqual(result["executed"], 3)
        hidden = {item["object"] for item in result["findings"] if item["rule"] == "HIDDEN"}
        self.assertIn("SortOrder", hidden)
        matched = {item["object"] for item in result["findings"] if item["rule"] == "NAME_RX"}
        self.assertEqual(matched, {"Total Sales"})
        fuzzed = ruleset.fuzz()
        self.assertEqual(fuzzed["fuzz_errors"], 0)
        self.assertGreaterEqual(fuzzed["cases"], 1)

    def test_rewrite_unused_var_removes_var_and_return(self):
        with self._closed_desktop():
            out = rewrite.run(self.ws, self.project, "unused-var", "[Top Unused]")
        self.assertEqual(out["result"], "CHANGED")
        expr = self._model().measure("Top Unused").expression
        sig = lexers.significant(lexers.lex_dax(expr))
        self.assertTrue(any(tok.kind == "bracket" and tok.value == "Base Unused" for tok in sig))
        self.assertFalse(any(tok.text == "unusedVar" for tok in sig))
        self.assertFalse(any(tok.text.upper() in ("VAR", "RETURN") for tok in sig))

    def test_rewrite_divide_needs_accept_semantic(self):
        self._insert_measure("Slash", "a/b", "10000000-0000-0000-0000-000000000091")
        with self._closed_desktop():
            with self.assertRaises(PbzError) as caught:
                rewrite.run(self.ws, self.project, "divide", "[Slash]")
            self.assertEqual(caught.exception.code, "REFUSED")
            unchanged = self._model().measure("Slash").expression
            self.assertEqual(unchanged.strip(), "a/b")
            self.assertNotIn("DIVIDE", unchanged)
            changed = rewrite.run(self.ws, self.project, "divide", "[Slash]", accept_semantic=True)
            self.assertEqual(changed["result"], "CHANGED")
            expr = self._model().measure("Slash").expression
            self.assertIn("DIVIDE(a, b)", expr)
            self.assertNotIn("/", expr)
            before = self._model().measure("Total Sales").expression
            skipped = rewrite.run(self.ws, self.project, "divide", "[Total Sales]", accept_semantic=True)
        self.assertEqual(skipped["result"], "NOTHING_TO_CHANGE")
        self.assertEqual(self._model().measure("Total Sales").expression, before)
        self.assertNotIn("/", before)

    def test_expand_returns_define_without_executing(self):
        with patch("pbz.desktop.require_port", side_effect=AssertionError("expand executed")), \
             patch("pbz.common.engine_call", side_effect=AssertionError("expand executed")):
            out = expand.run(self.ws, self.project, "[Base Unused]")
        self.assertIn("DEFINE", out["dax"])
        self.assertIn("Total Sales", out["dax"])
        self.assertIn("Total Sales", out["measures"])
        self.assertIn("Base Unused", out["measures"])

    def test_calendar_and_kpi_parse_back(self):
        added = calendars.add_calendar(self.ws, self.project, "Date", "Order", "Date", unit="Day")
        self.assertEqual(added["unit"], "Day")
        kpi = calendars.set_kpi(self.ws, self.project, "[Total Sales]", "[Base Unused]", None, None)
        self.assertIn("target", kpi["set"])
        model = self._model()
        date_nodes = model.docs["tables/Date.tmdl"].nodes
        calendar = next(node for node in date_nodes if node.kind == "calendar" and node.name == "Order")
        group = next(node for node in calendar.children if node.kind == "calendarColumnGroup")
        self.assertEqual(group.props.get("unit", "").strip(), "Day")
        self.assertEqual(group.props.get("column", "").strip(), "Date")
        sales_nodes = model.docs["tables/Sales.tmdl"].nodes
        block = next(node for node in sales_nodes
                     if node.kind == "kpi" and node.parent is not None and node.parent.name == "Total Sales")
        self.assertEqual(block.kind, "kpi")

    def test_dax_arity_names_the_function(self):
        before = daxcatalog.findings(self._model())
        self.assertFalse(any(item["object"] == "[Total Sales]" for item in before))
        self._insert_measure("Bad Related", "RELATED()", "10000000-0000-0000-0000-000000000092")
        self._insert_measure("Mystery", "NopeFunc(Sales[Amount])", "10000000-0000-0000-0000-000000000093")
        found = daxcatalog.findings(self._model())
        self.assertFalse(any(item["object"] == "[Total Sales]" for item in found))
        hits = [item for item in found if item.get("object") == "[Bad Related]"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["message"], "RELATED has the wrong number of arguments")
        self.assertNotIn("RELATED()", hits[0]["message"])
        self.assertNotIn("RELATED()", hits[0]["object"])
        self.assertFalse(any(item["object"] == "[Mystery]" or "NOPEFUNC" in item["message"].upper() for item in found))

    def test_ci_gate_and_annotations_omit_expressions(self):
        finding = {
            "rule": "PLAN_RULE",
            "code": "PLAN_RULE",
            "object": "[Total Sales]",
            "where": "definition/tables/Sales.tmdl",
            "severity": "error",
            "message": LEAK_EXPR,
            "expression": LEAK_EXPR + " " + LEAK_TOKEN,
        }
        out = {}
        with self.assertRaises(PbzError) as caught:
            ciout.attach(self.ws, out, [finding], fail_on="new")
        self.assertEqual(caught.exception.code, "BAD_REQUEST")
        self.assertIn("since", caught.exception.detail)
        self.assertNotIn("gate", out)
        buf = io.StringIO()
        with redirect_stderr(buf):
            attached = ciout.attach(self.ws, {}, [finding], fail_on="any", ci="github")
        self.assertEqual(attached["gate"], "FAIL")
        self.assertEqual(attached["gate_count"], 1)
        annotations = attached["annotations"]
        self.assertEqual(len(annotations), 1)
        ann = annotations[0]
        self.assertTrue(set(ann) <= {"rule", "object", "path", "severity"})
        self.assertEqual(ann["rule"], "PLAN_RULE")
        self.assertEqual(ann["object"], "[Total Sales]")
        self.assertEqual(ann["path"], "definition/tables/Sales.tmdl")
        blob = json.dumps(annotations, ensure_ascii=False) + buf.getvalue()
        self.assertFalse(LEAK_EXPR in blob, "annotation contains a measure expression")
        self.assertFalse("«" in blob, "annotation contains a token")
        self.assertFalse(LEAK_TOKEN in blob, "annotation contains a token")


class PerfVerdictTests(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(perf.verdict(100, 100, 0), "SAME")
        self.assertEqual(perf.verdict(100, 130, 40), "NOISY")
        self.assertEqual(perf.verdict(100, 130, 10), "SLOWER")
        self.assertEqual(perf.verdict(100, 70, 10), "FASTER")
        self.assertEqual(perf.verdict(0, 100, 10), "NOISY")


class MirrorTransactionTests(unittest.TestCase):
    def test_restores_prior_bytes_when_fn_raises(self):
        tmp = tempfile.TemporaryDirectory(prefix="pbztxn-")
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "tree")
        os.makedirs(os.path.join(root, "sub"))
        kept = os.path.join(root, "sub", "kept.txt")
        prior = b"prior-bytes\n"
        with open(kept, "wb") as handle:
            handle.write(prior)
        created = os.path.join(root, "created.txt")

        def fn():
            with open(kept, "wb") as handle:
                handle.write(b"changed\n")
            with open(created, "wb") as handle:
                handle.write(b"new\n")
            raise RuntimeError("edit failed")

        with self.assertRaises(RuntimeError):
            mirror_txn.transaction(root, fn)
        with open(kept, "rb") as handle:
            self.assertEqual(handle.read(), prior)
        self.assertFalse(os.path.lexists(created))


if __name__ == "__main__":
    unittest.main()
