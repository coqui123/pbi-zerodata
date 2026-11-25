"""Compute suite weights from hosted shard-context seconds as allocated.

Only sharded offline-shadow reports form the timing basis, never serial runs.
"""
import argparse
from collections import defaultdict
import copy
import json
from pathlib import Path
from statistics import median


def load_reports(paths):
    """Read the supplied report JSON files."""
    return [json.loads(Path(path).read_text(encoding='utf-8')) for path in paths]


def _samples(reports):
    for report in reports:
        if report['shard'] == 'serial':
            raise ValueError('serial report is not a shard-context timing source')
        if report['shard'] not in ('0', '1'):
            raise ValueError(f"invalid shard report: {report['shard']!r}")
    shas = sorted({report['sha'] for report in reports})
    if len(shas) > 1:
        raise ValueError(f'differing sha values: {shas}')
    identities = sorted({json.dumps(report['identity'], sort_keys=True, indent=2)
                         for report in reports})
    if len(identities) > 1:
        raise ValueError('differing identity values:\n' + '\n'.join(identities))
    seen_reports = set()
    for report in reports:
        key = (report['run']['id'], report['run'].get('attempt'), report['shard'])
        if key in seen_reports:
            raise ValueError(f'duplicate report in pool: run {key[0]} attempt {key[1]} shard {key[2]}')
        seen_reports.add(key)
    samples = defaultdict(list)
    observed = set()
    for report in reports:
        run_key = (report['run']['id'], report['run'].get('attempt'))
        for entry in report['executed']:
            if (entry.get('launched', False) and entry.get('exit_code') == 0
                    and 'source' not in entry):
                if (run_key, entry['id']) in observed:
                    raise ValueError(f"duplicate eligible row for {entry['id']} in run {run_key[0]} attempt {run_key[1]}")
                observed.add((run_key, entry['id']))
                samples[entry['id']].append(entry['seconds'])
    return samples


def compute_weights(reports):
    """Return median shard-context seconds for suites with at least 3 samples."""
    return {name: round(max(0.1, median(values)), 1)
            for name, values in sorted(_samples(reports).items()) if len(values) >= 3}


def sample_counts(reports):
    """Return eligible sample counts with the same shard-context validation."""
    return {name: len(values) for name, values in sorted(_samples(reports).items())}


def apply_weights(entries, weights):
    """Return deep copies with measured weights; other entries keep prior state."""
    result = copy.deepcopy(entries)
    for entry in result:
        if entry['id'] in weights:
            entry['weight'] = weights[entry['id']]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports', nargs='+', type=Path)
    args = parser.parse_args()
    reports = load_reports(args.reports)
    weights = compute_weights(reports)
    counts = sample_counts(reports)
    entries = json.loads(Path(__file__).with_name('offline_suites.json').read_text(encoding='utf-8'))
    provenance = dict(
        runs=sorted({report['run']['id'] for report in reports}),
        sha=reports[0]['sha'], identity=reports[0]['identity'], sample_counts=counts,
        excluded_rows=sum(len(report['executed']) for report in reports) - sum(counts.values()),
        low_sample_suites=[name for name, count in counts.items() if 3 <= count < 5],
        fallback_suites=sorted(entry['id'] for entry in entries if entry['id'] not in weights))
    print(json.dumps(dict(weights=weights, provenance=provenance), indent=2))


if __name__ == '__main__':
    main()
