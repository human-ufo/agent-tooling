# agent-tooling v3

Worktrees por sesión, locks por repositorio/rama y publicación con lease exacto.
Requiere Git, Bash y Python 3 en Linux. No necesita credenciales de producción
para ejecutar las pruebas: todos los remotos de la suite son fixtures locales.

Cada repositorio se identifica por SHA256 completo del `realpath` de su
`git-common-dir` absoluto. Un clon, sus worktrees y sus alias por symlink
comparten identidad y locks. Actor y sesión identifican cooperación local;
no autentican al usuario frente al remoto.

```bash
# Instalación: conserva hooks existentes y copia un runtime durable al hooksPath.
./install-hooks.sh /ruta/al/repo

# La decisión de verificar contenido debe quedar explícita por repositorio.
git -C /ruta/al/repo config agent.pushVerifyMode optional
# Para este repositorio, exigir la suite del commit enviado. La ruta del
# verificador debe ser ABSOLUTA: el verificador corre en un checkout temporal
# descartable, donde una ruta relativa no existe.
git -C /ruta/al/agent-tooling config agent.pushVerifyMode required
git -C /ruta/al/agent-tooling config agent.pushVerify /ruta/al/agent-tooling/bin/agent-verify-selftest
# En un repositorio de gobernanza documental, el verificador es el helper de
# documentación. También con ruta ABSOLUTA.
git -C /ruta/al/personal-docs config agent.pushVerifyMode required
git -C /ruta/al/personal-docs config agent.pushVerify /ruta/al/agent-tooling/bin/agent-verify-docs
# Para cualquier repositorio con una suite de tests en `tests/`, el verificador
# genérico corre esa suite sobre el contenido del commit anunciado. El
# directorio de la suite es el primer argumento; el SHA lo anade el hook.
git -C /ruta/al/repo config agent.pushVerifyMode required
git -C /ruta/al/repo config agent.pushVerify /ruta/al/agent-tooling/bin/agent-verify-tests tests

# Sesión: la base se obtiene del default branch remoto, nunca de HEAD sucio.
agent-worktree open /ruta/al/repo --agent codex --session tarea-123 --branch agent/tarea-123
agent-worktree identity /ruta/al/worktree --json
cd /ruta/al/worktree

# Dentro del worktree gestionado se infieren actor/sesión.
agent-push origin HEAD:refs/heads/agent/tarea-123
# En un checkout sin registro, ambos valores son obligatorios:
AGENT_ACTOR=codex AGENT_SESSION=tarea-123 agent-push origin HEAD:refs/heads/agent/tarea-123

# Reescritura deliberada, siempre contra SHA remoto observado por el wrapper:
agent-push --rewrite origin HEAD:refs/heads/agent/tarea-123
# Borrado deliberado de una rama, también con lease exacto:
agent-push --delete origin :refs/heads/agent/tarea-123
# Excepción de trunk acotada a esta operación y registrada con su motivo:
agent-push --allow-trunk --reason 'recuperación aprobada' origin HEAD:refs/heads/main
```

`agent-push` acepta uno o varios `SRC:refs/heads/DST` (o `refs/tags/DST`), resuelve
cada objeto antes de publicar y usa `git push --atomic --porcelain` junto con
`--force-with-lease=<ref>:<sha>` para **cada destino**. Un remoto sin soporte
atomic produce error; no hay fallback parcial. El wrapper toma y libera solo
los locks que adquiere para esa operación; conserva los que ya pertenecían a
la sesión. Un lock ajeno, expirado, malformado o heredado sin migrar bloquea.
Una carrera remota invalida el lease, aunque el remote-tracking local esté viejo.

El hook recibe de Git solamente nombre/URL del remoto y las actualizaciones
por stdin. Comprueba cada SHA enviado, no HEAD ni el árbol de trabajo actual.
Reescrituras, reemplazo de tags, borrados y cambios directos al trunk
requieren la autorización de un solo uso del wrapper, ligada a repositorio,
remoto, conjunto exacto de refs/SHA, actor y sesión. Caduca en cinco minutos y
se consume incluso si la operación se rechaza. El trunk es `main`, `master` y
la rama por defecto que el remoto anuncia, sea cual sea su nombre.
`ALLOW_MAIN_PUSH` no autoriza trunk; `AGENT_SKIP_PUSH_CHECKS` se rechaza. Los
merges de PR hechos por GitHub siguen su flujo normal, fuera del hook local.

