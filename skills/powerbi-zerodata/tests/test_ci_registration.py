"""Registration, executed-set and shadow gate acceptance."""
from __future__ import annotations
import ast
import hashlib
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
from ci_equivalence import compare, rollout, serial_inventory, inventory
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
        self.assertCountEqual([{k: e[k] for k in ('id', 'argv')} for e in self.entries],
                         inventory())
        self.assertEqual({e['id'] for e in json.loads((BASELINE.parent / 'offline_additions.json').read_text())['suites']},
                         {'checks', 'rename_bindings', 'usage', 'evidence', 'skip_capture', 'rewrite_qualify', 'rewrite_scopes', 'dpapi_seam', 'similar_scope', 'formatstring_roundtrip', 'dax_binding_alignment', 'withheld_files', 'rebase_deletes'})
        self.assertFalse({'checks', 'rename_bindings', 'usage', 'evidence'} &
                         {e['id'] for e in json.loads(BASELINE.read_text())['suites']})
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
                                         ('plan_pure', 'engine', True, 'engine-free'),
                                         ('engine-offline', 'engine', False, 'engine and windows'),
                                         ('engine-offline', 'windows', False, 'engine and windows')]:
            entries = copy.deepcopy(self.entries)
            next(e for e in entries if e['id'] == name)[key] = value
            with self.subTest(name=name, key=key), self.assertRaisesRegex(ValueError, reason):
                validate(entries)
        registration = next(e for e in self.entries if e['id'] == 'ci_registration')
        self.assertFalse(registration['windows'])
        self.assertFalse(registration['engine'])
        self.assertFalse(next(e for e in self.entries if e['id'] == 'plan_pure')['engine'])

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
            self.assertEqual(report['executed'][0]['argv'], ['suite.py'])
            def child(argv, **kwargs):
                if Path(argv[2]).name == 'suite.py':
                    return subprocess.run(argv, **kwargs)
                return subprocess.CompletedProcess(argv, 0)
            fixed = {k: 'fixed' for k in ('python', 'os', 'image', 'dotnet', 'dependencies',
                                         'nuget', 'source', 'project', 'payload')}
            with patch.object(run_offline, 'identity', return_value=fixed), patch.object(run_offline, 'ROOT', root), contextlib.redirect_stdout(io.StringIO()):
                def execute(group, shard):
                    return run_offline.execute(group, shard=shard, output=root / str(shard), run=child,
                                              expected_failures=expected, scanner=lambda roots: {'completed': True, 'ok': True})
                with patch.dict(os.environ, GITHUB_RUN_ID='serial-fixture', GITHUB_RUN_ATTEMPT='1'):
                    reference = execute(entries, 'serial')
                with patch.dict(os.environ, GITHUB_RUN_ID='sharded-fixture', GITHUB_RUN_ATTEMPT='1'):
                    shards = [execute(group, i) for i, group in enumerate(allocate(entries, 2))]
                self.assertTrue(compare(reference, shards, control_path=[record]))
            source = next(row for row in reference['executed'] if row['id'] == 'guard')['source']
            self.assertEqual(source['revision'], revision)
            self.assertEqual(source['path'], 'suite.py')
            self.assertEqual(len(source['sha256']), 64)
            wrong_record = dict(record, sha256='0' * 64)
            agreeing = copy.deepcopy([reference, *shards])
            for report in agreeing:
                for row in report['executed']:
                    if row['id'] == 'guard':
                        row['source']['sha256'] = wrong_record['sha256']
            with self.assertRaisesRegex(ValueError, 'historical content identity mismatch'):
                controls(self.entries, [wrong_record], check_scripts=False)
            with self.assertRaisesRegex(ValueError, 'historical content identity mismatch'):
                compare(agreeing[0], agreeing[1:], control_path=[wrong_record])
            next(row for row in shards[0]['executed'] + shards[1]['executed'] if row['id'] == 'guard')['source']['sha256'] = '0' * 64
            with self.assertRaisesRegex(ValueError, 'source identity'):
                compare(reference, shards, control_path=[record])

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

    def test_default_scanner_roots_and_production_shard_aggregate(self):
        from canary_scan import CAN, redact
        groups = allocate(self.entries, 2)
        planted_shard = next(i for i, group in enumerate(groups)
                             if not any(e['id'] == 'engine-offline' for e in group))
        fixed = {k: 'fixed' for k in ('python', 'os', 'image', 'dotnet', 'dependencies',
                                     'nuget', 'source', 'project', 'payload')}
        def child(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0)
        for kind in (None, 'checkout', 'logs', 'output-root'):
            with self.subTest(root=kind), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                evidence = base / 'evidence'
                evidence.mkdir()
                planted_path = None
                for i, group in enumerate(groups):
                    roots = {name: base / str(i) / name for name in ('checkout', 'logs', 'output-root')}
                    for root in roots.values():
                        root.mkdir(parents=True)
                    failed = kind is not None and i == planted_shard
                    if failed:
                        planted_path = roots[kind] / 'planted.txt'
                        planted_path.write_text(CAN['canary-strings'][0], encoding='utf-8')
                    with patch.object(run_offline, 'ROOT', roots['checkout']), patch.object(run_offline, 'identity', return_value=fixed), patch.dict(os.environ, GITHUB_SHA='fixture', GITHUB_RUN_ID='sharded-fixture', GITHUB_RUN_ATTEMPT='1'), contextlib.redirect_stdout(io.StringIO()):
                        report = run_offline.execute(group, shard=i, output=roots['logs'],
                                                     roots=[roots['output-root']], run=child)
                    self.assertTrue(report['scan']['completed'])
                    self.assertEqual(report['scan']['ok'], not failed)
                    self.assertEqual(report['ok'], not failed)
                    self.assertEqual(report['scan']['failures'],
                                     [{'path': str(planted_path), 'reason': 'canary'}] if failed else [])
                    (evidence / f'shard-{i}.json').write_text(redact(json.dumps(report)), encoding='utf-8')
                aggregate = subprocess.run([sys.executable, str(BASELINE.parent / 'ci_equivalence.py'),
                                            '--shards-only', str(evidence)],
                                           env=dict(os.environ, PBZ_CI_CONTROLS=''), capture_output=True, text=True)
                if kind is None:
                    self.assertEqual(aggregate.returncode, 0, aggregate.stderr)
                    self.assertIn('shard aggregate passed', aggregate.stdout)
                else:
                    self.assertNotEqual(aggregate.returncode, 0)
                    # The synthesized reference inherits the first discovered scan.
                    self.assertRegex(aggregate.stderr,
                                     r'ValueError: (incomplete or failed scan|inconsistent final verdict)')
                    self.assertNotIn('shard aggregate passed', aggregate.stdout)
                    failed_report = json.loads((evidence / f'shard-{planted_shard}.json').read_text())
                    self.assertFalse(failed_report['ok'])
                    self.assertEqual(failed_report['scan']['failures'],
                                     [{'path': str(planted_path), 'reason': 'canary'}])

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

    def test_output_setup_and_scanner_failure_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocked = Path(tmp, 'file')
            blocked.write_text('not a directory')
            scanner = unittest.mock.Mock(return_value={'completed': True, 'ok': True})
            report = run_offline.execute([], output=blocked, scanner=scanner)
            scanner.assert_called_once()
            self.assertFalse(report['ok'])
            self.assertEqual(report['error'], 'FileExistsError')
            def child(argv, **kwargs):
                kwargs['stdout'].write(b'raw private log')
                return subprocess.CompletedProcess(argv, 0)
            with patch.object(run_offline, 'identity', return_value={}), patch.object(run_offline.subprocess, 'check_output', return_value='sha'), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'scanner'):
                    run_offline.execute([self.entries[0]], output=tmp, run=child,
                                        scanner=unittest.mock.Mock(side_effect=RuntimeError('scanner')))
            self.assertEqual(list(Path(tmp).glob('*.log')), [])

    def test_controls_are_workflow_environment_data(self):
        root = Path(__file__).resolve().parents[3]
        for name in ('tests.yml', 'offline-shadow.yml'):
            lines = (root / '.github/workflows' / name).read_text().splitlines()
            for i, line in enumerate(lines):
                if 'run: python' in line and '--rollout' not in line and ('run_offline.py' in line or 'ci_equivalence.py' in line):
                    self.assertNotIn('--controls', line)
                    self.assertNotIn('PBZ_CI_CONTROLS', line)
                    self.assertIn('PBZ_CI_CONTROLS:', lines[i-1])

    def test_staged_checker_isolates_absolute_outer_index(self):
        root = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
            env['GIT_INDEX_FILE'] = str(repo / 'alternate-index')
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=repo, env=env)
            git('init', '-q')
            tests = repo / 'skills/powerbi-zerodata/tests'
            tests.mkdir(parents=True)
            (repo / 'outer.txt').write_text('outer')
            (tests / 'run_offline.py').write_text('pass')
            (tests / 'test_plan_pure.py').write_text('pass')
            (tests / 'test_ci_registration.py').write_text("""import subprocess, tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as tmp:
    def git(*args):
        subprocess.run(['git', *args], cwd=tmp, check=True, capture_output=True)
    git('init', '-q')
    git('config', 'user.name', 'Fixture')
    git('config', 'user.email', 'fixture@example.test')
    Path(tmp, 'suite.py').write_text('pass')
    git('add', '.')
    git('commit', '-qm', 'fixture')
""")
            git('add', 'outer.txt', 'skills')
            before = (repo / 'alternate-index').read_bytes()
            staged = git('ls-files', '--stage')
            result = subprocess.run([sys.executable, str(root / 'scripts/check_staged.py')], cwd=repo,
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((repo / 'alternate-index').read_bytes(), before)
            self.assertEqual(git('ls-files', '--stage'), staged)


class EquivalenceTests(unittest.TestCase):
    def setUp(self):
        import ci_manifest
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        def git(*args):
            return subprocess.check_output(['git', *args], cwd=root)
        git('init', '-q')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.test')
        (root / 'tests').mkdir()
        (root / 'tests/test_guard.py').write_text('raise SystemExit(91)\n')
        git('add', '.')
        git('commit', '-qm', 'historical fixture')
        self.revision = git('rev-parse', 'HEAD').decode().strip()
        self.historical_digest = hashlib.sha256(git('show', f'{self.revision}:tests/test_guard.py')).hexdigest()
        patcher = patch.object(ci_manifest, 'ROOT', root)
        patcher.start()
        self.addCleanup(patcher.stop)

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
                              serial_run=f'serial-{i}', sharded_run=f'sharded-{i}', serial_attempt='1', sharded_attempt='1',
                              serial_seconds=400, sharded_seconds=250,
                              serial_billed_minutes=7, sharded_billed_minutes=9) for i in range(10)],
                **{k: {'verified': True, 'run_id': 'control', 'reason': 'expected specific failure'} for k in
                   ('historical_failure', 'injected_failure', 'canary_without_engine')}}
        for p in result['pairs']:
            p['reference'], p['shards'] = self.reports()
            p['reference']['run'] = {'id': p['serial_run'], 'attempt': '1'}
            for r in p['shards']: r['run'] = {'id': p['sharded_run'], 'attempt': '1'}
            for r in [p['reference'], *p['shards']]: r['cold_cache'] = p['cold_cache']
            for mode in ('serial', 'sharded'):
                p[mode + '_metrics'] = dict(run_id=p[mode + '_run'], attempt='1', sha=p['sha'], conclusion='success',
                                           elapsed_seconds=p[mode + '_seconds'], billed_minutes=p[mode + '_billed_minutes'])
        for key, purpose in (('historical_failure', 'historical'), ('injected_failure', 'injected')):
            ref, shards = self.reports()
            control = dict(id='guard' if purpose == 'historical' else 'injected-control',
                           argv=['tests/test_guard.py'] if purpose == 'historical' else ['tests/test_ci_registration.py', '--injected-failure'],
                           exit_code=91, reason='specific failure reason', purpose=purpose)
            if purpose == 'historical':
                control.update(revision=self.revision, script='tests/test_guard.py', sha256=self.historical_digest)
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
                            row.update(exit_code=91, failure_reason=control['reason'],
                                       source=dict(revision=control['revision'], path=control['script'], sha256=control['sha256']))
            for r in [ref, *shards]:
                r['ok'] = all(e['exit_code'] == 0 for e in r['executed'])
            result[key] = dict(run_id='control-' + purpose, controls=[control], reference=ref, shards=shards,
                               historical_revision=self.revision)
        ref, shards = self.reports()
        planted = next(r for r in shards if not any(e['id'] == 'engine-offline' for e in r['executed']))
        planted['ok'] = False
        planted['scan'] = dict(completed=True, ok=False, failures=[{'reason': 'canary'}])
        result['canary_without_engine'] = dict(run_id='canary-control', aggregate_verdict='failure', reference=ref, shards=shards)
        for key in ('historical_failure', 'injected_failure', 'canary_without_engine'):
            control = result[key]
            control.update(reference_run_id=control['run_id'] + '-reference', reference_attempt='1', attempt='1')
            control['reference']['run'] = dict(id=control['reference_run_id'], attempt='1')
            for shard in control['shards']:
                shard['run'] = dict(id=control['run_id'], attempt='1')
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
            ref['run'] = {'id': 'serial', 'attempt': '1'}
            for shard in shards: shard['run'] = {'id': 'shards', 'attempt': '1'}
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


    def test_unrecorded_manifest_addition_is_rejected(self):
        import ci_equivalence
        ref, shards = self.reports()
        entries = json.loads(MANIFEST.read_text()) + [dict(id='unrecorded', argv=['tests/test_unrecorded.py'])]
        row = dict(entries[-1], exit_code=0, skip_reasons=[], error=None)
        ref['executed'].append(row)
        rows = {r['id']: r for r in ref['executed']}
        for shard, group in zip(shards, allocate(entries, 2)):
            shard['executed'] = [rows[e['id']] for e in group]
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp, 'manifest.json')
            manifest.write_text(json.dumps(entries))
            with patch.object(ci_equivalence, 'MANIFEST', manifest), self.assertRaisesRegex(ValueError, 'membership'):
                compare(ref, shards)

    def test_windows_mcp_skip_inventory_matches_source(self):
        source = ast.parse((BASELINE.parent / 'test_mcp.py').read_text(encoding='utf-8'))
        expected = []
        for cls in (node for node in ast.walk(source) if isinstance(node, ast.ClassDef)):
            for method in cls.body:
                if not isinstance(method, ast.FunctionDef) or not method.name.startswith('test_'):
                    continue
                for decorator in method.decorator_list:
                    if (isinstance(decorator, ast.Call) and
                            ast.unparse(decorator.func) == 'unittest.skipIf' and
                            len(decorator.args) == 2 and
                            ast.unparse(decorator.args[0]) == "os.name == 'nt'"):
                        expected.append('PBZ_SKIP ' + json.dumps({
                            'test': f'__main__.{cls.name}.{method.name}',
                            'reason': ast.literal_eval(decorator.args[1])}))
        declared = json.loads((BASELINE.parent / 'offline_skips.json').read_text())['windows']['mcp']
        self.assertEqual(sorted(declared), sorted(expected))

    def test_run_identity_canonicalization(self):
        from ci_equivalence import run_id
        for value in (123, '123', '0123', '123 ', ' 00123 '):
            with self.subTest(value=value):
                self.assertEqual(run_id(value), '123')
        self.assertEqual(run_id(' canary-control '), 'canary-control')
        for value in ('', '   ', None, True):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'invalid run identity'):
                run_id(value)

    def test_platform_scoped_mcp_skips(self):
        allowed = json.loads((BASELINE.parent / 'offline_skips.json').read_text())['windows']['mcp']
        self.assertEqual(len(allowed), 7)
        for platform, extra, passes in [('Windows-2025', False, True), ('Windows-2025', True, False), ('Linux', False, False)]:
            ref, shards = self.reports()
            for report in [ref, *shards]:
                report['identity']['os'] = platform
                for row in report['executed']:
                    if row['id'] == 'mcp':
                        row['skip_reasons'] = allowed + (['undeclared'] if extra else [])
            if passes:
                self.assertTrue(compare(ref, shards))
            else:
                with self.assertRaisesRegex(ValueError, 'unexpected skip'):
                    compare(ref, shards)

    def test_rollout_requires_recorded_historical_revision(self):
        for revision in (None, '0' * 40):
            evidence = self.evidence()
            record = evidence['historical_failure']['controls'][0]
            if revision is None:
                for key in ('revision', 'script', 'sha256'):
                    record.pop(key)
            else:
                record['revision'] = revision
            with self.assertRaisesRegex(ValueError, 'revision record'):
                rollout(evidence)

    def test_canonical_run_attempt_and_control_bindings(self):
        for variant in ('mixed-duplicate', 'leading-zero-duplicate', 'whitespace-duplicate', 'shard-attempt', 'metric-attempt', 'missing-attempt',
                        'control-unbound', 'control-mismatch', 'control-attempt', 'control-cancelled', 'control-reused', 'canary-unbound'):
            evidence = self.evidence()
            pair = evidence['pairs'][0]
            if variant in ('mixed-duplicate', 'leading-zero-duplicate', 'whitespace-duplicate'):
                second_id = {'mixed-duplicate': '123', 'leading-zero-duplicate': '0123',
                             'whitespace-duplicate': '123 '}[variant]
                for duplicate, identity in zip(evidence['pairs'][:2], (123, second_id)):
                    duplicate['serial_run'] = identity
                    duplicate['reference']['run']['id'] = identity
                    duplicate['serial_metrics']['run_id'] = identity
            elif variant == 'shard-attempt':
                pair['shards'][0]['run']['attempt'] = '2'
            elif variant == 'metric-attempt':
                pair['serial_metrics']['attempt'] = '2'
            elif variant == 'missing-attempt':
                pair.pop('serial_attempt')
            elif variant == 'control-unbound':
                evidence['historical_failure']['reference'].pop('run')
            elif variant == 'control-mismatch':
                evidence['injected_failure']['run_id'] = 'wrong'
            elif variant == 'control-cancelled':
                evidence['cancelled_attempts'] = [evidence['injected_failure']['run_id']]
            elif variant == 'control-reused':
                evidence['injected_failure']['run_id'] = evidence['historical_failure']['run_id']
            elif variant == 'control-attempt':
                evidence['injected_failure']['shards'][0]['run']['attempt'] = '2'
            else:
                evidence['canary_without_engine']['run_id'] = 'wrong'
            reason = 'hosted runs reused across pairs' if variant.endswith('duplicate') else 'run binding|runs reused|metric identity'
            with self.subTest(variant=variant), self.assertRaisesRegex(ValueError, reason):
                rollout(evidence)

    def test_hosted_metrics_include_setup_and_final_aggregate(self):
        run = dict(id='run', run_attempt=2, head_sha='same', status='completed', conclusion='success', created_at='2026-11-19T00:00:00Z')
        jobs = [dict(status='completed', conclusion='success', started_at='2026-11-19T00:00:10Z', completed_at='2026-11-19T00:01:11Z'),
                dict(status='completed', conclusion='success', started_at='2026-11-19T00:01:20Z', completed_at='2026-11-19T00:01:30Z')]
        result = metrics(run, jobs, 3)
        self.assertEqual(result['attempt'], 2)
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
