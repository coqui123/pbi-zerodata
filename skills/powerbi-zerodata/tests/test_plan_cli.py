"""CLI and MCP wiring for the plan-mode library functions. Offline; no Desktop.

Run: python tests/test_plan_cli.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import cli, common, mcp, mirror, tmdl, validate  # noqa: E402

LEAK_EXPR = "SUM(Sales[Amount])"


def _subcommands(parser):
    import argparse
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    raise AssertionError("pbz parser has no subcommands")


class PlanCliParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser = cli.argument_parser()
        cls.commands = _subcommands(cls.parser)

    def test_relationship_paths_parses_without_requiring_columns(self):
        from_col = next(a for a in self.commands["relationship"]._actions if a.dest == "from_col")
        to_col = next(a for a in self.commands["relationship"]._actions if a.dest == "to_col")
        self.assertFalse(from_col.required)
        self.assertFalse(to_col.required)
        self.assertIn("paths", next(a for a in self.commands["relationship"]._actions if a.dest == "action").choices)
        args = self.parser.parse_args(["relationship", "paths", "Sales", "Date"])
        self.assertEqual(args.action, "paths")
        self.assertEqual(args.from_col, "Sales")
        self.assertEqual(args.to_col, "Date")
        added = self.parser.parse_args(["relationship", "add", "'Sales'[CustomerKey]", "'Customer'[CustomerKey]"])
        self.assertEqual(added.action, "add")
        self.assertEqual(added.from_col, "'Sales'[CustomerKey]")
        self.assertEqual(added.to_col, "'Customer'[CustomerKey]")

    def test_bpa_import_rules_stays_beside_the_overlay(self):
        args = self.parser.parse_args(["bpa", "--rules", "overlay.json", "--import-rules", "custom.json",
                                        "--fail-on", "high", "--since", "base"])
        self.assertEqual(args.rules, "overlay.json")
        self.assertEqual(args.import_rules, "custom.json")
        self.assertEqual(args.fail_on, "high")
        self.assertEqual(args.since, "base")

    def test_validate_ci_flags_parse(self):
        args = self.parser.parse_args(["validate", "--fail-on", "new", "--ci", "github", "--format", "junit",
                                        "--out", "report.xml", "--save-baseline", "nightly"])
        self.assertEqual(args.fail_on, "new")
        self.assertIsNone(args.since)
        self.assertEqual(args.ci, "github")
        self.assertEqual(args.format, "junit")
        self.assertEqual(args.out, "report.xml")
        self.assertEqual(args.save_baseline, "nightly")

    def test_function_import_is_a_choice(self):
        choices = next(a for a in self.commands["function"]._actions if a.dest == "action").choices
        self.assertIn("import", choices)
        args = self.parser.parse_args(["function", "import", "funcs.dax"])
        self.assertEqual(args.action, "import")
        self.assertEqual(args.name, "funcs.dax")

    def test_translate_export_parses_without_culture(self):
        args = self.parser.parse_args(["translate", "--export", "captions.json"])
        self.assertEqual(args.export, "captions.json")
        self.assertIsNone(args.code)
        self.assertIsNone(args.target)
        imported = self.parser.parse_args(["translate", "--import", "captions.json"])
        self.assertEqual(imported.import_path, "captions.json")
        caption = self.parser.parse_args(["translate", "fr-FR", "Sales", "--caption", "Ventes"])
        self.assertEqual(caption.code, "fr-FR")
        self.assertEqual(caption.target, "Sales")
        self.assertEqual(caption.caption, "Ventes")
        self.assertIsNone(caption.export)

    def test_similar_models_parses(self):
        args = self.parser.parse_args(["similar", "--models", "A", "--models", "B", "--against", "C"])
        self.assertEqual(args.models, ["A", "B"])
        self.assertEqual(args.against, "C")
        plain = self.parser.parse_args(["similar", "--threshold", "0.5"])
        self.assertEqual(plain.models, [])
        self.assertIsNone(plain.against)
        self.assertEqual(plain.threshold, 0.5)

    def test_unused_what_if_parses(self):
        args = self.parser.parse_args(["unused", "--what-if"])
        self.assertTrue(args.what_if)
        self.assertFalse(args.remove)

    def test_changes_against_parses(self):
        args = self.parser.parse_args(["changes", "--against", "other-workspace"])
        self.assertEqual(args.against, "other-workspace")
        plain = self.parser.parse_args(["changes", "--from", "previous", "--to", "last"])
        self.assertIsNone(plain.against)
        self.assertEqual(plain.from_, "previous")
        self.assertEqual(plain.to, "last")

    def test_lineage_from_source_parses(self):
        args = self.parser.parse_args(["lineage", "--from-source", "Sales", "Amount"])
        self.assertEqual(args.from_source, ["Sales", "Amount"])
        self.assertIsNone(args.target)
        native = self.parser.parse_args(["lineage", "--native-sql"])
        self.assertTrue(native.native_sql)
        exported = self.parser.parse_args(["lineage", "[Total Sales]", "--format", "json", "--out", "line.json"])
        self.assertEqual(exported.target, "[Total Sales]")
        self.assertEqual(exported.format, "json")
        self.assertEqual(exported.out, "line.json")
        impact = self.parser.parse_args(["impact", "--drop-source-column", "Amount"])
        self.assertEqual(impact.drop_source_column, "Amount")
        renamed = self.parser.parse_args(["impact", "--rename-source-column", "Amount", "--to", "Amt"])
        self.assertEqual(renamed.rename_source_column, "Amount")
        self.assertEqual(renamed.to, "Amt")

    def test_set_has_no_separate_property_allowlist(self):
        items = next(a for a in self.commands["set"]._actions if a.dest == "items")
        self.assertIsNone(items.choices)
        from pbz import props
        for prop in ("encodingHint", "isNullable", "lineageTag", "detailRowsExpression", "alternateOf"):
            self.assertIn(prop, props.PROPS)

    def test_check_spec_is_not_filtered_by_kind(self):
        check = self.commands["check"]
        self.assertFalse(any(a.dest == "kind" and a.choices for a in check._actions))

    def test_mcp_twins_keep_rename_required(self):
        self.assertEqual(set(mcp.BY_NAME["rename"].schema()["required"]), {"kind", "old", "new"})
        self.assertEqual(set(mcp.BY_NAME["relationship"].schema()["required"]), {"action", "from_col", "to_col"})
        self.assertNotIn("paths", mcp.BY_NAME["relationship"].schema()["properties"]["action"]["enum"])
        paths = mcp.BY_NAME["relationship_paths"]
        self.assertEqual(paths.verb, ["relationship", "paths"])
        self.assertEqual(set(paths.schema()["required"]), {"left", "right"})
        self.assertTrue(paths.read_only)
        self.assertNotIn("--dry-run", {spec[0] for spec in paths.opts.values()})
        self.assertIn("import", mcp.BY_NAME["function"].schema()["properties"]["action"]["enum"])
        self.assertIn("target", mcp.BY_NAME["lineage"].schema()["required"])
        source = mcp.BY_NAME["lineage_source"].schema()
        self.assertNotIn("target", source.get("required", []))
        self.assertIn("from_source", source["properties"])
        self.assertIn("native_sql", source["properties"])
        self.assertIn("--replay", {opt for a in self.commands["perf-import"]._actions for opt in a.option_strings})
        self.assertNotIn("--replay", {spec[0] for spec in mcp.BY_NAME["perf_import"].opts.values()})
        for name, flag in (("bpa", "--import-rules"), ("bpa", "--fail-on"), ("validate", "--fail-on"),
                           ("validate", "--save-baseline"), ("unused", "--what-if"), ("changes", "--against"),
                           ("similar", "--models"), ("similar", "--against"), ("translate", "--export"),
                           ("translate", "--import"), ("impact", "--drop-source-column"),
                           ("lineage_source", "--from-source"), ("lineage_source", "--format")):
            flags = {spec[0] for spec in mcp.BY_NAME[name].opts.values()}
            self.assertIn(flag, flags, name)


class PlanCliWireTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pbzplancli-")
        self.addCleanup(self.tmp.cleanup)
        private = os.path.join(self.tmp.name, "private")
        os.environ["PBZ_PRIVATE_ROOT"] = private
        self.addCleanup(lambda: os.environ.pop("PBZ_PRIVATE_ROOT", None))
        self.root = os.path.join(self.tmp.name, "ws")
        test_parity.build(self.root)
        self.ws = common.Workspace(self.root)
        self.project = self.ws.project()
        mirror.pull(self.ws, self.project)

    def _cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--workspace", self.root, "--project", "Parity", *argv])
        return rc, json.loads(out.getvalue()), err.getvalue()

    def test_relationship_paths_on_the_parity_model(self):
        rc, body, _err = self._cli(["relationship", "paths", "Sales", "Date"])
        self.assertEqual(rc, 0, body)
        self.assertEqual(body["action"], "paths")
        self.assertEqual(body["status"], "ONE")
        self.assertEqual(body["relationships"], ["r2"])
        self.assertEqual(body["from"], "Sales")
        self.assertEqual(body["to"], "Date")
        missing, rejected, _err = self._cli(["relationship", "add"])
        self.assertNotEqual(missing, 0)
        self.assertEqual(rejected["error"], "BAD_REQUEST")

    def test_what_if_and_circular_dependencies(self):
        rc, body, _err = self._cli(["unused", "--what-if"])
        self.assertEqual(rc, 0, body)
        self.assertEqual(body["action"], "what-if")
        self.assertEqual(body["status"], "UNKNOWN")
        self.assertEqual(body["reason"], "NO_BANDS")
        self.assertNotIn("summary", body)
        model = tmdl.load_model(os.path.join(self.project.mirror_model, "definition"))
        self.assertEqual(validate.circular_dependencies(model), [])

    def test_changes_against_a_semantic_model_is_refused(self):
        raw = os.path.join(self.root, "Parity.SemanticModel")
        rc, body, _err = self._cli(["changes", "--against", raw])
        self.assertNotEqual(rc, 0)
        self.assertEqual(body["error"], "REFUSED")
        plain, snapshot, _err = self._cli(["changes", "--list"])
        self.assertEqual(plain, 0, snapshot)
        self.assertNotEqual(snapshot.get("action"), "against")

    def test_validate_fail_on_new_requires_since_and_gate_stays(self):
        rc, body, _err = self._cli(["validate", "--fail-on", "new"])
        self.assertNotEqual(rc, 0)
        self.assertEqual(body["error"], "BAD_REQUEST")
        self.assertIn("since", body.get("detail", ""))
        self.assertNotIn("gate", body)
        finding = {"rule": "PLAN_RULE", "code": "PLAN_RULE", "object": "[Total Sales]",
                   "where": "definition/tables/Sales.tmdl", "severity": "error",
                   "message": LEAK_EXPR, "expression": LEAK_EXPR + " «lit#1»"}
        with patch("pbz.cli.validate.run_mirror", return_value=[finding]):
            rc, body, err = self._cli(["validate", "--fail-on", "any", "--ci", "github"])
        self.assertEqual(body["gate"], "FAIL")
        self.assertEqual(rc, 1)
        self.assertEqual(body["outcome"]["code"], "GATE_FAILED")
        ann = body["annotations"][0]
        self.assertTrue(set(ann) <= {"rule", "object", "path", "severity"})
        blob = json.dumps(ann) + err
        self.assertNotIn(LEAK_EXPR, blob)
        self.assertNotIn("«lit#1»", blob)
        self.assertIn("::error", err)

    def test_bpa_import_rules_reaches_run_and_the_gate(self):
        seen = {}
        finding = {"rule": "CUSTOM", "object": "[Total Sales]", "where": "rules.json", "severity": 3,
                   "message": LEAK_EXPR}

        def fake(ws, project, scope="all", rules_path=None, opinion=False, labels="letters", extra_reports=(),
                 import_rules=None):
            seen["rules_path"] = rules_path
            seen["import_rules"] = import_rules
            return {"verb": "bpa", "findings": [finding]}

        with patch("pbz.bpa.run", fake):
            rc, body, err = self._cli(["bpa", "--rules", "overlay.json", "--import-rules", "custom.json",
                                       "--fail-on", "any", "--ci", "azdo"])
        self.assertEqual(seen["rules_path"], "overlay.json")
        self.assertEqual(seen["import_rules"], "custom.json")
        self.assertEqual(body["gate"], "FAIL")
        self.assertEqual(rc, 1)
        blob = json.dumps(body["annotations"]) + err
        self.assertNotIn(LEAK_EXPR, blob)
        self.assertIn("##vso[task.logissue", err)

    def test_check_accepts_rls_and_perf_specs(self):
        spec = json.dumps({"checks": [
            {"id": "r1", "kind": "rls", "expect": "EMPTY", "role": "Region Reader"},
            {"id": "p1", "kind": "perf", "query": "EVALUATE {1}", "expect": "NOT_SLOWER"},
        ]})
        rc, body, _err = self._cli(["check", spec, "--validate-only"])
        self.assertEqual(rc, 0, body)
        self.assertTrue(body["spec_valid"])
        self.assertEqual(body["findings"], [])

    def test_set_passes_new_properties_to_props(self):
        rc, body, _err = self._cli(["set", "'Sales'[Amount]", "encodingHint=Hash", "isNullable=true", "--dry-run"])
        self.assertEqual(rc, 0, body)
        self.assertEqual(body["result"], "DRY_RUN")
        changed = body["changed"][0]["changes"]
        self.assertIn("encodingHint", changed)
        self.assertIn("isNullable", changed)
        rc, body, _err = self._cli(["set", "[Total Sales]", "lineageTag=regenerate",
                                    "detailRowsExpression=Sales[Amount]", "alternateOf=sum: Sales[Amount]", "--dry-run"])
        self.assertEqual(rc, 0, body)
        changed = body["changed"][0]["changes"]
        self.assertIn("lineageTag", changed)
        self.assertIn("detailRowsExpression", changed)
        self.assertIn("alternateOf", changed)

    def test_translate_export_returns_counts_and_import_refuses_tokens(self):
        rc, body, _err = self._cli(["translate", "--export", "captions.json"])
        self.assertEqual(rc, 0, body)
        self.assertEqual(body["action"], "export")
        self.assertNotIn("file", body)
        self.assertIn("translations", body)
        self.assertNotIn("value", body)
        self.assertNotIn(LEAK_EXPR, json.dumps(body))
        bad = os.path.join(self.root, "bad-translations.json")
        with open(bad, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "translations": [{
                "culture": "fr-FR", "objectType": "measure", "table": "Sales", "object": "Total Sales",
                "property": "caption", "value": "«lit#1»",
            }]}, handle)
        rc, body, _err = self._cli(["translate", "--import", "bad-translations.json"])
        self.assertEqual(body["error"], "BAD_REQUEST")
        self.assertNotIn("«lit#1»", json.dumps(body))

    def test_function_import_stays_inside_the_workspace(self):
        rc, body, _err = self._cli(["function", "import", "https://example.invalid/fn.json"])
        self.assertEqual(body["error"], "REFUSED")
        outside = os.path.join(self.tmp.name, "outside.dax")
        rc, body, _err = self._cli(["function", "import", outside])
        self.assertEqual(body["error"], "UNSAFE_PATH")

    def test_lineage_source_impact_and_local_output(self):
        rc, body, _err = self._cli(["lineage", "--from-source", "Sales", "Amount"])
        self.assertEqual(rc, 0, body)
        self.assertEqual(body["from_source"], {"table": "Sales", "column": "Amount"})
        self.assertIn(("Sales", "Amount"), {(c.get("table"), c.get("column")) for c in body["columns"]})
        rc, body, _err = self._cli(["lineage", "--native-sql"])
        self.assertEqual(rc, 0, body)
        self.assertIn("native_sql", body)
        self.assertNotIn("SalesDbSecret", json.dumps(body))
        self.assertTrue(all(item.get("status") == "OPAQUE" for item in body["native_sql"]))
        rc, body, _err = self._cli(["impact", "--drop-source-column", "Amount"])
        self.assertEqual(rc, 0, body)
        self.assertEqual(body["verb"], "impact")
        self.assertEqual(body["action"], "drop")
        self.assertEqual(body["source_column"], "Amount")
        rc, body, _err = self._cli(["impact", "--rename-source-column", "Amount", "--to", "Amt"])
        self.assertEqual(rc, 0, body)
        self.assertEqual(body["action"], "rename")
        self.assertEqual(body["to"], "Amt")
        rc, body, _err = self._cli(["lineage", "--format", "openlineage", "--out", "https://example.invalid/x.json"])
        self.assertEqual(body["error"], "REFUSED")
        rc, body, _err = self._cli(["lineage", "[Total Sales]", "--format", "json", "--out", "https://example.invalid/x.json"])
        self.assertEqual(body["error"], "REFUSED")
        rc, body, _err = self._cli(["lineage", "[Total Sales]", "--format", "json", "--out", "lineage.json"])
        self.assertEqual(rc, 0, body)
        self.assertEqual(body["format"], "json")
        self.assertEqual(body["file"], "lineage.json")
        with open(os.path.join(self.root, "lineage.json"), encoding="utf-8") as handle:
            written = handle.read()
        self.assertNotIn(LEAK_EXPR, written)
        self.assertIn("Total Sales", written)
        rc, body, _err = self._cli(["lineage", "--format", "openlineage", "--out", "lineage.ol.json"])
        self.assertEqual(rc, 0, body)
        self.assertEqual(body["format"], "openlineage")
        self.assertEqual(body["file"], "lineage.ol.json")
        self.assertIsInstance(body["inputs"], int)
        self.assertNotIn("SalesDbSecret", json.dumps(body))

    def test_mcp_paths_argv_matches_the_cli(self):
        argv, _cwd = mcp.build_argv(mcp.BY_NAME["relationship_paths"],
                                    {"left": "Sales", "right": "Date", "workspace": self.root}, None, "Parity")
        self.assertEqual(argv[argv.index("relationship"):], ["relationship", "paths", "Sales", "Date"])
        argv, _cwd = mcp.build_argv(mcp.BY_NAME["lineage_source"],
                                    {"from_source": ["Sales", "Amount"], "workspace": self.root}, None, None)
        self.assertEqual(argv[argv.index("lineage"):], ["lineage", "--from-source", "Sales", "Amount"])
        with self.assertRaises(mcp.ToolError):
            mcp.build_argv(mcp.BY_NAME["lineage"], {"depth": 3, "workspace": self.root}, None, None)


if __name__ == "__main__":
    unittest.main()
