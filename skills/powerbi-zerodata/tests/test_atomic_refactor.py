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

from pbz import common, deps, move, rename, tmdl  # noqa: E402
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


class MoveBindingTests(unittest.TestCase):
    def _project(self, tables, visual_entity, visual_measure):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        ws = common.Workspace(root)
        ws.private = os.path.join(root, "private")
        os.makedirs(ws.private)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        project = common.Project(ws, "Mini")
        mdef = os.path.join(project.mirror_model, "definition")
        for fname, text in tables.items():
            _write(os.path.join(mdef, "tables", fname), text)
        page = os.path.join(project.mirror_report, "definition", "pages", "p1", "visuals")
        visual = {"query": {"queryState": {"Y": {"projections": [
            {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": visual_entity}},
                                   "Property": visual_measure}}}]}}}}
        _write(os.path.join(page, "v_ok", "visual.json"), json.dumps(visual))
        os.makedirs(project.model_dir, exist_ok=True)
        return ws, project, mdef

    @staticmethod
    def _block_bindings(mdef, measure_name):
        """(bindings, block_text) for the measure in a freshly loaded model.

        bindings: [(kind, table, name, uncertain)] over every dax region inside the
        measure's block span, in span order, resolved with deps.dax_region.
        """
        model = tmdl.load_model(mdef, root=os.path.dirname(mdef))
        m = model.measure(measure_name)
        doc = model.docs[m.node.file]
        s, e = tmdl.block_span(doc, m.node)
        res = deps.Resolver(model)
        out = []
        for reg in doc.regions:
            if reg.kind == "dax" and s <= reg.start and reg.end <= e:
                d = deps.dax_region(doc.text, reg, res)
                out.extend((b.ref.kind, b.ref.table, b.ref.name, b.uncertain) for b in d.bindings)
        return out, doc.text[s:e]

    @staticmethod
    def _normalized(bindings, measure_name):
        """Self-measure owner normalization: (measure, any table, own name) compares
        by (kind, name, uncertain)."""
        return [(k, "" if (k == "measure" and n.lower() == measure_name.lower()) else t, n, u)
                for (k, t, n, u) in bindings]

    def test_same_named_columns_keep_the_home_binding(self):
        ws, project, mdef = self._project({
            "Sales.tmdl": "table Sales\n\n\tcolumn Amount\n\n\tmeasure Total = SUM([Amount])\n",
            "Metrics.tmdl": "table Metrics\n\n\tcolumn Amount\n\n\tmeasure Other = 1\n",
        }, "Sales", "Total")
        pre, _ = self._block_bindings(mdef, "Total")
        self.assertEqual(pre, [("column", "Sales", "Amount", False)])
        move.move_measure(ws, project, "Total", "Metrics")
        fresh, block = self._block_bindings(mdef, "Total")
        self.assertIn("'Sales'[Amount]", block)
        self.assertEqual(fresh, pre)

    def test_three_owner_columns_qualify_the_source(self):
        ws, project, mdef = self._project({
            "Sales.tmdl": "table Sales\n\n\tcolumn Amount\n\n\tmeasure Total = SUM([Amount])\n",
            "Metrics.tmdl": "table Metrics\n\n\tcolumn Amount\n\n\tmeasure Other = 1\n",
            "Alpha.tmdl": "table Alpha\n\n\tcolumn Amount\n",
        }, "Sales", "Total")
        pre, _ = self._block_bindings(mdef, "Total")
        self.assertEqual(pre, [("column", "Sales", "Amount", False)])
        move.move_measure(ws, project, "Total", "Metrics")
        fresh, block = self._block_bindings(mdef, "Total")
        self.assertIn("'Sales'[Amount]", block)
        self.assertEqual(fresh, pre)

    def test_unresolved_reference_that_would_capture_refuses(self):
        ws, project, mdef = self._project({
            "Sales.tmdl": "table Sales\n\n\tmeasure Total = SUM([Rate])\n",
            "Metrics.tmdl": "table Metrics\n\n\tcolumn Rate\n\n\tmeasure Other = 1\n",
            "Tax.tmdl": "table Tax\n\n\tcolumn Rate\n",
        }, "Sales", "Total")
        pre, _ = self._block_bindings(mdef, "Total")
        self.assertEqual(pre, [("unresolved", "", "Rate", True)])
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            move.move_measure(ws, project, "Total", "Metrics")
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(_bytes(ws.mirror), before)

    def test_home_only_column_is_qualified_not_refused(self):
        ws, project, mdef = self._project({
            "Sales.tmdl": "table Sales\n\n\tcolumn Qty\n\n\tmeasure Total = SUM([Qty])\n",
            "Metrics.tmdl": "table Metrics\n\n\tmeasure Other = 1\n",
            "Alpha.tmdl": "table Alpha\n\n\tcolumn Qty\n",
            "Beta.tmdl": "table Beta\n\n\tcolumn Qty\n",
        }, "Sales", "Total")
        pre, _ = self._block_bindings(mdef, "Total")
        self.assertEqual(pre, [("column", "Sales", "Qty", False)])
        move.move_measure(ws, project, "Total", "Metrics")
        fresh, block = self._block_bindings(mdef, "Total")
        self.assertIn("SUM('Sales'[Qty])", block)
        self.assertEqual(fresh, pre)

    def test_iterator_teaching_cascade_heals(self):
        ws, project, mdef = self._project({
            "Sales.tmdl": (
                "table Sales\n\n"
                "\tcolumn Qty\n\n"
                "\tmeasure Margin = 1\n\n"
                "\tmeasure Total = SUMX(VALUES([Qty]), [Margin])\n"
            ),
            "Metrics.tmdl": "table Metrics\n\n\tmeasure Other = 1\n",
            "Alpha.tmdl": "table Alpha\n\n\tcolumn Qty\n",
            "Beta.tmdl": "table Beta\n\n\tcolumn Qty\n",
        }, "Sales", "Total")
        pre, _ = self._block_bindings(mdef, "Total")
        self.assertEqual(pre, [
            ("column", "Sales", "Qty", False),
            ("measure", "Sales", "Margin", False),
        ])
        move.move_measure(ws, project, "Total", "Metrics")
        fresh, block = self._block_bindings(mdef, "Total")
        self.assertIn("VALUES('Sales'[Qty])", block)
        self.assertIn("SUMX(VALUES('Sales'[Qty]), [Margin])", block)
        self.assertNotIn("'Sales'[Margin]", block)
        self.assertEqual(fresh, pre)

    def test_self_reference_qualifier_moves_with_the_block(self):
        ws, project, mdef = self._project({
            "Sales.tmdl": (
                "table Sales\n\n"
                "\tcolumn Amount\n\n"
                "\tmeasure Total = SUM([Amount]) + 'Sales'[Total] * 0 + [Total] * 0\n"
            ),
            "Metrics.tmdl": "table Metrics\n\n\tcolumn Amount\n\n\tmeasure Other = 1\n",
        }, "Sales", "Total")
        pre, _ = self._block_bindings(mdef, "Total")
        self.assertEqual(pre, [
            ("column", "Sales", "Amount", False),
            ("measure", "Sales", "Total", False),
            ("measure", "Sales", "Total", False),
        ])
        move.move_measure(ws, project, "Total", "Metrics")
        fresh, block = self._block_bindings(mdef, "Total")
        self.assertIn("'Metrics'[Total]", block)
        self.assertNotIn("'Sales'[Total]", block)
        self.assertIn("SUM('Sales'[Amount]) + 'Metrics'[Total] * 0 + [Total] * 0", block)
        self.assertEqual(self._normalized(fresh, "Total"), self._normalized(pre, "Total"))

    def test_clean_move_inserts_no_qualifier(self):
        ws, project, mdef = self._project({
            "Sales.tmdl": (
                "table Sales\n\n"
                "\tcolumn Amount\n\n"
                "\tmeasure Other = 1\n\n"
                "\tmeasure Total = SUM([Amount]) + 'Sales'[Amount] + [Other]\n"
            ),
            "Metrics.tmdl": "table Metrics\n\n\tmeasure Reserve = 1\n",
        }, "Sales", "Total")
        pre, source_block = self._block_bindings(mdef, "Total")
        self.assertEqual(pre, [
            ("column", "Sales", "Amount", False),
            ("column", "Sales", "Amount", False),
            ("measure", "Sales", "Other", False),
        ])
        move.move_measure(ws, project, "Total", "Metrics")
        fresh, block = self._block_bindings(mdef, "Total")
        self.assertEqual(block.rstrip("\n"), source_block.rstrip("\n"))
        destination = Path(mdef, "tables", "Metrics.tmdl").read_text(encoding="utf-8")
        self.assertNotIn("'Metrics'", destination)
        self.assertEqual(fresh, pre)

    def test_unrelated_measure_text_is_left_alone(self):
        ws, project, mdef = self._project({
            "Sales.tmdl": (
                "table Sales\n\n"
                "\tcolumn Amount\n\n"
                "\tmeasure Keeper = SUM([Amount]) * 2\n\n"
                "\tmeasure Total = SUM([Amount])\n"
            ),
            "Metrics.tmdl": "table Metrics\n\n\tcolumn Amount\n\n\tmeasure Other = 1\n",
        }, "Sales", "Total")
        pre, _ = self._block_bindings(mdef, "Total")
        self.assertEqual(pre, [("column", "Sales", "Amount", False)])
        keeper_pre, keeper_block = self._block_bindings(mdef, "Keeper")
        self.assertEqual(keeper_pre, [("column", "Sales", "Amount", False)])
        move.move_measure(ws, project, "Total", "Metrics")
        keeper_fresh, fresh_block = self._block_bindings(mdef, "Keeper")
        self.assertEqual(fresh_block, keeper_block)
        self.assertEqual(keeper_fresh, [("column", "Sales", "Amount", False)])

    def test_move_within_one_file_keeps_bindings(self):
        ws, project, mdef = self._project({
            "Model.tmdl": (
                "table Sales\n\n"
                "\tcolumn Amount\n\n"
                "\tmeasure Total = SUM([Amount])\n\n"
                "table Metrics\n\n"
                "\tcolumn Amount\n\n"
                "\tmeasure Other = 1\n"
            ),
        }, "Sales", "Total")
        pre, _ = self._block_bindings(mdef, "Total")
        self.assertEqual(pre, [("column", "Sales", "Amount", False)])
        move.move_measure(ws, project, "Total", "Metrics")
        fresh, block = self._block_bindings(mdef, "Total")
        self.assertIn("'Sales'[Amount]", block)
        self.assertEqual(fresh, pre)

    def test_qualification_quotes_a_spaced_table_name(self):
        ws, project, mdef = self._project({
            "Sales Data.tmdl": (
                "table 'Sales Data'\n\n"
                "\tcolumn Amount\n\n"
                "\tmeasure Total = SUM([Amount])\n"
            ),
            "Metrics.tmdl": "table Metrics\n\n\tcolumn Amount\n\n\tmeasure Other = 1\n",
        }, "Sales Data", "Total")
        pre, _ = self._block_bindings(mdef, "Total")
        self.assertEqual(pre, [("column", "Sales Data", "Amount", False)])
        move.move_measure(ws, project, "Total", "Metrics")
        fresh, block = self._block_bindings(mdef, "Total")
        self.assertIn("'Sales Data'[Amount]", block)
        self.assertEqual(fresh, pre)

    def test_flag_only_difference_heals_without_an_edit(self):
        ws, project, mdef = self._project({
            "Sales.tmdl": (
                "table Sales\n\n"
                "\tcolumn Qty\n\n"
                "\tcolumn Amount\n\n"
                "\tmeasure Total = SUMX(VALUES([Qty]), [Amount])\n"
            ),
            "Metrics.tmdl": "table Metrics\n\n\tmeasure Other = 1\n",
            "Alpha.tmdl": "table Alpha\n\n\tcolumn Qty\n",
            "Beta.tmdl": "table Beta\n\n\tcolumn Qty\n",
        }, "Sales", "Total")
        pre, _ = self._block_bindings(mdef, "Total")
        self.assertEqual(pre, [
            ("column", "Sales", "Qty", False),
            ("column", "Sales", "Amount", False),
        ])
        model = tmdl.load_model(mdef, root=os.path.dirname(mdef))
        total = model.measure("Total")
        doc = model.docs[total.node.file]
        region = next(reg for reg in doc.regions_for(total.node) if reg.kind == "dax")
        expr_text = doc.text[region.start:region.end]
        destination = deps.dax_bindings(expr_text, "Metrics", deps.Resolver(model))
        self.assertEqual(
            [(b.ref.kind, b.ref.table, b.ref.name, b.uncertain) for b in destination.bindings],
            [("unresolved", "", "Qty", True), ("column", "Sales", "Amount", True)],
        )
        move.move_measure(ws, project, "Total", "Metrics")
        fresh, block = self._block_bindings(mdef, "Total")
        self.assertIn("VALUES('Sales'[Qty])", block)
        self.assertIn("SUMX(VALUES('Sales'[Qty]), [Amount])", block)
        self.assertNotIn("'Sales'[Amount]", block)
        self.assertEqual(fresh, pre)

    def test_column_to_measure_cascade_leaves_the_victim_bare(self):
        ws, project, mdef = self._project({
            "Sales.tmdl": (
                "table Sales\n\n"
                "\tcolumn Qty\n\n"
                "\tcolumn Margin\n\n"
                "\tmeasure Total = SUMX(VALUES([Qty]), [Margin])\n"
            ),
            "Metrics.tmdl": "table Metrics\n\n\tcolumn Qty\n\n\tmeasure Margin = 1\n",
        }, "Sales", "Total")
        pre, _ = self._block_bindings(mdef, "Total")
        self.assertEqual(pre, [
            ("column", "Sales", "Qty", False),
            ("column", "Sales", "Margin", False),
        ])
        move.move_measure(ws, project, "Total", "Metrics")
        fresh, block = self._block_bindings(mdef, "Total")
        self.assertIn("VALUES('Sales'[Qty])", block)
        self.assertIn("SUMX(VALUES('Sales'[Qty]), [Margin])", block)
        self.assertNotIn("'Sales'[Margin]", block)
        self.assertEqual(fresh, pre)

    def test_kpi_target_expression_keeps_its_binding(self):
        ws, project, mdef = self._project({
            "Sales.tmdl": (
                "table Sales\n\n"
                "\tcolumn Amount\n\n"
                "\tmeasure Total = SUM([Amount])\n\n"
                "\t\tkpi\n"
                "\t\t\ttargetExpression = SUM([Amount])\n"
            ),
            "Metrics.tmdl": "table Metrics\n\n\tcolumn Amount\n\n\tmeasure Other = 1\n",
        }, "Sales", "Total")
        pre, _ = self._block_bindings(mdef, "Total")
        self.assertEqual(pre, [
            ("column", "Sales", "Amount", False),
            ("column", "Sales", "Amount", False),
        ])
        move.move_measure(ws, project, "Total", "Metrics")
        fresh, block = self._block_bindings(mdef, "Total")
        self.assertGreaterEqual(block.count("'Sales'[Amount]"), 2)
        self.assertEqual(fresh, pre)

    def test_stray_quote_expression_refuses_without_writes(self):
        ws, project, mdef = self._project({
            "Sales.tmdl": (
                "table Sales\n\n"
                "\tcolumn Qty\n\n"
                "\tcolumn Amount\n\n"
                "\tmeasure Total = SUM([Qty]) ' + [Amount]\n"
            ),
            "Metrics.tmdl": (
                "table Metrics\n\n"
                "\tcolumn Qty\n\n"
                "\tcolumn Amount\n\n"
                "\tmeasure Other = 1\n"
            ),
        }, "Sales", "Total")
        pre, _ = self._block_bindings(mdef, "Total")
        self.assertEqual(pre, [
            ("column", "Sales", "Qty", False),
            ("column", "Sales", "Amount", False),
        ])
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            move.move_measure(ws, project, "Total", "Metrics")
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(_bytes(ws.mirror), before)

    def test_foreign_qualifier_on_a_self_reference_is_left_alone(self):
        ws, project, mdef = self._project({
            "Sales.tmdl": (
                "table Sales\n\n"
                "\tcolumn Amount\n\n"
                "\tmeasure Total = SUM([Amount]) + 'Alpha'[Total] * 0\n"
            ),
            "Metrics.tmdl": "table Metrics\n\n\tmeasure Other = 1\n",
            "Alpha.tmdl": "table Alpha\n\n\tcolumn Qty\n",
        }, "Sales", "Total")
        pre, _ = self._block_bindings(mdef, "Total")
        self.assertEqual(pre, [
            ("column", "Sales", "Amount", False),
            ("measure", "Sales", "Total", False),
        ])
        move.move_measure(ws, project, "Total", "Metrics")
        fresh, block = self._block_bindings(mdef, "Total")
        self.assertIn("'Alpha'[Total]", block)
        self.assertEqual(self._normalized(fresh, "Total"), self._normalized(pre, "Total"))

    def test_two_insertions_in_one_region(self):
        ws, project, mdef = self._project({
            "Sales.tmdl": (
                "table Sales\n\n"
                "\tcolumn Qty\n\n"
                "\tcolumn Margin\n\n"
                "\tcolumn Amount\n\n"
                "\tmeasure Total = SUMX(VALUES([Qty]), [Margin]) + SUM([Amount])\n"
            ),
            "Metrics.tmdl": (
                "table Metrics\n\n"
                "\tcolumn Qty\n\n"
                "\tcolumn Amount\n\n"
                "\tmeasure Margin = 1\n"
            ),
        }, "Sales", "Total")
        pre, _ = self._block_bindings(mdef, "Total")
        self.assertEqual(pre, [
            ("column", "Sales", "Qty", False),
            ("column", "Sales", "Margin", False),
            ("column", "Sales", "Amount", False),
        ])
        move.move_measure(ws, project, "Total", "Metrics")
        fresh, block = self._block_bindings(mdef, "Total")
        self.assertIn("VALUES('Sales'[Qty])", block)
        self.assertIn("SUM('Sales'[Amount])", block)
        self.assertNotIn("'Sales'[Margin]", block)
        self.assertIn("SUMX(VALUES('Sales'[Qty]), [Margin]) + SUM('Sales'[Amount])", block)
        self.assertEqual(block.count("'Sales'"), 2)
        self.assertEqual(fresh, pre)


if __name__ == "__main__":
    unittest.main()
