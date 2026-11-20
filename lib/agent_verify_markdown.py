#!/usr/bin/env python3
"""Offline structural checks for Markdown reference and tool catalog repositories."""
import argparse
from collections import Counter
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit


def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()


def prose(text):
    # Keep line structure for diagnostics; examples in fences are not real links.
    return re.sub(r'^([ \t]*)(`{3,}|~{3,})[^\n]*\n.*?^\1\2[^\n]*$',
                  '', text, flags=re.M | re.S)


def destinations(text):
    # Inline links/images (including URL titles) and reference definitions.
    pattern = r'!?\[[^\]\n]*\]\(\s*(<[^>\n]*>|[^\s)]+)(?:\s+[^)\n]*)?\)'
    for match in re.finditer(pattern, text):
        yield match.group(1).strip('<>')
    for match in re.finditer(r'^\s{0,3}\[[^\]\n]+\]:\s*(<[^>\n]*>|\S+)', text, re.M):
        yield match.group(1).strip('<>')


def validate(repo, profile, files):
    errors = []
    texts = {}
    tracked = set(files)
    for name in files:
        if not name.endswith('.md'):
            continue
        path = repo / name
        if path.is_symlink():
            errors.append(f'{name}: Markdown symlinks are unsupported')
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            errors.append(f'{name}: unreadable UTF-8 document')
            continue
        texts[name] = text
        if not text.strip():
            errors.append(f'{name}: empty document')
        if re.search(r'^(?:<{7}|={7}|>{7})(?:\s|$)', text, re.M):
            errors.append(f'{name}: unresolved merge marker')
        if not re.search(r'^#\s+\S', prose(text), re.M):
            errors.append(f'{name}: missing document title')
        for destination in destinations(prose(text)):
            url = urlsplit(destination)
            if url.scheme or url.netloc:
                if url.scheme == 'file':
                    errors.append(f'{name}: nonportable file URL')
                continue
            target = unquote(url.path)
            if not target:
                continue  # Fragment existence is outside this structural check.
            resolved = ((repo if target.startswith('/') else path.parent) / target.lstrip('/')).resolve()
            try:
                relative = resolved.relative_to(repo).as_posix()
            except ValueError:
                errors.append(f'{name}: link escapes repository')
                continue
            if relative not in tracked and not any(p.startswith(relative.rstrip('/') + '/') for p in tracked):
                errors.append(f'{name}: missing tracked link target {target}')
    if 'README.md' not in texts:
        errors.append('README.md: required reference index missing')
    if profile == 'radar':
        validate_radar(texts, errors)
    return errors


def validate_radar(texts, errors):
    for required in ('INDEX.md', 'TAXONOMY.md', '_plantilla-ficha.md'):
        if required not in texts:
            errors.append(f'{required}: required catalog document missing')
    categories = set(re.findall(r'^\|\s*`([^`]+)`\s*\|', texts.get('TAXONOMY.md', ''), re.M))
    fields = ('Qué es', 'Para qué sirve', 'Repo', 'Sitio / docs', 'Licencia',
              'Lenguaje / stack', 'Estado', 'Fuente', 'Fork', 'Etiquetas')
    states = {'radar', 'evaluar', 'probada', 'adoptada', 'descartada'}
    cards = {name: text for name, text in texts.items() if name.startswith('tools/')}
    entries = Counter()
    for row in texts.get('INDEX.md', '').splitlines():
        if not row.startswith('|'):
            continue
        columns = [c.strip() for c in row.strip('|').split('|')]
        links = list(destinations(row))
        for target in links:
            if not target.startswith('tools/'):
                continue
            entries[target] += 1
            if target in cards and len(columns) == 4:
                category = target.split('/')[1] if len(target.split('/')) == 3 else ''
                state = re.search(r'^- \*\*Estado:\*\*\s*(\S+)', cards[target], re.M)
                if columns[1] != category or not state or columns[2] != state.group(1):
                    errors.append(f'INDEX.md: category/state disagrees with {target}')
            elif target in cards:
                errors.append(f'INDEX.md: malformed row for {target}')
    for name, text in cards.items():
        parts = name.split('/')
        if len(parts) != 3 or parts[1] not in categories:
            errors.append(f'{name}: unknown category or invalid card location')
        for field in fields:
            if not re.search(r'^- \*\*' + re.escape(field) + r':\*\*', text, re.M):
                errors.append(f'{name}: missing card field {field}')
        state = re.search(r'^- \*\*Estado:\*\*\s*(\S+)', text, re.M)
        if not state or state.group(1) not in states:
            errors.append(f'{name}: invalid catalog state')
        if entries[name] != 1:
            errors.append(f'{name}: requires exactly one INDEX.md entry')
    if not cards:
        errors.append('tools/: catalog contains no cards')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=('reference', 'radar'), required=True)
    parser.add_argument('sha')
    args = parser.parse_args()
    try:
        repo = Path(git('rev-parse', '--show-toplevel')).resolve()
        if git('rev-parse', 'HEAD') != git('rev-parse', args.sha + '^{commit}'):
            parser.exit(1, 'Checkout does not match announced SHA\n')
        if git('status', '--porcelain', '--untracked-files=all'):
            parser.exit(1, 'Verification requires a clean checkout\n')
        files = subprocess.check_output(['git', 'ls-files', '-z']).decode().rstrip('\0').split('\0')
        errors = validate(repo, args.profile, files)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, 'Cannot validate Markdown tree: ' + str(exc) + '\n')
    if errors:
        parser.exit(1, '\n'.join(errors) + '\n')
    print(f'Markdown {args.profile}: complete tracked tree passed')


if __name__ == '__main__':
    main()
