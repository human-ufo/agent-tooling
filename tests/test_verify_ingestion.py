"""Offline safety gate must bind to SHA and never leak scanner matches."""
import os
import hashlib
import json
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest

VERIFIER = Path(__file__).resolve().parents[1] / "bin/agent-verify-ingestion"

FIXTURE_CI = "\nimport argparse, json, os, subprocess\nfrom pathlib import Path\np=argparse.ArgumentParser(); p.add_argument('command'); p.add_argument('sha'); p.add_argument('--scope'); p.add_argument('--base'); p.add_argument('--reuse',action='store_true'); a=p.parse_args()\nidentity={'sha':a.sha,'requested_scope':a.scope,'plan':{'coverage':a.scope}}\ncache=Path.home()/'.cache'/'fixture-ci'/os.environ['CI_LOCAL_REPOSITORY_KEY'];cache.mkdir(parents=True,exist_ok=True)\npath=cache/(a.sha+'-'+a.scope+'-'+a.base+'.json')\nif a.command=='run':\n rc=subprocess.run(['bash','.github/scripts/check-repository-safety.sh'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode\n path.write_text(json.dumps({'identity':identity,'returncode':rc}));raise SystemExit(rc)\nif not path.exists(): raise SystemExit(1)\nr=json.loads(path.read_text())\nif r['returncode']: raise SystemExit(1)\nprint(json.dumps(r))\n"

class IngestionVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = dict(os.environ, HOME=str(self.root / "home"), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        Path(self.env["HOME"]).mkdir()
        for key in list(self.env):
            if key.startswith("GIT_") and key not in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"):
                del self.env[key]
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.invalid")
        self.check = self.repo / ".github/scripts/check-repository-safety.sh"
        self.check.parent.mkdir(parents=True)
        self.check.write_text("#!/bin/bash\nexit 0\n")
        scripts = self.check.parent
        for name in ('run-ci-local.sh', 'run-ci-selected.sh'):
            (scripts / name).write_text('# fixture runner\n')
        (scripts / 'ci_local.py').write_text(FIXTURE_CI)
        self.commit()
        self.remote = self.root / 'remote.git'
        subprocess.run(['git', 'clone', '--bare', str(self.repo), str(self.remote)], check=True, capture_output=True)
        self.git('remote', 'add', 'origin', str(self.remote))
        key = hashlib.sha256(os.fsencode(self.repo / '.git')).hexdigest()
        self.env.update(AGENT_VERIFY_REPOSITORY_ID=key, AGENT_VERIFY_REMOTE_URL=str(self.remote),
                        AGENT_POST_PUSH_REPOSITORY_ID=key)
        self.binary = self.root / 'bin'; self.binary.mkdir()
        gh = self.binary / 'gh'
        gh.write_text('#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\nPath(os.environ["HOME"],"status.json").write_text(json.dumps(sys.argv[1:]))\n')
        gh.chmod(0o755)
        self.env['PATH'] = str(self.binary) + os.pathsep + self.env['PATH']

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

    def test_missing_provenance_blocks(self):
        self.env.pop('AGENT_VERIFY_REPOSITORY_ID')
        self.assertNotEqual(self.verify().returncode, 0)

    def test_evidence_survives_verification_clone_and_status_uses_announced_sha(self):
        (self.repo / 'content.md').write_text('# changed\n'); self.commit()
        clone = self.root / 'prepush-clone'
        subprocess.run(['git', 'clone', '--shared', str(self.repo), str(clone)], check=True, capture_output=True)
        result = subprocess.run([str(VERIFIER), self.sha], cwd=clone, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.git('push', 'origin', self.sha + ':refs/heads/topic')
        # Mutate source HEAD: post-push must still certify the announced object.
        (self.repo / 'later.md').write_text('# later\n'); old = self.sha; self.commit()
        payload = dict(remote='origin', url=str(self.remote), updates=[dict(ref='refs/heads/topic', new=old, old='0'*40)])
        result = subprocess.run([str(VERIFIER.with_name('agent-ci-ingestion')), 'post-push'], cwd=self.repo,
                                env=self.env, input=json.dumps(payload), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        status = json.loads((Path(self.env['HOME']) / 'status.json').read_text())
        self.assertIn('repos/avla-tech/avla-data-ingestion-platform/statuses/' + old, status)
        self.assertIn('context=local-ci/affected', status)

    def test_postpush_missing_evidence_never_publishes(self):
        self.git('push', 'origin', self.sha + ':refs/heads/topic')
        payload = dict(remote='origin', url=str(self.remote), updates=[dict(ref='refs/heads/topic', new=self.sha, old='0'*40)])
        result = subprocess.run([str(VERIFIER.with_name('agent-ci-ingestion')), 'post-push'], cwd=self.repo,
                                env=self.env, input=json.dumps(payload), capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((Path(self.env['HOME']) / 'status.json').exists())
