# Network Volume — XTTS pour Foukenstein Serverless

État du Network Volume Runpod qui contient tous les assets XTTS nécessaires
au handler serverless. Les fichiers ont été uploadés depuis le pod par
`scripts/sync_xtts_to_volume.sh` le 2026-04-14.

---

## Identification

| Paramètre | Valeur |
|---|---|
| **Bucket (= Network Volume ID)** | `srvvzpk6jj` |
| **Région** | `eu-ro-1` |
| **Endpoint S3** | `https://s3api-eu-ro-1.runpod.io` |
| **Prefix racine** | `xtts/` |
| **Taille totale** | **7.2 GiB** (8 objets) |

**Region-lock** : ce volume est attaché à la région EU-RO-1. Toute instance
serverless qui doit le monter **doit tourner dans la même région**. C'est
un choix délibéré (latence faible depuis l'Europe).

---

## Contenu du volume

```
s3://srvvzpk6jj/xtts/
├── best_model_19875.pth                          5.2 GiB  ← checkpoint fine-tuné (25 epochs voix Foucault)
├── test_speaker.wav                              749 KiB  ← voix de référence pour le speaker embedding
└── XTTS_v2.0_original_model_files/               ~1.9 GiB  ← modèle XTTS v2 base (Coqui)
    ├── config.json                               4.3 KiB
    ├── vocab.json                                353 KiB
    ├── mel_stats.pth                             1.0 KiB
    ├── speakers_xtts.pth                         132 KiB
    ├── dvae.pth                                  201 MiB
    └── model.pth                                 1.7 GiB
```

### Intégrité

Au moment du sync, la taille du checkpoint critique a été vérifiée octet-près :

```
local  : 5607927189 bytes
remote : 5607927189 bytes
✓ identiques
```

---

## Points de montage côté worker serverless

Quand tu attaches ce volume à un endpoint Runpod Serverless, le worker voit
les fichiers sous `/runpod-volume/` par défaut (chemin standard Runpod).

Chemins à utiliser dans le handler :

| Rôle | Chemin sur le worker |
|---|---|
| Checkpoint fine-tuné (`FTCKPT`) | `/runpod-volume/xtts/best_model_19875.pth` |
| Dossier XTTS v2 base (`ORIG`) | `/runpod-volume/xtts/XTTS_v2.0_original_model_files` |
| Speakers file (`SPEAKERS_PTH`) | `/runpod-volume/xtts/XTTS_v2.0_original_model_files/speakers_xtts.pth` |
| Voix de référence (`SPEAKER_WAV`) | `/runpod-volume/xtts/test_speaker.wav` |

Ce sont exactement les mêmes variables que `/workspace/start_tts_frozen.sh`
utilise sur le pod actuel, juste avec le préfixe `/runpod-volume/` à la place
de `/workspace/runs/...`.

---

## Re-sync / mise à jour

Le script `scripts/sync_xtts_to_volume.sh` est idempotent :

- `aws s3 sync` skippe automatiquement les fichiers dont la taille et la date
  sont inchangées → relancer le script est sûr et rapide.
- Si tu modifies un fichier source, le script ré-uploadera uniquement celui-là.
- Si tu ajoutes un nouveau fichier dans `XTTS_v2.0_original_model_files/`, il
  sera uploadé aussi.
- Si tu **supprimes** un fichier source et veux nettoyer côté volume, il faut
  le faire manuellement — le script n'utilise pas `--delete` sur `s3 sync`,
  par prudence (on ne casse rien par accident).

Pour relancer :

```bash
cd /workspace/deployment_v1
# Assure-toi que AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY sont exportés
# (soit inline, soit via env.secret.sh)
bash scripts/sync_xtts_to_volume.sh
```

---

## Prochaines étapes vers le déploiement serverless

### Étape 1 — Écrire le handler serverless

À créer dans un dossier dédié (suggéré : `deployment_v1/runpod_handler/`).

Le handler est un port de `/workspace/IA/bin/xtts_http_server.py` vers la
signature Runpod :

```python
def handler(event):
    chunks = event["input"]["chunks"]
    # ... inférence XTTS ...
    return {"audio_base64": "...", "format": "wav", "chunks": n}
```

Points critiques :

1. **Charger le modèle une seule fois** au scope global du module (hors de
   la fonction `handler`). Runpod garde le worker en vie entre invocations
   → le modèle reste en VRAM. FlashBoot snapshotera cet état post-load.
2. **Réutiliser la logique stitching** de `xtts_http_server.py` :
   `equal_power_crossfade`, `_subsplit`, `_split_on_word_repetition`,
   les paramètres figés (XFADE_MS=320, PAUSE_MS=160, etc.) — copie verbatim.
3. **Encoder en base64** le WAV final avant retour :
   ```python
   import base64, io, soundfile as sf
   buf = io.BytesIO()
   sf.write(buf, final_audio.numpy(), SR, format="WAV")
   b64 = base64.b64encode(buf.getvalue()).decode("ascii")
   return {"output": {"audio_base64": b64, "format": "wav", "chunks": n}}
   ```

Contrat d'entrée/sortie (déjà fixé côté client `runpod_tts_client.py`) :

**Input :**
```json
{ "input": { "chunks": ["...", "..."], "language": "fr", "out_format": "wav" } }
```

**Output :**
```json
{ "output": { "audio_base64": "...", "format": "wav", "chunks": 5 } }
```

### Étape 2 — Créer l'image Docker du worker

Base recommandée : `nvidia/cuda:11.8.0-runtime-ubuntu22.04` (~2.5 GB).

Dockerfile minimal attendu :

```dockerfile
FROM nvidia/cuda:11.8.0-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.10 python3-pip ffmpeg libsndfile1 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir torch==2.1.0 torchaudio==2.1.0 \
    --index-url https://download.pytorch.org/whl/cu118 && \
    pip install --no-cache-dir -r requirements.txt

COPY handler.py .

CMD ["python3", "-u", "handler.py"]
```

`requirements.txt` minimum :
```
TTS==0.22.0
runpod
soundfile
numpy
```

Taille finale attendue : **~5–6 GB** (la plus grosse part est `torch` avec
les libs CUDA bundlées). C'est la norme pour du PyTorch GPU.

### Étape 3 — Créer l'endpoint Runpod Serverless

Sur la console Runpod :

1. **Serverless** → **New Endpoint** → **Custom Source**
2. Pointer vers ton image Docker (Docker Hub, GHCR, ou Runpod registry)
3. **Attacher le Network Volume `srvvzpk6jj`** (section "Storage" de la
   config d'endpoint)
4. **GPU** : A4000 ou A5000 (24 GB VRAM — XTTS en fp32 tient large)
5. **Region** : **EU-RO-1** (obligatoire, sinon le volume ne peut pas être monté)
6. **Min workers** : commencer à `0` (scale-to-zero, accepter cold start ~30–60 s)
7. **Max workers** : `1` ou `2` suffit pour Foukenstein
8. **Idle timeout** : `5 s` (libère le worker rapidement après une requête)
9. **Env vars** à définir sur l'endpoint :
   ```
   FTCKPT=/runpod-volume/xtts/best_model_19875.pth
   ORIG=/runpod-volume/xtts/XTTS_v2.0_original_model_files
   SPEAKERS_PTH=/runpod-volume/xtts/XTTS_v2.0_original_model_files/speakers_xtts.pth
   SPEAKER_WAV=/runpod-volume/xtts/test_speaker.wav
   LANG=fr
   XFADE_MS=320
   EDGE_FADE_MS=80
   PAUSE_MS=160
   MICRO_PAUSE_MS=40
   TAIL_SILENCE_MS=700
   ```
   (Ces valeurs reproduisent exactement `start_tts_frozen.sh`.)

Une fois l'endpoint créé, tu récupères son `ENDPOINT_ID` (dans l'URL de la
console Runpod), et tu le renseignes côté `deployment_v1/env.sh` :

```bash
export RUNPOD_SERVERLESS_ENDPOINT_ID="xxxxxxxxxxxx"
```

### Étape 4 — Test bout-en-bout

```bash
# Test direct de l'endpoint serverless (sans passer par le web server)
curl -X POST https://api.runpod.ai/v2/$RUNPOD_SERVERLESS_ENDPOINT_ID/runsync \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":{"chunks":["Bonjour, ceci est un test."],"language":"fr","out_format":"wav"}}' \
  | jq -r '.output.audio_base64' | base64 -d > /tmp/test.wav
aplay /tmp/test.wav   # ou télécharge et joue localement
```

Si ça marche, relance le web server `deployment_v1/` et teste `POST /api/ask`
depuis le navigateur : tu dois obtenir le WAV via `/audio/<uuid>.wav`.

---

## Sécurité

- **Aucune clé API** n'est stockée dans ce dossier. Tout passe par
  `env.secret.sh` (non versionné) ou par l'env du worker Runpod.
- Les credentials S3 Runpod utilisés pour l'upload initial ne servent plus
  une fois le volume peuplé — tu peux les révoquer sur la console Runpod
  si tu veux (elles sont nécessaires uniquement pour re-sync ou inspection
  via CLI).
- Le Network Volume lui-même est accessible uniquement aux workers qui le
  montent : pas d'exposition publique.

---

## Références

- Script de sync : `/workspace/deployment_v1/scripts/sync_xtts_to_volume.sh`
- Log du dernier sync : `/workspace/deployment_v1/logs/sync_xtts_live.log`
- Template des secrets : `/workspace/deployment_v1/env.secret.sh.example`
- Architecture globale : `/workspace/deployment_v1/README.md`
- Pipeline d'origine (inchangé) : `/workspace/IA/bin/xtts_http_server.py`
  — c'est ce fichier qui sert de base au futur `handler.py`.
