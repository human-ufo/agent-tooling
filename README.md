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
`agent.pushVerifyTimeout` (1–86400).

La ruta del verificador debe ser absoluta: corre con el directorio de trabajo
en el checkout descartable, donde una ruta relativa no existe.

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
