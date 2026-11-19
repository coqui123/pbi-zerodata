"""Collect completed-run elapsed and runner cost; billing input must be observed.

Run after aggregate completion. Per-job rounding is reported as an estimate, never
substituted for actual billed minutes in the rollout gate.
"""
import argparse
from datetime import datetime
import json
import math
import subprocess


def metrics(run, jobs, billed_minutes):
    if run['status'] != 'completed' or run['conclusion'] == 'cancelled':
        raise ValueError('cancelled or incomplete attempts are not evidence')
    if not jobs or any(j['status'] != 'completed' or j['conclusion'] == 'cancelled' for j in jobs):
        raise ValueError('incomplete job evidence')
    def seconds(stamp):
        return datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
    intervals = [seconds(j['completed_at']) - seconds(j['started_at']) for j in jobs
                 if j['conclusion'] != 'skipped']
    if not intervals or billed_minutes <= 0:
        raise ValueError('observed billing and completed jobs required')
    return {'run_id': run['id'], 'sha': run['head_sha'], 'conclusion': run['conclusion'],
            'elapsed_seconds': max(seconds(j['completed_at']) for j in jobs if j['conclusion'] != 'skipped') - seconds(run['created_at']),
            'runner_minutes': sum(intervals) / 60,
            'rounded_job_minutes_estimate': sum(math.ceil(s / 60) for s in intervals),
            'billed_minutes': billed_minutes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repository')
    parser.add_argument('run_id')
    parser.add_argument('--observed-billed-minutes', type=float, required=True)
    args = parser.parse_args()
    def api(path):
        return json.loads(subprocess.check_output(['gh', 'api', path], text=True))
    base = f'repos/{args.repository}/actions/runs/{args.run_id}'
    run = api(base)
    jobs, page = [], 1
    while True:
        batch = api(base + f'/jobs?per_page=100&page={page}')['jobs']
        jobs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    print(json.dumps(metrics(run, jobs, args.observed_billed_minutes), indent=2))


if __name__ == '__main__':
    main()
