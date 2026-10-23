"""Failure/recovery and environment identity integration regressions."""
import importlib.util
import json
import os
from pathlib import Path
import sys
from unittest import mock

from test_agent_worktree import LifecycleFixture, ROOT


class RecoveryTests(LifecycleFixture):
    def test_identity_unmanaged_is_distinct_from_malformed(self):
        result = self.wt('identity', self.repo, '--json')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['status'], 'unmanaged')
        self.state_file().write_text('{bad')
        result = self.wt('identity', self.repo, '--json')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['status'], 'error')

    def test_environment_cannot_impersonate_other_managed_worktree(self):
        first = self.opened('first')
        second = self.opened('second')
        self.env.update(AGENT_ACTOR='codex', AGENT_SESSION='first')
        conflicting = self.wt('identity', second['path'], '--json')
        self.assertEqual(conflicting.returncode, 2)
        self.assertEqual(json.loads(conflicting.stdout)['status'], 'error')
        current = self.wt('identity', first['path'], '--json')
        self.assertEqual(current.returncode, 0, current.stdout)
        self.assertEqual(json.loads(current.stdout)['session'], 'first')
        unmanaged = self.wt('identity', self.repo, '--json')
        self.assertEqual(unmanaged.returncode, 1, unmanaged.stdout)
        explicit_lookup = self.wt('identity', self.repo, '--session', 'first', '--json')
        self.assertEqual(explicit_lookup.returncode, 0, explicit_lookup.stdout)
        conflicting_cli = self.wt('identity', second['path'], '--session', 'first', '--json')
        self.assertEqual(conflicting_cli.returncode, 2)

    def test_environment_identity_and_idempotent_acquire(self):
        self.env.update(AGENT_ACTOR='codex', AGENT_SESSION='stable')
        opened = self.wt('open', self.repo, '--json')
        self.assertEqual(opened.returncode, 0, opened.stdout)
        row = json.loads(opened.stdout)
        self.assertEqual(row['session'], 'stable')
        self.assertEqual(row['agent'], 'codex')
        acquired = self.lock('acquire', self.repo, '--branch', row['branch'], '--json')
        reacquired = self.lock('acquire', row['path'], '--branch', row['branch'], '--json')
        self.assertEqual(acquired.returncode, 0, acquired.stdout)
        self.assertEqual(reacquired.returncode, 0, reacquired.stdout)
        first = json.loads(acquired.stdout)['lock']
        second = json.loads(reacquired.stdout)['lock']
        self.assertEqual(first['acquired'], second['acquired'])
        self.assertGreater(second['expires_epoch'], first['expires_epoch'])

    def test_legacy_orphans_preserved_while_active_sessions_migrate(self):
        active = self.opened('active')
        orphan = dict(active, session='orphan', path=str(Path(self.tmp) / 'missing-worktree'), branch='agent/codex/orphan')
        self.fixture.run_git(self.repo, 'branch', orphan['branch'])
        self.state_file().unlink()
        source = self.state / 'old-path.json'
        source.write_text(json.dumps([active, orphan]))
        before = source.read_bytes()
        migrated = self.wt('migrate', self.repo, '--json')
        self.assertEqual(migrated.returncode, 0, migrated.stdout)
        rows = json.loads(self.state_file().read_text())['sessions']
        self.assertEqual({r['session']: r['lifecycle'] for r in rows}, {'active': 'active', 'orphan': 'orphan'})
        orphan_record = next(r for r in rows if r['session'] == 'orphan')
        self.assertEqual(orphan_record['legacy_record'], orphan)
        self.assertEqual(source.with_suffix('.json.migrated').read_bytes(), before)
        self.assertEqual(self.wt('identity', active['path'], '--json').returncode, 0)
        self.assertEqual(self.wt('identity', self.repo, '--session', 'orphan', '--json').returncode, 2)
        self.assertNotEqual(self.wt('close', self.repo, '--agent', 'codex', '--session', 'orphan').returncode, 0)
        self.assertEqual(self.fixture.run_git(self.repo, 'show-ref', '--verify', f'refs/heads/{orphan["branch"]}').returncode, 0)
        diagnosed = json.loads(self.wt('reconcile', self.repo, '--json').stdout)
        self.assertEqual(diagnosed['counts']['orphan_or_changed'], 1)
        self.assertEqual(diagnosed['counts']['ok'], 1)

    def test_close_registry_failure_after_removal_has_durable_retry(self):
        row = self.opened()
        oid = self.commit(row['path'])
        spec = importlib.util.spec_from_file_location('lifecycle_under_test', ROOT / 'lib/agent_lifecycle.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real_save = module.save_registry
        calls = 0

        def fail_final_write(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('simulated registry disk error after Git removal')
            return real_save(*args)

        argv = ['agent-worktree', 'close', str(self.repo), '--agent', 'codex', '--session', 's1', '--json']
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(sys, 'argv', argv), mock.patch.object(module, 'save_registry', fail_final_write):
            self.assertEqual(module.main('agent-worktree'), 2)
        self.assertFalse(Path(row['path']).exists())
        remaining = json.loads(self.state_file().read_text())['sessions']
        self.assertEqual(remaining[0]['lifecycle'], 'closing')
        self.assertEqual(remaining[0]['close_oid'], oid)
        retry = self.wt('close', self.repo, '--agent', 'codex', '--session', 's1', '--json')
        self.assertEqual(retry.returncode, 0, retry.stdout)
        self.assertEqual(json.loads(self.state_file().read_text())['sessions'], [])
        self.assertEqual(self.fixture.run_git(self.repo, 'rev-parse', row['branch']).stdout.strip(), oid)

    def test_prune_does_not_trust_fake_stale_remote_tracking_ref(self):
        row = self.opened()
        oid = self.commit(row['path'])
        self.fixture.run_git(self.repo, 'update-ref', 'refs/remotes/nonexistent/fake', oid)
        result = self.wt('close', self.repo, '--agent', 'codex', '--session', 's1', '--prune')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(Path(row['path']).exists())
        self.assertEqual(self.fixture.run_git(self.repo, 'rev-parse', row['branch']).stdout.strip(), oid)

    def test_lock_declared_identity_must_match_current_managed_worktree(self):
        first = self.opened('first')
        second = self.opened('second')
        args = ['--agent', 'codex', '--session', 'first', '--branch', first['branch'], '--json']
        acquired = self.lock('acquire', self.repo, *args)
        self.assertEqual(acquired.returncode, 0, acquired.stdout)
        self.assertTrue(json.loads(acquired.stdout)['lock']['host'])
        before = next(self.locks.glob('*.lock')).read_bytes()
        for command in ('check', 'acquire', 'renew', 'release', 'recover'):
            with self.subTest(command=command):
                result = self.lock(command, second['path'], *args, '--reason', 'test mismatch')
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(json.loads(result.stdout)['status'], 'error')
                self.assertEqual(next(self.locks.glob('*.lock')).read_bytes(), before)
        release = self.lock('release', self.repo, *args)
        self.assertEqual(release.returncode, 0, release.stdout)

    def test_entrypoints_work_through_standard_bin_symlinks(self):
        from test_agent_worktree import LOCK, WORKTREE, run
        bindir = Path(self.tmp) / '.local/bin'
        bindir.mkdir(parents=True)
        for source in (LOCK, WORKTREE):
            target = bindir / source.name
            target.symlink_to(source)
            result = run(target, '--help', env=self.env)
            self.assertEqual(result.returncode, 0, result.stderr)
        result = run(bindir / 'agent-worktree', 'identity', self.repo, '--json', env=self.env)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'unmanaged')
