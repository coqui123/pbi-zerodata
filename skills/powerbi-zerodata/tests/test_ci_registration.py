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
import os
import types
from ci_equivalence import compare, rollout, serial_inventory
from ci_timing import measured, instrument
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
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'manifest.json')
            lines = MANIFEST.read_text().splitlines()
            index = next(i for i, line in enumerate(lines) if '"id": "guard"' in line)
            lines[index] = '// ' + lines[index]
            path.write_text('\n'.join(lines))
            with self.assertRaises(json.JSONDecodeError):
                load(path)
            with self.assertRaisesRegex(ValueError, 'missing=.*test_guard.py'):
                validate([e for e in self.entries if e['id'] != 'guard'])

    def test_non_test_script_and_requirement_flags(self):
        with self.assertRaisesRegex(ValueError, 'non-test'):
            validate(self.entries + [dict(id='extra', argv=['tests/ci_manifest.py'], windows=False, engine=False)])
        for name, key, value, reason in [('guard', 'windows', 1, 'boolean'),
                                         ('guard', 'engine', 'true', 'boolean'),
                                         ('ci_registration', 'engine', True, 'engine-free'),
                                         ('engine-offline', 'engine', False, 'engine and windows'),
                                         ('engine-offline', 'windows', False, 'engine and windows')]:
            entries = copy.deepcopy(self.entries)
            next(e for e in entries if e['id'] == name)[key] = value
            with self.subTest(name=name, key=key), self.assertRaisesRegex(ValueError, reason):
                validate(entries)
        registration = next(e for e in self.entries if e['id'] == 'ci_registration')
        self.assertFalse(registration['windows'])
        self.assertFalse(registration['engine'])

    def test_workflow_offline_jobs_use_only_executors(self):
        root = Path(__file__).resolve().parents[3]
        for name in ('tests.yml', 'offline-shadow.yml'):
            job = None
            in_run = False
            for line in (root / '.github/workflows' / name).read_text().splitlines():
                if line.startswith('  ') and not line.startswith('   ') and line.endswith(':'):
                    job = line.strip().rstrip(':')
                # Vendor validator and opt-in Desktop jobs have separate live contracts.
                if job in ('vendor-validator', 'live-synth'):
                    continue
                if 'run:' in line:
                    in_run = True
                elif line.strip() and len(line) - len(line.lstrip()) <= 8:
                    in_run = False
                if in_run:
                    self.assertNotRegex(line, r'tests[/\\]test_[A-Za-z0-9_]+\.py')

    def test_shards_cover_exact_inventory_once(self):
        groups = allocate(self.entries, 2)
        self.assertCountEqual([e['id'] for g in groups for e in g], [e['id'] for e in self.entries])

    def test_executed_set_is_emitted_even_on_failure(self):
        observed = []
        def child(argv, **kw):
            observed.append(argv)
            if 'nonexistent.py' in argv:
                raise FileNotFoundError('injected launch failure')
            kw['stdout'].write(b'ok\n')
            return subprocess.CompletedProcess(argv, 1 if 'tests/test_guard.py' in argv else 0)
        entries = [*self.entries, dict(id='launch-failure', argv=['nonexistent.py'], windows=False, engine=False)]
        with tempfile.TemporaryDirectory() as tmp, patch.object(run_offline, 'identity', return_value={}), contextlib.redirect_stdout(io.StringIO()), patch.object(run_offline.subprocess, 'check_output', return_value='fixture'):
            report = run_offline.execute(entries, output=tmp, run=child,
                                         scanner=lambda roots: {'completed': True, 'ok': True})
            self.assertEqual(list(Path(tmp).glob('*.log')), [])
        self.assertEqual([r['argv'] for r in report['executed']], [argv[2:] for argv in observed])
        self.assertEqual([r['id'] for r in report['executed']], [e['id'] for e in entries])
        for entry, row in zip(entries, report['executed']):
            self.assertEqual((row['windows'], row['engine']), (entry['windows'], entry['engine']))
        self.assertTrue(all(r['launched'] for r in report['executed'][:-1]))
        self.assertFalse(report['executed'][-1]['launched'])
        self.assertIsNotNone(report['executed'][-1]['error'])
        self.assertFalse(report['ok'])

    def test_setup_failures_finalize_and_output_errors_fail_verdict(self):
        for target in ('identity', 'sha', 'log-read'):
            with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()), patch.object(run_offline, 'identity', side_effect=RuntimeError('setup') if target == 'identity' else None, return_value={}), patch.object(run_offline.subprocess, 'check_output', side_effect=RuntimeError('sha') if target == 'sha' else None, return_value='fixture'), patch.dict(os.environ, {}, clear=True):
                def child(argv, **kw):
                    kw['stdout'].write(b'ok')
                    return subprocess.CompletedProcess(argv, 0)
                scanner = unittest.mock.Mock(return_value={'completed': True, 'ok': True})
                with patch.object(Path, 'read_text', side_effect=OSError('log read') if target == 'log-read' else None):
                    report = run_offline.execute([self.entries[0]], output=tmp, run=child, scanner=scanner)
                scanner.assert_called_once()
                self.assertFalse(report['ok'])
                if target == 'log-read':
                    self.assertNotIn('error', report)
                    self.assertEqual(report['executed'][0]['exit_code'], 0)
                    self.assertIsNotNone(report['executed'][0]['error'])

    def test_hosted_run_binding_and_relative_controls(self):
        import ci_manifest
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='2', GITHUB_WORKFLOW='tests'), patch.object(run_offline, 'identity', return_value={}), patch.object(run_offline.subprocess, 'check_output', return_value='fixture'):
            report = run_offline.execute([], output=tmp, scanner=lambda roots: {'completed': True, 'ok': True})
        self.assertEqual(report['run'], dict(id='123', attempt='2', workflow='tests'))
        records, expected = controls(self.entries, 'skills/powerbi-zerodata/tests/ci_controls.json')
        self.assertIn('injected-control', expected)

    def test_historical_revision_executes_old_script(self):
        import ci_manifest
        with tempfile.TemporaryDirectory() as tmp, patch.object(ci_manifest, 'ROOT', Path(tmp)):
            root = Path(tmp)
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=root, text=True).strip()
            git('init', '-q')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.test')
            path = root / 'suite.py'
            path.write_text("print('HISTORICAL_FAILURE')\nraise SystemExit(7)\n")
            git('add', '.')
            git('commit', '-qm', 'failing')
            revision = git('rev-parse', 'HEAD')
            path.write_text('raise SystemExit(0)\n')
            git('commit', '-qam', 'passing')
            record = dict(id='guard', purpose='historical', exit_code=7,
                          reason='HISTORICAL_FAILURE', revision=revision, script='suite.py')
            entries, expected = controls(self.entries, [record])
            with patch.object(run_offline, 'identity', return_value={}), patch.object(run_offline, 'ROOT', root), contextlib.redirect_stdout(io.StringIO()):
                report = run_offline.execute([next(e for e in entries if e['id'] == 'guard')], output=root / 'output', expected_failures=expected,
                                             scanner=lambda roots: {'completed': True, 'ok': True})
            self.assertEqual(report['shard'], 'serial')
            self.assertEqual(report['executed'][0]['exit_code'], 7)
            self.assertEqual(report['executed'][0]['failure_reason'], 'HISTORICAL_FAILURE')
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
        entries = json.loads(MANIFEST.read_text())
        rows = [dict(e, exit_code=0, skip_reasons=[], error=None) for e in entries]
        base = {'sha': 'same', 'identity': {k: 'fixed' for k in
                ('python', 'os', 'image', 'dotnet', 'dependencies', 'nuget', 'source', 'project', 'payload')},
                'ok': True, 'scan': {'completed': True, 'ok': True}}
        return dict(base, shard='serial', executed=copy.deepcopy(rows)), [
            dict(copy.deepcopy(base), shard=i, executed=copy.deepcopy([next(r for r in rows if r['id'] == e['id']) for e in group])) for i, group in enumerate(allocate(entries, 2))]

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
            with self.subTest(variant=variant), self.assertRaisesRegex(ValueError, {'missing': 'missing, duplicate', 'duplicate': 'missing, duplicate', 'cancelled': 'scan', 'omission': 'allocation', 'extra': 'allocation', 'argv': 'wrong executed argv', 'identity': 'identity', 'skip': 'different skip_reasons', 'scan': 'scan', 'verdict': 'verdict', 'sha': 'different SHAs'}[variant]):
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
            with self.subTest(variant=variant), self.assertRaisesRegex(ValueError, {'missing': 'missing, duplicate', 'duplicate': 'allocation', 'cancelled': 'scan', 'omission': 'allocation', 'extra': 'allocation', 'argv': 'wrong executed argv', 'identity': 'identity', 'skip': 'different skip_reasons', 'scan': 'scan', 'verdict': 'verdict', 'sha': 'different SHAs'}[variant]):
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
            with self.subTest(variant=variant), self.assertRaisesRegex(ValueError, {'missing': 'missing, duplicate', 'duplicate': 'missing, duplicate', 'cancelled': 'scan', 'omission': 'allocation', 'extra': 'allocation', 'argv': 'wrong executed argv', 'identity': 'identity', 'skip': 'different skip_reasons', 'scan': 'scan', 'verdict': 'verdict', 'sha': 'different SHAs'}[variant]):
                compare(ref, shards)

    def test_red_agreement_requires_expected_suite_and_reason(self):
        ref, shards = self.reports()
        row = shards[0]['executed'][0]
        row.update(exit_code=9, failure_reason='injected-control')
        next(e for e in ref['executed'] if e['id'] == row['id']).update(exit_code=9, failure_reason='injected-control')
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
            p['reference']['run'] = {'id': p['serial_run']}
            for r in p['shards']: r['run'] = {'id': p['sharded_run']}
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
                inventory, _ = controls(json.loads(MANIFEST.read_text()), [control], check_scripts=False)
                rows = {e['id']: e for e in ref['executed']}
                for shard, group in zip(shards, allocate(inventory, 2)):
                    shard['executed'] = copy.deepcopy([rows[e['id']] for e in group])
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
            with self.subTest(variant=variant), self.assertRaisesRegex(ValueError, {'pairs': 'ten distinct', 'cold': 'cold-cache', 'median': 'median improvement', 'maximum': 'maximum elapsed', 'cost': 'runner-minute', 'cancelled': 'cancelled or unproven', 'control': 'missing expected-failure'}[variant]):
                rollout(evidence)

    def test_run_binding_allocation_and_agreeing_new_skip(self):
        for variant, reason in [('same-run', 'run binding'), ('split-run', 'run binding'),
                                ('partial-binding', 'run binding'), ('allocation', 'allocation'), ('agreeing-skip', 'unexpected skip')]:
            ref, shards = self.reports()
            ref['run'] = {'id': 'serial'}
            for shard in shards: shard['run'] = {'id': 'shards'}
            if variant == 'same-run':
                ref['run']['id'] = 'shards'
            elif variant == 'split-run':
                shards[0]['run']['id'] = 'different'
            elif variant == 'partial-binding':
                shards[0].pop('run')
            elif variant == 'allocation':
                shards[0]['executed'][0], shards[1]['executed'][0] = shards[1]['executed'][0], shards[0]['executed'][0]
            else:
                row = shards[0]['executed'][0]
                row['skip_reasons'] = ['new reason']
                next(e for e in ref['executed'] if e['id'] == row['id'])['skip_reasons'] = ['new reason']
            with self.subTest(variant=variant), self.assertRaisesRegex(ValueError, reason):
                compare(ref, shards)

    def test_rollout_bindings_and_canary_evidence(self):
        for variant, reason in [('pair-binding', 'run binding'), ('cancelled-id', 'run binding'),
                                ('incomplete-canary', 'incomplete canary scan'),
                                ('peer-read-error', 'canary must fail'), ('planted-read-error', 'failed scan')]:
            evidence = self.evidence()
            if variant == 'pair-binding':
                evidence['pairs'][0]['reference']['run']['id'] = 'wrong'
            elif variant == 'cancelled-id':
                evidence['cancelled_attempts'] = [{'run_id': 'serial-0'}]
            else:
                shards = evidence['canary_without_engine']['shards']
                planted = next(r for r in shards if not r['scan']['ok'])
                if variant == 'incomplete-canary':
                    planted['scan']['completed'] = False
                elif variant == 'peer-read-error':
                    peer = next(r for r in shards if r['scan']['ok'])
                    peer['scan'] = dict(completed=True, ok=False, failures=[{'reason': 'read error'}])
                    peer['ok'] = False
                else:
                    planted['scan']['failures'].append({'reason': 'read error'})
            with self.subTest(variant=variant), self.assertRaisesRegex(ValueError, reason):
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

    def test_instrument_counts_real_verbs_and_emits_both_counters(self):
        desktop = types.SimpleNamespace(processes=lambda: 'discovered')
        validate_module = types.SimpleNamespace(engine_call=lambda verb: verb)
        pbz = types.ModuleType('pbz')
        pbz.desktop, pbz.validate = desktop, validate_module
        with patch.dict(sys.modules, pbz=pbz), patch('ci_timing.atexit.register') as register:
            totals = instrument()
            self.assertEqual(validate_module.engine_call('tmdl-validate'), 'tmdl-validate')
            self.assertEqual(totals['tom']['calls'], 1)
            self.assertEqual(totals['discovery']['calls'], 0)
            self.assertEqual(desktop.processes(), 'discovered')
            self.assertEqual(validate_module.engine_call('other'), 'other')
            self.assertEqual(totals['tom']['calls'], 1)
            self.assertEqual(totals['discovery']['calls'], 1)
            output = io.StringIO()
            with contextlib.redirect_stdout(output): register.call_args.args[0]()
            line = output.getvalue().strip()
            self.assertTrue(line.startswith('PBZ_TIMING '))
            emitted = json.loads(line.removeprefix('PBZ_TIMING '))
            self.assertEqual(emitted['tom']['calls'], 1)
            self.assertEqual(emitted['discovery']['calls'], 1)


if __name__ == '__main__':
    if '--injected-failure' in sys.argv:
        print('PBZ_INJECTED_FAILURE')
        raise SystemExit(91)
    if sys.argv[1:] == ['.']:
        sys.argv.pop()
    unittest.main()
