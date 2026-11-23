"""Compute suite weights from serial report durations without editing manifests."""
import argparse
from collections import defaultdict
import copy
import json
from pathlib import Path
from statistics import median


def load_reports(paths):
    """Read the supplied report JSON files."""
    return [json.loads(Path(path).read_text(encoding='utf-8')) for path in paths]


def compute_weights(reports):
    """Return rounded, positive median seconds for suites with executed samples.

    Only rows that launched, exited 0 and are not expected-failure control
    rows (which carry a source key) count as duration samples.
    """
    samples = defaultdict(list)
    for report in reports:
        for entry in report['executed']:
            if (entry.get('launched', True) and entry.get('exit_code', 0) == 0
                    and 'source' not in entry):
                samples[entry['id']].append(entry['seconds'])
    return {name: max(0.1, round(median(values), 1))
            for name, values in sorted(samples.items())}


def apply_weights(entries, weights):
    """Return copies of entries with computed weights set; others unchanged.

    Entries whose id has no computed weight keep exactly their prior state,
    and the input entries are not mutated.
    """
    result = []
    for entry in entries:
        entry = copy.deepcopy(entry)
        if entry['id'] in weights:
            entry['weight'] = weights[entry['id']]
        result.append(entry)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports', nargs='+', type=Path)
    args = parser.parse_args()
    print(json.dumps(compute_weights(load_reports(args.reports)), indent=2))


if __name__ == '__main__':
    main()
