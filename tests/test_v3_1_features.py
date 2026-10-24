"""Regressions for the v3.1 package: lifecycle, collision radar, verifier queue.

Everything runs against local repositories and local bare remotes. No external
remote and no production credential is involved.
"""
import json
import os
from pathlib import Path
import time

from test_agent_worktree import LifecycleFixture
from test_pre_push_hook import PublishFixture, ROOT


class LifecycleCloseTests(LifecycleFixture):
    """F1: closing a worktree also releases the lock it owns."""

    def test_close_releases_the_lock_owned_by_the_same_session(self):
        opened = self.opened('closing')
        acquired = self.lock('acquire', self.repo, '--branch', opened['branch'],
                              '--agent', 'codex', '--session', 'closing', '--json')
        self.assertEqual(acquired.returncode, 0, acquired.stdout)
        self.assertTrue(list(self.locks.glob('*.lock')))
        closed = self.wt('close', self.repo, '--agent', 'codex', '--session', 'closing', '--json')
        self.assertEqual(closed.returncode, 0, closed.stderr + closed.stdout)
        self.assertFalse(list(self.locks.glob('*.lock')))
        self.assertEqual(json.loads(closed.stdout)['lock']['status'], 'released')
        audit_file = Path(self.locks) / (self.repo_id + '.audit.jsonl')
        events = [json.loads(line) for line in audit_file.read_text().splitlines()]
        self.assertIn('lock-release', [e['event'] for e in events])

    def test_close_releases_an_expired_lock_of_the_same_session(self):
        opened = self.opened('expired')
        self.lock('acquire', self.repo, '--branch', opened['branch'],
                  '--agent', 'codex', '--session', 'expired', '--json')
        lock_file = next(self.locks.glob('*.lock'))
        payload = json.loads(lock_file.read_text())
        payload['expires_epoch'] = time.time() - 60
        lock_file.write_text(json.dumps(payload))
        closed = self.wt('close', self.repo, '--agent', 'codex', '--session', 'expired', '--json')
        self.assertEqual(closed.returncode, 0, closed.stderr + closed.stdout)
        self.assertEqual(json.loads(closed.stdout)['lock']['status'], 'released')
        self.assertTrue(json.loads(closed.stdout)['lock']['expired'])
        self.assertFalse(list(self.locks.glob('*.lock')))

    def test_close_never_releases_a_lock_of_another_session(self):
        opened = self.opened('owner')
        self.lock('acquire', self.repo, '--branch', opened['branch'],
                  '--agent', 'other', '--session', 'other-session', '--json')
        before = next(self.locks.glob('*.lock')).read_bytes()
        closed = self.wt('close', self.repo, '--agent', 'codex', '--session', 'owner', '--json')
        self.assertEqual(closed.returncode, 0, closed.stderr + closed.stdout)
        self.assertEqual(json.loads(closed.stdout)['lock']['status'], 'foreign')
        self.assertEqual(next(self.locks.glob('*.lock')).read_bytes(), before)

    def test_close_completes_when_the_worktree_directory_disappeared(self):
        # A direct `git worktree remove` leaves the record active with no
        # directory. The record must not be dropped until the commit of its
        # branch is proven preserved.
        opened = self.opened('vanished')
        oid = self.commit(opened['path'])
        self.fixture.run_git(self.repo, 'push', '-q', 'origin', f'{oid}:refs/heads/published')
        self.fixture.run_git(self.repo, 'worktree', 'remove', opened['path'])
        self.assertFalse(Path(opened['path']).exists())
        closed = self.wt('close', self.repo, '--agent', 'codex', '--session', 'vanished', '--json')
        self.assertEqual(closed.returncode, 0, closed.stderr + closed.stdout)
        payload = json.loads(closed.stdout)
        self.assertTrue(payload['branch_retained'])
        self.assertEqual(payload['commit'], oid)
        self.assertEqual(json.loads(self.state_file().read_text())['sessions'], [])
        audit_file = Path(self.state) / (self.repo_id + '.audit.jsonl')
        events = [json.loads(line) for line in audit_file.read_text().splitlines()]
        close_event = next(e for e in events if e['event'] == 'worktree-close')
        self.assertTrue(close_event['previous']['worktree_absent'])

    def test_close_refuses_when_the_commit_cannot_be_proven_preserved(self):
        opened = self.opened('localonly')
        oid = self.commit(opened['path'])
        # The branch itself disappears: the commit of the session has no
        # reference left, so the record must be retained.
        self.fixture.run_git(self.repo, 'worktree', 'remove', opened['path'])
        self.fixture.run_git(self.repo, 'branch', '-D', opened['branch'])
        self.assertNotEqual(self.fixture.run_git(self.repo, 'rev-parse', opened['branch']).returncode, 0)
        closed = self.wt('close', self.repo, '--agent', 'codex', '--session', 'localonly', '--json')
        self.assertNotEqual(closed.returncode, 0)
        self.assertIn('preservation unproven', json.loads(closed.stdout)['error'])
        # The record is retained, so the commit keeps a reference.
        rows = json.loads(self.state_file().read_text())['sessions']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['session'], 'localonly')


