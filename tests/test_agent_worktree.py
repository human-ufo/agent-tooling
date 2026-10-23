"""Lifecycle regressions using real Git repositories and local remotes only."""
from pathlib import Path
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKTREE = ROOT / 'bin' / 'agent-worktree'
LOCK = ROOT / 'bin' / 'agent-lock'


def run(cmd, *args, cwd=None, env=None):
    return subprocess.run([str(cmd), *map(str, args)], capture_output=True, text=True, cwd=cwd, env=env)


class RepoFixture:
    def __init__(self, base, branch='main'):
        self.base = Path(base)
        self.origin = self.base / 'origin'
        self.clone = self.base / 'clone'
        self.origin.mkdir(parents=True)
        self.run_git(self.origin, 'init', '-q', '-b', branch)
        (self.origin / 'README.md').write_text('hola\n')
        self.run_git(self.origin, 'add', '.')
        self.run_git(self.origin, '-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'initial')
        self.run_git(self.origin, 'config', 'receive.denyCurrentBranch', 'ignore')
        self.run_git(self.base, 'clone', '-q', str(self.origin), str(self.clone))
        self.run_git(self.clone, 'config', 'user.name', 't')
        self.run_git(self.clone, 'config', 'user.email', 't@t')
        self.run_git(self.clone, 'config', 'commit.gpgsign', 'false')

    @staticmethod
    def run_git(cwd, *args):
        return run('git', *args, cwd=cwd)


class LifecycleFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fixture = RepoFixture(self.tmp)
        self.repo = self.fixture.clone
        self.state = Path(self.tmp) / 'state'
        self.locks = Path(self.tmp) / 'locks'
        self.state.mkdir()
        self.locks.mkdir()
        self.env = dict(os.environ, AGENT_WORKTREE_DIR=str(self.state), AGENT_LOCK_DIR=str(self.locks))
        self.env.pop('AGENT_NAME', None)
        self.env.pop('AGENT_SESSION_ID', None)
        self.env.pop('AGENT_ACTOR', None)
        self.env.pop('AGENT_SESSION', None)
        common = os.path.realpath(self.fixture.run_git(self.repo, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip())
        self.repo_id = hashlib.sha256(common.encode()).hexdigest()

    def wt(self, *args, cwd=None):
        return run('bash', WORKTREE, *args, env=self.env, cwd=cwd)

    def lock(self, *args, cwd=None):
        return run('bash', LOCK, *args, env=self.env, cwd=cwd)

    def opened(self, session='s1', agent='codex'):
        result = self.wt('open', self.repo, '--agent', agent, '--session', session, '--json')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def state_file(self):
        return self.state / f'{self.repo_id}.json'

    def commit(self, path, name='local.txt'):
        (Path(path) / name).write_text('local only\n')
        self.fixture.run_git(path, 'add', name)
        result = self.fixture.run_git(path, 'commit', '-qm', 'local')
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.fixture.run_git(path, 'rev-parse', 'HEAD').stdout.strip()


class WorktreeTests(LifecycleFixture):
    def test_open_identity_and_isolation_are_canonical(self):
        first, second = self.opened(), self.opened('s2')
        self.assertNotEqual(first['path'], second['path'])
        self.assertEqual(first['repo_id'], self.repo_id)
        (Path(first['path']) / 'private.txt').write_text('one')
        self.assertFalse((Path(second['path']) / 'private.txt').exists())
        subdir = Path(first['path']) / 'nested'
        subdir.mkdir()
        identified = self.wt('identity', subdir, '--json')
        row = json.loads(identified.stdout)
        self.assertEqual(row['session'], 's1')
        self.assertEqual(row['actor'], 'codex')
        self.assertEqual(row['worktree'], first['path'])
        self.assertEqual(row['repo_id'], self.repo_id)
        state = json.loads(self.state_file().read_text())
        self.assertEqual(state['version'], 3)
        self.assertEqual(len(state['sessions']), 2)

    def test_generated_session_is_stable_and_lookup_has_no_pid_fallback(self):
        result = self.wt('open', self.repo, '--agent', 'codex', '--json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = json.loads(result.stdout)
        for _ in range(2):
            identity = json.loads(self.wt('identity', row['path'], '--json').stdout)
            self.assertEqual(identity['session'], row['session'])
        path = self.wt('path', self.repo, '--agent', 'codex', '--session', row['session'])
        self.assertEqual(path.stdout.strip(), row['path'])
        ambiguous = self.wt('path', self.repo, '--agent', 'codex')
        self.assertNotEqual(ambiguous.returncode, 0)

    def test_session_and_actor_paths_are_validated(self):
        for value in ('../escape', '/tmp/escape', '.', '..', 'a/b', '-bad'):
            out = self.wt('open', self.repo, '--agent', 'codex', '--session', value)
            self.assertNotEqual(out.returncode, 0, value)
        self.assertFalse(self.state_file().exists())

    def test_reinclusion_gitignore_cannot_expose_external_worktrees(self):
        (self.repo / '.gitignore').write_text('*\n!*/\n')
        self.fixture.run_git(self.repo, 'add', '-f', '.gitignore')
        self.fixture.run_git(self.repo, 'commit', '-qm', 'reinclusion')
        opened = self.opened()
        self.assertNotIn(str(self.repo) + '/', opened['path'])
        self.assertEqual(self.fixture.run_git(self.repo, 'status', '--porcelain').stdout, '')

    def test_branch_conflict_and_cross_actor_reopen_fail(self):
        row = self.opened()
        duplicate = self.wt('open', self.repo, '--agent', 'other', '--session', 's2', '--branch', row['branch'])
        self.assertNotEqual(duplicate.returncode, 0)
        foreign = self.wt('open', self.repo, '--agent', 'other', '--session', 's1')
        self.assertNotEqual(foreign.returncode, 0)
        close = self.wt('close', self.repo, '--agent', 'other', '--session', 's1')
        self.assertNotEqual(close.returncode, 0)

    def test_close_preserves_local_only_branch(self):
        row = self.opened()
        oid = self.commit(row['path'])
        out = self.wt('close', self.repo, '--agent', 'codex', '--session', 's1')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(Path(row['path']).exists())
        self.assertEqual(self.fixture.run_git(self.repo, 'rev-parse', row['branch']).stdout.strip(), oid)
        self.assertEqual(json.loads(self.state_file().read_text())['sessions'], [])

    def test_close_refuses_tracked_untracked_and_ignored_even_force(self):
        for name in ('README.md', 'untracked.txt', 'ignored.tmp'):
            with self.subTest(name=name):
                row = self.opened('s' + str(len(name)))
                path = Path(row['path'])
                if name == 'ignored.tmp':
                    exclude = Path(self.fixture.run_git(self.repo, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()) / 'info/exclude'
                    exclude.write_text('*.tmp\n')
                (path / name).write_text('do not discard')
                result = self.wt('close', self.repo, '--agent', 'codex', '--session', row['session'], '--force')
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue((path / name).exists())
                self.assertIn(row['session'], self.state_file().read_text())

    def test_prune_rejects_local_only_and_squash_work(self):
        row = self.opened()
        self.commit(row['path'])
        self.fixture.run_git(self.repo, 'merge', '--squash', row['branch'])
        self.fixture.run_git(self.repo, 'commit', '-qm', 'squash')
        self.fixture.run_git(self.repo, 'push', '-q', 'origin', 'main')
        out = self.wt('close', self.repo, '--agent', 'codex', '--session', 's1', '--prune')
        self.assertNotEqual(out.returncode, 0)
        self.assertTrue(Path(row['path']).exists())
        self.assertIn('local-only', out.stderr)

    def test_prune_allows_exact_commit_preserved_on_remote(self):
        row = self.opened()
        self.commit(row['path'])
        self.fixture.run_git(row['path'], 'push', '-q', 'origin', row['branch'])
        out = self.wt('close', self.repo, '--agent', 'codex', '--session', 's1', '--prune')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotEqual(self.fixture.run_git(self.repo, 'rev-parse', '--verify', f'refs/heads/{row["branch"]}').returncode, 0)

    def test_removed_or_changed_worktree_preserves_record_and_reconcile_is_read_only(self):
        row = self.opened()
        self.fixture.run_git(row['path'], 'checkout', '--detach', '-q')
        before = self.state_file().read_bytes()
        close = self.wt('close', self.repo, '--agent', 'codex', '--session', 's1')
        self.assertNotEqual(close.returncode, 0)
        out = json.loads(self.wt('reconcile', self.repo, '--json').stdout)
        self.assertEqual(out['sessions'][0]['status'], 'missing-or-changed')
        self.assertTrue(out['read_only'])
        self.assertEqual(before, self.state_file().read_bytes())
        self.assertTrue(Path(row['path']).exists())

    def test_remove_failure_retains_registry(self):
        row = self.opened()
        self.fixture.run_git(self.repo, 'worktree', 'lock', row['path'])
        before = self.state_file().read_bytes()
        close = self.wt('close', self.repo, '--agent', 'codex', '--session', 's1')
        self.assertNotEqual(close.returncode, 0)
        self.assertEqual(before, self.state_file().read_bytes())
        self.assertTrue(Path(row['path']).exists())

    def test_remote_master_and_fresh_remote_default_are_used(self):
        self.fixture.run_git(self.fixture.origin, 'branch', '-m', 'main', 'master')
        (self.fixture.origin / 'new.txt').write_text('remote advance')
        self.fixture.run_git(self.fixture.origin, 'add', 'new.txt')
        self.fixture.run_git(self.fixture.origin, '-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'advance')
        row = self.opened()
        self.assertEqual(row['base'], 'refs/remotes/origin/master')
        self.assertTrue((Path(row['path']) / 'new.txt').exists())

    def test_unavailable_remote_refuses_head_fallback(self):
        self.fixture.run_git(self.repo, 'remote', 'set-url', 'origin', str(Path(self.tmp) / 'missing'))
        out = self.wt('open', self.repo, '--agent', 'codex', '--session', 's1')
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse(self.state_file().exists())

    def test_legacy_registry_requires_explicit_migration(self):
        row = self.opened()
        self.state_file().unlink()
        old = self.state / 'old_path.json'
        old.write_text(json.dumps([row]))
        result = self.wt('identity', row['path'], '--json')
        self.assertEqual(json.loads(result.stdout)['status'], 'error')
        before = old.read_bytes()
        diagnosed = self.wt('reconcile', self.repo, '--json')
        self.assertEqual(diagnosed.returncode, 0, diagnosed.stdout)
        self.assertEqual(old.read_bytes(), before)
        migrated = self.wt('migrate', self.repo)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.assertEqual(json.loads(self.wt('identity', row['path'], '--json').stdout)['session'], 's1')

    def test_malformed_registry_fails_closed(self):
        self.state_file().write_text('{broken')
        before = self.state_file().read_bytes()
        out = self.wt('open', self.repo, '--agent', 'codex', '--session', 's1', '--json')
        self.assertEqual(json.loads(out.stdout)['status'], 'error')
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(before, self.state_file().read_bytes())

    def test_concurrent_open_keeps_every_session(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda n: self.wt('open', self.repo, '--agent', 'codex', '--session', f's{n}'), range(6)))
        self.assertTrue(all(r.returncode == 0 for r in results), [(r.stdout, r.stderr) for r in results])
        rows = json.loads(self.state_file().read_text())['sessions']
        self.assertEqual({r['session'] for r in rows}, {f's{n}' for n in range(6)})


class LockScopeTests(LifecycleFixture):
    def acquire(self, branch='main', session='s1', agent='codex', repo=None):
        return self.lock('acquire', repo or self.repo, '--agent', agent, '--session', session, '--branch', branch, '--json')

    def check(self, branch='main', session='s1', agent='codex', repo=None):
        return self.lock('check', repo or self.repo, '--agent', agent, '--session', session, '--branch', branch, '--json')

    def lock_file(self, branch='main'):
        return self.locks / f'{self.repo_id}.{hashlib.sha256(branch.encode()).hexdigest()}.lock'

    def test_canonical_lock_spans_worktrees_symlinks_and_multiple_branches(self):
        row = self.opened()
        self.assertEqual(self.acquire().returncode, 0)
        linked = self.check(repo=row['path'], session='s1')
        self.assertEqual(json.loads(linked.stdout)['status'], 'owned')
        self.assertEqual(json.loads(self.check(session='other').stdout)['status'], 'busy')
        symlink = Path(self.tmp) / 'alias'
        symlink.symlink_to(self.repo)
        self.assertEqual(json.loads(self.check(repo=symlink).stdout)['status'], 'owned')
        second = self.acquire(branch=row['branch'], session='s2')
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertEqual(len(list(self.locks.glob('*.lock'))), 2)
        self.assertEqual(json.loads(self.check(branch='unlocked').stdout)['status'], 'free')

    def test_strict_actor_and_session_on_check_release_and_renew(self):
        self.acquire()
        for actor, session in [('codex', 's2'), ('other', 's1')]:
            out = self.check(agent=actor, session=session)
            self.assertEqual(json.loads(out.stdout)['status'], 'busy')
            for command in ('release', 'renew'):
                result = self.lock(command, self.repo, '--agent', actor, '--session', session, '--branch', 'main')
                self.assertNotEqual(result.returncode, 0)
        missing = self.lock('release', self.repo, '--agent', 'codex', '--branch', 'main')
        self.assertNotEqual(missing.returncode, 0)
        self.assertTrue(self.lock_file().exists())
        release = self.lock('release', self.repo, '--agent', 'codex', '--session', 's1', '--branch', 'main')
        self.assertEqual(release.returncode, 0, release.stderr)
        self.assertFalse(self.lock_file().exists())

    def test_default_ttl_and_renewal(self):
        result = self.acquire()
        row = json.loads(result.stdout)['lock']
        self.assertGreater(row['expires_epoch'] - time.time(), 88 * 60)
        renewed = self.lock('renew', self.repo, '--agent', 'codex', '--session', 's1', '--ttl', '180', '--json')
        self.assertEqual(renewed.returncode, 0, renewed.stdout)
        self.assertGreater(json.loads(renewed.stdout)['lock']['expires_epoch'], row['expires_epoch'] + 89 * 60)

    def test_expired_is_never_implicitly_free_and_recovery_is_audited(self):
        self.acquire()
        data = json.loads(self.lock_file().read_text())
        data['expires_epoch'] = time.time() - 60
        self.lock_file().write_text(json.dumps(data))
        checked = self.check()
        self.assertEqual(json.loads(checked.stdout)['status'], 'expired')
        self.assertNotEqual(checked.returncode, 0)
        self.assertNotEqual(self.acquire(session='s2').returncode, 0)
        for command in ('renew', 'release'):
            self.assertNotEqual(self.lock(command, self.repo, '--agent', 'codex', '--session', 's1').returncode, 0)
        recover = self.lock('recover', self.repo, '--agent', 'other', '--session', 's2', '--reason', 'previous owner ended', '--json')
        self.assertEqual(recover.returncode, 0, recover.stdout)
        entries = [json.loads(line) for line in (self.locks / f'{self.repo_id}.audit.jsonl').read_text().splitlines()]
        self.assertEqual(entries[0]['event'], 'lock-recover')
        self.assertEqual(entries[0]['previous']['session'], 's1')
        self.assertEqual(json.loads(self.check(agent='other', session='s2').stdout)['status'], 'owned')

    def test_recovery_requires_reason_and_cannot_take_active_lock(self):
        self.acquire()
        for extra in ([], ['--reason', 'want it'], ['--force']):
            result = self.lock('recover', self.repo, '--agent', 'other', '--session', 's2', *extra)
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(self.check().stdout)['status'], 'owned')

    def test_malformed_lock_fails_closed_without_mutation(self):
        for payload in ('{bad', '{}', json.dumps({'version': 3, 'repo_id': self.repo_id, 'branch': 'main', 'agent': 'codex', 'session': 's1', 'expires_epoch': 'yesterday'})):
            self.lock_file().write_text(payload)
            result = self.check()
            self.assertEqual(json.loads(result.stdout)['status'], 'error')
            self.assertEqual(result.returncode, 2)
            self.assertNotEqual(self.acquire().returncode, 0)
            self.assertEqual(self.lock_file().read_text(), payload)

    def test_concurrent_same_branch_acquire_has_exactly_one_winner(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda n: self.acquire(session=f's{n}'), range(8)))
        self.assertEqual(sum(r.returncode == 0 for r in results), 1)
        self.assertEqual(len(list(self.locks.glob('*.lock'))), 1)
        winner = next(json.loads(r.stdout)['lock']['session'] for r in results if r.returncode == 0)
        self.assertEqual(json.loads(self.lock_file().read_text())['session'], winner)

    def test_concurrent_different_branch_locks_do_not_overwrite(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda n: self.acquire(branch=f'feature/{n}', session=f's{n}'), range(8)))
        self.assertTrue(all(r.returncode == 0 for r in results), [r.stdout for r in results])
        self.assertEqual(len(list(self.locks.glob('*.lock'))), 8)

    def test_legacy_lock_is_not_ignored_or_overwritten(self):
        old = self.locks / 'old_repo_path.lock'
        old.write_text(json.dumps({'repo': str(self.repo), 'agent': 'codex', 'session': 's1', 'branch': 'main', 'expires_epoch': time.time() + 3600, 'acquired': 'legacy'}))
        checked = self.check()
        self.assertEqual(json.loads(checked.stdout)['status'], 'error')
        before = old.read_bytes()
        self.assertNotEqual(self.acquire().returncode, 0)
        diagnose = self.lock('reconcile', self.repo, '--json')
        self.assertTrue(json.loads(diagnose.stdout)['read_only'])
        self.assertEqual(old.read_bytes(), before)
        migrated = self.lock('migrate', self.repo, '--agent', 'codex', '--session', 's1', '--branch', 'main', '--reason', 'upgrade', '--json')
        self.assertEqual(migrated.returncode, 0, migrated.stdout)
        self.assertEqual(json.loads(self.check().stdout)['status'], 'owned')


if __name__ == '__main__':
    unittest.main()
