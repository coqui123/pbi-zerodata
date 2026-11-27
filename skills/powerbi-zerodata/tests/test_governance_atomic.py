"""Offline checks that governance add/remove and culture writers commit together.

Run: python tests/test_governance_atomic.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import common, governance  # noqa: E402
from pbz import modeling as md  # noqa: E402
from pbz import tmdl  # noqa: E402
from pbz.common import PbzError  # noqa: E402

MODEL = "model Model\n\tculture: en-US\n\nref table Sales\n"
SALES = "table Sales\n\n\tmeasure Amount = 1\n\n"
DATABASE = "database\n\tcompatibilityLevel: 1400\n"
CULTURE = (
    "cultureInfo en-US\n\n"
    "\ttranslations\n\n"
    "\t\tmodel Model\n\n"
    "\t\t\ttable Src\n\n"
    "\t\t\t\tmeasure Amount\n"
    "\t\t\t\t\tcaption: One\n"
)


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


class GovernanceAtomicTests(unittest.TestCase):
    def _project(self, *, model=True):
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
        _write(os.path.join(mdef, "database.tmdl"), DATABASE)
        if model:
            _write(os.path.join(mdef, "model.tmdl"), MODEL)
        return ws, project, mdef

    def test_second_write_rolls_back(self):
        ws, project, mdef = self._project()
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
                governance.culture_add(ws, project, "fr-FR")
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertFalse(os.path.exists(os.path.join(mdef, "cultures", "fr-FR.tmdl")))
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])

    def test_missing_model_refuses_before_write(self):
        ws, project, mdef = self._project(model=False)
        before = _bytes(ws.mirror)
        with patch("pbz.common.write_text", side_effect=AssertionError("wrote")):
            with self.assertRaises(PbzError) as caught:
                governance.culture_add(ws, project, "fr-FR")
        self.assertEqual(caught.exception.code, "NO_MIRROR")
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertFalse(os.path.isdir(os.path.join(mdef, "cultures")))

    def test_filename_collision_refuses(self):
        ws, project, mdef = self._project()
        _write(os.path.join(mdef, "perspectives", "A_B.tmdl"), "perspective A_B\n")
        text = common.read_text(os.path.join(mdef, "model.tmdl"))[0]
        common.write_text(os.path.join(mdef, "model.tmdl"), text + "ref perspective A_B\n")
        before = _bytes(ws.mirror)
        with patch("pbz.common.commit_bytes", side_effect=AssertionError("committed")):
            with self.assertRaises(PbzError) as caught:
                governance.perspective_add(ws, project, "A/B")
        self.assertEqual(caught.exception.code, "NAME_TAKEN")
        self.assertNotIn("A/B", caught.exception.detail)
        self.assertEqual(_bytes(ws.mirror), before)

    def test_remove_reports_files(self):
        ws, project, _mdef = self._project()
        governance.culture_add(ws, project, "fr-FR")
        out = governance.culture_remove(ws, project, "fr-FR")
        self.assertIn("files", out)
        self.assertTrue(any(name.endswith("fr-FR.tmdl") for name in out["files"]))

    def test_culture_multi_file_rolls_back(self):
        _ws, _project, mdef = self._project()
        _write(os.path.join(mdef, "cultures", "en-US.tmdl"), CULTURE)
        _write(os.path.join(mdef, "cultures", "fr-FR.tmdl"), CULTURE.replace("en-US", "fr-FR").replace("One", "Un"))
        before = _bytes(mdef)
        real = common.write_text
        calls = {"n": 0}

        def boom(path, text, bom=b"", *, root=None, **_kw):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("injected")
            return real(path, text, bom, root=root)

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                governance.culture_edits_move(mdef, "Src", "Dst", "Amount")
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(_bytes(mdef), before)
        self.assertEqual(list(Path(mdef).rglob("*.pbz-tmp")), [])


def _role_state(path):
    script = r"""
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from pbz import tmdl

