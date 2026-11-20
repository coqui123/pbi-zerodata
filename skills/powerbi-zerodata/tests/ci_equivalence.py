"""Fail-closed comparison against the independent pre-migration inventory."""
import argparse
import os
import copy
import re
from collections import Counter
import json
from pathlib import Path
import statistics
from ci_manifest import BASELINE, controls, allocate, MANIFEST


def inventory():
    frozen = json.loads(BASELINE.read_text(encoding='utf-8'))['suites']
    additions = json.loads((BASELINE.parent / 'offline_additions.json').read_text(encoding='utf-8'))['suites']
    rows = frozen + additions
    if len({e['id'] for e in rows}) != len(rows):
        raise ValueError('duplicate baseline/addition suite')
    return rows


def run_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise ValueError('run binding: invalid run identity')
    value = str(value).strip()
    return str(int(value)) if value.isdigit() else value


def attempt(value):
    if isinstance(value, bool) or not re.fullmatch(r'[1-9][0-9]*', str(value)):
        raise ValueError('run binding: explicit attempt required')
    return str(value)


def bind_reports(reference, shards, serial_run, sharded_run, serial_attempt, sharded_attempt):
    for report, intended_run, intended_attempt in [(reference, serial_run, serial_attempt),
            *((r, sharded_run, sharded_attempt) for r in shards)]:
        binding = report.get('run', {})
        if (run_id(binding.get('id')) != run_id(intended_run) or
                attempt(binding.get('attempt')) != attempt(intended_attempt)):
            raise ValueError('run binding: report run/attempt mismatch')


def serial_inventory(report):
    """Normal serial CI checks coverage without inventing a historical skip baseline."""
    expected = {e['id']: e['argv'] for e in inventory()}
    rows = report['executed']
    if Counter(e['id'] for e in rows) != Counter(expected.keys()):
        raise ValueError('serial executed membership/multiplicity')
    if any(e['argv'] != expected[e['id']] for e in rows):
        raise ValueError('serial executed argv')
    if not report.get('ok') or not report.get('scan', {}).get('completed') or not report['scan'].get('ok'):
        raise ValueError('serial verdict or scan failed')
    if any(e['exit_code'] != 0 or e.get('error') for e in rows):
        raise ValueError('serial suite failed')
    return True


def compare(reference, shards, expected_shards=2, expected_failures=None, control_path=None):
    declared, configured_failures = controls(inventory(), control_path, check_scripts=False)
    expected_failures = expected_failures or configured_failures
    expected = {e['id']: e['argv'] for e in declared}
    sources = {e['id']: e.get('source') for e in declared}
    if len(shards) != expected_shards or Counter(str(s['shard']) for s in shards) != Counter(map(str, range(expected_shards))):
        raise ValueError('missing, duplicate or cancelled shards')
    if reference.get('run') and any(s.get('run') for s in shards):
        if not all(s.get('run') for s in shards):
            raise ValueError('run binding: missing shard binding')
        shard_ids = {run_id(s['run']['id']) for s in shards}
        if len({attempt(s['run'].get('attempt')) for s in shards}) != 1:
            raise ValueError('run binding: mixed shard attempts')
        if len(shard_ids) != 1 or run_id(reference['run']['id']) in shard_ids:
            raise ValueError('run binding: reference and shards must be independent runs')
    allocation, _ = controls(json.loads(MANIFEST.read_text(encoding='utf-8')), control_path, check_scripts=False)
    groups = allocate(allocation, expected_shards)
    for shard in shards:
        if Counter(e['id'] for e in shard['executed']) != Counter(e['id'] for e in groups[int(shard['shard'])]):
            raise ValueError('allocation: executed IDs differ from assigned shard')
    all_reports = [reference, *shards]
    for r in all_reports:
        if not r.get('scan', {}).get('completed') or not r['scan'].get('ok'):
            raise ValueError('incomplete or failed scan')
        if r['sha'] != reference['sha']:
            raise ValueError('different SHAs')
        for key in ('python', 'os', 'image', 'dotnet', 'dependencies', 'nuget', 'source', 'project'):
            value = r.get('identity', {}).get(key)
            if not value or value == 'unavailable' or value != reference['identity'].get(key):
                raise ValueError('engine/environment identity: ' + key)
        if not r['identity'].get('payload'):
            raise ValueError('missing engine payload identity')
        verdict = all(e['exit_code'] == 0 and not e.get('error') for e in r['executed'])
        if r.get('ok') != verdict:
            raise ValueError('inconsistent final verdict')
    def rows(reports):
        result = [e for r in reports for e in r['executed']]
        if Counter(e['id'] for e in result) != Counter(expected.keys()):
            raise ValueError('executed membership/multiplicity')
        for row in result:
            if row.get('source') != sources[row['id']]:
                raise ValueError('wrong executed source identity')
            if row['argv'] != expected[row['id']]:
                raise ValueError('wrong executed argv')
        return {e['id']: e for e in result}
    left, right = rows([reference]), rows(shards)
    failures = expected_failures or {}
    for name in expected:
        for key in ('exit_code', 'skip_reasons', 'error', 'failure_reason'):
            if left[name].get(key) != right[name].get(key):
                raise ValueError(f'{name}: different {key}')
        # Baseline skips are compared exactly; new skips need independent approval.
        allowed = json.loads((BASELINE.parent / 'offline_skips.json').read_text(encoding='utf-8'))
        if left[name]['skip_reasons'] != allowed.get('windows' if reference['identity']['os'].startswith('Windows') else 'posix', {}).get(name, []):
            raise ValueError(name + ': unexpected skip')
        if name in failures:
            code, reason = failures[name]
            if left[name]['exit_code'] != code or left[name].get('failure_reason') != reason:
                raise ValueError(name + ': expected failure reason absent')
        elif left[name]['exit_code'] != 0 or left[name].get('error'):
            raise ValueError(name + ': unexpected suite failure')
    print('PBZ_INVENTORY ' + json.dumps({'frozen_count': len(json.loads(BASELINE.read_text())['suites']),
                                        'post_migration': json.loads((BASELINE.parent / 'offline_additions.json').read_text())['suites']}))
    return True


