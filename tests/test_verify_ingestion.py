"""Offline safety gate must bind to SHA and never leak scanner matches."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

VERIFIER = Path(__file__).resolve().parents[1] / "bin/agent-verify-ingestion"

class IngestionVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        for key in list(self.env):
            if key.startswith("GIT_") and key not in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"):
                del self.env[key]
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.invalid")
        self.check = self.repo / ".github/scripts/check-repository-safety.sh"
        self.check.parent.mkdir(parents=True)
        self.check.write_text("#!/bin/bash\nexit 0\n")
        self.commit()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, env=self.env,
                              text=True, capture_output=True, check=True).stdout.strip()

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "fixture")
        self.sha = self.git("rev-parse", "HEAD")

    def verify(self, sha=None):
        return subprocess.run([str(VERIFIER), sha or self.sha], cwd=self.repo,
                              env=self.env, text=True, capture_output=True)

    def test_clean_announced_commit_passes(self):
        self.assertEqual(self.verify().returncode, 0)

    def test_failed_scan_blocks_without_leaking_output(self):
        self.check.write_text("#!/bin/bash\necho simulated-sensitive-match >&2\nexit 1\n")
        self.commit()
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("simulated-sensitive-match", result.stdout + result.stderr)

    def test_wrong_sha_and_dirty_checkout_fail(self):
        self.assertEqual(self.verify("a" * 40).returncode, 1)
        self.check.write_text("exit 0\n# dirty\n")
        self.assertEqual(self.verify().returncode, 1)

    def test_missing_check_fails(self):
        self.check.unlink()
        (self.repo / "placeholder").write_text("fixture")
        self.commit()
        self.assertEqual(self.verify().returncode, 1)

    def test_valid_but_different_commit_fails(self):
        prior = self.sha
        (self.repo / "new-file").write_text("fixture")
        self.commit()
        self.assertEqual(self.verify(prior).returncode, 1)

    def test_success_output_is_suppressed(self):
        self.check.write_text("echo simulated-sensitive-match\nexit 0\n")
        self.commit()
        result = self.verify()
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("simulated-sensitive-match", result.stdout + result.stderr)

    def test_symlink_check_is_rejected(self):
        self.check.unlink()
        self.check.symlink_to("/dev/null")
        self.commit()
        self.assertEqual(self.verify().returncode, 1)
