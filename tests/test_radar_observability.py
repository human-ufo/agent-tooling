"""Radar audit events stay explicit when refresh or diagnosis fails."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from test_pre_push_hook import PublishFixture

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("agent_publish_radar_test", ROOT / "lib/agent_publish.py")
PUBLISH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PUBLISH)


class Trace:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append(dict(event=event, **fields))


class RadarObservabilityTests(unittest.TestCase):
    def invoke(self, fetch_code=0, responses=()):
        trace = Trace()
        fetch = mock.Mock(returncode=fetch_code)
        calls = [mock.Mock(returncode=code, stdout=stdout, stderr="private helper detail")
                 for code, stdout in responses]

        def run(*args, **kwargs):
            return calls.pop(0)

        with mock.patch.object(PUBLISH, "git", return_value=fetch), \
                mock.patch.object(PUBLISH.subprocess, "run", side_effect=run), \
                mock.patch.dict(os.environ, {"AGENT_WORKTREE_BIN": "/mock/agent-worktree"}), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = PUBLISH.radar_notice(Path("/repo"), Path("/common"), "repo-id", "agent",
                                          "session", trace, [], remote="origin")
        self.assertEqual(len(trace.events), 1)
        self.assertEqual(trace.events[0]["event"], "collision-radar")
        self.assertNotIn("private helper detail", json.dumps(trace.events))
        return result, trace.events[0], stderr.getvalue()

    def test_clean_result_is_recorded_even_without_collisions(self):
        result, event, warning = self.invoke(responses=[(0, json.dumps({
            "status": "clean", "baseline_available": True, "collisions": []
        }))])
        self.assertEqual(result["status"], "clean")
        self.assertEqual(event["status"], "clean")
        self.assertEqual(event["freshness"], "refreshed")
        self.assertTrue(event["diagnostic_available"])
        self.assertFalse(event["index_only"])
        self.assertEqual(warning, "")

    def test_index_only_overlap_notice_does_not_ask_to_delay_merging_into_trunk(self):
        result, event, warning = self.invoke(responses=[(0, json.dumps({
            "status": "conflict", "baseline_available": True, "index_only": True,
            "collisions": [{
                "target": "chore/other-session", "type": "collision", "index_only": True,
                "paths": [{"type": "content", "path": "AI_KNOWLEDGE_MAP.md"}]
            }]
        }))])
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(event["status"], "conflict")
        self.assertTrue(event["index_only"])
        self.assertIn("regenerable index overlap with chore/other-session: content:AI_KNOWLEDGE_MAP.md", warning)
        self.assertIn("can be merged cleanly into trunk", warning)
        self.assertNotIn("coordinate before merging", warning)

    def test_fetch_failure_marks_unknown_and_keeps_partial_conflict_diagnosis(self):
        result, event, warning = self.invoke(fetch_code=1, responses=[(0, json.dumps({
            "status": "conflict", "baseline_available": True,
            "collisions": [{"target": "main", "paths": []}]
        }))])
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(event["status"], "unknown")
        self.assertEqual(event["comparison_status"], "conflict")
        self.assertEqual(event["freshness"], "stale")
        self.assertIn("stale remote-tracking data", warning)

    def test_stale_clean_comparison_never_becomes_a_clean_event(self):
        _, event, _ = self.invoke(fetch_code=1, responses=[(0, json.dumps({
            "status": "clean", "baseline_available": True, "collisions": []
        }))])
        self.assertEqual(event["status"], "unknown")
        self.assertEqual(event["comparison_status"], "clean")

    def test_missing_helper_executable_is_reported_without_its_error(self):
        trace = Trace()
        with mock.patch.object(PUBLISH, "git", return_value=mock.Mock(returncode=0)), \
                mock.patch.object(PUBLISH.subprocess, "run", side_effect=OSError('private detail')), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            PUBLISH.radar_notice(Path('/repo'), Path('/common'), 'id', 'a', 's', trace, [])
        self.assertEqual(trace.events[0]['status'], 'unknown')
        self.assertNotIn('private detail', stderr.getvalue())

    def test_helper_failures_and_malformed_json_are_unknown_without_raw_stderr(self):
        for responses in (
            [(2, ""), (2, "")],
            [(0, "not-json")],
            [(0, json.dumps({"status": "clean", "baseline_available": False, "collisions": []}))],
            [(0, json.dumps({"status": "unknown", "baseline_available": True, "collisions": []}))],
        ):
            with self.subTest(responses=responses):
                result, event, warning = self.invoke(responses=responses)
                self.assertEqual(event["status"], "unknown")
                self.assertFalse(event["diagnostic_available"])
                self.assertIn("could not produce a reliable diagnosis", warning)
                self.assertNotIn("private helper detail", warning)


class RadarFailurePublicationTests(PublishFixture):
    def test_unavailable_radar_diagnostic_does_not_block_branch_publication(self):
        helper = self.base / "broken-radar-helper"
        helper.write_text("#!/bin/sh\nif [ \"$1\" = identity ]; then echo '{\"status\":\"unmanaged\"}'; exit 1; fi\n"
                          "echo 'private helper detail' >&2\necho malformed\nexit 0\n")
        helper.chmod(0o755)
        env = dict(self.env, AGENT_WORKTREE_BIN=str(helper))
        self.commit("published despite unavailable radar")
        result = self.command(str(ROOT / "bin/agent-push"), "origin", "HEAD:refs/heads/topic", env=env,
                              check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("private helper detail", result.stderr)
        radar = [event for event in self.events() if event["event"] == "collision-radar"]
        self.assertEqual(len(radar), 1)
        self.assertEqual(radar[0]["status"], "unknown")
        self.assertEqual(self.remote_sha(), self.git("rev-parse", "HEAD").stdout.strip())


if __name__ == "__main__":
    unittest.main()