path = Path(sys.argv[2])
text = path.read_bytes().decode("utf-8")
doc = tmdl.parse(str(path), text)
state = {key: {} for key in (
    "perms", "filters", "parents", "annotations", "described", "fenced",
    "counts", "child_counts",
)}
for node in doc.nodes:
    if node.kind != "tablePermission":
        continue
    name = node.name
    state["counts"][name] = state["counts"].get(name, 0) + 1
    if "metadataPermission" in node.props:
        state["perms"][name + "|"] = node.props.get("metadataPermission")
    for child in node.children:
        if child.kind == "columnPermission":
            key = name + "|" + child.name
            state["perms"][key] = child.props.get("metadataPermission")
            state["child_counts"][key] = state["child_counts"].get(key, 0) + 1
    state["filters"][name] = (
        " ".join(text[node.expr.start:node.expr.end].split()) if node.expr else None
    )
    state["parents"][name] = (
        node.parent.name if node.parent and node.parent.kind == "role" else None
    )
    state["annotations"][name] = [
        child.name + "=" + " ".join(
            text[region.start:region.end].split()
        )
        for child in node.children if child.kind == "annotation"
        for region in doc.regions_for(child) if region.prop == "value"
    ]
    state["described"][name] = any(
        region.prop == "description" for region in doc.regions_for(node)
    )
    declaration = doc.lines[node.line][1]
    state["fenced"][name] = declaration.partition("=")[2].lstrip().startswith("```")
