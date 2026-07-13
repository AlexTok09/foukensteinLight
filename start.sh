#!/usr/bin/env bash
# Foukenstein deployment_v1 — lancement du web server
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# venv existant du pod (réutilisé tel quel)
if [[ -f /workspace/venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source /workspace/venv/bin/activate
fi

# Env non-secret puis secrets (secrets écrasent)
if [[ -f "$HERE/env.sh" ]]; then
  # shellcheck disable=SC1091
  source "$HERE/env.sh"
else
  echo "WARN: $HERE/env.sh introuvable (copie env.sh.example → env.sh)" >&2
fi

if [[ -f "$HERE/env.secret.sh" ]]; then
  # shellcheck disable=SC1091
  source "$HERE/env.secret.sh"
else
  echo "WARN: $HERE/env.secret.sh introuvable (copie env.secret.sh.example → env.secret.sh)" >&2
fi

# Sanity checks
: "${MISTRAL_API_KEY:?ERROR: MISTRAL_API_KEY non défini (env.secret.sh)}"
: "${RUNPOD_API_KEY:?ERROR: RUNPOD_API_KEY non défini (env.secret.sh)}"
: "${RUNPOD_SERVERLESS_ENDPOINT_ID:?ERROR: RUNPOD_SERVERLESS_ENDPOINT_ID non défini (env.sh)}"

mkdir -p "$HERE/data/audio" "$HERE/data/memory" "$HERE/logs/requests"

exec python "$HERE/app/app.py"
