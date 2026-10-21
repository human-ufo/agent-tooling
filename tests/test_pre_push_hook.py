"""Prueba el hook pre-push: la barrera de escritura local.

El hook se ejercita de verdad: se invoca como git lo invoca, con los refs a
empujar por stdin, contra un remoto local. Cada caso comprueba que la operacion
queda bloqueada o permitida segun la politica.
"""
from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "pre-push"
LOCK = ROOT / "bin" / "agent-lock"


def run_hook(cwd, env, args=("origin", "file:///dev/null"), stdin=""):
    return subprocess.run(
        ["bash", str(HOOK), *args], cwd=str(cwd), input=stdin,
        capture_output=True, text=True, env=env)


class PushHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clone = Path(self.tmp) / "clone"
        self.clone.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "t")
        self.git("config", "user.email", "t@t")
        (self.clone / "a.md").write_text("x\n")
        self.git("add", ".")
        self.git("commit", "-qm", "inicial")
        self.git("checkout", "-qb", "docs/una")
        (self.clone / "b.md").write_text("y\n")
        self.git("add", ".")
        self.git("commit", "-qm", "doc")

        self.lockdir = Path(self.tmp) / "locks"
        self.lockdir.mkdir()
        self.changesets = Path(self.tmp) / "changesets"
        self.env = dict(
            os.environ,
            AGENT_LOCK_DIR=str(self.lockdir),
            AGENT_CHANGESET_DIR=str(self.changesets),
            AGENT_LOCK_BIN=str(LOCK),
            AGENT_ACTOR="agy-uno",
            AGENT_SESSION="s1",
        )

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=str(self.clone),
                              capture_output=True, text=True)

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def push_stdin(self, branch="docs/una"):
        return (f"refs/heads/{branch} {self.head()} refs/heads/{branch} "
                f"{'0' * 40}\n")

    def take_lock(self, agent="agy-dos", branch="docs/una", session="s9"):
        subprocess.run(
            ["bash", str(LOCK), "acquire", str(self.clone), "--agent", agent,
             "--session", session, "--branch", branch, "--note", "n"],
            env=self.env, capture_output=True, text=True)

    # --- trunk protegido ----------------------------------------------------
    def test_push_to_main_is_blocked(self):
        self.git("checkout", "-q", "main")
        result = run_hook(self.clone, self.env, stdin=self.push_stdin("main"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("push directo a 'main' bloqueado", result.stderr)

    def test_push_to_main_allowed_with_explicit_bypass(self):
        self.git("checkout", "-q", "main")
        env = dict(self.env, ALLOW_MAIN_PUSH="1")
        result = run_hook(self.clone, env, stdin=self.push_stdin("main"))
        self.assertEqual(result.returncode, 0, result.stderr)

    # --- force sin lease ----------------------------------------------------
    def test_plain_force_is_blocked(self):
        result = run_hook(self.clone, self.env, args=("origin", "url", "--force"),
                           stdin=self.push_stdin())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--force sin --force-with-lease bloqueado", result.stderr)

    def test_force_with_lease_is_allowed(self):
        result = run_hook(self.clone, self.env,
                           args=("origin", "url", "--force-with-lease=refs/heads/docs/una:abc"),
                           stdin=self.push_stdin())
        self.assertEqual(result.returncode, 0, result.stderr)

    # --- lock de otra sesion ------------------------------------------------
    def test_push_of_a_branch_taken_by_another_session_is_blocked(self):
        self.take_lock(agent="agy-dos", branch="docs/una")
        result = run_hook(self.clone, self.env, stdin=self.push_stdin("docs/una"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("está tomada por otra sesión", result.stderr)

    def test_push_of_a_free_branch_is_allowed_with_another_branch_locked(self):
        self.take_lock(agent="agy-dos", branch="docs/otra")
        self.git("checkout", "-qb", "docs/libre")
        (self.clone / "c.md").write_text("z\n")
        self.git("add", ".")
        self.git("commit", "-qm", "libre")
        result = run_hook(self.clone, self.env, stdin=self.push_stdin("docs/libre"))
        self.assertEqual(result.returncode, 0, result.stderr)

    # --- log de cambios -----------------------------------------------------
    def test_push_records_a_changeset_with_the_actor(self):
        result = run_hook(self.clone, self.env, stdin=self.push_stdin("docs/una"))
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.changesets / "clone.jsonl"
        self.assertTrue(log.exists(), "no se escribio el log de cambios")
        entry = json.loads(log.read_text().strip().splitlines()[-1])
        self.assertEqual(entry["actor"], "agy-uno")
        self.assertEqual(entry["session"], "s1")
        self.assertEqual(entry["branch"], "docs/una")
        self.assertEqual(entry["local_sha"], self.head())

    def test_blocked_push_writes_no_changeset(self):
        self.take_lock(agent="agy-dos", branch="docs/una")
        run_hook(self.clone, self.env, stdin=self.push_stdin("docs/una"))
        log = self.changesets / "clone.jsonl"
        if log.exists():
            entries = [l for l in log.read_text().strip().splitlines() if l.strip()]
            self.assertEqual(entries, [], "un push bloqueado no debe registrar cambios")

    # --- verificacion sobre el arbol exacto --------------------------------
    def test_verify_failure_blocks_the_push(self):
        env = dict(self.env, AGENT_PUSH_VERIFY="false")
        result = run_hook(self.clone, env, stdin=self.push_stdin("docs/una"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("la verificación falló", result.stderr)

    def test_verify_success_allows_the_push(self):
        env = dict(self.env, AGENT_PUSH_VERIFY="true")
        result = run_hook(self.clone, env, stdin=self.push_stdin("docs/una"))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_skip_flag_bypasses_everything(self):
        env = dict(self.env, AGENT_SKIP_PUSH_CHECKS="1")
        self.git("checkout", "-q", "main")
        result = run_hook(self.clone, env, stdin=self.push_stdin("main"))
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
