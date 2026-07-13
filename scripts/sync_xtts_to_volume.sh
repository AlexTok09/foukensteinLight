#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# sync_xtts_to_volume.sh — Copie read-only des assets XTTS vers le Runpod
#                          Network Volume via son API S3-compatible.
#
# Contraintes :
#   * Lecture seule sur la source : aucun rm, aucun mv.
#   * Aucun secret dans le code : tout via variables d'environnement.
#   * Vérifie l'accès + loggue + compare les tailles après upload.
#
# Variables requises (à exporter avant de lancer — typiquement via env.secret.sh) :
#   AWS_ACCESS_KEY_ID       clé d'accès S3 Runpod (créée sur la console Runpod)
#   AWS_SECRET_ACCESS_KEY   clé secrète associée
#
# Variables optionnelles (défauts adaptés à eu-ro-1 / bucket srvvzpk6jj) :
#   S3_BUCKET               défaut: srvvzpk6jj
#   S3_ENDPOINT_URL         défaut: https://s3api-eu-ro-1.runpod.io
#   AWS_DEFAULT_REGION      défaut: eu-ro-1
#   S3_PREFIX               défaut: xtts
#
# Sources (chemins figés — modifie ici si tu déplaces les fichiers) :
#   FTCKPT         le checkpoint fine-tuné (5.3 GB)
#   ORIG           le dossier XTTS v2 base (2.0 GB)
#   SPEAKER_WAV    la voix de référence (749 KB)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

LOG_FILE="${LOG_FILE:-/workspace/deployment_v1/logs/sync_xtts_$(date +%Y%m%d_%H%M%S).log}"
mkdir -p "$(dirname "$LOG_FILE")"

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG_FILE"; }
die() { log "ERROR: $*"; exit 1; }

# ── Charge env.secret.sh si présent (sans écraser l'env déjà défini) ─────────
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_ROOT="$(cd "$HERE/.." && pwd)"
if [[ -f "$DEPLOY_ROOT/env.secret.sh" ]]; then
  # shellcheck disable=SC1091
  source "$DEPLOY_ROOT/env.secret.sh"
fi

# ── Validation secrets ───────────────────────────────────────────────────────
: "${AWS_ACCESS_KEY_ID:?AWS_ACCESS_KEY_ID non défini — voir env.secret.sh.example}"
: "${AWS_SECRET_ACCESS_KEY:?AWS_SECRET_ACCESS_KEY non défini — voir env.secret.sh.example}"

export AWS_ACCESS_KEY_ID
export AWS_SECRET_ACCESS_KEY

# ── Config bucket ────────────────────────────────────────────────────────────
S3_BUCKET="${S3_BUCKET:-srvvzpk6jj}"
S3_ENDPOINT_URL="${S3_ENDPOINT_URL:-https://s3api-eu-ro-1.runpod.io}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-eu-ro-1}"
S3_PREFIX="${S3_PREFIX:-xtts}"

S3_BASE="s3://${S3_BUCKET}/${S3_PREFIX}"
AWS_OPTS=(--endpoint-url "$S3_ENDPOINT_URL" --region "$AWS_DEFAULT_REGION")

# ── Sources XTTS (read-only) ─────────────────────────────────────────────────
FTCKPT="${FTCKPT:-/workspace/runs/run_20260210_110029_ft_v2_like_ref_mixed_25ep/run/training/GPT_XTTS_FT-February-10-2026_11+02AM-0000000/best_model_19875.pth}"
ORIG="${ORIG:-/workspace/runs/run_20260210_110029_ft_v2_like_ref_mixed_25ep/run/training/XTTS_v2.0_original_model_files}"
SPEAKER_WAV="${SPEAKER_WAV:-/workspace/test_speaker.wav}"

# ── Vérifs d'existence côté source ───────────────────────────────────────────
log "=== Sync XTTS → Runpod Network Volume (EU-RO-1) ==="
log "Bucket     : $S3_BUCKET"
log "Endpoint   : $S3_ENDPOINT_URL"
log "Prefix     : $S3_PREFIX"
log "Log file   : $LOG_FILE"
log ""

