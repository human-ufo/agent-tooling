#!/usr/bin/env python3
"""Verificador de publicación para repos de gobernanza documental.

`agent.pushVerify` ejecuta este script en un checkout descartable del SHA
anunciado y le pasa ese SHA como argumento final. El verificador:

  1. resuelve el objeto anunciado y comprueba que el checkout corresponde a ese
     commit;
  2. lista los `.md` agregados y modificados **en ese commit**, con
     `git diff-tree`, sin usar `origin/main`;
  3. corre el linter de documentación sobre esos archivos con la misma regla de
     severidad que el `pre-commit`: un documento nuevo tiene que pasar todas las
     reglas; un documento previo modificado solo bloquea por errores críticos
     (mojibake, posible secreto, links `file://`) y el resto se informa como
     aviso.

Por qué no se usa `origin/main`: en el checkout descartable el `origin` es el
worktree local y no GitHub, así que `origin/main` puede no existir (y cuando
existe no describe el commit anunciado). El conjunto de archivos sale del
propio commit, con lo que el verificador juzga exactamente lo que se publica.

El linter no se modifica: vive fuera del repositorio verificado y se carga por
ruta absoluta. El perfil `tasks` cubre `agent-tasks`, cuyo linter vive dentro
del repositorio y no distingue errores críticos.
"""
import argparse
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys

# Linter de documentación por defecto: el skill `documentar` del usuario, que
# es el único punto de entrada de la documentación. La ruta vieja se conserva
# como último recurso para que una instalación que todavía no se actualizó no
# se quede sin linter; el orden es: preferencia, respaldo.
DOCS_LINT_RELATIVES = (
    ".agents/skills/documentar/lint_docs.py",
    ".agents/skills/docs-writer/lint_docs.py",
)
DOCS_LINT_RELATIVE = DOCS_LINT_RELATIVES[0]
# Linter de tareas por defecto: vive dentro del repositorio verificado.
TASKS_LINT_RELATIVE = "scripts/lint_tasks.py"

# Variables de entorno que el hook no filtra: el hook solo deja pasar los
# nombres declarados en `agent.pushVerifyEnv`, y rechaza los prefijos `GIT_`
# y `AGENT_`. Por eso estas variables no usan ninguno de los dos prefijos.
DOCS_LINT_ENV = ("DOCS_LINT",)
TASKS_LINT_ENV = ("TASKS_LINT",)
PROFILE_ENV = "DOCS_LINT_PROFILE"


class VerificationError(Exception):
    """Fallo del verificador con un mensaje accionable para el operador."""


