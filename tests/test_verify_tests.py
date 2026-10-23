"""The generic test-suite verifier judges the announced commit, not the tree."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "bin/agent-verify-tests"

PASSING = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_ok(self):\n"
    "        self.assertTrue(True)\n"
)
FAILING = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_fails(self):\n"
    "        self.assertEqual(1, 2)\n"
)
OTHER = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_other(self):\n"
    "        self.assertTrue(True)\n"
)


class VerifyTestsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.run_git("init", "-q", "-b", "main", str(self.repo), cwd=self.base)
        self.run_git("config", "user.name", "test")
        self.run_git("config", "user.email", "test@example.invalid")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_sample.py").write_text(PASSING)
        self.run_git("add", "-A")
        self.run_git("commit", "-qm", "first")
        self.sha = self.run_git("rev-parse", "HEAD").stdout.strip()

    @staticmethod
    def environment():
        # The environment is explicit: without it the current directory stays
        # in the PATH and "git" resolves to /usr/sbin/init.
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_CONFIG_NOSYSTEM="1")
        for key in list(env):
            if key.startswith("GIT_") and key not in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"):
                env.pop(key)
        return env

    def run_git(self, *args, cwd=None, check=True):
        env = self.environment()
        p = subprocess.run(["git", *args], cwd=cwd or self.repo, env=env,
                           text=True, capture_output=True)
        if check and p.returncode:
            raise AssertionError(p.stdout + p.stderr)
        return p

    def verify(self, *args):
        return subprocess.run([str(VERIFIER), *args], cwd=self.repo,
                              env=self.environment(), text=True,
                              capture_output=True)

    def commit(self, content, message):
        (self.repo / "tests" / "test_sample.py").write_text(content)
        self.run_git("add", "-A")
        self.run_git("commit", "-qm", message)
        return self.run_git("rev-parse", "HEAD").stdout.strip()

    def test_a_passing_suite_is_accepted(self):
        result = self.verify("tests", self.sha)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_failing_suite_is_rejected(self):
        broken = self.commit(FAILING, "broken")
        result = self.verify("tests", broken)
        self.assertNotEqual(result.returncode, 0)

    def test_a_suite_with_no_tests_is_not_a_pass(self):
        (self.repo / "tests" / "test_sample.py").unlink()
        self.run_git("add", "-A")
        self.run_git("commit", "-qm", "no tests")
        empty = self.run_git("rev-parse", "HEAD").stdout.strip()
        result = self.verify("tests", empty)
        self.assertNotEqual(result.returncode, 0)

    def test_a_sha_that_is_not_the_checkout_is_rejected(self):
        # The checkout stays at the first commit while the verifier is asked
        # about a different one: the content does not match what is published.
        different = self.commit(OTHER, "second")
        self.assertNotEqual(different, self.sha)
        self.run_git("reset", "--hard", self.sha)
        result = self.verify("tests", different)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differs from the pushed commit", result.stderr)

    def test_a_missing_suite_is_rejected(self):
        result = self.verify("no-existe", self.sha)
        self.assertNotEqual(result.returncode, 0)

    def test_the_verifier_needs_the_announced_sha(self):
        result = self.verify("tests")
        self.assertNotEqual(result.returncode, 0)

    def test_the_suite_directory_can_come_from_the_environment(self):
        result = subprocess.run([str(VERIFIER)], cwd=self.repo, text=True,
                                capture_output=True,
                                env={"PATH": "/usr/bin:/bin", "HOME": str(self.base),
                                     "AGENT_VERIFY_SUITE": "tests"})
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