Para compatibilidad, `git push` identificado permite crear refs y avances
fast-forward que pasan los controles. Ese camino no aporta atomicidad ni
confirmación del resultado remoto. El flujo de agentes usa `agent-push`.

Bypasses conocidos, todos deliberados y visibles: `git push --no-verify` evita
que Git ejecute el hook, y el hook local no puede impedirlo. Es el mismo límite
que `git update-ref` directo sobre una ref: el hook protege el push, no el
repositorio. Esta coordinación presupone clientes cooperativos y no sustituye
controles del servidor. Lo que sí hace `agent-push` es detectar que el hook no
consumió la autorización de la operación, y en ese caso reporta fallo aunque
Git haya publicado: por eso el flujo de agentes no usa `git push` directo.

El verificador se configura con `agent.pushVerify`: es una cadena de argumentos
interpretada con `shlex`, sin shell; recibe el SHA anunciado como argumento
final y `AGENT_VERIFY_SHA`/`AGENT_VERIFY_REF`/`AGENT_VERIFY_OBJECT_TYPE` en su
entorno. Corre una vez por ref no borrada en un checkout descartable limpio de
ese SHA. `optional` permite que no haya comando; cualquier comando configurado
que falle bloquea **todo** el push, igual que `required`. `required` sin comando
también bloquea. No se usa la antigua variable `AGENT_PUSH_VERIFY`. El timeout
predeterminado es de 300 segundos por ref, configurable con
`agent.pushVerifyTimeout` (1–86400). Para los repos de gobernanza documental
hay un verificador listo, `bin/agent-verify-docs`, descrito más abajo.

La ruta del verificador debe ser absoluta: corre con el directorio de trabajo
en el checkout descartable, donde una ruta relativa no existe.

## Verificador de documentación: `bin/agent-verify-docs`

`agent-verify-docs` es el verificador para los repos de gobernanza documental
(`personal-docs`, `avla-docs-reporteria`) y para `agent-tasks`. Recibe el SHA
anunciado como argumento final, en el mismo contrato que cualquier otro
verificador de `agent.pushVerify`.

Qué hace, en este orden:

1. Resuelve el objeto anunciado y comprueba que el `HEAD` del checkout
   corresponde a ese commit. Si no, falla: el verificador juzga una cosa
   distinta de la que se publica.
2. Lista los `.md` **agregados y modificados en ese commit**, con
   `git diff-tree --no-commit-id --name-only -r --diff-filter=AM <sha>`.
3. Corre el linter de documentación sobre esos archivos con la **misma regla
   de severidad que el `pre-commit`**: un documento **nuevo** tiene que pasar
   todas las reglas; un documento **previo modificado** solo bloquea por
   errores críticos (mojibake, posible secreto, links `file://`) y el resto se
   informa como aviso.
4. Si el commit no agrega ni modifica ningún `.md`, sale con 0: un commit de
   código no tiene por qué fallar.

Por qué no usa `origin/main`: en el checkout descartable el `origin` es el
**worktree local**, no GitHub. Ahí `origin/main` no existe cuando el
repositorio de origen no tiene una rama local `main`, y cuando existe
describe otra cosa: no el commit anunciado sino el estado de la rama. El
linter en modo `--changed` (`git diff origin/main...HEAD`) falla en ese
checkout o revisa el árbol equivocado. El helper saca el conjunto de archivos
del **propio commit**, así que juzga exactamente lo que se publica, sin
depender de ninguna referencia remota.

Configuración:

