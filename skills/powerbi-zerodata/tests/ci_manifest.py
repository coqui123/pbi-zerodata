"""Explicit registration and deterministic whole-script allocation."""
import collections
import json
from pathlib import Path

TESTS = Path(__file__).resolve().parent
MANIFEST = TESTS / 'offline_suites.json'
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
        if script.name == 'test_e2e.py':
            raise ValueError('e2e is live-only')
        if script.name == 'test_engine.py' and argv != ['tests/test_engine.py', '--offline']:
            raise ValueError('engine requires --offline')
        if not isinstance(e['windows'], bool) or not isinstance(e['engine'], bool):
            raise ValueError('requirements must be boolean')
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
    records = path if isinstance(path, list) else json.loads(Path(path).read_text(encoding='utf-8'))
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
        if check_scripts and not (TESTS.parent / record['argv'][0]).is_file():
            raise ValueError('missing control script')
        result[name] = dict(id=name, argv=record['argv'], windows=True, engine=True)
        expected[name] = (record['exit_code'], record['reason'])
    return list(result.values()), expected
