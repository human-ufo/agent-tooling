#!/usr/bin/env python3
"""Cooperative local CI gates; validate immutable commits, never mutable HEAD."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit

REPOSITORY = 'avla-tech/avla-data-ingestion-platform'
REAL_GH = '/usr/bin/gh'
SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')


class GateError(Exception):
    pass


def command(args, cwd=None, env=None):
    p = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True)
    if p.returncode:
        # Raw scanner/API errors can contain credentials or source matches.
        raise GateError(f'{Path(args[0]).name} command failed (exit {p.returncode})')
    return p.stdout.strip()


def git(repo, *args):
    return command(['git', '-C', str(repo), *args])


def repository_identity(repo):
    common = git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    return hashlib.sha256(os.fsencode(Path(common).resolve())).hexdigest()


def ci_environment(key):
    if not re.fullmatch(r'[0-9a-f]{64}', key):
        raise GateError('missing trusted repository identity; reinstall publication hooks')
    allowed = {'PATH', 'HOME', 'USER', 'LOGNAME', 'TMPDIR', 'LANG', 'SYSTEMROOT'}
    env = {k: v for k, v in os.environ.items() if k in allowed or k.startswith('LC_')}
    env.update(CI_LOCAL_REPOSITORY_KEY=key, GIT_TERMINAL_PROMPT='0', PYTHONDONTWRITEBYTECODE='1')
    return env


def exact_commit(repo, sha):
    if not SHA.fullmatch(sha):
        raise GateError('expected a full announced object SHA')
    resolved = git(repo, 'rev-parse', '--verify', sha + '^{commit}')
    if not SHA.fullmatch(resolved):
        raise GateError('announced object does not resolve to a commit')
    return resolved


def remote_sha(repo, remote, ref):
    rows = [r.split() for r in git(repo, 'ls-remote', remote, ref).splitlines()]
    matches = [r[0] for r in rows if len(r) == 2 and r[1] == ref]
    if len(matches) != 1 or not SHA.fullmatch(matches[0]):
        raise GateError('remote ref is absent or ambiguous')
    return matches[0]


def fetch_main(repo, remote):
    git(repo, 'fetch', '--quiet', '--no-tags', remote,
        '+refs/heads/main:refs/remotes/ci-gate/main')
    return git(repo, 'rev-parse', 'refs/remotes/ci-gate/main')


def ci(repo, sha, scope, base, key, run=True):
    for name in ('ci_local.py', 'run-ci-local.sh', 'run-ci-selected.sh', 'check-repository-safety.sh'):
        path = repo / '.github/scripts' / name
        if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents)):
            raise GateError('required in-checkout CI script is absent or a symlink')
    env = ci_environment(key)
    script = repo / '.github/scripts/ci_local.py'
    tail = [sha, '--scope', scope, '--base', base]
    if run:
        logs = Path.home() / '.cache/agent-ci-ingestion'
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(mode='w', prefix=sha[:12] + '-', suffix='.log', dir=logs, delete=False) as output:
            path = output.name
            p = subprocess.run([sys.executable, str(script), 'run', *tail, '--reuse'],
                               cwd=repo, env=env, stdout=output, stderr=subprocess.STDOUT)
        print(f'CI {scope} sha={sha} returncode={p.returncode}; log={path}', flush=True)
        if p.returncode:
            raise GateError('local CI failed; publication/merge is blocked')
    record = json.loads(command([sys.executable, str(script), 'evidence', *tail], cwd=repo, env=env))
    identity = record.get('identity', {})
    if identity.get('sha') != sha or identity.get('requested_scope') != scope or record.get('returncode') != 0:
        raise GateError('CI evidence does not match the requested commit and scope')
    if scope == 'full' and identity.get('plan', {}).get('coverage') != 'full':
        raise GateError('full CI cannot reuse affected evidence')
    return record


@contextmanager
def snapshot(repo, sha, key):
    with tempfile.TemporaryDirectory(prefix='agent-ci-ingestion-') as temp:
        checkout = Path(temp) / 'checkout'
        env = ci_environment(key)
        command(['git', 'clone', '--quiet', '--shared', '--no-checkout', '--local', str(repo), str(checkout)], env=env)
        command(['git', 'checkout', '--quiet', '--detach', sha], cwd=checkout, env=env)
        yield checkout


def verify(announced):
    repo = Path.cwd().resolve()
    sha = exact_commit(repo, announced)
    if git(repo, 'rev-parse', 'HEAD') != sha or git(repo, 'status', '--porcelain', '--untracked-files=all'):
        raise GateError('verification requires the clean announced checkout')
    key = os.environ.get('AGENT_VERIFY_REPOSITORY_ID', '')
    remote = os.environ.get('AGENT_VERIFY_REMOTE_URL', '')
    if not remote:
        raise GateError('missing trusted remote; reinstall publication hooks')
    base = fetch_main(repo, remote)
    ci(repo, sha, 'affected', base, key)
    if remote_sha(repo, remote, 'refs/heads/main') != base:
        raise GateError('main moved during verification; retry publication')


def status(sha, scope):
    context = 'local-ci/platform' if scope == 'full' else 'local-ci/affected'
    command(['gh', 'api', '--method', 'POST', f'repos/{REPOSITORY}/statuses/{sha}',
             '-f', 'state=success', '-f', 'context=' + context,
             '-f', f'description=Local CI {scope} validated for this commit',
             '-f', f'target_url=https://github.com/{REPOSITORY}/commit/{sha}'])
    print(f'Published {context} for {sha}', flush=True)


def publish(repo, remote, ref, announced, key, run=False):
    if not ref.startswith('refs/heads/'):
        return
    if remote_sha(repo, remote, ref) != announced:
        raise GateError('published branch moved; no status will be attached')
    sha = exact_commit(repo, announced)
    with snapshot(repo, sha, key) as checkout:
        # The snapshot's origin is the local source clone, not GitHub.
        base = fetch_main(checkout, remote)
        ci(checkout, sha, 'affected', base, key, run=run)
        if remote_sha(repo, remote, ref) != announced or remote_sha(repo, remote, 'refs/heads/main') != base:
            raise GateError('branch or main moved; no status will be attached')
        status(sha, 'affected')


def post_push():
    payload = json.load(sys.stdin)
    repo = Path(git(Path.cwd(), 'rev-parse', '--show-toplevel'))
    key = os.environ.get('AGENT_POST_PUSH_REPOSITORY_ID', '')
    if key != repository_identity(repo):
        raise GateError('post-push repository identity mismatch')
    remote = payload['remote']
    if git(repo, 'remote', 'get-url', '--push', '--all', remote).splitlines() != [payload['url']]:
        raise GateError('publication remote configuration changed')
    for update in payload['updates']:
        if set(update['new']) != {'0'}:
            publish(repo, payload['url'], update['ref'], update['new'], key)


def merge_options(argv):
    args = list(argv)
    if args[:1] == ['--']:
        args.pop(0)
    prefix = []
    while args and args[0] != 'pr':
        item = args.pop(0)
        prefix.append(item)
        if item in ('-R', '--repo') and args:
            prefix.append(args.pop(0))
        elif not item.startswith(('-R', '--repo=')):
            raise GateError('unsupported global options for guarded merge')
    if args[:2] != ['pr', 'merge']:
        raise GateError('expected gh pr merge arguments')
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False, exit_on_error=False)
    parser.add_argument('selector', nargs='?')
    parser.add_argument('-R', '--repo')
    parser.add_argument('-h', '--help', action='store_true')
    for flag in ('auto', 'admin', 'disable-auto', 'squash', 'merge', 'rebase', 'delete-branch'):
        parser.add_argument('--' + flag, action='store_true')
    for short, flag in (('-b', 'body'), ('-F', 'body-file'), ('-t', 'subject'), ('-A', 'author-email')):
        parser.add_argument(short, '--' + flag)
    parser.add_argument('--match-head-commit')
    options, unknown = parser.parse_known_args(prefix + args[2:])
    if unknown:
        raise GateError('unsupported merge options; no merge attempted')
    return options


def guarded_merge(argv):
    original = argv[1:] if argv[:1] == ['--'] else argv
    options = merge_options(original)
    if options.help:
        return subprocess.call([REAL_GH, *original])
    lookup = ['gh', 'pr', 'view'] + ([options.selector] if options.selector else [])
    if options.repo:
        lookup += ['--repo', options.repo]
    lookup += ['--json', 'number,url,headRefOid,headRefName,baseRefName,isCrossRepository,state']
    def view():
        return json.loads(command(lookup))
    pr = view()
    if urlsplit(pr['url']).path.strip('/').split('/pull/')[0] != REPOSITORY:
        return subprocess.call([REAL_GH, *original])
    if options.auto or options.admin:
        raise GateError('--auto and --admin are disabled for the local CI merge gate')
    if pr['isCrossRepository'] or pr['baseRefName'] != 'main' or pr['state'] != 'OPEN':
        raise GateError('local gate requires an open same-repository PR into main')
    sha = pr['headRefOid']
    if options.match_head_commit and options.match_head_commit != sha:
        raise GateError('requested head SHA differs from the current PR')
    repo = Path(git(Path.cwd(), 'rev-parse', '--show-toplevel'))
    origin = git(repo, 'remote', 'get-url', 'origin')
    normalized = origin.removesuffix('.git').replace('git@github.com:', 'https://github.com/').rstrip('/')
    if normalized != 'https://github.com/' + REPOSITORY:
        raise GateError('run guarded AVLA merge from a checkout of the target repository')
    git(repo, 'fetch', '--quiet', '--no-tags', 'origin', f'refs/pull/{pr["number"]}/head')
    if git(repo, 'rev-parse', 'FETCH_HEAD') != sha:
        raise GateError('PR changed while fetching; retry merge')
    key = repository_identity(repo)
    with snapshot(repo, sha, key) as checkout:
        base = fetch_main(checkout, origin)
        result = subprocess.run(['git', '-C', str(checkout), 'merge-base', '--is-ancestor', base, sha],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            raise GateError('PR must include current main; update its base and republish before full CI')
        ci(checkout, sha, 'full', base, key)
        current = view()
        if (current['headRefOid'] != sha or current['baseRefName'] != 'main'
                or current['state'] != 'OPEN' or current['isCrossRepository']
                or remote_sha(repo, origin, 'refs/heads/main') != base):
            raise GateError('PR or main changed during full CI; merge cancelled')
        status(sha, 'full')
        # Only server-side protection can close the final base race. Head is
        # checked atomically by GitHub's --match-head-commit.
        if remote_sha(repo, origin, 'refs/heads/main') != base:
            raise GateError('main changed before merge; retry')
        final = [*original]
        if not options.match_head_commit:
            final += ['--match-head-commit', sha]
        return subprocess.call([REAL_GH, *final])


def guard_api(argv):
    args = argv[1:] if argv[:1] == ['--'] else argv
    endpoints = [unquote(urlsplit(a).path.lstrip('/')).rstrip('/') for a in args
                 if a in ('graphql', '/graphql') or a.startswith(('repos/', '/repos/', 'https://api.github.com/'))]
    method = 'GET'
    for i, arg in enumerate(args):
        if arg in ('-X', '--method') and i + 1 < len(args):
            method = args[i + 1].upper()
        elif arg.startswith('-X') and len(arg) > 2:
            method = arg[2:].upper()
        elif arg.startswith('--method='):
            method = arg.split('=', 1)[1].upper()
    if any(a in ('-f', '-F', '--field', '--raw-field', '--input') or a.startswith(('--field=', '--raw-field=', '--input=')) for a in args) and method == 'GET':
        method = 'POST'
    pattern = r'repos/(?:' + re.escape(REPOSITORY) + r'|\{owner\}/\{repo\})/pulls/\d+/merge'
    if any(re.fullmatch(pattern, endpoint) for endpoint in endpoints) and method != 'GET':
        raise GateError('direct merge API disabled; use gh pr merge with local full CI')
    if 'graphql' in endpoints:
        queries = []
        for i, arg in enumerate(args):
            if arg.startswith('query='):
                value = arg[6:]
                queries.append(Path(value[1:]).read_text() if value.startswith('@') else value)
            if arg == '--input' or arg.startswith('--input='):
                path = args[i + 1] if arg == '--input' else arg.split('=', 1)[1]
                if path == '-':
                    raise GateError('GraphQL stdin cannot be inspected; use an explicit query file')
                queries.append(json.loads(Path(path).read_text()).get('query', ''))
        if not queries or any(re.search(r'\bmergePullRequest\b', query) for query in queries):
            raise GateError('direct GraphQL merge disabled; use gh pr merge with local full CI')


def main():
    try:
        action, *args = sys.argv[1:]
        if action == 'verify' and len(args) == 1:
            verify(args[0])
        elif action == 'post-push' and not args:
            post_push()
        elif action == 'publish' and len(args) == 3:
            remote, ref, sha = args
            repo = Path(git(Path.cwd(), 'rev-parse', '--show-toplevel'))
            urls = git(repo, 'remote', 'get-url', '--push', '--all', remote).splitlines()
            if len(urls) != 1:
                raise GateError('publication requires exactly one push endpoint')
            publish(repo, urls[0], ref, sha, repository_identity(repo), run=True)
        elif action == 'merge':
            return guarded_merge(args)
        elif action == 'guard-api':
            guard_api(args)
        else:
            raise GateError('usage: agent-ci-ingestion verify SHA | post-push | merge -- gh-args | guard-api -- gh-args')
        return 0
    except (GateError, OSError, ValueError, KeyError, TypeError, argparse.ArgumentError) as error:
        print('Local CI gate: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
