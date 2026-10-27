#!/usr/bin/env python3
"""Local publication policy; advisory against cooperating clients, not authentication.

agent-push [--rewrite] [--delete] [--allow-trunk --reason TEXT] REMOTE SRC:DST...
Every destination must be a full refs/heads/... or refs/tags/... name. --delete
uses :DST. Rewrites/deletions carry exact leases, even when remote tracking refs
are stale. A single-use authorization binds the complete advertised update set.
Repository config: agent.pushVerifyMode=optional|required (mandatory declaration);
agent.pushVerify is an argv string (shlex, no shell), receiving SHA as final arg.
The verifier runs in a disposable checkout of each pushed SHA, once per ref.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import math
import os
import re
from contextlib import contextmanager
from pathlib import Path
import shlex
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]

# Branches that are always treated as trunk, in addition to whatever the
# remote advertises as its default branch.
TRUNK_REFS = ("refs/heads/main", "refs/heads/master")


def default_branch_ref(url):
    """Resolve the default branch of a remote as a full ref, or None.

    The remote's HEAD is the authority. A remote without an advertised HEAD
    keeps the explicit list only, so an unknown remote never silently widens
    or narrows the trunk protection.
    """
    head = git("ls-remote", "--symref", url, "HEAD", check=False)
    if head.returncode:
        return None
    for line in head.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "ref:" and parts[1].startswith("refs/heads/"):
            return parts[1]
    return None


class Rejected(Exception):
    pass


def redact(value):
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+@", r"\1[redacted]@", value)
        return re.sub(r"([?&](?:token|key|password|credential|signature|access_token)=)[^&\s]+", r"\1[redacted]", value, flags=re.I)
    return value


def clean_env():
    env = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX"):
        env.pop(key, None)
    return env


def git(*args, cwd=None, check=True):
    p = subprocess.run(["git", *args], cwd=cwd, env=clean_env(), text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode:
        raise Rejected(p.stderr.strip() or "git command failed")
    return p


def repo_context():
    worktree = Path(git("rev-parse", "--show-toplevel").stdout.strip()).resolve()
    common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()).resolve()
    return worktree, common, hashlib.sha256(os.fsencode(common)).hexdigest()


def identity(worktree):
    actor, session = os.getenv("AGENT_ACTOR"), os.getenv("AGENT_SESSION")
    if bool(actor) != bool(session):
        raise Rejected("AGENT_ACTOR and AGENT_SESSION must both be set")
    helper = os.getenv("AGENT_WORKTREE_BIN", str(ROOT / "bin/agent-worktree"))
    p = subprocess.run([helper, "identity", str(worktree), "--json"],
                       capture_output=True, text=True, env=clean_env())
    managed = None
    if p.returncode == 0:
        try:
            managed = json.loads(p.stdout)
            ma, ms = managed.get("actor", managed.get("agent")), managed.get("session")
            if not ma or not ms:
                raise ValueError("missing managed identity")
        except (ValueError, TypeError, AttributeError) as e:
            raise Rejected("malformed managed worktree identity") from e
        if actor and (actor, session) != (ma, ms):
            raise Rejected("explicit identity conflicts with managed worktree")
        actor, session = ma, ms
    else:
        try:
            unmanaged = json.loads(p.stdout)
        except (ValueError, TypeError) as e:
            raise Rejected("malformed managed worktree identity response") from e
        if p.returncode != 1 or not isinstance(unmanaged, dict) or unmanaged.get("status") != "unmanaged":
            raise Rejected("cannot determine managed worktree identity: " + str(unmanaged.get("error", "registry error") if isinstance(unmanaged, dict) else "registry error"))
    if not actor or not session:
        raise Rejected("identify publication with AGENT_ACTOR and AGENT_SESSION or a managed worktree")
    if any(not isinstance(v, str) or not v.strip() or "\n" in v for v in (actor, session)):
        raise Rejected("invalid actor/session")
    return actor, session


class Trace:
    def __init__(self, worktree, repo_id, actor, session, remote, operation=None):
        self.base = dict(schema_version=3, repo_id=repo_id, host=socket.gethostname(), actor=actor,
                         session=session, operation_id=operation or uuid.uuid4().hex,
                         worktree=str(worktree), remote=remote)
        self.directory = Path(os.getenv("AGENT_CHANGESET_DIR", str(Path.home() / ".cache/agent-changesets")))
        self.path = self.directory / (repo_id + ".jsonl")

    def emit(self, event, **fields):
        self.directory.mkdir(parents=True, exist_ok=True)
        record = dict(self.base, ts=datetime.datetime.now(datetime.timezone.utc).isoformat(), event=event, **fields)
        # One O_APPEND write per bounded event avoids interleaving JSON records.
        data = (json.dumps(redact(record), sort_keys=True) + "\n").encode()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if os.write(fd, data) != len(data):
                raise Rejected("incomplete audit write")
        finally:
            os.close(fd)


def config(key):
    p = git("config", "--get", key, check=False)
    if p.returncode not in (0, 1):
        raise Rejected("invalid repository configuration: " + key)
    return p.stdout.strip()


def lock_call(command, worktree, branch, actor, session):
    helper = os.getenv("AGENT_LOCK_BIN", str(ROOT / "bin/agent-lock"))
    p = subprocess.run([helper, command, str(worktree), "--branch", branch,
                        "--agent", actor, "--session", session, "--json"],
                       text=True, capture_output=True, env=clean_env())
    try:
        result = json.loads(p.stdout)
        if not isinstance(result, dict):
            raise ValueError()
    except (ValueError, TypeError) as e:
        raise Rejected("malformed lock response") from e
    return p.returncode, result


def check_lock(worktree, ref, actor, session):
    branch = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ref
    code, result = lock_call("check", worktree, branch, actor, session)
    if code or result.get("status") not in ("free", "owned"):
        raise Rejected("lock rejects " + ref + ": " + str(result.get("status", "error")))
    return branch, result["status"]


def zero(sha):
    return bool(sha) and set(sha) == {"0"}


def validate_ref(ref):
    if not ref.startswith(("refs/heads/", "refs/tags/")) or git("check-ref-format", ref, check=False).returncode:
        raise Rejected("destination must be a full branch/tag ref: " + ref)


def parse_updates(data):
    updates = []
    for line in data.splitlines():
        parts = line.split()
        if len(parts) != 4:
            raise Rejected("malformed pre-push ref input")
        local_ref, new, ref, old = parts
        validate_ref(ref)
        if any(len(s) not in (40, 64) or any(c not in "0123456789abcdef" for c in s) for s in (old, new)):
            raise Rejected("malformed object id")
        if any(u["ref"] == ref for u in updates):
            raise Rejected("duplicate destination")
        updates.append(dict(ref=ref, old=old, new=new))
    return updates


def consume_authorization(common, repo_id, remote, url, actor, session, updates):
    supplied = os.getenv("AGENT_PUSH_AUTH")
    if not supplied:
        return None
    path = Path(supplied)
    directory = common / "agent-publish" / "authorizations"
    if path.parent.resolve() != directory.resolve() or not path.name.endswith(".json") or path.is_symlink():
        raise Rejected("invalid publication authorization path")
    # Rename first: only one hook can consume the capability, including rejects.
    claimed = path.with_suffix(".consumed-" + uuid.uuid4().hex)
    try:
        path.rename(claimed)
        try:
            token = json.loads(claimed.read_text())
        finally:
            claimed.unlink()
    except (OSError, ValueError) as e:
        raise Rejected("missing, consumed or malformed publication authorization") from e
    if not isinstance(token, dict):
        raise Rejected("malformed publication authorization")
    issued, expires = token.get("issued_at"), token.get("expires_at")
    # The window is five minutes. The tolerance absorbs the floating point
    # drift of a window that is exactly the limit, which must not be read as
    # an invalid lifetime.
    if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (issued, expires))
            or issued > time.time() + 30 or issued >= expires or expires - issued > 301 or expires <= time.time()):
        raise Rejected("expired or invalid publication authorization lifetime")
    expected = dict(repo_id=repo_id, remote=remote, url=url, actor=actor, session=session,
                    updates=sorted(updates, key=lambda u: u["ref"]))
    if token.get("repo_id") != repo_id or token.get("remote") != remote or token.get("url") != url \
            or token.get("actor") != actor or token.get("session") != session:
        raise Rejected("publication authorization does not match actual push")
    # The announced "old" has to be the one the wrapper observed against the
    # live remote. A ref that moved in between is reported on its own, so the
    # cause of the refusal is visible instead of a generic mismatch.
    authorized_updates = token.get("updates")
    if not isinstance(authorized_updates, list):
        raise Rejected("malformed publication authorization")
    announced = {u["ref"]: u for u in updates if isinstance(u, dict) and "ref" in u}
    for row in authorized_updates:
        if not isinstance(row, dict) or "ref" not in row:
            raise Rejected("malformed publication authorization")
        seen = announced.get(row["ref"])
        if seen is not None and seen["old"] != row.get("old"):
            raise Rejected("remote ref moved since the publication was authorized: " + row["ref"])
    if any(token.get(k) != v for k, v in expected.items()):
        raise Rejected("publication authorization does not match actual push")
    if not isinstance(token.get("operation_id"), str) or not token["operation_id"]:
        raise Rejected("authorization lacks operation id")
    return token


def verify_queue_path(repo_id):
    """The FIFO mutex that serializes verifiers of one repository.

    It lives next to the locks, because it is the same kind of resource: a
    local mutex that coordinates cooperating processes on this host.
    """
    directory = Path(os.getenv("AGENT_LOCK_DIR", str(Path.home() / ".cache/agent-locks")))
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"verify.{repo_id}.mutex"


@contextmanager
def verify_queue(repo_id):
    """Serialize the verifiers of one repository.

    Two sessions publishing at the same time would otherwise run two heavy
    suites concurrently and starve each other of CPU and disk, which shows up
    as a timeout and a false failure. The queue is FIFO and per repository, so
    unrelated repositories keep running in parallel.
    """
    path = verify_queue_path(repo_id)
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def verify(worktree, update, trace, mode, command, repo_id=None):
    if zero(update["new"]):
        trace.emit("verification", ref=update["ref"], sha=update["new"], result="skipped", reason="deletion")
        return
    if not command:
        trace.emit("verification", ref=update["ref"], sha=update["new"], result="skipped", reason="optional-unconfigured")
        return
    try:
        argv = shlex.split(command)
        if not argv:
            raise Rejected("empty verification command")
        started = time.monotonic()
        with verify_queue(repo_id):
            waited = int((time.monotonic() - started) * 1000)
            started = time.monotonic()
            with tempfile.TemporaryDirectory(prefix="agent-push-verify-") as temp:
                checkout = Path(temp) / "checkout"
                git("clone", "--quiet", "--shared", "--no-checkout", "--local", str(worktree), str(checkout))
                # An annotated tag names a tag object, not a commit. Checking out
                # the tag object resolves to its commit, so the checkout would show
                # a different SHA than the one announced. The announced object is
                # what the verifier has to judge, so it is passed unchanged and the
                # tag object stays in the store for verify-tag style checks.
                target = update["new"]
                kind = git("cat-file", "-t", target, cwd=checkout, check=False).stdout.strip()
                if kind == "tag":
                    git("-c", "core.hooksPath=/dev/null", "checkout", "--quiet", "--detach", target + "^{commit}", cwd=checkout)
                else:
                    git("-c", "core.hooksPath=/dev/null", "checkout", "--quiet", "--detach", target, cwd=checkout)
                # This is a trusted command, not a security sandbox: same UID can
                # still read the filesystem. Ambient provider tokens are not passed.
                allowed = {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "SYSTEMROOT"}
                additions = shlex.split(config("agent.pushVerifyEnv"))
                for name in additions:
                    if (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                            or name.startswith(("GIT_", "AGENT_"))):
                        raise Rejected("invalid verifier environment allowlist name")
                allowed.update(additions)
                env = {key: value for key, value in os.environ.items() if key in allowed or key.startswith("LC_")}
                env.update(AGENT_VERIFY_REF=update["ref"], AGENT_VERIFY_SHA=update["new"],
                           AGENT_VERIFY_OBJECT_TYPE=kind or "unknown",
                           GIT_TERMINAL_PROMPT="0")
                timeout_text = config("agent.pushVerifyTimeout") or "300"
                try:
                    timeout = int(timeout_text)
                    if timeout < 1 or timeout > 86400:
                        raise ValueError()
                except ValueError as e:
                    raise Rejected("agent.pushVerifyTimeout must be 1..86400 seconds") from e
                p = subprocess.Popen([*argv, update["new"]], cwd=checkout, env=env, start_new_session=True)
                # The verifier receives the announced object. For an annotated tag
                # that is the tag object, so a verifier that compares against HEAD
                # must resolve the tag itself. The checkout is the peeled commit;
                # AGENT_VERIFY_OBJECT_TYPE says which of the two it is.
                try:
                    p.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait()
                    raise Rejected("verification timed out for " + update["ref"])
                if p.returncode:
                    raise Rejected("verification failed for " + update["ref"])
            duration = int((time.monotonic() - started) * 1000)
        trace.emit("verification", ref=update["ref"], sha=update["new"], result="passed",
                   queue_wait_ms=waited, run_duration_ms=duration)
    except (OSError, ValueError, subprocess.TimeoutExpired, Rejected) as e:
        trace.emit("verification", ref=update["ref"], sha=update["new"], result="failed", reason=str(e), mode=mode)
        # Configured verifiers are gating even in optional mode; optional means
        # the command itself may be absent, never that a failure is ignored.
        raise Rejected(str(e)) from e


def hook(args):
    if len(args) != 2:
        raise Rejected("pre-push expects only remote name and URL")
    remote, url = args
    worktree, common, repo_id = repo_context()
    trace = Trace(worktree, repo_id, None, None, remote)
    authorized = None
    try:
        actor, session = identity(worktree)
        trace.base.update(actor=actor, session=session)
        updates = parse_updates(sys.stdin.read())
        if not updates:
            trace.emit("attempt", source="pre-push", updates=[])
            return 0
        authorized = consume_authorization(common, repo_id, remote, url, actor, session, updates)
        if authorized:
            trace.base["operation_id"] = authorized["operation_id"]
        trace.emit("attempt", source="pre-push", updates=updates)
        if os.getenv("AGENT_SKIP_PUSH_CHECKS"):
            raise Rejected("AGENT_SKIP_PUSH_CHECKS is unsupported; checks cannot be disabled")
        mode, command = config("agent.pushVerifyMode"), config("agent.pushVerify")
        if mode not in ("optional", "required"):
            raise Rejected("configure agent.pushVerifyMode explicitly as optional or required")
        if mode == "required" and not command:
            raise Rejected("required verifier is not configured")
        # Git reports the remote-tracking value it had, which may be stale.
        # The authorization carries the value the wrapper observed against the
        # live remote, so the hook decides on the same "old" the lease uses.
        expected_old = {}
        if authorized:
            expected_old = {u["ref"]: u["old"] for u in authorized.get("updates", [])}
        trunk = default_branch_ref(url)
        for update in updates:
            ref, old, new = update["ref"], update["old"], update["new"]
            if ref in expected_old and expected_old[ref] != old:
                raise Rejected("remote ref moved since the publication was authorized: " + ref)
            check_lock(worktree, ref, actor, session)
            if ref in TRUNK_REFS or ref == trunk:
                if not authorized or not authorized.get("allow_trunk") or not authorized.get("reason", "").strip():
                    raise Rejected("direct trunk push requires agent-push --allow-trunk --reason")
                trace.emit("trunk-exception", ref=ref, reason=authorized["reason"])
            if zero(new):
                if not authorized or not authorized.get("delete"):
                    raise Rejected("deletion requires agent-push --delete")
            elif not zero(old):
                if ref.startswith("refs/tags/") and old != new and (not authorized or not authorized.get("rewrite")):
                    raise Rejected("tag replacement requires agent-push --rewrite")
                ff = git("merge-base", "--is-ancestor", old, new, check=False)
                if ff.returncode and (not authorized or not authorized.get("rewrite")):
                    raise Rejected("non-fast-forward requires agent-push --rewrite with exact lease")
            verify(worktree, update, trace, mode, command, repo_id=repo_id)
        trace.emit("hook-accepted", updates=updates)
        if not authorized:
            trace.emit("remote-result", result="indeterminate", reason="raw git push: pre-push cannot observe remote result")
        return 0
    except (Rejected, OSError) as e:
        trace.emit("reject", source="pre-push", reason=str(e))
        raise


def radar_notice(worktree, common, repo_id, actor, session, trace, updates, remote="origin"):
    """Warn about textual collisions with the other sessions or the trunk.

    The notice never blocks the publication: publishing the branch preserves
    the work in the remote, and refusing to publish would leave it only on the
    local disk. It exists so the agent or the person knows before merging the
    pull request which files will collide and with which session.

    The remote is fetched first, because a radar that compares against a stale
    `origin/main` reports collisions that were resolved long ago and stays
    blind to the work another machine published in the meantime.
    """
    try:
        fetch = git("fetch", "--quiet", remote, cwd=worktree, check=False)
        freshness = "refreshed" if fetch.returncode == 0 else "stale"
    except Exception:
        freshness = "stale"
    helper = os.getenv("AGENT_WORKTREE_BIN", str(ROOT / "bin/agent-worktree"))
    result = None
    diagnostic_available = False
    try:
        p = subprocess.run([helper, "radar", str(worktree), "--session", session, "--remote", remote, "--json"],
                           capture_output=True, text=True, env=clean_env())
        if p.returncode != 0:
            # An unmanaged checkout or unknown session can still compare its current branch.
            p = subprocess.run([helper, "radar", str(worktree), "--remote", remote, "--json"],
                               capture_output=True, text=True, env=clean_env())
        if p.returncode == 0:
            decoded = json.loads(p.stdout)
            if isinstance(decoded, dict):
                result = decoded
                diagnostic_available = True
    except Exception:
        # Helper output and stderr are deliberately excluded from the audit event.
        pass

    collisions = result.get("collisions") if isinstance(result, dict) else None
    baseline_available = result.get("baseline_available") if isinstance(result, dict) else None
    status = result.get("status") if isinstance(result, dict) else None
    if (not diagnostic_available or status not in ("clean", "conflict")
            or baseline_available is not True or not isinstance(collisions, list)
            or any(not isinstance(row, dict) for row in collisions)
            or (status == "conflict" and not collisions)):
        status = "unknown"
        collisions = []
        diagnostic_available = False
    elif status == "clean" and collisions:
        # Do not report a contradictory payload as a clean comparison.
        status = "unknown"
        collisions = []

    comparison_status = status
    if freshness == "stale":
        status = "unknown"

    trace.emit("collision-radar", status=status, freshness=freshness,
               comparison_status=comparison_status,
               diagnostic_available=diagnostic_available,
               baseline_available=baseline_available if isinstance(baseline_available, bool) else None,
               collisions=collisions)
    if freshness == "stale":
        print("agent-push: collision radar used stale remote-tracking data because fetch failed",
              file=sys.stderr)
    if status == "unknown":
        print("agent-push: collision radar could not produce a reliable diagnosis",
              file=sys.stderr)
        return result
    if not collisions:
        return result
    for row in collisions:
        paths = ", ".join(f"{p.get('type')}:{p.get('path')}" for p in row.get("paths", []))
        print("agent-push: collision detected with " + str(row.get("target"))
              + ((": " + paths) if paths else ""), file=sys.stderr)
    print("agent-push: the publication proceeds; coordinate before merging the pull request",
          file=sys.stderr)
    return result


def wrapper(args):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rewrite", action="store_true")
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("--allow-trunk", action="store_true")
    parser.add_argument("--reason", default="")
    parser.add_argument("remote")
    parser.add_argument("refspec", nargs="+")
    opt = parser.parse_args(args)
    worktree, common, repo_id = repo_context()
    trace = Trace(worktree, repo_id, None, None, opt.remote)
    trace.emit("attempt", source="agent-push", refspecs=opt.refspec)
    held, token_path = [], None
    try:
        actor, session = identity(worktree)
        trace.base.update(actor=actor, session=session)
        ensure_hook(worktree)
        if opt.remote.startswith("-"):
            raise Rejected("invalid remote")
        urls = git("remote", "get-url", "--push", "--all", opt.remote).stdout.splitlines()
        if len(urls) != 1:
            raise Rejected("exact publication requires one configured push URL")
        url = urls[0]
        advertised = git("ls-remote", "--refs", url).stdout
        before = dict(line.split()[::-1] for line in advertised.splitlines())
        # The default branch of the remote is protected as trunk, whatever it
        # is called. A remote whose default branch is "trunk" would otherwise
        # publish to it without the exception that main and master require.
        trunk = default_branch_ref(url)
        hashlen = len(git("hash-object", "--stdin").stdout.strip())
        updates, refspecs = [], []
        for spec in opt.refspec:
            if ":" not in spec or spec.startswith("+"):
                raise Rejected("use explicit SRC:refs/heads/DST or :refs/heads/DST")
            src, ref = spec.split(":", 1)
            validate_ref(ref)
            if any(u["ref"] == ref for u in updates):
                raise Rejected("duplicate destination")
            if not src and not opt.delete:
                raise Rejected("deletion requires --delete")
            if src and opt.delete:
                raise Rejected("--delete accepts only :DST refspecs")
            new = git("rev-parse", "--verify", src + "^{object}").stdout.strip() if src else "0" * hashlen
            old = before.get(ref, "0" * hashlen)
            if zero(new) and zero(old):
                raise Rejected("cannot delete absent remote ref")
            if ref in TRUNK_REFS or ref == trunk:
                if not opt.allow_trunk or not opt.reason.strip():
                    raise Rejected("trunk exception requires --allow-trunk --reason TEXT")
            if ref.startswith("refs/tags/") and old != new and not zero(old) and not opt.rewrite and not opt.delete:
                raise Rejected("tag replacement requires --rewrite")
            if not zero(new) and not zero(old) and git("merge-base", "--is-ancestor", old, new, check=False).returncode and not opt.rewrite:
                raise Rejected("non-fast-forward requires --rewrite")
            if old == new:
                continue
            updates.append(dict(ref=ref, old=old, new=new))
            refspecs.append(("" if zero(new) else new) + ":" + ref)
        if not updates:
            trace.emit("remote-result", result="confirmed", outcome="unchanged", statuses=[])
            return 0
        # Ordered acquisition prevents cooperating multi-ref publishers deadlocking.
        for update in sorted(updates, key=lambda u: u["ref"]):
            branch, status = check_lock(worktree, update["ref"], actor, session)
            if status == "free":
                code, result = lock_call("acquire", worktree, branch, actor, session)
                if code or result.get("status") != "owned":
                    raise Rejected("cannot acquire publication lock: " + str(result))
                held.append(branch)
        directory = common / "agent-publish" / "authorizations"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        token_path = directory / (uuid.uuid4().hex + ".json")
        issued = time.time()
        token = dict(issued_at=issued, expires_at=issued + 300, repo_id=repo_id, remote=opt.remote, url=url, actor=actor, session=session,
                     updates=sorted(updates, key=lambda u: u["ref"]), operation_id=trace.base["operation_id"],
                     rewrite=opt.rewrite, delete=opt.delete, allow_trunk=opt.allow_trunk, reason=opt.reason)
        with open(token_path, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
            json.dump(token, f)
        env = clean_env()
        env.update(AGENT_ACTOR=actor, AGENT_SESSION=session, AGENT_PUSH_AUTH=str(token_path))
        # The radar is informative: it tells which files will collide before
        # the merge, and it never refuses the publication.
        try:
            radar_notice(worktree, common, repo_id, actor, session, trace, updates, remote=opt.remote)
        except Exception:
            # Radar remains advisory; failures must not block publication.
            print("agent-push: collision radar unavailable; publication proceeds", file=sys.stderr)
        leases = ["--force-with-lease=" + u["ref"] + ":" + ("" if zero(u["old"]) else u["old"]) for u in updates]
        p = subprocess.run(["git", "push", "--atomic", "--porcelain", *leases, opt.remote, *refspecs], env=env,
                           text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        sys.stdout.write(redact(p.stdout))
        sys.stderr.write(redact(p.stderr))
        # Porcelain reports individual remote statuses. Overall nonzero can mean
        # transport loss after acceptance, so absent explicit status is uncertain.
        statuses = []
        for line in p.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and len(parts[0]) == 1:
                statuses.append(dict(flag=parts[0], refspec=parts[1], detail=parts[2]))
        consumed = not token_path.exists()
        confirmed = len(statuses) == len(updates)
        trace.emit("remote-result", result=("confirmed" if confirmed else "indeterminate"),
                   outcome=("accepted" if p.returncode == 0 and confirmed else "rejected" if confirmed else "unknown"),
                   returncode=p.returncode, statuses=statuses, authorization_consumed=consumed)
        if p.returncode == 0 and not consumed:
            raise Rejected("installed pre-push hook did not consume operation authorization")
        return p.returncode
    except (Rejected, OSError) as e:
        trace.emit("reject", source="agent-push", reason=str(e))
        raise
    finally:
        if token_path:
            token_path.unlink(missing_ok=True)
        for branch in reversed(held):
            try:
                code, result = lock_call("release", worktree, branch, actor, session)
                if code:
                    print("agent-push: publication lock retained: " + str(result), file=sys.stderr)
            except (Rejected, OSError) as e:
                print("agent-push: publication lock release failed: " + str(e), file=sys.stderr)


def ensure_hook(worktree):
    hooks = Path(git("rev-parse", "--path-format=absolute", "--git-path", "hooks", cwd=worktree).stdout.strip())
    try:
        manifest = json.loads((hooks / "agent-tooling-manifest.json").read_text())
        target = hooks / "pre-push"
        expected = manifest["hooks"]["pre-push"]["dispatcher_sha256"]
        if manifest["version"] != 3 or not os.access(target, os.X_OK) or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise Rejected("install the v3 pre-push hook before agent-push") from e


def legacy_managed(target, source):
    if not target.is_symlink():
        return False
    if target.resolve() == source.resolve():
        return True
    try:
        content = target.read_text()
        return ("# Hook pre-push" in content and "AGENT_SKIP_PUSH_CHECKS" in content
                and (target.resolve().parent.parent / "bin/agent-worktree").is_file())
    except OSError:
        return False


def dispatcher(commands):
    return ("#!/usr/bin/env python3\n# agent-tooling v3: replay identical stdin to every chained hook.\n"
            "import subprocess, sys\n"
            "data = sys.stdin.buffer.read()\n"
            "for command in " + repr(commands) + ":\n"
            "    result = subprocess.run(command + sys.argv[1:], input=data)\n"
            "    if result.returncode: sys.exit(result.returncode)\n")


# Repositories whose documents follow the documentation standard. The lint of
# the `documentar` skill is only meaningful there.
DOCUMENTATION_REPOSITORIES = ("personal-docs", "avla-docs-reporteria")


def documentation_repository(repo):
    """True when the repository is one of the governance documentation repos.

    The directory name decides, and so does the origin remote: a clone of a
    documentation repository can sit in any directory.
    """
    candidates = [Path(repo).resolve().name]
    try:
        url = git("config", "--get", "remote.origin.url", cwd=repo, check=False).stdout.strip()
        if url:
            candidates.append(url.rstrip("/").rsplit("/", 1)[-1])
            if url.endswith(".git"):
                candidates.append(url.rstrip("/")[:-4].rsplit("/", 1)[-1])
    except (OSError, Rejected):
        pass
    return any(candidate in DOCUMENTATION_REPOSITORIES for candidate in candidates)


def install_one(repo, hooks):
    manifest_path = hooks / "agent-tooling-manifest.json"
    original_manifest = manifest_path.read_bytes() if manifest_path.exists() else None
    try:
        manifest = json.loads(original_manifest) if original_manifest else dict(version=3, hooks={})
        if manifest.get("version") != 3 or not isinstance(manifest.get("hooks"), dict):
            raise ValueError()
    except (ValueError, AttributeError) as e:
        raise Rejected("incompatible installation manifest") from e
    # Validate every live dispatcher and rollback source before touching runtime.
    for name, entry in manifest["hooks"].items():
        if name not in ("pre-push", "pre-commit") or not isinstance(entry, dict):
            raise Rejected("invalid managed hook manifest")
        target = hooks / name
        if (not target.is_file() or not os.access(target, os.X_OK)
                or hashlib.sha256(target.read_bytes()).hexdigest() != entry.get("dispatcher_sha256")):
            raise Rejected("installed hook changed; refusing to replace " + str(target))
        backup = entry.get("backup")
        if backup and not (Path(backup).exists() or Path(backup).is_symlink()):
            raise Rejected("hook rollback backup is missing: " + str(backup))
    payload = {}
    for relative in ("hooks/pre-push", "bin/agent-lock", "bin/agent-worktree", "lib/agent_publish.py", "lib/agent_lifecycle.py"):
        payload[relative] = (ROOT / relative).read_bytes()
    # The documentation lint only applies to the governance repositories. Its
    # rules demand sections that a task file or a source file does not have, so
    # installing it anywhere else blocks legitimate commits.
    if documentation_repository(repo):
        # The hook of the `documentar` skill, the single documentation entry
        # point. The retired `docs-writer` path stays as a fallback so an
        # installation that has not been updated yet keeps its barrier.
        lint = None
        for relative in (".agents/skills/documentar/pre-commit",
                         ".agents/skills/docs-writer/pre-commit"):
            candidate = Path.home() / relative
            if candidate.is_file():
                lint = candidate
                break
        if lint is not None:
            payload["hooks/pre-commit-docs-lint"] = lint.read_bytes()
        elif "pre-commit" in manifest["hooks"]:
            # Keep previously installed lint when source skill isn't available.
            payload["hooks/pre-commit-docs-lint"] = Path(manifest["hooks"]["pre-commit"]["source"]).read_bytes()
    names = ["pre-push"] + (["pre-commit"] if "hooks/pre-commit-docs-lint" in payload else [])
    for name in names:
        target = hooks / name
        if target.is_dir() or (target.is_symlink() and not target.exists()):
            raise Rejected("incompatible existing hook: " + str(target))
    digest = hashlib.sha256()
    for relative, data in sorted(payload.items()):
        digest.update(relative.encode() + b"\0" + hashlib.sha256(data).digest())
    source_hash = digest.hexdigest()
    runtime = hooks / ("agent-tooling-runtime-" + source_hash)
    if runtime.exists():
        if any(not (runtime / name).is_file() or (runtime / name).read_bytes() != data for name, data in payload.items()):
            raise Rejected("immutable installed runtime changed")
    else:
        with tempfile.TemporaryDirectory(prefix=".agent-runtime-stage-", dir=hooks) as staging:
            staged = Path(staging) / "runtime"
            for name, data in payload.items():
                dest = staged / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                dest.chmod(0o755 if name.startswith(("bin/", "hooks/")) else 0o644)
            staged.rename(runtime)
    sources = {"pre-push": runtime / "hooks/pre-push"}
    if "pre-commit" in names:
        sources["pre-commit"] = runtime / "hooks/pre-commit-docs-lint"
    # A hook that is no longer part of the payload has to be removed: the
    # documentation lint does not apply outside the governance repositories,
    # and leaving it installed blocks legitimate commits.
    for name in sorted(set(manifest["hooks"]) - set(sources)):
        entry = manifest["hooks"].pop(name)
        target = hooks / name
        previous = entry.get("previous", [])
        backup = entry.get("backup")
        if previous:
            body = dispatcher([[str(p)] for p in previous])
            temp_target = hooks / (name + ".new")
            temp_target.write_text(body)
            temp_target.chmod(0o755)
            temp_target.replace(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
        print("removed: " + str(target))
    # Snapshot all targets, then atomically replace each dispatcher. If any write
    # fails, restore every target and the old manifest before releasing the lock.
    created_backups, changed = [], []
    with tempfile.TemporaryDirectory(prefix=".agent-hook-transaction-", dir=hooks) as staging:
        snapshots = Path(staging)
        for name in names:
            target = hooks / name
            if target.exists() or target.is_symlink():
                shutil.copy2(target, snapshots / name, follow_symlinks=False)
        try:
            for name, source in sources.items():
                target = hooks / name
                entry = manifest["hooks"].get(name)
                previous, backup = [], entry.get("backup") if entry else None
                if entry:
                    previous = entry.get("previous", [])
                    if not previous and backup and entry.get("chained", False):
                        previous = [backup]
                elif target.exists() or target.is_symlink():
                    backup = str(hooks / (name + ".agent-backup-" + uuid.uuid4().hex))
                    managed = legacy_managed(target, ROOT / "hooks/pre-push") if name == "pre-push" else (target.is_symlink() and lint.exists() and target.resolve() == lint.resolve())
                    shutil.copy2(target, backup, follow_symlinks=False)
                    created_backups.append(Path(backup))
                    if os.access(target, os.X_OK) and not managed:
                        previous = [backup]
                commands = [[str(source)], *([previous] if previous else [])]
                body = dispatcher(commands)
                if target.is_file() and target.read_text() == body and entry:
                    print("already installed: " + str(target))
                    continue
                temp_target = snapshots / (name + ".new")
                temp_target.write_text(body)
                temp_target.chmod(0o755)
                temp_target.replace(target)
                changed.append(name)
                manifest["hooks"][name] = dict(source=str(source), backup=backup, previous=previous,
                    chained=bool(previous), dispatcher_sha256=hashlib.sha256(body.encode()).hexdigest(),
                    rollback="remove dispatcher; restore backup to hook path, or leave absent if backup=null")
                print("installed: " + str(target))
            manifest.update(source_hash=source_hash, source_root=str(ROOT), runtime=str(runtime),
                            repository=str(repo), installed_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
            temp_manifest = snapshots / "manifest.new"
            temp_manifest.write_text(json.dumps(manifest, indent=2) + "\n")
            temp_manifest.replace(manifest_path)
        except BaseException:
            for name in reversed(changed):
                snapshot = snapshots / name
                if snapshot.exists() or snapshot.is_symlink():
                    snapshot.replace(hooks / name)
                else:
                    (hooks / name).unlink(missing_ok=True)
            if original_manifest is None:
                manifest_path.unlink(missing_ok=True)
            else:
                restore = snapshots / "manifest.restore"
                restore.write_bytes(original_manifest)
                restore.replace(manifest_path)
            for backup in created_backups:
                backup.unlink(missing_ok=True)
            raise


def install(args):
    if not args:
        raise Rejected("usage: install-hooks.sh REPO [REPO ...]")
    for arg in args:
        repo = Path(arg).resolve()
        hooks = Path(git("rev-parse", "--path-format=absolute", "--git-path", "hooks", cwd=repo).stdout.strip())
        hooks.mkdir(parents=True, exist_ok=True)
        with (hooks / ".agent-tooling-install.lock").open("a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            install_one(repo, hooks)
    return 0


def main():
    try:
        command, args = sys.argv[1], sys.argv[2:]
        return {"hook": hook, "push": wrapper, "install": install}[command](args)
    except (Rejected, OSError) as e:
        print("agent-publish: " + redact(str(e)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
