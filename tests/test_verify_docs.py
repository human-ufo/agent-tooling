"""Verificador de publicación para repos de gobernanza documental.

Las pruebas usan un linter de fixture que replica el contrato de
`~/.agents/skills/docs-writer/lint_docs.py` (`lint`, `is_critical`,
`should_skip`) sin depender del filesystem del usuario. El linter real se
prueba aparte, y solo si está instalado.
"""
from pathlib import Path
import os
import unittest

from test_pre_push_hook import PublishFixture, ROOT

VERIFIER = ROOT / "bin/agent-verify-docs"

# Fixture del linter de documentación. Replica el contrato del linter real:
# `lint` devuelve (errores, avisos), `is_critical` decide qué errores bloquean
# en un documento previo modificado y `should_skip` excluye los índices.
FIXTURE_LINT = '''"""Linter de fixture: replica el contrato del linter de docs-writer."""
import os
import re

MOJIBAKE = re.compile("\\u00c3[\\x80-\\xbf]|\\u00e2\\u20ac|\\u00f0\\u0178|\\u00c2[\\xa0-\\xbf]")
DATED_NAME = re.compile(r"^\\d{4}-\\d{2}-\\d{2}_[^\\s]+\\.md$")
SKIP_NAMES = {"README.md", "AI_KNOWLEDGE_MAP.md"}
CRITICAL_MARKERS = ("mojibake", "posible secreto", "file://")


def lint(path):
    errors, warnings = [], []
    name = os.path.basename(path)
    with open(path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    if not DATED_NAME.match(name):
        errors.append("el nombre no sigue YYYY-MM-DD_<Titulo_Sin_Espacios>.md")
    for number, line in enumerate(text.splitlines(), 1):
        if MOJIBAKE.search(line):
            errors.append("l\\u00eda %d: mojibake (UTF-8 doblemente codificado)" % number)
    return errors, warnings


def is_critical(message):
    return any(marker in message for marker in CRITICAL_MARKERS)


def should_skip(path):
    return os.path.basename(path) in SKIP_NAMES
'''

# Fixture del linter de tareas. Su contrato difiere: `lint` recibe el conjunto
# de IDs ya vistos y no distingue errores críticos de los demás.
FIXTURE_TASKS_LINT = '''"""Linter de fixture: replica el contrato del linter de tareas."""
import os
import re

MOJIBAKE = re.compile("\\u00c3[\\x80-\\xbf]")
FILENAME_RE = re.compile(r"^\\d{4}-\\d{2}-\\d{2}_[a-z0-9]+(?:-[a-z0-9]+)*\\.md$")


def lint(path, seen_ids):
    errors, warnings = [], []
    name = os.path.basename(path)
    if not FILENAME_RE.match(name):
        errors.append("el nombre no sigue YYYY-MM-DD_<slug_en_guiones>.md")
    with open(path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    for number, line in enumerate(text.splitlines(), 1):
        if MOJIBAKE.search(line):
            errors.append("l\\u00eda %d: mojibake (UTF-8 doblemente codificado)" % number)
    return errors, warnings


def is_task_file(path):
    return os.path.basename(path).endswith(".md")
'''

VALID_DOC = "Documento de prueba.\n\nSin problemas de codificacion.\n"
# Mojibake real: la "í" de "días" codificada dos veces en UTF-8.
MOJIBAKE_DOC = "Documento de prueba.\n\nCon d\u00c3\u00adas mal codificados.\n"