| Qué | Dónde | Valor por defecto |
| --- | --- | --- |
| Ruta del verificador | `agent.pushVerify` | ninguna: hay que declararla, y **absoluta** |
| Modo | `agent.pushVerifyMode` | ninguna: hay que declararlo (`optional` o `required`) |
| Ruta del linter de documentación | `DOCS_LINT` | `~/.agents/skills/docs-writer/lint_docs.py` |
| Perfil del linter | `--profile` o `DOCS_LINT_PROFILE` | `docs` |
| Linter de tareas | `--profile tasks` | `<repo>/scripts/lint_tasks.py` (dentro del repositorio) |

El valor por defecto del linter es `~/.agents/skills/docs-writer/lint_docs.py`:
es el mismo archivo que usa el `pre-commit` de los repos de documentación, así
que el hook y el verificador aplican las mismas reglas. El linter **no** se
modifica ni se copia a este repositorio: vive fuera, en el filesystem del
usuario, y el helper lo carga por ruta absoluta. Si no está, el verificador
falla con un mensaje accionable (no se encontró el linter en …; instalá el
skill docs-writer o configurá la ruta), nunca con una excepción sin contexto.

`DOCS_LINT` y `DOCS_LINT_PROFILE` no llegan al verificador por sí solas: el
hook filtra el entorno y solo deja pasar las variables declaradas en
`agent.pushVerifyEnv`. Para usarlas hay que declararlas:

```bash
git -C /ruta/al/repo config agent.pushVerifyEnv "DOCS_LINT DOCS_LINT_PROFILE"
```

Los prefijos `AGENT_` y `GIT_` están rechazados en ese allowlist, así que las
variables de este helper no los usan.

Declarar `required` en un repositorio documental:

```bash
# personal-docs
git -C /home/human/Documents/personal-docs config agent.pushVerifyMode required
git -C /home/human/Documents/personal-docs config agent.pushVerify \
  /home/human/Documents/agent-tooling/bin/agent-verify-docs

# avla-docs-reporteria
git -C /home/human/Documents/avla-docs-reporteria config agent.pushVerifyMode required
git -C /home/human/Documents/avla-docs-reporteria config agent.pushVerify \
  /home/human/Documents/agent-tooling/bin/agent-verify-docs

# agent-tasks, cuyo linter vive dentro del propio repositorio
git -C /home/human/Documents/agent-tasks config agent.pushVerifyMode required
git -C /home/human/Documents/agent-tasks config agent.pushVerify \
  "/home/human/Documents/agent-tooling/bin/agent-verify-docs --profile tasks"
```

La ruta puede ser la del repositorio de tooling o un enlace en
`~/.local/bin`, que es donde ya viven `agent-push`, `agent-lock` y
`agent-worktree`:

```bash
ln -sfn /home/human/Documents/agent-tooling/bin/agent-verify-docs ~/.local/bin/agent-verify-docs
git -C /home/human/Documents/personal-docs config agent.pushVerify \
  /home/human/.local/bin/agent-verify-docs
```

El enlace tiene que ser un **enlace simbólico**, no una copia: el wrapper
resuelve su propia ruta con `readlink -f` para encontrar `lib/agent_verify_docs.py`
que tiene al lado. Un archivo copiado a otro lugar no encuentra la librería y
falla.

El perfil `tasks` cubre el linter de `agent-tasks`, que no distingue errores
críticos de los demás: ahí cualquier error bloquea, en documentos nuevos y en
documentos previos modificados, que es la misma regla que su `pre-commit`.

**Limitación fundamental:** el verificador ve el **commit anunciado**, no el
árbol de trabajo. No puede juzgar si un cambio sin commitear está bien, ni
detectar un archivo roto que todavía no está en ningún commit. Solo publica
correctamente lo que ya está commiteado. Además, el helper no modifica el
linter: si las reglas cambian, hay que actualizar el skill docs-writer y
reinstalarlo, no el repositorio de tooling.

Un tag anotado anuncia el **objeto tag**, no el commit. El checkout se hace del
commit peeled, así que `HEAD` no coincide con el SHA anunciado; el verificador
recibe el objeto tag y `AGENT_VERIFY_OBJECT_TYPE=tag`, y el objeto tag queda
en el store del checkout para validaciones como `git verify-tag`. Un verificador
que compare contra `HEAD` debe resolver el tag antes.

