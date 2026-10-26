"""Regressions for the v3.2 package: session collector, branch sync, conflict guard.

Everything runs against local repositories and local bare remotes. No external
remote and no production credential is involved.
"""
import json
import os
from pathlib import Path
import time
import unittest

from test_agent_worktree import LifecycleFixture
from test_pre_push_hook import PublishFixture, ROOT

VERIFIER_DOCS = ROOT / "bin/agent-verify-docs"
VERIFIER_TESTS = ROOT / "bin/agent-verify-tests"


class AuditReasonTests(LifecycleFixture):
    """The audit trail names why a lock was released."""

    def test_the_auto_release_records_its_reason(self):
        opened = self.opened('reason')
        self.lock('acquire', self.repo, '--branch', opened['branch'],
                  '--agent', 'codex', '--session', 'reason', '--json')
        closed = self.wt('close', self.repo, '--agent', 'codex', '--session', 'reason', '--json')
        self.assertEqual(closed.returncode, 0, closed.stderr + closed.stdout)
        audit_file = Path(self.locks) / (self.repo_id + '.audit.jsonl')
        events = [json.loads(line) for line in audit_file.read_text().splitlines()]
        release = next(e for e in events if e['event'] == 'lock-release')
        self.assertEqual(release['reason'], 'auto-released on worktree close')
        self.assertEqual(release['actor'], 'codex')
        self.assertEqual(release['session'], 'reason')


class BranchSyncTests(LifecycleFixture):
    """A session may rename its branch inside the managed worktree."""

    def test_a_renamed_branch_is_adopted_and_its_lock_migrates(self):
        opened = self.opened('rename')
        self.lock('acquire', self.repo, '--branch', opened['branch'],
                  '--agent', 'codex', '--session', 'rename', '--json')
        # The agent switches to a Conventional Commits branch inside the worktree.
        self.fixture.run_git(opened['path'], 'checkout', '-q', '-b', 'feat/nueva-rama')
        identity = self.wt('identity', opened['path'], '--json')
        self.assertEqual(identity.returncode, 0, identity.stdout + identity.stderr)
        self.assertEqual(json.loads(identity.stdout)['branch'], 'feat/nueva-rama')
        rows = json.loads(self.state_file().read_text())['sessions']
        self.assertEqual(rows[0]['branch'], 'feat/nueva-rama')
        self.assertIn(opened['branch'], rows[0]['previous_branches'])
        # The lock travelled with the session: the old name is free and the
        # new name is owned.
        self.assertEqual(json.loads(self.lock('check', self.repo, '--branch', opened['branch'],
                                              '--agent', 'codex', '--session', 'rename', '--json').stdout)['status'], 'free')
        self.assertEqual(json.loads(self.lock('check', self.repo, '--branch', 'feat/nueva-rama',
                                              '--agent', 'codex', '--session', 'rename', '--json').stdout)['status'], 'owned')
        audit_file = Path(self.state) / (self.repo_id + '.audit.jsonl')
        events = [json.loads(line) for line in audit_file.read_text().splitlines()]
        sync = next(e for e in events if e['event'] == 'worktree-branch-sync')
        self.assertEqual(sync['previous']['old_branch'], opened['branch'])
        self.assertEqual(sync['previous']['new_branch'], 'feat/nueva-rama')
        self.assertTrue(sync['previous']['lock_moved'])

    def test_a_detached_head_is_still_refused(self):
        opened = self.opened('detached')
        self.fixture.run_git(opened['path'], 'checkout', '--detach', '-q')
        identity = self.wt('identity', opened['path'], '--json')
        self.assertNotEqual(identity.returncode, 0)
        self.assertIn('branch changed', json.loads(identity.stdout)['error'])

    def test_close_releases_the_lock_of_the_abandoned_branch_name(self):
        opened = self.opened('leftover')
        self.lock('acquire', self.repo, '--branch', opened['branch'],
                  '--agent', 'codex', '--session', 'leftover', '--json')
        self.fixture.run_git(opened['path'], 'checkout', '-q', '-b', 'fix/otro-nombre')
        self.wt('identity', opened['path'], '--json')
        closed = self.wt('close', self.repo, '--agent', 'codex', '--session', 'leftover', '--json')
        self.assertEqual(closed.returncode, 0, closed.stderr + closed.stdout)
        self.assertFalse(list(self.locks.glob('*.lock')))