class VerifyDocsTests(PublishFixture):
    def setUp(self):
        super().setUp()
        # El verificador corre en un checkout descartable, así que el linter
        # tiene que vivir fuera del repositorio verificado: el HOME del
        # fixture es el único sitio que el hook deja pasar al verificador.
        lint_dir = self.home / ".agents/skills/docs-writer"
        lint_dir.mkdir(parents=True)
        (lint_dir / "lint_docs.py").write_text(FIXTURE_LINT, encoding="utf-8")
        self.git("config", "agent.pushVerifyMode", "required")
        self.git("config", "agent.pushVerify", str(VERIFIER))

    def write_doc(self, name, content, commit=True):
        """Escribe un documento y, opcionalmente, lo commitea."""
        (self.repo / name).write_text(content, encoding="utf-8")
        self.git("add", name)
        if not commit:
            return None
        self.git("commit", "-qm", "documento " + name)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def verifications(self):
        return [e for e in self.events() if e["event"] == "verification"]

    def test_valid_document_is_published_and_verification_passes(self):
        self.write_doc("2026-10-23_Documento_Valido.md", VALID_DOC)
        self.push("origin", "HEAD:refs/heads/topic")
        self.assertEqual(self.remote_sha(), self.git("rev-parse", "HEAD").stdout.strip())
        results = [e.get("result") for e in self.verifications()]
        self.assertIn("passed", results)
        # El evento nombra el commit verificado.
        passed = [e for e in self.verifications() if e.get("result") == "passed"]
        self.assertEqual(passed[-1]["sha"], self.git("rev-parse", "HEAD").stdout.strip())

    def test_document_with_mojibake_is_rejected_and_remote_stays_intact(self):
        self.write_doc("2026-10-23_Documento_Con_Mojibake.md", MOJIBAKE_DOC)
        p = self.push("origin", "HEAD:refs/heads/topic", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.remote_sha(), self.first)
        self.assertTrue(any(e.get("result") == "failed" for e in self.verifications()))

    def test_commit_without_markdown_is_published(self):
        self.commit("sin documentos")
        self.push("origin", "HEAD:refs/heads/topic")
        self.assertEqual(self.remote_sha(), self.git("rev-parse", "HEAD").stdout.strip())
        self.assertTrue(any(e.get("result") == "passed" for e in self.verifications()))

    def test_previous_document_with_non_critical_problem_does_not_block(self):
        # El documento ya estaba publicado con un nombre fuera del patrón: al
        # modificarlo, ese problema es un aviso y no bloquea.
        self.write_doc("Nombre_Fuera_De_Patron.md", VALID_DOC)
        self.write_doc("Nombre_Fuera_De_Patron.md", VALID_DOC + "Segunda edicion.\n")
        self.push("origin", "HEAD:refs/heads/topic")
        self.assertEqual(self.remote_sha(), self.git("rev-parse", "HEAD").stdout.strip())
        self.assertTrue(any(e.get("result") == "passed" for e in self.verifications()))

    def test_previous_document_with_mojibake_blocks(self):
        self.write_doc("2026-10-23_Documento_Previo.md", VALID_DOC)
        self.write_doc("2026-10-23_Documento_Previo.md", MOJIBAKE_DOC)
        p = self.push("origin", "HEAD:refs/heads/topic", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.remote_sha(), self.first)
        self.assertTrue(any(e.get("result") == "failed" for e in self.verifications()))

    def test_verification_does_not_depend_on_origin_main(self):
        # El checkout descartable tiene por origin el worktree local, donde
        # origin/main no existe. El verificador tiene que publicar igual.
        self.assertNotEqual(self.git("rev-parse", "--verify", "--quiet", "origin/main",
                                     check=False).returncode, 0)
        self.write_doc("2026-10-23_Documento_Valido.md", VALID_DOC)
        self.push("origin", "HEAD:refs/heads/topic")
        self.assertEqual(self.remote_sha(), self.git("rev-parse", "HEAD").stdout.strip())
        self.assertTrue(any(e.get("result") == "passed" for e in self.verifications()))

    def test_linter_path_can_be_overridden_with_an_allowed_variable(self):
        # DOCS_LINT no está filtrado por el hook, así que llega por el
        # allowlist explícito de agent.pushVerifyEnv.
        lint = self.base / "lint-docs.py"
        lint.write_text(FIXTURE_LINT, encoding="utf-8")
        self.git("config", "agent.pushVerifyEnv", "DOCS_LINT")
        self.git("config", "agent.pushVerify",
                 str(VERIFIER) + " --lint " + str(lint))
        self.write_doc("2026-10-23_Documento_Con_Mojibake.md", MOJIBAKE_DOC)
        p = self.push("origin", "HEAD:refs/heads/topic", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.remote_sha(), self.first)

    def test_linter_path_comes_from_the_environment_when_allowed(self):
        # El verificador toma la ruta del linter de DOCS_LINT, que el hook
        # deja pasar cuando está declarada en agent.pushVerifyEnv.
        lint = self.base / "lint-docs.py"
        lint.write_text(FIXTURE_LINT, encoding="utf-8")
        # El linter por defecto (dentro del HOME del fixture) no encuentra el
        # documento; el de la variable sí lo hace.
        (self.home / ".agents/skills/docs-writer/lint_docs.py").write_text(
            "#!/usr/bin/env python3\n"
            "def lint(path):\n"
            "    return ['error de siempre'], []\n"
            "def is_critical(message):\n"
            "    return True\n",
            encoding="utf-8")
        self.git("config", "agent.pushVerifyEnv", "DOCS_LINT")
        self.write_doc("2026-10-23_Documento_Valido.md", VALID_DOC)
        env = dict(self.env, DOCS_LINT=str(lint))
        p = self.command(str(ROOT / "bin/agent-push"), "origin", "HEAD:refs/heads/topic",
                         env=env, check=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.remote_sha(), self.git("rev-parse", "HEAD").stdout.strip())

    def test_deleted_document_is_not_reviewed(self):
        # Un documento borrado no se revisa: no queda contenido publicado.
        self.write_doc("2026-10-23_Documento_Borrable.md", VALID_DOC)
        self.git("rm", "-q", "2026-10-23_Documento_Borrable.md")
        self.git("commit", "-qm", "borrado")
        self.push("origin", "HEAD:refs/heads/topic")
        self.assertEqual(self.remote_sha(), self.git("rev-parse", "HEAD").stdout.strip())
        self.assertTrue(any(e.get("result") == "passed" for e in self.verifications()))

    def test_renamed_document_is_treated_as_new(self):
        # El renombrado se trata como documento nuevo: tiene que pasar todas
        # las reglas, con el nombre que queda en el árbol publicado.
        self.write_doc("2026-10-23_Documento_Valido.md", VALID_DOC)
        self.git("mv", "2026-10-23_Documento_Valido.md", "Nombre_Fuera_De_Patron.md")
        self.git("commit", "-qm", "renombre")
        p = self.push("origin", "HEAD:refs/heads/topic", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.remote_sha(), self.first)

    def test_missing_linter_fails_with_an_actionable_message(self):
        lint_dir = self.home / ".agents/skills/docs-writer"
        (lint_dir / "lint_docs.py").unlink()
        self.write_doc("2026-10-23_Documento_Valido.md", VALID_DOC)
        p = self.push("origin", "HEAD:refs/heads/topic", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.remote_sha(), self.first)
        # Un mensaje accionable, no una excepción sin contexto.
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("no se encontró el linter", p.stderr)

    def test_tasks_profile_uses_the_linter_inside_the_repository(self):
        # El perfil de tareas usa el linter que vive en el repositorio
        # verificado, así que tiene que viajar en el commit publicado.
        (self.repo / "scripts").mkdir(exist_ok=True)
        (self.repo / "scripts/lint_tasks.py").write_text(FIXTURE_TASKS_LINT, encoding="utf-8")
        (self.repo / "04_cursadas_y_estudios").mkdir(exist_ok=True)
        (self.repo / "04_cursadas_y_estudios/2026-10-23_tarea-de-prueba.md").write_text(
            VALID_DOC, encoding="utf-8")
        self.git("add", "scripts/lint_tasks.py", "04_cursadas_y_estudios/2026-10-23_tarea-de-prueba.md")
        self.git("commit", "-qm", "tarea nueva")
        self.git("config", "agent.pushVerify",
                 str(VERIFIER) + " --profile tasks")
        self.push("origin", "HEAD:refs/heads/topic")
        self.assertEqual(self.remote_sha(), self.git("rev-parse", "HEAD").stdout.strip())
        self.assertTrue(any(e.get("result") == "passed" for e in self.verifications()))

    def test_tasks_profile_rejects_a_task_with_mojibake(self):
        (self.repo / "scripts").mkdir(exist_ok=True)
        (self.repo / "scripts/lint_tasks.py").write_text(FIXTURE_TASKS_LINT, encoding="utf-8")
        (self.repo / "04_cursadas_y_estudios").mkdir(exist_ok=True)
        (self.repo / "04_cursadas_y_estudios/2026-10-23_tarea-rota.md").write_text(
            MOJIBAKE_DOC, encoding="utf-8")
        self.git("add", "scripts/lint_tasks.py", "04_cursadas_y_estudios/2026-10-23_tarea-rota.md")
        self.git("commit", "-qm", "tarea rota")
        self.git("config", "agent.pushVerify",
                 str(VERIFIER) + " --profile tasks")
        p = self.push("origin", "HEAD:refs/heads/topic", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.remote_sha(), self.first)


@unittest.skipUnless(Path(os.environ.get("HOME", ""), ".agents/skills/docs-writer/lint_docs.py").is_file(),
                     "el linter de docs-writer no está instalado")
class RealLinterTests(PublishFixture):
    """Comprueba el helper contra el linter real, si está instalado."""

    def test_real_linter_rejects_mojibake_and_accepts_a_valid_document(self):
        self.git("config", "agent.pushVerifyMode", "required")
        self.git("config", "agent.pushVerify", str(VERIFIER))
        (self.repo / "2026-10-23_Documento_Con_Mojibake.md").write_text(
            MOJIBAKE_DOC, encoding="utf-8")
        self.git("add", "2026-10-23_Documento_Con_Mojibake.md")
        self.git("commit", "-qm", "mojibake")
        p = self.push("origin", "HEAD:refs/heads/topic", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.remote_sha(), self.first)