class RadarTests(LifecycleFixture):
    """F2: the collision radar replays merges in memory."""

    def write(self, path, name, content):
        target = Path(path) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.fixture.run_git(path, 'add', name)
        self.fixture.run_git(path, 'commit', '-qm', 'change ' + name)

    def test_radar_is_clean_when_the_sessions_touch_different_files(self):
        first = self.opened('one')
        second = self.opened('two')
        self.write(first['path'], 'alpha.txt', 'alpha\n')
        self.write(second['path'], 'beta.txt', 'beta\n')
        result = self.wt('radar', self.repo, '--session', 'one', '--json')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['status'], 'clean')
        self.assertEqual(payload['collisions'], [])

    def test_radar_reports_a_content_collision_with_another_session(self):
        first = self.opened('one')
        second = self.opened('two')
        self.write(first['path'], 'shared.txt', 'one\n')
        self.fixture.run_git(first['path'], 'push', '-q', 'origin', 'HEAD:refs/heads/base-one')
        # Both sessions start from the same base and edit the same line.
        self.write(second['path'], 'shared.txt', 'two\n')
        self.write(first['path'], 'shared.txt', 'one changed\n')
        result = self.wt('radar', self.repo, '--session', 'one', '--json')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['status'], 'conflict')
        collisions = [c for c in payload['collisions'] if c['target'] == second['branch']]
        self.assertEqual(len(collisions), 1)
        kinds = {p['type'] for p in collisions[0]['paths']}
        self.assertTrue(kinds & {'content', 'add/add'}, collisions)

    def test_radar_reports_a_structural_add_add_collision(self):
        first = self.opened('one')
        second = self.opened('two')
        self.write(first['path'], 'new.txt', 'from one\n')
        self.write(second['path'], 'new.txt', 'from two\n')
        result = self.wt('radar', self.repo, '--session', 'one', '--json')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        collisions = [c for c in payload['collisions'] if c['target'] == second['branch']]
        self.assertEqual(len(collisions), 1)
        self.assertIn('add/add', {p['type'] for p in collisions[0]['paths']})

    def test_radar_does_not_touch_any_working_tree(self):
        first = self.opened('one')
        self.write(first['path'], 'dirty.txt', 'uncommitted\n')
        before = self.fixture.run_git(first['path'], 'status', '--porcelain').stdout
        result = self.wt('radar', self.repo, '--session', 'one', '--json')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.fixture.run_git(first['path'], 'status', '--porcelain').stdout, before)


class VerifyQueueTests(PublishFixture):
    """F3: the verifiers of one repository run in a FIFO queue."""
    def test_two_concurrent_publications_serialize_the_verifier(self):
        verifier = self.base / "slow-verify.sh"
        verifier.write_text(
            "#!/bin/sh\n"
            "set -e\n"
            "sleep 2\n"
            'test "$(git rev-parse HEAD)" = "$(git rev-parse --verify "$1^{commit}")"\n'
        )
        verifier.chmod(0o755)
        self.git("config", "agent.pushVerifyMode", "required")
        self.git("config", "agent.pushVerify", str(verifier))
        self.commit("next")
        import subprocess
        import threading
        results = {}

        def publish(name):
            results[name] = self.push("origin", "HEAD:refs/heads/" + name, check=False)

        threads = [threading.Thread(target=publish, args=(name,)) for name in ("topic-a", "topic-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for name, result in results.items():
            self.assertEqual(result.returncode, 0, name + ": " + result.stdout + result.stderr)
        verifications = [e for e in self.events() if e["event"] == "verification" and e.get("result") == "passed"]
        self.assertEqual(len(verifications), 2)
        waits = sorted(e.get("queue_wait_ms", 0) for e in verifications)
        # The second publication waited for the first one to finish.
        self.assertGreater(waits[-1], 0, verifications)


class PushRadarNoticeTests(PublishFixture):
    """F2: agent-push warns about collisions without refusing the publication."""

    def test_a_collision_against_the_default_branch_is_reported_and_published(self):
        # The published branch and the remote default diverge on the same
        # file, so the merge will collide. The notice is informative: the
        # publication still happens, because publishing is what preserves the
        # work in the remote.
        self.push("--allow-trunk", "--reason", "fixture default branch", "origin", "HEAD:refs/heads/main")
        self.git("--git-dir", str(self.remote), "symbolic-ref", "HEAD", "refs/heads/main")
        self.git("fetch", "-q", "origin", "main:refs/remotes/origin/main")
        # The remote default gets a different content on the same file.
        self.git("checkout", "-q", "-b", "other", "origin/main")
        (self.repo / "content").write_text("diverged on the remote default\n")
        self.git("add", "content")
        self.git("commit", "-qm", "remote side")
        self.push("--allow-trunk", "--reason", "fixture default branch", "origin", "HEAD:refs/heads/main")
        # The session branch goes back to the base and edits the same file.
        self.git("checkout", "-q", "topic")
        self.git("reset", "-q", "--hard", self.first)
        (self.repo / "content").write_text("diverged on my side\n")
        self.git("add", "content")
        self.git("commit", "-qm", "my side")
        self.git("fetch", "-q", "origin", "main:refs/remotes/origin/main")
        result = self.push("origin", "HEAD:refs/heads/topic", check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("collision detected", result.stderr)
        self.assertIn("coordinate before merging", result.stderr)
        self.assertTrue(any(e["event"] == "collision-radar" for e in self.events()))
        # The publication happened anyway.
        self.assertEqual(self.remote_sha(), self.git("rev-parse", "HEAD").stdout.strip())

    def test_a_clean_publication_does_not_report_a_collision(self):
        self.commit("next")
        result = self.push("origin", "HEAD:refs/heads/topic", check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("collision detected", result.stderr)
        self.assertFalse(any(e["event"] == "collision-radar" for e in self.events()))