class BranchIntegrationTests(LifecycleFixture):
    """A branch whose work is already merged stops being a collision target."""

    def test_a_squash_merged_branch_is_not_a_collision(self):
        # The session publishes its branch, the pull request is squash-merged
        # and the session forgets to close its worktree.
        first = self.opened('merged')
        (Path(first['path']) / 'shared.txt').write_text('one\n')
        self.fixture.run_git(first['path'], 'add', 'shared.txt')
        self.fixture.run_git(first['path'], 'commit', '-qm', 'change')
        self.fixture.run_git(self.repo, 'push', '-q', 'origin', f'{first["branch"]}:refs/heads/topic')
        # The squash merge lands the same content on the default branch.
        self.fixture.run_git(self.repo, 'checkout', '-q', '-b', 'squash', 'origin/main')
        (self.repo / 'shared.txt').write_text('one\n')
        self.fixture.run_git(self.repo, 'add', 'shared.txt')
        self.fixture.run_git(self.repo, 'commit', '-qm', 'squash merge')
        self.fixture.run_git(self.repo, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.fixture.run_git(self.repo, 'fetch', '-q', 'origin', 'main:refs/remotes/origin/main')
        # A second session starts from the new main and edits the same file.
        second = self.opened('second')
        (Path(second['path']) / 'shared.txt').write_text('two\n')
        self.fixture.run_git(second['path'], 'add', 'shared.txt')
        self.fixture.run_git(second['path'], 'commit', '-qm', 'change')
        result = self.wt('radar', self.repo, '--session', 'second', '--json')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        # The merged session is not a target anymore, so the only remaining
        # comparison is against the default branch itself.
        targets = [c['target'] for c in payload['collisions']]
        self.assertNotIn(first['branch'], targets)
        self.assertNotIn(first['session'], [s['session'] for s in payload['sessions']])

    def test_a_session_without_commits_is_not_a_collision(self):
        first = self.opened('empty')
        second = self.opened('second')
        (Path(second['path']) / 'new.txt').write_text('two\n')
        self.fixture.run_git(second['path'], 'add', 'new.txt')
        self.fixture.run_git(second['path'], 'commit', '-qm', 'change')
        result = self.wt('radar', self.repo, '--session', 'second', '--json')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertNotIn(first['session'], [s['session'] for s in payload['sessions']])


class CollectorTests(LifecycleFixture):
    """agent-worktree gc closes the sessions that are already integrated."""

    def test_gc_closes_an_integrated_session_and_releases_its_lock(self):
        opened = self.opened('integrated')
        (Path(opened['path']) / 'shared.txt').write_text('one\n')
        self.fixture.run_git(opened['path'], 'add', 'shared.txt')
        self.fixture.run_git(opened['path'], 'commit', '-qm', 'change')
        self.fixture.run_git(self.repo, 'push', '-q', 'origin', f'{opened["branch"]}:refs/heads/topic')
        self.lock('acquire', self.repo, '--branch', opened['branch'],
                  '--agent', 'codex', '--session', 'integrated', '--json')
        # The pull request is merged with a squash commit.
        self.fixture.run_git(self.repo, 'checkout', '-q', '-b', 'squash', 'origin/main')
        (self.repo / 'shared.txt').write_text('one\n')
        self.fixture.run_git(self.repo, 'add', 'shared.txt')
        self.fixture.run_git(self.repo, 'commit', '-qm', 'squash merge')
        self.fixture.run_git(self.repo, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.fixture.run_git(self.repo, 'fetch', '-q', 'origin', 'main:refs/remotes/origin/main')
        result = self.wt('gc', self.repo, '--json')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(len(payload['cleaned']), 1)
        self.assertEqual(payload['cleaned'][0]['session'], 'integrated')
        self.assertEqual(json.loads(self.state_file().read_text())['sessions'], [])
        self.assertFalse(list(self.locks.glob('*.lock')))
        self.assertFalse(Path(opened['path']).exists())

    def test_gc_keeps_a_session_with_unmerged_work(self):
        opened = self.opened('pending')
        (Path(opened['path']) / 'shared.txt').write_text('one\n')
        self.fixture.run_git(opened['path'], 'add', 'shared.txt')
        self.fixture.run_git(opened['path'], 'commit', '-qm', 'change')
        result = self.wt('gc', self.repo, '--json')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['cleaned'], [])
        self.assertEqual(payload['remaining'], 1)
        self.assertTrue(Path(opened['path']).exists())

    def test_gc_keeps_a_session_with_uncommitted_work(self):
        opened = self.opened('dirty')
        self.fixture.run_git(self.repo, 'checkout', '-q', '-b', 'squash', 'origin/main')
        (self.repo / 'shared.txt').write_text('one\n')
        self.fixture.run_git(self.repo, 'add', 'shared.txt')
        self.fixture.run_git(self.repo, 'commit', '-qm', 'squash merge')
        self.fixture.run_git(self.repo, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.fixture.run_git(self.repo, 'fetch', '-q', 'origin', 'main:refs/remotes/origin/main')
        (Path(opened['path']) / 'dirty.txt').write_text('uncommitted\n')
        result = self.wt('gc', self.repo, '--json')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)['cleaned'], [])
        self.assertTrue(Path(opened['path']).exists())


