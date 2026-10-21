"""Prueba el aislamiento por worktree y el lock con alcance, en directorios temporales."""
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKTREE = ROOT / "bin" / "agent-worktree"
LOCK = ROOT / "bin" / "agent-lock"


def run(cmd, *args, cwd=None, env=None):
    return subprocess.run(
        [str(cmd), *args], capture_output=True, text=True, cwd=cwd, env=env)


class RepoFixture:
    """Un clon git mínimo, con un origin remoto local."""

    def __init__(self, base):
        self.base = Path(base)
        self.origin = self.base / "origin"
        self.clone = self.base / "clone"
        self.origin.mkdir(parents=True)
        self.run_git(self.origin, "init", "-q", "-b", "main")
        (self.origin / "README.md").write_text("hola\n")
        self.run_git(self.origin, "add", ".")
        self.run_git(self.origin, "-c", "user.name=t", "-c", "user.email=t@t",
                     "commit", "-qm", "inicial")
        self.run_git(self.origin, "config", "receive.denyCurrentBranch", "ignore")
        self.run_git(self.base, "clone", "-q", str(self.origin), str(self.clone))
        self.run_git(self.clone, "config", "user.name", "t")
        self.run_git(self.clone, "config", "user.email", "t@t")
        self.run_git(self.clone, "config", "commit.gpgsign", "false")

    @staticmethod
    def run_git(cwd, *args):
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = Path(self.tmp) / "state"
        self.state.mkdir()
        self.fixture = RepoFixture(self.tmp)
        self.env = dict(os.environ, AGENT_WORKTREE_DIR=str(self.state))

    def wt(self, *args):
        return run("bash", str(WORKTREE), *args, env=self.env)

    def test_open_creates_isolated_worktree_with_own_branch(self):
        opened = self.wt("open", str(self.fixture.clone), "--agent", "agy-uno",
                         "--session", "s1")
        self.assertEqual(opened.returncode, 0, opened.stderr)
        path = Path(self.fixture.clone) / ".worktrees" / "s1"
        self.assertTrue(path.is_dir(), opened.stdout)
        listed = self.wt("list", str(self.fixture.clone))
        self.assertIn("s1", listed.stdout)
        branches = self.fixture.run_git(self.fixture.clone, "branch", "--list")
        self.assertIn("agent/agy-uno/", branches.stdout)

    def test_two_sessions_get_different_worktrees(self):
        a = self.wt("open", str(self.fixture.clone), "--agent", "agy-uno", "--session", "s1")
        b = self.wt("open", str(self.fixture.clone), "--agent", "agy-uno", "--session", "s2")
        self.assertEqual(a.returncode, 0, a.stderr)
        self.assertEqual(b.returncode, 0, b.stderr)
        pa = Path(self.fixture.clone) / ".worktrees" / "s1"
        pb = Path(self.fixture.clone) / ".worktrees" / "s2"
        self.assertNotEqual(pa, pb)
        self.assertTrue(pa.is_dir() and pb.is_dir())

    def test_worktree_is_ignored_by_the_parent_clone(self):
        self.wt("open", str(self.fixture.clone), "--agent", "agy-uno", "--session", "s1")
        status = self.fixture.run_git(self.fixture.clone, "status", "--porcelain")
        self.assertNotIn(".worktrees", status.stdout, status.stdout)

    def test_open_refuses_a_branch_that_already_exists(self):
        self.wt("open", str(self.fixture.clone), "--agent", "agy-uno", "--session", "s1")
        taken = self.fixture.run_git(
            self.fixture.clone, "branch", "--list", "--format=%(refname:short)")
        branch = next(l for l in taken.stdout.splitlines() if l.startswith("agent/"))
        second = self.wt("open", str(self.fixture.clone), "--agent", "agy-dos",
                         "--session", "s2", "--branch", branch)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("ya existe", second.stderr)

    def test_close_refuses_to_discard_uncommitted_work(self):
        self.wt("open", str(self.fixture.clone), "--agent", "agy-uno", "--session", "s1")
        path = Path(self.fixture.clone) / ".worktrees" / "s1"
        (path / "nuevo.md").write_text("trabajo sin commitear\n")
        closed = self.wt("close", str(self.fixture.clone), "--agent", "agy-uno",
                         "--session", "s1")
        self.assertNotEqual(closed.returncode, 0)
        self.assertIn("cambios sin commitear", closed.stderr)
        forced = self.wt("close", str(self.fixture.clone), "--agent", "agy-uno",
                         "--session", "s1", "--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)

    def state_file(self):
        slug = re.sub(r"[^A-Za-z0-9._-]", "_", str(self.fixture.clone))
        return self.state / f"{slug}.json"

    def test_state_file_is_json_and_lists_the_session(self):
        self.wt("open", str(self.fixture.clone), "--agent", "agy-uno", "--session", "s1")
        state = json.loads(self.state_file().read_text())
        self.assertEqual(state[0]["session"], "s1")
        self.assertEqual(state[0]["agent"], "agy-uno")
        self.assertIn("branch", state[0])


class LockScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lockdir = Path(self.tmp) / "locks"
        self.lockdir.mkdir()
        self.env = dict(os.environ, AGENT_LOCK_DIR=str(self.lockdir))

    def lock(self, *args):
        return run("bash", str(LOCK), *args, env=self.env)

    def write_lock(self, repo, agent, session="", branch="", expires_in=3600):
        import time
        now = int(time.time())
        payload = {
            "repo": repo, "agent": agent, "session": session, "branch": branch,
            "note": "tarea", "acquired": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 600)),
            "expires_epoch": now + expires_in,
            "expires_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + expires_in)),
        }
        (self.lockdir / f"{repo}.lock").write_text(json.dumps(payload))
        return payload

    def test_release_of_another_agent_lock_fails(self):
        self.write_lock("repo-a", "agy-uno")
        released = self.lock("release", "repo-a", "--agent", "agy-dos")
        self.assertNotEqual(released.returncode, 0)
        self.assertIn("No lo libero", released.stderr)
        self.assertTrue((self.lockdir / "repo-a.lock").exists())

    def test_release_of_another_session_of_same_agent_fails(self):
        self.write_lock("repo-a", "agy-uno", session="s1")
        released = self.lock("release", "repo-a", "--agent", "agy-uno", "--session", "s2")
        self.assertNotEqual(released.returncode, 0)
        self.assertTrue((self.lockdir / "repo-a.lock").exists())

    def test_force_release_works(self):
        self.write_lock("repo-a", "agy-uno")
        released = self.lock("release", "repo-a", "--agent", "agy-dos", "--force")
        self.assertEqual(released.returncode, 0, released.stderr)
        self.assertFalse((self.lockdir / "repo-a.lock").exists())

    def test_check_on_other_branch_reports_no_conflict(self):
        self.write_lock("repo-a", "agy-uno", branch="docs/una")
        checked = self.lock("check", "repo-a", "--branch", "docs/otra")
        self.assertEqual(checked.returncode, 0, checked.stdout)
        self.assertIn("Sin conflicto real", checked.stdout)

    def test_check_on_same_branch_reports_conflict(self):
        self.write_lock("repo-a", "agy-uno", branch="docs/una")
        checked = self.lock("check", "repo-a", "--branch", "docs/una")
        self.assertNotEqual(checked.returncode, 0)
        self.assertIn("TOMADO", checked.stdout)

    def test_acquire_records_branch_and_session(self):
        acquired = self.lock("acquire", "repo-a", "--agent", "agy-uno",
                             "--session", "s1", "--branch", "docs/x", "--note", "n")
        self.assertEqual(acquired.returncode, 0, acquired.stderr)
        payload = json.loads((self.lockdir / "repo-a.lock").read_text())
        self.assertEqual(payload["session"], "s1")
        self.assertEqual(payload["branch"], "docs/x")

    def test_default_ttl_is_ninety_minutes(self):
        self.lock("acquire", "repo-a", "--agent", "agy-uno")
        payload = json.loads((self.lockdir / "repo-a.lock").read_text())
        import time
        remaining = payload["expires_epoch"] - int(time.time())
        self.assertLess(remaining, 91 * 60)
        self.assertGreater(remaining, 88 * 60)


if __name__ == "__main__":
    unittest.main()
