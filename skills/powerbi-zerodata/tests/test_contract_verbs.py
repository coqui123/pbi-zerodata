"""Every engine dispatch verb has a schema entry and a CONTRACT.md heading.

Run: python tests/test_contract_verbs.py
"""
from __future__ import annotations

import json
import os
import re
import unittest

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
