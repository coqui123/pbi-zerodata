"""Offline usage graph regression tests for DAX and M bindings."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.dirname(__file__))

from pbz import deps, tmdl, usage
import test_unused_atomic as fixtures


class UsageBindingsTests(unittest.TestCase):
    _project = fixtures.UnusedAtomicTests._project
    _sales = fixtures.UnusedAtomicTests._sales

    def graph(self, texts):
        model = tmdl.load_model('', texts=texts)
        graph = usage.Graph()
        usage._model_objects(graph, model)
        usage._model_edges(graph, model, deps.Resolver(model))
        return graph, model

    def test_return_adjacent_measure_edge(self):
        ws, project = self._project()
        path = Path(self._sales(ws, project))
        path.write_text(path.read_text() + '\tmeasure Total = 1\n\tmeasure Use = VAR x = 1 RETURN[Total]\n')
        graph = usage.build(ws, project)
        src = usage.key('measure', 'Sales', 'Use')
        self.assertIn(usage.key('measure', 'Sales', 'Total'), [e.dst for e in graph.out[src]])
        self.assertFalse(graph.objects[src].issues)

    def test_calculation_item_binds_measure_before_home_column(self):
        graph, model = self.graph({
            'tables/Home.tmdl': 'table Home\n\tcolumn Qty\n\tcalculationGroup\n\t\tcalculationItem Item = [Qty]\n',
            'tables/Measures.tmdl': 'table Measures\n\tmeasure Qty = 1\n',
        })
        src = usage.key('calc_item', 'Home', 'Item')
        self.assertIn(usage.key('measure', 'Measures', 'Qty'), [e.dst for e in graph.out[src]])
        self.assertNotIn(usage.key('column', 'Home', 'Qty'), [e.dst for e in graph.out[src]])
        doc = model.docs['tables/Home.tmdl']
        reg = next(r for r in doc.regions if r.kind == 'dax')
        self.assertEqual(deps.dax_region(doc.text, reg, deps.Resolver(model)).refs,
                         [deps.Ref('measure', 'Measures', 'Qty')])

    def test_dax_edges_honors_explicit_context(self):
        graph, model = self.graph({
            'tables/Home.tmdl': 'table Home\n\tcolumn Qty\n\tmeasure Use = [Qty]\n',
            'tables/Other.tmdl': 'table Other\n\tcolumn Qty\n\tmeasure Qty = 1\n',
        })
        refs = usage._dax_edges(graph, usage.key('measure', 'Home', 'Use'), model, deps.Resolver(model),
                                model.measure('Use').node, 'Other', 'test', row_home=True)
        self.assertEqual(refs, [deps.Ref('column', 'Other', 'Qty')])

    def test_case_sensitive_query_objects_and_deduped_edges(self):
        graph, _ = self.graph({
            'expressions.tmdl': 'expression Source = 1\n\nexpression source = 2\n\n'
                                'expression Use = let a = Source, b = Source, c = source in {a,b,c}\n',
        })
        upper, lower, use = (usage.key('m_query', '', n) for n in ('Source', 'source', 'Use'))
        self.assertNotEqual(upper, lower)
        self.assertIn(upper, graph.objects)
        self.assertIn(lower, graph.objects)
        self.assertEqual([e.dst for e in graph.out[use]], [upper, lower])
        self.assertEqual(len(graph.edges), 2)

    def test_m_edges_dedupe_across_partitions_and_keep_locks(self):
        graph, _ = self.graph({
            'expressions.tmdl': 'expression Source = 1\n',
            'tables/Home.tmdl': 'table Home\n\tpartition One = m\n\t\tsource = Source\n'
                                '\tpartition Two = m\n\t\tsource = try Source otherwise Source\n',
        })
        src, dst = usage.key('table', '', 'Home'), usage.key('m_query', '', 'Source')
        self.assertEqual([e.dst for e in graph.out[src] if e.via == 'm'], [dst])
        self.assertIn('uncertain M binding', ' '.join(graph.locks[dst]))

    def test_m_literals_have_certain_graph_edges(self):
        for expr in ('Table.TransformColumnTypes(Source, {{"A", type text}})', '#date(2026,1,1) & Source'):
            with self.subTest(expr=expr):
                graph, model = self.graph({'expressions.tmdl': f'expression Source = 1\n\nexpression Use = {expr}\n'})
                src, dst = (usage.key('m_query', '', n) for n in ('Use', 'Source'))
                self.assertEqual([e.dst for e in graph.out[src]], [dst])
                self.assertNotIn(dst, graph.locks)
                self.assertFalse(any(b.uncertain for b in deps.m_bindings(expr, deps.Resolver(model))))

    def test_uncertainty_does_not_self_lock(self):
        graph, _ = self.graph({'expressions.tmdl': 'expression Self = try Self otherwise 1\n'})
        self.assertNotIn(usage.key('m_query', '', 'Self'), graph.locks)


if __name__ == '__main__':
    unittest.main()