def rollout(evidence):
    """Metrics are collected after completed hosted runs, including final aggregation.

    Each pair references completed run IDs/SHAs, GitHub timestamps and rounded
    billable minutes; cancelled attempts are retained separately and never count.
    """
    if not isinstance(evidence.get('cancelled_attempts'), list):
        raise ValueError('cancelled attempts must be reported separately (an empty list is valid)')
    pairs = evidence['pairs']
    if len(pairs) < 10 or len({p['pair_id'] for p in pairs}) != len(pairs):
        raise ValueError('ten distinct completed pairs required')
    run_ids = [run_id(p.get(k)) for p in pairs for k in ('serial_run', 'sharded_run')]
    cancelled = {run_id(r.get('run_id', r.get('id'))) if isinstance(r, dict) else run_id(r)
                 for r in evidence['cancelled_attempts']}
    if any(r in cancelled for r in run_ids):
        raise ValueError('run binding: counted run was cancelled')
    if len(set(run_ids)) != len(run_ids):
        raise ValueError('hosted runs reused across pairs')
    if not any(p.get('cold_cache') for p in pairs):
        raise ValueError('cold-cache pair required')
    for p in pairs:
        if not all(k in p for k in ('reference', 'shards', 'serial_metrics', 'sharded_metrics')):
            raise ValueError('executed reports and completed hosted metrics required')
        bind_reports(p['reference'], p['shards'], p['serial_run'], p['sharded_run'],
                     p.get('serial_attempt'), p.get('sharded_attempt'))
        compare(p['reference'], p['shards'])
        if p.get('cold_cache') and not all(r.get('cold_cache') for r in [p['reference'], *p['shards']]):
            raise ValueError('missing cold-cache execution evidence')
        if p['reference']['sha'] != p['sha']:
            raise ValueError('pair SHA does not match executed evidence')
        for mode in ('serial', 'sharded'):
            measured = p[mode + '_metrics']
            if (run_id(measured['run_id']) != run_id(p[mode + '_run']) or
                    attempt(measured.get('attempt')) != attempt(p.get(mode + '_attempt')) or measured['sha'] != p['sha'] or measured['conclusion'] != 'success'):
                raise ValueError('hosted metric identity/verdict mismatch')
            if measured['elapsed_seconds'] != p[mode + '_seconds'] or measured['billed_minutes'] != p[mode + '_billed_minutes']:
                raise ValueError('metric values differ from collected evidence')
        if not p.get('equivalent') or p.get('cancelled') or not p.get('sha'):
            raise ValueError('cancelled or unproven pair')
        if not p.get('serial_run') or not p.get('sharded_run'):
            raise ValueError('hosted run identities required')
        if any(p[k] <= 0 for k in ('serial_seconds', 'sharded_seconds', 'serial_billed_minutes', 'sharded_billed_minutes')):
            raise ValueError('invalid hosted measurements')
    serial = [p['serial_seconds'] for p in pairs]
    sharded = [p['sharded_seconds'] for p in pairs]
    if statistics.median(sharded) > .7 * statistics.median(serial):
        raise ValueError('median improvement below 30 percent')
    if max(sharded) > max(serial):
        raise ValueError('maximum elapsed regressed')
    if sum(p['sharded_billed_minutes'] for p in pairs) > 1.5 * sum(p['serial_billed_minutes'] for p in pairs):
        raise ValueError('billed runner-minute ceiling exceeded')
    def bind_control(control):
        identities = [run_id(control.get(k)) for k in ('reference_run_id', 'run_id')]
        if any(r in cancelled for r in identities):
            raise ValueError('run binding: control run was cancelled')
        if len(set([*run_ids, *identities])) != len(run_ids) + len(identities):
            raise ValueError('run binding: control hosted run reused')
        bind_reports(control['reference'], control['shards'], identities[0], identities[1],
                     control.get('reference_attempt'), control.get('attempt'))
        run_ids.extend(identities)

    for name, purpose in (('historical_failure', 'historical'), ('injected_failure', 'injected')):
        control = evidence.get(name, {})
        if not control.get('run_id') or not control.get('controls') or not control.get('reference') or not control.get('shards'):
            raise ValueError('missing expected-failure control: ' + name)
        if not any(c['purpose'] == purpose for c in control['controls']):
            raise ValueError('wrong expected-failure control purpose: ' + name)
        if purpose == 'historical' and not re.fullmatch(r'[0-9a-f]{40}', control.get('historical_revision', '')):
            raise ValueError('historical revision identity required')
        if purpose == 'historical' and not all(
                c.get('revision') == control['historical_revision']
                for c in control['controls'] if c['purpose'] == 'historical'):
            raise ValueError('historical revision record mismatch or absent')
        bind_control(control)
        compare(control['reference'], control['shards'], control_path=control['controls'])
    canary = evidence.get('canary_without_engine', {})
    if not canary.get('run_id') or canary.get('aggregate_verdict') != 'failure' or not canary.get('shards'):
        raise ValueError('missing canary aggregate failure evidence')
    bind_control(canary)
    if any(not r.get('scan', {}).get('completed') for r in canary['shards']):
        raise ValueError('incomplete canary scan')
    planted = [r for r in canary['shards'] if not r['scan'].get('ok')]
    if len(planted) != 1 or planted[0].get('ok') or any(e['id'] == 'engine-offline' for e in planted[0]['executed']):
        raise ValueError('canary must fail a shard without engine-offline')
    if not any(f['reason'] == 'canary' for f in planted[0]['scan'].get('failures', [])):
        raise ValueError('canary detection evidence absent')
    normalized = copy.deepcopy(canary['shards'])
    r = next(r for r in normalized if str(r['shard']) == str(planted[0]['shard']))
    r['scan']['failures'] = [f for f in r['scan'].get('failures', []) if f['reason'] != 'canary']
    r['scan']['ok'] = not r['scan']['failures']
    r['ok'] = r['scan']['ok'] and all(e['exit_code'] == 0 and not e.get('error') for e in r['executed'])
    compare(canary['reference'], normalized)
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument('directory', nargs='?')
    p.add_argument('--rollout')
    p.add_argument('--inventory-only', action='store_true')
    p.add_argument('--serial-only', action='store_true')
    p.add_argument('--controls', default=os.environ.get('PBZ_CI_CONTROLS') or None)
    p.add_argument('--shards-only', action='store_true')
    args = p.parse_args()
    if args.rollout:
        rollout(json.loads(Path(args.rollout).read_text(encoding='utf-8')))
    else:
        reports = [json.loads(f.read_text(encoding='utf-8')) for f in Path(args.directory).rglob('*.json')]
        if args.shards_only:
            if not reports:
                raise ValueError('missing shard reports')
            reference = dict(reports[0], shard='serial', executed=[e for r in reports for e in r['executed']],
                             ok=all(r.get('ok') for r in reports))
            reference.pop('run', None)
            compare(reference, reports, control_path=args.controls)
            print('shard aggregate passed')
            return
        refs = [r for r in reports if r.get('shard') == 'serial']
        if len(refs) != 1:
            raise ValueError('exactly one serial reference required')
        if args.inventory_only:
            if len(reports) != 1:
                raise ValueError('extra serial reports')
            serial_inventory(refs[0])
        elif args.serial_only:
            if len(reports) != 1:
                raise ValueError('extra serial reports')
            inventory, _ = controls(json.loads(MANIFEST.read_text(encoding='utf-8')), args.controls, check_scripts=False)
            rows = {e['id']: e for e in refs[0]['executed']}
            shards = [dict(refs[0], shard=i, executed=[rows[e['id']] for e in group],
                           ok=all(rows[e['id']]['exit_code'] == 0 and not rows[e['id']].get('error') for e in group))
                      for i, group in enumerate(allocate(inventory, 2))]
            for shard in shards:
                shard.pop('run', None)
            compare(refs[0], shards, control_path=args.controls)
        else:
            compare(refs[0], [r for r in reports if r.get('shard') != 'serial'], control_path=args.controls)
    print('equivalence gate passed')


if __name__ == '__main__':
    main()
