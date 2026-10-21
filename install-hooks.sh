#!/usr/bin/env bash
# Instala los hooks de escritura en uno o más repos.
# Uso: install-hooks.sh <repo> [<repo> ...]
#
# Instala:
#   .git/hooks/pre-push  (la barrera de escritura)
#   .git/hooks/pre-commit (el lint de documentación, si el repo lo trae)
#
# Un hook previo distinto se respalda como <nombre>.bak-<fecha>.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
stamp="$(date +%Y%m%d%H%M%S)"

install_hook() {
  # $1 = repo, $2 = nombre del hook, $3 = fuente
  local repo="$1" name="$2" src="$3"
  local hooks target
  hooks="$(git -C "${repo}" rev-parse --git-path hooks)"
  [[ "${hooks}" = /* ]] || hooks="${repo}/${hooks}"
  target="${hooks}/${name}"
  mkdir -p "${hooks}"
  if [[ -L "${target}" && "$(readlink -f "${target}")" == "$(readlink -f "${src}")" ]]; then
    echo "ya instalado: ${target}"
    return 0
  fi
  if [[ -e "${target}" ]]; then
    mv "${target}" "${target}.bak-${stamp}"
    echo "respaldo: ${target}.bak-${stamp}"
  fi
  ln -s "${src}" "${target}"
  echo "instalado: ${target} -> ${src}"
}

for repo in "$@"; do
  [[ -d "${repo}" ]] || { echo "no existe: ${repo}" >&2; continue; }
  install_hook "${repo}" pre-push "${here}/pre-push"
  # El lint de documentacion vive con la skill que lo define.
  lint_hook="${HOME}/.agents/skills/docs-writer/pre-commit"
  if [[ -f "${lint_hook}" ]]; then
    install_hook "${repo}" pre-commit "${lint_hook}"
  fi
done
