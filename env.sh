#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Foukenstein light — environnement (sans secrets)
# Les secrets vont dans env.secret.sh (non versionné).
# ─────────────────────────────────────────────────────────────────────────────

# ── Web server ───────────────────────────────────────────────────────────────
# Render/Fly/Heroku injectent PORT ; on garde WEB_PORT en fallback local.
export WEB_HOST="0.0.0.0"
export WEB_PORT="9999"

# Dossiers : auto-dérivés depuis app.py (dossier parent de app/).
# Décommenter uniquement pour forcer des chemins custom.
#export WEB_PUBLIC_DIR="/abs/path/to/web"
#export MEMORY_DIR="/abs/path/to/data/memory"

# ── Concurrence / rate limit ─────────────────────────────────────────────────
export ASK_CONCURRENCY_LIMIT="4"
export MAX_QUESTION_CHARS="160"
export RATE_LIMIT_SECONDS="40"

# ── Pipeline Mistral grounded ────────────────────────────────────────────────
export MISTRAL_MODEL="mistral-large-latest"
export TEMPERATURE="0.70"
export MODULES_MAX="2"
export MODULES_MAX_CHARS="17000"

# Grounding Wikipedia ON par défaut (mettre "0" pour désactiver)
export GROUNDING_ENABLED="1"
