"""Structured skip capture and approved Windows skip inventory regressions."""
import os
import sys

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, 'tests'))

import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import run_offline
from ci_equivalence import compare, serial_inventory, inventory
from ci_manifest import MANIFEST, allocate


APPROVED_WINDOWS = {
    'doctor_ci': [
        'PBZ_SKIP ' + json.dumps({'test': test, 'reason': reason})
        for test, reason in [
            ('__main__.DoctorCiTests.test_doctor_ci_skip_unreadable_evidence_dir_is_not_a_match',
             'needs a non-root POSIX process so directory read permission is enforced'),
        ]
    ],
    'mcp': [
        'PBZ_SKIP ' + json.dumps({'test': test, 'reason': reason})
        for test, reason in [
            ('__main__.GitHookTests.test_chain_bytes_args_stdin_and_status',
             'direct shell hook execution is POSIX-only'),
            ('__main__.GitHookTests.test_non_executable_host_is_ignored',
             'executable mode semantics require POSIX'),
            ('__main__.InstallerTransactionTests.test_atomic_text_preserves_mode_and_symlink',
             'POSIX modes and symlink'),
            ('__main__.InstallerTransactionTests.test_posix_lock_timeout_is_named_conflict',
             'POSIX flock'),
            ('__main__.InstallerTransactionTests.test_absent_noop_holder_removes_parents_before_waiter_retries',
             'POSIX flock'),
            ('__main__.InstallerTransactionTests.test_absent_noop_lock_unlinks_before_unlocking',
             'POSIX flock and unlink of open file'),
            ('__main__.InstallerTransactionTests.test_absent_noop_lock_waiter_retries_replaced_inode',
             'POSIX flock and unlink of open file'),
        ]
    ],
    'mirror_recovery_honesty': [
        'PBZ_SKIP ' + json.dumps({'test': test, 'reason': reason})
        for test, reason in [
            ('__main__.MirrorEditJournalTests.test_recovery_guard_excludes_concurrent_recovery',
             'fcntl recovery guard seam'),
            ('__main__.RecoveryHonestyTests.test_open_editor_save_during_successful_quarantine_return_is_preserved',
             'open editor handle follows renamed inode'),
        ]
    ],
    'pbz_state_atomic': [
        'PBZ_SKIP ' + json.dumps({'test': test, 'reason': reason})
        for test, reason in [
            ('__main__.StateAtomicTests.test_allocator_path_swapped_during_acquisition_refused',
             'POSIX flock only'),
            ('__main__.StateAtomicTests.test_allocator_posix_deadline',
             'POSIX flock only'),
            ('__main__.StateAtomicTests.test_allocator_replaced_path_refused',
             'cannot rename an open lock on Windows'),
        ]
    ],
}


def _reports(skip_map):
    entries = json.loads(MANIFEST.read_text())
    rows = [dict(entry, exit_code=0,
                 skip_reasons=list(skip_map.get(entry['id'], [])), error=None)
            for entry in entries]
    base = {
        'sha': 'same',
        'identity': {k: 'fixed' for k in
                     ('python', 'os', 'image', 'dotnet', 'dependencies',
                      'nuget', 'source', 'project', 'payload')},
        'ok': True,
        'scan': {'completed': True, 'ok': True},
    }
    base['identity']['os'] = 'Windows-2025'
    return dict(copy.deepcopy(base), shard='serial', executed=copy.deepcopy(rows)), [
        dict(copy.deepcopy(base), shard=i, executed=copy.deepcopy([
            next(row for row in rows if row['id'] == entry['id']) for entry in group
        ])) for i, group in enumerate(allocate(entries, 2))
    ]


def _serial_report(skip_map):
    reference, _ = _reports(skip_map)
    return reference


