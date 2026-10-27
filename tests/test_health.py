"""Read-only health reporting fixtures use temporary local Git repositories."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
import unittest

ROOT = Path(__file__).resolve().parents[1]
HEALTH = ROOT / "bin/agent-health"
RUNTIME_FILES = ("hooks/pre-push", "bin/agent-lock", "bin/agent-worktree",
                 "lib/agent_publish.py", "lib/agent_lifecycle.py")


def run(*args, cwd=None, env=None):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        run("git", "init", "-q", "-b", "main", str(self.repo))
        run("git", "-C", str(self.repo), "-c", "user.name=test", "-c", "user.email=t@t",
            "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "initial")
        common = run("git", "-C", str(self.repo), "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
        self.repo_id = hashlib.sha256(os.fsencode(os.path.realpath(common))).hexdigest()
        self.state = self.tmp / "state"
        self.locks = self.tmp / "locks"
        self.changes = self.tmp / "changes"
        self.hooks = self.tmp / "hooks"
        self.runtime = self.tmp / "runtime"
        for rel in RUNTIME_FILES:
            target = self.runtime / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, target)
        self.hooks.mkdir()
        dispatcher = b"#!/bin/sh\nexit 0\n"
        (self.hooks / "pre-push").write_bytes(dispatcher)
        (self.hooks / "pre-push").chmod(0o755)
        manifest = {"version": 3, "source_root": str(ROOT), "runtime": str(self.runtime),
                    "hooks": {"pre-push": {"dispatcher_sha256": hashlib.sha256(dispatcher).hexdigest()}}}
        (self.hooks / "agent-tooling-manifest.json").write_text(json.dumps(manifest))
        run("git", "-C", str(self.repo), "config", "core.hooksPath", str(self.hooks))
        self.env = dict(os.environ, AGENT_WORKTREE_DIR=str(self.state), AGENT_LOCK_DIR=str(self.locks),
                        AGENT_CHANGESET_DIR=str(self.changes))

    def invoke(self, *args):
        return run(str(HEALTH), "--repo", str(self.repo), "--json", *args, env=self.env)

    def report(self, *args):
        result = self.invoke(*args)
        return result, json.loads(result.stdout)

    def test_healthy_and_does_not_create_state_directories(self):
        result, payload = self.report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["status"], "healthy")
        self.assertFalse(self.state.exists())
        self.assertFalse(self.locks.exists())
        self.assertFalse(self.changes.exists())

    def test_missing_session_path_and_expired_lock_are_attention(self):
        self.state.mkdir()
        (self.state / f"{self.repo_id}.json").write_text(json.dumps({
            "version": 3, "repo_id": self.repo_id,
            "sessions": [{"session": "gone", "agent": "test", "path": str(self.tmp / "gone"),
                          "branch": "agent/test/gone", "lifecycle": "active"}]}))
        self.locks.mkdir()
        (self.locks / f"{self.repo_id}.lock-id.lock").write_text(json.dumps({
            "version": 3, "repo_id": self.repo_id, "branch": "agent/test/gone", "agent": "test",
            "session": "gone", "expires_epoch": 1}))
        result, payload = self.report()
        self.assertEqual(result.returncode, 1)
        attention = payload["repos"][0]["attention"]
        self.assertEqual(attention["session_path_missing"], 1)
        self.assertEqual(attention["lock_expired"], 1)

    def test_dispatcher_and_runtime_drift_are_reported(self):
        (self.hooks / "pre-push").write_text("#!/bin/sh\nexit 7\n")
        (self.runtime / "lib/agent_publish.py").write_text("drift\n")
        result, payload = self.report()
        self.assertEqual(result.returncode, 1)
        attention = payload["repos"][0]["attention"]
        self.assertIn("hook_dispatcher_drift", attention)
        self.assertIn("runtime_drift", attention)

    def test_current_root_takes_precedence_over_matching_old_source_root(self):
        old_source = self.tmp / "old-source"
        for rel in RUNTIME_FILES:
            target = old_source / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.runtime / rel, target)
        altered = b"old runtime payload\n"
        (self.runtime / "lib/agent_publish.py").write_bytes(altered)
        (old_source / "lib/agent_publish.py").write_bytes(altered)
        manifest_path = self.hooks / "agent-tooling-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["source_root"] = str(old_source)
        manifest_path.write_text(json.dumps(manifest))
        result, payload = self.report()
        self.assertEqual(result.returncode, 1)
        self.assertIn("runtime_drift", payload["repos"][0]["attention"])

    def test_manifest_requires_pre_push_and_bad_runtime_path_is_fixed_attention(self):
        manifest_path = self.hooks / "agent-tooling-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["hooks"] = {"pre-commit": {"dispatcher_sha256": "0" * 64}}
        manifest["runtime"] = ["malformed", "path"]
        manifest_path.write_text(json.dumps(manifest))
        result, payload = self.report()
        self.assertEqual(result.returncode, 1)
        attention = payload["repos"][0]["attention"]
        self.assertIn("hook_pre_push_missing", attention)
        self.assertIn("runtime_unverifiable", attention)

    def test_unknown_remote_is_not_contacted_and_jsonl_errors_are_sanitized(self):
        run("git", "-C", str(self.repo), "remote", "add", "origin", "ssh://invalid.example/secret/repo")
        self.changes.mkdir()
        (self.changes / f"{self.repo_id}.jsonl").write_text(
            '{"ts":"2026-10-27T12:00:00+00:00","repo_id":"' + self.repo_id +
            '","event":"attempt","operation_id":"op1"}\nnot-json-secret\n')
        result, payload = self.report()
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("invalid.example", result.stdout + result.stderr)
        self.assertNotIn("not-json-secret", result.stdout + result.stderr)
        attention = payload["repos"][0]["attention"]
        self.assertEqual(attention["changeset_malformed"], 1)

    def test_indeterminate_publish_stays_pending_until_later_confirmation(self):
        self.changes.mkdir()
        path = self.changes / f"{self.repo_id}.jsonl"

        def record(event, result=None):
            row = {"ts": datetime.now(timezone.utc).isoformat(), "repo_id": self.repo_id,
                   "operation_id": "operation-secret", "event": event}
            if result is not None:
                row["result"] = result
            with path.open("a") as stream:
                stream.write(json.dumps(row) + "\n")

        record("remote-result", "indeterminate")
        first, payload = self.report()
        self.assertEqual(first.returncode, 1)
        self.assertEqual(payload["repos"][0]["counts"]["indeterminate_operations"], 1)
        record("remote-result", "confirmed")
        second, payload = self.report()
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertEqual(payload["repos"][0]["counts"]["indeterminate_operations"], 0)

    def test_malformed_lock_is_redacted_and_orphan_closing_are_counted(self):
        self.locks.mkdir()
        (self.locks / f"{self.repo_id}.bad.lock").write_text("credential-secret")
        self.state.mkdir()
        sessions = [{"lifecycle": "orphan"}, {"lifecycle": "closing"}]
        (self.state / f"{self.repo_id}.json").write_text(json.dumps({
            "version": 3, "repo_id": self.repo_id, "sessions": sessions}))
        result, payload = self.report()
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("credential-secret", result.stdout + result.stderr)
        report = payload["repos"][0]
        self.assertEqual(report["attention"]["lock_malformed"], 1)
        self.assertEqual(report["counts"]["orphan_sessions"], 1)
        self.assertEqual(report["counts"]["closing_sessions"], 1)

    def test_unreadable_repo_does_not_hide_other_repo_reports(self):
        missing = self.tmp / "missing-repository"
        result = run(str(HEALTH), "--repo", str(missing), "--repo", str(self.repo), "--json", env=self.env)
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(len(payload["repos"]), 2)
        self.assertEqual(payload["repos"][0]["attention"], {"repo_unreadable": 1})
        self.assertEqual(payload["repos"][1]["status"], "healthy")
        self.assertNotIn(str(missing), result.stdout + result.stderr)

    def test_repos_file_requires_absolute_paths_and_cli_errors_exit_two(self):
        file = self.tmp / "repos"
        file.write_text("-h\n")
        result = run(str(HEALTH), "--repos-file", str(file), "--json", env=self.env)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("-h", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