def run_git(*args, cwd=None):
    """Ejecuta un comando de Git y devuelve su salida estándar."""
    p = subprocess.run(["git", *args], cwd=cwd, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        detail = (p.stderr or p.stdout).strip().splitlines()
        raise VerificationError("git " + " ".join(args) + " falló: "
                                + (detail[-1] if detail else "sin detalle"))
    return p.stdout


def load_linter(path, hint):
    """Carga el linter desde una ruta absoluta y devuelve el módulo."""
    path = Path(path)
    if not path.exists():
        raise VerificationError(
            "no se encontró el linter en " + str(path) + ". " + hint)
    if not path.is_file():
        raise VerificationError("el linter no es un archivo regular: " + str(path))
    if not os.access(path, os.R_OK):
        raise VerificationError("el linter no se puede leer: " + str(path))
    try:
        loader = importlib.machinery.SourceFileLoader("agent_verify_docs_linter", str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
    except Exception as e:  # noqa: BLE001 - importa el motivo, no el tipo
        raise VerificationError("no se pudo cargar el linter " + str(path) + ": " + str(e)) from e
    return module


class DocsProfile:
    """Perfil de los repos de gobernanza documental (personal-docs, avla-docs-reporteria).

    Reproduce la regla de severidad del `pre-commit` de docs-writer: un
    documento nuevo tiene que pasar todas las reglas, y un documento previo
    modificado solo bloquea por los errores críticos.
    """

    name = "docs"

    def __init__(self, module, source):
        for attribute in ("lint", "is_critical"):
            if not callable(getattr(module, attribute, None)):
                raise VerificationError(
                    "el linter " + str(source) + " no expone " + attribute
                    + "(): no es un linter de documentación compatible")
        self.module = module
        self.source = source

    def skip(self, name):
        should_skip = getattr(self.module, "should_skip", None)
        return bool(should_skip(name)) if callable(should_skip) else False

    def check(self, name, seen_ids):
        return self.module.lint(name)

    def critical(self, message):
        return bool(self.module.is_critical(message))

    def seen_ids_outside(self, repo, changed):
        """El linter de documentación no lleva IDs: no hay nada que recoger."""
        return set()


class TasksProfile:
    """Perfil de `agent-tasks`.

    Su linter vive dentro del repositorio verificado y no distingue errores
    críticos de los demás: cualquier error bloquea, en documentos nuevos y en
    documentos previos modificados. Es la misma regla que su `pre-commit`.
    """

    name = "tasks"

    def __init__(self, module, source):
        if not callable(getattr(module, "lint", None)):
            raise VerificationError(
                "el linter " + str(source) + " no expone lint(): no es un linter de tareas compatible")
        self.module = module
        self.source = source

    def skip(self, name):
        is_task_file = getattr(self.module, "is_task_file", None)
        return not bool(is_task_file(name)) if callable(is_task_file) else False

    def check(self, name, seen_ids):
        return self.module.lint(name, seen_ids)

    def critical(self, message):
        return True

    def seen_ids_outside(self, repo, changed):
        """IDs que ya usan las tareas que este commit no modifica.

        Así una tarea nueva no puede repetir el ID de una tarea existente, que
        es una regla del linter de tareas y depende del repo completo.
        """
        seen = set()
        iter_task_files = getattr(self.module, "iter_task_files", None)
        parse_fields = getattr(self.module, "parse_fields", None)
        if not callable(iter_task_files) or not callable(parse_fields):
            return seen
        touched = set(changed)
        for path in iter_task_files():
            try:
                relative = os.path.relpath(str(path), str(repo))
            except ValueError:
                relative = str(path)
            if relative in touched:
                continue
            fields, _ = parse_fields(path)
            task_id = (fields.get("ID") or "").strip()
            if task_id:
                seen.add(task_id)
        return seen


def linter_source(args, repo):
    """Resuelve la ruta del linter: --lint, variable de entorno o valor por defecto.

    Devuelve la ruta elegida y el mensaje con la acción a seguir si no está.
    """
    if args.lint:
        return args.lint, ""
    if args.profile == "tasks":
        names = TASKS_LINT_ENV
        default = str(repo / TASKS_LINT_RELATIVE)
        hint = ("El linter de tareas vive en el repositorio verificado, en "
                + TASKS_LINT_RELATIVE + ". Configurá la ruta con --lint o con la "
                "variable de entorno TASKS_LINT.")
    else:
        names = DOCS_LINT_ENV
        home = os.environ.get("HOME") or str(Path.home())
        default = str(Path(home) / DOCS_LINT_RELATIVE)
        hint = ("Instalá el skill documentar o configurá la ruta con --lint o con "
                "la variable de entorno DOCS_LINT.")
    for name in names:
        value = os.environ.get(name)
        if value:
            return value, hint
    if args.profile != "tasks":
        # Perfil de documentación: sin ruta explícita se prefiere la del skill
        # actual y, si no está, la del skill retirado. Un rename del skill no
        # debe dejar al push sin linter.
        home = os.environ.get("HOME") or str(Path.home())
        for relative in DOCS_LINT_RELATIVES:
            candidate = Path(home) / relative
            if candidate.is_file():
                return str(candidate), hint
    return default, hint


def resolve_profile(args, repo):
    """Resuelve el perfil y carga su linter."""
    source, hint = linter_source(args, repo)
    module = load_linter(source, hint)
    if args.profile == "tasks":
        return TasksProfile(module, source)
    return DocsProfile(module, source)


def resolve_commit(repo, sha):
    """Devuelve el commit anunciado y comprueba que el checkout corresponde."""
    p = subprocess.run(["git", "rev-parse", "--verify", "--quiet", sha + "^{commit}"],
                       cwd=str(repo), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode or not p.stdout.strip():
        raise VerificationError(
            "el objeto anunciado no es un commit (ni un tag anotado) en el checkout: " + sha)
    commit = p.stdout.strip()
    head = run_git("rev-parse", "HEAD", cwd=str(repo)).strip()
    if head != commit:
        raise VerificationError(
            "el checkout no corresponde al commit anunciado: HEAD=" + head + " commit=" + commit)
    return commit


def changed_markdown(repo, commit):
    """Nombres de los `.md` agregados y modificados en ese commit.

    `git diff-tree` contra el primer progenitor evita depender de `origin/main`,
    que no existe en el checkout descartable. `--root` cubre el commit inicial
    y `--first-parent` las fusiones, donde el diff simple no lista nada.

    `--no-renames` fija la detección de renombres en apagada, así el resultado
    no depende de la configuración `diff.renames` del usuario. Un archivo
    renombrado se trata entonces como documento nuevo y tiene que pasar todas
    las reglas, que es más estricto que el `pre-commit` (allí un renombre no
    llega al linter) pero más coherente con la regla de nombres.
    """
    out = run_git("diff-tree", "--root", "-m", "--first-parent", "--no-commit-id",
                  "--name-status", "-r", "-z", "--no-renames", commit, cwd=str(repo))
    fields = out.split("\0")
    changed, added = [], set()
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if not status:
            continue
        # Con --no-renames el estado es de una sola letra y le sigue una ruta.
        if index >= len(fields):
            break
        name = fields[index]
        index += 1
        if status[0] not in ("A", "M") or not name.endswith(".md"):
            continue
        changed.append(name)
        if status[0] == "A":
            added.add(name)
    return changed, added


CONFLICT_MARKER_RE = re.compile(r"^(?:<{7}(?: .*)?|={7}|>{7}(?: .*)?)$")


def conflict_marker_errors(path):
    """Marcadores de conflicto de Git sin resolver en un documento.

    Una fusión por squash que resuelve el conflicto en el editor puede dejar
    las lineas `<<<<<<<`, `=======` y `>>>>>>>` dentro del texto publicado.
    El linter de documentación no las ve, y los indices que el linter excluye
    (`README.md`, `AI_KNOWLEDGE_MAP.md`, `AGENDA.md`) son los que mas se tocan
    en paralelo, asi que la guarda va antes de cualquier exclusion.
    """
    errors = []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return errors
    for number, line in enumerate(text.splitlines(), 1):
        if CONFLICT_MARKER_RE.match(line):
            errors.append("línea %d: marcador de conflicto de Git sin resolver (%s)"
                          % (number, line[:7]))
    return errors


def review(profile, repo, changed, added):
    """Revisa los documentos y devuelve el número de errores bloqueantes."""
    seen_ids = profile.seen_ids_outside(repo, changed)
    failures = 0
    for name in changed:
        path = repo / name
        if not path.is_file():
            print("SKIP  " + name + " (no está en el checkout)")
            continue
        marker_errors = conflict_marker_errors(path)
        if profile.skip(name):
            # Un indice que el linter excluye igual se publica: si trae
            # marcadores de conflicto, el error bloquea igual.
            if marker_errors:
                print("FAIL  " + name + " (marcadores de conflicto)")
                for message in marker_errors:
                    print("  error: " + message)
                failures += len(marker_errors)
            else:
                print("SKIP  " + name + " (excluido por el linter)")
            continue
        errors, warnings = profile.check(name, seen_ids)
        marker_set = set(marker_errors)
        errors = list(marker_errors) + [error for error in (errors or []) if error not in marker_set]
        warnings = list(warnings or [])
        is_new = name in added
        if is_new:
            blocking, notices = errors, warnings
        else:
            blocking = list(marker_errors) + [error for error in errors
                                             if error not in marker_set and profile.critical(error)]
            notices = [error for error in errors if error not in blocking] + warnings
        if not errors and not warnings:
            print("OK    " + name)
            continue
        print(("FAIL  " if blocking else "WARN  ") + name
              + (" (documento nuevo)" if is_new else " (documento previo modificado)"))
        for message in blocking:
            print("  error: " + message)
        for message in notices:
            print("  aviso: " + message)
        failures += len(blocking)
    print("agent-verify-docs: %d documento(s) revisado(s), %d error(es) bloqueante(s)."
          % (len(changed), failures))
    return failures


def main(argv):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sha", nargs="+",
                        help="objeto anunciado; el hook pasa el SHA como último argumento")
    parser.add_argument("--profile", choices=("docs", "tasks"),
                        default=os.environ.get(PROFILE_ENV, "docs"),
                        help="perfil del linter: docs (por defecto) o tasks")
    parser.add_argument("--lint", default=None, help="ruta absoluta del linter")
    args = parser.parse_args(argv)
    sha = args.sha[-1]
    try:
        repo = Path(run_git("rev-parse", "--show-toplevel").strip()).resolve()
        commit = resolve_commit(repo, sha)
        changed, added = changed_markdown(repo, commit)
        if not changed:
            print("agent-verify-docs: el commit " + commit[:12]
                  + " no agrega ni modifica documentos .md; nada que verificar.")
            return 0
        profile = resolve_profile(args, repo)
        os.chdir(str(repo))
        failures = review(profile, repo, changed, added)
        return 1 if failures else 0
    except VerificationError as e:
        print("agent-verify-docs: " + str(e), file=sys.stderr)
        return 2
    except OSError as e:
        print("agent-verify-docs: no se pudo revisar el commit: " + str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
