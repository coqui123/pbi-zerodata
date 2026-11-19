"""Offline regression tests for the shared rename bindings and atomic refusals."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.dirname(__file__))

from pbz import consolidate, deps, rename, tmdl
from pbz.common import PbzError
from pbz.tokens import apply_edits
import test_modeling_atomic as fixtures
from test_modeling_atomic import _bytes, _write


class RenameBindingsTests(unittest.TestCase):
    _project = fixtures.ModelingAtomicTests._project
    _column_project = fixtures.ModelingAtomicTests._column_project

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

    def test_bare_after_identifier_refuses_rename(self):
        ws, project, mdef = self._project()
        Path(mdef, 'tables', 'Sales.tmdl').write_text(
            'table Sales\n\tmeasure Total = 1\n\tmeasure Use = VAR x = 1 RETURN[Total]\n')
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
            self.assertTrue(all(b.uncertain for b in deps.m_bindings(expr, res)), expr)

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