print(json.dumps(state))
"""
    return json.loads(subprocess.check_output([sys.executable, "-c", script, SKILL, str(path)]))


class RoleFilterPermissionTests(unittest.TestCase):
    ROLE = "role Limited\n"
    BARE = "\ttablePermission Customer"
    RETAIL = '[Segment] = "Retail"'
    WHOLESALE = '[Segment] = "Wholesale"'
    FILTER = BARE + " = " + RETAIL + "\n"
    TABLE = "\t\tmetadataPermission: none\n"
    COLUMN = "\t\tcolumnPermission Name\n\t\t\tmetadataPermission: none\n"
    DEEP_COLUMN = "\t\t\tcolumnPermission Name\n\t\t\t\tmetadataPermission: none\n"
    COMBINED = FILTER + TABLE + COLUMN
    NO_FILTER = BARE + "\n" + TABLE + COLUMN
    TABLE_PERMS = {"Customer|": "none"}
    COLUMN_PERMS = {"Customer|Name": "none"}
    COMBINED_PERMS = TABLE_PERMS | COLUMN_PERMS
    FENCED = BARE + " = ``` \n\t\t\t" + RETAIL + "\n\t\t```\n"
    FENCE_BODY = "```\nTRUE()\n```"
    NOTE = "\t/// Customer note.\n"
    QUOTED = "\ttablePermission 'Customer'"
    QUOTED_BLOCK = QUOTED + "  = " + RETAIL + "\n" + TABLE
    CONTINUATION = '[Name] <> "East"'
    MULTILINE = FILTER.rstrip("\n") + " &&\n\t\t\t" + CONTINUATION + "\n" + TABLE

    def _project(self, role_text=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        ws = common.Workspace(str(root))
        ws.private = str(root / "private")
        os.makedirs(ws.private)
        _write(root / "Mini.pbip", "{}")
        project = common.Project(ws, "Mini")
        mdef = Path(project.mirror_model) / "definition"
        segment = "\tcolumn Segment\n\t\tdataType: string\n"
        _write(mdef / "database.tmdl", DATABASE)
        _write(mdef / "model.tmdl", "model Model\n\nref table Customer\nref table Sales\n")
        _write(
            mdef / "tables" / "Customer.tmdl",
            "table Customer\n\n" + segment + "\n\tcolumn Name\n\t\tdataType: string\n",
        )
        _write(mdef / "tables" / "Sales.tmdl", "table Sales\n\n" + segment)
        governance.role_add(ws, project, "Limited")
        path = mdef / "roles" / "Limited.tmdl"
        if role_text is not None:
            _write(path, role_text)
        return ws, project, path

    def _filter(self, fixture, dax, table="Customer"):
        ws, project, _path = fixture
        governance.role_filter(ws, project, "Limited", table, dax)

    def _assert_state(self, path, perms, dax):
        state = _role_state(path)
        self.assertEqual(state["perms"], perms)
        self.assertEqual(state["filters"], {"Customer": dax})
        self.assertEqual(state["parents"], {"Customer": "Limited"})
        self.assertEqual(state["counts"], {"Customer": 1})
        for count in state["child_counts"].values():
            self.assertEqual(count, 1)
        return state

    def _assert_bare(self, path, declaration=None):
        self.assertIn((declaration or self.BARE).encode(), path.read_bytes().splitlines())

    def _parse(self, path):
        return tmdl.parse(str(path), path.read_bytes().decode("utf-8"))

    def _permission(self, doc, table="Customer"):
        node, = (node for node in doc.nodes if node.kind == "tablePermission" and node.name == table)
        self.assertEqual(node.parent.kind, "role")
        self.assertEqual(node.parent.name, "Limited")
        return node

    def _refused(self, fixture, dax, table="Customer", code="REFUSED"):
        path = fixture[2]
        before = _bytes(str(path.parent))
        with patch("pbz.governance._write") as writer, self.assertRaises(PbzError) as caught:
            self._filter(fixture, dax, table)
        self.assertEqual(caught.exception.code, code)
        writer.assert_not_called()
        self.assertEqual(_bytes(str(path.parent)), before)

    def _refuse_block(self, block, *values, table="Customer", proof=None):
        for dax in values:
            with self.subTest(dax=dax):
                fixture = self._project(self.ROLE + block)
                if proof is not None:
                    proof(self._parse(fixture[2]))
                self._refused(fixture, dax, table)

    def test_01_table_restriction_update(self):
        fixture = self._project(self.ROLE + self.COMBINED)
        self._filter(fixture, self.WHOLESALE)
        self._assert_state(fixture[2], self.COMBINED_PERMS, self.WHOLESALE)

    def test_02_table_restriction_removal(self):
        fixture = self._project(self.ROLE + self.COMBINED)
        self._filter(fixture, None)
        self._assert_state(fixture[2], self.COMBINED_PERMS, None)
        self._assert_bare(fixture[2])

    def test_03_column_restriction_update(self):
        fixture = self._project(self.ROLE + self.FILTER + self.COLUMN)
        self._filter(fixture, self.WHOLESALE)
        self._assert_state(fixture[2], self.COLUMN_PERMS, self.WHOLESALE)

    def test_04_column_restriction_removal(self):
        fixture = self._project(self.ROLE + self.FILTER + self.COLUMN)
        self._filter(fixture, None)
        self._assert_state(fixture[2], self.COLUMN_PERMS, None)
        self._assert_bare(fixture[2])

    def test_05_removal_without_filter_not_found(self):
        self._refused(self._project(self.ROLE + self.NO_FILTER), None, code="NOT_FOUND")

    def test_05b_bare_block_removal_not_found(self):
        self._refused(self._project(self.ROLE + self.BARE + "\n"), None, code="NOT_FOUND")

    def test_06_annotation_survives(self):
        block = self.FILTER + self.TABLE + "\t\tannotation Marker = keep me\n"
        for dax in (self.WHOLESALE, None):
            with self.subTest(dax=dax):
                fixture = self._project(self.ROLE + block)
                node = self._permission(self._parse(fixture[2]))
                self.assertEqual([(c.kind, c.name) for c in node.children], [("annotation", "Marker")])
                self._filter(fixture, dax)
                state = self._assert_state(fixture[2], self.TABLE_PERMS, dax)
                self.assertEqual(state["annotations"], {"Customer": ["Marker=keep me"]})

    def test_07_first_set_preserves_permissions(self):
        fixture = self._project()
        ws, project, path = fixture
        md.role_ols(ws, project, "Limited", "Customer", "none")
        md.role_ols(ws, project, "Limited", "'Customer'[Name]", "none")
        node = self._permission(self._parse(path))
        self.assertIsNone(node.expr)
        self.assertEqual(node.props, {"metadataPermission": "none"})
        column, = node.children
        self.assertEqual((column.kind, column.name), ("columnPermission", "Name"))
        self.assertEqual(column.props, {"metadataPermission": "none"})
        self._filter(fixture, self.RETAIL)
        self._assert_state(path, self.COMBINED_PERMS, self.RETAIL)
        self.assertIn(self.FILTER.rstrip("\n").encode(), path.read_bytes().splitlines())

    def test_08_filter_only_matches_base(self):
        for dax, expected in (
            (self.WHOLESALE, self.ROLE + self.BARE + " = " + self.WHOLESALE + "\n\n"),
            (None, self.ROLE),
        ):
            with self.subTest(dax=dax):
                fixture = self._project(self.ROLE + self.FILTER)
                self._filter(fixture, dax)
                self.assertEqual(fixture[2].read_bytes(), expected.encode())
                if dax is None:
                    self.assertEqual(_role_state(fixture[2])["filters"], {})
                else:
                    self._assert_state(fixture[2], {}, dax)

    def test_09a_filter_expression_property_refused(self):
        block = self.BARE + "\n\t\tfilterExpression = " + self.RETAIL + "\n" + self.TABLE
        self._refuse_block(block, self.WHOLESALE, None)

    def test_09b_stray_expression_child_refused(self):
        block = self.BARE + "\n" + self.TABLE + "\t\texpression = " + self.RETAIL + "\n"
        self._refuse_block(block, self.WHOLESALE)

    def test_09c_whitespace_only_rest_refused(self):
        self._refuse_block(self.BARE + " =  \n" + self.TABLE, self.WHOLESALE)

    def test_09d_fence_leading_body_update_refused(self):
        self._refuse_block(self.COMBINED, self.FENCE_BODY)

    def test_09e_fence_leading_body_first_set_refused(self):
        self._refuse_block(self.NO_FILTER, self.FENCE_BODY)

    def _described_removal(self, properties, perms):
        fixture = self._project(self.ROLE + self.NOTE + self.FILTER + properties)
        self._filter(fixture, None)
        state = self._assert_state(fixture[2], perms, None)
        self.assertEqual(state["described"], {"Customer": True})
        self._assert_bare(fixture[2])
        self.assertIn(self.NOTE.encode(), fixture[2].read_bytes())

    def test_10_described_parent_removal(self):
        self._described_removal(self.TABLE, self.TABLE_PERMS)

    def test_10b_description_only_removal(self):
        self._described_removal("", {})

    def test_11a_fenced_with_permissions(self):
        for dax in (self.WHOLESALE, None):
            with self.subTest(dax=dax):
                fixture = self._project(self.ROLE + self.FENCED + self.TABLE)
                self._filter(fixture, dax)
                state = self._assert_state(fixture[2], self.TABLE_PERMS, dax)
                self.assertEqual(state["fenced"], {"Customer": False})
                self.assertNotIn(b"```", fixture[2].read_bytes())
                self.assertIn(self.TABLE.rstrip("\n").encode(), fixture[2].read_bytes().splitlines())
                if dax is None:
                    self._assert_bare(fixture[2])

    def test_11b_fenced_filter_only_removal(self):
        fixture = self._project(self.ROLE + self.FENCED)
        self._filter(fixture, None)
        self.assertEqual(_role_state(fixture[2])["filters"], {})
        self.assertNotIn(b"```", fixture[2].read_bytes())
        self.assertEqual(fixture[2].read_bytes(), self.ROLE.encode())

    def _prove_table_perm(self, doc):
        node = self._permission(doc)
        self.assertEqual(node.props, {"metadataPermission": "none"})
        self.assertEqual(node.children, [])

    def _prove_column_perm(self, doc):
        node = self._permission(doc)
        self.assertEqual(node.props, {})
        child, = node.children
        self.assertEqual((child.kind, child.name), ("columnPermission", "Name"))
        self.assertEqual(child.props, {"metadataPermission": "none"})

    def test_12a_deep_property_first_set_refused(self):
        self._refuse_block(
            self.BARE + "\n\t" + self.TABLE, self.WHOLESALE, proof=self._prove_table_perm,
        )

    def test_12b_deep_property_after_fence_refused(self):
        self._refuse_block(self.FENCED + "\t" + self.TABLE, self.WHOLESALE, proof=self._prove_table_perm)

    def test_12c_deep_child_first_set_refused(self):
        self._refuse_block(
            self.BARE + "\n" + self.DEEP_COLUMN, self.WHOLESALE, proof=self._prove_column_perm,
        )

    def test_12d_deep_child_after_fence_refused(self):
        self._refuse_block(self.FENCED + self.DEEP_COLUMN, self.WHOLESALE, proof=self._prove_column_perm)

    def _sibling_description_refused(self, dax):
        block = self.FILTER + "\t\t/// Sibling note.\n\ttablePermission Sales = " + self.RETAIL + "\n"

        def prove(doc):
            customer = self._permission(doc)
            sales = self._permission(doc, "Sales")
            self.assertIsNotNone(customer.expr)
            self.assertTrue(any(r.prop == "description" for r in doc.regions_for(sales)))

        self._refuse_block(block, dax, proof=prove)

    def test_13a_sibling_description_removal_refused(self):
        self._sibling_description_refused(None)

    def test_13b_sibling_description_update_refused(self):
        self._sibling_description_refused(self.WHOLESALE)

    def test_14a_two_tab_table_permission_update_refused(self):
        block = "\t" + self.FILTER + "\t" + self.TABLE
        self._refuse_block(block, self.WHOLESALE, proof=self._prove_table_perm)

    def test_14b_two_tab_table_permission_removal_refused(self):
        block = "\t" + self.FILTER + self.DEEP_COLUMN
        self._refuse_block(block, None, proof=self._prove_column_perm)

    def _quoted_removal(self, fixture):
        self._filter(fixture, None)
        self._assert_bare(fixture[2], self.QUOTED)
        self._assert_state(fixture[2], self.TABLE_PERMS, None)

    def test_15_prefix_preservation_round_trip(self):
        fixture = self._project(self.ROLE + self.QUOTED_BLOCK)
        self._filter(fixture, self.WHOLESALE)
        declaration, = (line for line in fixture[2].read_bytes().splitlines() if b"tablePermission" in line)
        self.assertEqual(declaration.partition(b"=")[0], (self.QUOTED + "  ").encode())
        self._assert_state(fixture[2], self.TABLE_PERMS, self.WHOLESALE)
        self._quoted_removal(fixture)

    def test_15b_prefix_preservation_removal_only(self):
        self._quoted_removal(self._project(self.ROLE + self.QUOTED_BLOCK))

    def test_16a_empty_rest_multiline_update(self):
        block = self.BARE + " =\n\n\t\t\t" + self.RETAIL + "\n" + self.TABLE
        fixture = self._project(self.ROLE + block)
        self._filter(fixture, self.WHOLESALE)
        self._assert_state(fixture[2], self.TABLE_PERMS, self.WHOLESALE)

    def _multiline_removal(self, fixture):
        self._filter(fixture, None)
        self._assert_state(fixture[2], self.TABLE_PERMS, None)
        self.assertNotIn(self.CONTINUATION.encode(), fixture[2].read_bytes())
        self.assertEqual(fixture[2].read_bytes(), (self.ROLE + self.BARE + "\n" + self.TABLE).encode())

    def test_16b_inline_multiline_round_trip(self):
        fixture = self._project(self.ROLE + self.MULTILINE)
        dax = self.WHOLESALE + " &&\n" + self.CONTINUATION
        self._filter(fixture, dax)
        self._assert_state(fixture[2], self.TABLE_PERMS, " ".join(dax.split()))
        self._multiline_removal(fixture)

    def test_16c_deep_note_inside_region_update(self):
        fixture = self._project(self.ROLE + self.FILTER + "\t\t\t/// deep note\n" + self.TABLE)
        self._filter(fixture, self.WHOLESALE)
        self._assert_state(fixture[2], self.TABLE_PERMS, self.WHOLESALE)

    def test_16d_multiline_removal_only(self):
        self._multiline_removal(self._project(self.ROLE + self.MULTILINE))

    def test_17a_valueless_filter_expression_refused(self):
        self._refuse_block(self.BARE + "\n\t\tfilterExpression =\n", self.WHOLESALE, None)

    def test_17b_unowned_trailing_note_refused(self):
        self._refuse_block(self.FILTER + "\t\t/// trailing note\n", self.WHOLESALE, None)

    def test_17c_unowned_note_inside_child_span_refused(self):
        self._refuse_block(self.COMBINED + "\t\t\t/// trailing note\n", self.WHOLESALE, None)

    def test_17d_ignored_line_inside_child_span_refused(self):
        self._refuse_block(self.COMBINED + "\t\t\t??? ignored text\n", self.WHOLESALE, None)

    def test_18_duplicate_targets_refused(self):
        self._refuse_block(self.FILTER + self.FILTER.replace("Customer", "CUSTOMER"), self.WHOLESALE, None)

    def test_19_unclosed_fence_append_refused(self):
        block = self.FENCED.removesuffix("\t\t```\n").replace("Customer", "Sales")
        fixture = self._project(self.ROLE + block + self.FILTER)
        path = fixture[2]
        self.assertIn(self.FILTER.encode(), path.read_bytes())
        doc = self._parse(path)
        self.assertTrue(doc.unclosed_fence)
        sales = self._permission(doc, "Sales")
        self.assertIn(self.FILTER.rstrip("\n"), self._expression_text(doc, sales))
        self.assertFalse(any(
            node.kind == "tablePermission" and node.name == "Customer"
            for node in doc.nodes
        ))
        self._refused(fixture, self.RETAIL)

    def test_19b_fence_leading_append_refused(self):
        self._refuse_block("", self.FENCE_BODY)

    def test_19c_unclosed_fence_on_target_refused(self):
        block = self.FENCED.removesuffix("\t\t```\n")
        fixture = self._project(self.ROLE + block)
        path = fixture[2]
        self.assertIn((self.BARE + " = ``` ").encode(), path.read_bytes().splitlines())
        doc = self._parse(path)
        self.assertTrue(doc.unclosed_fence)
        self.assertTrue(any(
            node.kind == "tablePermission" and node.name == "Customer" and node.expr is not None
            for node in doc.nodes
        ))
        self._refused(fixture, self.RETAIL)

    def test_20a_trailing_note_append_refused(self):
        self._refuse_block(self.FILTER + "\n/// trailing note\n", self.RETAIL, table="Sales")

    def test_20b_second_role_append_refused(self):
        block = "\tmodelPermission: read\n\nrole Other\n\tmodelPermission: read\n"
        self._refuse_block(block, self.RETAIL)

    def test_20c_in_block_note_append_refused(self):
        self._refuse_block(self.FILTER + "\t\t/// trailing note\n", self.RETAIL, table="Sales")

    def test_20d_note_before_property_append_refused(self):
        self._refuse_block("\t/// dangling note\n\tmodelPermission: read\n", self.RETAIL)

    def test_20e_indented_role_append_refused(self):
        fixture = self._project("\trole Limited\n\tmodelPermission: read\n")
        self._refused(fixture, self.RETAIL)

    def test_20f_append_control(self):
        for member in ("", "\tmember some-token\n"):
            with self.subTest(member=member):
                authored = self.ROLE + member
                fixture = self._project(authored)
                self._filter(fixture, self.RETAIL)
                self._assert_state(fixture[2], {}, self.RETAIL)
                expected = authored.rstrip("\r\n") + "\n\n" + self.FILTER
                self.assertEqual(fixture[2].read_bytes(), expected.encode())

    def test_20g_note_plus_member_append_control(self):
        authored = self.ROLE + "/// a note\n\tmember some-token\n"
        fixture = self._project(authored)
        doc = self._parse(fixture[2])
        member, = (node for node in doc.nodes if node.kind == "member")
        self.assertTrue(any(r.prop == "description" for r in doc.regions_for(member)))
        self._filter(fixture, self.RETAIL)
        self._assert_state(fixture[2], {}, self.RETAIL)
        self.assertTrue(fixture[2].read_bytes().startswith(authored.encode()))

    def test_20h_zero_tab_property_append_refused(self):
        self._refuse_block("modelPermission: read\n", self.RETAIL)

    def test_20i_zero_tab_flag_append_refused(self):
        self._refuse_block("isHidden\n", self.RETAIL)

    def test_21_bare_flag_preserved(self):
        flag = "\t\tisHidden\n"
        fixture = self._project(self.ROLE + self.FILTER + flag)
        self._filter(fixture, self.WHOLESALE)
        self._assert_state(fixture[2], {}, self.WHOLESALE)
        self.assertIn(flag.encode(), fixture[2].read_bytes().splitlines(keepends=True))

    def test_22a_duplicate_metadata_permission_refused(self):
        block = self.FILTER + self.TABLE + "\t\tmetadataPermission: read\n" + self.COLUMN
        self._refuse_block(block, self.WHOLESALE, None)

    def test_22b_duplicate_bare_flag_refused(self):
        self._refuse_block(self.FILTER + "\t\tisHidden\n" * 2, self.WHOLESALE)

    def test_22c_mixed_flag_spelling_refused(self):
        self._refuse_block(self.FILTER + "\t\tisHidden: false\n\t\tisHidden\n", self.WHOLESALE)

    def test_23_lone_cr_body_refused(self):
        self._refuse_block(self.COMBINED, "1\r+ 2")

    def _expression_text(self, doc, node):
        self.assertIsNotNone(node.expr)
        return doc.text[node.expr.start:node.expr.end]

    def test_24_structural_continuation_refused(self):
        continuation = "\t\t\tcolumnPermission Name\n\t\t\t\tmetadataPermission: none"

        def prove(doc):
            node = self._permission(doc)
            self.assertEqual(node.children, [])
            self.assertEqual(node.props, {})
            self.assertIn(continuation, self._expression_text(doc, node))

        self._refuse_block(self.FILTER + continuation + "\n", self.WHOLESALE, None, proof=prove)

    def _swallowed_continuation_refused(self, line):
        continuation = "\t\t\t" + line

        def prove(doc):
            node = self._permission(doc)
            self.assertIn(continuation, self._expression_text(doc, node))
            self.assertEqual(node.props, {"metadataPermission": "none"})
            self.assertEqual([child.kind for child in node.children], ["columnPermission"])
            self.assertEqual([r.prop for r in doc.regions_for(node)], ["expression"])

        block = self.FILTER + continuation + "\n" + self.TABLE + self.COLUMN
        self._refuse_block(block, self.WHOLESALE, proof=prove)

    def test_24b_swallowed_equals_property_refused(self):
        self._swallowed_continuation_refused("filterExpression = FALSE()")

    def test_24c_swallowed_bare_flag_refused(self):
        self._swallowed_continuation_refused("isHidden")

    def test_24d_swallowed_unknown_declaration_refused(self):
        self._swallowed_continuation_refused("futureKind Item = 1")

    def test_25a_separator_first_set_refused(self):
        self._refuse_block(self.BARE + "\u2028" + self.TABLE, self.WHOLESALE)

    def test_25b_separator_update_refused(self):
        self._refuse_block(self.FILTER.rstrip("\n") + "\u2028" + self.TABLE, self.WHOLESALE)

    def test_25c_separator_removal_refused(self):
        self._refuse_block(self.FILTER.rstrip("\n") + "\u2028" + self.TABLE, None)

    def _closing_fence_separator_refused(self, dax):
        def prove(doc):
            self.assertFalse(doc.unclosed_fence)
            self._prove_table_perm(doc)
            node = self._permission(doc)
            self.assertEqual(self._expression_text(doc, node).strip(), self.RETAIL)

        block = self.FENCED.rstrip("\n") + "\u2028\n" + self.TABLE
        self._refuse_block(block, dax, proof=prove)

    def test_25d_separator_closing_fence_update_refused(self):
        self._closing_fence_separator_refused(self.WHOLESALE)

    def test_25e_separator_closing_fence_removal_refused(self):
        self._closing_fence_separator_refused(None)

    def test_26_crlf_update_preserves(self):
        fixture = self._project((self.ROLE + self.COMBINED).replace("\n", "\r\n"))
        before = fixture[2].read_bytes()
        self._filter(fixture, self.WHOLESALE)
        self._assert_state(fixture[2], self.COMBINED_PERMS, self.WHOLESALE)
        after = fixture[2].read_bytes()
        self.assertNotIn(b"\n", after.replace(b"\r\n", b""))
        self.assertEqual(after, before.replace(self.RETAIL.encode(), self.WHOLESALE.encode(), 1))

    def test_27a_duplicate_roles_same_file_refused(self):
        block = "\tmodelPermission: read\n" + self.ROLE + self.FILTER + self.TABLE
        self._refuse_block(block, self.WHOLESALE, None)

    def test_27b_nested_duplicate_role_refused(self):
        def proof(doc):
            node = self._permission(doc)
            outer = node.parent.parent
            self.assertIsNotNone(outer)
            self.assertEqual((outer.kind, outer.name), ("role", "Limited"))
            self.assertIsNotNone(node.expr)
            self.assertEqual(node.props, {"metadataPermission": "none"})

        block = "\t" + self.ROLE + "\t" + self.FILTER + "\t" + self.TABLE
        self._refuse_block(block, self.WHOLESALE, None, proof=proof)

    def test_27c_duplicate_role_second_file_refused(self):
        fixture = self._project(self.ROLE + self.COMBINED)
        _write(fixture[2].with_name("Dup.tmdl"), self.ROLE + "\tmodelPermission: read\n")
        self._refused(fixture, self.WHOLESALE)

    def test_28_swallowed_target_append_refused(self):
        continuation = "\t\t\ttablePermission Customer = FALSE()\n\t\t\t\tmetadataPermission: none"

        def prove(doc):
            sales = self._permission(doc, "Sales")
            self.assertIn(continuation, self._expression_text(doc, sales))
            self.assertNotIn(("tablePermission", "Customer"), [(node.kind, node.name) for node in doc.nodes])

        block = "\ttablePermission Sales = " + self.RETAIL + "\n" + continuation + "\n"
        self._refuse_block(block, self.RETAIL, proof=prove)

    def test_28b_escaped_apostrophe_swallowed_refused(self):
        continuation = "\t\t\ttablePermission 'O''Brien' = FALSE()"
        fixture = self._project(
            self.ROLE + "\ttablePermission Sales = " + self.RETAIL + "\n" + continuation + "\n"
        )
        path = fixture[2]
        definition = path.parent.parent
        _write(
            definition / "tables" / "OBrien.tmdl",
            "table 'O''Brien'\n\n\tcolumn Segment\n\t\tdataType: string\n",
        )
        _write(
            definition / "model.tmdl",
            "model Model\n\tculture: en-US\n\nref table Customer\nref table Sales\nref table 'O''Brien'\n",
        )

        doc = self._parse(path)
        sales = self._permission(doc, "Sales")
        self.assertIn(continuation, self._expression_text(doc, sales))
        self.assertNotIn(("tablePermission", "O'Brien"), [(node.kind, node.name) for node in doc.nodes])
        self._refused(fixture, self.RETAIL, table="O'Brien")

    def test_28c_distinct_quoted_name_append_control(self):
        authored = (
            self.ROLE + "\ttablePermission Sales = " + self.RETAIL + "\n"
            "\t\t\ttablePermission 'Customer''' = FALSE()\n"
        )
        fixture = self._project(authored)
        self._filter(fixture, self.RETAIL)
        state = _role_state(fixture[2])
        self.assertEqual(state["parents"]["Customer"], "Limited")
        self.assertEqual(state["filters"]["Customer"], self.RETAIL)
        expected = authored.rstrip("\r\n") + "\n\n" + self.FILTER
        self.assertEqual(fixture[2].read_bytes(), expected.encode())

    def test_28d_fenced_escaped_apostrophe_append_control(self):
        fixture = self._project(
            self.ROLE + "\ttablePermission Sales = ``` \n"
            "\t\t\ttablePermission 'O''Brien' = FALSE()\n"
            "\t\t```\n"
        )
        path = fixture[2]
        definition = path.parent.parent
        _write(
            definition / "tables" / "OBrien.tmdl",
            "table 'O''Brien'\n\n\tcolumn Segment\n\t\tdataType: string\n",
        )
        _write(
            definition / "model.tmdl",
            "model Model\n\tculture: en-US\n\nref table Customer\nref table Sales\nref table 'O''Brien'\n",
        )

        self._filter(fixture, self.RETAIL, table="O'Brien")
        self.assertEqual(_role_state(path)["parents"]["O'Brien"], "Limited")

    def test_28e_casefold_distinct_name_append_control(self):
        continuation = "\t\t\ttablePermission Straße = TRUE()"
        fixture = self._project(
            self.ROLE + "\ttablePermission Sales = TRUE()\n" + continuation + "\n"
        )
        path = fixture[2]
        definition = path.parent.parent
        _write(
            definition / "tables" / "Strasse.tmdl",
            "table Strasse\n\n\tcolumn Segment\n\t\tdataType: string\n",
        )
        _write(
            definition / "tables" / "StrasseU.tmdl",
            "table Straße\n\n\tcolumn Segment\n\t\tdataType: string\n",
        )
        _write(
            definition / "model.tmdl",
            "model Model\n\tculture: en-US\n\n"
            "ref table Customer\nref table Sales\nref table Strasse\nref table Straße\n",
        )

        doc = self._parse(path)
        sales = self._permission(doc, "Sales")
        self.assertIn(continuation, self._expression_text(doc, sales))
        self.assertNotIn(("tablePermission", "Straße"), [(node.kind, node.name) for node in doc.nodes])
        self._filter(fixture, self.RETAIL, table="Strasse")
        state = _role_state(path)
        self.assertEqual(state["counts"]["Strasse"], 1)
        self.assertEqual(state["parents"]["Strasse"], "Limited")
        self.assertEqual(state["filters"]["Strasse"], self.RETAIL)

    def test_28f_casefold_matching_name_refused(self):
        continuation = "\t\t\ttablePermission Straße = TRUE()"
        fixture = self._project(
            self.ROLE + "\ttablePermission Sales = TRUE()\n" + continuation + "\n"
        )
        path = fixture[2]
        definition = path.parent.parent
        _write(
            definition / "tables" / "Strasse.tmdl",
            "table Strasse\n\n\tcolumn Segment\n\t\tdataType: string\n",
        )
        _write(
            definition / "tables" / "StrasseU.tmdl",
            "table Straße\n\n\tcolumn Segment\n\t\tdataType: string\n",
        )
        _write(
            definition / "model.tmdl",
            "model Model\n\tculture: en-US\n\n"
            "ref table Customer\nref table Sales\nref table Strasse\nref table Straße\n",
        )

        doc = self._parse(path)
        sales = self._permission(doc, "Sales")
        self.assertIn(continuation, self._expression_text(doc, sales))
        self.assertNotIn(("tablePermission", "Straße"), [(node.kind, node.name) for node in doc.nodes])
        self._refused(fixture, self.RETAIL, table="Straße")

    def test_29_var_body_set_then_remove_refused(self):
        fixture = self._project(self.ROLE + self.COMBINED)
        path = fixture[2]
        perms = _role_state(path)["perms"]
        self._filter(fixture, "VAR allowed = TRUE()\nRETURN allowed")
        self._assert_state(path, perms, "VAR allowed = TRUE() RETURN allowed")
        self._refused(fixture, None)


if __name__ == "__main__":
    unittest.main()
