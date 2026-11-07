"""Synthetic canary, preflight, check-schema and transaction fault regressions.

No Desktop, real model, private user store or company data is accessed.
Run: python tests/test_improvement_core.py
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))
from pbz import checks, classify, common, dpapi, images, mirror, tmdl, tokens, validate  # noqa: E402


def memory_tokens():
    result = object.__new__(tokens.TokenMap)
    result.data = {"version": 1, "next": {}, "tokens": {}, "keys": {}}
    result.dirty = False
    return result


class PrivacyTests(unittest.TestCase):
    def test_embedded_small_numbers_are_data_and_roundtrip(self):
        source = ('table Metrics\n\tcolumn Hours\n\t\tdataType: double\n'
                  '\tpartition Metrics = calculated\n\t\tmode: import\n'
                  '\t\tsource = DATATABLE("Hours", DOUBLE, {{0}, {7}, {42}, {314}, {1200}, {-9}, {3.5}})\n')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'tables').mkdir()
            (root / 'tables/Metrics.tmdl').write_text(source, encoding='utf-8')
            model = tmdl.load_model(temp)
            for policy in ({}, {"literals": "strict"}):
                tm = memory_tokens()
                result = tokens.tokenize_tmdl(source, 'tables/Metrics.tmdl', model,
                                              classify.Classifier(model, None, policy), tm)
                for cell in ('{0}', '{7}', '{42}', '{314}', '{1200}', '{-9}', '{3.5}'):
                    self.assertNotIn(cell, result)
                restored, unknown = tokens.detokenize_tmdl(result, tm)
                self.assertEqual((restored, unknown), (source, []))

    def test_long_indexed_title_and_small_comment_counts(self):
        name = 'synthetic zephyr atlas migration rollout alpha'
        clf = classify.Classifier(tmdl.Model(), None, {'literals': 'strict'})
        clf.mode, clf.key = 'index', b'synthetic-test-key'
        clf.hmac_to_col[dpapi.hmac_hex(clf.key, name)] = 'Project[Title]'
        clf.col_kind['Project[Title]'] = 'title'
        source = '/// Snapshot has 7 projects and 42 rows: ' + name + '\ntable Project\n'
        tm = memory_tokens()
        result = tokens.tokenize_tmdl(source, 'tables/Project.tmdl', tmdl.Model(), clf, tm)
        self.assertNotIn(name, result)
        self.assertNotIn('7 projects', result)
        self.assertNotIn('42 rows', result)
        self.assertEqual(tokens.detokenize_tmdl(result, tm), (source, []))
        self.assertEqual(clf.free_text('unmatched ' * 300), [(0, len('unmatched ' * 300), 'lit')])

    def test_authored_dax_constants_are_not_embedded_data(self):
        source = 'table Metrics\n\tmeasure Threshold = IF(1 < 2, 7, 42)\n'
        model = tmdl.Model()
        result = tokens.tokenize_tmdl(source, 'tables/Metrics.tmdl', model,
                                      classify.Classifier(model, None, {}), memory_tokens())
        self.assertEqual(source, result)


class SpecTests(unittest.TestCase):
    def test_malformed_specs_reject_before_private_access(self):
        invalid = [None, {}, {'checks': 'secret-invalid'}, {'checks': [None]},
                   {'checks': [{'kind': 'subset', 'column': 'T[C]', 'of': ['private-value']}]},
                   {'checks': [{'kind': 'equal', 'left': '[A]'}]},
                   {'checks': [{'kind': 'additive', 'expr': '[A]', 'by': []}]},
                   {'checks': [{'id': 'same', 'kind': 'nonblank', 'expr': '[A]'},
                               {'id': 'same', 'kind': 'nonblank', 'expr': '[B]'}]},
                   {'checks': [{'kind': 'equal', 'left': '[A]', 'right': '1', 'tol': float('nan')}]},
                   {'checks': [{'kind': 'equal', 'left': '[A]', 'right': '1', 'by': [7]}]}]
        with patch.object(checks.policy_mod, 'load', side_effect=AssertionError('private access')):
            for spec in invalid:
                result = checks.run_checks(None, SimpleNamespace(name='Synthetic'), spec)
                self.assertFalse(result['spec_valid'])
                self.assertTrue(all(r['class'] == 'BAD_REQUEST' for r in result['results']))
                self.assertNotIn('private-value', json.dumps(result))

    def test_valid_shapes_and_schema(self):
        self.assertTrue(checks.validate_spec({'checks': [
            {'kind': 'subset', 'column': "'T'[Key]", 'of': "'U'[Key]"},
            {'kind': 'equal', 'left': '[A]', 'right': '[B]', 'by': ['Date[Month]'], 'tol': 1e-9},
            {'kind': 'sign', 'expr': '[A]', 'expect': ['ZERO', 'BLANK']},
        ]})['valid'])
        self.assertEqual(checks.check_schema()['properties']['checks']['type'], 'array')


class ValidatorTests(unittest.TestCase):
    def test_short_private_values_are_scrubbed_from_diagnostics(self):
        tm = memory_tokens()
        tm.data['tokens'] = {'«num#1»': {'value': 7}, '«lit#1»': {'value': 'ZX'}, '«num#2»': {'value': 1}}
        scrub = validate.retokenizer(tm)
        self.assertEqual(scrub('cell 7 title ZX; field A7'), 'cell «num#1» title «lit#1»; field A7')

    def run_validator(self, process=None, error=None):
        with patch.object(validate.os.path, 'isdir', return_value=True), \
             patch.object(validate.os.path, 'exists', return_value=True), \
             patch.object(validate.subprocess, 'run', return_value=process, side_effect=error):
            return validate.report_author('synthetic-stage', SimpleNamespace(name='Synthetic'), lambda s: s)

    def test_nonzero_empty_response_is_error(self):
        findings = self.run_validator(SimpleNamespace(returncode=1, stdout=b'{}'))
        self.assertTrue(any(f['severity'] == 'error' for f in findings))

    def test_zero_empty_response_is_not_a_validation_pass(self):
        findings = self.run_validator(SimpleNamespace(returncode=0, stdout=b'{}'))
        self.assertTrue(any(f['code'] == 'REPORT_VALIDATOR_RESPONSE' for f in findings))

    def test_warning_without_success_envelope_is_not_a_pass(self):
        findings = self.run_validator(SimpleNamespace(returncode=0, stdout=b'{"diagnostics":[{"severity":"warning","message":"synthetic"}]}'))
        self.assertTrue(any(f['code'] == 'REPORT_VALIDATOR_RESPONSE' for f in findings))
        self.assertFalse(any(f['code'] == 'REPORT_SCHEMA_PASSED' for f in findings))

    def test_timeout_is_error(self):
        findings = self.run_validator(error=subprocess.TimeoutExpired('synthetic', 1))
        self.assertTrue(any(f['severity'] == 'error' for f in findings))

    def test_success_records_schema_coverage(self):
        findings = self.run_validator(SimpleNamespace(returncode=0, stdout=b'{"data":{"result":"succeeded"}}'))
        self.assertTrue(any(f['code'] == 'REPORT_SCHEMA_PASSED' for f in findings))


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ws = SimpleNamespace(root=str(self.root), mirror=str(self.root / 'pbz-workspace'),
                                  private=str(self.root / 'private'), pbz_dir=str(self.root / '.pbz'))
        self.project = SimpleNamespace(name='Synthetic', model_dir=str(self.root / 'Synthetic.SemanticModel'),
            report_dir=str(self.root / 'Synthetic.Report'), pbip=str(self.root / 'Synthetic.pbip'),
            mirror_model=str(Path(self.ws.mirror) / 'Synthetic.SemanticModel'),
            mirror_report=str(Path(self.ws.mirror) / 'Synthetic.Report'))
        self.paths = ['Synthetic.Report/definition/a.json', 'Synthetic.Report/definition/b.json']
        manifest = {'files': {}}
        for rel in self.paths:
            for p, text in ((self.root / rel, '{"old":true}'),
                            (Path(self.ws.mirror) / rel, '{"new":true}'),
                            (Path(mirror.base_dir(self.ws, self.project)) / rel, '{"old":true}')):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding='utf-8')
            digest = common.file_sha(str(self.root / rel))
            manifest['files'][rel] = {'real': digest, 'mirror': digest}
        mirror.save_manifest(self.ws, self.project, manifest)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(mirror.desktop, 'find_instance', return_value=None))
        self.stage_validation = self.stack.enter_context(patch.object(mirror.validate, 'run_stage', return_value=[]))
        self.stack.enter_context(patch.object(checks, 'record_authored', return_value=0))
        self.stack.enter_context(patch.object(mirror, '_object_summary', return_value=['synthetic object']))

    def test_partial_commit_rolls_back_without_losing_mirror_edits(self):
        original = mirror.write_text
        def fail(path, text, bom=b''):
            if Path(path) == self.root / self.paths[1]:
                raise OSError('synthetic write fault')
            return original(path, text, bom)
        with patch.object(mirror, 'write_text', side_effect=fail):
            out = mirror.push(self.ws, self.project)
        self.assertEqual(out['result'], 'ROLLED_BACK')
        for rel in self.paths:
            self.assertEqual((self.root / rel).read_text(), '{"old":true}')
            self.assertEqual((Path(self.ws.mirror) / rel).read_text(), '{"new":true}')
        self.assertFalse(mirror.pending_recovery(self.ws, self.project))

    def test_manifest_failure_restores_project_and_sync_base(self):
        with patch.object(mirror, 'save_manifest', side_effect=OSError('synthetic manifest fault')):
            out = mirror.push(self.ws, self.project)
        self.assertEqual(out['result'], 'ROLLED_BACK')
        for rel in self.paths:
            self.assertEqual((self.root / rel).read_text(), '{"old":true}')
            self.assertEqual((Path(mirror.base_dir(self.ws, self.project)) / rel).read_text(), '{"old":true}')

    def test_preflight_while_desktop_dirty_never_mutates_project(self):
        with patch.object(mirror.desktop, 'find_instance', return_value={'unsaved': True}):
            out = mirror.preflight(self.ws, self.project)
        self.assertEqual(out['result'], 'PREFLIGHT_OK')
        self.stage_validation.assert_called_once()
        self.assertEqual((self.root / self.paths[0]).read_text(), '{"old":true}')
        self.assertFalse(mirror.pending_recovery(self.ws, self.project))

    def test_interrupted_commit_recovery_and_external_conflict(self):
        rel = self.paths[0]
        after = common.sha('{"new":true}')
        directory, journal = mirror._begin_transaction(self.ws, self.project, 'synthetic-recovery', [('workspace', rel, after)])
        (self.root / rel).write_text('{"new":true}', encoding='utf-8')
        self.assertEqual(mirror.push(self.ws, self.project)['result'], 'RECOVERY_REQUIRED')
        self.assertEqual(mirror.recover(self.ws, self.project)['result'], 'RECOVERED')
        self.assertEqual((self.root / rel).read_text(), '{"old":true}')
        directory, journal = mirror._begin_transaction(self.ws, self.project, 'synthetic-conflict', [('workspace', rel, after)])
        (self.root / rel).write_text('{"external":true}', encoding='utf-8')
        result = mirror.recover(self.ws, self.project)
        self.assertEqual(result['result'], 'RECOVERY_REQUIRED')
        self.assertEqual((self.root / rel).read_text(), '{"external":true}')

    def test_success_and_repeated_push_noop(self):
        self.assertEqual(mirror.push(self.ws, self.project)['result'], 'PUSHED')
        self.assertEqual(mirror.push(self.ws, self.project)['result'], 'NOTHING_TO_PUSH')
        self.assertFalse(mirror.pending_recovery(self.ws, self.project))

    def test_added_deleted_files_restore_together(self):
        extra = Path(self.ws.mirror) / 'Synthetic.Report/definition/c.json'
        extra.write_text('{"added":true}', encoding='utf-8')
        (Path(self.ws.mirror) / self.paths[1]).unlink()
        with patch.object(mirror, 'save_manifest', side_effect=OSError('synthetic manifest fault')):
            result = mirror.push(self.ws, self.project)
        self.assertEqual(result['result'], 'ROLLED_BACK')
        self.assertFalse((self.root / 'Synthetic.Report/definition/c.json').exists())
        self.assertEqual((self.root / self.paths[1]).read_text(), '{"old":true}')
        self.assertTrue(extra.exists())

    def test_resource_and_registration_roll_back_pending_asset_survives(self):
        item = 'Synthetic000000000000001.png'
        rel = 'Synthetic.Report/definition/report.json'
        real, proposed = self.root / rel, Path(self.ws.mirror) / rel
        real.write_text('{}', encoding='utf-8')
        proposed.write_text(json.dumps({'resourcePackages': [{'name': 'RegisteredResources', 'type': 'RegisteredResources',
            'items': [{'name': item, 'path': item, 'type': 'Image'}]}]}), encoding='utf-8')
        baseline = Path(mirror.base_dir(self.ws, self.project)) / rel
        baseline.write_text('{}', encoding='utf-8')
        manifest = mirror.load_manifest(self.ws, self.project)
        manifest['files'][rel] = {'real': common.file_sha(str(real)), 'mirror': common.file_sha(str(real))}
        mirror.save_manifest(self.ws, self.project, manifest)
        pending = Path(images._pending_dir(self.ws, self.project)) / item
        pending.parent.mkdir(parents=True)
        pending.write_bytes(b'synthetic-image-fixture')
        with patch.object(mirror, 'save_manifest', side_effect=OSError('synthetic manifest fault')):
            result = mirror.push(self.ws, self.project)
        self.assertEqual(result['result'], 'ROLLED_BACK')
        self.assertEqual(real.read_text(), '{}')
        self.assertFalse((self.root / 'Synthetic.Report/StaticResources/RegisteredResources' / item).exists())
        self.assertTrue(pending.exists(), 'Staged image was destroyed before a successful commit')

    def test_stale_commit_lock_requires_explicit_recovery(self):
        directory = Path(mirror._transaction_dir(self.ws, self.project))
        directory.mkdir(parents=True)
        lock = directory / '.commit-lock'
        lock.write_text('123456789', encoding='utf-8')
        self.assertEqual(mirror.push(self.ws, self.project)['result'], 'COMMIT_BUSY')
        with patch.object(mirror, '_process_alive', return_value=False):
            self.assertEqual(mirror.recover(self.ws, self.project)['result'], 'RECOVERED')
        self.assertFalse(lock.exists())

    def test_path_escape_rejected(self):
        with self.assertRaises(common.PbzError):
            mirror._transaction_path(self.ws, self.project, {'scope': 'workspace', 'path': '../outside'})


if __name__ == '__main__':
    unittest.main()
