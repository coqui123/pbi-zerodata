"""Offline checks that invalid filter and relationship requests write nothing.

Run: python tests/test_edit_refusals.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import cli, common, filters, move, props, rename, tmdl, unused  # noqa: E402
from pbz.common import PbzError  # noqa: E402

SALES = "table Sales\n\n\tcolumn Region\n\t\tdataType: string\n\n\tmeasure Amount = 1\n\n"
MODEL = "model Model\n\tculture: en-US\n\nref table Sales\n"
DATABASE = "database\n\tcompatibilityLevel: 1400\n"


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


class EditRefusalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        self.ws = common.Workspace(root)
        self.ws.private = os.path.join(root, "private")
        os.makedirs(self.ws.private)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        self.project = common.Project(self.ws, "Mini")
        mdef = os.path.join(self.project.mirror_model, "definition")
        _write(os.path.join(mdef, "tables", "Sales.tmdl"), SALES)
        _write(os.path.join(mdef, "model.tmdl"), MODEL)
        _write(os.path.join(mdef, "database.tmdl"), DATABASE)
        _write(os.path.join(self.project.mirror_report, "definition", "report.json"), "{}\n")
        self.model = tmdl.load_model(mdef)
        self.before = _bytes(self.ws.mirror)

    def _refuse(self, fn):
        with patch("pbz.common.write_text", side_effect=AssertionError("wrote")):
            with self.assertRaises(PbzError) as caught:
                fn()
        self.assertEqual(_bytes(self.ws.mirror), self.before)
        return caught.exception

    def test_sanitized_table_path_identity_refuses(self):
        mdef = os.path.join(self.project.mirror_model, "definition")
        tables = os.path.join(mdef, "tables")
        for old, new, filename in (("Sales", "sales", "Sales.tmdl"), ("A/B", "a?b", "A_B.tmdl")):
            with self.subTest(old=old, new=new):
                if old != "Sales":
                    os.rename(os.path.join(tables, "Sales.tmdl"), os.path.join(tables, filename))
                    _write(os.path.join(tables, filename), "table 'A/B'\n")
                    _write(os.path.join(mdef, "model.tmdl"), "model Model\n\nref table 'A/B'\n")
                self.before = _bytes(self.ws.mirror)
                err = self._refuse(lambda: rename.rename(self.ws, self.project, "table", old, new))
                self.assertEqual(err.code, "UNSUPPORTED")
                self.assertEqual(err.detail, "table rename requires a distinct sanitized file path")
                # Reopen the project and inspect directory entries, also on Windows.
                reopened = common.Project(self.ws, "Mini")
                reopened_tables = os.path.join(reopened.mirror_model, "definition", "tables")
                self.assertEqual(os.listdir(reopened_tables), [filename])

    def test_normal_table_rename_still_works(self):
        mdef = os.path.join(self.project.mirror_model, "definition")
        result = rename.rename(self.ws, self.project, "table", "Sales", "Accounts")
        self.assertEqual(result["to"], "Accounts")
        reopened = common.Project(self.ws, "Mini")
        self.assertEqual(os.listdir(os.path.join(reopened.mirror_model, "definition", "tables")), ["Accounts.tmdl"])
        self.assertEqual(set(tmdl.load_model(mdef).tables), {"Accounts"})

    def _fenced_fixture(self, eol, target_fenced=True, fence_style="flat", description_gap=""):
        mdef = os.path.join(self.project.mirror_model, "definition")
        def block(name, fenced):
            if not fenced:
                declaration = f"\tmeasure {name} = 1\n"
            elif fence_style == "property":
                declaration = (f"\tmeasure {name}\n"
                               "\t\tfilterExpression = ```\n\t\t\t1\n\t\t```\n")
            else:
                expr = ("```\n1\n```suffix\n\t```" if fence_style == "flat"
                        else "```\n\t\t1\n\t\t```")
                declaration = f"\tmeasure {name} = {expr}\n"
            return (f"\t/// {name} description\n{description_gap}{declaration}"
                    f"\t\tformatString: {name}\n"
                    f"\t\tannotation Note = {name}\n\n").replace("\n", eol)
        before, target, after = block("Before", True), block("Target", target_fenced), block("After", True)
        path = os.path.join(mdef, "tables", "Sales.tmdl")
        _write(path, "table Sales" + eol + eol + before + target + after)
        _write(os.path.join(mdef, "tables", "Destination.tmdl"), "table Destination" + eol + eol)
        _write(os.path.join(mdef, "model.tmdl"), MODEL + "ref table Destination\n")
        _write(os.path.join(self.project.mirror_report, "definition.pbir"),
               json.dumps({"datasetReference": {"byPath": {"path": "../Mini.SemanticModel"}}}))
        _write(os.path.join(self.project.mirror_report, "definition", "pages", "p1", "page.json"),
               json.dumps({"name": "p1", "displayName": "Synthetic"}))
        visual = {"visual": {"visualType": "card", "query": {"queryState": {"Values": {"projections": [
            {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": name}}}
            for name in ("Before", "After")
        ]}}}}}
        _write(os.path.join(self.project.mirror_report, "definition", "pages", "p1", "visuals", "v1", "visual.json"),
               json.dumps(visual))
        return path, before.encode(), target.encode(), after.encode()

    def test_fenced_siblings_are_disjoint_for_move_delete_and_property_edit(self):
        for eol in ("\n", "\r\n"):
            for fence_style in ("flat", "indented", "property"):
                for fenced in (False, True):
                    for action in ("move", "delete", "property"):
                        with self.subTest(eol=repr(eol), fence_style=fence_style, target_fenced=fenced, action=action):
                            self._check_fenced_edit(eol, fenced, action, fence_style)

    def test_fenced_description_across_blank_line_moves_and_deletes_with_target(self):
        for eol in ("\n", "\r\n"):
            for action in ("move", "delete"):
                with self.subTest(eol=repr(eol), action=action):
                    self._check_fenced_edit(eol, True, action, description_gap="\n")

    def _check_fenced_edit(self, eol, fenced, action, fence_style="flat", description_gap=""):
        path, before, target, after = self._fenced_fixture(eol, fenced, fence_style, description_gap)
        original = common.read_checked(path, root=self.ws.mirror)
        model = tmdl.load_model(os.path.dirname(os.path.dirname(path)))
        doc = model.docs["tables/Sales.tmdl"]
        self.assertFalse(doc.unclosed_fence)
        node = model.measure("Target").node
        if fenced and fence_style == "property":
            self.assertTrue(any(r.prop == "filterExpression" and r.node is node
                                for r in doc.regions_for(node)))
        if fenced and fence_style == "indented":
            closing = [line for line in target.decode().splitlines() if line.strip() == "```"]
            self.assertEqual(closing, ["\t\t```"])
        self.assertEqual(doc.text[slice(*tmdl.block_span(doc, node))].encode(), target)
        if action == "move":
            move.move_measure(self.ws, self.project, "Target", "Destination")
            dest = os.path.join(os.path.dirname(path), "Destination.tmdl")
            moved = common.read_checked(dest, root=self.ws.mirror)
            # Empty destination tables get a literal LF separator before the preserved block.
            self.assertEqual(moved, ("table Destination" + eol + eol + "\n").encode() + target)
        elif action == "delete":
            result = unused.remove(self.ws, self.project, only=["measure"])
            self.assertEqual(result["removed"], ["measure [Target]"])
            # Each subcase needs its own restore-point timestamp directory.
            shutil.rmtree(os.path.join(self.ws.mirror, unused.TRASH))
        else:
            props.run(self.ws, self.project, "[Target]", ["formatString=Changed"])
        changed = common.read_checked(path, root=self.ws.mirror)
        self.assertEqual(changed.count(before), 1)
        self.assertEqual(changed.count(after), 1)
        expected = (original.replace(target, target.replace(b"formatString: Target", b"formatString: Changed"))
                    if action == "property" else original.replace(target, b""))
        self.assertEqual(changed, expected)
        updated = tmdl.load_model(os.path.dirname(os.path.dirname(path)))
        after_node = updated.measure("After").node
        updated_doc = updated.docs[after_node.file]
        descriptions = [updated_doc.text[r.start:r.end] for r in updated_doc.regions_for(after_node)
                        if r.prop == "description"]
        self.assertEqual(descriptions, [" After description"])

    def test_property_insert_at_unterminated_eof_preserves_eol(self):
        from pbz.tokens import apply_edits
        for prefix, eol in (("table T\r\n", "\r\n"), ("table T\n", "\n"),
                            ("table T\n\r\n", "\r\n"), ("table T\r\n\n", "\n"), ("", "\n")):
            with self.subTest(prefix=repr(prefix)):
                text = prefix + "\tmeasure M = 7"
                doc = tmdl.parse("tables/T.tmdl", text)
                node = next(n for n in doc.nodes if n.kind == "measure")
                result = apply_edits(text, tmdl.prop_edit(doc, node, "displayFolder", "KPIs"))
                self.assertEqual(result.encode(),
                                 (text + eol + "\t\tdisplayFolder: KPIs" + eol).encode())

    def test_pending_description_before_sibling_property_does_not_expand_target_span(self):
        for eol in ("\n", "\r\n"):
            for action in ("move", "delete"):
                with self.subTest(eol=repr(eol), action=action):
                    path, *_ = self._fenced_fixture(eol)
                    prefix = ("table Sales\n\n\t/// Before description\n\tmeasure Before = 1\n"
                              "\t/// Target description\n\t\tformatString: Before\n").replace("\n", eol)
                    target = ("\tmeasure Target = 1\n\t\tformatString: Target\n\n").replace("\n", eol)
                    after = ("\t/// After description\n\tmeasure After = 1\n\n").replace("\n", eol)
                    _write(path, prefix + target + after)
                    doc = tmdl.parse("tables/Sales.tmdl", prefix + target + after)
                    node = next(n for n in doc.nodes if n.name == "Target")
                    self.assertEqual([doc.text[r.start:r.end] for r in doc.regions_for(node)
                                      if r.prop == "description"], [" Target description"])
                    if action == "move":
                        move.move_measure(self.ws, self.project, "Target", "Destination")
                        dest = os.path.join(os.path.dirname(path), "Destination.tmdl")
                        self.assertEqual(common.read_checked(dest, root=self.ws.mirror),
                                         ("table Destination" + eol + eol + "\n" + target).encode())
                    else:
                        result = unused.remove(self.ws, self.project, only=["measure"])
                        self.assertEqual(result["removed"], ["measure [Target]"])
                        shutil.rmtree(os.path.join(self.ws.mirror, unused.TRASH))
                    self.assertEqual(common.read_checked(path, root=self.ws.mirror),
                                     (prefix + after).encode())

    def test_move_refuses_unclosed_fence_in_third_file_without_writes(self):
        path, *_ = self._fenced_fixture("\n", target_fenced=False)
        third = os.path.join(os.path.dirname(path), "References.tmdl")
        _write(third, "table References\n\tmeasure Ref = ```\n\t\t'Sales'[Target]\n")
        doc = tmdl.parse("tables/References.tmdl", common.read_checked(third, root=self.ws.mirror).decode())
        self.assertTrue(doc.unclosed_fence)
        self.assertTrue(any(move.dax_qualifier_edits(doc.text, r.start, r.end,
                                                   "Target", "Sales", "Destination")
                            for r in doc.regions if r.kind == "dax"))
        self.before = _bytes(self.ws.mirror)
        err = self._refuse(lambda: move.move_measure(self.ws, self.project, "Target", "Destination"))
        self.assertEqual(err.code, "REFUSED")
        self.assertEqual(err.detail, "unclosed TMDL expression fence")

    def test_block_span_requires_parser_span_but_accepts_first_parsed_node(self):
        text = "function F = 1\n"
        doc = tmdl.parse("functions.tmdl", text)
        self.assertEqual(tmdl.block_span(doc, doc.nodes[0]), (0, len(text)))
        with self.assertRaises(PbzError) as caught:
            tmdl.block_span(doc, tmdl.Node("function", "F", 0, 0))
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(caught.exception.detail, "node has no parser span")

    def test_unclosed_fence_allows_parse_and_load_but_refuses_block_span(self):
        path, *_ = self._fenced_fixture("\r\n")
        text = "table Sales\r\n\tmeasure Target = ```\r\n1\r\n\tmeasure After = 2\r\n"
        _write(path, text)
        parsed = tmdl.parse("tables/Sales.tmdl", text)
        model = tmdl.load_model(os.path.dirname(os.path.dirname(path)))
        loaded = model.docs["tables/Sales.tmdl"]
        self.assertEqual(model.measure("Target").expression, "1\r\n\tmeasure After = 2\r\n")
        for doc in (parsed, loaded):
            with self.subTest(path=doc.path):
                self.assertTrue(doc.unclosed_fence)
                node = next(n for n in doc.nodes if n.kind == "measure")
                self.assertEqual(node.expr.end, len(text))
                with self.assertRaises(PbzError) as caught:
                    tmdl.block_span(doc, node)
                self.assertEqual(caught.exception.code, "REFUSED")
                self.assertEqual(caught.exception.detail, "unclosed TMDL expression fence")

    def test_unclosed_fence_refuses_mutations_without_writes(self):
        path, *_ = self._fenced_fixture("\r\n")
        _write(path, "table Sales\r\n\tmeasure Target = ```\r\n1\r\n\tmeasure After = 2\r\n")
        self.before = _bytes(self.ws.mirror)
        for action in (
            lambda: move.move_measure(self.ws, self.project, "Target", "Destination"),
            lambda: props.run(self.ws, self.project, "[Target]", ["formatString=Changed"]),
            lambda: unused.remove(self.ws, self.project, only=["measure"]),
        ):
            err = self._refuse(action)
            self.assertEqual(err.code, "REFUSED")
            self.assertEqual(err.detail, "unclosed TMDL expression fence")

    def test_fenced_block_at_eof_includes_properties_and_children(self):
        for eol in ("\n", "\r\n"):
            text = ("table Sales\n\tmeasure Target = ```\n1\n```suffix\n\t```\n"
                    "\t\tformatString: Target\n\t\tannotation Note = Target\n\n").replace("\n", eol)
            doc = tmdl.parse("tables/Sales.tmdl", text)
            node = next(n for n in doc.nodes if n.kind == "measure")
            start, end = tmdl.block_span(doc, node)
            self.assertEqual(start, len("table Sales" + eol))
            self.assertEqual(end, len(text))
            self.assertEqual(node.props["formatString"], "Target")
            self.assertEqual([child.kind for child in node.children], ["annotation"])

    def test_top_n_and_ranking_refuse(self):
        err = self._refuse(lambda: filters.add(
            self.ws, self.project, "'Sales'[Region]", report=True, top=0, by="[Amount]"))
        self.assertEqual(err.code, "BAD_REQUEST")
        self.assertNotIn("0", err.detail)
        err = self._refuse(lambda: filters.build(self.model, "'Sales'[Region]", top=-3, by="[Amount]"))
        self.assertNotIn("-3", err.detail)
        err = self._refuse(lambda: filters.build(self.model, "'Sales'[Region]", by="[Amount]"))
        self.assertIn("ranking", err.detail)
        err = self._refuse(lambda: filters.build(self.model, "'Sales'[Region]", bottom=True))
        self.assertIn("ranking", err.detail)

    def test_non_finite_and_bad_datetime_refuse(self):
        err = self._refuse(lambda: filters.literal("nan", "double"))
        self.assertNotIn("nan", err.detail.lower())
        err = self._refuse(lambda: filters.literal("inf", "decimal"))
        self.assertNotIn("inf", err.detail.lower())
        err = self._refuse(lambda: filters.literal("2020-02-31", "datetime"))
        self.assertNotIn("2020-02-31", err.detail)
        err = self._refuse(lambda: filters.build(self.model, "'Sales'[Region]", where=["nope"]))
        self.assertNotIn("nope", err.detail)

    def test_valid_values_still_work(self):
        self.assertEqual(filters.literal("«lit#1»", "int64"), "«lit#1»")
        self.assertEqual(filters.literal("1.5", "double"), "1.5D")
        self.assertTrue(filters.literal("2020-01-02", "datetime").startswith("datetime'"))
        built = filters.build(self.model, "'Sales'[Region]", top=3, by="[Amount]")
        self.assertEqual(built["type"], "TopN")
        added = filters.add(self.ws, self.project, "'Sales'[Region]", report=True, values_in=["East"])
        self.assertEqual(added["action"], "add")
        self.assertTrue(added["files"])

    def test_opposing_relationship_flags(self):
        parser = cli.argument_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["relationship", "set", "'A'[A]", "'B'[B]", "--active", "--inactive"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["relationship", "set", "'A'[A]", "'B'[B]", "--both", "--single"])
        args = parser.parse_args(["relationship", "set", "'A'[A]", "'B'[B]", "--active", "--single"])
        self.assertTrue(args.active)
        self.assertTrue(args.single)


if __name__ == "__main__":
    unittest.main()
