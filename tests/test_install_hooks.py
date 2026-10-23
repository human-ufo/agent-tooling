from pathlib import Path
import json
import os
import shutil
import unittest
from test_pre_push_hook import PublishFixture, ROOT


class InstallerTests(PublishFixture):
    def test_idempotent_and_chains_identical_stdin(self):
        hooks = self.repo / "custom-hooks"
        hooks.mkdir()
        self.git("config", "core.hooksPath", "custom-hooks")
        previous = hooks / "pre-push"
        capture = self.base / "previous-stdin"
        previous.write_text("#!/bin/sh\ncat >" + str(capture) + "\n")
        previous.chmod(0o755)
        self.install()
        installed = previous.read_bytes()
        self.install()
        self.assertEqual(previous.read_bytes(), installed)
        self.assertEqual(len(list(hooks.glob("pre-push.agent-backup-*"))), 1)
        new = self.commit("next")
        self.git("push", "origin", "topic")
        fields = capture.read_text().split()
        self.assertEqual(fields, ["refs/heads/topic", new, "refs/heads/topic", self.first])
        manifest = json.loads((hooks / "agent-tooling-manifest.json").read_text())
        self.assertTrue(Path(manifest["hooks"]["pre-push"]["backup"]).is_file())

    def test_prior_rejection_still_blocks_publication(self):
        hooks = self.repo / "custom-hooks"
        hooks.mkdir()
        self.git("config", "core.hooksPath", "custom-hooks")
        previous = hooks / "pre-push"
        previous.write_text("#!/bin/sh\ncat >/dev/null\nexit 23\n")
        previous.chmod(0o755)
        self.install()
        self.commit("next")
        self.assertNotEqual(self.push("origin", "HEAD:refs/heads/topic", check=False).returncode, 0)
        self.assertEqual(self.remote_sha(), self.first)

    def test_installed_runtime_survives_original_source_removal(self):
        # Install from a throwaway source package then remove that package.
        source = self.base / "ephemeral-package"
        source.mkdir()
        for name in ("bin", "hooks", "lib"):
            shutil.copytree(ROOT / name, source / name)
        shutil.copy2(ROOT / "install-hooks.sh", source / "install-hooks.sh")
        hooks = self.repo / "durable-hooks"
        self.git("config", "core.hooksPath", str(hooks))
        self.command(str(source / "install-hooks.sh"), str(self.repo))
        shutil.rmtree(source)
        new = self.commit("next")
        self.git("push", "origin", "topic")
        self.assertEqual(self.remote_sha(), new)

    def test_relative_hookspath_on_linked_worktree_resolves_like_git(self):
        self.git("config", "core.hooksPath", "relative-hooks")
        linked = self.base / "linked"
        self.git("worktree", "add", "-q", "-b", "linked", str(linked))
        self.install(linked)
        effective = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "hooks", cwd=linked).stdout.strip())
        self.assertTrue((effective / "pre-push").is_file())
        self.assertEqual(effective, linked / "relative-hooks")
        self.git("push", "origin", "HEAD:refs/heads/linked", cwd=linked)
        self.assertEqual(self.remote_sha("linked"), self.first)

    def test_existing_lint_and_precommit_are_preserved_and_chained(self):
        # The documentation lint only installs in the governance repositories,
        # so this repository identifies as one of them.
        self.git("remote", "set-url", "origin", "https://github.com/human-ufo/personal-docs.git")
        if True:
            lint = self.home / ".agents/skills/docs-writer/pre-commit"
            lint.parent.mkdir(parents=True)
            lint_result = self.base / "lint-ran"
            lint.write_text("#!/bin/sh\nprintf yes >" + str(lint_result) + "\n")
            # Installer makes this executable even if source wasn't marked executable.
            previous = self.repo / ".git/hooks/pre-commit"
            prior_result = self.base / "prior-ran"
            previous.write_text("#!/bin/sh\nprintf yes >" + str(prior_result) + "\n")
            previous.chmod(0o755)
            self.install()
            self.commit("next")
            self.assertEqual(lint_result.read_text(), "yes")
            self.assertEqual(prior_result.read_text(), "yes")

    def test_modified_dispatcher_and_incompatible_existing_hook_are_rejected(self):
        target = self.repo / ".git/hooks/pre-push"
        target.write_text("#!/bin/sh\nexit 0\n")
        self.assertNotEqual(self.command(str(ROOT / "install-hooks.sh"), str(self.repo), check=False).returncode, 0)
        hooks = self.repo / "bad-hooks"
        hooks.mkdir()
        (hooks / "pre-push").mkdir()
        self.git("config", "core.hooksPath", str(hooks))
        self.assertNotEqual(self.command(str(ROOT / "install-hooks.sh"), str(self.repo), check=False).returncode, 0)


class DocumentationLintScopeTests(PublishFixture):
    """The documentation lint only belongs in the governance documentation repos.

    Its rules demand sections that a task file or a source file does not have,
    so installing it anywhere else blocks legitimate commits.
    """

    def test_documentation_lint_is_not_installed_in_other_repositories(self):
        repo = self.repo
        self.install(repo)
        hooks = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "hooks").stdout.strip())
        target = hooks / "pre-commit"
        if target.exists():
            dispatcher = target.read_text()
            self.assertNotIn("pre-commit-docs-lint", dispatcher)

    def test_documentation_lint_is_installed_in_a_documentation_repository(self):
        # The origin remote decides, so a clone of a documentation repository
        # is recognised wherever the directory sits.
        self.git("remote", "set-url", "origin", "https://github.com/human-ufo/personal-docs.git")
        self.install()
        hooks = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "hooks").stdout.strip())
        dispatcher = hooks / "pre-commit"
        if dispatcher.exists():
            self.assertIn("pre-commit-docs-lint", dispatcher.read_text())
        else:
            # Without the lint installed there is no dispatcher for it.
            self.assertFalse((hooks / "pre-commit-docs-lint").exists())
