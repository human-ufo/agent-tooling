# agent-tooling

Coordinación de escritura entre sesiones de agentes. Agnóstico de marca: no
depende de Codex, OpenCode, Kimi, Qwen ni ningún otro agente. Son scripts y
hooks de shell sobre `git`, y nada más.

El problema que resuelve: varias sesiones de agentes trabajan sobre los mismos
repos y se pisan. El síntoma real no es «dos sesiones empujaron a main»: es que
**una sesión corre `git stash`, `git checkout -- .` o `git reset --hard` sobre
el trabajo sin commitear de otra**. Un lock advisory no lo evita, porque el
daño ocurre en el working tree, no en el remoto.

## Qué hay

| Pieza | Qué hace |
|---|---|
| `bin/agent-worktree` | Un worktree por sesión. Cada sesión trabaja en su propio árbol y su propia rama. Es la garantía que evita el daño. |
| `bin/agent-lock` v2 | Coordinación advisory con alcance por rama y sesión. A diferencia de v1, **se niega a liberar** el lock de otro y avisa cuando no hay conflicto real. |
| `hooks/pre-push` | La barrera de escritura: protege el trunk, exige `--force-with-lease` en vez de `--force`, bloquea si otra sesión tiene tomada esa rama, y registra cada cambio con su actor. |
| `install-hooks.sh` | Instala los hooks en uno o más repos. |

## Por qué aquí y no en el repo de una marca

Porque el problema es de todos los agentes, y una herramienta compartida por
todos no debería vivir en el repositorio de uno. Este repo no depende de
ningún agente: son scripts de shell sobre `git`.

## Por qué hooks locales y no branch protection

El branch protection de GitHub no está disponible en repos privados del plan
gratis, y aun con plan pagado la barrera vive en el remoto: protege el push y
no el working tree. Lo que faltó el 21/10 de octubre fue que una sesión
tocara el trabajo de otra en el disco. Eso lo resuelve el aislamiento, no una
política de servidor.

El `pre-push` local cubre además lo que el plan gratis no da: proteger el
trunk, impedir el `--force` pelado y dejar registro de quién empujó qué.

## Uso

```bash
# Instalar los hooks en los repos con los que se trabaja
./install-hooks.sh ~/Documents/avla-docs-reporteria ~/Documents/avla-data-ingestion-platform

# Abrir el worktree de esta sesión y trabajar ahí
agent-worktree open ~/Documents/avla-docs-reporteria --agent agy-docs-writer --session docs-mx

# Tomar la rama
agent-lock acquire ~/Documents/avla-docs-reporteria --agent agy-docs-writer \
  --session docs-mx --branch agent/agy-docs-writer/20261021-2100

# Estado
agent-worktree list ~/Documents/avla-docs-reporteria
agent-lock list

# Cerrar
agent-lock release ~/Documents/avla-docs-reporteria --agent agy-docs-writer --session docs-mx
agent-worktree close ~/Documents/avla-docs-reporteria --agent agy-docs-writer --session docs-mx
```

## Lo que cada capa cubre

| Capa | Cubre | No cubre |
|---|---|---|
| Worktree por sesión | Que una sesión toque el working tree de otra. Es la capa que previene el incidente. | Que dos sesiones compitan por la misma rama: git lo impide con un error, que es la respuesta correcta. |
| Lock con alcance | Que dos sesiones se avisen antes de trabajar en la misma rama. | Nada por la fuerza: sigue siendo advisory. |
| Hook `pre-push` | Push al trunk, `--force` sin lease, rama tomada por otra sesión, registro con actor. | El working tree: para eso está el worktree. |

## Registro de cambios

Cada push permitido deja una línea en `~/.cache/agent-changesets/<repo>.jsonl`:

```json
{"ts":"2026-10-21T21:04:12Z","actor":"agy-uno","session":"s1","repo":"clone",
 "branch":"docs/una","local_sha":"76a6045…","remote_sha_before":"0000000…",
 "worktree":"/home/human/Documents/avla-docs-reporteria/.worktrees/s1","remote":"origin"}
```

Un push bloqueado no deja rastro, porque no hubo cambio.

## Verificación

```bash
python3 -m unittest discover -s tests -v
```

24 tests: 13 del aislamiento y el lock, 11 del hook de push.

## Límites

- **El hook corre en la máquina del agente.** Una sesión que lo desinstale no lo tiene. Contra eso está el worktree, que no depende de un hook.
- **El aislamiento es por directorio.** Dos sesiones en la misma máquina con el mismo clon siguen compartiendo `.git`, y por lo tanto los refs. El worktree resuelve el árbol de trabajo, no el directorio de refs.
- **`agent-lock` no es una barrera.** Si dos sesiones lo ignoran, el hook `pre-push` los frena solo si toman el lock; sin lock, cada una puede empujar su rama. Para el trunk está el hook, que no se puede saltar sin `ALLOW_MAIN_PUSH=1`.
- **La verificación previa al push es optativa** (`AGENT_PUSH_VERIFY`). Sin ella, el hook no corre ningún control de contenido.
- **No resuelve los conflictos de contenido.** Dos sesiones que tocan las mismas líneas van a chocar cuando una mergea su pull request. El merge lo hace el agente, como siempre; lo que se detiene es el caso en que hay que decidir qué línea queda, y ese lo resuelve una persona. La verificación puede rechazar el push antes de llegar ahí.
