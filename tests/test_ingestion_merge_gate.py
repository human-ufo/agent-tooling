"""Focused offline tests for the local CI merge and API gates."""
from contextlib import contextmanager
import importlib.util
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "agent_ci_ingestion", ROOT / "lib/agent_ci_ingestion.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

HEAD = "a" * 40
OTHER_HEAD = "b" * 40
BASE = "c" * 40
OTHER_BASE = "d" * 40
REPOSITORY = "avla-tech/avla-data-ingestion-platform"
REMOTE_URL = "https://github.com/avla-tech/avla-data-ingestion-platform.git"
KEY = "e" * 64


class GuardedMergeTests(unittest.TestCase):
    def setUp(self):
        self.pr = {
            "number": 17,
            "url": f"https://github.com/{REPOSITORY}/pull/17",
            "headRefOid": HEAD,
            "headRefName": "feat/test",
            "baseRefName": "main",
            "isCrossRepository": False,
            "state": "OPEN",
        }

    @contextmanager
    def harness(self, *, ci_error=None, second_pr=None, latest_base=BASE,
                ancestor=True):
        views = [self.pr, second_pr or self.pr]
        calls = []

        def command(args, cwd=None, env=None):
            calls.append(("command", args))
            if args[:3] == ["gh", "pr", "view"]:
                return json.dumps(views.pop(0))
            raise AssertionError(f"unexpected command: {args!r}")

        def git(repo, *args):
            if args == ("rev-parse", "--show-toplevel"):
                return "/tmp/avla-checkout"
            if args == ("remote", "get-url", "origin"):
                return REMOTE_URL
            if args[:2] == ("fetch", "--quiet"):
                return ""
            if args == ("rev-parse", "FETCH_HEAD"):
                return HEAD
            raise AssertionError(f"unexpected git command: {args!r}")

        @contextmanager
        def snapshot(repo, sha, key):
            self.assertEqual(sha, HEAD)
            self.assertEqual(key, KEY)
            yield Path("/tmp/isolated-announced-commit")

        def ci_call(repo, sha, scope, base, key):
            calls.append(("ci", sha, scope, base))
            if ci_error:
                raise ci_error
            self.assertEqual((sha, scope, base, key), (HEAD, "full", BASE, KEY))
            return {"identity": {"sha": sha, "requested_scope": scope}}

        def remote_sha(repo, remote, ref):
            self.assertEqual(ref, "refs/heads/main")
            return latest_base

        def merge_base(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 0 if ancestor else 1)

        with patch.multiple(
                gate,
                command=command,
                git=git,
                repository_identity=lambda repo: KEY,
                snapshot=snapshot,
                fetch_main=lambda repo, remote: BASE,
                ci=ci_call,
                remote_sha=remote_sha,
                status=lambda sha, scope: calls.append(("status", sha, scope)),
                ), patch.object(gate.subprocess, "run", side_effect=merge_base), \
                patch.object(gate.subprocess, "call", side_effect=lambda argv: calls.append(("merge", argv)) or 0):
            yield calls

    def test_full_ci_failure_blocks_remote_merge(self):
        with self.harness(ci_error=gate.GateError("full failed")) as calls:
            with self.assertRaisesRegex(gate.GateError, "full failed"):
                gate.guarded_merge(["--", "pr", "merge", "17"])
        self.assertFalse(any(row[0] == "merge" for row in calls))

    def test_head_change_after_full_ci_blocks_merge(self):
        changed = dict(self.pr, headRefOid=OTHER_HEAD)
        with self.harness(second_pr=changed) as calls:
            with self.assertRaisesRegex(gate.GateError, "changed during full CI"):
                gate.guarded_merge(["--", "pr", "merge", "17"])
        self.assertFalse(any(row[0] == "merge" for row in calls))

    def test_main_change_after_full_ci_blocks_merge(self):
        with self.harness(latest_base=OTHER_BASE) as calls:
            with self.assertRaisesRegex(gate.GateError, "changed during full CI"):
                gate.guarded_merge(["--", "pr", "merge", "17"])
        self.assertFalse(any(row[0] == "merge" for row in calls))

    def test_pr_must_include_current_main_before_full_ci(self):
        with self.harness(ancestor=False) as calls:
            with self.assertRaisesRegex(gate.GateError, "must include current main"):
                gate.guarded_merge(["--", "pr", "merge", "17"])
        self.assertFalse(any(row[0] == "ci" for row in calls))
        self.assertFalse(any(row[0] == "merge" for row in calls))

    def test_admin_and_auto_are_rejected_before_ci_or_merge(self):
        for flag in ("--admin", "--auto"):
            with self.subTest(flag=flag), self.harness() as calls:
                with self.assertRaisesRegex(gate.GateError, "--auto and --admin are disabled"):
                    gate.guarded_merge(["--", "pr", "merge", "17", flag])
                self.assertFalse(any(row[0] == "ci" for row in calls))
                self.assertFalse(any(row[0] == "merge" for row in calls))

    def test_help_as_a_body_argument_never_bypasses_validation(self):
        with self.harness() as calls:
            with self.assertRaises((gate.GateError, argparse.ArgumentError)):
                gate.guarded_merge(['pr', 'merge', '17', '--body', '--help'])
        self.assertFalse(any(row[0] == 'merge' for row in calls))

    def test_success_publishes_full_status_and_pins_exact_head(self):
        argv = ["--", "pr", "merge", "17", "--match-head-commit", HEAD]
        with self.harness() as calls:
            self.assertEqual(gate.guarded_merge(argv), 0)
        self.assertIn(("ci", HEAD, "full", BASE), calls)
        self.assertIn(("status", HEAD, "full"), calls)
        merge = next(row[1] for row in calls if row[0] == "merge")
        self.assertEqual(merge, ["/usr/bin/gh", *argv[1:]])


