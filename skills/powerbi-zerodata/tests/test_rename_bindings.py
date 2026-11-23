"""Offline regression tests for the shared rename bindings and atomic refusals."""
from __future__ import annotations

import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.dirname(__file__))

from pbz import consolidate, deps, rename, tmdl, pbir, usage, unused
from pbz.common import PbzError
from pbz.tokens import apply_edits
import test_modeling_atomic as fixtures
from test_modeling_atomic import _bytes, _write


class RenameBindingsTests(unittest.TestCase):
    _project = fixtures.ModelingAtomicTests._project
    _column_project = fixtures.ModelingAtomicTests._column_project

    def test_missed_metadata_bindings_refuse_without_writes(self):
        for seam in ("columnPermission", "baseColumn", "ref table"):
            with self.subTest(seam=seam):
                ws, project, _mdef = self._column_project()
                before = _bytes(ws.mirror)
                original = rename.apply_edits

                def omit(text, edits):
                    return original(text, [e for e in edits if not text[:e[0]].split("\n")[-1].lstrip().startswith(seam)])

                with patch("pbz.rename.apply_edits", side_effect=omit):
                    with self.assertRaises(PbzError) as caught:
                        if seam == "ref table":
                            rename.rename(ws, project, "table", "Sales", "Revenue")
                        else:
                            rename.rename(ws, project, "column", "Amount", "Value", table="Sales")
                self.assertEqual(caught.exception.code, "REFUSED")
                self.assertEqual(_bytes(ws.mirror), before)

    def test_role_and_base_column_crlf_bytes(self):
        ws, project, mdef = self._column_project()
        role = Path(mdef, "roles", "Reader.tmdl")
        other = Path(mdef, "tables", "Other.tmdl")
        for path in (role, other):
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        role_before, other_before = role.read_bytes(), other.read_bytes()
        rename.rename(ws, project, "column", "Amount", "Value", table="Sales")
        self.assertEqual(role.read_bytes(), role_before.replace(b"columnPermission Amount", b"columnPermission Value", 1))
        self.assertEqual(other.read_bytes(), other_before.replace(b"baseColumn: Sales.Amount", b"baseColumn: Sales.Value"))

    def _report_project(self, obj):
        ws, project, mdef = self._column_project()
        path = Path(project.mirror_report, "definition", "report.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj))
        return ws, project, mdef, path

    def test_report_sibling_paths_and_missed_first_binding(self):
        field = {"Column": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Amount"}}
        ws, project, mdef, path = self._report_project({"projections": [field, field]})
        model = tmdl.load_model(mdef)
        paths = [site for _ref, site in pbir.walk_fields_with_path(pbir.parse_json(path.read_text()), model)]
        self.assertEqual(paths, [("projections", 0), ("projections", 1)])
        before = _bytes(ws.mirror)
        original = rename.pbir_ref_edits
        with patch("pbz.rename.pbir_ref_edits", side_effect=lambda *a, **k: original(*a, **k)[1:]):
            with self.assertRaises(PbzError) as caught:
                rename.rename(ws, project, "column", "Amount", "Value", table="Sales")
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(_bytes(ws.mirror), before)
        rename.rename(ws, project, "column", "Amount", "Value", table="Sales")
        self.assertEqual(path.read_text().count('"Value"'), 2)

    def test_report_siblings_with_different_targets_rename_correctly(self):
        obj = {"projections": [
            {"Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": "Amount"}}
            for table in ("Sales", "Other")]}
        ws, project, _mdef, path = self._report_project(obj)
        rename.rename(ws, project, "column", "Amount", "Value", table="Sales")
        after = json.loads(path.read_text())["projections"]
        self.assertEqual([entry["Column"]["Property"] for entry in after], ["Value", "Amount"])

    def test_unrelated_report_string_change_refuses(self):
        ws, project, mdef, path = self._report_project({"queryRef": "Other.Amount"})
        model = tmdl.load_model(mdef)
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            rename._verify_plan(ws, project, model, mdef,
                                [("write", str(path), '{"queryRef":"Other.Value"}', b"")],
                                "column", "Sales", "Absent", "New")
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(_bytes(ws.mirror), before)

    def test_standalone_report_string_substrings_unchanged(self):
        for value in ("Sales.Amount Margin", "Other Sales.Amount", "Sales.Amount-1"):
            with self.subTest(value=value):
                ws, project, _mdef, path = self._report_project({"refs": [{"queryRef": value}]})
                before = path.read_bytes()
                rename.rename(ws, project, "column", "Amount", "Value", table="Sales")
                self.assertEqual(path.read_bytes(), before)

    def test_other_projection_native_query_ref_unchanged(self):
        obj = {"projections": [{
            "Column": {"Expression": {"SourceRef": {"Entity": "Other"}}, "Property": "Amount"},
            "nativeQueryRef": "Amount",
        }]}
        ws, project, mdef, path = self._report_project(obj)
        model = tmdl.load_model(mdef)
        self.assertIsNotNone(model.column("Other", "Amount"))
        fields = pbir.walk_fields_with_path(pbir.parse_json(path.read_text()), model)
        self.assertEqual([(ref.kind, ref.table, ref.name, site) for ref, site in fields],
                         [("column", "Other", "Amount", ("projections", 0))])
        before = path.read_bytes()
        rename.rename(ws, project, "column", "Amount", "Value", table="Sales")
        projection = json.loads(path.read_text())["projections"][0]
        self.assertEqual(projection["Column"]["Property"], "Amount")
        self.assertEqual(projection["nativeQueryRef"], "Amount")
        self.assertEqual(path.read_bytes(), before)

    def test_report_scope_violation_planned_write_refuses(self):
        for case, obj in (
            ("substring", {"refs": [{"queryRef": "Sales.Amount Margin"}]}),
            ("other_projection", {"projections": [{
                "Column": {"Expression": {"SourceRef": {"Entity": "Other"}}, "Property": "Amount"},
                "nativeQueryRef": "Amount",
            }]}),
        ):
            with self.subTest(case=case):
                ws, project, mdef, path = self._report_project({})
                model = tmdl.load_model(mdef)
                model_ops = []
                for rel, old, new in (("tables/Sales.tmdl", "Amount", "Value"),
                                      ("tables/Other.tmdl", "Sales.Amount", "Sales.Value"),
                                      ("roles/Reader.tmdl", "columnPermission Amount", "columnPermission Value")):
                    model_path = Path(mdef, rel)
                    text = model_path.read_text()
                    changed = text.replace(old, new, 1) if rel.startswith("roles/") else text.replace(old, new)
                    model_ops.append(("write", str(model_path), changed, b""))
                # Prove the model plan is valid before adding a report scope violation.
                rename._verify_plan(ws, project, model, mdef, model_ops,
                                    "column", "Sales", "Amount", "Value")
                original = json.dumps(obj)
                path.write_text(original)
                if case == "other_projection":
                    self.assertIsNotNone(model.column("Other", "Amount"))
                    fields = pbir.walk_fields_with_path(pbir.parse_json(original), model)
                    self.assertEqual([(ref.kind, ref.table, ref.name, site) for ref, site in fields],
                                     [("column", "Other", "Amount", ("projections", 0))])
                    changed = original.replace('"nativeQueryRef": "Amount"', '"nativeQueryRef": "Value"')
                else:
                    changed = original.replace("Sales.Amount Margin", "Sales.Value Margin")
                before = _bytes(ws.mirror)
                with self.assertRaises(PbzError) as caught:
                    rename._verify_plan(ws, project, model, mdef,
                                        model_ops + [("write", str(path), changed, b"")],
                                        "column", "Sales", "Amount", "Value")
                self.assertEqual(caught.exception.code, "REFUSED")
                self.assertIn("report string", str(caught.exception))
                self.assertEqual(_bytes(ws.mirror), before)

    def test_exact_standalone_report_strings_follow_column_rename(self):
        ws, project, _mdef, path = self._report_project({"refs": [
            {"queryRef": "Sales.Amount"}, {"nativeQueryRef": "Amount"}, {"queryRef": "Other.Amount"},
        ]})
        rename.rename(ws, project, "column", "Amount", "Value", table="Sales")
        self.assertEqual(json.loads(path.read_text()), {"refs": [
            {"queryRef": "Sales.Value"}, {"nativeQueryRef": "Value"}, {"queryRef": "Other.Amount"},
        ]})

    def test_malformed_planned_report_refuses(self):
        ws, project, mdef, path = self._report_project({})
        model = tmdl.load_model(mdef)
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            rename._verify_plan(ws, project, model, mdef, [("write", str(path), '{', b"")],
                                "column", "Sales", "Absent", "New")
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(_bytes(ws.mirror), before)

    def test_report_kind_change_refuses(self):
        field = {"Column": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Amount"}}
        ws, project, mdef, path = self._report_project(field)
        model = tmdl.load_model(mdef)
        renamed = json.dumps(field).replace('"Amount"', '"Value"').replace('"Column"', '"Measure"')
        before = _bytes(ws.mirror)
        model_ops = []
        for rel, old, new in (("tables/Sales.tmdl", "Amount", "Value"),
                              ("tables/Other.tmdl", "Sales.Amount", "Sales.Value"),
                              ("roles/Reader.tmdl", "columnPermission Amount", "columnPermission Value")):
            model_path = Path(mdef, rel)
            model_ops.append(("write", str(model_path), model_path.read_text().replace(old, new, 1)
                              if rel.startswith("roles/") else model_path.read_text().replace(old, new), b""))
        correct = json.dumps(field).replace('"Amount"', '"Value"')
        rename._verify_plan(ws, project, model, mdef,
                            model_ops + [("write", str(path), correct, b"")],
                            "column", "Sales", "Amount", "Value")
        with self.assertRaises(PbzError) as caught:
            rename._verify_plan(ws, project, model, mdef, model_ops + [("write", str(path), renamed, b"")],
                                "column", "Sales", "Amount", "Value")
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(_bytes(ws.mirror), before)

    def test_missed_metadata_and_query_name_refuse(self):
        for kind, key, old, new in (("column", "metadata", "Amount", "Value"),
                                    ("table", "metadata", "Sales", "Revenue"),
                                    ("table", "queryName", "Sales", "Revenue")):
            with self.subTest(kind=kind, key=key):
                ws, project, _mdef, path = self._report_project({key: "Sales.Amount"})
                before = _bytes(ws.mirror)
                with patch("pbz.move.plan_pbir", return_value=[]):
                    with self.assertRaises(PbzError) as caught:
                        rename.rename(ws, project, kind, old, new, table="Sales")
                self.assertEqual(caught.exception.code, "REFUSED")
                self.assertEqual(_bytes(ws.mirror), before)
                rename.rename(ws, project, kind, old, new, table="Sales")
                self.assertEqual(json.loads(path.read_text())[key],
                                 "Sales.Value" if kind == "column" else "Revenue.Amount")

    def test_column_query_name_is_outside_planner_scope(self):
        ws, project, _mdef, path = self._report_project({"queryName": "Sales.Amount"})
        rename.rename(ws, project, "column", "Amount", "Value", table="Sales")
        self.assertEqual(json.loads(path.read_text())["queryName"], "Sales.Amount")

    def test_council_round_shapes(self):
        # Sibling string of a wrapper field for Other stays unchanged.
        obj = {"projections": [{"field": {"Column": {"Expression": {"SourceRef": {"Entity": "Other"}}, "Property": "Amount"}}, "nativeQueryRef": "Amount"}]}
        ws, project, _mdef, path = self._report_project(obj)
        rename.rename(ws, project, "column", "Amount", "Value", table="Sales")
        self.assertEqual(json.loads(path.read_text())["projections"][0]["nativeQueryRef"], "Amount")
        # Standalone wrapped aggregation string follows a column rename.
        ws, project, _mdef, path = self._report_project({"objects": {"metadata": "Sum(Sales.Amount)"}})
        rename.rename(ws, project, "column", "Amount", "Value", table="Sales")
        self.assertEqual(json.loads(path.read_text())["objects"]["metadata"], "Sum(Sales.Value)")
        # Descriptive metadata containing two wrapped terms is not one reference.
        ws, project, _mdef, path = self._report_project({"objects": {"metadata": "Sum(Sales.Amount) vs Sum(Other.Amount)"}})
        rename.rename(ws, project, "table", "Sales", "Revenue")
        self.assertEqual(json.loads(path.read_text())["objects"]["metadata"],
                         "Sum(Sales.Amount) vs Sum(Other.Amount)")
        # Case-variant standalone string follows.
        ws, project, _mdef, path = self._report_project({"refs": [{"queryRef": "sales.amount"}]})
        rename.rename(ws, project, "column", "Amount", "Value", table="Sales")
        self.assertEqual(json.loads(path.read_text())["refs"][0]["queryRef"], "Sales.Value")

    def test_standalone_report_strings_verified(self):
        for kind, old, new, strings in (
            ("column", "Amount", "Value", ["Sales.Amount", "Other.Amount", "Amount"]),
            ("table", "Sales", "Revenue", ["Sales.Amount", "Other.Amount", "Sales"]),
        ):
            for missed in (True, False):
                with self.subTest(kind=kind, missed=missed):
                    obj = {"refs": [{"queryRef": v, "nativeQueryRef": v} for v in strings]}
                    ws, project, _mdef, path = self._report_project(obj)
                    before = _bytes(ws.mirror)
                    if missed:
                        with patch("pbz.move.plan_pbir", return_value=[]):
                            with self.assertRaises(PbzError) as caught:
                                rename.rename(ws, project, kind, old, new, table="Sales")
                        self.assertEqual(caught.exception.code, "REFUSED")
                        self.assertEqual(_bytes(ws.mirror), before)
                    else:
                        rename.rename(ws, project, kind, old, new, table="Sales")
                        after = json.loads(path.read_text())["refs"]
                        self.assertEqual(after[0]["queryRef"], "Revenue.Amount" if kind == "table" else "Sales.Value")
                        self.assertEqual(after[1], obj["refs"][1])
                        self.assertEqual(after[2]["nativeQueryRef"], "Sales" if kind == "table" else "Value")

    def test_table_rename_expression_metadata_unchanged(self):
        for value in ("Sales.Amount.Share IN { 2025 }", "Sales.Amount.Share % Sales.Total",
                      "Sales.Amount.Share || Sales.Amount", "Sales.Amount.Share AND Sales.Total",
                      "Sales.Amount.Share IN AllowedYears", "Sales.Amount.Share vs Other.Amount"):
            with self.subTest(value=value):
                ws, project, _mdef, path = self._report_project({"refs": [{"metadata": value}]})
                before = path.read_bytes()
                rename.rename(ws, project, "table", "Sales", "Revenue")
                self.assertEqual(path.read_bytes(), before)
        # A pure dotted extension of a qualified path still follows, including
        # a segment that merely ends in the table's name.
        for value, expected in (("Sales.Amount.Share", "Revenue.Amount.Share"),
                                ("Sales.Amount.Net Sales", "Revenue.Amount.Net Sales")):
            with self.subTest(value=value):
                ws, project, _mdef, path = self._report_project({"refs": [{"metadata": value}]})
                rename.rename(ws, project, "table", "Sales", "Revenue")
                self.assertEqual(json.loads(path.read_text())["refs"][0]["metadata"], expected)

    def test_nested_string_follows_owning_projection(self):
        def proj(entity):
            return {"field": {"Column": {"Expression": {"SourceRef": {"Entity": entity}},
                                         "Property": "Amount"}},
                    "details": {"nativeQueryRef": "Amount"}}
        ws, project, _mdef, path = self._report_project({"projections": [proj("Other"), proj("Sales")]})
        rename.rename(ws, project, "column", "Amount", "Value", table="Sales")
        after = json.loads(path.read_text())["projections"]
        self.assertEqual(after[0]["details"]["nativeQueryRef"], "Amount")
        self.assertEqual(after[1]["details"]["nativeQueryRef"], "Value")

    def test_string_ownership_scoped_to_containing_element(self):
        col = lambda e: {"Column": {"Expression": {"SourceRef": {"Entity": e}}, "Property": "Amount"}}
        # A standalone string in a sibling element does not inherit its field.
        ws, project, _mdef, path = self._report_project(
            {"projections": [col("Other"), {"queryRef": "Sales.Amount"}]})
        rename.rename(ws, project, "column", "Amount", "Value", table="Sales")
        self.assertEqual(json.loads(path.read_text())["projections"][1]["queryRef"], "Sales.Value")
        # A nearer unique field is not poisoned by the array's other elements.
        ws, project, _mdef, path = self._report_project({"projections": [
            col("Other"), col("Sales"),
            {"field": col("Sales"), "queryRef": "Sales.Amount",
             "details": {"nativeQueryRef": "Amount"}}]})
        rename.rename(ws, project, "column", "Amount", "Value", table="Sales")
        after = json.loads(path.read_text())["projections"]
        self.assertEqual(after[2]["queryRef"], "Sales.Value")
        self.assertEqual(after[2]["details"]["nativeQueryRef"], "Value")
        self.assertEqual(after[0]["Column"]["Property"], "Amount")

    def _colliding_project(self, obj):
        """A project where Sales has a column named 'Region.Amount' and a
        second table is named 'Sales.Region', plus a report holding obj."""
        ws, project, mdef = self._project()
        _write(os.path.join(mdef, "model.tmdl"),
               "model Model\n\tculture: en-US\n\nref table Sales\n\nref table 'Sales.Region'\n")
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn 'Region.Amount'\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Sales.Region.tmdl"),
               "table 'Sales.Region'\n\tcolumn Amount\n\t\tdataType: int64\n")
        path = Path(project.mirror_report, "definition", "report.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj))
        return ws, project, mdef, path

    def _bound_colliding(self, query_ref):
        return {"projections": [{
            "field": {"Column": {"Expression": {"SourceRef": {"Entity": "Sales"}},
                                 "Property": "Region.Amount"}},
            "queryRef": query_ref}]}

    def test_table_rename_field_binding_beats_longer_table_prefix(self):
        ws, project, mdef, path = self._colliding_project(self._bound_colliding("Sales.Region.Amount"))
        rename.rename(ws, project, "table", "Sales", "Revenue")
        after = json.loads(path.read_text())["projections"][0]
        self.assertEqual(after["queryRef"], "Revenue.Region.Amount")
        self.assertEqual(after["field"]["Column"]["Expression"]["SourceRef"]["Entity"], "Revenue")
        # The same string with no binding stays untouched: the longer table owns it.
        ws, project, mdef, path = self._colliding_project({"refs": [{"queryRef": "Sales.Region.Amount"}]})
        model = tmdl.load_model(mdef)
        self.assertIsNotNone(model.column("Sales", "Region.Amount"))
        self.assertIsNotNone(model.table("Sales.Region"))
        before = path.read_bytes()
        rename.rename(ws, project, "table", "Sales", "Revenue")
        self.assertEqual(path.read_bytes(), before)

    def test_table_rename_other_table_keeps_bound_string(self):
        ws, project, mdef, path = self._colliding_project(self._bound_colliding("Sales.Region.Amount"))
        rename.rename(ws, project, "table", "Sales.Region", "Territory")
        after = json.loads(path.read_text())["projections"][0]
        self.assertEqual(after["queryRef"], "Sales.Region.Amount")
        self.assertEqual(after["field"]["Column"]["Expression"]["SourceRef"]["Entity"], "Sales")

    def test_table_rename_bound_wrapped_extension_follows(self):
        ws, project, mdef, path = self._colliding_project(
            self._bound_colliding("Sum(Sales.Region.Amount.Share)"))
        rename.rename(ws, project, "table", "Sales", "Revenue")
        after = json.loads(path.read_text())["projections"][0]
        self.assertEqual(after["queryRef"], "Sum(Revenue.Region.Amount.Share)")

    def test_column_rename_ambiguous_unbound_string_unchanged(self):
        ws, project, mdef, path = self._colliding_project({"refs": [{"queryRef": "Sales.Region.Amount"}]})
        before = path.read_bytes()
        rename.rename(ws, project, "column", "Region.Amount", "Value", table="Sales")
        self.assertEqual(path.read_bytes(), before)

    def test_table_rename_casefold_length_mismatch(self):
        from types import SimpleNamespace as NS
        field = NS(table="İ", name="Amount", kind="column")
        out = rename._report_string_rename("queryRef", "i̇.Amount", "table", "", "İ", "Revenue",
                                           field=field, model=None)
        self.assertEqual(out, "Revenue.Amount")

    def test_table_rewrite_unicode_qualified_prefix_offsets(self):
        from types import SimpleNamespace as NS
        model = NS(tables={"İ": NS(name="İ")})
        qualified = {"İ.Amount".lower()}
        out = rename._table_rewrite("İ.Amount.X", "İ", "Revenue", qualified, NS(name="İ"), model)
        self.assertEqual(out, "Revenue.Amount.X")
        out = rename._table_rewrite("İ.Amount.% Share", "İ", "Revenue", qualified, NS(name="İ"), model)
        self.assertEqual(out, "İ.Amount.% Share")

    def test_column_rename_extension_owned_string_unchanged(self):
        ws, project, mdef, path = self._colliding_project({"refs": [{"queryRef": "Sales.Region.Ext"}]})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn 'Region.Ext'\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Sales.Region", "measures": [{"name": "Ext", "expression": "1"}]}]}))
        before = path.read_bytes()
        rename.rename(ws, project, "column", "Region.Ext", "Value", table="Sales")
        self.assertEqual(path.read_bytes(), before)

    def test_column_rename_hierarchy_owned_string_unchanged(self):
        ws, project, mdef, path = self._colliding_project({"refs": [{"queryRef": "Sales.Region.Calendar.Year"}]})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn 'Region.Calendar.Year'\n\t\tdataType: string\n")
        _write(os.path.join(mdef, "tables", "Sales.Region.tmdl"),
               "table 'Sales.Region'\n\tcolumn Amount\n\t\tdataType: int64\n\n\thierarchy Calendar\n\n\t\tlevel Year\n\t\t\tcolumn: Amount\n")
        before = path.read_bytes()
        rename.rename(ws, project, "column", "Region.Calendar.Year", "Value", table="Sales")
        self.assertEqual(path.read_bytes(), before)

    def test_table_rewrite_unicode_equal_length_guard(self):
        from types import SimpleNamespace as NS
        t1, other = "i̇i̇", "İİ.X"
        tables = {t1: NS(name=t1, columns={"X.Amount": NS(name="X.Amount")}, measures={}, hierarchies={}),
                  other: NS(name=other, columns={"Amount": NS(name="Amount")}, measures={}, hierarchies={})}
        model = NS(tables=tables, table=lambda name: tables.get(name))
        qualified = {f"{t1}.X.Amount".lower(), f"{other}.Amount".lower()}
        out = rename._table_rewrite("İİ.X.Amount", t1, "Revenue", qualified, tables[t1], model)
        self.assertEqual(out, "İİ.X.Amount")

    def test_table_rename_longer_table_not_blocked_by_prefix_table(self):
        ws, project, mdef, path = self._colliding_project(
            {"refs": [{"queryRef": "Sales.Region.Amount"}, {"queryRef": "Sum(Sales.Region.Amount)"}]})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Total\n\t\tdataType: int64\n")
        rename.rename(ws, project, "table", "Sales.Region", "Territory")
        refs = [r["queryRef"] for r in json.loads(path.read_text())["refs"]]
        self.assertEqual(refs, ["Territory.Amount", "Sum(Territory.Amount)"])

    def test_table_rename_exact_string_owned_by_shorter_table_stays(self):
        ws, project, mdef, path = self._colliding_project(
            {"refs": [{"queryRef": "Sales.Region.Amount"}, {"queryRef": "Sum(Sales.Region.Amount)"}]})
        before = path.read_bytes()
        rename.rename(ws, project, "table", "Sales.Region", "Territory")
        self.assertEqual(path.read_bytes(), before)

    def test_table_rename_bound_string_of_other_table_unchanged(self):
        field = {"Column": {"Expression": {"SourceRef": {"Entity": "Other"}}, "Property": "Amount"},
                 "queryRef": "Sales.Amount"}
        ws, project, mdef, path = self._report_project({"projections": [field]})
        before = path.read_bytes()
        rename.rename(ws, project, "table", "Sales", "Revenue")
        self.assertEqual(path.read_bytes(), before)

    def test_column_rename_bare_native_query_ref_extension_measure_of_other(self):
        ws, project, mdef, path = self._report_project({"refs": [{"nativeQueryRef": "Other.Ext.Share"}]})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn 'Other.Ext.Share'\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Other", "measures": [{"name": "Ext", "expression": "1"}]}]}))
        before = path.read_bytes()
        rename.rename(ws, project, "column", "Other.Ext.Share", "Value", table="Sales")
        self.assertEqual(path.read_bytes(), before)

    def test_table_rename_own_column_under_longer_prefix(self):
        ws, project, mdef, path = self._colliding_project({"refs": [{"queryRef": "Sales.Region.Budget"}]})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn 'Region.Budget'\n\t\tdataType: int64\n")
        rename.rename(ws, project, "table", "Sales", "Revenue")
        self.assertEqual(json.loads(path.read_text())["refs"][0]["queryRef"], "Revenue.Region.Budget")

    def test_column_rename_bare_native_query_ref_owned_by_other_table(self):
        ws, project, mdef, path = self._colliding_project({"refs": [{"nativeQueryRef": "Sales.Region.Amount"}]})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn 'Sales.Region.Amount'\n\t\tdataType: int64\n")
        before = path.read_bytes()
        rename.rename(ws, project, "column", "Sales.Region.Amount", "Value", table="Sales")
        self.assertEqual(path.read_bytes(), before)

    def test_table_rename_extension_measure_on_longer_table_stays(self):
        ws, project, mdef, path = self._colliding_project({"refs": [{"queryRef": "Sales.Region.Ext"}]})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn 'Region.Ext'\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Sales.Region", "measures": [{"name": "Ext", "expression": "1"}]}]}))
        before = path.read_bytes()
        rename.rename(ws, project, "table", "Sales", "Revenue")
        self.assertEqual(path.read_bytes(), before)

    def test_column_rename_bare_native_query_ref_owned_by_unrelated_table(self):
        ws, project, mdef, path = self._report_project({"refs": [{"nativeQueryRef": "Other.Amount"}]})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn 'Other.Amount'\n\t\tdataType: int64\n")
        before = path.read_bytes()
        rename.rename(ws, project, "column", "Other.Amount", "Value", table="Sales")
        self.assertEqual(path.read_bytes(), before)

    def test_column_rename_owned_extension_string_unchanged(self):
        ws, project, mdef, path = self._colliding_project({"refs": [{"queryRef": "Sales.Region.Amount.Share"}]})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn 'Region.Amount.Share'\n\t\tdataType: int64\n")
        before = path.read_bytes()
        rename.rename(ws, project, "column", "Region.Amount.Share", "Value", table="Sales")
        self.assertEqual(path.read_bytes(), before)

    def test_column_rename_bare_native_query_ref_extension_of_unrelated_table(self):
        ws, project, mdef, path = self._report_project({"refs": [{"nativeQueryRef": "Other.Amount.Share"}]})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn 'Other.Amount.Share'\n\t\tdataType: int64\n")
        before = path.read_bytes()
        rename.rename(ws, project, "column", "Other.Amount.Share", "Value", table="Sales")
        self.assertEqual(path.read_bytes(), before)

    def test_column_rename_prefix_table_extension_measure_does_not_block(self):
        ws, project, mdef, path = self._colliding_project({"refs": [{"queryRef": "Sales.Region.Amount.Share"}]})
        _write(os.path.join(mdef, "tables", "Sales.Region.tmdl"),
               "table 'Sales.Region'\n\tcolumn 'Amount.Share'\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Sales", "measures": [{"name": "Region.Amount", "expression": "1"}]}]}))
        rename.rename(ws, project, "column", "Amount.Share", "Value", table="Sales.Region")
        self.assertEqual(json.loads(path.read_text())["refs"][0]["queryRef"], "Sales.Region.Value")

    def test_table_rename_extension_of_shorter_table_does_not_block(self):
        ws, project, mdef, path = self._colliding_project({"refs": [{"queryRef": "Sales.Region.Amount.Share"}]})
        _write(os.path.join(mdef, "tables", "Sales.Region.tmdl"),
               "table 'Sales.Region'\n\tcolumn 'Amount.Share'\n\t\tdataType: int64\n")
        rename.rename(ws, project, "table", "Sales.Region", "Revenue")
        self.assertEqual(json.loads(path.read_text())["refs"][0]["queryRef"], "Revenue.Amount.Share")

    def test_table_rewrite_wrapper_does_not_bypass_exact_block(self):
        from types import SimpleNamespace as NS
        sales = NS(name="Sales", columns={"Amount": NS(name="Amount")}, measures={}, hierarchies={})
        weird = NS(name="Sum(Sales", columns={"Amount)": NS(name="Amount)")}, measures={}, hierarchies={})
        tables = {"Sales": sales, "Sum(Sales": weird}
        model = NS(tables=tables, table=lambda name: tables.get(name))
        out = rename._table_rewrite("Sum(Sales.Amount)", "Sales", "Revenue", {"sales.amount"}, sales, model)
        self.assertEqual(out, "Sum(Sales.Amount)")

    def test_table_rename_hierarchy_bound_query_ref_follows(self):
        hl = {"HierarchyLevel": {"Expression": {"Hierarchy": {"Expression": {"SourceRef": {"Entity": "Sales"}},
                                                              "Hierarchy": "Calendar"}}, "Level": "Year"},
              "queryRef": "Sales.Calendar.Year"}
        ws, project, mdef, path = self._report_project({"projections": [hl]})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn OrderDate\n\t\tdataType: dateTime\n\n\thierarchy Calendar\n\n\t\tlevel Year\n\t\t\tcolumn: OrderDate\n")
        rename.rename(ws, project, "table", "Sales", "Revenue")
        self.assertEqual(json.loads(path.read_text())["projections"][0]["queryRef"], "Revenue.Calendar.Year")

    def test_column_rename_wrapped_whole_string_owned_by_other_table(self):
        ws, project, mdef, path = self._report_project({"refs": [{"queryRef": "Sum(Sales.Amount)"}]})
        _write(os.path.join(mdef, "tables", "Sum(Sales.tmdl"),
               "table 'Sum(Sales'\n\tcolumn 'Amount)'\n\t\tdataType: int64\n")
        before = path.read_bytes()
        rename.rename(ws, project, "column", "Amount", "Value", table="Sales")
        self.assertEqual(path.read_bytes(), before)

    def test_table_rename_bound_literal_path_that_looks_wrapped(self):
        fld = {"Column": {"Expression": {"SourceRef": {"Entity": "Sum(Sales"}}, "Property": "Amount)"},
               "queryRef": "Sum(Sales.Amount)"}
        ws, project, mdef, path = self._report_project({"projections": [fld]})
        _write(os.path.join(mdef, "tables", "Sum(Sales.tmdl"),
               "table 'Sum(Sales'\n\tcolumn 'Amount)'\n\t\tdataType: int64\n")
        rename.rename(ws, project, "table", "Sum(Sales", "Totals")
        self.assertEqual(json.loads(path.read_text())["projections"][0]["queryRef"], "Totals.Amount)")

    def test_table_rename_ambiguous_site_unchanged(self):
        out = rename._report_string_rename("queryRef", "Sales.Amount", "table", "", "Sales", "Revenue",
                                           field=rename._AMBIGUOUS, model=None)
        self.assertEqual(out, "Sales.Amount")

    def test_table_rename_final_sigma_casefold(self):
        from types import SimpleNamespace as NS
        field = NS(table="İΟΣ", name="Amount", kind="column")
        out = rename._report_string_rename("queryRef", "i̇οσ.Amount", "table", "", "İΟΣ", "Revenue",
                                           field=field, model=None)
        self.assertEqual(out, "Revenue.Amount")
        self.assertEqual(rename._head_swap("i̇οσ.Amount", "İΟΣ", "Revenue"), "Revenue.Amount")

    def test_column_rename_bound_wrapped_query_ref_follows(self):
        fld = {"Column": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Amount"},
               "queryRef": "Sum(Sales.Amount)"}
        ws, project, mdef, path = self._report_project({"projections": [fld]})
        rename.rename(ws, project, "column", "Amount", "Value", table="Sales")
        self.assertEqual(json.loads(path.read_text())["projections"][0]["queryRef"], "Sum(Sales.Value)")

    def test_table_rename_medial_sigma_entity_and_string_follow(self):
        fld = {"Column": {"Expression": {"SourceRef": {"Entity": "οσ"}}, "Property": "Amount"},
               "queryRef": "οσ.Amount"}
        ws, project, mdef, path = self._report_project({"projections": [fld]})
        _write(os.path.join(mdef, "tables", "ΟΣ.tmdl"),
               "table 'ΟΣ'\n\tcolumn Amount\n\t\tdataType: int64\n")
        rename.rename(ws, project, "table", "ΟΣ", "Revenue")
        proj = json.loads(path.read_text())["projections"][0]
        self.assertEqual(proj["Column"]["Expression"]["SourceRef"]["Entity"], "Revenue")
        self.assertEqual(proj["queryRef"], "Revenue.Amount")

    def test_column_rename_sharp_s_names_stay_distinct(self):
        fld = {"Column": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Masse"},
               "queryRef": "Sales.Masse"}
        ws, project, mdef, path = self._report_project({"projections": [fld]})
        sales_path = os.path.join(mdef, "tables", "Sales.tmdl")
        _write(sales_path,
               "table Sales\n\tcolumn 'Maße'\n\t\tdataType: int64\n\tcolumn Masse\n\t\tdataType: int64\n\tmeasure Use = SUM(Sales[Masse])\n")
        rename.rename(ws, project, "column", "Maße", "Value", table="Sales")
        proj = json.loads(path.read_text())["projections"][0]
        self.assertEqual(proj["queryRef"], "Sales.Masse")
        sales = open(sales_path).read()
        self.assertIn("SUM(Sales[Masse])", sales)
        self.assertIn("column Value", sales)

    def test_column_rename_to_fold_collision_refused(self):
        ws, project, mdef = self._column_project()
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n\tcolumn 'ΟΣ'\n\t\tdataType: int64\n")
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "column", "Amount", "οσ", table="Sales")
        self.assertEqual(caught.exception.code, "NAME_TAKEN")
        self.assertEqual(_bytes(ws.mirror), before)

    def test_table_rename_fresh_reference_to_extension_entity_stays(self):
        ws, project, mdef, path = self._report_project({"refs": [{"metadata": "Sales.Amount.Share vs Local.Total"}]})
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Local", "measures": [{"name": "Total", "expression": "1"}]}]}))
        before = path.read_bytes()
        rename.rename(ws, project, "table", "Sales", "Revenue")
        self.assertEqual(path.read_bytes(), before)

    def test_table_rename_translated_sigma_name_refused(self):
        ws, project, mdef = self._column_project()
        _write(os.path.join(mdef, "tables", "ΟΣ.tmdl"),
               "table 'ΟΣ'\n\tcolumn Amount\n\t\tdataType: int64\n")
        with patch("pbz.rename.translated_names", return_value={"ος"}):
            with self.assertRaises(PbzError) as caught:
                rename.rename(ws, project, "table", "ΟΣ", "Revenue")
        self.assertEqual(caught.exception.code, "UNSUPPORTED")

    def test_table_rename_into_extension_entity_name_refused(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Sales", "measures": [{"name": "A", "expression": "1"}]},
            {"name": "Revenue", "measures": [{"name": "B", "expression": "2"}]}]}))
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "table", "Sales", "Revenue")
        self.assertEqual(caught.exception.code, "NAME_TAKEN")

    def test_table_rename_source_lookup_folds_sigma(self):
        ws, project, mdef = self._column_project()
        _write(os.path.join(mdef, "tables", "ΟΣ.tmdl"),
               "table 'ΟΣ'\n\tcolumn Amount\n\t\tdataType: int64\n")
        rename.rename(ws, project, "table", "οσ", "Revenue")
        self.assertTrue(os.path.isfile(os.path.join(mdef, "tables", "Revenue.tmdl")))

    def test_table_rename_query_order_folds_sigma(self):
        ws, project, mdef = self._column_project()
        _write(os.path.join(mdef, "tables", "ΟΣ.tmdl"),
               "table 'ΟΣ'\n\tcolumn Amount\n\t\tdataType: int64\n")
        model_path = Path(mdef, "model.tmdl")
        model_path.write_text(model_path.read_text() + '\nannotation PBI_QueryOrder = ["οσ"]\n')
        rename.rename(ws, project, "table", "ΟΣ", "Revenue")
        self.assertIn('"Revenue"', model_path.read_text())

    def test_column_rename_variation_hierarchy_follows(self):
        hl = {"HierarchyLevel": {"Expression": {"Hierarchy": {
                "Expression": {"PropertyVariationSource": {"Expression": {"SourceRef": {"Entity": "Sales"}},
                                                           "Property": "OrderDate"}},
                "Hierarchy": "Date Hierarchy"}}, "Level": "Year"},
              "queryRef": "Sales.OrderDate.Date Hierarchy.Year"}
        ws, project, mdef, path = self._report_project({"projections": [hl]})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn OrderDate\n\t\tdataType: dateTime\n")
        rename.rename(ws, project, "column", "OrderDate", "BookedDate", table="Sales")
        proj = json.loads(path.read_text())["projections"][0]
        self.assertEqual(proj["queryRef"], "Sales.BookedDate.Date Hierarchy.Year")
        pvs = proj["HierarchyLevel"]["Expression"]["Hierarchy"]["Expression"]["PropertyVariationSource"]
        self.assertEqual(pvs["Property"], "BookedDate")

    def test_column_rename_variation_wrapped_and_bare_follow(self):
        hl = {"HierarchyLevel": {"Expression": {"Hierarchy": {
                "Expression": {"PropertyVariationSource": {"Expression": {"SourceRef": {"Entity": "Sales"}},
                                                           "Property": "OrderDate"}},
                "Hierarchy": "Date Hierarchy"}}, "Level": "Year"}}
        for key, given, expected in (("queryRef", "Min(Sales.OrderDate)", "Min(Sales.BookedDate)"),
                                     ("nativeQueryRef", "OrderDate", "BookedDate"),
                                     ("queryRef", "Sales.OrderDate.OrderDate.Year",
                                      "Sales.BookedDate.OrderDate.Year")):
            with self.subTest(given=given):
                ws, project, mdef, path = self._report_project({"projections": [dict(hl, **{key: given})]})
                _write(os.path.join(mdef, "tables", "Sales.tmdl"),
                       "table Sales\n\tcolumn OrderDate\n\t\tdataType: dateTime\n")
                rename.rename(ws, project, "column", "OrderDate", "BookedDate", table="Sales")
                self.assertEqual(json.loads(path.read_text())["projections"][0][key], expected)

    def test_measure_rename_fold_spelling_keeps_culture(self):
        ws, project, mdef = self._column_project()
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tmeasure 'ΟΣ' = 1\n")
        cult = Path(mdef, "cultures", "en-US.tmdl")
        cult.parent.mkdir(parents=True, exist_ok=True)
        cult.write_text("cultureInfo en-US\n\ttranslations\n\t\ttable Sales\n\t\t\tmeasure 'ΟΣ'\n\t\t\t\tcaption: Sigma\n")
        rename.rename(ws, project, "measure", "οσ", "Total")
        self.assertNotIn("ΟΣ", cult.read_text())
        self.assertIn("Total", cult.read_text())

    def test_measure_rename_into_extension_measure_refused(self):
        ws, project, mdef, path = self._report_project({"refs": [{"queryRef": "Sales.Net"}]})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tmeasure Net = 1\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Sales", "measures": [{"name": "Total", "expression": "2"}]}]}))
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "measure", "Net", "Total")
        self.assertEqual(caught.exception.code, "NAME_TAKEN")

    def test_table_rename_report_extension_measure_follows(self):
        ws, project, _mdef, path = self._report_project({"refs": [{"queryRef": "Sales.Ext"}]})
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Sales", "measures": [{"name": "Ext", "expression": "SUM(Sales[Amount])"}]}]}))
        rename.rename(ws, project, "table", "Sales", "Revenue")
        self.assertEqual(json.loads(path.read_text())["refs"][0]["queryRef"], "Revenue.Ext")
        self.assertEqual(json.loads(ext_path.read_text())["entities"][0]["name"], "Revenue")
        self.assertEqual(json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"],
                         "SUM(Revenue[Amount])")

    def test_table_rename_qualified_native_query_ref_follows(self):
        fld = {"Column": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Amount"},
               "nativeQueryRef": "Sales.Amount"}
        ws, project, mdef, path = self._report_project({"projections": [fld]})
        rename.rename(ws, project, "table", "Sales", "Revenue")
        self.assertEqual(json.loads(path.read_text())["projections"][0]["nativeQueryRef"], "Revenue.Amount")

    def test_table_rename_case_variant_hierarchy_entity_not_refused(self):
        hl = {"HierarchyLevel": {"Expression": {"Hierarchy": {"Expression": {"SourceRef": {"Entity": "sales"}},
                                                              "Hierarchy": "Geo"}}, "Level": "City"},
              "queryRef": "Sales.Geo.City"}
        ws, project, mdef, path = self._report_project({"projections": [hl]})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn CityName\n\t\tdataType: string\n\n\thierarchy Geo\n\n\t\tlevel City\n\t\t\tcolumn: CityName\n")
        rename.rename(ws, project, "table", "Sales", "Revenue")
        self.assertEqual(json.loads(path.read_text())["projections"][0]["queryRef"], "Revenue.Geo.City")

    def test_column_rename_extension_measure_bare_ref_not_retargeted(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [{"name": "Amount", "expression": "7"},
                                            {"name": "Twice", "expression": "2 * [Amount]"}]}]}))
        rename.rename(ws, project, "column", "Amount", "Net", table="Sales")
        twice = json.loads(ext_path.read_text())["entities"][0]["measures"][1]["expression"]
        self.assertEqual(twice, "2 * [Amount]")

    def test_table_rename_extension_qualified_ref_follows_qualifier(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        _write(os.path.join(mdef, "tables", "Budget.tmdl"),
               "table Budget\n\tcolumn Code\n\t\tdataType: string\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [{"name": "Amount", "expression": "7"},
                                            {"name": "Twice", "expression": "2 * 'Budget'[Amount]"}]}]}))
        rename.rename(ws, project, "table", "Budget", "Plan")
        twice = json.loads(ext_path.read_text())["entities"][0]["measures"][1]["expression"]
        self.assertEqual(twice, "2 * 'Plan'[Amount]")

    def test_table_rename_extension_table_ref_not_shadowed_by_measure_name(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [{"name": "Sales", "expression": "1"},
                                            {"name": "Rows", "expression": "COUNTROWS('Sales')"}]}]}))
        rename.rename(ws, project, "table", "Sales", "Revenue")
        rows = json.loads(ext_path.read_text())["entities"][0]["measures"][1]["expression"]
        self.assertEqual(rows, "COUNTROWS('Revenue')")

    def test_column_rename_extension_row_context_ref_follows(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [{"name": "Amount", "expression": "7"},
                                            {"name": "Total", "expression": "SUMX('Sales', [Amount])"}]}]}))
        rename.rename(ws, project, "column", "Amount", "Net", table="Sales")
        tot = json.loads(ext_path.read_text())["entities"][0]["measures"][1]["expression"]
        self.assertEqual(tot, "SUMX('Sales', [Net])")

    def test_column_rename_variation_under_group_ref_follows(self):
        grp = {"GroupRef": {"Expression": {"PropertyVariationSource": {"Expression": {"SourceRef": {"Entity": "Sales"}},
                                                                      "Name": "Variation",
                                                                      "Property": "OrderDate"}},
                            "Property": "Year"},
               "queryRef": "Sales.OrderDate.Variation.Year"}
        ws, project, mdef, path = self._report_project({"refs": [grp]})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn OrderDate\n\t\tdataType: dateTime\n")
        rename.rename(ws, project, "column", "OrderDate", "BookedDate", table="Sales")
        pvs = json.loads(path.read_text())["refs"][0]["GroupRef"]["Expression"]["PropertyVariationSource"]
        self.assertEqual(pvs["Property"], "BookedDate")

    def test_column_rename_unrelated_expression_object_untouched(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        other = Path(project.mirror_report, "definition", "other.json")
        other.write_text(json.dumps({"name": "example", "expression": "'Sales'[Amount]"}))
        rename.rename(ws, project, "column", "Amount", "Value", table="Sales")
        self.assertEqual(json.loads(other.read_text())["expression"], "'Sales'[Amount]")

    def test_column_rename_into_extension_measure_name_refused(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [{"name": "Net", "expression": "7"},
                                            {"name": "Total", "expression": "SUMX('Sales', [Net])"}]}]}))
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "column", "Amount", "Net", table="Sales")
        self.assertEqual(caught.exception.code, "NAME_TAKEN")

    def test_measure_rename_into_column_on_other_table_refused(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n\tmeasure Margin = 1\n")
        _write(os.path.join(mdef, "tables", "Other.tmdl"),
               "table Other\n\tcolumn Net\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [{"name": "Total", "expression": "SUMX('Other', [Margin])"}]}]}))
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "measure", "Margin", "Net")
        self.assertEqual(caught.exception.code, "NAME_TAKEN")

    def test_column_rename_destination_string_owned_by_longer_table_refused(self):
        ws, project, mdef, path = self._report_project({"refs": [{"queryRef": "Sales.Amount"}]})
        _write(os.path.join(mdef, "tables", "Sales.Region.tmdl"),
               "table 'Sales.Region'\n\tcolumn Value\n\t\tdataType: int64\n")
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "column", "Amount", "Region.Value", table="Sales")
        self.assertEqual(caught.exception.code, "NAME_TAKEN")

    def test_table_rename_destination_string_owned_by_longer_table_refused(self):
        ws, project, mdef, path = self._report_project({"refs": [{"queryRef": "Sales.Region.Amount"}]})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn 'Region.Amount'\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Revenue.Region.tmdl"),
               "table 'Revenue.Region'\n\tcolumn Amount\n\t\tdataType: int64\n")
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "table", "Sales", "Revenue")
        self.assertEqual(caught.exception.code, "NAME_TAKEN")

    def test_column_rename_destination_extending_extension_path_allowed(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Sales.Region.tmdl"),
               "table 'Sales.Region'\n\tcolumn Amount\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Sales", "measures": [{"name": "Region.Fee", "expression": "1"}]}]}))
        rename.rename(ws, project, "column", "Amount", "Fee.Share", table="Sales.Region")

    def test_column_rename_bare_destination_owned_by_other_table_refused(self):
        fld = {"Column": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Amount"},
               "nativeQueryRef": "Amount"}
        ws, project, mdef, path = self._report_project({"projections": [fld]})
        _write(os.path.join(mdef, "tables", "Other.tmdl"),
               "table Other\n\tcolumn Amount\n\t\tdataType: int64\n")
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "column", "Amount", "Other.Amount", table="Sales")
        self.assertEqual(caught.exception.code, "NAME_TAKEN")

    def test_column_rename_bare_destination_extending_other_path_refused(self):
        fld = {"Column": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Amount"},
               "nativeQueryRef": "Amount"}
        ws, project, mdef, path = self._report_project({"projections": [fld]})
        _write(os.path.join(mdef, "tables", "Other.tmdl"),
               "table Other\n\tcolumn Metric\n\t\tdataType: int64\n")
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "column", "Amount", "Other.Metric.Level", table="Sales")
        self.assertEqual(caught.exception.code, "NAME_TAKEN")

    def test_table_rename_destination_extending_new_namespace_path_refused(self):
        ws, project, mdef, path = self._report_project({"refs": [{"queryRef": "Orders.Region.Amount.Level"}]})
        _write(os.path.join(mdef, "tables", "Orders.tmdl"),
               "table Orders\n\tcolumn 'Region.Amount.Level'\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Sales.Region.tmdl"),
               "table 'Sales.Region'\n\tcolumn Amount\n\t\tdataType: int64\n")
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "table", "Orders", "Sales")
        self.assertEqual(caught.exception.code, "NAME_TAKEN")

    def test_table_rename_retained_suffix_destination_refused(self):
        ws, project, mdef, path = self._report_project({"refs": [{"queryRef": "A.X.Level"}]})
        _write(os.path.join(mdef, "tables", "A.tmdl"),
               "table A\n\tcolumn X\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "B.X.tmdl"),
               "table 'B.X'\n\tcolumn Level\n\t\tdataType: int64\n")
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "table", "A", "B")
        self.assertEqual(caught.exception.code, "NAME_TAKEN")

    def test_column_rename_wrapped_destination_owned_refused(self):
        ws, project, mdef, path = self._report_project({"refs": [{"queryRef": "Sum(Sales.Amount)"}]})
        _write(os.path.join(mdef, "tables", "Odd.tmdl"),
               "table 'Sum(Sales'\n\tcolumn 'Revenue)'\n\t\tdataType: int64\n")
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "column", "Amount", "Revenue", table="Sales")
        self.assertEqual(caught.exception.code, "NAME_TAKEN")

    def test_table_rename_destination_extending_new_entity_extension_refused(self):
        ws, project, mdef, path = self._report_project({"refs": [{"queryRef": "A.X.Y.Level"}]})
        _write(os.path.join(mdef, "tables", "A.tmdl"),
               "table A\n\tcolumn 'X.Y.Level'\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "B.X", "measures": [{"name": "Y", "expression": "1"}]}]}))
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "table", "A", "B")
        self.assertEqual(caught.exception.code, "NAME_TAKEN")

    def test_table_rename_wrapped_destination_inner_path_refused(self):
        ws, project, mdef, path = self._report_project({"refs": [{"queryRef": "Sum(A.X.Level)"}]})
        _write(os.path.join(mdef, "tables", "A.tmdl"),
               "table A\n\tcolumn X\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "B.X.tmdl"),
               "table 'B.X'\n\tcolumn Level\n\t\tdataType: int64\n")
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "table", "A", "B")
        self.assertEqual(caught.exception.code, "NAME_TAKEN")

    def test_table_rename_to_shorter_name_own_extension_path_follows(self):
        ws, project, mdef, path = self._report_project({"refs": [{"queryRef": "Sales.Region.Total.Level"}]})
        os.remove(os.path.join(mdef, "tables", "Sales.tmdl"))
        _write(os.path.join(mdef, "tables", "Sales.Region.tmdl"),
               "table 'Sales.Region'\n\tcolumn Amount\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Sales.Region", "measures": [{"name": "Total", "expression": "1"}]}]}))
        rename.rename(ws, project, "table", "Sales.Region", "Sales")
        self.assertEqual(json.loads(path.read_text())["refs"][0]["queryRef"], "Sales.Total.Level")

    def test_table_rename_to_shorter_name_no_self_preflight_block(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        os.remove(os.path.join(mdef, "tables", "Sales.tmdl"))
        _write(os.path.join(mdef, "tables", "Sales.Region.tmdl"),
               "table 'Sales.Region'\n\tcolumn 'Region.Total'\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Sales.Region", "measures": [{"name": "Total", "expression": "1"}]}]}))
        rename.rename(ws, project, "table", "Sales.Region", "Sales")

    def test_table_rename_shorter_source_not_blocked_by_destination_set(self):
        ws, project, mdef, path = self._report_project({"refs": [{"queryRef": "A.B.C.X"}]})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "ABC.tmdl"),
               "table 'A.B.C'\n\tcolumn X\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "A.B", "measures": [{"name": "C", "expression": "1"}]}]}))
        rename.rename(ws, project, "table", "A.B.C", "A")
        self.assertEqual(json.loads(path.read_text())["refs"][0]["queryRef"], "A.X")

    def test_table_rename_longer_destination_extending_shorter_entity_allowed(self):
        ws, project, mdef, path = self._report_project({"refs": [{"queryRef": "A.X"}]})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "A.tmdl"),
               "table A\n\tcolumn X\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "A.B", "measures": [{"name": "C", "expression": "1"}]}]}))
        rename.rename(ws, project, "table", "A", "A.B.C")
        self.assertEqual(json.loads(path.read_text())["refs"][0]["queryRef"], "A.B.C.X")

    def test_table_rename_destination_suffix_classified_post_rename(self):
        ws, project, mdef, path = self._report_project({"refs": [{"queryRef": "Old.Sub.X.Old.Level"}]})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Old.tmdl"),
               "table Old\n\tcolumn 'Sub.X.Old.Level'\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Dest.Sub.tmdl"),
               "table 'Dest.Sub'\n\tcolumn X\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Old", "measures": [{"name": "M", "expression": "1"}]}]}))
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "table", "Old", "Dest")
        self.assertEqual(caught.exception.code, "NAME_TAKEN")

    def test_table_rename_emitting_unrecognizable_variation_path_refused(self):
        hl = {"HierarchyLevel": {"Expression": {"Hierarchy": {
                "Expression": {"PropertyVariationSource": {"Expression": {"SourceRef": {"Entity": "Sales"}},
                                                           "Property": "OrderDate"}},
                "Hierarchy": "Calendar"}}, "Level": "Year"},
              "queryRef": "Sales.OrderDate.Calendar.Year"}
        ws, project, mdef, path = self._report_project({"projections": [hl]})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn OrderDate\n\t\tdataType: dateTime\n")
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "table", "Sales", "Calendar")
        self.assertEqual(caught.exception.code, "AMBIGUOUS")

    def test_table_rename_unrecognizable_path_not_masked_by_longer_column(self):
        hl = {"HierarchyLevel": {"Expression": {"Hierarchy": {
                "Expression": {"PropertyVariationSource": {"Expression": {"SourceRef": {"Entity": "Sales"}},
                                                           "Property": "OrderDate"}},
                "Hierarchy": "Calendar"}}, "Level": "Year"},
              "queryRef": "Sales.OrderDate.Calendar.Year"}
        ws, project, mdef, path = self._report_project({"projections": [hl]})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn OrderDate\n\t\tdataType: dateTime\n\tcolumn 'OrderDate.Calendar'\n\t\tdataType: string\n")
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "table", "Sales", "Calendar")
        self.assertEqual(caught.exception.code, "AMBIGUOUS")

    def test_table_rename_unrecognizable_extension_path_unicode_name_refused(self):
        ws, project, mdef, path = self._report_project({"refs": [{"queryRef": "İ.M.Calendar.Year"}]})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "I.tmdl"),
               "table 'İ'\n\tcolumn X\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "İ", "measures": [{"name": "M", "expression": "1"}]}]}))
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "table", "İ", "Calendar")
        self.assertEqual(caught.exception.code, "AMBIGUOUS")

    def test_table_rename_literal_destination_not_reparsed_as_wrapper(self):
        fld = {"Column": {"Expression": {"SourceRef": {"Entity": "Totals"}}, "Property": "Amount)"},
               "queryRef": "Totals.Amount)"}
        ws, project, mdef, path = self._report_project({"projections": [fld]})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Totals.tmdl"),
               "table Totals\n\tcolumn 'Amount)'\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        rename.rename(ws, project, "table", "Totals", "Sum(Sales")
        self.assertEqual(json.loads(path.read_text())["projections"][0]["queryRef"], "Sum(Sales.Amount)")

    def test_table_rename_literal_paths_looking_like_wrappers_follow(self):
        fld = {"Column": {"Expression": {"SourceRef": {"Entity": "Sum(Old"}}, "Property": "Amount)"},
               "queryRef": "Sum(Old.Amount)"}
        ws, project, mdef, path = self._report_project({"projections": [fld]})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "SumOld.tmdl"),
               "table 'Sum(Old'\n\tcolumn 'Amount)'\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "New.tmdl"),
               "table New\n\tcolumn Amount\n\t\tdataType: int64\n")
        rename.rename(ws, project, "table", "Sum(Old", "Sum(New")
        self.assertEqual(json.loads(path.read_text())["projections"][0]["queryRef"], "Sum(New.Amount)")

    def test_table_rename_wrapped_emission_recognizability_on_inner_path(self):
        ws, project, mdef, path = self._report_project({"refs": [{"metadata": "Sum(Sales.Amount.Level)"}]})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "SumRevenue.tmdl"),
               "table 'Sum(Revenue'\n\tcolumn Amount\n\t\tdataType: int64\n")
        rename.rename(ws, project, "table", "Sales", "Revenue")
        self.assertEqual(json.loads(path.read_text())["refs"][0]["metadata"], "Sum(Revenue.Amount.Level)")

    def test_table_rename_recognizability_picks_most_consuming_base(self):
        ws, project, mdef, path = self._report_project({"refs": [{"queryRef": "Sales.İİİ.X.Y"}]})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        col1 = "i\u0307" * 3
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn '" + col1 + "'\n\t\tdataType: string\n\tcolumn 'İİİ.X'\n\t\tdataType: string\n")
        rename.rename(ws, project, "table", "Sales", "X")
        self.assertEqual(json.loads(path.read_text())["refs"][0]["queryRef"], "X.İİİ.X.Y")

    def test_column_rename_destination_extension_spelling_invariant(self):
        for new_name in ("Region.Amount.\u0130", "Region.Amount.i\u0307"):
            ws, project, mdef, path = self._report_project({"refs": []})
            _write(os.path.join(mdef, "tables", "Sales.tmdl"),
                   "table Sales\n\tcolumn Cost\n\t\tdataType: int64\n")
            _write(os.path.join(mdef, "tables", "Sales.Region.tmdl"),
                   "table 'Sales.Region'\n\tcolumn Amount\n\t\tdataType: int64\n")
            with self.assertRaises(PbzError) as caught:
                rename.rename(ws, project, "column", "Cost", new_name, table="Sales")
            self.assertEqual(caught.exception.code, "NAME_TAKEN")

    def test_column_rename_destination_extension_with_mark_inside_name_refused(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Cost\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Sales.Region.tmdl"),
               "table 'Sales.Region'\n\tcolumn Amount\n\t\tdataType: int64\n")
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "column", "Cost", "Region.Amount.İSales.Year", table="Sales")
        self.assertEqual(caught.exception.code, "NAME_TAKEN")

    def test_column_rename_extension_bare_and_spaced_refs_stay_row_context_follows(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [{"name": "Amount", "expression": "7"},
                                            {"name": "Twice", "expression": "2 * [Amount]"},
                                            {"name": "Total", "expression": "SUMX('Sales', Budget [Amount])"},
                                            {"name": "Ctx", "expression": "SUMX('Sales', [Amount])"}]}]}))
        rename.rename(ws, project, "column", "Amount", "Net", table="Sales")
        ms = {m["name"]: m["expression"]
              for m in json.loads(ext_path.read_text())["entities"][0]["measures"]}
        self.assertEqual(ms["Twice"], "2 * [Amount]")
        self.assertEqual(ms["Total"], "SUMX('Sales', Budget [Amount])")
        self.assertEqual(ms["Ctx"], "SUMX('Sales', [Net])")

    def test_column_rename_into_column_on_other_table_refused(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Budget.tmdl"),
               "table Budget\n\tcolumn Total\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [{"name": "M", "expression": "SUM([Amount])"}]}]}))
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "column", "Amount", "Total", table="Sales")
        self.assertEqual(caught.exception.code, "NAME_TAKEN")

    def test_table_rename_capturing_extension_var_refused(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "VAR Batch = 1 RETURN COUNTROWS(Sales)"}]}]}))
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "table", "Sales", "Batch")
        self.assertEqual(caught.exception.code, "AMBIGUOUS")

    def test_column_rename_return_keyword_not_a_spaced_qualifier(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Return", "measures": [{"name": "Amount", "expression": "7"}]},
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "SUMX(Sales, VAR x = 1 RETURN [Amount] + x)"}]}]}))
        rename.rename(ws, project, "column", "Amount", "Net", table="Sales")
        ms = json.loads(ext_path.read_text())["entities"][1]["measures"]
        self.assertEqual(ms[0]["expression"], "SUMX(Sales, VAR x = 1 RETURN [Net] + x)")

    def test_table_rename_keyword_qualifier_untouched_quoted_follows(self):
        def fresh(expr):
            ws, project, mdef, path = self._report_project({"refs": []})
            for f in os.listdir(os.path.join(mdef, "tables")):
                os.remove(os.path.join(mdef, "tables", f))
            _write(os.path.join(mdef, "tables", "Sales.tmdl"),
                   "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
            _write(os.path.join(mdef, "tables", "Return.tmdl"),
                   "table Return\n\tcolumn Code\n\t\tdataType: string\n")
            ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
            ext_path.write_text(json.dumps({"name": "extension", "entities": [
                {"name": "Return", "measures": [{"name": "Amount", "expression": "7"}]},
                {"name": "Budget", "measures": [{"name": "M", "expression": expr}]}]}))
            return ws, project, ext_path

        ws, project, ext_path = fresh("SUMX(Sales, VAR x = 1 RETURN [Amount] + x)")
        rename.rename(ws, project, "table", "Return", "Archive")
        m = json.loads(ext_path.read_text())["entities"][1]["measures"][0]["expression"]
        self.assertEqual(m, "SUMX(Sales, VAR x = 1 RETURN [Amount] + x)")

        ws, project, ext_path = fresh("SUM('Return' [Amount])")
        rename.rename(ws, project, "table", "Return", "Archive")
        m = json.loads(ext_path.read_text())["entities"][1]["measures"][0]["expression"]
        self.assertEqual(m, "SUM('Archive' [Amount])")

    def test_column_rename_keyword_qualifier_row_context_still_follows(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Return.tmdl"),
               "table Return\n\tcolumn Code\n\t\tdataType: string\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Return", "measures": [{"name": "Amount", "expression": "7"}]},
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "SUMX(Sales, VAR x = 1 RETURN [Amount] + x)"}]}]}))
        rename.rename(ws, project, "column", "Amount", "Net", table="Sales")
        ms = json.loads(ext_path.read_text())["entities"][1]["measures"]
        self.assertEqual(ms[0]["expression"], "SUMX(Sales, VAR x = 1 RETURN [Net] + x)")

    def test_unanchored_bare_native_query_ref_with_two_owners_stays(self):
        ws, project, mdef, path = self._report_project({"projections": [{"nativeQueryRef": "Amount"}]})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Budget.tmdl"),
               "table Budget\n\tcolumn Amount\n\t\tdataType: int64\n")
        rename.rename(ws, project, "column", "Amount", "NetAmount", table="Sales")
        self.assertEqual(json.loads(path.read_text()),
                         {"projections": [{"nativeQueryRef": "Amount"}]})

    def test_table_rename_return_keyword_standalone_untouched(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Return.tmdl"),
               "table Return\n\tcolumn Code\n\t\tdataType: string\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [{"name": "M", "expression": "VAR x = 1 RETURN x"}]}]}))
        rename.rename(ws, project, "table", "Return", "Archive")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"]
        self.assertEqual(m, "VAR x = 1 RETURN x")

    def test_unanchored_bare_native_query_ref_vs_extension_measure_stays(self):
        ws, project, mdef, path = self._report_project({"projections": [{"nativeQueryRef": "Amount"}]})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [{"name": "Amount", "expression": "7"}]}]}))
        rename.rename(ws, project, "column", "Amount", "NetTotal", table="Sales")
        self.assertEqual(json.loads(path.read_text()),
                         {"projections": [{"nativeQueryRef": "Amount"}]})

    def test_unanchored_bare_native_query_ref_dotted_name_two_owners_stays(self):
        ws, project, mdef, path = self._report_project({"projections": [{"nativeQueryRef": "Net.Amount"}]})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn 'Net.Amount'\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Budget.tmdl"),
               "table Budget\n\tcolumn 'Net.Amount'\n\t\tdataType: int64\n")
        rename.rename(ws, project, "column", "Net.Amount", "NetTotal", table="Sales")
        self.assertEqual(json.loads(path.read_text()),
                         {"projections": [{"nativeQueryRef": "Net.Amount"}]})

    def test_unanchored_bare_ref_dotted_extension_measure_blocks_plain_does_not(self):
        ws, project, mdef, path = self._report_project({"projections": [{"nativeQueryRef": "Net.Amount"}]})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn 'Net.Amount'\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [{"name": "Net.Amount", "expression": "7"}]}]}))
        rename.rename(ws, project, "column", "Net.Amount", "Revised", table="Sales")
        self.assertEqual(json.loads(path.read_text()),
                         {"projections": [{"nativeQueryRef": "Net.Amount"}]})

        ws, project, mdef, path = self._report_project({"projections": [{"nativeQueryRef": "Amount"}]})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [{"name": "Net.Amount", "expression": "7"}]}]}))
        rename.rename(ws, project, "column", "Amount", "Revised", table="Sales")
        self.assertEqual(json.loads(path.read_text()),
                         {"projections": [{"nativeQueryRef": "Revised"}]})

    def test_wrapped_query_ref_emission_ignores_bare_name_collision(self):
        ws, project, mdef, path = self._report_project({"refs": [{"queryRef": "Sum(Sales.Amount)"}]})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Budget.tmdl"),
               "table Budget\n\tcolumn 'Sum(Sales.Total)'\n\t\tdataType: int64\n")
        rename.rename(ws, project, "column", "Amount", "Total", table="Sales")
        self.assertEqual(json.loads(path.read_text()), {"refs": [{"queryRef": "Sum(Sales.Total)"}]})

    def test_table_rename_to_keyword_name_quotes_and_renames_on(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [{"name": "M", "expression": "COUNTROWS(Sales)"}]}]}))
        rename.rename(ws, project, "table", "Sales", "Return")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"]
        self.assertEqual(m, "COUNTROWS('Return')")
        rename.rename(ws, project, "table", "Return", "Archive")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"]
        self.assertEqual(m, "COUNTROWS('Archive')")

    def test_bare_ref_ambiguous_between_model_and_extension_measure_stays(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tmeasure M = 1\n\tcolumn Amount\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [{"name": "M", "expression": "7"}]},
            {"name": "Forecast", "measures": [{"name": "F", "expression": "[M]"}]}]}))
        rename.rename(ws, project, "measure", "M", "Net", table="Sales")
        f = json.loads(ext_path.read_text())["entities"][1]["measures"][0]["expression"]
        self.assertEqual(f, "[M]")

    def test_column_rename_keyword_mask_keeps_row_context(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Budget.tmdl"),
               "table Budget\n\tcolumn Amount\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Return.tmdl"),
               "table Return\n\tcolumn Code\n\t\tdataType: string\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "SUMX(Sales, VAR x = Budget RETURN [Amount])"}]}]}))
        rename.rename(ws, project, "column", "Amount", "Net", table="Sales")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"]
        self.assertEqual(m, "SUMX(Sales, VAR x = Budget RETURN [Net])")

    def test_table_rename_sort_keyword_untouched_quoted_follows(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Asc.tmdl"),
               "table Asc\n\tcolumn Code\n\t\tdataType: string\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "RANKX(Sales, Sales[Amount], , ASC)"},
                {"name": "N", "expression": "COUNTROWS('Asc')"}]}]}))
        rename.rename(ws, project, "table", "Asc", "Archive")
        ms = {m["name"]: m["expression"]
              for m in json.loads(ext_path.read_text())["entities"][0]["measures"]}
        self.assertEqual(ms["M"], "RANKX(Sales, Sales[Amount], , ASC)")
        self.assertEqual(ms["N"], "COUNTROWS('Archive')")

    def test_table_rename_dense_ties_untouched_quoted_follows(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Dense.tmdl"),
               "table Dense\n\tcolumn Code\n\t\tdataType: string\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "RANKX(Sales, Sales[Amount], , DESC, DENSE)"},
                {"name": "N", "expression": "COUNTROWS('Dense')"}]}]}))
        rename.rename(ws, project, "table", "Dense", "Archive")
        ms = {m["name"]: m["expression"]
              for m in json.loads(ext_path.read_text())["entities"][0]["measures"]}
        self.assertEqual(ms["M"], "RANKX(Sales, Sales[Amount], , DESC, DENSE)")
        self.assertEqual(ms["N"], "COUNTROWS('Archive')")

    def test_table_rename_to_reserved_keyword_destination_quoted(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [{"name": "M", "expression": "COUNTROWS(Sales)"}]}]}))
        rename.rename(ws, project, "table", "Sales", "Evaluate")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"]
        self.assertEqual(m, "COUNTROWS('Evaluate')")

    def test_table_rename_interval_argument_untouched_quoted_follows(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Calendar.tmdl"),
               "table Calendar\n\tcolumn Date\n\t\tdataType: dateTime\n")
        _write(os.path.join(mdef, "tables", "Year.tmdl"),
               "table Year\n\tcolumn Code\n\t\tdataType: string\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "COUNTROWS(DATEADD('Calendar'[Date], -1, YEAR)) + COUNTROWS('Year')"}]}]}))
        rename.rename(ws, project, "table", "Year", "Archive")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"]
        self.assertEqual(m, "COUNTROWS(DATEADD('Calendar'[Date], -1, YEAR)) + COUNTROWS('Archive')")

    def test_table_rename_datediff_interval_untouched_quoted_follows(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Hour.tmdl"),
               "table Hour\n\tcolumn Code\n\t\tdataType: string\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "DATEDIFF(DATE(2024,1,1), DATE(2024,1,2), HOUR) + COUNTROWS('Hour')"}]}]}))
        rename.rename(ws, project, "table", "Hour", "Archive")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"]
        self.assertEqual(m, "DATEDIFF(DATE(2024,1,1), DATE(2024,1,2), HOUR) + COUNTROWS('Archive')")

    def test_column_rename_keyword_row_context_poison_masked(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Return.tmdl"),
               "table Return\n\tcolumn Amount\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "SUMX(VAR x = 1 RETURN Sales, [Amount])"}]}]}))
        rename.rename(ws, project, "column", "Amount", "Net", table="Sales")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"]
        self.assertEqual(m, "SUMX(VAR x = 1 RETURN Sales, [Net])")

    def test_table_rename_crossfilter_direction_untouched_quoted_follows(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn ProductKey\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Product.tmdl"),
               "table Product\n\tcolumn ProductKey\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Both.tmdl"),
               "table Both\n\tcolumn Code\n\t\tdataType: string\n")
        _write(os.path.join(mdef, "tables", "relationships.tmdl"),
               "relationship rel1\n\tfromColumn: Sales.ProductKey\n\ttoColumn: Product.ProductKey\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "CALCULATE(COUNTROWS('Both'), CROSSFILTER(Sales[ProductKey], Product[ProductKey], BOTH))"}]}]}))
        rename.rename(ws, project, "table", "Both", "Archive")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"]
        self.assertEqual(m, "CALCULATE(COUNTROWS('Archive'), CROSSFILTER(Sales[ProductKey], Product[ProductKey], BOTH))")

    def test_table_rename_crossfilter_directional_spelling_untouched(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "A.tmdl"),
               "table A\n\tcolumn K\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "B.tmdl"),
               "table B\n\tcolumn K\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "OneWay_LeftFiltersRight.tmdl"),
               "table OneWay_LeftFiltersRight\n\tcolumn Code\n\t\tdataType: string\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "CALCULATE(COUNTROWS('OneWay_LeftFiltersRight'), CROSSFILTER(A[K], B[K], OneWay_LeftFiltersRight))"}]}]}))
        rename.rename(ws, project, "table", "OneWay_LeftFiltersRight", "Archive")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"]
        self.assertEqual(m, "CALCULATE(COUNTROWS('Archive'), CROSSFILTER(A[K], B[K], OneWay_LeftFiltersRight))")

    def test_column_rename_keyword_touching_bracket_still_follows(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [{"name": "Amount", "expression": "7"},
                                            {"name": "M", "expression": "SUMX(Sales, VAR x = 1 RETURN[Amount] + x)"}]}]}))
        rename.rename(ws, project, "column", "Amount", "Net", table="Sales")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][1]["expression"]
        self.assertEqual(m, "SUMX(Sales, VAR x = 1 RETURN[Net] + x)")

    def test_table_rename_window_boundary_modes_untouched_quoted_follows(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Abs.tmdl"),
               "table Abs\n\tcolumn Amount\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "COUNTROWS(WINDOW(1, ABS, 0, REL, ALL(Sales[Amount]), ORDERBY(Sales[Amount], ASC)))"},
                {"name": "N", "expression": "SUM('Abs'[Amount])"}]}]}))
        rename.rename(ws, project, "table", "Abs", "Archive")
        ms = {m["name"]: m["expression"]
              for m in json.loads(ext_path.read_text())["entities"][0]["measures"]}
        self.assertEqual(ms["M"], "COUNTROWS(WINDOW(1, ABS, 0, REL, ALL(Sales[Amount]), ORDERBY(Sales[Amount], ASC)))")
        self.assertEqual(ms["N"], "SUM('Archive'[Amount])")

    def test_table_rename_rank_blank_option_untouched_quoted_follows(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Last.tmdl"),
               "table Last\n\tcolumn Code\n\t\tdataType: string\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "RANK(DENSE, ALL(Sales[Amount]), ORDERBY(Sales[Amount], ASC), LAST) + COUNTROWS('Last')"}]}]}))
        rename.rename(ws, project, "table", "Last", "Archive")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"]
        self.assertEqual(m, "RANK(DENSE, ALL(Sales[Amount]), ORDERBY(Sales[Amount], ASC), LAST) + COUNTROWS('Archive')")

    def test_table_rename_blanks_and_convert_type_untouched(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Blanks.tmdl"),
               "table Blanks\n\tcolumn Code\n\t\tdataType: string\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "RANK(DENSE, ALL(Sales[Amount]), ORDERBY(Sales[Amount], ASC BLANKS LAST))"}]}]}))
        rename.rename(ws, project, "table", "Blanks", "Archive")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"]
        self.assertEqual(m, "RANK(DENSE, ALL(Sales[Amount]), ORDERBY(Sales[Amount], ASC BLANKS LAST))")

        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Integer.tmdl"),
               "table Integer\n\tcolumn Code\n\t\tdataType: string\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "CONVERT(1, INTEGER) + COUNTROWS('Integer')"}]}]}))
        rename.rename(ws, project, "table", "Integer", "Archive")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"]
        self.assertEqual(m, "CONVERT(1, INTEGER) + COUNTROWS('Archive')")

    def test_table_rename_datatable_type_and_precise_untouched(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Int64.tmdl"),
               "table Int64\n\tcolumn Code\n\t\tdataType: string\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "COUNTROWS(DATATABLE(\"K\", INT64, {{1}})) + COUNTROWS('Int64')"}]}]}))
        rename.rename(ws, project, "table", "Int64", "Archive")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"]
        self.assertEqual(m, "COUNTROWS(DATATABLE(\"K\", INT64, {{1}})) + COUNTROWS('Archive')")

        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "FiscalCalendar.tmdl"),
               "table FiscalCalendar\n\tcolumn Date\n\t\tdataType: dateTime\n")
        _write(os.path.join(mdef, "tables", "Precise.tmdl"),
               "table Precise\n\tcolumn Code\n\t\tdataType: string\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "COUNTROWS(DATEADD(FiscalCalendar, 1, MONTH, PRECISE)) + COUNTROWS('Precise')"}]}]}))
        rename.rename(ws, project, "table", "Precise", "Archive")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"]
        self.assertEqual(m, "COUNTROWS(DATEADD(FiscalCalendar, 1, MONTH, PRECISE)) + COUNTROWS('Archive')")

    def test_table_rename_dateadd_extending_option_untouched(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "FiscalCalendar.tmdl"),
               "table FiscalCalendar\n\tcolumn Date\n\t\tdataType: dateTime\n")
        _write(os.path.join(mdef, "tables", "Extending.tmdl"),
               "table Extending\n\tcolumn Code\n\t\tdataType: string\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "COUNTROWS(DATEADD(FiscalCalendar, 1, MONTH, EXTENDING)) + COUNTROWS('Extending')"}]}]}))
        rename.rename(ws, project, "table", "Extending", "Archive")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"]
        self.assertEqual(m, "COUNTROWS(DATEADD(FiscalCalendar, 1, MONTH, EXTENDING)) + COUNTROWS('Archive')")

    def test_table_rename_dateadd_anchored_option_untouched(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "FiscalCalendar.tmdl"),
               "table FiscalCalendar\n\tcolumn Date\n\t\tdataType: dateTime\n")
        _write(os.path.join(mdef, "tables", "Anchored.tmdl"),
               "table Anchored\n\tcolumn Code\n\t\tdataType: string\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "COUNTROWS(DATEADD(FiscalCalendar, -1, MONTH, PRECISE, ANCHORED)) + COUNTROWS('Anchored')"}]}]}))
        rename.rename(ws, project, "table", "Anchored", "Archive")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"]
        self.assertEqual(m, "COUNTROWS(DATEADD(FiscalCalendar, -1, MONTH, PRECISE, ANCHORED)) + COUNTROWS('Archive')")

    def test_table_rename_endaligned_and_text_type_untouched(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "FiscalCalendar.tmdl"),
               "table FiscalCalendar\n\tcolumn Date\n\t\tdataType: dateTime\n")
        _write(os.path.join(mdef, "tables", "EndAligned.tmdl"),
               "table EndAligned\n\tcolumn Code\n\t\tdataType: string\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "COUNTROWS(DATEADD(FiscalCalendar, 1, MONTH, ENDALIGNED)) + COUNTROWS('EndAligned')"}]}]}))
        rename.rename(ws, project, "table", "EndAligned", "Archive")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"]
        self.assertEqual(m, "COUNTROWS(DATEADD(FiscalCalendar, 1, MONTH, ENDALIGNED)) + COUNTROWS('Archive')")

        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Text.tmdl"),
               "table Text\n\tcolumn Code\n\t\tdataType: string\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "COUNTROWS(DATATABLE(\"K\", TEXT, {{\"x\"}})) + COUNTROWS('Text')"}]}]}))
        rename.rename(ws, project, "table", "Text", "Archive")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"]
        self.assertEqual(m, "COUNTROWS(DATATABLE(\"K\", TEXT, {{\"x\"}})) + COUNTROWS('Archive')")

    def test_table_rename_datatable_decimal_alias_untouched(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Decimal.tmdl"),
               "table Decimal\n\tcolumn Code\n\t\tdataType: string\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "COUNTROWS(DATATABLE(\"K\", DECIMAL, {{1}})) + COUNTROWS('Decimal')"}]}]}))
        rename.rename(ws, project, "table", "Decimal", "Archive")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"]
        self.assertEqual(m, "COUNTROWS(DATATABLE(\"K\", DECIMAL, {{1}})) + COUNTROWS('Archive')")

    def test_column_rename_extension_ref_does_not_poison_row_inference(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n\tcolumn SortKey\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Decoy.tmdl"),
               "table Decoy\n\tcolumn Limit\n\t\tdataType: int64\n\tcolumn Amount\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [{"name": "Limit", "expression": "3"}]},
            {"name": "Decoy", "measures": [
                {"name": "M", "expression": "SUMX(TOPN(Budget [Limit], Sales, Sales[SortKey]), [Amount])"}]}]}))
        rename.rename(ws, project, "column", "Amount", "Net", table="Sales")
        m = json.loads(ext_path.read_text())["entities"][1]["measures"][0]["expression"]
        self.assertEqual(m, "SUMX(TOPN(Budget [Limit], Sales, Sales[SortKey]), [Net])")

    def test_bare_extension_measure_does_not_poison_row_inference(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n\tcolumn SortKey\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Decoy.tmdl"),
               "table Decoy\n\tcolumn Amount\n\t\tdataType: int64\n\tcolumn Limit\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [{"name": "Limit", "expression": "3"},
                                            {"name": "M", "expression": "SUMX(TOPN([Limit], Sales, Sales[SortKey]), [Amount])"}]}]}))
        rename.rename(ws, project, "column", "Amount", "Net", table="Decoy")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][1]["expression"]
        self.assertEqual(m, "SUMX(TOPN([Limit], Sales, Sales[SortKey]), [Amount])")

    def test_anchor_in_select_name_counts_for_bare_native_query_ref(self):
        ws, project, mdef, path = self._report_project({
            "Select": [{"Name": "Sales.Amount", "Expression": "Sales[Amount]"}],
            "projections": [{"nativeQueryRef": "Amount"}]})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Budget.tmdl"),
               "table Budget\n\tcolumn Amount\n\t\tdataType: int64\n")
        rename.rename(ws, project, "column", "Amount", "Net", table="Sales")
        self.assertEqual(json.loads(path.read_text())["projections"][0]["nativeQueryRef"], "Net")

    def test_table_rename_datatable_logical_alias_untouched(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Logical.tmdl"),
               "table Logical\n\tcolumn Code\n\t\tdataType: string\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "M", "expression": "COUNTROWS(DATATABLE(\"Flag\", LOGICAL, {{TRUE()}})) + COUNTROWS('Logical')"}]}]}))
        rename.rename(ws, project, "table", "Logical", "Archive")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["expression"]
        self.assertEqual(m, "COUNTROWS(DATATABLE(\"Flag\", LOGICAL, {{TRUE()}})) + COUNTROWS('Archive')")

    def test_column_rename_model_table_extension_ref_does_not_poison_row_inference(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n\tcolumn SortKey\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "Decoy.tmdl"),
               "table Decoy\n\tcolumn Limit\n\t\tdataType: int64\n\tcolumn Amount\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Decoy", "measures": [{"name": "Limit", "expression": "3"},
                                           {"name": "M", "expression": "SUMX(TOPN(Decoy [Limit], Sales, Sales[SortKey]), [Amount])"}]}]}))
        rename.rename(ws, project, "column", "Amount", "Net", table="Sales")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][1]["expression"]
        self.assertEqual(m, "SUMX(TOPN(Decoy [Limit], Sales, Sales[SortKey]), [Net])")

    def test_table_rename_extension_ref_qualifier_still_follows(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Decoy.tmdl"),
               "table Decoy\n\tcolumn Limit\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Decoy", "measures": [{"name": "Limit", "expression": "3"},
                                           {"name": "M", "expression": "Decoy [Limit] + COUNTROWS(Decoy)"}]}]}))
        rename.rename(ws, project, "table", "Decoy", "Plan")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][1]["expression"]
        self.assertEqual(m, "Plan [Limit] + COUNTROWS(Plan)")

    def test_table_rename_mask_glue_phantom_untouched(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "RETURN111111.tmdl"),
               "table RETURN111111\n\tcolumn Code\n\t\tdataType: string\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "B", "measures": [{"name": "M", "expression": "3"},
                                       {"name": "N", "expression": "VAR x = 1 RETURN'B'[M]"}]}]}))
        rename.rename(ws, project, "table", "RETURN111111", "Archive")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][1]["expression"]
        self.assertEqual(m, "VAR x = 1 RETURN'B'[M]")

    def test_column_rename_mask_glue_does_not_steer_row_inference(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n\tcolumn SortKey\n\t\tdataType: int64\n")
        _write(os.path.join(mdef, "tables", "RETURN111111.tmdl"),
               "table RETURN111111\n\tcolumn Amount\n\t\tdataType: int64\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        expr = "SUMX(TOPN(VAR x = 1 RETURN'B'[M], Sales, Sales[SortKey]), [Amount])"
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "B", "measures": [{"name": "M", "expression": "3"},
                                       {"name": "N", "expression": expr}]}]}))
        rename.rename(ws, project, "column", "Amount", "Net", table="RETURN111111")
        m = json.loads(ext_path.read_text())["entities"][0]["measures"][1]["expression"]
        self.assertEqual(m, expr)

    def test_table_rename_nested_entities_array_untouched(self):
        doc = {"payload": {"entities": [{"name": "Sales", "measures": [
            {"name": "Local", "expression": "1"},
            {"name": "UseLocal", "expression": "Sales[Local]"}]}]}}
        ws, project, mdef, path = self._report_project(doc)
        rename.rename(ws, project, "table", "Sales", "Revenue")
        got = json.loads(path.read_text())
        ent = got["payload"]["entities"][0]
        self.assertEqual(ent["name"], "Sales")
        self.assertEqual(ent["measures"][1]["expression"], "Sales[Local]")

    def test_table_rename_empty_key_entities_array_untouched(self):
        doc = {"": {"entities": [{"name": "Sales", "measures": [
            {"name": "Local", "expression": "1"},
            {"name": "UseLocal", "expression": "Sales[Local]"}]}]}}
        ws, project, mdef, path = self._report_project(doc)
        rename.rename(ws, project, "table", "Sales", "Revenue")
        got = json.loads(path.read_text())
        ent = got[""]["entities"][0]
        self.assertEqual(ent["name"], "Sales")
        self.assertEqual(ent["measures"][1]["expression"], "Sales[Local]")

    def test_column_rename_duplicate_entity_entries_union(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [{"name": "Amount", "expression": "1"}]},
            {"name": "Budget", "measures": [{"name": "Forecast", "expression": "2"}]},
            {"name": "Sales", "measures": [{"name": "UseIt", "expression": "Budget [Amount]"}]}]}))
        rename.rename(ws, project, "column", "Amount", "Net", table="Sales")
        m = json.loads(ext_path.read_text())["entities"][2]["measures"][0]["expression"]
        self.assertEqual(m, "Budget [Amount]")

    def test_column_case_only_rename_allowed_with_same_named_column_elsewhere(self):
        # A case-only rename changes nothing in folded space, so the
        # cross-table destination check does not fire: the base suite
        # pins this in test_modeling_atomic.test_case_only_column_rename
        # (Other.Amount present). The qualified reference follows and
        # the other table's column is untouched.
        ws, project, mdef, path = self._report_project(
            {"refs": [{"queryRef": "Sales.Amount"}]})
        rename.rename(ws, project, "column", "Amount", "AMOUNT", table="Sales")
        got = json.loads(path.read_text())["refs"][0]["queryRef"]
        self.assertEqual(got, "Sales.AMOUNT")
        other = Path(mdef, "tables", "Other.tmdl").read_text()
        self.assertIn("column Amount", other)

    def test_column_rename_wrapper_spelling_fold_consistent(self):
        for wrap in ("A\u0130", "Ai\u0307"):
            ws, project, mdef, path = self._report_project(
                {"refs": [{"queryRef": f"{wrap}(Sales.Amount)"}]})
            rename.rename(ws, project, "column", "Amount", "Net", table="Sales")
            got = json.loads(path.read_text())["refs"][0]["queryRef"]
            self.assertEqual(got, f"{wrap}(Sales.Net)")

    def test_nearer_ambiguous_container_stops_field_inheritance(self):
        def fld(entity):
            return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": "Amount"}}
        doc = {"projections": [{
            **fld("Sales"),
            "details": {"left": fld("Budget"), "right": fld("Forecast"),
                        "nativeQueryRef": "Amount"}}]}
        ws, project, mdef, path = self._report_project(doc)
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        for name in ("Sales", "Budget", "Forecast"):
            _write(os.path.join(mdef, "tables", f"{name}.tmdl"),
                   f"table {name}\n\tcolumn Amount\n\t\tdataType: int64\n")
        rename.rename(ws, project, "column", "Amount", "Net", table="Sales")
        got = json.loads(path.read_text())["projections"][0]["details"]["nativeQueryRef"]
        self.assertEqual(got, "Amount")

    def test_nearer_ambiguous_container_stops_inheritance_through_nested_array(self):
        def fld(entity):
            return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": "Amount"}}
        doc = {"projections": [{
            **fld("Sales"),
            "details": {"left": fld("Budget"), "right": fld("Forecast"),
                        "refs": [{"nativeQueryRef": "Amount"}]}}]}
        ws, project, mdef, path = self._report_project(doc)
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        for name in ("Sales", "Budget", "Forecast"):
            _write(os.path.join(mdef, "tables", f"{name}.tmdl"),
                   f"table {name}\n\tcolumn Amount\n\t\tdataType: int64\n")
        rename.rename(ws, project, "column", "Amount", "Net", table="Sales")
        got = json.loads(path.read_text())["projections"][0]["details"]["refs"][0]["nativeQueryRef"]
        self.assertEqual(got, "Amount")

    def test_two_calls_are_not_one_wrapper(self):
        ws, project, mdef, path = self._report_project(
            {"refs": [{"metadata": "SUM(Sales.A) + SUM(Sales.B)"}]})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn A\n\t\tdataType: int64\n\tcolumn B\n\t\tdataType: int64\n"
               "\tcolumn 'A) + SUM(Sales.B'\n\t\tdataType: int64\n")
        rename.rename(ws, project, "column", "A) + SUM(Sales.B", "Net", table="Sales")
        got = json.loads(path.read_text())["refs"][0]["metadata"]
        self.assertEqual(got, "SUM(Sales.A) + SUM(Sales.B)")

    def test_table_rename_breaking_wrapper_refused(self):
        ws, project, mdef, path = self._report_project(
            {"refs": [{"queryRef": "Sum(Sales.Amount)"}]})
        with self.assertRaises(PbzError) as ctx:
            rename.rename(ws, project, "table", "Sales", "Sales(")
        self.assertEqual(ctx.exception.code, "NAME_TAKEN")
        got = json.loads(path.read_text())["refs"][0]["queryRef"]
        self.assertEqual(got, "Sum(Sales.Amount)")

    def _references_project(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        for f in os.listdir(os.path.join(mdef, "tables")):
            os.remove(os.path.join(mdef, "tables", f))
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tcolumn Amount\n\t\tdataType: int64\n\tmeasure Net = 1\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [
                {"name": "UseNet", "expression": "Sales[Net]",
                 "references": {"measures": [{"entity": "Sales", "name": "Net"},
                                             {"entity": "Sales", "name": "Net", "schema": "extension"}]}}]}]}))
        return ws, project, ext_path

    def test_measure_rename_references_record_follows(self):
        ws, project, ext_path = self._references_project()
        rename.rename(ws, project, "measure", "Net", "Margin")
        ent = json.loads(ext_path.read_text())["entities"][0]["measures"][0]
        self.assertEqual(ent["expression"], "Sales[Margin]")
        recs = ent["references"]["measures"]
        self.assertEqual(recs[0]["name"], "Margin")
        self.assertEqual(recs[1]["name"], "Net")

    def test_table_rename_references_record_entity_follows(self):
        ws, project, ext_path = self._references_project()
        rename.rename(ws, project, "table", "Sales", "Revenue")
        recs = json.loads(ext_path.read_text())["entities"][0]["measures"][0]["references"]["measures"]
        self.assertEqual(recs[0]["entity"], "Revenue")
        self.assertEqual(recs[1]["entity"], "Sales")

    def test_extension_inventory_symlink_escape_refused(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        outside = Path(tempfile.mkdtemp()) / "outside-ext.json"
        outside.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [{"name": "Margin", "expression": "1"}]}]}))
        try:
            ext_path.symlink_to(outside)
        except OSError:
            outside.unlink(missing_ok=True)
            return  # Windows may not grant symlink creation.
        try:
            with self.assertRaises(PbzError) as ctx:
                rename.rename(ws, project, "measure", "Keep", "Margin")
            self.assertEqual(ctx.exception.code, "UNSAFE_PATH")
        finally:
            outside.unlink(missing_ok=True)

    def test_measure_rename_into_extension_measure_on_other_entity_refused(self):
        ws, project, mdef, path = self._report_project({"refs": []})
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tmeasure Net = 1\n")
        ext_path = Path(project.mirror_report, "definition", "reportExtensions.json")
        ext_path.write_text(json.dumps({"name": "extension", "entities": [
            {"name": "Budget", "measures": [{"name": "Total", "expression": "2"}]}]}))
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, "measure", "Net", "Total")
        self.assertEqual(caught.exception.code, "NAME_TAKEN")

    def test_verify_report_parse_failure_refuses_and_pbzerror_passes_through(self):
        ws, project, mdef, _path = self._report_project({})
        model = tmdl.load_model(mdef)
        for error in (pbir.JsonError("bad json"), PbzError("UNSUPPORTED", "sentinel")):
            before = _bytes(ws.mirror)
            with patch("pbz.pbir.parse_json", side_effect=error):
                with self.assertRaises(PbzError) as caught:
                    rename._verify_plan(ws, project, model, mdef, [], "column", "Sales", "Absent", "New")
            self.assertEqual(caught.exception.code, "UNSUPPORTED" if isinstance(error, PbzError) else "REFUSED")
            if isinstance(error, PbzError):
                self.assertIs(caught.exception, error)
            self.assertEqual(_bytes(ws.mirror), before)

    def test_measure_rename_end_to_end_written_qualifier(self):
        ws, project, mdef = self._project()
        path = Path(mdef, 'tables', 'Sales.tmdl')
        path.write_text('table Sales\n\tmeasure M = 1\n'
                        '\tmeasure Use = [M] + Sales[M] + Other[M]\n')
        _write(str(Path(mdef, 'tables', 'Other.tmdl')), 'table Other\n\tcolumn X\n')
        rename.rename(ws, project, 'measure', 'M', 'New')
        self.assertIn('measure New = 1', path.read_text())
        self.assertIn('[New] + Sales[New] + Other[M]', path.read_text())

    def test_golden_offsets_measure_and_empty_preview(self):
        model = tmdl.load_model('', texts={
            'tables/Home.tmdl': 'table Home\n\tmeasure M = 1\n',
            'tables/Other.tmdl': 'table Other\n\tcolumn X\n',
        })
        for marked in ('Other[M] + Home<<[M]>> + <<[M]>>', "'hOmE'<<[M]>>", 'Other[M]'):
            text, expected = '', []
            while marked:
                if marked.startswith('<<'):
                    end = marked.index('>>')
                    val = marked[2:end]
                    expected.append((7 + len(text), 7 + len(text) + len(val), '[New]'))
                    text += val
                    marked = marked[end + 2:]
                else:
                    text += marked[0]
                    marked = marked[1:]
            with self.subTest(text=text):
                edits = rename.dax_ref_edits('prefix!' + text, 7, 7 + len(text), 'measure',
                                             'Home', 'M', 'New', 'Home', res=deps.Resolver(model))
                self.assertEqual(edits, expected)
                if not expected:
                    self.assertEqual(apply_edits(text, edits), text)

    def test_column_rename_rls_and_uncertain_candidate_atomicity(self):
        ws, project, mdef = self._column_project()
        role = Path(mdef, 'roles', 'Reader.tmdl')
        role.write_text(role.read_text() + '\ttablePermission Sales = [Amount] > 0\n')
        rename.rename(ws, project, 'column', 'Amount', 'Value', table='Sales')
        self.assertIn('tablePermission Sales = [Value]', role.read_text())
        sales = Path(mdef, 'tables', 'Sales.tmdl')
        sales.write_text(sales.read_text() + '\tmeasure Uncertain = Missing[Value]\n')
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, 'column', 'Value', 'Next', table='Sales')
        self.assertEqual(caught.exception.code, 'AMBIGUOUS')
        self.assertEqual(_bytes(ws.mirror), before)

    def test_spaced_bare_after_keyword_is_certain_and_home_rename_succeeds(self):
        for expr, name in (('VAR x = 1 RETURN [Total]', 'Total'), ('NOT [Flag]', 'Flag')):
            with self.subTest(expr=expr):
                ws, project, mdef = self._project()
                path = Path(mdef, 'tables', 'Sales.tmdl')
                path.write_text(f'table Sales\n\tmeasure {name} = 1\n\tmeasure Use = {expr}\n')
                bindings = deps.dax_bindings(expr, 'Sales', deps.Resolver(tmdl.load_model(mdef))).bindings
                self.assertEqual([b.ref for b in bindings], [deps.Ref('measure', 'Sales', name)])
                self.assertFalse(bindings[0].uncertain)
                rename.rename(ws, project, 'table', 'Sales', 'Revenue')
                path = Path(mdef, 'tables', 'Revenue.tmdl')
                self.assertIn('measure Use = ' + expr, path.read_text())
                rename.rename(ws, project, 'measure', name, 'New')
                self.assertIn(expr.replace(f'[{name}]', '[New]'), path.read_text())

    def test_bare_after_identifier_refuses_rename(self):
        ws, project, mdef = self._project()
        Path(mdef, 'tables', 'Sales.tmdl').write_text(
            'table Sales\n\tmeasure Total = 1\n\tmeasure Use = VAR x = 1 RETURN[Total]\n')
        bindings = deps.dax_bindings('VAR x = 1 RETURN[Total]', 'Sales',
                                     deps.Resolver(tmdl.load_model(mdef))).bindings
        self.assertEqual([b.ref for b in bindings], [deps.Ref('measure', 'Sales', 'Total')])
        self.assertTrue(bindings[0].uncertain)
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, 'measure', 'Total', 'New')
        self.assertEqual(caught.exception.code, 'AMBIGUOUS')
        self.assertEqual(_bytes(ws.mirror), before)

    def test_iterator_context_precedes_home_then_falls_back(self):
        for other_columns, expected in (('\tcolumn Qty\n', 'Other'), ('\tcolumn X\n', 'Sales')):
            with self.subTest(expected=expected):
                model = tmdl.load_model('', texts={
                    'tables/Sales.tmdl': 'table Sales\n\tcolumn Qty\n',
                    'tables/Measures.tmdl': 'table Measures\n\tmeasure Qty = 1\n',
                    'tables/Other.tmdl': 'table Other\n' + other_columns,
                })
                refs = deps.dax_bindings('SUMX(Other, [Qty])', 'Sales', deps.Resolver(model), row_home=True).refs
                self.assertIn(deps.Ref('column', expected, 'Qty'), refs)
        # Iterator row contexts also precede measures when there is no home row context.
        self.assertIn(deps.Ref('column', 'Other', 'Qty'), deps.dax_bindings(
            'SUMX(Other, [Qty])', 'Sales', deps.Resolver(tmdl.load_model('', texts={
                'tables/Sales.tmdl': 'table Sales\n\tmeasure Qty = 1\n',
                'tables/Other.tmdl': 'table Other\n\tcolumn Qty\n',
            }))).refs)

    def test_m_shared_source_and_literal_types_rename(self):
        for expr, expected in (
            ('#shared[Sales]', '#shared[Revenue]'),
            ('#shared[ Sales ]', '#shared[ Revenue ]'),
            ('Table.TransformColumnTypes(Sales, {{"A", type text}})',
             'Table.TransformColumnTypes(Revenue, {{"A", type text}})'),
            ('#date(2026,1,1) & Sales', '#date(2026,1,1) & Revenue'),
        ):
            with self.subTest(expr=expr):
                ws, project, mdef = self._project()
                path = Path(mdef, 'tables', 'Sales.tmdl')
                path.write_text('table Sales\n\tpartition Sales = m\n\t\tsource = ' + expr + '\n')
                rename.rename(ws, project, 'table', 'Sales', 'Revenue')
                self.assertIn('source = ' + expected, Path(mdef, 'tables', 'Revenue.tmdl').read_text())

    def test_m_shared_selector_trivia_and_quoted_name(self):
        for expr, target, expected in (
            ('#shared[/* note */ Sales]', 'Revenue', '#shared[/* note */ Revenue]'),
            ('#shared[ /* before */ Sales /* after */ ]', 'Revenue',
             '#shared[ /* before */ Revenue /* after */ ]'),
            ('#shared[// note\n Sales // end\n]', 'Revenue', '#shared[// note\n Revenue // end\n]'),
            ('#shared[#"Sales"]', 'New Sales', '#shared[#"New Sales"]'),
        ):
            with self.subTest(expr=expr):
                ws, project, mdef = self._project()
                path = Path(mdef, 'tables', 'Sales.tmdl')
                source = '\n'.join('\t\t\t\t' + line for line in expr.splitlines())
                path.write_text('table Sales\n\tpartition Sales = m\n\t\tsource =\n' + source + '\n')
                res = deps.Resolver(tmdl.load_model(mdef))
                res.expressions.update({name: name for name in res.model.tables})
                bindings = deps.m_bindings(expr, res, base=7)
                self.assertEqual([b.ref for b in bindings], [deps.Ref('m_query', '', 'Sales', 'm')])
                self.assertFalse(bindings[0].uncertain)
                token = '#"Sales"' if '#"Sales"' in expr else 'Sales'
                start = 7 + expr.index(token)
                self.assertEqual((bindings[0].start, bindings[0].end), (start, start + len(token)))
                before = path.read_bytes()
                rename.rename(ws, project, 'table', 'Sales', target)
                renamed = Path(mdef, 'tables', target + '.tmdl').read_bytes()
                # All non-name bytes, including comments, survive the real rename.
                decl_name = rename.lexers.tmdl_name(target).encode()
                expected_bytes = before.replace(b'table Sales', b'table ' + decl_name).replace(
                    b'partition Sales', b'partition ' + decl_name).replace(
                    b'#"Sales"' if token.startswith('#') else b'Sales',
                    rename._m_ident(target).encode())
                self.assertEqual(renamed, expected_bytes)
                self.assertIn(expected.splitlines()[0], renamed.decode())

        ws, project, mdef = self._project()
        path = Path(mdef, 'tables', 'Sales.tmdl')
        expr = '#shared[Sales Total]'
        path.write_text("table 'Sales Total'\n\tpartition 'Sales Total' = m\n\t\tsource = " + expr + '\n')
        model_path = Path(mdef, 'model.tmdl')
        model_path.write_text(model_path.read_text().replace('ref table Sales', "ref table 'Sales Total'"))
        res = deps.Resolver(tmdl.load_model(mdef))
        res.expressions.update({name: name for name in res.model.tables})
        bindings = deps.m_bindings(expr, res, base=7)
        self.assertEqual([b.ref for b in bindings], [deps.Ref('m_query', '', 'Sales Total', 'm')])
        self.assertFalse(bindings[0].uncertain)
        start = 7 + expr.index('Sales Total')
        self.assertEqual((bindings[0].start, bindings[0].end), (start, start + len('Sales Total')))
        rename.rename(ws, project, 'table', 'Sales Total', 'Revenue')
        self.assertIn('source = #shared[Revenue]', path.read_text())

    def test_m_shared_selector_only_dependency_is_used(self):
        from test_unused_atomic import UnusedAtomicTests, SALES

        for selector in ('#shared[/* note */ Sales]', '#shared[#"Sales"]'):
            with self.subTest(selector=selector):
                ws, project = UnusedAtomicTests._project(self)
                mdef = Path(project.mirror_model, 'definition')
                (mdef / 'expressions.tmdl').write_text(
                    'expression Sales = 1\n\nexpression GenuinelyUnused = 1\n')
                (mdef / 'tables' / 'Sales.tmdl').write_text(
                    SALES + '\tpartition Sales = m\n\t\tsource = ' + selector + '\n')
                graph = usage.build(ws, project)
                used = usage.key('m_query', '', 'Sales')
                dead = usage.key('m_query', '', 'GenuinelyUnused')
                self.assertIn(used, [e.dst for e in graph.out[usage.key('table', '', 'Sales')]])
                self.assertEqual(graph.state[used], 'used')
                self.assertEqual(graph.state[dead], 'unused')
                self.assertEqual(unused.remove(ws, project, only=['m_query'])['result'], 'REMOVED')
                self.assertEqual(set(tmdl.load_model(str(mdef)).expressions), {'Sales'})

        ws, project = UnusedAtomicTests._project(self)
        mdef = Path(project.mirror_model, 'definition')
        (mdef / 'expressions.tmdl').write_text(
            "expression 'Sales Total' = 1\n\nexpression GenuinelyUnused = 1\n")
        (mdef / 'tables' / 'Sales.tmdl').write_text(
            SALES + '\tpartition Sales = m\n\t\tsource = #shared[Sales Total]\n')
        graph = usage.build(ws, project)
        used = usage.key('m_query', '', 'Sales Total')
        dead = usage.key('m_query', '', 'GenuinelyUnused')
        self.assertIn(used, [e.dst for e in graph.out.get(usage.key('table', '', 'Sales'), [])])
        self.assertEqual(graph.state[used], 'used')
        self.assertEqual(graph.state[dead], 'unused')
        self.assertEqual(unused.remove(ws, project, only=['m_query'])['result'], 'REMOVED')
        self.assertEqual(set(tmdl.load_model(str(mdef)).expressions), {'Sales Total'})

    def test_m_shared_selector_non_ident_is_uncertain(self):
        model = tmdl.Model()
        model.expressions = dict.fromkeys(('Sales + Total', 'Other'))
        bindings = deps.m_bindings('{#shared[Sales + Total], Other}', deps.Resolver(model))
        self.assertTrue(all(b.uncertain for b in bindings))
        self.assertEqual({b.ref.name for b in bindings}, {'Sales + Total', 'Other'})

    def test_m_shared_selector_fallback_merges_decoded_binding(self):
        from test_unused_atomic import UnusedAtomicTests, SALES

        expr = 'try #shared[/* note */ Sales] otherwise 0'
        ws, project = UnusedAtomicTests._project(self)
        mdef = Path(project.mirror_model, 'definition')
        (mdef / 'tables' / 'Sales.tmdl').write_text(
            SALES + '\tpartition Sales = m\n\t\tsource = ' + expr + '\n')
        res = deps.Resolver(tmdl.load_model(str(mdef)))
        res.expressions.update({name: name for name in res.model.tables})
        bindings = deps.m_bindings(expr, res, base=7)
        self.assertEqual([b.ref for b in bindings], [deps.Ref('m_query', '', 'Sales', 'm')])
        self.assertTrue(bindings[0].uncertain)
        start = 7 + expr.index('Sales')
        self.assertEqual((bindings[0].start, bindings[0].end), (start, start + len('Sales')))
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, 'table', 'Sales', 'Revenue')
        self.assertEqual(caught.exception.code, 'AMBIGUOUS')
        self.assertEqual(_bytes(ws.mirror), before)
        (mdef / 'expressions.tmdl').write_text(
            'expression Sales = 1\n\nexpression GenuinelyUnused = 1\n')
        graph = usage.build(ws, project)
        used = usage.key('m_query', '', 'Sales')
        dead = usage.key('m_query', '', 'GenuinelyUnused')
        self.assertIn(used, [e.dst for e in graph.out[usage.key('table', '', 'Sales')]])
        self.assertEqual(graph.state[dead], 'unused')
        self.assertEqual(unused.remove(ws, project, only=['m_query'])['result'], 'REMOVED')
        self.assertEqual(set(tmdl.load_model(str(mdef)).expressions), {'Sales'})

    def test_m_type_scan_stops_before_expression_operators(self):
        expressions = ['(Value.Type(1) = type number and Sales)',
                       '(Value.Type(1) = type number = Sales)']
        for expr in expressions:
            with self.subTest(expr=expr):
                ws, project, mdef = self._project()
                path = Path(mdef, 'tables', 'Sales.tmdl')
                path.write_text('table Sales\n\tpartition Sales = m\n\t\tsource = ' + expr + '\n')
                model = tmdl.Model()
                model.expressions = {'Sales': None}
                bindings = deps.m_bindings(expr, deps.Resolver(model))
                self.assertEqual([b.ref.name for b in bindings], ['Sales'])
                self.assertFalse(bindings[0].uncertain)
                rename.rename(ws, project, 'table', 'Sales', 'Revenue')
                self.assertIn(expr.replace('Sales', 'Revenue'),
                              Path(mdef, 'tables', 'Revenue.tmdl').read_text())

    def test_m_capture_and_type_query_refuse_without_writes(self):
        for expr in ('let Revenue = 1 in Sales',
                     'let r = [Revenue = 1] in Sales',
                     'let r = [#"Revenue" = 1] in Sales',
                     'let f = (Revenue) => Revenue in Sales',
                     'let f = (x as type Sales) => x in Sales',
                     'let f = (x as Sales) => x in Sales',
                     'type Sales'):
            with self.subTest(expr=expr):
                ws, project, mdef = self._project()
                Path(mdef, 'tables', 'Sales.tmdl').write_text(
                    'table Sales\n\tpartition Sales = m\n\t\tsource = ' + expr + '\n')
                before = _bytes(ws.mirror)
                with self.assertRaises(PbzError) as caught:
                    rename.rename(ws, project, 'table', 'Sales', 'Revenue')
                self.assertEqual(caught.exception.code, 'AMBIGUOUS')
                self.assertEqual(_bytes(ws.mirror), before)

    def test_m_tracker_brackets_uncertainty_and_defined_names(self):
        model = tmdl.Model()
        model.expressions = dict.fromkeys(('Sales', 'Other', 'Revenue', '_'))
        res = deps.Resolver(model)
        self.assertEqual([b.ref.name for b in deps.m_bindings('Other[Sales]', res)], ['Other'])
        shared = deps.m_bindings('#shared[Sales]', res, base=4)
        self.assertEqual([(b.start, b.end, b.uncertain) for b in shared], [(12, 17, False)])
        uncertain = deps.m_bindings('try Other[Sales] otherwise 1', res)
        self.assertEqual([b.ref.name for b in uncertain], ['Other', 'Sales'])
        self.assertEqual([('try Other[Sales] otherwise 1')[b.start:b.end] for b in uncertain], ['Other', 'Sales'])
        self.assertTrue(all(b.uncertain for b in uncertain))
        self.assertEqual(deps.m_bindings('try Sales otherwise #shared[Sales]', res, own='Sales'), [])
        self.assertEqual(deps.m_defined_names('let r = [Revenue = 1], f = (Sales) => Sales in each _'),
                         {'r', 'Revenue', 'f', 'Sales', '_'})
        for expr in ('{type {text}, Sales}', '{type table [A = text], Sales}',
                     '{type function (x as text) as text, Sales}', '#time(1,2,3) & Sales',
                     '#datetime(2026,1,1,0,0,0) & Sales', '#datetimezone(2026,1,1,0,0,0,0,0) & Sales',
                     '#duration(1,0,0,0) & Sales', '#table({}, {}) & Sales', '#binary({}) & Sales'):
            bindings = deps.m_bindings(expr, res)
            self.assertEqual([b.ref.name for b in bindings], ['Sales'], expr)
            self.assertFalse(any(b.uncertain for b in bindings), expr)
        for expr in ('type Sales', 'type [A = Sales]', 'type {Sales}', 'type table [A = Sales]',
                     'try Sales otherwise 1', '#unknown() & Sales', 'Sales meta []', '@Sales', 'shared Sales'):
            bindings = deps.m_bindings(expr, res)
            self.assertIn('Sales', [b.ref.name for b in bindings], expr)
            self.assertTrue(all(b.uncertain for b in bindings), expr)

    def test_table_dax_qualifiers_and_unbindable_refusal(self):
        ws, project, mdef = self._column_project()
        rename.rename(ws, project, 'column', 'Amount', 'Value', table='Sales')
        path = Path(mdef, 'tables', 'Sales.tmdl')
        self.assertIn('SUM(Sales[Value])', path.read_text())
        rename.rename(ws, project, 'table', 'Sales', 'Revenue')
        path = Path(mdef, 'tables', 'Revenue.tmdl')
        self.assertIn('SUM(Revenue[Value])', path.read_text())
        path.write_text(path.read_text() + '\tmeasure Bad = Revenue[Missing]\n')
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            rename.rename(ws, project, 'table', 'Revenue', 'Next')
        self.assertEqual(caught.exception.code, 'AMBIGUOUS')
        self.assertEqual(_bytes(ws.mirror), before)

    def test_consolidate_cross_table_qualifiers_use_original_text(self):
        ws, project, mdef = self._project()
        path = Path(mdef, 'tables', 'Sales.tmdl')
        path.write_text('table Sales\n\tmeasure Dup = 1\n'
                        '\tmeasure Use = Sales[Dup] + Sales[Target] + [Dup]\n')
        _write(str(Path(mdef, 'tables', 'Other.tmdl')), 'table Other\n\tmeasure Target = 1\n')
        consolidate.merge(ws, project, 'Dup', 'Target')
        self.assertIn("'Other'[Target] + Sales[Target] + [Target]", path.read_text())

    def test_consolidate_bare_nonunique_home_column(self):
        ws, project, mdef = self._project()
        Path(mdef, 'tables', 'Sales.tmdl').write_text(
            'table Sales\n\tcolumn Qty\n\tmeasure Dup = [Qty]\n\tmeasure Target = [Qty]\n'
            '\tmeasure Use = [Dup] + [Qty]\n')
        _write(str(Path(mdef, 'tables', 'Other.tmdl')), 'table Other\n\tcolumn Qty\n')
        bound = []
        original_bind = deps.dax_bindings

        def capture(*args, **kwargs):
            result = original_bind(*args, **kwargs)
            if '[Qty]' in args[0]:
                bound.extend(b for b in result.bindings if b.ref.name == 'Qty')
            return result

        with patch('pbz.deps.dax_bindings', side_effect=capture):
            consolidate.merge(ws, project, 'Dup', 'Target')
        self.assertTrue(bound)
        self.assertTrue(all(b.ref == deps.Ref('column', 'Sales', 'Qty') and not b.uncertain for b in bound))
        text = Path(mdef, 'tables', 'Sales.tmdl').read_text()
        self.assertNotIn('measure Dup', text)
        self.assertIn('measure Use = [Target] + [Qty]', text)
        from types import SimpleNamespace
        bound.clear()
        with patch('pbz.deps.dax_bindings', side_effect=capture):
            retargeted = consolidate._retarget_dax('[Dup] + [Qty]',
                         SimpleNamespace(table='Sales', name='Dup'), SimpleNamespace(table='Sales', name='Target'),
                         deps.Resolver(tmdl.load_model('', texts={
                             'tables/Sales.tmdl': 'table Sales\n\tcolumn Qty\n\tmeasure Dup = 1\n',
                             'tables/Other.tmdl': 'table Other\n\tcolumn Qty\n',
                         })))
        self.assertEqual(retargeted, ('[Target] + [Qty]', 1))
        self.assertEqual([b.ref for b in bound], [deps.Ref('column', 'Sales', 'Qty')])


if __name__ == '__main__':
    unittest.main()
