"""Offline checks that move/rename plan every edit before touching the mirror.

A shared-alias conflict or a PBIR parse failure must leave every mirrored file
byte-identical. culture_edits_move still moves every translation hit when the
move path commits.

Run: python tests/test_atomic_refactor.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import common, move, rename, tmdl  # noqa: E402
from pbz.common import PbzError  # noqa: E402

SALES = "table Sales\n\n\tmeasure Amount = 1\n\n"
METRICS = "table Metrics\n\n\tmeasure Other = 1\n\n"
CULTURE = (
    "cultureInfo en-US\n\n"
    "\ttranslations\n\n"
    "\t\tmodel Model\n\n"
    "\t\t\ttable Sales\n\n"
    "\t\t\t\tmeasure Amount\n"
    "\t\t\t\t\tcaption: One\n\n"
    "\t\t\t\tmeasure Amount\n"
    "\t\t\t\t\tcaption: Two\n\n"
    "\t\t\ttable Keep\n\n"
    "\t\t\t\tmeasure Other\n"
    "\t\t\t\t\tcaption: Stay\n"
)
PERSPECTIVE = (
    "perspective P\n"
    "\tperspectiveTable Sales\n"
    "\t\tperspectiveMeasure Amount\n"
)
CLEAN = {
    "visual": {"query": {"queryState": {"Y": {"projections": [
        {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Amount"}}}
    ]}}}}
}
SHARED = {
    "From": [{"Name": "s", "Entity": "Sales"}],
    "Where": [{"Condition": {"In": {"Expressions": [
        {"Measure": {"Expression": {"SourceRef": {"Source": "s"}}, "Property": "Amount"}},
        {"Column": {"Expression": {"SourceRef": {"Source": "s"}}, "Property": "Qty"}},
    ]}}}],
}


def _bytes(root: str) -> dict[str, bytes]:
    out = {}
    for cur, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(cur, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, root)] = f.read()
    return out


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


class AtomicRefactorTests(unittest.TestCase):
    def _project(self, *, shared=False, broken=False, culture=False, real_culture=False, perspective=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        ws = common.Workspace(root)
        ws.private = os.path.join(root, "private")
        os.makedirs(ws.private)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        project = common.Project(ws, "Mini")
        mdef = os.path.join(project.mirror_model, "definition")
        _write(os.path.join(mdef, "tables", "Sales.tmdl"), SALES)
        _write(os.path.join(mdef, "tables", "Metrics.tmdl"), METRICS)
        page = os.path.join(project.mirror_report, "definition", "pages", "p1", "visuals")
        _write(os.path.join(page, "v_ok", "visual.json"), json.dumps(CLEAN))
        if shared:
            _write(os.path.join(page, "v_bad", "visual.json"), json.dumps(SHARED))
        if broken:
            _write(os.path.join(page, "v_broken", "visual.json"), "{")
        if culture:
            _write(os.path.join(mdef, "cultures", "en-US.tmdl"), CULTURE)
        if perspective:
            _write(os.path.join(mdef, "perspectives", "P.tmdl"), PERSPECTIVE)
        if real_culture:
            _write(os.path.join(project.model_dir, "definition", "cultures", "de-DE.tmdl"), CULTURE)
        else:
            os.makedirs(project.model_dir, exist_ok=True)
        return ws, project

    def test_m_rename_edits_comparisons_but_preserves_scopes_and_case(self):
        from pbz import rename
        ws, project = self._project()
        mdef = os.path.join(project.mirror_model, "definition")
        path = os.path.join(mdef, "tables", "Metrics.tmdl")
        _write(path, METRICS + "\tpartition Metrics = m\n\t\tsource = "
               "{if Sales = Other then Sales else Other, (let Sales = 1 in Sales), sales, "
               "[Sales = 1, x = Sales], Sales, (Sales) => Sales, each [Sales] + Sales}\n")
        rename.rename(ws, project, "table", "Sales", "Revenue")
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("if Revenue = Other then Revenue else Other", text)
        self.assertIn("(let Sales = 1 in Sales), sales", text)
        self.assertIn("[Sales = 1, x = Sales], Revenue, (Sales) => Sales, each [Sales] + Revenue", text)

    def test_uncertain_m_rename_refuses_before_writing(self):
        from pbz import rename
        ws, project = self._project()
        path = os.path.join(project.mirror_model, "definition", "tables", "Metrics.tmdl")
        _write(path, METRICS + "\tpartition Metrics = m\n\t\tsource = try let Sales = 1 in Sales otherwise Sales\n")
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "table", "Sales", "Revenue")
        self.assertEqual(caught.exception.code, "AMBIGUOUS")
        self.assertEqual(_bytes(ws.mirror), before)

    def test_column_rename_rls_and_uncertain_candidate_atomicity(self):
        from pbz import rename
        ws, project = self._project()
        mdef = os.path.join(project.mirror_model, "definition")
        path = os.path.join(mdef, "tables", "Sales.tmdl")
        _write(path, SALES + "\tcolumn Qty\n\tmeasure Homed = [Qty]\n"
               "\tmeasure Iterator = SUMX(Metrics, [Qty])\n")
        other = os.path.join(mdef, "tables", "Metrics.tmdl")
        _write(other, METRICS + "\tcolumn Qty\n")
        role = os.path.join(mdef, "roles", "Reader.tmdl")
        _write(role, "role Reader\n\ttablePermission Sales = [Qty] > 0 && Sales[Qty] > 0\n")
        # A bare candidate in a third table is ambiguous; no declaration or RLS
        # edits may be committed before that is detected.
        unknown = os.path.join(mdef, "tables", "Unknown.tmdl")
        _write(unknown, "table Unknown\n\tmeasure Ambiguous = [Qty]\n")
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError):
            rename.rename(ws, project, "column", "Qty", "New", table="Sales")
        self.assertEqual(_bytes(ws.mirror), before)
        _write(unknown, "table Unknown\n")
        rename.rename(ws, project, "column", "Qty", "New", table="Sales")
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("column New", text)
        self.assertIn("Homed = [New]", text)
        self.assertIn("SUMX(Metrics, [Qty])", text)
        self.assertIn("[New] > 0 && Sales[New] > 0", Path(role).read_text(encoding="utf-8"))

    def test_shared_alias_leaves_mirror_unchanged(self):
        ws, project = self._project(shared=True, culture=True, perspective=True)
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            move.move_measure(ws, project, "Amount", "Metrics")
        self.assertEqual(caught.exception.code, "UNSUPPORTED")
        self.assertIn("v_bad/visual.json", caught.exception.detail)
        self.assertEqual(_bytes(ws.mirror), before)
        sales = before[os.path.join("Mini.SemanticModel", "definition", "tables", "Sales.tmdl")]
        self.assertIn(b"measure Amount", sales)

    def test_pbir_parse_failure_leaves_mirror_unchanged(self):
        ws, project = self._project(broken=True)
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            move.move_measure(ws, project, "Amount", "Metrics")
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertIn("v_broken/visual.json", caught.exception.detail)
        self.assertEqual(_bytes(ws.mirror), before)

    def test_rename_parse_failure_leaves_mirror_unchanged(self):
        ws, project = self._project(broken=True)
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "table", "Sales", "Clients")
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertTrue(os.path.isfile(os.path.join(
            project.mirror_model, "definition", "tables", "Sales.tmdl")))

    def test_move_applies_every_culture_hit(self):
        ws, project = self._project(culture=True, perspective=True)
        out = move.move_measure(ws, project, "Amount", "Metrics")
        self.assertIn("Mini.SemanticModel/definition/cultures/en-US.tmdl", out["files"])
        self.assertIn("Mini.SemanticModel/definition/perspectives/P.tmdl", out["files"])
        path = os.path.join(project.mirror_model, "definition", "cultures", "en-US.tmdl")
        doc = tmdl.parse("cultures/en-US.tmdl", common.read_text(path)[0])
        amounts = [n for n in doc.nodes if n.kind == "measure" and n.name == "Amount"]
        self.assertEqual(len(amounts), 2)
        self.assertTrue(all(n.parent is not None and n.parent.name == "Metrics" for n in amounts))
        self.assertEqual(sorted(n.props.get("caption") for n in amounts), ["One", "Two"])
        other = next(n for n in doc.nodes if n.kind == "measure" and n.name == "Other")
        self.assertEqual(other.parent.name, "Keep")
        persp = common.read_text(os.path.join(project.mirror_model, "definition", "perspectives", "P.tmdl"))[0]
        self.assertIn("perspectiveTable Metrics", persp)
        self.assertIn("perspectiveMeasure Amount", persp)
        self.assertNotIn("perspectiveMeasure Amount\n", persp.split("perspectiveTable Metrics", 1)[0])

    def test_unmirrored_cultures_say_pull(self):
        ws, project = self._project(real_culture=True)
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            move.move_measure(ws, project, "Amount", "Metrics")
        self.assertEqual(caught.exception.code, "UNSUPPORTED")
        self.assertIn("cultures not in this mirror: run pbz pull", caught.exception.detail)
        self.assertEqual(_bytes(ws.mirror), before)
        with self.assertRaises(PbzError) as caught_table:
            rename.rename(ws, project, "table", "Sales", "Clients")
        self.assertIn("cultures not in this mirror: run pbz pull", caught_table.exception.detail)
        self.assertEqual(_bytes(ws.mirror), before)

    def test_write_failure_rolls_back(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        real = common.write_text
        calls = {"n": 0}

        def boom(path, text, bom=b"", *, root=None, **_kw):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("disk")
            return real(path, text, bom, root=root)

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                move.move_measure(ws, project, "Amount", "Metrics")
            self.assertEqual(caught.exception.code, "FAILED")
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(_bytes(ws.mirror), before)

    def test_replace_failure_cleans_tmp(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        real_replace = os.replace
        calls = {"n": 0}

        def boom(src, dst):
            # Fail only a real write's .pbz-tmp→dest; leave restore/tmp cleanup intact.
            if str(src).endswith(".pbz-tmp") and str(dst).endswith(".tmdl"):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("replace")
            return real_replace(src, dst)

        with patch("pbz.common.os.replace", boom):
            with self.assertRaises(PbzError) as caught:
                move.move_measure(ws, project, "Amount", "Metrics")
            self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(_bytes(ws.mirror), before)
        leftovers = list(Path(ws.mirror).rglob("*.pbz-tmp"))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