class ConflictGuardTests(PublishFixture):
    """Unresolved Git conflict markers never reach the remote."""

    def publish_with_markers(self, name, content):
        (self.repo / name).write_text(content)
        self.git("add", name)
        self.git("commit", "-qm", "documento con conflicto")
        return self.push("origin", "HEAD:refs/heads/topic", check=False)

    def test_the_docs_verifier_blocks_a_document_with_conflict_markers(self):
        self.git("config", "agent.pushVerifyMode", "required")
        self.git("config", "agent.pushVerify", str(VERIFIER_DOCS))
        result = self.publish_with_markers(
            "2026-10-26_Documento_Con_Conflicto.md",
            "texto\n\n<<<<<<< HEAD\nnuevo\n=======\nviejo\n>>>>>>> origin/main\n")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.remote_sha(), self.first)
        failures = [e for e in self.events() if e["event"] == "verification" and e.get("result") == "failed"]
        self.assertTrue(failures)

    def test_the_docs_verifier_blocks_markers_in_an_excluded_index(self):
        # The documentation linter skips README.md and AI_KNOWLEDGE_MAP.md, and
        # those are the files every session regenerates in parallel.
        self.git("config", "agent.pushVerifyMode", "required")
        self.git("config", "agent.pushVerify", str(VERIFIER_DOCS))
        result = self.publish_with_markers(
            "AI_KNOWLEDGE_MAP.md",
            "| fecha | titulo |\n|---|---|\n<<<<<<< HEAD\n| a | b |\n=======\n| c | d |\n>>>>>>> origin/main\n")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.remote_sha(), self.first)

    def test_the_tests_verifier_blocks_markers_in_a_published_document(self):
        (self.repo / "tests").mkdir(exist_ok=True)
        (self.repo / "tests" / "test_sample.py").write_text(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "suite")
        self.git("config", "agent.pushVerifyMode", "required")
        self.git("config", "agent.pushVerify", str(VERIFIER_TESTS) + " tests")
        (self.repo / "index.md").write_text("<<<<<<< HEAD\nnuevo\n=======\nviejo\n>>>>>>> origin/main\n")
        self.git("add", "index.md")
        self.git("commit", "-qm", "documento con conflicto")
        result = self.push("origin", "HEAD:refs/heads/topic", check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.remote_sha(), self.first)


if __name__ == '__main__':
    unittest.main()