El entorno del verificador conserva PATH, HOME, USER, LOGNAME, TMPDIR, LANG,
LC_* y SYSTEMROOT; excluye tokens de proveedores y la autorización del push.
`agent.pushVerifyEnv` permite agregar nombres de variables revisados y
explícitos. Es código de confianza ejecutado con el mismo usuario y acceso
al filesystem: el checkout descartable **no es un sandbox de seguridad**.

```bash
agent-lock acquire /ruta/al/repo --branch agent/tarea-123 --agent codex --session tarea-123
agent-lock check /ruta/al/repo --branch agent/tarea-123 --agent codex --session tarea-123 --json
agent-lock renew /ruta/al/repo --branch agent/tarea-123 --agent codex --session tarea-123
agent-lock release /ruta/al/repo --branch agent/tarea-123 --agent codex --session tarea-123
# Un lock expirado requiere recuperación explícita con motivo, nunca --force.
agent-lock recover /ruta/al/repo --branch agent/tarea-123 --agent codex --session tarea-123 --reason 'sesión terminada'
agent-worktree close /ruta/al/repo --agent codex --session tarea-123
```

Los worktrees se crean fuera del clon, en `.agent-worktrees/<repo_id>/<sesión>`
junto a su directorio principal. `close` rechaza cambios tracked, untracked e
ignorados, incluso con `--force`; conserva la rama por defecto. `close --prune`
solo elimina una rama si un ref remoto recién consultado conserva su commit
exacto. Un squash merge no acredita esa preservación: se conserva la rama.
`reconcile` inspecciona registros y locks sin borrar trabajo; el estado legado
se migra explícitamente mediante `migrate` con identidad y motivo.

Los eventos JSONL están en `~/.cache/agent-changesets/<repo_id>.jsonl` (o
`AGENT_CHANGESET_DIR`). Incluyen versión de esquema, host, repositorio, actor,
sesión y operación. Registran intentos, rechazos, verificaciones y excepciones.
`hook-accepted` solo confirma que pasó el hook. `remote-result` distingue
`confirmed` de `indeterminate`; el wrapper usa el resultado porcelain para
confirmar aceptación/rechazo. Un push crudo queda `indeterminate`, pues el
hook no ve su resultado. No se debe contar intentos como publicaciones.

La apertura y el cierre de sesiones, y el borrado de una rama, se auditan aparte
en `~/.cache/agent-worktrees/<repo_id>.audit.jsonl` (o `AGENT_WORKTREE_DIR`),
con actor, sesión, rama, commit y motivo. Un cierre con rama conservada también
queda registrado: el registro de la sesión termina ahí. Las migraciones del
estado legado exigen `--reason` y quedan en el mismo archivo.

Variables de entorno que aíslan el estado: `AGENT_LOCK_DIR` (locks),
`AGENT_WORKTREE_DIR` (registro de sesiones) y `AGENT_CHANGESET_DIR` (eventos de
publicación). Sin ellas, el estado vive bajo `~/.cache/`. Dos sesiones que
compartan el mismo `HOME` comparten esos archivos por diseño; las que necesiten
aislar el estado deben fijar las tres variables.

El instalador respeta `core.hooksPath`, también relativo a un linked worktree.
Encadena hooks ejecutables anteriores con el mismo stdin y conserva el lint
pre-commit de docs-writer. No encadena la antigua barrera gestionada al migrarla.
Reinstalar valida los dispatchers antes de cambiar nada; los runtimes tienen
hash de contenido e identidad inmutable. Cada hooksPath conserva un
`agent-tooling-manifest.json` con runtime, hash de fuente, hooks y backups.
Para rollback, detener nuevas publicaciones, restaurar cada `backup` en su
nombre de hook (o quitar el dispatcher si `backup` es null) y retirar el
manifest. Se conservan los runtimes anteriores para no romper procesos activos.

```bash
python3 -m unittest discover -s tests -v
```
