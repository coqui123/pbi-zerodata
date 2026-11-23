"""Synthetic shard-context fixtures for measured whole-suite weights."""
import copy
from statistics import median
import unittest

from ci_manifest import allocate
from ci_weights import compute_weights, apply_weights


def report(run, shard, **seconds):
    return dict(schema=1, run=dict(id=run, attempt='1'), shard=shard,
                sha='a' * 40,
                identity=dict(os='Linux-fixture', python='3.12', image='fixture'),
                executed=[dict(id=name, seconds=value, launched=True, exit_code=0)
                          for name, value in seconds.items()])


def entries():
    return [dict(id=name, argv=[f'tests/test_{name}.py'], windows=False, engine=False)
            for name in 'abcdef']


class CiWeightsTests(unittest.TestCase):
    def test_pools_both_shards_across_dispatches(self):
        reports = []
        for run, a, b in [('one', 1.21, 8), ('two', 9, 2),
                          ('three', 1.26, 4), ('four', 4, 6)]:
            reports.extend([report(run, '0', a=a), report(run, '1', b=b)])
        self.assertEqual(compute_weights(reports), {'a': 2.6, 'b': 5.0})

    def test_refuses_serial_reports(self):
        reports = [report(str(i), '0', a=1) for i in range(3)]
        reports.append(report('serial-run', 'serial', a=1))
        with self.assertRaisesRegex(ValueError, 'serial'):
            compute_weights(reports)
        with self.assertRaisesRegex(ValueError, 'invalid shard'):
            compute_weights([report(str(i), str(i % 2), a=1) for i in range(3)]
                            + [report('extra', '2', a=1)])

    def test_refuses_mixed_sha(self):
        reports = [report(str(i), str(i % 2), a=1) for i in range(3)]
        reports[-1]['sha'] = 'b' * 40
        with self.assertRaises(ValueError) as raised:
            compute_weights(reports)
        for sha in ('a' * 40, 'b' * 40):
            self.assertIn(sha, str(raised.exception))

    def test_refuses_mixed_identity(self):
        reports = [report(str(i), str(i % 2), a=1) for i in range(3)]
        reports[-1]['identity']['os'] = 'Windows-fixture'
        with self.assertRaises(ValueError) as raised:
            compute_weights(reports)
        for os in ('Linux-fixture', 'Windows-fixture'):
            self.assertIn(os, str(raised.exception))

    def test_filters_rows_without_discarding_eligible_peers(self):
        reports = [report(str(i), str(i % 2), a=value)
                   for i, value in enumerate((1, 2, 3))]
        for data in reports:
            for excluded in (dict(launched=False), dict(exit_code=1), dict(source=None)):
                for name in ('a', 'excluded_only'):
                    row = dict(id=name, seconds=500, launched=True, exit_code=0)
                    row.update(excluded)
                    data['executed'].append(row)
        self.assertEqual(compute_weights(reports), {'a': 2.0})

    def test_refuses_duplicate_report_and_duplicate_rows(self):
        reports = [report('one', '0', a=1), report('one', '1', b=2),
                   report('two', '0', a=3), report('three', '0', a=5)]
        with self.assertRaisesRegex(ValueError, 'duplicate report'):
            compute_weights(reports + [reports[0]])
        doubled = [report('one', '0', a=1), report('one', '1', a=2, b=2),
                   report('two', '0', a=3), report('three', '0', a=5)]
        with self.assertRaisesRegex(ValueError, 'duplicate eligible row'):
            compute_weights(doubled)

    def test_rows_missing_outcome_keys_contribute_nothing(self):
        reports = [report(str(i), str(i % 2), a=value)
                   for i, value in enumerate((1, 2, 3))]
        for data in reports:
            for incomplete in ({'launched': None}, {'exit_code': None}):
                row = dict(id='a', seconds=500, launched=True, exit_code=0)
                key = next(iter(incomplete))
                del row[key]
                data['executed'].append(row)
        self.assertEqual(compute_weights(reports), {'a': 2.0})

    def test_floor_and_rounding(self):
        reports = [report(str(i), str(i % 2), tiny=tiny, down=down, up=up)
                   for i, (tiny, down, up) in enumerate(
                       [(0.01, 1.21, 7.31), (0.02, 1.24, 7.36), (0.03, 1.29, 7.39)])]
        self.assertEqual(compute_weights(reports), {'tiny': 0.1, 'down': 1.2, 'up': 7.4})

    def test_sample_minimum_preserves_prior_weight(self):
        reports = [report('one', '0', sparse=20, enough=2),
                   report('two', '0', sparse=30, enough=3),
                   report('three', '0', enough=4)]
        weights = compute_weights(reports)
        self.assertEqual(weights, {'enough': 3.0})
        suites = [dict(id='sparse', weight=7.3), dict(id='enough', weight=1.0),
                  dict(id='absent', weight=9.1)]
        applied = apply_weights(suites, weights)
        self.assertEqual(applied, [suites[0], dict(id='enough', weight=3.0), suites[2]])

    def test_apply_weights_is_pure_and_preserves_entries_without_data(self):
        suites = entries()
        suites[0]['weight'] = 9.0
        suites[1]['weight'] = 7.3
        original = copy.deepcopy(suites)
        weights = {'a': 3.2}
        applied = apply_weights(suites, weights)
        self.assertIsNot(applied, suites)
        self.assertEqual(suites, original)
        self.assertEqual(weights, {'a': 3.2})
        self.assertEqual(applied[0], dict(original[0], weight=3.2))
        self.assertEqual(applied[1:], original[1:])
        for before, after in zip(suites, applied):
            self.assertIsNot(before, after)
            self.assertIsNot(before['argv'], after['argv'])
        applied[1]['argv'].append('--changed')
        self.assertEqual(suites, original)

    def test_shard_basis_balances_one_fixed_shard_context(self):
        shard_seconds = dict(a=50, b=40, c=30, d=20, e=10, f=10)
        suites = entries()
        serial_reports, shard_reports = [], []
        for run in ('one', 'two', 'three'):
            serial_reports.append(report(run, 'serial', **dict.fromkeys(shard_seconds, 10)))
            for shard, group in enumerate(allocate(suites, 2)):
                shard_reports.append(report(run, str(shard), **{
                    entry['id']: shard_seconds[entry['id']] for entry in group}))

        # Serial medians are a comparison oracle; the tool must refuse serial input.
        serial_weights = {
            name: max(0.1, round(median(row['seconds'] for data in serial_reports
                                      for row in data['executed'] if row['id'] == name), 1))
            for name in shard_seconds}
        shard_weights = compute_weights(shard_reports)
        self.assertEqual(serial_weights, dict.fromkeys(shard_seconds, 10.0))
        self.assertEqual(shard_weights, shard_seconds)
        serial_allocation = allocate(apply_weights(suites, serial_weights), 2)
        shard_allocation = allocate(apply_weights(suites, shard_weights), 2)
        serial_sums = [sum(shard_seconds[e['id']] for e in group)
                       for group in serial_allocation]
        shard_sums = [sum(shard_seconds[e['id']] for e in group)
                      for group in shard_allocation]
        self.assertEqual(serial_sums, [90, 70])
        self.assertEqual(shard_sums, [80, 80])
        self.assertGreater(max(serial_sums), min(serial_sums) * 1.05)
        self.assertLessEqual(max(shard_sums), min(shard_sums) * 1.05)


if __name__ == '__main__':
    unittest.main()