[[ -f "$FTCKPT"      ]] || die "FTCKPT introuvable : $FTCKPT"
[[ -d "$ORIG"        ]] || die "ORIG introuvable : $ORIG"
[[ -f "$SPEAKER_WAV" ]] || die "SPEAKER_WAV introuvable : $SPEAKER_WAV"

FTCKPT_SIZE=$(stat -c%s "$FTCKPT")
ORIG_SIZE=$(du -sb "$ORIG" | awk '{print $1}')
SPEAKER_SIZE=$(stat -c%s "$SPEAKER_WAV")
TOTAL_SIZE=$(( FTCKPT_SIZE + ORIG_SIZE + SPEAKER_SIZE ))

log "Sources :"
log "  FTCKPT      : $FTCKPT"
log "                $(numfmt --to=iec --suffix=B $FTCKPT_SIZE)"
log "  ORIG        : $ORIG"
log "                $(numfmt --to=iec --suffix=B $ORIG_SIZE)"
log "  SPEAKER_WAV : $SPEAKER_WAV"
log "                $(numfmt --to=iec --suffix=B $SPEAKER_SIZE)"
log "  TOTAL       : $(numfmt --to=iec --suffix=B $TOTAL_SIZE)"
log ""

# ── Test de connexion au bucket (list) ───────────────────────────────────────
log "--- Test de connexion au bucket (list) ---"
if aws s3 ls "${AWS_OPTS[@]}" "s3://${S3_BUCKET}/" 2>&1 | tee -a "$LOG_FILE"; then
  log "✓ Accès bucket OK"
else
  die "Impossible de lister le bucket — vérifier credentials / endpoint"
fi
log ""

# ── Upload : speaker wav (petit, premier pour valider la chaîne complète) ────
log "--- [1/3] Upload test_speaker.wav ---"
aws s3 cp "${AWS_OPTS[@]}" "$SPEAKER_WAV" "${S3_BASE}/test_speaker.wav" 2>&1 | tee -a "$LOG_FILE"
log ""

# ── Upload : XTTS v2 base files (dossier) ────────────────────────────────────
log "--- [2/3] Upload XTTS_v2.0_original_model_files/ ---"
aws s3 sync "${AWS_OPTS[@]}" "$ORIG" "${S3_BASE}/XTTS_v2.0_original_model_files/" 2>&1 | tee -a "$LOG_FILE"
log ""

# ── Upload : checkpoint fine-tuné (gros, en dernier) ─────────────────────────
log "--- [3/3] Upload best_model_19875.pth (5.3 GB) ---"
aws s3 cp "${AWS_OPTS[@]}" "$FTCKPT" "${S3_BASE}/best_model_19875.pth" 2>&1 | tee -a "$LOG_FILE"
log ""

# ── Vérification des tailles côté destination ───────────────────────────────
log "--- Vérification des tailles côté bucket ---"
aws s3 ls "${AWS_OPTS[@]}" --recursive --human-readable --summarize "${S3_BASE}/" 2>&1 | tee -a "$LOG_FILE"
log ""

# ── Vérification octet-près sur le checkpoint critique ──────────────────────
log "--- Vérif taille exacte du checkpoint ---"
REMOTE_FTCKPT_SIZE=$(aws s3api head-object "${AWS_OPTS[@]}" \
  --bucket "$S3_BUCKET" \
  --key    "${S3_PREFIX}/best_model_19875.pth" \
  --query  'ContentLength' --output text)

log "  local  : $FTCKPT_SIZE bytes"
log "  remote : $REMOTE_FTCKPT_SIZE bytes"

if [[ "$REMOTE_FTCKPT_SIZE" == "$FTCKPT_SIZE" ]]; then
  log "✓ Tailles identiques — upload validé"
else
  die "✗ DIVERGENCE de taille sur best_model_19875.pth"
fi

log ""
log "=== Sync terminé ==="
log "Log complet : $LOG_FILE"
