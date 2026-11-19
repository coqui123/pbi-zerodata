"""Registration, executed-set and shadow gate acceptance."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import contextlib
import io
from ci_equivalence import compare, rollout, serial_inventory
from ci_timing import measured
from ci_hosted_metrics import metrics
import unittest
from unittest.mock import patch
from ci_manifest import BASELINE, MANIFEST, allocate, controls, load, validate
import run_offline


class CiRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.entries = load()

    def test_checked_in_workflow_registers_offline_suites(self):
        self.assertEqual([{k: e[k] for k in ('id', 'argv')} for e in self.entries],
                         json.loads(BASELINE.read_text())['suites'])
        self.assertEqual(sum(e['argv'] == ['tests/test_engine.py', '--offline'] for e in self.entries), 1)

    def test_unregistered_offline_suite_fails_the_guard(self):
        with self.assertRaisesRegex(ValueError, 'omission'):
            validate([e for e in self.entries if e['id'] != 'ci_registration'])

    def test_engine_offline_invocation_is_registration(self):
        entries = copy.deepcopy(self.entries)
        next(e for e in entries if e['id'] == 'engine-offline')['argv'].pop()
        with self.assertRaisesRegex(ValueError, '--offline'):
            validate(entries)

    def test_duplicates_extra_nonexistent_e2e(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            validate(self.entries + [self.entries[0]])
        for script, reason in [('missing', 'nonexistent'), ('e2e', 'live-only')]:
            with self.subTest(script=script), self.assertRaisesRegex(ValueError, reason):
                validate(self.entries + [dict(id='extra', argv=[f'tests/test_{script}.py'], windows=True, engine=True)])
        entry = dict(self.entries[0], id='extra')
        with self.assertRaisesRegex(ValueError, 'multiplicity'):
            validate(self.entries + [entry])

    def test_commented_out_entry_is_not_registration(self):
        text = MANIFEST.read_text().replace('"id": "guard"', '// "id": "guard"')
        with self.assertRaises(json.JSONDecodeError):
            json.loads(text)

    def test_shards_cover_exact_inventory_once(self):
        groups = allocate(self.entries, 2)
        self.assertCountEqual([e['id'] for g in groups for e in g], [e['id'] for e in self.entries])

    def test_executed_set_is_emitted_even_on_failure(self):
        def child(argv, **kw):
            kw['stdout'].write(b'ok\n')
            return subprocess.CompletedProcess(argv, 1 if any('test_guard.py' in a for a in argv) else 0)
        with tempfile.TemporaryDirectory() as tmp, patch.object(run_offline, 'identity', return_value={}), contextlib.redirect_stdout(io.StringIO()), patch.object(run_offline.subprocess, 'check_output', return_value='fixture'):
            report = run_offline.execute(self.entries, output=tmp, run=child,
                                         scanner=lambda roots: {'completed': True, 'ok': True})
        self.assertEqual([r['id'] for r in report['executed']], [e['id'] for e in self.entries])
        self.assertFalse(report['ok'])

    def test_real_fresh_process_reports_exit_skip_and_scan(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(run_offline, 'identity', return_value={}), contextlib.redirect_stdout(io.StringIO()), patch.object(run_offline.subprocess, 'check_output', return_value='fixture'):
            path = Path(tmp, 'suite.py')
            path.write_text("import unittest\nclass T(unittest.TestCase):\n def test_skip(self): self.skipTest('injected reason')\nunittest.main()\n", encoding='utf-8')
            report = run_offline.execute([{'id': 'control', 'argv': [str(path)]}], output=tmp,
                                         scanner=lambda roots: {'completed': True, 'ok': True})
            row = report['executed'][0]
            self.assertEqual(row['exit_code'], 0)
            self.assertEqual(len(row['skip_reasons']), 1)
            self.assertIn('injected reason', row['skip_reasons'][0])
            self.assertTrue(report['scan']['completed'])

    def test_injected_control_records_specific_failure_reason(self):
        record = dict(id='injected-control', argv=['tests/test_ci_registration.py', '--injected-failure'], purpose='injected',
                      exit_code=91, reason='PBZ_INJECTED_FAILURE')
        entries, expected = controls(self.entries, [record])
        with tempfile.TemporaryDirectory() as tmp, patch.object(run_offline, 'identity', return_value={}), contextlib.redirect_stdout(io.StringIO()), patch.object(run_offline.subprocess, 'check_output', return_value='fixture'):
            report = run_offline.execute([entries[-1]], output=tmp, expected_failures=expected,
                                         scanner=lambda roots: {'completed': True, 'ok': True})
        self.assertFalse(report['ok'])
        self.assertEqual(report['executed'][0]['exit_code'], 91)
        self.assertEqual(report['executed'][0]['failure_reason'], 'PBZ_INJECTED_FAILURE')


class EquivalenceTests(unittest.TestCase):
    def reports(self):
        entries = json.loads(BASELINE.read_text())['suites']
        rows = [dict(e, exit_code=0, skip_reasons=[], error=None) for e in entries]
        base = {'sha': 'same', 'identity': {k: 'fixed' for k in
                ('python', 'os', 'image', 'dotnet', 'dependencies', 'nuget', 'source', 'project', 'payload')},
                'ok': True, 'scan': {'completed': True, 'ok': True}}
        return dict(base, shard='serial', executed=copy.deepcopy(rows)), [
            dict(copy.deepcopy(base), shard=i, executed=copy.deepcopy(rows[i::2])) for i in range(2)]

    def test_positive_control(self):
        ref, shards = self.reports()
        self.assertTrue(compare(ref, shards))

    def test_serial_aggregate_checks_actual_inventory(self):
        ref, _ = self.reports()
        self.assertTrue(serial_inventory(ref))
        ref['executed'].pop()
        with self.assertRaisesRegex(ValueError, 'membership'):
            serial_inventory(ref)

    def test_reject_missing_duplicate_cancelled_shards(self):
        for variant in ('missing', 'duplicate', 'cancelled'):
            ref, shards = self.reports()
            if variant == 'missing':
                shards.pop()
            elif variant == 'duplicate':
                shards[1]['shard'] = 0
            else:
                shards[1]['scan'] = {}
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                compare(ref, shards)

    def test_reject_wrong_executed_inventory_and_argv(self):
        for variant in ('omission', 'duplicate', 'extra', 'argv'):
            ref, shards = self.reports()
            rows = shards[0]['executed']
            if variant == 'omission':
                rows.pop()
            elif variant == 'duplicate':
                rows.append(rows[0])
            elif variant == 'extra':
                rows.append(dict(rows[0], id='unknown'))
            else:
                rows[0]['argv'] = ['tests/test_engine.py']
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                compare(ref, shards)

    def test_identity_skips_scans_verdict_sha(self):
        for variant in ('identity', 'skip', 'scan', 'verdict', 'sha'):
            ref, shards = self.reports()
            if variant == 'identity':
                shards[0]['identity']['dotnet'] = 'different'
            elif variant == 'skip':
                shards[0]['executed'][0]['skip_reasons'] = ['new skip']
            elif variant == 'scan':
                shards[0]['scan']['completed'] = False
            elif variant == 'sha':
                shards[0]['sha'] = 'different'
            else:
                shards[0]['ok'] = False
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                compare(ref, shards)

    def test_red_agreement_requires_expected_suite_and_reason(self):
        ref, shards = self.reports()
        row = shards[0]['executed'][0]
        row.update(exit_code=9, failure_reason='injected-control')
        ref['executed'][0].update(exit_code=9, failure_reason='injected-control')
        ref['ok'] = shards[0]['ok'] = False
        with self.assertRaisesRegex(ValueError, 'unexpected suite failure'):
            compare(ref, shards)
        self.assertTrue(compare(ref, shards, expected_failures={row['id']: (9, 'injected-control')}))
        with self.assertRaisesRegex(ValueError, 'expected failure reason'):
            compare(ref, shards, expected_failures={row['id']: (9, 'wrong-reason')})

    def evidence(self):
        result = {'cancelled_attempts': [], 'pairs': [dict(pair_id=i, sha='same', equivalent=True, cold_cache=i == 0,
                              serial_run=f'serial-{i}', sharded_run=f'sharded-{i}',
                              serial_seconds=400, sharded_seconds=250,
                              serial_billed_minutes=7, sharded_billed_minutes=9) for i in range(10)],
                **{k: {'verified': True, 'run_id': 'control', 'reason': 'expected specific failure'} for k in
                   ('historical_failure', 'injected_failure', 'canary_without_engine')}}
        for p in result['pairs']:
            p['reference'], p['shards'] = self.reports()
            for r in [p['reference'], *p['shards']]: r['cold_cache'] = p['cold_cache']
            for mode in ('serial', 'sharded'):
                p[mode + '_metrics'] = dict(run_id=p[mode + '_run'], sha=p['sha'], conclusion='success',
                                           elapsed_seconds=p[mode + '_seconds'], billed_minutes=p[mode + '_billed_minutes'])
        for key, purpose in (('historical_failure', 'historical'), ('injected_failure', 'injected')):
            ref, shards = self.reports()
            control = dict(id='guard' if purpose == 'historical' else 'injected-control',
                           argv=['tests/test_guard.py'] if purpose == 'historical' else ['tests/test_ci_registration.py', '--injected-failure'],
                           exit_code=91, reason='specific failure reason', purpose=purpose)
            if purpose == 'injected':
                row = dict(id=control['id'], argv=control['argv'], exit_code=91, error=None,
                           skip_reasons=[], failure_reason=control['reason'])
                ref['executed'].append(row)
                shards[0]['executed'].append(copy.deepcopy(row))
            else:
                for r in [ref, *shards]:
                    for row in r['executed']:
                        if row['id'] == control['id']:
                            row.update(exit_code=91, failure_reason=control['reason'])
            for r in [ref, *shards]:
                r['ok'] = all(e['exit_code'] == 0 for e in r['executed'])
            result[key] = dict(run_id='control-' + purpose, controls=[control], reference=ref, shards=shards,
                               historical_revision='a' * 40)
        ref, shards = self.reports()
        planted = next(r for r in shards if not any(e['id'] == 'engine-offline' for e in r['executed']))
        planted['ok'] = False
        planted['scan'] = dict(completed=True, ok=False, failures=[{'reason': 'canary'}])
        result['canary_without_engine'] = dict(run_id='canary-control', aggregate_verdict='failure', reference=ref, shards=shards)
        return result

    def test_rollout_positive_and_all_thresholds(self):
        self.assertTrue(rollout(self.evidence()))
        for variant in ('pairs', 'cold', 'median', 'maximum', 'cost', 'cancelled', 'control'):
            evidence = self.evidence()
            if variant == 'pairs':
                evidence['pairs'].pop()
            elif variant == 'cold':
                for p in evidence['pairs']: p['cold_cache'] = False
            elif variant == 'median':
                for p in evidence['pairs']: p['sharded_seconds'] = 300
            elif variant == 'maximum':
                evidence['pairs'][0]['sharded_seconds'] = 401
            elif variant == 'cost':
                for p in evidence['pairs']: p['sharded_billed_minutes'] = 11
            elif variant == 'cancelled':
                evidence['pairs'][0]['cancelled'] = True
            else:
                evidence.pop('historical_failure')
            for pair in evidence['pairs']:
                for mode in ('serial', 'sharded'):
                    pair[mode + '_metrics']['elapsed_seconds'] = pair[mode + '_seconds']
                    pair[mode + '_metrics']['billed_minutes'] = pair[mode + '_billed_minutes']
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                rollout(evidence)

    def test_hosted_metrics_include_setup_and_final_aggregate(self):
        run = dict(id='run', head_sha='same', status='completed', conclusion='success', created_at='2026-11-19T00:00:00Z')
        jobs = [dict(status='completed', conclusion='success', started_at='2026-11-19T00:00:10Z', completed_at='2026-11-19T00:01:11Z'),
                dict(status='completed', conclusion='success', started_at='2026-11-19T00:01:20Z', completed_at='2026-11-19T00:01:30Z')]
        result = metrics(run, jobs, 3)
        self.assertEqual(result['elapsed_seconds'], 90)
        self.assertEqual(result['rounded_job_minutes_estimate'], 3)
        self.assertEqual(result['billed_minutes'], 3)
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            metrics(dict(run, conclusion='cancelled'), jobs, 3)

    def test_timing_preserves_real_calls_and_exceptions(self):
        totals = {}
        def real(verb):
            if verb == 'fail': raise RuntimeError('real error')
            return verb
        fn = measured(real, 'tom', totals)
        self.assertEqual(fn('ok'), 'ok')
        with self.assertRaises(RuntimeError):
            fn('fail')
        self.assertEqual(totals['tom']['calls'], 2)
        self.assertGreaterEqual(totals['tom']['seconds'], 0)


if __name__ == '__main__':
    if '--injected-failure' in sys.argv:
        print('PBZ_INJECTED_FAILURE')
        raise SystemExit(91)
    unittest.main()