class FullEvidenceTests(unittest.TestCase):
    def test_affected_evidence_cannot_satisfy_full_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            scripts = repo / ".github/scripts"
            scripts.mkdir(parents=True)
            for name in ("ci_local.py", "run-ci-local.sh", "run-ci-selected.sh",
                         "check-repository-safety.sh"):
                (scripts / name).write_text("fixture\n")
            evidence = {
                "identity": {"sha": HEAD, "requested_scope": "full",
                             "plan": {"coverage": "affected"}},
                "returncode": 0,
            }
            with patch.object(gate, "ci_environment", return_value={}), \
                    patch.object(gate, "command", return_value=json.dumps(evidence)):
                with self.assertRaisesRegex(gate.GateError, "full CI cannot reuse affected"):
                    gate.ci(repo, HEAD, "full", BASE, KEY, run=False)


class GuardApiTests(unittest.TestCase):
    def assert_blocked(self, *args):
        with self.assertRaises(gate.GateError):
            gate.guard_api(["--", *args])

    def test_rest_merge_methods_are_blocked_in_short_and_long_forms(self):
        endpoint = f"repos/{REPOSITORY}/pulls/17/merge"
        for args in (
                ("api", "-X", "PUT", endpoint),
                ("api", "-XPUT", endpoint),
                ("api", "-XPUT", endpoint + "?preview=1"),
                ("api", "--method", "POST", endpoint),
                ("api", "--method=PUT", endpoint),
                ("api", "--input", "repos/input.json", "-XPUT", endpoint),
                ("api", endpoint, "-f", "sha=" + HEAD),
        ):
            with self.subTest(args=args):
                self.assert_blocked(*args)

    def test_graphql_merge_mutation_is_blocked_from_field_and_file(self):
        query = "mutation { mergePullRequest(input: {pullRequestId: \"x\"}) { clientMutationId } }"
        self.assert_blocked("api", "graphql", "-f", "query=" + query)
        self.assert_blocked("api", "https://api.github.com/graphql", "-f", "query=" + query)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "merge.graphql"
            path.write_text(query)
            self.assert_blocked("api", "graphql", "-f", "query=@" + str(path))

    def test_graphql_read_query_allowed_and_uninspectable_query_fails_closed(self):
        gate.guard_api(["--", "api", "graphql", "-f", "query={ viewer { login } }"])
        self.assert_blocked("api", "graphql", "--input", "-")
        self.assert_blocked("api", "graphql")


if __name__ == "__main__":
    unittest.main()
