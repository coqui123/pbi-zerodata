"""Offline checks for token-safe DAX formatting.

Run: python tests/test_daxfmt.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import common, daxfmt, lexers  # noqa: E402

SMALL = "SUM ( Sales[Amount] )"
TOKENS = 'CALCULATE(SUM(Sales[Amount]), Sales[Region] = "North", Sales[Qty] > «num#2», Sales[Code] = «lit#1»)'
COMMENTED = "SUM(Sales[Amount]) // keep this comment"
COUNTISH = "COUNT(Sales[Amount]) + COUNTROWS(Sales)"


def _sig(expr: str):
    out = []
    for tok in lexers.significant(lexers.lex_dax(expr)):
        text = tok.text.upper() if tok.kind == "ident" else tok.text
        out.append((tok.kind, text))
    return out


class DaxFmtTests(unittest.TestCase):
    def test_small_measure_is_idempotent(self):
        once = daxfmt.format_dax(SMALL)
        self.assertIsNotNone(once)
        self.assertEqual(daxfmt.format_dax(once), once)
        self.assertIn("Sales[Amount]", once)

    def test_preserves_tokens_and_string_literals(self):
        once = daxfmt.format_dax(TOKENS)
        self.assertIsNotNone(once)
        self.assertIn("«lit#1»", once)
        self.assertIn("«num#2»", once)
        self.assertIn('"North"', once)
        self.assertEqual(daxfmt.format_dax(once), once)
        self.assertEqual(_sig(TOKENS), _sig(once))

    def test_preserves_comments(self):
        self.assertIsNone(daxfmt.format_dax(COMMENTED))
        self.assertIsNone(daxfmt.format_dax("SUM(Sales[Amount])\n/* block */"))

    def test_does_not_invent_counta_or_change_references(self):
        once = daxfmt.format_dax(COUNTISH)
        self.assertIsNotNone(once)
        self.assertNotIn("COUNTA", once)
        self.assertRegex(once, r"\bCOUNT\b")
        self.assertIn("COUNTROWS", once)
        self.assertIn("Sales[Amount]", once)
        self.assertEqual(_sig(COUNTISH), _sig(once))

    def test_run_formats_a_measure_and_leaves_comments(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        ws = common.Workspace(root)
        with open(os.path.join(root, "Fmt.pbip"), "w", encoding="utf-8") as handle:
            handle.write("{}")
        project = common.Project(ws, "Fmt")
        path = os.path.join(project.mirror_model, "definition", "tables", "Sales.tmdl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        original = (
            "table Sales\n\n"
            "\tmeasure Total = SUM ( Sales[Amount] )\n\n"
            "\tmeasure Noted = SUM(Sales[Amount]) // keep this comment\n\n"
            "\tmeasure Refs = COUNT ( Sales[Amount] ) + COUNTROWS ( Sales )\n\n"
            "\tmeasure Tok = CALCULATE ( SUM ( Sales[Amount] ) , Sales[Region] = \"North\" , "
            "Sales[Code] = «lit#1» )\n"
        )
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(original)
        first = daxfmt.run(ws, project, apply=True)
        self.assertEqual(first["verb"], "format")
        self.assertGreaterEqual(first["formatted"], 1)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("// keep this comment", text)
        self.assertIn("«lit#1»", text)
        self.assertIn('"North"', text)
        self.assertNotIn("COUNTA", text)
        self.assertRegex(text, r"\bCOUNT\b")
        self.assertIn("Sales[Amount]", text)
        again = daxfmt.run(ws, project, apply=True)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), text)
        self.assertEqual(again["formatted"], 0)


if __name__ == "__main__":
    unittest.main()
