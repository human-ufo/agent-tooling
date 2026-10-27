#!/usr/bin/env python3
"""Read-only consolidated health report for agent-tooling state."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_FILES = ("hooks/pre-push", "bin/agent-lock", "bin/agent-worktree",
                 "lib/agent_publish.py", "lib/agent_lifecycle.py")


class CliError(Exception):
    pass


def git(path, *args):
    env = dict(os.environ, LC_ALL="C", LANG="C")
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX"):
        env.pop(key, None)
    try:
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                              text=True, env=env, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None


def repo_info(path):
    p = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if p is None or p.returncode:
        raise CliError("repo_unreadable")
    common = os.path.realpath(p.stdout.strip())
    if not common:
        raise CliError("repo_unreadable")
    return {"path": str(Path(path).resolve()),
            "repo_id": hashlib.sha256(common.encode("utf-8")).hexdigest(),
            "common_dir": common}


def add(attention, code, n=1):
    attention[code] = attention.get(code, 0) + n


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None


def check_sessions(info, attention, counts):
    root = Path(os.environ.get("AGENT_WORKTREE_DIR", str(Path.home() / ".cache/agent-worktrees")))
    path = root / (info["repo_id"] + ".json")
    if not path.exists():
        return
    state = read_json(path)
    if not isinstance(state, dict) or state.get("version") != 3 or state.get("repo_id") != info["repo_id"] or not isinstance(state.get("sessions"), list):
        add(attention, "registry_malformed")
        return
    for row in state["sessions"]:
        if not isinstance(row, dict):
            add(attention, "session_malformed")
            continue
        lifecycle = row.get("lifecycle", "active")
        if lifecycle == "orphan":
            counts["orphan_sessions"] += 1
            add(attention, "session_orphan")
            continue
        if lifecycle == "closing":
            counts["closing_sessions"] += 1
            add(attention, "session_closing")
            continue
        if lifecycle != "active":
            add(attention, "session_malformed")
            continue
        counts["active_sessions"] += 1
        session_path, branch = row.get("path"), row.get("branch")
        if not isinstance(session_path, str) or not os.path.isabs(session_path) or not isinstance(branch, str) or not branch:
            add(attention, "session_malformed")
            continue
        if not Path(session_path).is_dir():
            add(attention, "session_path_missing")
            continue
        session_repo = git(session_path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if session_repo is None or session_repo.returncode or os.path.realpath(session_repo.stdout.strip()) != info["common_dir"]:
            add(attention, "session_repository_changed")
            continue
        actual = git(session_path, "symbolic-ref", "--quiet", "--short", "HEAD")
        if actual is None or actual.returncode:
            add(attention, "session_detached_or_unreadable")
        elif actual.stdout.strip() != branch:
            add(attention, "session_branch_changed")


def check_locks(info, attention, counts):
    root = Path(os.environ.get("AGENT_LOCK_DIR", str(Path.home() / ".cache/agent-locks")))
    try:
        files = sorted(root.glob(info["repo_id"] + ".*.lock"))
    except OSError:
        add(attention, "locks_unreadable")
        return
    now = time.time()
    for path in files:
        row = read_json(path)
        expiry = row.get("expires_epoch") if isinstance(row, dict) else None
        valid = (isinstance(row, dict) and row.get("version") == 3 and row.get("repo_id") == info["repo_id"]
                 and isinstance(row.get("branch"), str) and isinstance(row.get("agent"), str)
                 and isinstance(row.get("session"), str) and isinstance(expiry, (int, float))
                 and not isinstance(expiry, bool) and math.isfinite(expiry) and expiry > 0)
        if not valid:
            add(attention, "lock_malformed")
        elif expiry <= now:
            counts["expired_locks"] += 1
            add(attention, "lock_expired")
        else:
            counts["active_locks"] += 1


def check_hooks(info, attention, counts):
    result = git(info["path"], "rev-parse", "--path-format=absolute", "--git-path", "hooks")
    if result is None or result.returncode:
        add(attention, "hooks_path_unreadable")
        return
    hooks = Path(result.stdout.strip())
    manifest_path = hooks / "agent-tooling-manifest.json"
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("version") != 3 or not isinstance(manifest.get("hooks"), dict):
        add(attention, "hook_manifest_missing_or_malformed")
        return
    entries = manifest["hooks"]
    if "pre-push" not in entries:
        add(attention, "hook_pre_push_missing")
    for name, entry in entries.items():
        if name not in ("pre-push", "pre-commit") or not isinstance(entry, dict):
            add(attention, "hook_manifest_entry_malformed")
            continue
        hook = hooks / name
        try:
            good = hook.is_file() and os.access(hook, os.X_OK) and hashlib.sha256(hook.read_bytes()).hexdigest() == entry.get("dispatcher_sha256")
        except OSError:
            good = False
        if not good:
            add(attention, "hook_dispatcher_drift")
        else:
            counts["verified_dispatchers"] += 1

    runtime_value = manifest.get("runtime")
    source_value = manifest.get("source_root")
    if not isinstance(runtime_value, str) or not runtime_value or (source_value is not None and not isinstance(source_value, str)):
        add(attention, "runtime_unverifiable")
        return
    runtime = Path(runtime_value)
    if not runtime.is_dir():
        add(attention, "runtime_unverifiable")
        return
    source = ROOT if all((ROOT / rel).is_file() for rel in RUNTIME_FILES) else Path(source_value or "")
    if not source.is_dir() or not all((source / rel).is_file() for rel in RUNTIME_FILES):
        add(attention, "runtime_unverifiable")
        return
    try:
        runtime_bytes = [(runtime / rel).read_bytes() for rel in RUNTIME_FILES]
        matches_source = runtime_bytes == [(source / rel).read_bytes() for rel in RUNTIME_FILES]
    except OSError:
        matches_source = False
    if not matches_source:
        add(attention, "runtime_drift")
    else:
        counts["verified_runtime_files"] = len(RUNTIME_FILES)


def check_changesets(info, since, attention, counts):
    root = Path(os.environ.get("AGENT_CHANGESET_DIR", str(Path.home() / ".cache/agent-changesets")))
    path = root / (info["repo_id"] + ".jsonl")
    if not path.exists():
        return
    events = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        add(attention, "changeset_unreadable")
        return
    for line in lines:
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError()
            ts = datetime.fromisoformat(str(row.get("ts", "")).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                raise ValueError()
            if row.get("repo_id") == info["repo_id"] and ts.timestamp() >= since:
                events.append(row)
        except (ValueError, TypeError, OverflowError):
            add(attention, "changeset_malformed")
    counts["changeset_events"] = len(events)
    latest = {}
    for row in events:
        operation = row.get("operation_id")
        event = row.get("event")
        if isinstance(operation, str) and operation:
            if event == "remote-result":
                if row.get("result") == "confirmed":
                    latest[operation] = False
                elif row.get("result") == "indeterminate":
                    latest[operation] = True
            elif operation not in latest:
                latest[operation] = False
    uncertain = sum(latest.values())
    counts["indeterminate_operations"] = uncertain
    if uncertain:
        add(attention, "publish_indeterminate", uncertain)


def inspect(path, since):
    info = repo_info(path)
    attention, counts = {}, {"active_sessions": 0, "orphan_sessions": 0, "closing_sessions": 0,
                             "active_locks": 0, "expired_locks": 0,
                             "verified_dispatchers": 0, "verified_runtime_files": 0,
                             "changeset_events": 0, "indeterminate_operations": 0}
    check_sessions(info, attention, counts)
    check_locks(info, attention, counts)
    check_hooks(info, attention, counts)
    check_changesets(info, since, attention, counts)
    return {"repo_id": info["repo_id"], "status": "attention" if attention else "healthy",
            "attention": attention, "counts": counts}


def inspect_safe(path, since):
    try:
        return inspect(path, since)
    except (CliError, OSError, TypeError, ValueError):
        repo_id = hashlib.sha256(os.fsencode(os.path.realpath(path))).hexdigest()
        return {"repo_id": repo_id, "status": "attention",
                "attention": {"repo_unreadable": 1}, "counts": {}}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="agent-health")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--repo", action="append", metavar="PATH")
    group.add_argument("--repos-file", metavar="FILE")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--since-hours", type=float, default=24)
    args = parser.parse_args(argv)
    if not math.isfinite(args.since_hours) or args.since_hours < 0:
        parser.error("--since-hours must be a finite non-negative number")
    if args.repo:
        repos = args.repo
    else:
        try:
            repos = []
            for line in Path(args.repos_file).read_text(encoding="utf-8").splitlines():
                value = line.strip()
                if not value:
                    continue
                if value.startswith("-") or not os.path.isabs(value):
                    raise CliError("repos_file_invalid")
                repos.append(value)
        except (OSError, UnicodeError):
            raise CliError("repos_file_unreadable")
    if not repos or any(not os.path.isabs(repo) for repo in repos):
        raise CliError("repo_path_must_be_absolute")
    return args, repos


def main(argv=None):
    try:
        args, repos = parse_args(argv)
        since = time.time() - args.since_hours * 3600
        reports = [inspect_safe(repo, since) for repo in repos]
    except CliError as exc:
        payload = {"status": "error", "error": str(exc)}
        print(json.dumps(payload, sort_keys=True) if (argv and "--json" in argv) or (argv is None and "--json" in sys.argv) else "agent-health: " + str(exc), file=sys.stderr)
        return 2
    result = {"status": "attention" if any(row["status"] == "attention" for row in reports) else "healthy",
              "repos": reports}
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print("agent-health: " + result["status"])
        for row in reports:
            print(f"{row['repo_id']} {row['status']} " + ",".join(f"{k}={v}" for k, v in sorted(row["attention"].items())))
    return 1 if result["status"] == "attention" else 0


if __name__ == "__main__":
    raise SystemExit(main())