class SkipCaptureTests(unittest.TestCase):
    def test_sidecar_records_skip_from_real_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(run_offline, 'identity', return_value={}), patch.object(run_offline.subprocess, 'check_output', return_value='fixture'), contextlib.redirect_stdout(io.StringIO()):
            path = Path(tmp, 'suite.py')
            path.write_text(
                "import unittest\n"
                "class SkipFixtureTests(unittest.TestCase):\n"
                " def test_a_pass(self): pass\n"
                " def test_b_pass(self): pass\n"
                " def test_c_skip(self): self.skipTest('injected reason')\n"
                "unittest.main()\n", encoding='utf-8')
            report = run_offline.execute(
                [{'id': 'control', 'argv': [str(path)]}], output=tmp,
                scanner=lambda roots: {'completed': True, 'ok': True})
        row = report['executed'][0]
        self.assertEqual(row['exit_code'], 0)
        self.assertIsNone(row['error'])
        self.assertEqual(len(row['skip_reasons']), 1)
        entry = row['skip_reasons'][0]
        self.assertTrue(entry.startswith('PBZ_SKIP '))
        self.assertEqual(json.loads(entry[len('PBZ_SKIP '):]), {
            'test': '__main__.SkipFixtureTests.test_c_skip',
            'reason': 'injected reason',
        })

    def test_missing_sidecar_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(run_offline, 'identity', return_value={}), patch.object(run_offline.subprocess, 'check_output', return_value='fixture'), contextlib.redirect_stdout(io.StringIO()):
            path = Path(tmp, 'suite.py')
            path.write_text(
                "import os\n"
                "import unittest\n"
                "os.unlink(os.environ['PBZ_SKIP_SIDECAR'])\n"
                "class PassFixtureTests(unittest.TestCase):\n"
                " def test_pass(self): pass\n"
                "unittest.main()\n", encoding='utf-8')
            report = run_offline.execute(
                [{'id': 'control', 'argv': [str(path)]}], output=tmp,
                scanner=lambda roots: {'completed': True, 'ok': True})
        self.assertEqual(report['executed'][0]['exit_code'], 0)
        self.assertEqual(report['executed'][0]['error'], 'skip sidecar missing')

    def test_malformed_sidecar_record_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(run_offline, 'identity', return_value={}), patch.object(run_offline.subprocess, 'check_output', return_value='fixture'), contextlib.redirect_stdout(io.StringIO()):
            path = Path(tmp, 'suite.py')
            path.write_text(
                "import os\n"
                "import unittest\n"
                "with open(os.environ['PBZ_SKIP_SIDECAR'], 'a') as stream:\n"
                " stream.write('not json\\n')\n"
                "class PassFixtureTests(unittest.TestCase):\n"
                " def test_pass(self): pass\n"
                "unittest.main()\n", encoding='utf-8')
            report = run_offline.execute(
                [{'id': 'control', 'argv': [str(path)]}], output=tmp,
                scanner=lambda roots: {'completed': True, 'ok': True})
        self.assertEqual(report['executed'][0]['exit_code'], 0)
        self.assertEqual(report['executed'][0]['error'], 'invalid skip sidecar record')

    def test_wrong_suite_sidecar_record_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(run_offline, 'identity', return_value={}), patch.object(run_offline.subprocess, 'check_output', return_value='fixture'), contextlib.redirect_stdout(io.StringIO()):
            path = Path(tmp, 'suite.py')
            path.write_text(
                "import json\n"
                "import os\n"
                "import unittest\n"
                "with open(os.environ['PBZ_SKIP_SIDECAR'], 'a') as stream:\n"
                " stream.write(json.dumps({'suite': 'other', 'test': 'x', 'reason': 'y'}) + '\\n')\n"
                "class PassFixtureTests(unittest.TestCase):\n"
                " def test_pass(self): pass\n"
                "unittest.main()\n", encoding='utf-8')
            report = run_offline.execute(
                [{'id': 'control', 'argv': [str(path)]}], output=tmp,
                scanner=lambda roots: {'completed': True, 'ok': True})
        self.assertEqual(report['executed'][0]['exit_code'], 0)
        self.assertEqual(report['executed'][0]['error'], 'invalid skip sidecar record')

    def test_compare_rejects_unapproved_skip(self):
        reference, shards = _reports({'mcp': APPROVED_WINDOWS['mcp'] + [
            'PBZ_SKIP ' + json.dumps({
                'test': '__main__.GitHookTests.test_unapproved',
                'reason': 'unapproved',
            })]})
        with self.assertRaisesRegex(ValueError, 'mcp: unexpected skip'):
            compare(reference, shards)

    def test_relative_output_sidecar_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'suite.py')
            path.write_text(
                "import unittest\n"
                "class PassFixtureTests(unittest.TestCase):\n"
                " def test_pass(self): pass\n"
                "unittest.main()\n", encoding='utf-8')
            previous = os.getcwd()
            os.chdir(tmp)
            try:
                with patch.object(run_offline, 'identity', return_value={}), patch.object(run_offline.subprocess, 'check_output', return_value='fixture'), contextlib.redirect_stdout(io.StringIO()):
                    report = run_offline.execute(
                        [{'id': 'control', 'argv': [str(path)]}], output=Path('out'),
                        scanner=lambda roots: {'completed': True, 'ok': True})
            finally:
                os.chdir(previous)
            row = report['executed'][0]
            self.assertIsNone(row['error'])
            self.assertEqual(row['skip_reasons'], [])
            self.assertTrue(Path(tmp, 'out', 'control.skips.jsonl').exists())

    def test_compare_rejects_error_on_expected_failure(self):
        reference, shards = _reports({})
        for report in [reference, *shards]:
            for row in report['executed']:
                if row['id'] == 'guard':
                    row['exit_code'] = 7
                    row['failure_reason'] = 'planted guard marker'
                    row['error'] = 'skip sidecar missing'
                    report['ok'] = False
        with self.assertRaisesRegex(ValueError, 'guard: unexpected error'):
            compare(reference, shards, expected_failures={'guard': (7, 'planted guard marker')})

    def test_missing_or_malformed_skip_reasons_fail_closed(self):
        for variant, value in [('missing', None), ('null', None), ('string', ''),
                               ('mapping', {}), ('non_string_entry', [None])]:
            for gate in ('compare', 'serial_inventory'):
                with self.subTest(variant=variant, gate=gate):
                    reference, shards = _reports(APPROVED_WINDOWS)
                    for report in [reference, *shards]:
                        row = next((row for row in report['executed'] if row['id'] == 'guard'), None)
                        if row is not None:
                            if variant == 'missing':
                                del row['skip_reasons']
                            else:
                                row['skip_reasons'] = value
                    message = 'missing' if variant == 'missing' else 'invalid'
                    with self.assertRaisesRegex(ValueError, 'guard: ' + message + ' skip_reasons'):
                        if gate == 'compare':
                            compare(reference, shards)
                        else:
                            serial_inventory(reference)

    def test_compare_ignores_skip_order(self):
        ref_skips = {'mcp': APPROVED_WINDOWS['mcp']}
        shard_skips = {'mcp': list(reversed(APPROVED_WINDOWS['mcp']))}
        reference, _ = _reports(ref_skips)
        _, shards = _reports(shard_skips)
        self.assertTrue(compare(reference, shards))

    def test_serial_inventory_enforces_approved_skips(self):
        report = _serial_report(APPROVED_WINDOWS)
        self.assertTrue(serial_inventory(report))
        extra = copy.deepcopy(report)
        next(row for row in extra['executed'] if row['id'] == 'pbz_state_atomic')['skip_reasons'].append(
            'PBZ_SKIP ' + json.dumps({
                'test': '__main__.StateAtomicTests.test_unapproved_extra',
                'reason': 'unapproved reason',
            }))
        missing = copy.deepcopy(report)
        next(row for row in missing['executed'] if row['id'] == 'mcp')['skip_reasons'].pop()
        for variant, changed in [('extra', extra), ('missing', missing)]:
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                serial_inventory(changed)

    def test_declared_skips_match_approved_inventory(self):
        declared = json.loads((MANIFEST.parent / 'offline_skips.json').read_text())['windows']
        self.assertEqual(
            {suite: sorted(skips) for suite, skips in declared.items()},
            {suite: sorted(skips) for suite, skips in APPROVED_WINDOWS.items()})

    def test_bare_skip_line_still_captured(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(run_offline, 'identity', return_value={}), patch.object(run_offline.subprocess, 'check_output', return_value='fixture'), contextlib.redirect_stdout(io.StringIO()):
            path = Path(tmp, 'suite.py')
            path.write_text("print('SKIP creating a junction is unavailable')\n", encoding='utf-8')
            report = run_offline.execute(
                [{'id': 'control', 'argv': [str(path)]}], output=tmp,
                scanner=lambda roots: {'completed': True, 'ok': True})
        self.assertIn('SKIP creating a junction is unavailable', report['executed'][0]['skip_reasons'])

    def test_stale_sidecar_does_not_leak(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(run_offline, 'identity', return_value={}), patch.object(run_offline.subprocess, 'check_output', return_value='fixture'), contextlib.redirect_stdout(io.StringIO()):
            Path(tmp, 'control.skips.jsonl').write_text(
                json.dumps({'suite': 'control', 'test': 'x', 'reason': 'stale'}) + '\n',
                encoding='utf-8')
            path = Path(tmp, 'suite.py')
            path.write_text(
                "import unittest\n"
                "class PassFixtureTests(unittest.TestCase):\n"
                " def test_pass(self): pass\n"
                "unittest.main()\n", encoding='utf-8')
            report = run_offline.execute(
                [{'id': 'control', 'argv': [str(path)]}], output=tmp,
                scanner=lambda roots: {'completed': True, 'ok': True})
        self.assertEqual(report['executed'][0]['skip_reasons'], [])


if __name__ == '__main__':
    unittest.main()
