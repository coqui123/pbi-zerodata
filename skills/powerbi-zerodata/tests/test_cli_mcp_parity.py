"""CLI / MCP contract: every MCP tool argv is a real pbz subcommand, and new verbs need a twin or an exclusion.

Run: python tests/test_cli_mcp_parity.py
"""
from __future__ import annotations

import argparse
import os
import sys
import unittest

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import cli, mcp  # noqa: E402

# Top-level verbs that stay on the CLI on purpose (no MCP twin).
CLI_ONLY = {"admin", "mcp"}

# Human-only surfaces MCP already omits. A new exposure should fail this test.
HUMAN_ONLY = (
    ("consent", "approve"),
    ("requirements", "accept"),
    ("admin",),
    ("memscrub", "--apply"),
)


def _subcommands(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    raise AssertionError("pbz parser has no subcommands")


def _enums(tool: mcp.Tool, arg: str) -> set[str]:
    found = set()
    for name, schema, _required in tool.pos:
        if name == arg and schema.get("enum"):
            found |= set(schema["enum"])
    for name, (_flag, schema, _mode) in tool.opts.items():
        if name == arg and schema.get("enum"):
            found |= set(schema["enum"])
    return found


class CliMcpParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.commands = _subcommands(cli.argument_parser())

    def test_mcp_argv_matches_cli(self):
        for tool in mcp.TOOLS:
            verb = list(tool.verb)
            self.assertTrue(verb, tool.name)
            self.assertIn(verb[0], self.commands, tool.name)
            parser = self.commands[verb[0]]
            choice_actions = [a for a in parser._actions if a.choices and not a.option_strings]
            for token in verb[1:]:
                self.assertTrue(any(token in a.choices for a in choice_actions), f"{tool.name} argv {verb}")
            flags = {opt for action in parser._actions for opt in action.option_strings}
            for _arg, (flag, _schema, _mode) in tool.opts.items():
                self.assertIn(flag, flags, f"{tool.name} {flag}")

    def test_new_cli_verb_needs_a_twin_or_an_exclusion(self):
        twins = {tool.verb[0] for tool in mcp.TOOLS}
        missing = set(self.commands) - twins - CLI_ONLY
        self.assertEqual(missing, set(), f"CLI verb without an MCP twin or CLI_ONLY entry: {sorted(missing)}")
        for name in CLI_ONLY:
            self.assertIn(name, self.commands, name)
        self.assertNotIn("admin", twins)
        self.assertNotIn("mcp", twins)

    def test_human_only_surfaces_stay_off_mcp(self):
        for item in HUMAN_ONLY:
            if item == ("admin",):
                self.assertIn("admin", CLI_ONLY)
                continue
            if item == ("memscrub", "--apply"):
                flags = {spec[0] for spec in mcp.BY_NAME["memscrub"].opts.values()}
                self.assertNotIn("--apply", flags)
                mem = self.commands["memscrub"]
                self.assertIn("--apply", {opt for action in mem._actions for opt in action.option_strings})
                continue
            verb, action = item
            exposed = set()
            for tool in mcp.TOOLS:
                if tool.verb[0] == verb:
                    exposed |= set(tool.verb[1:])
                    exposed |= _enums(tool, "action")
            self.assertNotIn(action, exposed, item)
            actions = [a for a in self.commands[verb]._actions if a.dest == "action" and a.choices]
            self.assertTrue(actions and action in actions[0].choices, item)

    def test_interaction_and_interactions_stay_distinct(self):
        self.assertIn("interaction", self.commands)
        self.assertIn("interactions", self.commands)
        self.assertIsNot(self.commands["interaction"], self.commands["interactions"])
        roots = [tool.verb[0] for tool in mcp.TOOLS]
        self.assertIn("interaction", roots)
        self.assertIn("interactions", roots)
        self.assertIn("capabilities", roots)

    def test_review_accept_stays_off_mcp(self):
        review = self.commands["review"]
        flags = {opt for action in review._actions for opt in action.option_strings}
        self.assertIn("--accept", flags)
        for tool in mcp.TOOLS:
            if tool.verb[0] == "review":
                exposed = {spec[0] for spec in tool.opts.values()}
                self.assertNotIn("--accept", exposed)

    def test_cli_dry_run_verbs_have_mcp_twins(self):
        for name, parser in self.commands.items():
            flags = {opt for action in parser._actions for opt in action.option_strings}
            if "--dry-run" not in flags:
                continue
            tools = [tool for tool in mcp.TOOLS if tool.verb and tool.verb[0] == name]
            self.assertTrue(tools, name)
            exposed = {spec[0] for tool in tools for spec in tool.opts.values()}
            self.assertIn("--dry-run", exposed, name)

    def test_touched_refusal_codes_share_the_envelope(self):
        """CLI and MCP both emit through outcomes.envelope. Touched Round 17 codes stay closed."""
        from pbz.outcomes import envelope
        unavailable, rc = envelope({"ok": False, "error": "ENGINE_UNAVAILABLE"})
        self.assertEqual(unavailable["outcome"]["status"], "UNAVAILABLE")
        self.assertEqual(unavailable["outcome"]["code"], "ENGINE_UNAVAILABLE")
        self.assertEqual(unavailable["error"], "ENGINE_UNAVAILABLE")
        self.assertEqual(rc, 4)
        for code in ("REFUSED", "UNSAFE_PATH", "HUMAN_ONLY", "NOT_RECORDED"):
            wrapped, code_rc = envelope({"ok": False, "error": code})
            self.assertEqual(wrapped["error"], code)
            self.assertEqual(wrapped["outcome"]["code"], code)
            self.assertNotEqual(wrapped["outcome"]["status"], "SUCCEEDED")
            self.assertNotEqual(code_rc, 0)

    def test_preview_spec_is_an_object(self):
        flag, schema, mode = mcp.BY_NAME["preview"].opts["spec"]
        self.assertEqual(flag, "--spec")
        self.assertEqual(mode, "json")
        self.assertEqual(schema["type"], "object")


if __name__ == "__main__":
    unittest.main()
