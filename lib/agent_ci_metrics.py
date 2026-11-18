#!/usr/bin/env python3
"""Summarize existing publication audit records without changing repository state."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path


def percentile(values, fraction):
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def summarize(directory, days, now=None):
    if not directory.is_dir():
        raise OSError('audit directory is missing or is not a directory')
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    repos = {}
    malformed = 0
    seen = set()
    for path in sorted(directory.glob('*.jsonl')):
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    stamp = datetime.fromisoformat(row['ts'].replace('Z', '+00:00'))
                    if stamp.tzinfo is None:
                        raise ValueError('timestamp lacks timezone')
                    repo = row['repo_id']
                    if not isinstance(repo, str):
                        raise ValueError('invalid repository identifier')
                except (ValueError, KeyError, TypeError, AttributeError):
                    malformed += 1
                    continue
                if not start <= stamp <= now or row.get('event') != 'verification':
                    continue
                # A copied audit file must not inflate the counts. Different refs
                # and retries remain independent verification events.
                identity = (repo, row.get('operation_id'), row.get('ref'), row.get('sha'), row['ts'], row.get('result'))
                if any(value is not None and not isinstance(value, str) for value in identity):
                    malformed += 1
                    continue
                if identity in seen:
                    continue
                seen.add(identity)
                data = repos.setdefault(repo, dict(passed=0, failed=0, skipped=0,
                    optional_unconfigured=0, duration_samples_ms=[], queue_samples_ms=[]))
                result = row.get('result')
                if result in ('passed', 'failed', 'skipped'):
                    data[result] += 1
                if result == 'skipped' and row.get('reason') == 'optional-unconfigured':
                    data['optional_unconfigured'] += 1
                for field, target in (('run_duration_ms', 'duration_samples_ms'), ('queue_wait_ms', 'queue_samples_ms')):
                    value = row.get(field)
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                        data[target].append(value)
    totals = dict(passed=0, failed=0, skipped=0, optional_unconfigured=0)
    durations = []
    queues = []
    for data in repos.values():
        for field in totals:
            totals[field] += data[field]
        durations.extend(data['duration_samples_ms'])
        queues.extend(data['queue_samples_ms'])
        finish_durations(data)
    totals['duration_samples_ms'] = durations
    totals['queue_samples_ms'] = queues
    finish_durations(totals)
    return dict(window_start=start.isoformat(), window_end=now.isoformat(), days=days,
                malformed_records=malformed, totals=totals, repos=repos,
                repos_with_unconfigured_verifications=sorted(repo for repo, data in repos.items() if data['optional_unconfigured']))


def finish_durations(data):
    for source, prefix in (('duration_samples_ms', 'run'), ('queue_samples_ms', 'queue')):
        values = data.pop(source)
        data[prefix + '_samples'] = len(values)
        data[prefix + '_p50_ms'] = percentile(values, .50)
        data[prefix + '_p95_ms'] = percentile(values, .95)


def positive_days(value):
    days = float(value)
    if not math.isfinite(days) or days <= 0:
        raise argparse.ArgumentTypeError('days must be finite and positive')
    return days


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=positive_days, default=14)
    parser.add_argument('--directory', type=Path, default=Path(os.environ.get('AGENT_CHANGESET_DIR', str(Path.home() / '.cache/agent-changesets'))))
    args = parser.parse_args()
    try:
        report = summarize(args.directory, args.days)
    except (OSError, OverflowError) as exc:
        parser.exit(2, 'Cannot read audit records: ' + str(exc) + '\n')
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
