"""Offline checks that unused --remove / --restore commit one round at a time.

A failed round leaves that round's mirror and trash byte-identical. A later cascade
round that cannot finish reports PARTIAL and keeps a restore point for pre-command bytes.

Run: python tests/test_unused_atomic.py
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

from pbz import common, consumers, unused, usage  # noqa: E402
from pbz.common import PbzError  # noqa: E402
from pbz.outcomes import envelope  # noqa: E402

SALES = "table Sales\n\n\tmeasure Keep = 1\n\n\tmeasure Mid = [Leaf]\n\n\tmeasure Leaf = 1\n\n"
ORPHAN = "table Orphan\n\n\tmeasure Gone = 1\n\n"
MODEL = "model Model\n\tculture: en-US\n\nref table Sales\nref table Orphan\n"
DATABASE = "database\n\tcompatibilityLevel: 1400\n"
VISUAL = {"visual": {"visualType": "card", "query": {"queryState": {"Values": {"projections": [
    {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Keep"}}}
]}}}}}


def _bytes(root: str) -> dict[str, bytes]:
    out = {}
    for cur, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(cur, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, root)] = f.read()
    return out


def _write(path: str, data: str | bytes, *, bom: bool = False, newline: str = "\n") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(data, bytes):
        with open(path, "wb") as f:
            f.write(data)
        return
    raw = data.encode("utf-8")
    if bom:
        raw = b"\xef\xbb\xbf" + raw
    if newline != "\n":
        raw = raw.replace(b"\n", newline.encode("ascii"))
    with open(path, "wb") as f:
        f.write(raw)


class UnusedAtomicTests(unittest.TestCase):
    def _project(self, *, broken=False, bom=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        ws = common.Workspace(root)
        ws.private = os.path.join(root, "private")
        os.makedirs(ws.private)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        project = common.Project(ws, "Mini")
        mdef = os.path.join(project.mirror_model, "definition")
        sales = SALES.replace("\n", "\r\n") if bom else SALES
        _write(os.path.join(mdef, "tables", "Sales.tmdl"), sales, bom=bom)
        _write(os.path.join(mdef, "tables", "Orphan.tmdl"), ORPHAN)
        _write(os.path.join(mdef, "model.tmdl"), MODEL)
        _write(os.path.join(mdef, "database.tmdl"), DATABASE)
        report = project.mirror_report
        _write(os.path.join(report, "definition.pbir"),
               json.dumps({"datasetReference": {"byPath": {"path": "../Mini.SemanticModel"}}}))
        page = os.path.join(report, "definition", "pages", "p1")
        _write(os.path.join(page, "page.json"), json.dumps({"name": "p1", "displayName": "Synthetic"}))
        _write(os.path.join(page, "visuals", "v1", "visual.json"), "{" if broken else json.dumps(VISUAL))
        return ws, project

    def _sales(self, ws, project) -> str:
        return os.path.join(project.mirror_model, "definition", "tables", "Sales.tmdl")

    def test_m_tracker_graph_and_query_removal(self):
        ws, project = self._project()
        mdef = os.path.join(project.mirror_model, "definition")
        queries = {
            "Query": "1", "Other": "1", "Dead": "1", "UnusedSafe": "1", "Risk": "1",
            "Comparison": "if Query = Other then Query else Other",
            "Nested": "let x = (let Query = 1 in Query), y = Query in y",
            "Record": "let r = [Query = 1, x = Query], y = Query in y",
            "Case": "let query = 1 in Query",
            "NoEdge": "let Dead = 1 in Dead",
            "Uncertain": "try let Risk = 1 in Risk otherwise Risk",  # no consumer
        }
        _write(os.path.join(mdef, "expressions.tmdl"),
               "".join(f"expression {name} = {expr}\n\n" for name, expr in queries.items()))
        _write(self._sales(ws, project), SALES +
               "\tpartition Sales = m\n\t\tsource = {Comparison, Nested, Record, Case, NoEdge}\n")
        graph = usage.build(ws, project)
        query_key = lambda n: usage.key("m_query", "", n)
        self.assertEqual(graph.state[query_key("Query")], "used")
        for name in ("Comparison", "Nested", "Record", "Case"):
            self.assertIn(query_key("Query"), [e.dst for e in graph.out[query_key(name)]])
        self.assertNotIn(query_key("Dead"), [e.dst for e in graph.out.get(query_key("NoEdge"), [])])
        self.assertEqual(graph.state[query_key("UnusedSafe")], "unused")
        self.assertIn("uncertain M binding", " ".join(graph.locks[query_key("Risk")]))
        self.assertEqual(unused._removal(graph, query_key("Risk"), graph.model)[0], "manual")
        result = unused.remove(ws, project, only=["m_query"])
        self.assertEqual(result["result"], "REMOVED")
        from pbz import tmdl
        remaining = tmdl.load_model(mdef).expressions
        self.assertIn("Query", remaining)
        self.assertIn("Other", remaining)
        self.assertIn("Risk", remaining)
        self.assertNotIn("Dead", remaining)
        self.assertNotIn("UnusedSafe", remaining)

    def test_shared_bracket_only_query_survives_remove(self):
        ws, project = self._project()
        mdef = os.path.join(project.mirror_model, "definition")
        _write(os.path.join(mdef, "expressions.tmdl"),
               "expression SharedOnly = 1\n\nexpression GenuinelyUnused = 1\n")
        _write(self._sales(ws, project), SALES +
               "\tpartition Sales = m\n\t\tsource = #shared[SharedOnly]\n")
        graph = usage.build(ws, project)
        self.assertEqual(graph.state[usage.key("m_query", "", "SharedOnly")], "used")
        self.assertEqual(graph.state[usage.key("m_query", "", "GenuinelyUnused")], "unused")
        result = unused.remove(ws, project, only=["m_query"])
        self.assertEqual(result["result"], "REMOVED")
        from pbz import tmdl
        remaining = tmdl.load_model(mdef).expressions
        self.assertIn("SharedOnly", remaining)
        self.assertNotIn("GenuinelyUnused", remaining)

    def test_query_order_removal_is_case_exact(self):
        ws, project = self._project()
        mdef = Path(project.mirror_model, 'definition')
        queries = mdef / 'expressions.tmdl'
        queries.write_text('expression Source = 1\n\nexpression source = 2\n')
        model_path = mdef / 'model.tmdl'
        model_path.write_text(MODEL + '\nannotation PBI_QueryOrder = ["Source","source"]\n')
        _write(self._sales(ws, project), SALES +
               '\tpartition Sales = m\n\t\tsource = Source\n')
        graph = usage.build(ws, project)
        self.assertEqual(graph.state[usage.key('m_query', '', 'Source')], 'used')
        self.assertEqual(graph.state[usage.key('m_query', '', 'source')], 'unused')
        self.assertEqual(unused.remove(ws, project, only=['m_query'])['result'], 'REMOVED')
        from pbz import tmdl
        self.assertEqual(set(tmdl.load_model(str(mdef)).expressions), {'Source'})
        self.assertIn('expression Source = 1', queries.read_text())
        order = model_path.read_text().split('annotation PBI_QueryOrder = ', 1)[1].strip()
        self.assertEqual(json.loads(order), ['Source'])

    def test_failed_outcome_is_exit_1(self):
        out, rc = envelope({"ok": False, "error": "FAILED",
                            "detail": "write failed; mirror rolled back, no file changed"})
        self.assertEqual(rc, 1)
        self.assertEqual(out["outcome"]["status"], "FAILED")
        self.assertEqual(out["outcome"]["code"], "FAILED")

    def test_mid_write_rolls_back_the_round(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        real = common.write_text
        calls = {"n": 0}

        def boom(path, text, bom=b"", *, root=None, **_kw):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("injected")
            return real(path, text, bom, root=root)

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                unused.remove(ws, project, only=["measure"])
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertIn("no file changed", caught.exception.detail)
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])

    def test_manifest_write_rolls_back_the_round(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        real = common.write_text

        def boom(path, text, bom=b"", *, root=None, **_kw):
            if str(path).replace("\\", "/").endswith("manifest.json"):
                raise OSError("manifest")
            return real(path, text, bom, root=root)

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                unused.remove(ws, project, only=["measure"])
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])

    def test_mid_delete_rolls_back_the_round(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        real = common.write_text

        def boom(path, text, bom=b"", *, root=None, **_kw):
            if str(path).replace("\\", "/").endswith("model.tmdl"):
                raise OSError("ref")
            return real(path, text, bom, root=root)

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                unused.remove(ws, project, only=["table"])
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertTrue(os.path.isfile(os.path.join(project.mirror_model, "definition", "tables", "Orphan.tmdl")))
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])

    def test_restore_failure_rolls_back(self):
        ws, project = self._project()
        out = unused.remove(ws, project, only=["measure"])
        removed = _bytes(ws.mirror)
        real = common.write_text

        def boom(path, text, bom=b"", *, root=None, **_kw):
            raise OSError("restore")

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                unused.restore(ws, project, out["restore_point"])
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(_bytes(ws.mirror), removed)

    def test_conflict_refuses_before_write(self):
        ws, project = self._project()
        out = unused.remove(ws, project, only=["measure"])
        path = self._sales(ws, project)
        with open(path, "ab") as f:
            f.write(b"\n")
        changed = _bytes(ws.mirror)
        with patch("pbz.common.write_text", side_effect=AssertionError("wrote")):
            result = unused.restore(ws, project, out["restore_point"])
        self.assertEqual(result["result"], "CONFLICT")
        self.assertEqual(_bytes(ws.mirror), changed)

    def test_dry_run_writes_nothing(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        out = unused.remove(ws, project, only=["measure"], dry_run=True)
        self.assertEqual(out["result"], "DRY_RUN")
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertFalse(os.path.isdir(os.path.join(ws.mirror, ".pbz-trash")))

    def test_bom_crlf_round_trip(self):
        ws, project = self._project(bom=True)
        path = self._sales(ws, project)
        original = open(path, "rb").read()
        self.assertTrue(original.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", original)
        out = unused.remove(ws, project, only=["measure"])
        restored = unused.restore(ws, project, out["restore_point"])
        self.assertEqual(restored["result"], "RESTORED")
        self.assertEqual(open(path, "rb").read(), original)

    def test_incomplete_refuses_before_write(self):
        ws, project = self._project(broken=True)
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            unused.remove(ws, project, only=["measure"])
        self.assertEqual(caught.exception.code, "INCOMPLETE")
        self.assertIn("nothing removed", caught.exception.detail)
        self.assertEqual(_bytes(ws.mirror), before)

    def test_bad_stamp_and_corrupt_backup_refuse(self):
        ws, project = self._project()
        with self.assertRaises(PbzError) as caught:
            unused.restore(ws, project, "../stamp")
        self.assertEqual(caught.exception.code, "BAD_REQUEST")
        out = unused.remove(ws, project, only=["measure"])
        removed = _bytes(ws.mirror)
        stamp = out["restore_point"]
        backups = list(Path(ws.mirror, ".pbz-trash", stamp, "files").rglob("*.tmdl"))
        self.assertTrue(backups)
        backups[0].write_bytes(b"not-the-backup")
        mirror_now = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            unused.restore(ws, project, stamp)
        self.assertEqual(caught.exception.code, "INVALID")
        self.assertEqual(_bytes(ws.mirror), mirror_now)

    def _cascade(self, fail):
        ws, project = self._project()
        original = open(self._sales(ws, project), "rb").read()
        real_build = usage.build
        calls = {"n": 0}

        def build(*args, **kwargs):
            graph = real_build(*args, **kwargs)
            calls["n"] += 1
            if calls["n"] >= 2 and fail == "incomplete":
                graph.incomplete = True
            return graph

        real_commit = common.commit_bytes

        def commit(ops):
            if fail == "write" and calls["n"] >= 2:
                def boom(path, text, bom=b"", *, root=None, **_kw):
                    raise OSError("round2")
                with patch("pbz.common.write_text", boom):
                    return real_commit(ops)
            return real_commit(ops)

        with patch("pbz.usage.build", build), patch("pbz.unused.commit_bytes", commit):
            out = unused.remove(ws, project, only=["measure"], cascade=True)
        self.assertEqual(out["result"], "PARTIAL")
        self.assertNotIn("nothing removed", json.dumps(out))
        self.assertNotIn("no file changed", json.dumps(out))
        self.assertTrue(out["restore_point"])
        sales = open(self._sales(ws, project), "rb").read()
        self.assertNotIn(b"measure Mid", sales)
        self.assertIn(b"measure Leaf", sales)
        restored = unused.restore(ws, project, out["restore_point"])
        self.assertEqual(restored["result"], "RESTORED")
        self.assertEqual(open(self._sales(ws, project), "rb").read(), original)

    def test_cascade_incomplete_is_partial(self):
        self._cascade("incomplete")

    def test_cascade_write_failure_is_partial(self):
        self._cascade("write")


DEFINITE = "active filter path between used tables"
MANUAL = "possible filter path between used tables: filter direction does not propagate as modeled; manual review before removal"
STRUCTURAL = "relationship between used tables"
BOTH = "\tcrossFilteringBehavior: bothDirections"
INACTIVE = "\tisActive: false"


def _col(name, nested):
    return f"\tcolumn {name}\n\n{nested}"


def _table(name, columns, measures=(), tails=()):
    chunks = [f"table {name}\n"]
    for col in columns:
        if isinstance(col, str) and not col.startswith("\t"):
            col = f"\tcolumn {col}\n"
        chunks.append("\n" + col)
    for measure in measures:
        chunks.append(f"\n\tmeasure {measure} = 1\n")
    for tail in tails:
        chunks.append("\n" + tail)
    chunks.append("\n")
    return "".join(chunks)


def _rel(name, from_column, to_column, *props):
    lines = [
        f"relationship {name}",
        f"\tfromColumn: {from_column}",
        f"\ttoColumn: {to_column}",
        *props,
    ]
    return "\n".join(lines) + "\n"


def _join_rels(*blocks):
    return "\n".join(blocks)


def _reversed_rels(text):
    blocks = []
    for block in text.split("\n\n"):
        if block.strip():
            blocks.append(block if block.endswith("\n") else block + "\n")
    return "\n".join(reversed(blocks))


def _hierarchy(column):
    return f"\thierarchy H1\n\n\t\tlevel L1\n\t\t\tcolumn: {column}\n"


def _variation(hierarchy):
    return f"\t\tvariation\n\t\t\tdefaultHierarchy: {hierarchy}\n"


class UnusedFilterPathTests(unittest.TestCase):
    def _visual(self, used):
        projections = []
        for table, measure in used:
            projections.append({"field": {"Measure": {
                "Expression": {"SourceRef": {"Entity": table}},
                "Property": measure,
            }}})
        return {"visual": {"visualType": "card", "query": {"queryState": {"Values": {
            "projections": projections}}}}}

    def _build(self, tables, rels="", used=(), *, refs=None, extra=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        ws = common.Workspace(root)
        ws.private = os.path.join(root, "private")
        os.makedirs(ws.private)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        project = common.Project(ws, "Mini")
        mdef = os.path.join(project.mirror_model, "definition")
        for name, text in tables.items():
            _write(os.path.join(mdef, "tables", f"{name}.tmdl"), text)
        ref_names = list(tables) if refs is None else list(refs)
        model = "model Model\n\tculture: en-US\n\n" + "".join(
            f"ref table {name}\n" for name in ref_names)
        _write(os.path.join(mdef, "model.tmdl"), model)
        _write(os.path.join(mdef, "database.tmdl"), DATABASE)
        if rels:
            _write(os.path.join(mdef, "relationships.tmdl"), rels)
        report = project.mirror_report
        _write(os.path.join(report, "definition.pbir"),
               json.dumps({"datasetReference": {"byPath": {"path": "../Mini.SemanticModel"}}}))
        page = os.path.join(report, "definition", "pages", "p1")
        _write(os.path.join(page, "page.json"), json.dumps({"name": "p1", "displayName": "Synthetic"}))
        _write(os.path.join(page, "visuals", "v1", "visual.json"), json.dumps(self._visual(used)))
        if extra is not None:
            extra(ws, project)
        return ws, project, usage.build(ws, project)

    def _path(self, project, *parts):
        return os.path.join(project.mirror_model, "definition", *parts)

    def _relpath(self, *parts):
        return os.path.join("definition", *parts)

    def _read(self, project, *parts):
        with open(self._path(project, *parts), encoding="utf-8") as handle:
            return handle.read()

    def _expect(self, graph, state, rows):
        for kind, table, name in rows:
            key = usage.key(kind, table, name)
            self.assertEqual(graph.state[key], state, key)

    def _cols(self, *pairs):
        return [("column", table, name) for table, name in pairs]

    def _reasons(self, graph, kind, table, name):
        return graph.locks.get(usage.key(kind, table, name), [])

    def _assert_definite(self, graph, table):
        self._expect(graph, "locked", [("table", "", table)])
        reasons = self._reasons(graph, "table", "", table)
        self.assertIn(DEFINITE, reasons)
        self.assertNotIn(MANUAL, reasons)

    def _assert_manual(self, graph, table):
        self._expect(graph, "locked", [("table", "", table)])
        reasons = self._reasons(graph, "table", "", table)
        self.assertIn(MANUAL, reasons)
        self.assertNotIn(DEFINITE, reasons)

    def _assert_structural(self, graph, *names):
        self._expect(graph, "locked", [("relationship", "", name) for name in names])
        for name in names:
            self.assertIn(STRUCTURAL, self._reasons(graph, "relationship", "", name))

    def _s1(self, bridge_columns=("BAKey", "BGKey", "Note"), rels=None, used=None):
        tables = {
            "Alpha": _table("Alpha", ["AKey"], ["UsedA"]),
            "Bridge": _table("Bridge", list(bridge_columns)),
            "Gamma": _table("Gamma", ["GKey"], ["UsedG"]),
            "Orphan": _table("Orphan", [], ["Gone"]),
        }
        if rels is None:
            rels = _join_rels(
                _rel("rAB", "Bridge.BAKey", "Alpha.AKey"),
                _rel("rBG", "Gamma.GKey", "Bridge.BGKey"),
            )
        if used is None:
            used = (("Alpha", "UsedA"), ("Gamma", "UsedG"))
        return tables, rels, used

    def _assert_s1_endpoints(self, graph):
        self._assert_structural(graph, "rAB", "rBG")
        self._expect(graph, "locked", self._cols(
            ("Bridge", "BAKey"), ("Bridge", "BGKey"), ("Alpha", "AKey"), ("Gamma", "GKey")))

    def _assert_s1_rels_and_keys(self, graph):
        self._assert_s1_endpoints(graph)
        self._expect(graph, "unused", [("column", "Bridge", "Note")])

    def _assert_s1_full(self, graph):
        self._expect(graph, "used", [("table", "", "Alpha"), ("table", "", "Gamma")])
        self._assert_definite(graph, "Bridge")
        self._assert_s1_rels_and_keys(graph)
        self._expect(graph, "unused", [
            ("table", "", "Orphan"),
            ("measure", "Orphan", "Gone"),
        ])

    def _funnel(self):
        tables = {
            "P": _table("P", ["PKey"], ["UsedP"]),
            "Q": _table("Q", ["QKey", "QWKey"], ["UsedQ"]),
            "V": _table("V", ["VPKey", "VXKey", "VQKey"]),
            "X": _table("X", ["XVKey", "XWKey"]),
            "W": _table("W", ["WKey", "WQKey"]),
        }
        rels = _join_rels(
            _rel("rPV", "V.VPKey", "P.PKey", BOTH),
            _rel("rVX", "X.XVKey", "V.VXKey", BOTH),
            _rel("rVQ", "Q.QKey", "V.VQKey", BOTH),
            _rel("rWX", "X.XWKey", "W.WKey"),
            _rel("rWQ", "Q.QWKey", "W.WQKey"),
        )
        return tables, rels, (("P", "UsedP"), ("Q", "UsedQ"))

    def _assert_funnel(self, graph):
        self._assert_definite(graph, "V")
        self._assert_manual(graph, "X")
        self._assert_manual(graph, "W")
        self._assert_structural(graph, "rPV", "rVX", "rVQ", "rWX", "rWQ")
        self._expect(graph, "locked", self._cols(
            ("V", "VPKey"), ("P", "PKey"),
            ("X", "XVKey"), ("V", "VXKey"),
            ("Q", "QKey"), ("V", "VQKey"),
            ("X", "XWKey"), ("W", "WKey"),
            ("Q", "QWKey"), ("W", "WQKey"),
        ))

    def _limitation(self):
        tables = {
            "Alpha": _table("Alpha", [
                "AKey",
                _col("ADate", _variation("DateTableTemplate_Mid.H1")),
            ], ["UsedA"]),
            "DateTableTemplate_Mid": _table(
                "DateTableTemplate_Mid", ["MAKey", "MGKey", "Note"],
                tails=(_hierarchy("MAKey"),)),
            "Gamma": _table("Gamma", ["GKey"], ["UsedG"]),
            "Orphan": _table("Orphan", [], ["Gone"]),
        }
        rels = _join_rels(
            _rel("rAB", "DateTableTemplate_Mid.MAKey", "Alpha.AKey"),
            _rel("rBG", "Gamma.GKey", "DateTableTemplate_Mid.MGKey"),
        )
        return tables, rels, (("Alpha", "UsedA"), ("Gamma", "UsedG"))

    def _remove_adate(self, ws, project):
        out = unused.remove(ws, project, only=["column"])
        self.assertEqual(out["result"], "REMOVED")
        self.assertIn("column Alpha[ADate]", out["removed"])
        self.assertFalse(os.path.isfile(self._path(project, "tables", "DateTableTemplate_Mid.tmdl")))
        text = self._read(project, "relationships.tmdl")
        self.assertNotIn("relationship rAB", text)
        self.assertNotIn("relationship rBG", text)

    def test_s1_witness_states(self):
        _ws, _project, graph = self._build(*self._s1())
        self._assert_s1_full(graph)

    def test_s1_remove_and_restore(self):
        ws, project, _graph = self._build(*self._s1())
        dry = unused.remove(ws, project, dry_run=True)
        self.assertEqual(dry["result"], "DRY_RUN")
        self.assertEqual(set(dry["removed"]), {"table Orphan", "column Bridge[Note]"})
        snap = _bytes(project.mirror_model)
        out = unused.remove(ws, project)
        self.assertEqual(out["result"], "REMOVED")
        self.assertEqual(set(out["removed"]), {"table Orphan", "column Bridge[Note]"})
        self.assertFalse(os.path.isfile(self._path(project, "tables", "Orphan.tmdl")))
        bridge = self._read(project, "tables", "Bridge.tmdl")
        self.assertIn("column BAKey", bridge)
        self.assertIn("column BGKey", bridge)
        self.assertNotIn("column Note", bridge)
        now = _bytes(project.mirror_model)
        for rel in (
            self._relpath("relationships.tmdl"),
            self._relpath("tables", "Alpha.tmdl"),
            self._relpath("tables", "Gamma.tmdl"),
        ):
            self.assertEqual(now[rel], snap[rel])
        restored = unused.restore(ws, project, out["restore_point"])
        self.assertEqual(restored["result"], "RESTORED")
        self.assertEqual(_bytes(project.mirror_model), snap)

    def test_s1_payload_free_remove(self):
        ws, project, _graph = self._build(*self._s1(bridge_columns=("BAKey", "BGKey")))
        snap = _bytes(project.mirror_model)
        out = unused.remove(ws, project)
        self.assertEqual(set(out["removed"]), {"table Orphan"})
        now = _bytes(project.mirror_model)
        self.assertEqual(now[self._relpath("tables", "Bridge.tmdl")], snap[self._relpath("tables", "Bridge.tmdl")])
        self.assertEqual(now[self._relpath("relationships.tmdl")], snap[self._relpath("relationships.tmdl")])
        restored = unused.restore(ws, project, out["restore_point"])
        self.assertEqual(restored["result"], "RESTORED")
        self.assertEqual(_bytes(project.mirror_model), snap)

    def test_s2_direction_manual_review(self):
        tables, _base, used = self._s1()
        rels = _join_rels(
            _rel("rAB", "Bridge.BAKey", "Alpha.AKey"),
            _rel("rBG", "Bridge.BGKey", "Gamma.GKey"),
        )
        _ws, _project, graph = self._build(tables, rels, used)
        self._assert_manual(graph, "Bridge")
        self._assert_s1_rels_and_keys(graph)

    def test_s2b_both_reversed_definite(self):
        tables, _base, used = self._s1()
        rels = _join_rels(
            _rel("rAB", "Alpha.AKey", "Bridge.BAKey"),
            _rel("rBG", "Bridge.BGKey", "Gamma.GKey"),
        )
        _ws, _project, graph = self._build(tables, rels, used)
        self._assert_definite(graph, "Bridge")
        self._assert_s1_rels_and_keys(graph)

    def test_s2c_both_directions_restores_definite(self):
        tables, _base, used = self._s1()
        rels = _join_rels(
            _rel("rAB", "Bridge.BAKey", "Alpha.AKey"),
            _rel("rBG", "Bridge.BGKey", "Gamma.GKey", BOTH),
        )
        _ws, _project, graph = self._build(tables, rels, used)
        self._assert_definite(graph, "Bridge")
        self._assert_s1_rels_and_keys(graph)

    def test_s3_inactive_leg(self):
        tables, _base, used = self._s1()
        rels = _join_rels(
            _rel("rAB", "Bridge.BAKey", "Alpha.AKey"),
            _rel("rBG", "Gamma.GKey", "Bridge.BGKey", INACTIVE),
        )
        _ws, _project, graph = self._build(tables, rels, used)
        self._expect(graph, "unused", [
            ("table", "", "Bridge"),
            ("relationship", "", "rAB"),
            ("relationship", "", "rBG"),
            ("column", "Bridge", "Note"),
        ])
        self._expect(graph, "used_by_unused", self._cols(
            ("Bridge", "BAKey"), ("Bridge", "BGKey"), ("Alpha", "AKey"), ("Gamma", "GKey")))

    def test_s4_dead_end_branch(self):
        tables = {
            "Alpha": _table("Alpha", ["AKey"], ["UsedA"]),
            "Gamma": _table("Gamma", ["GKey"], ["UsedG"]),
            "D1": _table("D1", ["D1AKey", "D1Key"]),
            "D2": _table("D2", ["D2Key"]),
        }
        rels = _join_rels(
            _rel("rAD1", "D1.D1AKey", "Alpha.AKey"),
            _rel("rD1D2", "D2.D2Key", "D1.D1Key"),
        )
        _ws, _project, graph = self._build(tables, rels, (("Alpha", "UsedA"), ("Gamma", "UsedG")))
        self._expect(graph, "unused", [
            ("table", "", "D1"),
            ("table", "", "D2"),
            ("relationship", "", "rAD1"),
            ("relationship", "", "rD1D2"),
        ])
        self._expect(graph, "used_by_unused", self._cols(
            ("D1", "D1AKey"), ("D1", "D1Key"), ("D2", "D2Key"), ("Alpha", "AKey")))

    def test_s4b_articulation(self):
        tables = {
            "Alpha": _table("Alpha", ["AKeyR", "AKeyD", "AKeyX", "AKeyT"], ["UsedA"]),
            "Gamma": _table("Gamma", ["GKeyR"], ["UsedG"]),
            "D1": _table("D1", ["D1AKey", "D1Key"]),
            "D2": _table("D2", ["D2Key"]),
            "X": _table("X", ["XAKey", "XKey"]),
            "Y": _table("Y", ["YKey", "YAKey"]),
        }
        rels = _join_rels(
            _rel("rAG", "Gamma.GKeyR", "Alpha.AKeyR"),
            _rel("rAD1", "D1.D1AKey", "Alpha.AKeyD"),
            _rel("rD1D2", "D2.D2Key", "D1.D1Key"),
            _rel("rAX", "X.XAKey", "Alpha.AKeyX"),
            _rel("rXY", "Y.YKey", "X.XKey"),
            _rel("rYA", "Alpha.AKeyT", "Y.YAKey"),
        )
        _ws, _project, graph = self._build(tables, rels, (("Alpha", "UsedA"), ("Gamma", "UsedG")))
        self._assert_structural(graph, "rAG")
        self._expect(graph, "locked", self._cols(("Alpha", "AKeyR"), ("Gamma", "GKeyR")))
        self._expect(graph, "unused", [
            ("table", "", "D1"),
            ("table", "", "D2"),
            ("table", "", "X"),
            ("table", "", "Y"),
            ("relationship", "", "rAD1"),
            ("relationship", "", "rD1D2"),
            ("relationship", "", "rAX"),
            ("relationship", "", "rXY"),
            ("relationship", "", "rYA"),
        ])
        self._expect(graph, "used_by_unused", self._cols(
            ("D1", "D1AKey"), ("D1", "D1Key"), ("D2", "D2Key"), ("Alpha", "AKeyD"),
            ("X", "XAKey"), ("X", "XKey"), ("Y", "YKey"), ("Y", "YAKey"),
            ("Alpha", "AKeyX"), ("Alpha", "AKeyT"),
        ))

    def test_s5_parallel_paths(self):
        tables = {
            "Alpha": _table("Alpha", ["AKey1", "AKey2"], ["UsedA"]),
            "Gamma": _table("Gamma", ["GKey1", "GKey2"], ["UsedG"]),
            "B1": _table("B1", ["B1AKey", "B1GKey", "Note1"]),
            "B2": _table("B2", ["B2AKey", "B2GKey", "Note2"]),
        }
        rels = _join_rels(
            _rel("rAB1", "B1.B1AKey", "Alpha.AKey1"),
            _rel("rB1G", "Gamma.GKey1", "B1.B1GKey"),
            _rel("rAB2", "B2.B2AKey", "Alpha.AKey2"),
            _rel("rB2G", "Gamma.GKey2", "B2.B2GKey"),
        )
        _ws, _project, graph = self._build(tables, rels, (("Alpha", "UsedA"), ("Gamma", "UsedG")))
        self._assert_definite(graph, "B1")
        self._assert_definite(graph, "B2")
        self._assert_structural(graph, "rAB1", "rB1G", "rAB2", "rB2G")
        self._expect(graph, "locked", self._cols(
            ("B1", "B1AKey"), ("B1", "B1GKey"),
            ("B2", "B2AKey"), ("B2", "B2GKey"),
            ("Alpha", "AKey1"), ("Gamma", "GKey1"),
            ("Alpha", "AKey2"), ("Gamma", "GKey2"),
        ))
        self._expect(graph, "unused", [("column", "B1", "Note1"), ("column", "B2", "Note2")])

    def test_s6a_cycle_on_path(self):
        tables = {
            "Alpha": _table("Alpha", ["AKey", "AKey2"], ["UsedA"]),
            "Gamma": _table("Gamma", ["GKey", "GKey2"], ["UsedG"]),
            "X": _table("X", ["XAKey", "XKey"]),
            "Y": _table("Y", ["YKey", "YGKey"]),
        }
        rels = _join_rels(
            _rel("rAX", "X.XAKey", "Alpha.AKey"),
            _rel("rXY", "Y.YKey", "X.XKey"),
            _rel("rYG", "Gamma.GKey", "Y.YGKey"),
            _rel("rGA", "Alpha.AKey2", "Gamma.GKey2"),
        )
        _ws, _project, graph = self._build(tables, rels, (("Alpha", "UsedA"), ("Gamma", "UsedG")))
        self._assert_definite(graph, "X")
        self._assert_definite(graph, "Y")
        self._assert_structural(graph, "rAX", "rXY", "rYG", "rGA")
        self._expect(graph, "locked", self._cols(
            ("X", "XAKey"), ("Alpha", "AKey"),
            ("X", "XKey"), ("Y", "YKey"),
            ("Y", "YGKey"), ("Gamma", "GKey"),
            ("Alpha", "AKey2"), ("Gamma", "GKey2"),
        ))

    def test_s6b_lollipop(self):
        tables = {
            "Anchor": _table("Anchor", ["AKey"], ["UsedAn"]),
            "S": _table("S", ["SKey", "SXKey", "SYKey"]),
            "X": _table("X", ["XKey", "XYKey"]),
            "Y": _table("Y", ["YKey", "YRKey"]),
        }
        rels = _join_rels(
            _rel("rAS", "S.SKey", "Anchor.AKey"),
            _rel("rSX", "X.XKey", "S.SXKey"),
            _rel("rXY", "Y.YKey", "X.XYKey"),
            _rel("rYS", "S.SYKey", "Y.YRKey"),
        )
        _ws, _project, graph = self._build(tables, rels, (("Anchor", "UsedAn"),))
        self._expect(graph, "unused", [
            ("table", "", "S"),
            ("table", "", "X"),
            ("table", "", "Y"),
            ("relationship", "", "rAS"),
            ("relationship", "", "rSX"),
            ("relationship", "", "rXY"),
            ("relationship", "", "rYS"),
        ])
        self._expect(graph, "used_by_unused", self._cols(
            ("S", "SKey"), ("Anchor", "AKey"),
            ("X", "XKey"), ("S", "SXKey"),
            ("Y", "YKey"), ("X", "XYKey"),
            ("S", "SYKey"), ("Y", "YRKey"),
        ))

    def test_s7_multi_hop_bridge(self):
        tables = {
            "Alpha": _table("Alpha", ["AKey"], ["UsedA"]),
            "B1": _table("B1", ["B1AKey", "B1GKey"]),
            "B2": _table("B2", ["B2Key", "B2GKey"]),
            "Gamma": _table("Gamma", ["GKey"], ["UsedG"]),
        }
        rels = _join_rels(
            _rel("rAB1", "B1.B1AKey", "Alpha.AKey"),
            _rel("rB1B2", "B2.B2Key", "B1.B1GKey"),
            _rel("rB2G", "Gamma.GKey", "B2.B2GKey"),
        )
        _ws, _project, graph = self._build(tables, rels, (("Alpha", "UsedA"), ("Gamma", "UsedG")))
        self._assert_definite(graph, "B1")
        self._assert_definite(graph, "B2")
        self._assert_structural(graph, "rAB1", "rB1B2", "rB2G")
        self._expect(graph, "locked", self._cols(
            ("B1", "B1AKey"), ("Alpha", "AKey"),
            ("B2", "B2Key"), ("B1", "B1GKey"),
            ("Gamma", "GKey"), ("B2", "B2GKey"),
        ))

    def test_s8_same_endpoint_trap(self):
        tables = {
            "Alpha": _table("Alpha", ["AKey"], ["UsedA"]),
            "Gamma": _table("Gamma", ["GKey"], ["UsedG"]),
            "Bridge": _table("Bridge", ["BAKey", "BMKey"]),
            "M": _table("M", ["MKey", "MGKey"]),
        }
        rels = _join_rels(
            _rel("rAB", "Bridge.BAKey", "Alpha.AKey", BOTH),
            _rel("rMB", "Bridge.BMKey", "M.MKey"),
            _rel("rMG", "Gamma.GKey", "M.MGKey"),
        )
        _ws, _project, graph = self._build(tables, rels, (("Alpha", "UsedA"), ("Gamma", "UsedG")))
        self._assert_manual(graph, "Bridge")
        self._assert_manual(graph, "M")
        self._assert_structural(graph, "rAB", "rMB", "rMG")
        self._expect(graph, "locked", self._cols(
            ("Bridge", "BAKey"), ("Alpha", "AKey"),
            ("Bridge", "BMKey"), ("M", "MKey"),
            ("Gamma", "GKey"), ("M", "MGKey"),
        ))

    def test_s8b_cycle_trap(self):
        tables = {
            "Alpha": _table("Alpha", ["AKey", "ARKey"], ["UsedA"]),
            "Gamma": _table("Gamma", ["GKey", "GRKey"], ["UsedG"]),
            "Bridge": _table("Bridge", ["BAKey", "BMKey"]),
            "M": _table("M", ["MKey", "MGKey"]),
        }
        rels = _join_rels(
            _rel("rAB", "Bridge.BAKey", "Alpha.AKey", BOTH),
            _rel("rMB", "Bridge.BMKey", "M.MKey"),
            _rel("rMG", "Gamma.GKey", "M.MGKey"),
            _rel("rAG", "Gamma.GRKey", "Alpha.ARKey"),
        )
        _ws, _project, graph = self._build(tables, rels, (("Alpha", "UsedA"), ("Gamma", "UsedG")))
        self._assert_manual(graph, "Bridge")
        self._assert_manual(graph, "M")
        self._assert_structural(graph, "rAB", "rMB", "rMG", "rAG")
        self._expect(graph, "locked", self._cols(
            ("Bridge", "BAKey"), ("Alpha", "AKey"),
            ("Bridge", "BMKey"), ("M", "MKey"),
            ("Gamma", "GKey"), ("M", "MGKey"),
            ("Gamma", "GRKey"), ("Alpha", "ARKey"),
        ))

    def test_s8c_funnel_trap(self):
        _ws, _project, graph = self._build(*self._funnel())
        self._assert_funnel(graph)

    def test_s9_used_external_endpoint(self):
        tables, rels, _used = self._s1()

        def extra(ws, project):
            consumers.add(ws, project, objects=["'Gamma'[UsedG]"], label="Board pack")

        _ws, _project, graph = self._build(
            tables, rels, (("Alpha", "UsedA"),), extra=extra)
        self._expect(graph, "used_external", [
            ("table", "", "Gamma"),
            ("measure", "Gamma", "UsedG"),
        ])
        self._assert_definite(graph, "Bridge")
        self._assert_s1_rels_and_keys(graph)

    def test_s10_pre_locked_endpoint(self):
        tables = {
            "Alpha": _table("Alpha", ["AKey"], ["UsedA"]),
            "Bridge": _table("Bridge", ["BAKey", "BGKey"]),
            "Gamma": _table(
                "Gamma", ["GKey"], ["UnusedG"],
                tails=("\trefreshPolicy\n\t\tincrementalGranularity: day\n",)),
        }
        rels = _join_rels(
            _rel("rAB", "Bridge.BAKey", "Alpha.AKey"),
            _rel("rBG", "Gamma.GKey", "Bridge.BGKey"),
        )
        _ws, _project, graph = self._build(tables, rels, (("Alpha", "UsedA"),))
        gamma = usage.key("table", "", "Gamma")
        self._expect(graph, "locked", [("table", "", "Gamma")])
        self.assertIn("incremental refresh policy", graph.locks[gamma])
        self._expect(graph, "unused", [("measure", "Gamma", "UnusedG")])
        self._assert_definite(graph, "Bridge")
        self._assert_s1_endpoints(graph)

    def test_s11a_private_leaf(self):
        tables = {
            "Alpha": _table("Alpha", ["AKey", "PrivKey"], ["UsedA"]),
            "LocalDateTable_Priv": _table("LocalDateTable_Priv", ["PKey"]),
        }
        rels = _rel("rPriv", "LocalDateTable_Priv.PKey", "Alpha.PrivKey")
        _ws, _project, graph = self._build(tables, rels, (("Alpha", "UsedA"),))
        self._expect(graph, "unused", [
            ("table", "", "LocalDateTable_Priv"),
            ("relationship", "", "rPriv"),
            ("column", "LocalDateTable_Priv", "PKey"),
            ("column", "Alpha", "PrivKey"),
        ])

    def test_s11_private_leaf_on_retained(self):
        tables, _base, used = self._s1()
        tables["Bridge"] = _table("Bridge", ["BAKey", "BGKey", "Note", "BPKey"])
        tables["LocalDateTable_Priv"] = _table("LocalDateTable_Priv", ["PKey"])
        rels = _join_rels(
            _rel("rAB", "Bridge.BAKey", "Alpha.AKey"),
            _rel("rBG", "Gamma.GKey", "Bridge.BGKey"),
            _rel("rBP", "LocalDateTable_Priv.PKey", "Bridge.BPKey"),
        )
        _ws, _project, graph = self._build(tables, rels, used)
        self._expect(graph, "unused", [
            ("table", "", "LocalDateTable_Priv"),
            ("relationship", "", "rBP"),
            ("column", "LocalDateTable_Priv", "PKey"),
            ("column", "Bridge", "BPKey"),
        ])
        self._assert_definite(graph, "Bridge")
        self._assert_s1_endpoints(graph)

    def test_s12_private_intermediate(self):
        tables = {
            "Alpha": _table("Alpha", ["AKey"], ["UsedA"]),
            "DateTableTemplate_Mid": _table("DateTableTemplate_Mid", ["MAKey", "MGKey", "Note"]),
            "Gamma": _table("Gamma", ["GKey"], ["UsedG"]),
            "Orphan": _table("Orphan", [], ["Gone"]),
        }
        rels = _join_rels(
            _rel("rAB", "DateTableTemplate_Mid.MAKey", "Alpha.AKey"),
            _rel("rBG", "Gamma.GKey", "DateTableTemplate_Mid.MGKey"),
        )
        _ws, _project, graph = self._build(tables, rels, (("Alpha", "UsedA"), ("Gamma", "UsedG")))
        self._assert_definite(graph, "DateTableTemplate_Mid")
        self._assert_structural(graph, "rAB", "rBG")
        self._expect(graph, "locked", self._cols(
            ("DateTableTemplate_Mid", "MAKey"),
            ("DateTableTemplate_Mid", "MGKey"),
            ("Alpha", "AKey"),
            ("Gamma", "GKey"),
        ))
        self._expect(graph, "unused", [
            ("column", "DateTableTemplate_Mid", "Note"),
            ("table", "", "Orphan"),
        ])

    def test_s13_cascade(self):
        tables = {
            "Alpha": _table("Alpha", ["AKey"], ["UsedA"]),
            "Bridge": _table("Bridge", ["BAKey", "BGKey"]),
            "Gamma": _table("Gamma", [
                _col("GKey", _variation("Delta.H1")),
                "GXKey",
            ], ["UsedG"]),
            "Delta": _table("Delta", ["DKey"], tails=(_hierarchy("DKey"),)),
            "X": _table("X", ["XDKey", "XGKey"]),
            "Omega": _table("Omega", ["OKey"], ["UsedO"]),
        }
        rels = _join_rels(
            _rel("rAB", "Bridge.BAKey", "Alpha.AKey"),
            _rel("rBG", "Gamma.GKey", "Bridge.BGKey"),
            _rel("rXD", "X.XDKey", "Delta.DKey"),
            _rel("rXG", "Gamma.GXKey", "X.XGKey"),
        )
        _ws, _project, graph = self._build(
            tables, rels, (("Alpha", "UsedA"), ("Gamma", "UsedG"), ("Omega", "UsedO")))
        self._assert_definite(graph, "Bridge")
        self._expect(graph, "locked", [("table", "", "Delta")])
        self._assert_definite(graph, "X")
        self._assert_structural(graph, "rXD", "rXG")
        self._expect(graph, "locked", self._cols(
            ("Delta", "DKey"), ("X", "XDKey"), ("X", "XGKey"), ("Gamma", "GXKey")))
        self._expect(graph, "unused", [("hierarchy", "Delta", "H1")])

    def test_s13b_reason_stability(self):
        tables = {
            "Alpha": _table("Alpha", ["AKey"], ["UsedA"]),
            "Bridge": _table("Bridge", ["BAKey", "BGKey", "BDKey"]),
            "Gamma": _table("Gamma", [_col("GKey", _variation("Delta.H1"))], ["UsedG"]),
            "Delta": _table("Delta", ["DKey"], tails=(_hierarchy("DKey"),)),
        }
        rels = _join_rels(
            _rel("rAB", "Bridge.BAKey", "Alpha.AKey"),
            _rel("rBG", "Bridge.BGKey", "Gamma.GKey"),
            _rel("rBD", "Delta.DKey", "Bridge.BDKey"),
        )
        _ws, _project, graph = self._build(tables, rels, (("Alpha", "UsedA"), ("Gamma", "UsedG")))
        self._assert_manual(graph, "Bridge")
        self._expect(graph, "locked", [("table", "", "Delta")])
        self._assert_structural(graph, "rBD")
        self._expect(graph, "locked", self._cols(
            ("Bridge", "BAKey"), ("Bridge", "BGKey"), ("Bridge", "BDKey"), ("Delta", "DKey")))

    def test_s14_incomplete_analysis(self):
        def extra(ws, _project):
            _write(os.path.join(ws.mirror, "Other.Report", "definition.pbir"), "{not json")

        _ws, _project, graph = self._build(*self._s1(), extra=extra)
        self.assertTrue(graph.incomplete)
        self._assert_definite(graph, "Bridge")
        self._assert_structural(graph, "rAB", "rBG")
        self._expect(graph, "locked", self._cols(
            ("Bridge", "BAKey"), ("Bridge", "BGKey"), ("Alpha", "AKey"), ("Gamma", "GKey")))
        self._expect(graph, "unknown", [
            ("column", "Bridge", "Note"),
            ("table", "", "Orphan"),
        ])

    def test_s15_order_independence(self):
        tables, rels, used = self._s1()
        _ws, _project, forward = self._build(tables, rels, used)
        _ws, _project, backward = self._build(
            tables, _reversed_rels(rels), used,
            refs=["Orphan", "Gamma", "Bridge", "Alpha"])
        self._assert_s1_full(forward)
        self._assert_s1_full(backward)
        self.assertEqual(forward.state, backward.state)
        self.assertEqual(forward.locks, backward.locks)

    def test_s16_budget_zero(self):
        tables, rels, used = self._funnel()
        _ws, _project, graph = self._build(tables, rels, used)
        self._expect(graph, "locked", [
            ("table", "", "V"), ("table", "", "X"), ("table", "", "W")])
        with patch.object(usage, "_PATH_REASON_BUDGET", 0):
            _ws, _project, forward = self._build(tables, rels, used)
            _ws, _project, backward = self._build(tables, _reversed_rels(rels), used)
            for built in (forward, backward):
                self._assert_manual(built, "V")
                self._assert_manual(built, "X")
                self._assert_manual(built, "W")
            self.assertEqual(forward.state, backward.state)
            for name in ("V", "X", "W"):
                key = usage.key("table", "", name)
                self.assertEqual(forward.locks[key], backward.locks[key])

    def test_s16b_positive_budget_determinism(self):
        tables = {
            "A": _table("A", ["AKey"], ["UsedA"]),
            "X1": _table("X1", ["X1AKey", "X1BKey"]),
            "B": _table("B", ["BKey"], ["UsedB"]),
            "C": _table("C", ["CKey"], ["UsedC"]),
            "X2": _table("X2", ["X2CKey", "X2DKey"]),
            "D": _table("D", ["DKey"], ["UsedD"]),
        }
        rels = _join_rels(
            _rel("rAX1", "X1.X1AKey", "A.AKey"),
            _rel("rX1B", "B.BKey", "X1.X1BKey"),
            _rel("rCX2", "X2.X2CKey", "C.CKey"),
            _rel("rX2D", "D.DKey", "X2.X2DKey"),
        )
        used = (("A", "UsedA"), ("B", "UsedB"), ("C", "UsedC"), ("D", "UsedD"))
        _ws, _project, base = self._build(tables, rels, used)
        self._expect(base, "locked", [("table", "", "X1"), ("table", "", "X2")])
        self._assert_definite(base, "X1")
        self._assert_definite(base, "X2")
        with patch.object(usage, "_PATH_REASON_BUDGET", 4):
            _ws, _project, graph = self._build(tables, rels, used)
            self._assert_definite(graph, "X1")
            self._assert_definite(graph, "X2")
            self.assertEqual(graph.state, base.state)
        with patch.object(usage, "_PATH_REASON_BUDGET", 3):
            _ws, _project, graph = self._build(tables, rels, used)
            self._assert_definite(graph, "X1")
            self._assert_manual(graph, "X2")
            self.assertEqual(graph.state, base.state)
            _ws, _project, rev = self._build(tables, _reversed_rels(rels), used,
                                             refs=["D", "X2", "C", "B", "X1", "A"])
            self._assert_definite(rev, "X1")
            self._assert_manual(rev, "X2")
            self.assertEqual(rev.state, base.state)
        with patch.object(usage, "_PATH_REASON_BUDGET", 0):
            _ws, _project, graph = self._build(tables, rels, used)
            self._assert_manual(graph, "X1")
            self._assert_manual(graph, "X2")
            self.assertEqual(graph.state, base.state)

    def test_s17a_limitation(self):
        ws, project, graph = self._build(*self._limitation())
        self._expect(graph, "unused", [("column", "Alpha", "ADate")])
        self._remove_adate(ws, project)

    def test_s17b_limitation_under_retention(self):
        ws, project, graph = self._build(*self._limitation())
        self._assert_definite(graph, "DateTableTemplate_Mid")
        self._assert_structural(graph, "rAB", "rBG")
        self._expect(graph, "locked", self._cols(
            ("DateTableTemplate_Mid", "MAKey"),
            ("DateTableTemplate_Mid", "MGKey"),
            ("Alpha", "AKey"),
            ("Gamma", "GKey"),
        ))
        self._expect(graph, "unused", [("column", "Alpha", "ADate")])
        self._remove_adate(ws, project)


if __name__ == "__main__":
    unittest.main()
