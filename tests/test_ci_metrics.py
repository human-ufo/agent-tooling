import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('ci_metrics', Path(__file__).resolve().parents[1] / 'lib/agent_ci_metrics.py')
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)


class MetricsTests(unittest.TestCase):
    def test_window_counts_duplicates_and_missing_durations(self):
        now = datetime(2026, 11, 18, tzinfo=timezone.utc)
        def record(result, offset=0, **fields):
            return dict(ts=(now - timedelta(days=offset)).isoformat(), repo_id='repo', event='verification', result=result, **fields)
        rows = [record('passed', run_duration_ms=10, operation_id='a'),
                record('passed', run_duration_ms=30, operation_id='b'),
                record('failed', operation_id='c'),
                record('skipped', reason='optional-unconfigured', operation_id='d'),
                record('skipped', reason='deletion', operation_id='e'),
                record('passed', offset=20, run_duration_ms=500),
                record('passed', offset=-1, run_duration_ms=900)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = '\n'.join(json.dumps(row) for row in rows)
            (root / 'first.jsonl').write_text(content + '\ninvalid\n[]\n{"ts": 12}\n' + json.dumps(record('passed', operation_id=[])) + '\n')
            (root / 'copy.jsonl').write_text(content)
            report = metrics.summarize(root, 14, now)
        totals = report['totals']
        self.assertEqual((totals['passed'], totals['failed'], totals['skipped']), (2, 1, 2))
        self.assertEqual((totals['run_samples'], totals['run_p50_ms'], totals['run_p95_ms']), (2, 10, 30))
        self.assertEqual(report['repos_with_unconfigured_verifications'], ['repo'])
        self.assertEqual(report['malformed_records'], 4)

    def test_empty_directory_and_invalid_window(self):
        with tempfile.TemporaryDirectory() as directory:
            report = metrics.summarize(Path(directory), 14)
        self.assertEqual(report['totals']['run_samples'], 0)
        self.assertIsNone(report['totals']['run_p95_ms'])
        for value in ('0', '-1', 'nan', 'inf'):
            with self.assertRaises(Exception):
                metrics.positive_days(value)

    def test_cli_missing_directory_and_json_output(self):
        command = Path(__file__).resolve().parents[1] / 'bin/agent-ci-metrics'
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([str(command), '--directory', directory, '--days', '14'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['days'], 14)
            result = subprocess.run([str(command), '--directory', directory + '/missing'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')
            self.assertIn('Cannot read audit records', result.stderr)
            result = subprocess.run([str(command), '--days', 'nan'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
