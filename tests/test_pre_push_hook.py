"""Real Git push fixtures: no external remotes or production credentials."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PublishFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.remote = self.base / "remote.git"
        self.home = self.base / "home"
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_CONFIG_NOSYSTEM="1", AGENT_ACTOR="test-agent", AGENT_SESSION="test-session",
                        AGENT_CHANGESET_DIR=str(self.base / "events"), AGENT_LOCK_DIR=str(self.base / "locks"),
                        AGENT_WORKTREE_DIR=str(self.base / "worktrees"))
        for key in list(self.env):
            if key.startswith("GIT_") and key not in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"):
                self.env.pop(key)
        self.command("git", "init", "--bare", "-q", str(self.remote), cwd=self.base)
        self.repo.mkdir()
        self.git("init", "-q", "-b", "topic")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "agent.pushVerifyMode", "optional")
        self.git("remote", "add", "origin", str(self.remote))
        self.first = self.commit("first")
        self.git("push", "-q", "origin", "HEAD:refs/heads/topic")
        self.install()

    def command(self, *args, cwd=None, env=None, input=None, check=True):
        p = subprocess.run(args, cwd=cwd or self.repo, env=env or self.env,
                           input=input, text=True, capture_output=True)
        if check:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def git(self, *args, **kwargs):
        return self.command("git", *args, **kwargs)

    def install(self, repo=None):
        return self.command(str(ROOT / "install-hooks.sh"), str(repo or self.repo))

    def commit(self, content):
        (self.repo / "content").write_text(content + "\n")
        self.git("add", "content")
        self.git("commit", "-qm", content)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def remote_sha(self, ref="topic"):
        return self.git("--git-dir", str(self.remote), "rev-parse", "refs/heads/" + ref).stdout.strip()

    def events(self):
        return [json.loads(line) for f in (self.base / "events").glob("*.jsonl") for line in f.read_text().splitlines()]

    def push(self, *args, **kwargs):
        return self.command(str(ROOT / "bin/agent-push"), *args, **kwargs)


class PushHookTests(PublishFixture):
    def test_actual_plain_force_is_blocked(self):
        published = self.commit("published")
        self.git("push", "origin", "HEAD:refs/heads/topic")
        self.git("reset", "--hard", self.first)
        self.commit("divergent")
        p = self.git("push", "--force", "origin", "HEAD:refs/heads/topic", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("non-fast-forward", p.stderr)
        self.assertEqual(self.remote_sha(), published)
        self.assertTrue(any(e["event"] == "reject" for e in self.events()))

    def test_raw_identified_fast_forward_is_compatible_and_indeterminate(self):
        sha = self.commit("next")
        self.git("push", "origin", "HEAD:refs/heads/topic")
        self.assertEqual(self.remote_sha(), sha)
        events = self.events()
        self.assertTrue(any(e["event"] == "hook-accepted" for e in events))
        self.assertTrue(any(e["event"] == "remote-result" and e["result"] == "indeterminate" for e in events))
        self.assertFalse(any(e.get("outcome") == "accepted" for e in events))
        common = self.git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
        self.assertEqual(events[-1]["repo_id"], hashlib.sha256(os.fsencode(Path(common).resolve())).hexdigest())

    def test_trunk_and_legacy_skip_flags_cannot_bypass(self):
        for env in (self.env, dict(self.env, ALLOW_MAIN_PUSH="1"), dict(self.env, AGENT_SKIP_PUSH_CHECKS="1")):
            p = self.git("push", "origin", "HEAD:refs/heads/main", env=env, check=False)
            self.assertNotEqual(p.returncode, 0, p.stderr)
        self.assertNotEqual(self.git("--git-dir", str(self.remote), "rev-parse", "refs/heads/main", check=False).returncode, 0)

    def test_unidentified_push_fails(self):
        env = dict(self.env)
        env.pop("AGENT_ACTOR")
        env.pop("AGENT_SESSION")
        self.commit("next")
        p = self.git("push", "origin", "topic", env=env, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.remote_sha(), self.first)

    def test_owned_lock_allowed_conflicting_and_malformed_locks_fail_closed(self):
        sha = self.commit("next")
        self.command(str(ROOT / "bin/agent-lock"), "acquire", str(self.repo), "--branch", "topic", "--agent", "test-agent", "--session", "test-session")
        self.git("push", "origin", "topic")
        self.assertEqual(self.remote_sha(), sha)
        self.commit("third")
        env = dict(self.env, AGENT_SESSION="other")
        self.assertNotEqual(self.git("push", "origin", "topic", env=env, check=False).returncode, 0)
        for file in (self.base / "locks").rglob("*.lock"):
            file.write_text("malformed")
        self.assertNotEqual(self.git("push", "origin", "topic", check=False).returncode, 0)

    def test_verify_exact_sha_in_clean_checkout_for_multiple_refs(self):
        first = self.first
        second = self.commit("second")
        self.git("branch", "older", first)
        verifier = self.base / "verify.py"
        results = self.base / "verified.jsonl"
        verifier.write_text("import json, pathlib, subprocess, sys\n"
                            "sha=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()\n"
                            "assert sha == sys.argv[1]\n"
                            "assert not subprocess.check_output(['git','status','--porcelain'],text=True).strip()\n"
                            "data=pathlib.Path('content').read_text().strip()\n"
                            f"with open({str(results)!r},'a') as f: f.write(json.dumps([sha,data])+chr(10))\n")
        self.git("config", "agent.pushVerifyMode", "required")
        self.git("config", "agent.pushVerify", "python3 " + str(verifier))
        (self.repo / "content").write_text("dirty live tree")
        self.git("push", "origin", "older:refs/heads/older", "HEAD:refs/heads/topic")
        checked = [json.loads(line) for line in results.read_text().splitlines()]
        self.assertEqual(sorted(checked), sorted([[first, "first"], [second, "second"]]))
        self.assertEqual((self.repo / "content").read_text(), "dirty live tree")

    def test_required_verification_failure_blocks_entire_push(self):
        self.commit("next")
        self.git("config", "agent.pushVerifyMode", "required")
        self.git("config", "agent.pushVerify", "/bin/false")
        p = self.git("push", "origin", "HEAD:refs/heads/topic", "HEAD:refs/heads/new", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.remote_sha(), self.first)
        self.assertNotEqual(self.git("--git-dir", str(self.remote), "show-ref", "--verify", "refs/heads/new", check=False).returncode, 0)
        self.assertTrue(any(e["event"] == "verification" and e["result"] == "failed" for e in self.events()))

    def test_config_mode_and_required_missing_verifier_are_explicit(self):
        self.commit("next")
        self.git("config", "--unset", "agent.pushVerifyMode")
        self.assertNotEqual(self.git("push", "origin", "topic", check=False).returncode, 0)
        self.git("config", "agent.pushVerifyMode", "required")
        self.assertNotEqual(self.git("push", "origin", "topic", check=False).returncode, 0)

    def test_verifier_timeout_blocks_and_is_audited(self):
        self.commit("next")
        self.git("config", "agent.pushVerifyMode", "required")
        self.git("config", "agent.pushVerifyTimeout", "1")
        self.git("config", "agent.pushVerify", "python3 -c 'import time; time.sleep(30)'")
        p = self.git("push", "origin", "topic", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("timed out", p.stderr)
        self.assertEqual(self.remote_sha(), self.first)
        self.assertTrue(any(e["event"] == "verification" and "timed out" in e.get("reason", "") for e in self.events()))

    def test_verifier_does_not_inherit_ambient_credentials_or_authorization(self):
        self.commit("next")
        verifier = self.base / "verify-env.py"
        verifier.write_text("import os\n"
                            "assert 'GH_TOKEN' not in os.environ\n"
                            "assert 'GOOGLE_APPLICATION_CREDENTIALS' not in os.environ\n"
                            "assert 'AWS_SECRET_ACCESS_KEY' not in os.environ\n"
                            "assert 'AGENT_SESSION' not in os.environ\n"
                            "assert 'AGENT_PUSH_AUTH' not in os.environ\n"
                            "assert os.environ['AGENT_VERIFY_REF'] == 'refs/heads/topic'\n"
                            "assert os.environ['EXPLICIT_TEST_VAR'] == 'reviewed'\n")
        self.git("config", "agent.pushVerifyMode", "required")
        self.git("config", "agent.pushVerify", "python3 " + str(verifier))
        self.git("config", "agent.pushVerifyEnv", "EXPLICIT_TEST_VAR")
        env = dict(self.env, GH_TOKEN="dummy-fixture-not-a-secret", GOOGLE_APPLICATION_CREDENTIALS="dummy-path",
                   AWS_SECRET_ACCESS_KEY="dummy-fixture-not-a-secret", EXPLICIT_TEST_VAR="reviewed")
        self.git("push", "origin", "topic", env=env)
