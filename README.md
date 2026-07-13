# Foukenstein light

Version text-only de Foukenstein — chatbot persona Foucault, réponses écrites au-dessus du col roulé 3D. Pas de synthèse vocale, pas de dépendance à RunPod.

## Architecture

```
Client (index.html + Three.js) ──POST /api/ask──> app.py
                                                     │
                                                     └──> pipeline_grounded.py
                                                              │
                                                              └──> generate_grounded.py
                                                                       │
                                                                       ├── Mistral API
                                                                       └── Wikipedia grounding
```

Réponse : `{ ok: true, chunks: ["…", "…"] }`. Le frontend affiche les chunks en typewriter au-dessus du col roulé.

## Contenu

- `app/` — serveur HTTP + pipeline Mistral + grounding + routeur sémantique
- `web/` — `index.html` (col roulé 3D + text-only chat), `about.html`, `contact.html`, `knitted_turtleneck_animated.glb`
- `prompts/` — `system_foucault.txt`
- `persona/` — `foukenstein_light.txt`
- `modules/` — corpus thématiques + noms propres (voir `[ROUTING]` / `[KEYWORDS]`)
- `data/memory/` — historique conversationnel par session (créé au runtime)

## Setup

```bash
cp env.secret.sh.example env.secret.sh
# Renseigner MISTRAL_API_KEY
chmod +x start.sh
./start.sh
```

Serveur sur `http://0.0.0.0:$WEB_PORT` (défaut : `9999`). En hébergement PaaS, `PORT` est respectée en priorité.

## Déploiement

Aucune dépendance Python externe (stdlib uniquement) → une VM ou un Web Service PaaS suffit.

- **Render** : `Web Service` type `Python`, build command vide, start command `bash start.sh`, env var `MISTRAL_API_KEY`. Ajouter un `Persistent Disk` monté sur `data/memory/` si tu veux garder l'historique conversationnel entre les déploiements.
- **Oracle Cloud Always Free (ARM Ampere)** : `systemd` unit qui exec `start.sh`, plus un `cloudflared` tunnel si tu veux garder l'URL publique en `*.foukenstein.lol`.
- **Hetzner CX22 / VPS** : idem.

## Variables d'environnement

Non-secrètes (`env.sh`) :

| Var | Défaut | Effet |
|---|---|---|
| `WEB_HOST` | `0.0.0.0` | Interface d'écoute |
| `WEB_PORT` | `9999` | Port (surchargé par `PORT` si présent) |
| `MISTRAL_MODEL` | `mistral-large-latest` | Modèle Mistral |
| `TEMPERATURE` | `0.70` | Température de génération |
| `MODULES_MAX` | `2` | Nombre max de modules injectés dans le system prompt |
| `MODULES_MAX_CHARS` | `17000` | Cap de longueur par module |
| `GROUNDING_ENABLED` | `1` | Grounding Wikipedia ON/OFF |
| `ASK_CONCURRENCY_LIMIT` | `4` | Requêtes /api/ask simultanées |
| `MAX_QUESTION_CHARS` | `160` | Longueur max d'une question |
| `RATE_LIMIT_SECONDS` | `40` | Fenêtre de rate-limit par IP |

Secrètes (`env.secret.sh`, non versionné) :

| Var | Effet |
|---|---|
| `MISTRAL_API_KEY` | Clé API Mistral |

## Différences avec `deployment_v1`

- Pas de `/api/tts`, `/audio/*`, `/download/*`, `/api/worker_status`
- Pas de `runpod_tts_client.py` ni de dossier `runpod_handler/`
- Pas de "slave mode" (`slave.html` retiré, lien nav retiré)
- Frontend : le lecteur audio est remplacé par un rendu typewriter du texte au-dessus du col roulé, avec pulse d'animation sur les morphs pendant la frappe
- `pipeline_grounded.py` retourne `list[str]` (chunks) au lieu de `(bytes, str, list[str])`
- `app.py` allégée d'environ 300 lignes ; toutes les variables `RUNPOD_*` / `TTS_*` / `OUT_FORMAT` / `AUDIO_TTL_*` sont supprimées

## Licence

Personnelle, non redistribuable sans accord.
