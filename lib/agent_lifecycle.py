#!/usr/bin/env python3
"""Local advisory coordination. State transitions are serialized and fail closed.

Identity is deliberately shared by linked Git worktrees, but independent clones
have independent identities. Actor/session are declared identities, not authentication.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid

VERSION = 3


class Failure(Exception):
    pass


class Unmanaged(Failure):
    pass


def git(path, *args, check=True):
    p = subprocess.run(['git', '-C', str(path), *args], capture_output=True, text=True)
    if check and p.returncode:
        raise Failure(p.stderr.strip() or f'git {args[0]} failed')
    return p


def iso():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def repository(path):
    path = Path(path or '.').expanduser()
    if not path.exists():
        candidates = [Path.home() / 'Documents' / path, Path.home() / path]
        path = next((p for p in candidates if p.exists()), path)
    common = os.path.realpath(git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip())
    return {'repo_id': digest(common), 'common_dir': common, 'repo': str(path.resolve())}


def safe_name(value, label):
    if not value or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', value):
        raise Failure(f'{label} must be 1..128 safe letters, digits, dot, underscore or dash; no paths')
    return value


def branch_name(repo, value):
    if not value:
        value = git(repo, 'symbolic-ref', '--quiet', '--short', 'HEAD').stdout.strip()
    if value.startswith('-') or git(repo, 'check-ref-format', '--branch', value, check=False).returncode:
        raise Failure('invalid branch name')
    return value


def directory(kind):
    key = 'AGENT_LOCK_DIR' if kind == 'locks' else 'AGENT_WORKTREE_DIR'
    root = Path(os.environ.get(key, str(Path.home() / '.cache' / f'agent-{kind}')))
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def serialized(root, repo_id):
    # The sidecar inode is never replaced or deleted, unlike the JSON payload.
    with (root / f'{repo_id}.mutex').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise Failure(f'malformed/unreadable state {path}: {exc}; use reconcile (read-only) and repair explicitly') from exc


def atomic_json(path, payload):
    fd, tmp = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def audit(root, event, args, repo, previous=None):
    entry = {'time': iso(), 'event': event, 'repo_id': repo['repo_id'],
             'actor': args.agent, 'session': args.session, 'reason': args.reason,
             'previous': previous}
    with (root / f"{repo['repo_id']}.audit.jsonl").open('a') as stream:
        stream.write(json.dumps(entry, sort_keys=True) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def legacy_files(root, repo, suffix):
    """Find old path-keyed state, including paths of now-missing worktrees.

    Unreadable unknown legacy files fail closed: ignoring one could hide a lock.
    """
    matches = []
    for file in sorted(root.glob(f'*{suffix}')):
        if re.fullmatch(r'[a-f0-9]{64}(?:\.[a-f0-9]{64})?' + re.escape(suffix), file.name):
            continue
        data = read_json(file)
        records = data if isinstance(data, list) else [data]
        if not all(isinstance(record, dict) for record in records):
            raise Failure(f'malformed legacy state {file}; inspect with reconcile')
        for record in records:
            old_repo = record.get('repo')
            if not isinstance(old_repo, str):
                raise Failure(f'legacy state {file} has no repository identity; inspect with reconcile')
            try:
                same = repository(old_repo)['repo_id'] == repo['repo_id']
            except Failure:
                # Path-keyed legacy records often store the original primary path.
                same = os.path.realpath(old_repo) in (repo['repo'], str(Path(repo['common_dir']).parent))
            if same:
                matches.append((file, data))
                break
    return matches


def registered_worktrees(repo):
    blocks = git(repo['repo'], 'worktree', 'list', '--porcelain', '-z').stdout.split('\0\0')
    rows = []
    for block in blocks:
        row = {}
        for line in block.strip('\0').split('\0'):
            key, _, value = line.partition(' ')
            row[key] = value
        if row.get('worktree'):
            row['worktree'] = os.path.realpath(row['worktree'])
            rows.append(row)
    return rows


def validate_record(record, repo):
    if not isinstance(record, dict):
        raise Failure('malformed session record')
    safe_name(record.get('agent'), 'record actor')
    safe_name(record.get('session'), 'record session')
    if record.get('repo_id') != repo['repo_id'] or not isinstance(record.get('path'), str) or not os.path.isabs(record['path']):
        raise Failure('session record repository/path mismatch')
    if not isinstance(record.get('branch'), str) or not record['branch']:
        raise Failure('session record has no branch')
    branch_name(repo['repo'], record['branch'])
    if record.get('lifecycle', 'active') not in ('active', 'orphan', 'closing'):
        raise Failure('invalid session lifecycle state')
    if record.get('lifecycle') == 'closing' and not re.fullmatch(r'[a-f0-9]{40,64}', record.get('close_oid', '')):
        raise Failure('malformed closing session commit')
    return record


def registry(root, repo, allow_legacy=False):
    old = legacy_files(root, repo, '.json')
    if old and not allow_legacy:
        raise Failure('legacy session registry found; run agent-worktree reconcile <repo> --json, then migrate <repo> explicitly')
    file = root / f"{repo['repo_id']}.json"
    if not file.exists():
        return file, []
    data = read_json(file)
    if not isinstance(data, dict) or data.get('version') != VERSION or data.get('repo_id') != repo['repo_id'] or not isinstance(data.get('sessions'), list):
        raise Failure(f'malformed registry {file}; run reconcile, repair explicitly')
    records = [validate_record(row, repo) for row in data['sessions']]
    if len({row['session'] for row in records}) != len(records) or len({row['path'] for row in records}) != len(records):
        raise Failure(f'duplicate session/path in registry {file}')
    return file, records


def save_registry(file, repo, records):
    atomic_json(file, {'version': VERSION, 'repo_id': repo['repo_id'], 'common_dir': repo['common_dir'], 'sessions': records})


def find_session(records, args, repo):
    if args.session:
        matches = [r for r in records if r['session'] == args.session]
    else:
        top = os.path.realpath(git(repo['repo'], 'rev-parse', '--show-toplevel').stdout.strip())
        matches = [r for r in records if os.path.realpath(r['path']) == top]
    if len(matches) != 1:
        raise Failure('session not identified; pass --session <stable-id> or run inside its managed worktree')
    rec = matches[0]
    if args.agent_explicit and rec['agent'] != args.agent:
        raise Failure('session belongs to another actor')
    return rec


def identity(args, repo):
    root = directory('worktrees')
    with serialized(root, repo['repo_id']):
        _, records = registry(root, repo)
        top = os.path.realpath(git(repo['repo'], 'rev-parse', '--show-toplevel').stdout.strip())
        current = next((r for r in records if os.path.realpath(r['path']) == top), None)
        if current:
            if args.session and args.session != current['session']:
                raise Failure('declared session conflicts with current managed worktree')
            if args.agent_explicit and args.agent != current['agent']:
                raise Failure('declared actor conflicts with current managed worktree')
            rec = current
        elif args.session_explicit:
            # Explicit lookup from the primary is useful for a separate shell.
            rec = find_session(records, args, repo)
        else:
            raise Unmanaged('worktree is not managed; supply explicit actor/session to the publishing wrapper')
        if rec.get('lifecycle', 'active') != 'active':
            raise Failure(f'session is {rec["lifecycle"]}; reconcile or complete close explicitly')
        rows = registered_worktrees(repo)
        actual = next((r for r in rows if r['worktree'] == os.path.realpath(rec['path'])), None)
        if not actual or not Path(rec['path']).is_dir():
            raise Failure('registered session worktree is missing; run reconcile; record retained')
        if actual.get('branch') != f"refs/heads/{rec['branch']}":
            raise Failure('registered session branch changed; run reconcile; record retained')
        return dict(rec, actor=rec['agent'], worktree=rec['path'])


def session_owner(args, repo):
    # Declared identities may be used from the primary/unmanaged shell, but
    # must never impersonate a different session inside a managed worktree.
    current_args = argparse.Namespace(**vars(args))
    current_args.session_explicit = False
    try:
        rec = identity(current_args, repo)
    except Unmanaged:
        if not args.session:
            raise Failure('stable session required: pass --session or run inside its managed worktree')
    else:
        args.session = rec['session']
        if not args.agent_explicit:
            args.agent = rec['agent']
    safe_name(args.agent, 'actor')
    safe_name(args.session, 'session')


def default_base(repo):
    # Consult the remote now; origin/HEAD or origin/main alone may be stale.
    output = git(repo['repo'], 'ls-remote', '--symref', 'origin', 'HEAD').stdout.splitlines()
    ref = next((line.split()[1] for line in output if line.startswith('ref: ')), '')
    oid = next((line.split()[0] for line in output if not line.startswith('ref: ') and line.endswith('\tHEAD')), '')
    if not ref.startswith('refs/heads/') or not re.fullmatch(r'[a-f0-9]{40,64}', oid):
        raise Failure('origin has no advertised default branch; set remote HEAD before opening')
    # Fetch exactly that default into its tracking ref. Never fall back to HEAD.
    target = f"refs/remotes/origin/{ref[len('refs/heads/'):]}"
    git(repo['repo'], 'fetch', '--quiet', 'origin', f'{ref}:{target}')
    fetched = git(repo['repo'], 'rev-parse', '--verify', target).stdout.strip()
    if fetched != oid:
        raise Failure('remote default moved while resolving; retry open')
    return target, fetched


def remote_preservation(repo, oid):
    """Prove ancestry against freshly advertised remote heads, not stale refs."""
    git(repo['repo'], 'fetch', '--quiet', '--all', '--prune')
    preserved = []
    for remote in git(repo['repo'], 'remote').stdout.splitlines():
        for line in git(repo['repo'], 'ls-remote', '--heads', remote).stdout.splitlines():
            tip, ref = line.split('\t', 1)
            if git(repo['repo'], 'merge-base', '--is-ancestor', oid, tip, check=False).returncode == 0:
                preserved.append({'remote': remote, 'ref': ref, 'oid': tip})
    return preserved


def report(args, payload):
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    elif isinstance(payload, list):
        for item in payload:
            print(json.dumps(item, sort_keys=True))
    else:
        print(payload.get('message', json.dumps(payload, sort_keys=True)))


def lock_path(root, repo, branch):
    """The canonical lock file for a branch inside a repository identity."""
    return root / f"{repo['repo_id']}.{digest(branch)}.lock"


def release_own_lock(root, repo, branch, agent, session, args, reason):
    """Release the lock of this branch when this session owns it.

    A session that ends its worktree must not leave its branch locked, because
    the next session on the same branch would be refused by a stale lock. Only
    the lock of the same actor and session is touched; an expired lock of this
    session is released too, because the owner is exactly the one that is
    closing. A lock of another actor is never removed, not even when expired.
    """
    file = lock_path(root, repo, branch)
    if not file.exists():
        return None
    try:
        data = read_json(file)
    except Failure:
        return {'status': 'malformed', 'branch': branch}
    if not isinstance(data, dict):
        return {'status': 'malformed', 'branch': branch}
    if data.get('agent') != agent or data.get('session') != session:
        return {'status': 'foreign', 'branch': branch,
                'owner': {'agent': data.get('agent'), 'session': data.get('session')}}
    audit(root, 'lock-release', args, repo, data)
    file.unlink(missing_ok=True)
    return {'status': 'released', 'branch': branch, 'expired': data.get('expires_epoch', 0) <= time.time()}


def missing_worktree_close_oid(repo, rec):
    """Prove that the commit of a vanished worktree is still preserved.

    The worktree directory can disappear without the tool being the one that
    removed it. The record must not be dropped until the commit of its branch
    is proven to be reachable somewhere else, because the branch is the only
    reference that keeps a local-only commit alive.
    """
    ref = f"refs/heads/{rec['branch']}"
    current = git(repo['repo'], 'rev-parse', '--verify', ref, check=False)
    if current.returncode == 0:
        oid = current.stdout.strip()
        if re.fullmatch(r'[a-f0-9]{40,64}', oid):
            return oid, 'local branch'
    # The branch itself is gone. The commit of the session cannot be identified
    # from the record alone, and the base commit of the session says nothing
    # about the work it did, so nothing is proven and the record is retained.
    return None, None


def detect_collisions(repo, source_oid, source_label, targets, exclude_branch=None):
    """Replay a three-way merge in memory to find textual collisions.

    git merge-tree --write-tree computes the merge without touching any working
    tree or index, so the check is safe to run while other sessions work. It
    detects the textual layer of conflicts only: content overlaps and the
    structural modify/delete and add/add cases.
    """
    collisions = []
    for label, oid in targets:
        if not oid or not re.fullmatch(r'[a-f0-9]{40,64}', oid):
            continue
        if exclude_branch and label == exclude_branch:
            continue
        p = git(repo['repo'], 'merge-tree', '--write-tree', oid, source_oid, check=False)
        if p.returncode == 0:
            continue
        if p.returncode != 1:
            # A merge that cannot even be computed is reported as unknown
            # rather than as a clean result.
            collisions.append({'target': label, 'type': 'unknown', 'paths': [],
                               'detail': (p.stderr or p.stdout).strip()[:200]})
            continue
        paths = []
        for line in p.stdout.splitlines():
            match = re.match(r'^CONFLICT \(([^)]+)\): (?:Merge conflict in )?(.+?)(?:  Version .*)?$', line)
            if match:
                kind, path = match.group(1), match.group(2).strip()
                paths.append({'type': kind, 'path': path})
        collisions.append({'target': label, 'type': 'collision', 'paths': paths})
    return {'source': source_label, 'collisions': collisions,
            'status': 'conflict' if collisions else 'clean'}


def active_branches(repo, records, exclude_session=None):
    """Branches of the sessions that are currently open, with their tip."""
    rows = []
    for rec in records:
        if rec.get('lifecycle', 'active') != 'active':
            continue
        if exclude_session and rec['session'] == exclude_session:
            continue
        ref = f"refs/heads/{rec['branch']}"
        oid = git(repo['repo'], 'rev-parse', '--verify', ref, check=False)
        if oid.returncode:
            continue
        rows.append({'session': rec['session'], 'agent': rec['agent'], 'branch': rec['branch'],
                     'oid': oid.stdout.strip()})
    return rows


def default_branch_oid(repo, remote='origin'):
    """Resolve the default branch of a remote and its locally known tip.

    The remote is the authority for which branch is the default, because that
    is the branch that will receive the merges. The tip is read from the local
    tracking ref, so the radar does not have to reach the network.
    """
    output = git(repo['repo'], 'ls-remote', '--symref', remote, 'HEAD', check=False).stdout.splitlines()
    ref = next((line.split()[1] for line in output if line.startswith('ref: ')), '')
    if not ref.startswith('refs/heads/'):
        return None, None
    name = ref[len('refs/heads/'):]
    for candidate in (f'refs/remotes/{remote}/{name}', 'refs/remotes/origin/HEAD'):
        oid = git(repo['repo'], 'rev-parse', '--verify', '--quiet', candidate, check=False)
        if oid.returncode == 0:
            return ref, oid.stdout.strip()
    return ref, None


def radar(args, repo):
    """Report textual collisions between the session and the other branches.

    The check runs entirely in memory through git merge-tree, so it never
    touches a working tree and it never blocks anything. It answers one
    question: which files of this session collide with the work of another
    session or with the default branch of the remote.
    """
    root = directory('worktrees')
    _, records = registry(root, repo, allow_legacy=True)
    top = os.path.realpath(git(repo['repo'], 'rev-parse', '--show-toplevel').stdout.strip())
    rec = None
    if args.session:
        matches = [r for r in records if r['session'] == args.session]
        if matches:
            rec = matches[0]
    else:
        rec = next((r for r in records if os.path.realpath(r['path']) == top), None)
    if rec:
        oid = git(repo['repo'], 'rev-parse', '--verify', f"refs/heads/{rec['branch']}", check=False)
        if oid.returncode:
            raise Failure('session branch has no commit to compare yet')
        source = {'session': rec['session'], 'agent': rec['agent'], 'branch': rec['branch'],
                  'oid': oid.stdout.strip(), 'worktree': rec['path']}
    else:
        # A checkout that is not a managed worktree, or a session that is not
        # registered, still has a branch to compare, so the radar is useful
        # outside a managed session.
        branch = git(repo['repo'], 'symbolic-ref', '--quiet', '--short', 'HEAD', check=False).stdout.strip()
        oid = git(repo['repo'], 'rev-parse', '--verify', 'HEAD', check=False)
        if not branch or oid.returncode:
            raise Failure('no branch to compare; pass --session <stable-id>')
        source = {'session': None, 'agent': None, 'branch': branch,
                  'oid': oid.stdout.strip(), 'worktree': top}
    targets = []
    default_ref, default_oid = default_branch_oid(repo, args.remote)
    if default_ref and default_oid:
        targets.append((default_ref, default_oid))
    for row in active_branches(repo, records, exclude_session=source['session']):
        targets.append((row['branch'], row['oid']))
    result = detect_collisions(repo, source['oid'], source['branch'], targets)
    result['repo_id'] = repo['repo_id']
    result['source'] = source
    result['sessions'] = active_branches(repo, records)
    report(args, result)
    return 0


def worktree_command(args):
    repo = repository(args.repo)
    root = directory('worktrees')
    if args.command == 'identity':
        report(args, identity(args, repo))
        return 0
    if args.command == 'radar':
        # The radar only reads state and replays merges in memory, so it does
        # not take the session mutex: it must be usable while other sessions
        # are working.
        return radar(args, repo)
    with serialized(root, repo['repo_id']):
        if args.command == 'reconcile':
            file, records = registry(root, repo, allow_legacy=True)
            actual = registered_worktrees(repo)
            paths = {r['worktree']: r for r in actual}
            diagnostics = []
            for rec in records:
                row = paths.get(os.path.realpath(rec['path']))
                status = rec.get('lifecycle', 'active')
                if status == 'active':
                    status = 'ok' if row and Path(rec['path']).is_dir() and row.get('branch') == f"refs/heads/{rec['branch']}" else 'missing-or-changed'
                diagnostics.append({'session': rec['session'], 'worktree': rec['path'], 'status': status})
            old = legacy_files(root, repo, '.json')
            report(args, {'repo_id': repo['repo_id'], 'sessions': diagnostics, 'git_worktrees': actual,
                          'legacy_files': [str(p) for p, _ in old], 'read_only': True,
                          'counts': {'registered': len(records), 'legacy_files': len(old), 'git_worktrees': len(actual), 'ok': sum(r['status'] == 'ok' for r in diagnostics), 'orphan_or_changed': sum(r['status'] not in ('ok', 'closing') for r in diagnostics), 'closing': sum(r['status'] == 'closing' for r in diagnostics)},
                          'action': 'migrate explicitly; missing records/data are never deleted by reconcile'})
            return 0
        if args.command == 'migrate':
            # Migration rewrites how sessions are tracked, so it needs a reason
            # in the audit trail, like the lock migration does.
            if not (args.reason or '').strip():
                raise Failure('migration requires --reason for the audit trail')
            file, records = registry(root, repo, allow_legacy=True)
            old = legacy_files(root, repo, '.json')
            actual = {r['worktree']: r for r in registered_worktrees(repo)}
            for source, data in old:
                if not isinstance(data, list):
                    raise Failure(f'legacy registry {source} is not an array')
                for row in data:
                    rec = dict(row, repo_id=repo['repo_id'])
                    validate_record(rec, repo)
                    worktree = actual.get(os.path.realpath(rec['path']))
                    active = bool(worktree and worktree.get('branch') == f"refs/heads/{rec['branch']}" and Path(rec['path']).is_dir())
                    rec['lifecycle'] = 'active' if active else 'orphan'
                    rec['legacy_record'] = row
                    if not active:
                        rec['orphan_reason'] = 'legacy path missing or Git branch/worktree changed; original payload retained; inspect manually'
                    existing = next((r for r in records if r['session'] == rec['session']), None)
                    if existing and existing != rec:
                        raise Failure('legacy session conflicts with canonical registry')
                    if not existing:
                        records.append(rec)
            if len({r['path'] for r in records}) != len(records):
                raise Failure('legacy worktree path conflicts with canonical registry')
            save_registry(file, repo, records)
            for source, _ in old:
                source.rename(source.with_suffix('.json.migrated'))
            audit(root, 'worktree-migrate', args, repo, {'migrated': len(old), 'sessions': len(records)})
            report(args, {'message': f'migrated {len(old)} legacy registries', 'repo_id': repo['repo_id']})
            return 0
        file, records = registry(root, repo)
        if args.command == 'list':
            report(args, {'repo_id': repo['repo_id'], 'sessions': records, 'git_worktrees': registered_worktrees(repo)})
            return 0
        if args.command == 'open':
            safe_name(args.agent, 'actor')
            args.session = args.session or f'{args.agent}-{uuid.uuid4().hex}'
            safe_name(args.session, 'session')
            if any(r['session'] == args.session for r in records):
                rec = find_session(records, args, repo)
                actual = next((r for r in registered_worktrees(repo) if r['worktree'] == os.path.realpath(rec['path'])), None)
                if rec['agent'] != args.agent or rec.get('lifecycle', 'active') != 'active' or not Path(rec['path']).is_dir() or not actual or actual.get('branch') != f"refs/heads/{rec['branch']}":
                    raise Failure('session ownership mismatch or orphan record; run reconcile')
                report(args, dict(rec, actor=rec['agent'], worktree=rec['path'], message=f'session {args.session} already open: {rec["path"]}'))
                return 0
            args.branch = branch_name(repo['repo'], args.branch or f'agent/{args.agent}/{args.session}')
            if git(repo['repo'], 'show-ref', '--verify', '--quiet', f'refs/heads/{args.branch}', check=False).returncode == 0:
                raise Failure(f'la rama {args.branch} ya existe; choose a new --branch')
            base, oid = default_base(repo)
            # Outside the clone: .gitignore reinclusion rules cannot expose nested worktrees.
            parent = Path(repo['common_dir']).parent
            wtroot = parent.parent / '.agent-worktrees' / repo['repo_id']
            wtroot.mkdir(parents=True, exist_ok=True)
            path = wtroot / args.session
            if path.exists() or path.is_symlink():
                raise Failure(f'worktree path already exists: {path}; inspect manually')
            git(repo['repo'], 'worktree', 'add', '-b', args.branch, str(path), oid)
            rec = {'repo_id': repo['repo_id'], 'session': args.session, 'agent': args.agent,
                   'repo': repo['repo'], 'path': str(path), 'branch': args.branch, 'opened_at': iso(), 'base': base, 'base_oid': oid, 'lifecycle': 'active'}
            try:
                save_registry(file, repo, records + [rec])
            except Exception as exc:
                raise Failure(f'worktree created at {path}, branch {args.branch}, but registry write failed; preserve it and reconcile: {exc}') from exc
            audit(root, 'worktree-open', args, repo, {'branch': args.branch, 'branch_oid': oid, 'worktree': str(path), 'base': base})
            report(args, dict(rec, actor=rec['agent'], worktree=rec['path'], message=f'session {args.session} open\n  rama: {args.branch}\n  ruta: {path}\n  base: {base}'))
            return 0
        rec = find_session(records, args, repo)
        # Mutations always require a matching declared actor, even if defaulted.
        if args.command == 'close' and rec['agent'] != args.agent:
            raise Failure('session belongs to another actor; close denied')
        if args.command == 'path':
            if not Path(rec['path']).is_dir():
                raise Failure('session path missing; run reconcile; record retained')
            print(rec['path']) if not args.json else report(args, dict(rec, worktree=rec['path'], actor=rec['agent']))
            return 0
        if args.command != 'close':
            raise Failure('unknown worktree command')
        if rec.get('lifecycle') == 'orphan':
            raise Failure('orphan record retained for manual reconciliation; close cannot remove its data or branch')
        path = Path(rec['path'])
        rows = registered_worktrees(repo)
        actual = next((r for r in rows if r['worktree'] == os.path.realpath(path)), None)
        if rec.get('lifecycle') == 'closing' and not path.exists() and not actual:
            # Previous removal succeeded but a later state write failed. Require
            # the recorded commit to remain protected before completing the close.
            ref = f"refs/heads/{rec['branch']}"
            current = git(repo['repo'], 'rev-parse', '--verify', ref, check=False)
            if current.returncode == 0:
                preserved = git(repo['repo'], 'merge-base', '--is-ancestor', rec['close_oid'], ref, check=False).returncode == 0
            else:
                preserved = bool(rec.get('close_prune') and remote_preservation(repo, rec['close_oid']))
            if not preserved:
                raise Failure('interrupted close commit preservation unproven; registry retained; inspect manually')
            save_registry(file, repo, [r for r in records if r['session'] != rec['session']])
            released = release_own_lock(directory('locks'), repo, rec['branch'], rec['agent'], rec['session'], args, 'auto-released on interrupted close')
            report(args, {'message': f'interrupted close completed for {rec["session"]}; commit preserved', 'repo_id': repo['repo_id'], 'branch': rec['branch'], 'branch_retained': current.returncode == 0, 'lock': released})
            return 0
        if not actual or not path.is_dir() or actual.get('branch') != f"refs/heads/{rec['branch']}":
            # The worktree directory can disappear without this tool removing
            # it, for example with a direct `git worktree remove`. The record
            # must not be dropped silently: the commit of the branch has to be
            # proven preserved first, otherwise the last reference to a local
            # commit would disappear with the record.
            oid, proof = missing_worktree_close_oid(repo, rec)
            if not oid:
                raise Failure('session worktree missing and branch commit preservation unproven; registry retained; inspect manually')
            if not actual and not path.exists():
                branch_ref = f"refs/heads/{rec['branch']}"
                if args.prune:
                    # Pruning deletes the branch, so the commit has to be
                    # proven to live on a remote head first. A local-only
                    # commit would lose its last reference.
                    if not remote_preservation(repo, oid):
                        raise Failure('branch contains local-only work or squash integration is unproven; retained; close without --prune')
                save_registry(file, repo, [r for r in records if r['session'] != rec['session']])
                if args.prune:
                    git(repo['repo'], 'update-ref', '-d', branch_ref, oid)
                released = release_own_lock(directory('locks'), repo, rec['branch'], rec['agent'], rec['session'], args, 'auto-released on close of a vanished worktree')
                audit(root, 'worktree-close', args, repo, {'branch': rec['branch'], 'branch_oid': oid,
                                                            'branch_pruned': bool(args.prune), 'worktree': str(path),
                                                            'worktree_absent': True, 'preservation': proof})
                report(args, {'message': f'session {rec["session"]} closed; worktree was already absent; commit preserved ({proof})',
                              'repo_id': repo['repo_id'], 'branch': rec['branch'], 'branch_retained': not args.prune,
                              'commit': oid, 'preservation': proof, 'lock': released})
                return 0
            raise Failure('session worktree missing or branch changed; run reconcile; record retained')
        # Even --force cannot discard untracked, ignored, or tracked user files.
        dirty = git(path, 'status', '--porcelain', '--untracked-files=all', '--ignored=matching').stdout
        if dirty:
            raise Failure('cambios sin commitear (including ignored/untracked files); preserve or remove them explicitly before close')
        branch_ref = f"refs/heads/{rec['branch']}"
        branch_oid = git(repo['repo'], 'rev-parse', '--verify', branch_ref).stdout.strip()
        if args.prune:
            # Only an actually fetched remote ref reaching this exact commit proves
            # preservation. Squash merges are intentionally not inferred by patches.
            refs = remote_preservation(repo, branch_oid)
            if not refs:
                raise Failure('branch contains local-only work or squash integration is unproven; retained; close without --prune')
        closing = dict(rec, lifecycle='closing', close_oid=branch_oid, close_prune=args.prune)
        closing_records = [closing if r['session'] == rec['session'] else r for r in records]
        save_registry(file, repo, closing_records)
        try:
            git(repo['repo'], 'worktree', 'remove', str(path))
        except Failure:
            save_registry(file, repo, records)
            raise
        if args.prune:
            # Compare-and-delete prevents racing an unrelated branch update.
            git(repo['repo'], 'update-ref', '-d', branch_ref, branch_oid)
        save_registry(file, repo, [r for r in records if r['session'] != rec['session']])
        # Closing a session and deleting a branch are the destructive steps, so
        # they leave an audit trail. A retained branch is also recorded, because
        # the record of the session ends here.
        audit(root, 'worktree-close', args, repo, {'branch': rec['branch'], 'branch_oid': branch_oid,
                                                   'branch_pruned': bool(args.prune), 'worktree': str(path)})
        # The session is over, so its own lock on that branch is released here.
        # A lock of another actor is left untouched.
        released = release_own_lock(directory('locks'), repo, rec['branch'], rec['agent'], rec['session'], args, 'auto-released on worktree close')
        report(args, {'message': f'session {rec["session"]} closed; branch {rec["branch"]} ' + ('pruned' if args.prune else 'retained'), 'branch': rec['branch'], 'branch_retained': not args.prune, 'repo_id': repo['repo_id'], 'lock': released})
        return 0


def validate_lock(data, repo, branch):
    if not isinstance(data, dict) or data.get('version') != VERSION or data.get('repo_id') != repo['repo_id'] or data.get('branch') != branch:
        raise Failure('malformed lock identity/version/branch; inspect reconcile and repair explicitly')
    safe_name(data.get('agent'), 'lock actor')
    safe_name(data.get('session'), 'lock session')
    expiration = data.get('expires_epoch')
    if isinstance(expiration, bool) or not isinstance(expiration, (int, float)) or not math.isfinite(expiration) or expiration <= 0:
        raise Failure('malformed lock expiry; refusing to treat it as free')
    return data


def lock_command(args):
    root = directory('locks')
    if args.command == 'list' and not args.repo:
        rows = []
        for file in sorted(root.glob('*.lock')):
            data = read_json(file)
            if not isinstance(data, dict):
                raise Failure(f'malformed lock {file}')
            rows.append(dict(data, file=str(file)))
        report(args, rows)
        return 0
    repo = repository(args.repo)
    args.branch = branch_name(repo['repo'], args.branch)
    with serialized(root, repo['repo_id']):
        old = legacy_files(root, repo, '.lock')
        if args.command == 'reconcile':
            report(args, {'repo_id': repo['repo_id'], 'legacy_files': [str(p) for p, _ in old], 'read_only': True,
                          'action': 'migrate legacy locks explicitly with --agent --session --branch --reason'})
            return 0
        if old and args.command != 'migrate':
            raise Failure('legacy locks found; run agent-lock reconcile <repo> --json then migrate explicitly')
        file = root / f"{repo['repo_id']}.{digest(args.branch)}.lock"
        data = validate_lock(read_json(file), repo, args.branch) if file.exists() else None
        if args.command == 'check':
            # No session means no ownership claim. It can still determine free/busy.
            if args.session:
                session_owner(args, repo)
            safe_name(args.agent, 'actor')
            status = 'free' if data is None else ('expired' if data['expires_epoch'] <= time.time() else ('owned' if data['agent'] == args.agent and data['session'] == args.session else 'busy'))
            report(args, {'status': status, 'repo_id': repo['repo_id'], 'branch': args.branch, 'lock': data})
            return 0 if status in ('free', 'owned') else 1
        session_owner(args, repo)
        if args.force:
            raise Failure('--force no longer bypasses ownership; expired recovery requires recover --reason')
        if args.command == 'migrate':
            if not args.reason:
                raise Failure('migration requires --reason for the audit trail')
            if len(old) != 1 or data:
                raise Failure('expected exactly one legacy lock and no canonical lock; inspect manually')
            source, previous = old[0]
            if not isinstance(previous, dict) or previous.get('agent') != args.agent or previous.get('branch') != args.branch or previous.get('session', '') not in ('', args.session):
                raise Failure('legacy lock actor/session/branch mismatch; refusing migration')
            data = dict(previous, version=VERSION, repo_id=repo['repo_id'], session=args.session)
            validate_lock(data, repo, args.branch)
            audit(root, 'lock-migrate', args, repo, previous)
            atomic_json(file, data)
            source.rename(source.with_suffix('.lock.migrated'))
            report(args, {'status': 'migrated', 'repo_id': repo['repo_id'], 'branch': args.branch})
            return 0
        if args.command == 'recover':
            if not args.reason:
                raise Failure('recovery requires --reason for the audit trail')
            if not data or data['expires_epoch'] > time.time():
                raise Failure('only an existing expired lock may be recovered; active ownership cannot be bypassed')
            audit(root, 'lock-recover', args, repo, data)
        elif args.command in ('renew', 'release'):
            if not data:
                raise Failure('no lock exists; cannot assert ownership')
            if data['agent'] != args.agent or data['session'] != args.session:
                raise Failure('lock belongs to another actor/session; No lo libero')
            if data['expires_epoch'] <= time.time():
                raise Failure('lock expired; explicit recover --reason required')
        elif args.command == 'acquire':
            if data:
                if data['expires_epoch'] > time.time() and data['agent'] == args.agent and data['session'] == args.session:
                    args.command = 'renew'
                else:
                    raise Failure('lock exists; use renew if owned, or recover --reason if expired')
        else:
            raise Failure('unknown lock command')
        if args.command == 'release':
            audit(root, 'lock-release', args, repo, data)
            file.unlink()
            report(args, {'status': 'released', 'repo_id': repo['repo_id'], 'branch': args.branch})
            return 0
        if not math.isfinite(args.ttl) or args.ttl <= 0:
            raise Failure('--ttl must be a positive finite number of minutes')
        payload = {'version': VERSION, 'repo_id': repo['repo_id'], 'repo': repo['repo'], 'common_dir': repo['common_dir'], 'agent': args.agent,
                   'session': args.session, 'host': socket.gethostname(), 'branch': args.branch, 'worktree': args.worktree or repo['repo'],
                   'note': args.note, 'acquired': data['acquired'] if args.command == 'renew' else iso(),
                   'updated_at': iso(), 'expires_epoch': time.time() + args.ttl * 60}
        atomic_json(file, payload)
        report(args, {'status': 'owned', 'repo_id': repo['repo_id'], 'branch': args.branch, 'lock': payload})
        return 0


def main(tool=None):
    if tool is None and len(sys.argv) > 1 and sys.argv[1] in ('agent-worktree', 'agent-lock'):
        tool = sys.argv.pop(1)
    tool = tool or Path(sys.argv[0]).name
    parser = argparse.ArgumentParser(description=__doc__)
    commands = ('open', 'close', 'path', 'identity', 'list', 'reconcile', 'migrate', 'radar') if tool == 'agent-worktree' else ('acquire', 'check', 'renew', 'release', 'recover', 'list', 'reconcile', 'migrate')
    parser.add_argument('command', choices=commands, nargs='?', default='list')
    parser.add_argument('repo', nargs='?', default=None)
    parser.add_argument('--agent', default=os.environ.get('AGENT_ACTOR', os.environ.get('AGENT_NAME', getpass.getuser())))
    parser.add_argument('--session', default=os.environ.get('AGENT_SESSION', os.environ.get('AGENT_SESSION_ID', '')))
    parser.add_argument('--branch', default='')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--force', action='store_true', help='deprecated; never discards user work or bypasses lock ownership')
    parser.add_argument('--prune', action='store_true', help='close only: delete branch only if a fetched remote ref preserves the exact commit')
    parser.add_argument('--ttl', type=float, default=90)
    parser.add_argument('--note', default='')
    parser.add_argument('--reason', default='')
    parser.add_argument('--worktree', default='')
    parser.add_argument('--remote', default='origin')
    args = parser.parse_args()
    args.agent_explicit = '--agent' in sys.argv or 'AGENT_NAME' in os.environ or 'AGENT_ACTOR' in os.environ
    args.session_explicit = '--session' in sys.argv
    try:
        return worktree_command(args) if tool == 'agent-worktree' else lock_command(args)
    except Unmanaged as exc:
        if args.json:
            print(json.dumps({'status': 'unmanaged', 'error': str(exc)}))
        else:
            print(f'{tool}: {exc}', file=sys.stderr)
        return 1
    except (Failure, OSError, ValueError, KeyError, TypeError) as exc:
        if args.json:
            print(json.dumps({'status': 'error', 'error': str(exc)}))
        else:
            print(f'{tool}: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
