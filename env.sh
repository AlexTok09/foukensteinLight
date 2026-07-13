#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Foukenstein deployment_v1 — template d'environnement (sans secrets)
#
# Copie ce fichier en `env.sh` et remplis les valeurs.
# Les secrets (clés API) doivent aller dans `env.secret.sh` (non versionné).
# ─────────────────────────────────────────────────────────────────────────────

# ── Web server ───────────────────────────────────────────────────────────────
export WEB_HOST="127.0.0.1"
export WEB_PORT="9999"

# Dossiers : par défaut auto-dérivés depuis l'emplacement de app.py
# (le dossier parent de app/). Ne décommenter que pour forcer des chemins custom.
#export WEB_PUBLIC_DIR="/workspace/deployment_v1/web"
#export AUDIO_DIR="/workspace/deployment_v1/data/audio"
#export MEMORY_DIR="/workspace/deployment_v1/data/memory"

# ── Runpod Serverless TTS ────────────────────────────────────────────────────
# L'endpoint ID se trouve dans l'URL de ta page Runpod Serverless :
#   https://www.runpod.io/console/serverless/user/endpoint/XXXXXXXXX
export RUNPOD_SERVERLESS_ENDPOINT_ID="rilfc916e99y2n"
export RUNPOD_TTS_TIMEOUT="180"
export RUNPOD_TTS_LANGUAGE="fr"
export ASK_CONCURRENCY_LIMIT="3"
export TTS_CONCURRENCY_LIMIT="3"
export RUNPOD_TTS_CONCURRENCY_LIMIT="3"
export MAX_QUESTION_CHARS="160"
export TTS_RATE_LIMIT_SECONDS="40"

# Format de sortie audio : "wav" ou "mp3"
export OUT_FORMAT="wav"

# ── TTL cleanup ──────────────────────────────────────────────────────────────
export AUDIO_TTL_HOURS="48"
export CLEANUP_INTERVAL_MIN="30"

# ── Pipeline Mistral grounded ────────────────────────────────────────────────
# Prompts, persona et modules sont embarqués dans deployment_v1/ → 100 % autonome.
# Les chemins SYSTEM_PROMPT_PATH / PERSONA_PATH / MODULES_DIR / DEBUG_LOG_DIR /
# TOKEN_LOG sont auto-dérivés depuis DEPLOY_ROOT par app.py au démarrage.
# Décommenter ci-dessous uniquement pour forcer des chemins custom.
export MISTRAL_MODEL="mistral-large-latest"
export TEMPERATURE="0.70"
export MODULES_MAX="2"
export MODULES_MAX_CHARS="17000"

#export SYSTEM_PROMPT_PATH="/abs/path/to/system_foucault.txt"
#export PERSONA_PATH="/abs/path/to/foukenstein_persona.txt"
#export MODULES_DIR="/abs/path/to/modules"
#export DEBUG_LOG_DIR="/abs/path/to/logs/requests"
#export TOKEN_LOG="/abs/path/to/logs/token_usage.log"
