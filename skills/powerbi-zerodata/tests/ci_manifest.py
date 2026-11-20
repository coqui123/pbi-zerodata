"""Explicit registration and deterministic whole-script allocation."""
import hashlib
import collections
import json
import re
import subprocess
import tempfile
import atexit
from pathlib import Path

TESTS = Path(__file__).resolve().parent
MANIFEST = TESTS / 'offline_suites.json'
ROOT = TESTS.parents[2]
_CONTROL_DIRS = []


def _cleanup_controls():
    for directory in _CONTROL_DIRS:
        directory.cleanup()


atexit.register(_cleanup_controls)


def control_path(path):
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


BASELINE = TESTS / 'offline_baseline.json'


def validate(entries, tests=TESTS):
    required = {'id', 'argv', 'windows', 'engine'}
    if not isinstance(entries, list) or any(not isinstance(e, dict) or not required <= e.keys() for e in entries):
        raise ValueError('invalid suite entry: id, argv and requirements required')
    if any(not isinstance(e['id'], str) or not isinstance(e['argv'], list) or not e['argv'] or
           not all(isinstance(a, str) for a in e['argv']) for e in entries):
        raise ValueError('invalid suite ID/argv')
    ids = [e['id'] for e in entries]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate suite ID')
    expected = {p.name for p in tests.glob('test_*.py')} - {'test_e2e.py'}
    scripts = []
    for e in entries:
        argv = e['argv']
        script = Path(argv[0])
        if script.parent != Path('tests') or not (tests / script.name).is_file():
            raise ValueError('nonexistent or extra script: ' + str(script))
        if not script.name.startswith('test_'):
            raise ValueError('non-test script: ' + str(script))
        if script.name == 'test_e2e.py':
            raise ValueError('e2e is live-only')
        if script.name == 'test_engine.py' and argv != ['tests/test_engine.py', '--offline']:
            raise ValueError('engine requires --offline')
        if not isinstance(e['windows'], bool) or not isinstance(e['engine'], bool):
            raise ValueError('requirements must be boolean')
        if e['id'] in ('ci_registration', 'plan_pure') and e['engine']:
            raise ValueError(e['id'] + ' must be engine-free')
        if script.name == 'test_engine.py' and not (e['engine'] and e['windows']):
            raise ValueError('engine-offline requires engine and windows')
        if e.get('weight', 1) <= 0:
            raise ValueError('weight must be positive')
        scripts.append(script.name)
    actual = set(scripts)
    if actual != expected:
        raise ValueError(f'omission/extra scripts: missing={sorted(expected-actual)} extra={sorted(actual-expected)}')
    # Only synth has two historical invocations, with distinct arguments.
    want = collections.Counter({s: 1 for s in expected})
    want['test_synth.py'] = 2
    if collections.Counter(scripts) != want:
        raise ValueError('script invocation multiplicity')
    if sorted(e['argv'] for e in entries if Path(e['argv'][0]).name == 'test_synth.py') != [
            ['tests/test_synth.py'], ['tests/test_synth.py', '--provenance']]:
        raise ValueError('wrong synth arguments')
    for e in entries:
        stem = Path(e['argv'][0]).stem.removeprefix('test_')
        permitted = {'engine': {'engine-offline'}, 'synth': {'synth', 'synth-provenance'}}.get(stem, {stem})
        if e['id'] not in permitted:
            raise ValueError('extra/omitted suite ID: ' + e['id'])
    return entries


def load(path=MANIFEST):
    return validate(json.loads(Path(path).read_text(encoding='utf-8')))


def allocate(entries, count):
    if count < 1:
        raise ValueError('shard count must be positive')
    groups, weights = [[] for _ in range(count)], [0.0] * count
    for entry in sorted(entries, key=lambda e: (-e.get('weight', 1), e['id'])):
        slot = min(range(count), key=lambda n: (weights[n], n))
        groups[slot].append(entry)
        weights[slot] += entry.get('weight', 1)
    return groups


def controls(entries, path=None, check_scripts=True):
    """Explicit expected-failure experiments; the normal manifest stays frozen."""
    if not path:
        return entries, {}
    records = path if isinstance(path, list) else json.loads(control_path(path).read_text(encoding='utf-8'))
    result = {e['id']: dict(e) for e in entries}
    expected = {}
    for record in records:
        name = record['id']
        if name in expected or record['purpose'] not in ('historical', 'injected'):
            raise ValueError('invalid/duplicate failure control')
        if not record['reason'] or not isinstance(record['exit_code'], int) or record['exit_code'] <= 0:
            raise ValueError('failure control needs a nonzero exit and specific reason')
        if record['purpose'] == 'historical' and name not in result:
            raise ValueError('historical control must replace a known suite')
        if record['purpose'] == 'injected' and name in result:
            raise ValueError('injected control must have a separate suite ID')
        argv = list(record.get('argv', [record.get('script', '')]))
        source, execution_argv = None, list(argv)
        if 'revision' in record or 'script' in record:
            revision, script = record.get('revision', ''), Path(record.get('script', ''))
            if (not re.fullmatch(r'[0-9a-fA-F]{40}', revision) or script.is_absolute()
                    or '..' in script.parts or not script.parts):
                raise ValueError('invalid historical revision/script')
            data = subprocess.check_output(['git', 'show', f'{revision}:{script.as_posix()}'], cwd=ROOT)
            digest = hashlib.sha256(data).hexdigest()
            if (not re.fullmatch(r'[0-9a-f]{64}', digest) or
                    record.get('sha256', digest) != digest):
                raise ValueError('historical content identity mismatch')
            source = dict(revision=revision, path=script.as_posix(), sha256=digest)
            if check_scripts:
                directory = tempfile.TemporaryDirectory(prefix='pbz-controls-')
                _CONTROL_DIRS.append(directory)
                target = Path(directory.name) / script.name
                target.write_bytes(data)
                execution_argv[0] = str(target)
        elif check_scripts and not (TESTS.parent / argv[0]).is_file():
            raise ValueError('missing control script')
        result[name] = dict(id=name, argv=argv, windows=True, engine=True)
        if source is not None:
            result[name]['source'] = source
            result[name]['execution_argv'] = execution_argv
        expected[name] = (record['exit_code'], record['reason'])
    return list(result.values()), expected
