from pathlib import Path
import json
import os
from test_pre_push_hook import PublishFixture, ROOT


class WrapperTests(PublishFixture):
    def test_explicit_rewrite_uses_exact_lease_and_confirms_remote(self):
        self.commit("published")
        self.git("push", "origin", "topic")
        self.git("reset", "--hard", self.first)
        replacement = self.commit("replacement")
        self.push("--rewrite", "origin", "HEAD:refs/heads/topic")
        self.assertEqual(self.remote_sha(), replacement)
        self.assertTrue(any(e["event"] == "remote-result" and e.get("outcome") == "accepted" for e in self.events()))
        # Wrapper does not leave a branch lock behind.
        p = self.command(str(ROOT / "bin/agent-lock"), "check", str(self.repo), "--branch", "topic", "--agent", "test-agent", "--session", "test-session", "--json")
        self.assertEqual(json.loads(p.stdout)["status"], "free")

    def test_stale_lease_race_cannot_overwrite_remote(self):
        replacement = self.commit("replacement")
        self.git("push", "origin", replacement + ":refs/heads/race-object")
        # The existing chained hook simulates a concurrent publisher after
        # agent-push reads ls-remote and our policy consumes its authorization.
        self.git("config", "core.hooksPath", "race-hooks")
        hooks = self.repo / "race-hooks"
        hooks.mkdir()
        hook = hooks / "pre-push"
        hook.write_text("#!/bin/sh\ncat >/dev/null\ngit --git-dir=" + str(self.remote) + " update-ref refs/heads/topic " + replacement + "\n")
        hook.chmod(0o755)
        self.install()
        self.git("reset", "--hard", self.first)
        desired = self.commit("desired")
        p = self.push("--rewrite", "origin", "HEAD:refs/heads/topic", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.remote_sha(), replacement)
        self.assertNotEqual(self.remote_sha(), desired)

    def test_deletions_require_explicit_wrapper(self):
        raw = self.git("push", "origin", ":refs/heads/topic", check=False)
        self.assertNotEqual(raw.returncode, 0)
        self.assertEqual(self.remote_sha(), self.first)
        self.push("--delete", "origin", ":refs/heads/topic")
        self.assertNotEqual(self.git("--git-dir", str(self.remote), "show-ref", "--verify", "refs/heads/topic", check=False).returncode, 0)
        self.assertTrue(any(e.get("outcome") == "accepted" for e in self.events()))

    def test_trunk_exception_is_narrow_and_logged(self):
        self.assertNotEqual(self.push("origin", "HEAD:refs/heads/main", check=False).returncode, 0)
        self.push("--allow-trunk", "--reason", "fixture recovery", "origin", "HEAD:refs/heads/main")
        self.assertEqual(self.remote_sha("main"), self.first)
        self.assertTrue(any(e["event"] == "trunk-exception" and e["reason"] == "fixture recovery" for e in self.events()))

    def test_remote_rejection_is_reported_without_success_claim(self):
        sha = self.commit("next")
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        p = self.push("origin", "HEAD:refs/heads/topic", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.remote_sha(), self.first)
        outcomes = [e for e in self.events() if e["event"] == "remote-result"]
        self.assertEqual(outcomes[-1]["outcome"], "rejected")
        self.assertEqual(outcomes[-1]["result"], "confirmed")

    def test_multiref_atomic_remote_rejection_changes_neither_ref(self):
        self.commit("next")
        hook = self.remote / "hooks/update"
        hook.write_text("#!/bin/sh\n[ \"$1\" != refs/heads/rejected ]\n")
        hook.chmod(0o755)
        p = self.push("origin", "HEAD:refs/heads/topic", "HEAD:refs/heads/rejected", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.remote_sha(), self.first)
        self.assertNotEqual(self.git("--git-dir", str(self.remote), "show-ref", "--verify", "refs/heads/rejected", check=False).returncode, 0)

    def test_noop_is_confirmed_without_unused_authorization(self):
        self.push("origin", "HEAD:refs/heads/topic")
        self.assertEqual(self.events()[-1]["outcome"], "unchanged")

    def test_missing_hook_rejected_before_publication(self):
        (self.repo / ".git/hooks/pre-push").unlink()
        self.commit("next")
        self.assertNotEqual(self.push("origin", "HEAD:refs/heads/topic", check=False).returncode, 0)
        self.assertEqual(self.remote_sha(), self.first)

    def authorization(self, updates, **overrides):
        import hashlib
        import time
        import uuid
        common = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()).resolve()
        directory = common / "agent-publish/authorizations"
        directory.mkdir(parents=True, exist_ok=True)
        token = directory / (uuid.uuid4().hex + ".json")
        data = dict(repo_id=hashlib.sha256(os.fsencode(common)).hexdigest(), remote="origin", url=str(self.remote),
                    actor="test-agent", session="test-session", operation_id=uuid.uuid4().hex,
                    updates=sorted(updates, key=lambda u: u["ref"]), rewrite=True, delete=False,
                    allow_trunk=False, reason="", issued_at=time.time(), expires_at=time.time()+300)
        data.update(overrides)
        token.write_text(json.dumps(data))
        return token

    def test_authorization_cannot_broaden_ref_or_replay_after_rejection(self):
        sha = self.commit("next")
        updates = [dict(ref="refs/heads/topic", old=self.first, new=sha)]
        token = self.authorization(updates)
        env = dict(self.env, AGENT_PUSH_AUTH=str(token))
        self.assertNotEqual(self.git("push", "origin", "HEAD:refs/heads/other", env=env, check=False).returncode, 0)
        self.assertFalse(token.exists())
        self.assertNotEqual(self.git("push", "origin", "topic", env=env, check=False).returncode, 0)
        self.assertEqual(self.remote_sha(), self.first)

    def test_crash_leftover_authorization_expires_and_is_consumed(self):
        import time
        sha = self.commit("next")
        token = self.authorization([dict(ref="refs/heads/topic", old=self.first, new=sha)],
                                   issued_at=time.time()-600, expires_at=time.time()-300)
        p = self.git("push", "origin", "topic", env=dict(self.env, AGENT_PUSH_AUTH=str(token)), check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("expired", p.stderr)
        self.assertFalse(token.exists())
        self.assertEqual(self.remote_sha(), self.first)

    def test_preowned_lock_is_preserved_after_wrapper(self):
        self.command(str(ROOT / "bin/agent-lock"), "acquire", str(self.repo), "--branch", "topic", "--agent", "test-agent", "--session", "test-session")
        self.commit("next")
        self.push("origin", "HEAD:refs/heads/topic")
        p = self.command(str(ROOT / "bin/agent-lock"), "check", str(self.repo), "--branch", "topic", "--agent", "test-agent", "--session", "test-session", "--json")
        self.assertEqual(json.loads(p.stdout)["status"], "owned")
