#!/usr/bin/env bash
# Idempotent installation with hook chaining and a rollback manifest.
set -Eeuo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${here}/lib/agent_publish.py" install "$@"
