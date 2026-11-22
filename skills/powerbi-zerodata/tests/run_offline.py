"""Only executor for offline CI, hooks and shadow shards. Reports actual executions."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time

from canary_scan import CAN, redact, scan
from ci_manifest import load, allocate, controls

SKILL = Path(__file__).resolve().parent.parent
ROOT = SKILL.parents[1]


def identity():
    def version(argv):
        try:
            p = subprocess.run(argv, capture_output=True, text=True, check=True)
            return p.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return 'unavailable'
    def digest(paths):
        h = hashlib.sha256()
        for p in sorted(p for p in paths if p.is_file()):
            h.update(str(p.relative_to(SKILL)).encode())
            h.update(p.read_bytes())
        return h.hexdigest()
    assets = SKILL / 'engine/obj/project.assets.json'
    nuget = sorted(json.loads(assets.read_text(encoding='utf-8'))['libraries']) if assets.is_file() else None
    return {'nuget': nuget, 'python': platform.python_version(), 'os': platform.platform(),
            'image': os.environ.get('ImageVersion', 'local'), 'dotnet': version(['dotnet', '--version']),
            'dependencies': version([sys.executable, '-m', 'pip', 'freeze']),
            'source': digest((SKILL / 'engine').glob('*.cs')),
            'project': digest((SKILL / 'engine').glob('*.csproj')),
            'payload': digest((SKILL / 'engine/bin').rglob('*')) if (SKILL / 'engine/bin/pbz-engine.exe').is_file() else None}


def execute(entries, *, shard='serial', output=None, roots=None, run=None, scanner=scan, expected_failures=None):
    output = Path(output or SKILL / 'ci-results')
    report = {'schema': 1, 'shard': str(shard), 'executed': [],
              'cold_cache': os.environ.get('PBZ_CI_COLD_CACHE') == 'true', 'started': time.time()}
    if os.environ.get('GITHUB_RUN_ID'):
        report['run'] = {key: os.environ[env] for key, env in
                         (('id', 'GITHUB_RUN_ID'), ('attempt', 'GITHUB_RUN_ATTEMPT'),
                          ('workflow', 'GITHUB_WORKFLOW')) if env in os.environ}
    injected = run is not None
    run = subprocess.run if run is None else run
    logs = []
    try:
        output.mkdir(parents=True, exist_ok=True)
        report['identity'] = identity()
        report['sha'] = os.environ.get('GITHUB_SHA') or subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        for entry in entries:
            started = time.monotonic()
            log = output / (entry['id'] + '.log')
            sidecar = (output / (entry['id'] + '.skips.jsonl')).resolve()
            logs.append(log)
            code, skips, error, reason, launched = -1, [], None, None, False
            try:
                sidecar.unlink(missing_ok=True)
                env = {**os.environ, 'PBZ_SKIP_SIDECAR': str(sidecar), 'PBZ_SKIP_SUITE': entry['id']}
                with log.open('wb') as handle:
                    result = run([sys.executable, str(SKILL / 'tests/ci_suite.py'), *entry.get('execution_argv', entry['argv'])], cwd=SKILL, stdout=handle, stderr=subprocess.STDOUT, env=env)
                launched = True
                code = result.returncode
                text = log.read_text(encoding='utf-8', errors='replace')
                skips = [line.strip() for line in text.splitlines() if re.match(r'^SKIP\b', line)]
                if sidecar.exists():
                    for line in sidecar.read_text(encoding='utf-8').splitlines():
                        if not line.strip():
                            continue
                        try:
                            record = json.loads(line)
                        except ValueError:
                            record = None
                        if (not isinstance(record, dict) or
                                not {'suite', 'test', 'reason'} <= record.keys() or
                                record['suite'] != entry['id'] or
                                not isinstance(record['test'], str) or
                                not isinstance(record['reason'], str)):
                            error = error or 'invalid skip sidecar record'
                            continue
                        skips.append('PBZ_SKIP ' + json.dumps({'test': record['test'], 'reason': record['reason']}))
                elif not injected:
                    error = error or 'skip sidecar missing'
                control = (expected_failures or {}).get(entry['id'])
                if control and control[1] in text:
                    reason = control[1]
                print(redact(text), end='', flush=True)
            except Exception as exc:
                error = type(exc).__name__
            row = {'id': entry['id'], 'argv': entry['argv'], 'exit_code': code,
                   'windows': entry.get('windows', False), 'engine': entry.get('engine', False),
                   'launched': launched, 'skip_reasons': skips, 'error': error, 'failure_reason': reason, 'seconds': time.monotonic() - started}
            if 'source' in entry:
                row['source'] = entry['source']
            report['executed'].append(row)
            # Case counts deliberately excluded: standalone scripts have incompatible counters.
            print('PBZ_SUITE ' + redact(json.dumps(row)), flush=True)
    except Exception as exc:
        report['error'] = type(exc).__name__
    finally:
        try:
            report['scan'] = scanner([ROOT, output, *(roots or [])])
            report['finished'] = time.time()
            report['ok'] = (len(report['executed']) == len(entries) and
                            not report.get('error') and
                            all(r['exit_code'] == 0 and r['error'] is None for r in report['executed']) and report['scan']['ok'])
        finally:
            for log in logs:
                log.unlink(missing_ok=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shard', type=int)
    parser.add_argument('--shards', type=int, default=2)
    parser.add_argument('--report', default='ci-results/serial.json')
    parser.add_argument('--output-root', action='append', default=[])
    parser.add_argument('--controls', default=os.environ.get('PBZ_CI_CONTROLS') or None)
    parser.add_argument('--plant-canary', action='store_true', help='explicit shadow failure control')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    entries = load()
    if args.check:
        return 0
    entries, expected_failures = controls(entries, args.controls)
    if args.shard is not None:
        if not 0 <= args.shard < args.shards:
            parser.error('shard outside allocation')
        entries = allocate(entries, args.shards)[args.shard]
    path = Path(args.report).absolute()
    if args.plant_canary or (args.shard is not None and str(args.shard) == os.environ.get('PBZ_CI_CANARY_SHARD')):
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.parent / 'planted-canary.txt').write_text(CAN['canary-strings'][0], encoding='utf-8')
    report = execute(entries, shard=args.shard if args.shard is not None else 'serial',
                     output=path.parent / path.stem, roots=args.output_root, expected_failures=expected_failures)
    path.write_text(redact(json.dumps(report, indent=2)) + '\n', encoding='utf-8')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
