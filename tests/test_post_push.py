"""Post-push callbacks only run after an authorized, confirmed publication."""
import hashlib
import json
import os
from pathlib import Path
import sys

from test_pre_push_hook import PublishFixture


class PostPushTests(PublishFixture):
    def callback(self, output, *, fail=False):
        script = self.base / "callback.py"
        script.write_text(
            "import json, os, pathlib, sys\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
            "'payload': json.load(sys.stdin), 'env': dict(os.environ)}))\n"
            + ("sys.exit(9)\n" if fail else "")
        )
        self.git("config", "agent.postPush", f"{sys.executable} {script} {output}")
        return script

    def test_callback_gets_payload_filtered_environment_and_repository_provenance(self):
        output = self.base / "callback.json"
        self.callback(output)
        callback_env = dict(self.env, GITHUB_TOKEN="provider-token-sentinel",
                            AGENT_POST_PUSH_REPOSITORY_ID="spoofed")
        next_sha = self.commit("post-push success")
        result = self.push("origin", "HEAD:refs/heads/topic", env=callback_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(output.read_text())
        payload = data["payload"]
        self.assertEqual(payload["remote"], "origin")
        self.assertEqual(payload["url"], str(self.remote))
        self.assertEqual(payload["updates"], [dict(ref="refs/heads/topic", old=self.first, new=next_sha)])
        env = data["env"]
        common = self.git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
        expected_id = hashlib.sha256(os.fsencode(Path(common).resolve())).hexdigest()
        self.assertEqual(env["AGENT_POST_PUSH_REPOSITORY_ID"], expected_id)
        self.assertEqual(env["PATH"], self.env["PATH"])
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertTrue(any(e["event"] == "post-push" and e["result"] == "success" for e in self.events()))

    def test_callback_failure_reports_failure_but_remote_remains_accepted(self):
        output = self.base / "callback-failure.json"
        self.callback(output, fail=True)
        next_sha = self.commit("callback failure")
        result = self.push("origin", "HEAD:refs/heads/topic", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("post-push callback failed after remote accepted", result.stderr)
        self.assertEqual(self.remote_sha(), next_sha)
        events = self.events()
        remote_results = [e for e in events if e["event"] == "remote-result"]
        self.assertEqual(remote_results[-1]["outcome"], "accepted")
        self.assertEqual(remote_results[-1]["result"], "confirmed")
        self.assertTrue(any(e["event"] == "post-push" and e["result"] == "failure" for e in events))

    def test_rejected_push_does_not_run_callback(self):
        output = self.base / "callback-rejected.json"
        self.callback(output)
        next_sha = self.commit("rejected trunk")
        result = self.push("origin", "HEAD:refs/heads/main", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        self.assertEqual(self.remote_sha(), self.first)
        self.assertFalse(any(e["event"] == "post-push" for e in self.events()))

    def test_verify_builtin_provenance_cannot_be_spoofed(self):
        output = self.base / "verify.json"
        verifier = self.base / "verify.py"
        verifier.write_text(
            "import json, os, pathlib, sys\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps(dict(os.environ)))\n"
        )
        self.git("config", "agent.pushVerifyMode", "required")
        self.git("config", "agent.pushVerify", f"{sys.executable} {verifier} {output}")
        self.git("config", "agent.pushVerifyEnv", "DOCS_LINT")
        env = dict(self.env, DOCS_LINT="allowed", AGENT_VERIFY_REPOSITORY_ID="spoofed")
        next_sha = self.commit("verify provenance")
        result = self.push("origin", "HEAD:refs/heads/topic", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        verify_env = json.loads(output.read_text())
        common = self.git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
        expected_id = hashlib.sha256(os.fsencode(Path(common).resolve())).hexdigest()
        self.assertEqual(verify_env["AGENT_VERIFY_REPOSITORY_ID"], expected_id)
        self.assertEqual(verify_env["AGENT_VERIFY_SOURCE"], str(self.repo.resolve()))
        self.assertEqual(verify_env["AGENT_VERIFY_REMOTE_URL"], str(self.remote))
        self.assertEqual(verify_env["AGENT_VERIFY_SHA"], next_sha)
        self.assertEqual(verify_env["DOCS_LINT"], "allowed")
