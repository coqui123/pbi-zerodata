"""Dynamic format expressions reload as complete, renameable DAX regions."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.dirname(__file__))

from pbz import modeling, rename, tmdl
import test_modeling_atomic as fixtures


BODY = 'SWITCH(TRUE(),\n[Other] >= 1000000, "#,0,,M",\n"#,0")'


class FormatStringRoundtripTests(unittest.TestCase):
    _project = fixtures.ModelingAtomicTests._project

    def _format_child(self, doc):
        measure = next(n for n in doc.nodes if n.kind == "measure" and n.name == "Keep")
        children = [c for c in measure.children if c.kind == "formatStringDefinition"]
        self.assertEqual(len(children), 1)
        return children[0]

    def _load_format(self, mdef):
        model = tmdl.load_model(mdef)
        measure = model.measure("Keep")
        self.assertIsNotNone(measure)
        doc = model.docs[measure.node.file]
        return doc, self._format_child(doc)

    def _assert_body_region(self, doc, child, body):
        regions = doc.regions_for(child)
        self.assertEqual(len(regions), 1)
        region = regions[0]
        self.assertEqual(region.prop, "expression")
        self.assertEqual(region.kind, "dax")
        text = doc.text[region.start:region.end]
        for line in body.splitlines():
            self.assertIn(line, text)
        self.assertEqual([line.lstrip("\t") for line in text.splitlines()], body.splitlines())
        self.assertNotIn("partition", text)
        return text

    def _literal(self, body_tabs):
        return ("table Sales\n\tmeasure Keep = 1\n\t\tformatStringDefinition =\n"
                + "".join("\t" * body_tabs + line + "\n" for line in BODY.splitlines())
                + "\tmeasure Other = 1\n\tpartition Sales = m\n"
                "\t\tmode: import\n\t\tsource = let x = 1 in x\n")

    def test_multiline_format_roundtrip_region(self):
        ws, project, mdef = self._project()
        modeling.format_expression(ws, project, "[Keep]", BODY, upgrade=True)
        doc, child = self._load_format(mdef)
        self._assert_body_region(doc, child, BODY)

    def test_multiline_format_refs_visible_to_rename(self):
        ws, project, mdef = self._project()
        modeling.format_expression(ws, project, "[Keep]", BODY, upgrade=True)
        rename.rename(ws, project, "measure", "Other", "Bigger", table="Sales")
        text = Path(mdef, "tables", "Sales.tmdl").read_text(encoding="utf-8")
        format_body = text.split("formatStringDefinition =", 1)[1].split("\n\tmeasure ", 1)[0]
        self.assertIn("[Bigger]", format_body)
        doc, child = self._load_format(mdef)
        self._assert_body_region(doc, child, BODY.replace("[Other]", "[Bigger]"))

    def test_reader_only_literal_corrected_indent(self):
        doc = tmdl.parse("tables/Sales.tmdl", self._literal(4))
        self._assert_body_region(doc, self._format_child(doc), BODY)

    def test_reader_only_literal_old_indent_no_region(self):
        doc = tmdl.parse("tables/Sales.tmdl", self._literal(3))
        self.assertEqual(doc.regions_for(self._format_child(doc)), [])

    def test_old_mirror_rewrite_repairs_region(self):
        ws, project, mdef = self._project()
        path = Path(mdef, "tables", "Sales.tmdl")
        old_text = self._literal(3)
        path.write_bytes(old_text.encode("utf-8"))
        prefix = old_text[:old_text.index("\t\tformatStringDefinition =")].encode("utf-8")
        suffix = old_text[old_text.index("\tmeasure Other = 1\n"):].encode("utf-8")
        doc, child = self._load_format(mdef)
        self.assertEqual(doc.regions_for(child), [])
        modeling.format_expression(ws, project, "[Keep]", BODY, upgrade=True)
        rewritten = path.read_bytes()
        self.assertTrue(rewritten.startswith(prefix))
        self.assertTrue(rewritten.endswith(suffix))
        self.assertEqual(rewritten.count(b'[Other] >= 1000000, "#,0,,M",'), 1)
        doc, child = self._load_format(mdef)
        self._assert_body_region(doc, child, BODY)

        self.assertNotIn(b"\n\t\t\tSWITCH", rewritten)

    def test_var_shaped_body_lines_no_phantom_nodes(self):
        ws, project, mdef = self._project()
        body = 'VAR x = [Other] + 1\nRETURN\nIF(x > 1000, "#,0,K", "#,0")'
        modeling.format_expression(ws, project, "[Keep]", body, upgrade=True)
        doc, child = self._load_format(mdef)
        self.assertFalse(any(n.kind == "VAR" for n in doc.nodes))
        self._assert_body_region(doc, child, body)

    def test_single_line_format_roundtrip_region(self):
        ws, project, mdef = self._project()
        body = 'IF([Other] > 1000, "#,0,K", "#,0")'
        modeling.format_expression(ws, project, "[Keep]", body, upgrade=True)
        doc, child = self._load_format(mdef)
        self._assert_body_region(doc, child, body)


if __name__ == "__main__":
    unittest.main()
