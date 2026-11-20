import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('verify_markdown', ROOT / 'lib/agent_verify_markdown.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class MarkdownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        self.write('README.md', '# Reference\n\n[Detail](notes/detail.md)\n')
        self.write('notes/detail.md', '# Detail\n')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True).strip()

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def run_verifier(self, sha, profile='reference'):
        return subprocess.run([str(ROOT / 'bin/agent-verify-markdown'), '--profile', profile, sha],
                              cwd=self.repo, capture_output=True, text=True)

    def test_reference_complete_tree_and_exact_commit(self):
        old = self.commit()
        self.assertEqual(self.run_verifier(old).returncode, 0)
        self.write('notes/detail.md', '# Detail\n\n[Broken](missing.md)\n')
        self.commit()
        # Even a later README-only commit must not hide the older broken link.
        self.write('README.md', '# Reference\n')
        sha = self.commit()
        result = self.run_verifier(sha)
        self.assertEqual(result.returncode, 1)
        self.assertIn('missing tracked link target', result.stderr)
        self.assertEqual(self.run_verifier(old).returncode, 1)

    def test_dirty_checkout_rejected(self):
        sha = self.commit()
        self.write('untracked.md', '# Pending\n')
        self.assertIn('clean checkout', self.run_verifier(sha).stderr)

    def test_portable_links_code_examples_and_references(self):
        self.write('notes/with space.md', '# Spaced\n')
        self.write('README.md', '# Reference\n\n[Space](notes/with%20space.md#section)\n'
                   '[Detail][ref]\n\n[ref]: notes/detail.md "Detail"\n'
                   '[Web](https://example.invalid/absent)\n'
                   '```md\n[Example](does-not-exist.md)\n```\n')
        sha = self.commit()
        self.assertEqual(self.run_verifier(sha).returncode, 0)
        self.write('README.md', '# Reference\n\n[x]: absent.md\n')
        self.assertIn('missing tracked link target', self.run_verifier(self.commit()).stderr)

    def test_conflict_marker_and_escape_block(self):
        for content, expected in [('# Title\n<<<<<<< HEAD\n', 'merge marker'),
                                  ('# Title\n[x](../../outside.md)\n', 'escapes repository'),
                                  ('# Title\n[x](file:///tmp/x)\n', 'file URL')]:
            with self.subTest(expected=expected):
                self.write('README.md', content)
                result = self.run_verifier(self.commit())
                self.assertEqual(result.returncode, 1)
                self.assertIn(expected, result.stderr)

    def catalog(self):
        self.write('TAXONOMY.md', '# Categories\n\n| `dev-tools` | Development |\n')
        self.write('_plantilla-ficha.md', '# Card template\n')
        card = '# Tool\n\n' + '\n'.join('- **' + f + ':** ' + ('radar' if f == 'Estado' else 'value')
            for f in ('Qué es', 'Para qué sirve', 'Repo', 'Sitio / docs', 'Licencia',
                      'Lenguaje / stack', 'Estado', 'Fuente', 'Fork', 'Etiquetas')) + '\n'
        self.write('tools/dev-tools/tool.md', card)
        self.write('INDEX.md', '# Index\n\n| Tool | dev-tools | radar | [Card](tools/dev-tools/tool.md) |\n')

    def test_catalog_index_state_category_and_fields(self):
        self.catalog()
        self.assertEqual(self.run_verifier(self.commit(), 'radar').returncode, 0)
        self.write('INDEX.md', '# Index\n\n| Tool | dev-tools | adoptada | [Card](tools/dev-tools/tool.md) |\n')
        self.assertIn('category/state disagrees', self.run_verifier(self.commit(), 'radar').stderr)
        self.write('INDEX.md', '# Index\n')
        self.assertIn('exactly one', self.run_verifier(self.commit(), 'radar').stderr)
        self.write('tools/dev-tools/tool.md', '# Tool\n\n- **Estado:** invalid\n')
        result = self.run_verifier(self.commit(), 'radar')
        self.assertIn('missing card field', result.stderr)
        self.assertIn('invalid catalog state', result.stderr)

    def test_deleted_target_rejected(self):
        self.commit()
        (self.repo / 'notes/detail.md').unlink()
        self.git('add', '-u')
        self.git('commit', '-qm', 'delete target')
        self.assertIn('missing tracked link target', self.run_verifier('HEAD').stderr)


if __name__ == '__main__':
    unittest.main()
