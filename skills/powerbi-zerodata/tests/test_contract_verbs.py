"""Every engine dispatch verb has a schema entry and a CONTRACT.md heading.

Run: python tests/test_contract_verbs.py
"""
from __future__ import annotations

import json
import os
import re
import unittest
import sys
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pbz import engine

ENGINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine")


def dispatch_verbs(text: str) -> list[str]:
    body = text.split("static JsonObject Dispatch", 1)[1].split("static readonly JsonSerializerOptions", 1)[0]
    return re.findall(r'"([A-Za-z0-9_-]+)"\s*=>', body)


def schema_verbs(doc: dict) -> tuple[set[str], set[str]]:
    enum = set(doc["$defs"]["verb"]["enum"])
    consts = set()

    def walk(node):
        if isinstance(node, dict):
            verb = node.get("verb")
            if isinstance(verb, dict) and isinstance(verb.get("const"), str):
                consts.add(verb["const"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(doc)
    return enum, consts


def headings(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("### ")]


class ContractVerbTests(unittest.TestCase):
    def test_probe_flags_flow_and_bad_siblings_are_rejected(self):
        req = {"table": "Sales", "columns": ["Amount", "Date"], "disclosure": "shape"}
        valid = {"column": "Amount", "type": "double", "has_blanks": False, "has_negatives": True,
                 "blank_bucket": "none", "card_bucket": "1-9"}
        for bad in ({"column": "Date", "type": "SECRET"},
                    {"column": "SECRET", "type": "dateTime"},
                    {"column": "Date", "type": "dateTime", "has_future_dates": [["raw"]]},
                    {"column": "Date", "type": "dateTime", "has_blanks": 1}):
            response = {"ok": True, "verb": "probe", "table": "Sales", "rows_bucket": "1-9",
                        "columns": [valid, bad]}
            with patch.object(engine, "ENGINE_EXE", __file__), patch.object(engine.subprocess, "run",
                    return_value=SimpleNamespace(stdout=json.dumps(response).encode())):
                out = engine.call("probe", req)
            self.assertEqual(out["columns"], [valid])
            strict = engine._project("probe", response, {**req, "disclosure": "strict"})
            self.assertTrue(strict["columns"][0]["has_negatives"])
            self.assertNotIn("_bucket", json.dumps(strict))
        self.assertFalse(engine._project("probe", {**response, "rows_bucket": {"rows": [["raw"]]}}, req)["ok"])

    def test_nested_known_types_and_unknown_keys(self):
        good = {"ok": True, "verb": "health", "objects": {"tables": 1, "columns": 1, "measures": 1,
                "relationships": 0}, "errors": [], "unprocessed_tables": []}
        self.assertEqual(engine._project("health", good), good)
        bad = {**good, "objects": {**good["objects"], "tables": {"rows": [["raw"]]}}}
        self.assertFalse(engine._project("health", bad)["ok"])
        projected = engine._project("health", {**good, "objects": {**good["objects"], "unknown": [["raw"]]}})
        self.assertEqual(projected, good)
        source = {"ok": True, "verb": "probe", "table": "T", "columns": [
            {"column": "C", "type": "string", "has_blanks": True, "extra": {"rows": [["raw"]]}}]}
        out = engine._project("probe", source, {"table": "T", "columns": ["C"]})
        self.assertEqual(out["columns"], [{"column": "C", "type": "string", "has_blanks": True}])

    def test_synth_query_rows_survive(self):
        response = {"ok": True, "verb": "synth-query", "verified": True, "columns": ["X"], "rows": [["synthetic"]], "truncated": False}
        self.assertEqual(engine._project("synth-query", response), response)

    def test_deps_truncated_projection(self):
        base = {"ok": True, "verb": "deps", "edges": [["MEASURE", "Sales", "Total", "COLUMN", "Sales", "Amount"]]}
        self.assertEqual(engine._project("deps", base), base)
        with_flag = {**base, "truncated": True}
        self.assertEqual(engine._project("deps", with_flag), with_flag)
        self.assertEqual(engine._project("deps", {**base, "truncated": "yes"}),
                         {"ok": False, "verb": "deps", "class": "ENGINE_UNAVAILABLE"})

    def test_group_key_single_shapes_and_mixed_union(self):
        def response(key):
            return {"ok": True, "verb": "check", "checks": [
                {"id": "fixture", "status": "FAIL", "groups": [{"Column": key}]}]}

        with open(os.path.join(ENGINE, "contract.schema.json"), encoding="utf-8") as handle:
            defs = json.load(handle)["$defs"]
        for key in ({"reveal": "label"}, {"hmac": "a" * 64}, {"blank": True}):
            with self.subTest(key=key):
                self.assertEqual(engine._closed(key, {"$ref": "#/$defs/groupKey"}, defs), key)
                self.assertEqual(engine._project("check", response(key)), response(key))
        mixed = {"reveal": "label", "blank": True}
        with self.assertRaisesRegex(ValueError, "union"):
            engine._closed(mixed, {"$ref": "#/$defs/groupKey"}, defs)
        self.assertEqual(engine._project("check", response(mixed)),
                         {"ok": False, "verb": "check", "class": "ENGINE_UNAVAILABLE"})

    def test_every_dispatch_verb_is_in_the_schema_and_the_contract(self):
        program = open(os.path.join(ENGINE, "Program.cs"), encoding="utf-8").read()
        schema = json.load(open(os.path.join(ENGINE, "contract.schema.json"), encoding="utf-8"))
        contract = open(os.path.join(ENGINE, "CONTRACT.md"), encoding="utf-8").read()
        enum, consts = schema_verbs(schema)
        missing = []
        for verb in dispatch_verbs(program):
            if verb not in enum or verb not in consts:
                missing.append(f"schema:{verb}")
            if not any(f"`{verb}`" in line for line in headings(contract)):
                missing.append(f"contract:{verb}")
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
