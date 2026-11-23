"""Fixture coverage for measured whole-suite allocation weights."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from ci_manifest import allocate


def report(**seconds):
    return {'executed': [dict(id=name, seconds=value) for name, value in seconds.items()]}


def entries():
    return [dict(id=name, argv=[f'tests/test_{name}.py'], windows=False, engine=False)
            for name in 'abcdef']


class CiWeightsTests(unittest.TestCase):
    def test_median_weights_from_fixture_reports(self):
        from ci_weights import compute_weights, load_reports
        reports = [report(a=1.21, even=2, tiny=0.01, single=7.36),
                   report(a=9, even=5, tiny=0.02), report(a=1.26, tiny=0.03)]
        reports[0]['executed'].append(dict(id='not_launched', seconds=99, launched=False))
        reports[0]['executed'].append(dict(id='a', seconds=500, exit_code=1))
        reports[0]['executed'].append(dict(id='a', seconds=500,
                                           source=dict(revision='r', path='p', sha256='s')))
        reports[0]['executed'].append(dict(id='failed_only', seconds=5, exit_code=1))
        reports[0]['executed'].append(dict(id='control_only', seconds=5,
                                           source=dict(revision='r', path='p', sha256='s')))
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index, data in enumerate(reports):
                path = Path(directory, f'{index}.json')
                path.write_text(json.dumps(data), encoding='utf-8')
                paths.append(path)
            self.assertEqual(compute_weights(load_reports(paths)),
                             {'a': 1.3, 'even': 3.5, 'tiny': 0.1, 'single': 7.4})

    def test_computed_weights_balance_fixture(self):
        from ci_weights import compute_weights
        seconds = dict(a=50, b=40, c=30, d=20, e=10, f=10)
        suites = entries()
        unit_sums = [sum(seconds[e['id']] for e in group) for group in allocate(suites, 2)]
        self.assertEqual(unit_sums, [90, 70])
        self.assertGreater(max(unit_sums), min(unit_sums) * 1.05)
        weights = compute_weights([report(**seconds)])
        from ci_weights import apply_weights
        weighted = apply_weights(suites, weights)
        sums = [sum(seconds[e['id']] for e in group) for group in allocate(weighted, 2)]
        self.assertLessEqual(max(sums), min(sums) * 1.05)
        self.assertEqual(sums, [80, 80])

    def test_allocate_partition_is_exact(self):
        suites = entries()
        groups = allocate(suites, 2)
        self.assertCountEqual([e['id'] for group in groups for e in group],
                              [e['id'] for e in suites])

    def test_entries_without_data_keep_no_weight(self):
        from ci_weights import apply_weights, compute_weights
        suites = entries() + [dict(id='future', argv=['tests/test_future.py'],
                                  windows=False, engine=False)]
        original = copy.deepcopy(suites)
        untouched = json.dumps(suites[-1]).encode('utf-8')
        weights = compute_weights([report(a=3.24)])
        applied = apply_weights(suites, weights)
        self.assertEqual(weights, {'a': 3.2})
        self.assertEqual(applied[0]['weight'], 3.2)
        self.assertEqual(json.dumps(applied[-1]).encode('utf-8'), untouched)
        self.assertNotIn('weight', applied[-1])
        self.assertEqual([{k: v for k, v in e.items() if k != 'weight'} for e in applied],
                         original)
        self.assertEqual(suites, original)


if __name__ == '__main__':
    unittest.main()
