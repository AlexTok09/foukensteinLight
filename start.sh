#!/usr/bin/env bash
# Foukenstein light — lancement du web server
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# venv optionnel
if [[ -f "$HERE/venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$HERE/venv/bin/activate"
elif [[ -f /workspace/venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source /workspace/venv/bin/activate
fi

# Env non-secret puis secrets (secrets écrasent)
if [[ -f "$HERE/env.sh" ]]; then
  # shellcheck disable=SC1091
  source "$HERE/env.sh"
else
  echo "WARN: $HERE/env.sh introuvable" >&2
fi

if [[ -f "$HERE/env.secret.sh" ]]; then
  # shellcheck disable=SC1091
  source "$HERE/env.secret.sh"
else
  echo "WARN: $HERE/env.secret.sh introuvable (copie env.secret.sh.example → env.secret.sh)" >&2
fi

# Sanity check : uniquement la clé Mistral (pas de RunPod / TTS)
: "${MISTRAL_API_KEY:?ERROR: MISTRAL_API_KEY non défini}"

mkdir -p "$HERE/data/memory" "$HERE/logs/requests"

exec python3 "$HERE/app/app.py"
