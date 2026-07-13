# Foukenstein — deployment_v1

Déploiement autonome de Foukenstein pour une architecture **web/orchestration CPU + synthèse XTTS sur RunPod Serverless GPU**.

Ce dossier est isolé : prompts, persona, modules, données runtime, logs et scripts vivent dans `deployment_v1/`. Le service actif est `app/app.py`, servi par un `ThreadingHTTPServer` Python sur `WEB_PORT` (`9999` par défaut).

## État Actuel

Le pipeline de production n'est plus le pipeline light historique. Les routes principales passent par le pipeline grounded :

```text
Client navigateur
  -> POST /api/ask {"question": "..."}
  -> app/app.py
  -> app/pipeline_grounded.py
  -> app/generate_grounded.py
  -> Mistral chat completions
  -> RunPod Serverless XTTS /runsync
  -> data/audio/<request_id>.wav
  -> { audio_url, download_url, format, chunks }
```

Le pipeline light historique (`pipeline.py` + `generate_light.py`) a été déplacé dans `Archive/` pour éviter qu'il reste dans le chemin actif. `/api/ask` utilise `pipeline_grounded.run()`.

Points importants du runtime actuel :

- `/api/ask` : pipeline complet grounded, génération + TTS.
- `/api/tts` : mode texte brut vers RunPod, sans Mistral.
- `/api/generate` : route désactivée (`404`), ancien handler archivé/nettoyé.
- `/test.html` : ancienne page de test archivée, route toujours désactivée côté serveur (`404`).
- `/api/token_usage` : désactivé côté serveur (`404`), malgré l'existence de `logs/token_usage.log`.
- La mémoire conversationnelle est maintenant par cookie de session hashé, plus seulement par IP.
- Les fichiers audio utilisent le `request_id` comme nom, pas un UUID déconnecté.
- Le cleanup audio tourne en thread daemon au démarrage.

## Arborescence

```text
deployment_v1/
├── app/
│   ├── app.py                  # Serveur HTTP, routes, mémoire, rate limits
│   ├── pipeline_grounded.py    # Pipeline actif: generate_grounded + RunPod TTS
│   ├── generate_grounded.py    # Mistral + persona + modules + grounding Wikipedia
│   ├── wikipedia_grounding.py  # Extraction personnes + Wikipedia/Wikidata
│   ├── semantic_router.py      # Routage modules [ROUTING] par embeddings Mistral
│   ├── build_routing_index.py  # Reconstruit data/routing_embeddings.json
│   ├── runpod_tts_client.py    # Client RunPod Serverless /runsync + polling
│   └── cleanup.py              # Suppression TTL des audios
├── web/                        # Interface statique
├── modules/                    # Modules injectables [KEYWORDS]/[ROUTING]/[POSITIONS]
├── prompts/                    # System prompt
├── persona/                    # Persona Foukenstein
├── data/
│   ├── audio/                  # Audios générés
│   ├── memory/                 # Mémoire conversationnelle par session
│   └── routing_embeddings.json # Cache embeddings des modules thématiques
├── logs/
│   ├── web.log                 # stdout/stderr du serveur actif
│   ├── requests/               # Debug JSON par request_id
│   └── token_usage.log         # Logs tokens Mistral
├── env.sh
├── env.secret.sh
└── start.sh
```

## Routes HTTP

### Actives

| Route | Méthode | Fonction |
|---|---:|---|
| `/` | GET | Sert `web/index.html` |
| `/health` | GET | Healthcheck minimal `{"ok": true}` |
| `/api/worker_status` | GET | État RunPod `/health`, avec cache serveur |
| `/api/ask` | POST | Question -> chunks grounded -> RunPod TTS -> audio |
| `/api/tts` | POST | Texte brut -> RunPod TTS, sans Mistral |
| `/audio/<name>.wav` | GET | Lecture audio inline |
| `/download/<name>.wav` | GET | Téléchargement audio forcé |

### Désactivées / Obsolètes À Corriger Plus Tard

| Route | État actuel |
|---|---|
| `/api/generate` | `404`, ancien handler retiré de `app.py` |
| `/test.html` | `404` côté serveur, fichier déplacé dans `Archive/` |
| `/api/token_usage` | `404`, même si le fichier de log existe |

## `/api/ask`

Requête :

```json
{ "question": "Qu'est-ce que le pouvoir pour toi ?" }
```

Réponse typique :

```json
{
  "ok": true,
  "request_id": "1a2b3c4d5e6f7890abcdef1234567890",
  "audio_url": "/audio/1a2b3c4d5e6f7890abcdef1234567890.wav",
  "download_url": "/download/1a2b3c4d5e6f7890abcdef1234567890.wav",
  "format": "wav",
  "chunks": 5
}
```

Garde-fous côté serveur :

- limite de longueur : `MAX_QUESTION_CHARS`, défaut `160`;
- rate limit par IP : `RATE_LIMIT_SECONDS`, défaut `40`;
- concurrence `/api/ask` : `ASK_CONCURRENCY_LIMIT`;
- concurrence globale RunPod TTS : `RUNPOD_TTS_CONCURRENCY_LIMIT`, réglé à `3`;
- cookie `fk_session` pour isoler la mémoire entre utilisateurs partageant une IP;
- extension du timeout TTS si RunPod semble en cold start (`RUNPOD_TTS_TIMEOUT_COLD`, défaut `360`).

## Pipeline Grounded

`pipeline_grounded.py` orchestre deux étapes :

1. `generate_grounded.py` est lancé en subprocess avec la question, l'historique conversationnel et le `REQUEST_ID`.
2. `runpod_tts_client.synthesize()` envoie les chunks à RunPod Serverless et récupère l'audio base64.

Le générateur doit produire un JSON strict :

```json
{"chunks":["chunk 1","chunk 2","chunk 3"]}
```

Si le JSON est invalide, `pipeline_grounded.py` retente jusqu'à 3 fois en ajoutant une instruction stricte.

## Construction Du Prompt

`generate_grounded.py` construit le prompt Mistral dans cet ordre :

1. `prompts/system_foucault.txt`
2. `persona/foukenstein_light.txt`
3. modules sélectionnés depuis `modules/`
4. grounding Wikipedia, si `GROUNDING_ENABLED=1`
5. dernier tour conversationnel, si disponible
6. question courante

Après l'appel Mistral, les chunks sont nettoyés pour le TTS : suppression de markdown, guillemets typographiques, `:`/`;` remplacés par des points, ponctuation finale forcée.

## Modules

Le dossier `modules/` contient actuellement plus de 160 modules `.txt`. Un module peut contenir :

- `[KEYWORDS]` : mots ou expressions qui déclenchent le module;
- `[ROUTING]` : description dense pour routage sémantique;
- `[POSITIONS]` ou `[MODULE]` : texte réellement injecté dans le prompt.

Deux familles sont utilisées :

| Type | Détection | Routage | Exemples |
|---|---|---|---|
| Noms propres | pas de `[ROUTING]` | mots-clés entiers | `latour.txt`, `buterin.txt`, `zuboff.txt` |
| Thématiques | avec `[ROUTING]` | embeddings Mistral + double gate | `technologie.txt`, `ecologie.txt`, `police.txt` |

Ordre de sélection :

1. modules sans `[ROUTING]` par mots-clés, priorité aux noms propres explicitement cités;
2. modules avec `[ROUTING]` pour les slots restants;
3. tiebreaker keyword si le meilleur module sémantique passe le score minimal mais échoue au gap;
4. fallback keyword-only si le routeur sémantique échoue.

Le cap global est `MODULES_MAX`.

## Routage Sémantique

Le routeur est dans `app/semantic_router.py`.

Il embed les sections `[ROUTING]` avec Mistral (`mistral-embed` par défaut), stocke les vecteurs dans `data/routing_embeddings.json`, puis classe les modules par similarité cosinus avec la question.

Sélection prudente :

- `ROUTING_MIN_SCORE`, défaut `0.65`;
- `ROUTING_MIN_GAP`, défaut `0.04`;
- `max_k`, limité ensuite par `MODULES_MAX`.

Modules actuellement indexés :

```text
blockchain.txt
culture.txt
ecologie.txt
identite.txt
medias.txt
police.txt
reseaux_sociaux.txt
tactiques.txt
technologie.txt
```

Reconstruire le cache après modification d'une section `[ROUTING]` :

```bash
cd /workspace/deployment_v1
source env.secret.sh
python app/build_routing_index.py
```

## Grounding Wikipedia

Le grounding est dans `app/wikipedia_grounding.py` et est actif par défaut avec `GROUNDING_ENABLED=1`.

Flux :

1. extraction des personnes citées via Mistral small;
2. fallback regex si l'appel Mistral d'extraction échoue;
3. skip Wikipedia si un module dédié couvre déjà la personne;
4. appel Wikipedia REST FR;
5. validation Wikidata `P31=Q5` pour ne garder que les humains;
6. injection d'un bloc `[FAITS VÉRIFIÉS]`;
7. si la personne n'est pas identifiable, injection d'un bloc qui force un refus propre.

Objectif : réduire les hallucinations biographiques sur les personnes nommées sans rendre la réponse encyclopédique.

Voir `GROUNDING.md` pour le journal technique détaillé.

## RunPod TTS

`app/runpod_tts_client.py` appelle :

```text
POST https://api.runpod.ai/v2/{RUNPOD_SERVERLESS_ENDPOINT_ID}/runsync
```

Payload envoyé :

```json
{
  "input": {
    "chunks": ["chunk 1", "chunk 2"],
    "language": "fr",
    "out_format": "wav"
  }
}
```

Output attendu :

```json
{
  "status": "COMPLETED",
  "output": {
    "audio_base64": "UklGR...",
    "format": "wav",
    "chunks": 2,
    "duration_ms": 12000
  }
}
```

Le client accepte aussi `wav_base64` ou `mp3_base64`. Si RunPod renvoie `IN_QUEUE` ou `IN_PROGRESS`, le client poll `/status/{job_id}` jusqu'à completion ou timeout.

Tous les appels à `runpod_tts_client.synthesize()` partagent un sémaphore global `RUNPOD_TTS_CONCURRENCY_LIMIT`. Avec 3 workers RunPod max, la valeur actuelle est `3`, ce qui empêche `/api/ask` et `/api/tts` de cumuler chacun leur propre plafond et d'envoyer 6 jobs simultanés.

Point runtime observé : les timeouts RunPod peuvent encore provoquer un `BrokenPipeError` si le client HTTP ferme la connexion avant que le serveur écrive la réponse d'erreur.

## Mémoire

La mémoire conversationnelle est stockée dans `data/memory/`.

Le serveur crée un cookie :

```text
fk_session=<uuid hex>; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800
```

La clé de fichier mémoire est `sha256(session_id)[:12]`. Cela évite que plusieurs utilisateurs derrière la même IP partagent le même historique. Le serveur conserve `MEMORY_MAX=2` tours dans le fichier, et injecte seulement le dernier échange dans le prompt.

## Audio Et Cleanup

Les audios sont écrits dans `data/audio/` :

```text
data/audio/<request_id>.wav
```

Un thread daemon lancé au boot supprime les `.wav` et `.mp3` plus vieux que `AUDIO_TTL_HOURS`.

Configuration actuelle typique :

```bash
export OUT_FORMAT="wav"
export AUDIO_TTL_HOURS="48"
export CLEANUP_INTERVAL_MIN="30"
```

## Variables D'Environnement

Obligatoires :

| Variable | Description |
|---|---|
| `MISTRAL_API_KEY` | Clé API Mistral |
| `RUNPOD_API_KEY` | Clé API RunPod |
| `RUNPOD_SERVERLESS_ENDPOINT_ID` | Endpoint RunPod Serverless XTTS |

Principales variables optionnelles :

| Variable | Défaut | Description |
|---|---:|---|
| `WEB_HOST` | `0.0.0.0` | Host HTTP |
| `WEB_PORT` | `9999` | Port HTTP |
| `OUT_FORMAT` | `wav` | `wav` ou `mp3` |
| `MISTRAL_MODEL` | `mistral-large-latest` côté env actuel | Modèle de génération |
| `TEMPERATURE` | `0.70` côté env actuel | Température génération |
| `MODULES_MAX` | `2` | Nombre maximal de modules injectés |
| `MODULES_MAX_CHARS` | `17000` | Troncature par module |
| `RUNPOD_TTS_TIMEOUT` | `180` | Timeout standard TTS |
| `RUNPOD_TTS_TIMEOUT_COLD` | `360` | Timeout si cold start détecté |
| `RUNPOD_TTS_LANGUAGE` | `fr` | Langue XTTS |
| `RUNPOD_TTS_CONCURRENCY_LIMIT` | `3` | Plafond global partagé par `/api/ask` et `/api/tts` pour les appels RunPod |
| `ASK_CONCURRENCY_LIMIT` | `4` par défaut code, `3` dans env actuel | Concurrence `/api/ask` |
| `TTS_CONCURRENCY_LIMIT` | suit `ASK_CONCURRENCY_LIMIT` par défaut | Concurrence `/api/tts` |
| `RATE_LIMIT_SECONDS` | `40` | Rate limit `/api/ask` par IP |
| `TTS_RATE_LIMIT_SECONDS` | `30` | Rate limit `/api/tts` par IP |
| `MAX_QUESTION_CHARS` | `160` | Taille max question/texte |
| `GROUNDING_ENABLED` | `1` | Active le grounding Wikipedia |
| `GROUNDING_EXTRACT_MODEL` | `mistral-small-latest` | Modèle extraction personnes |
| `ROUTING_MIN_SCORE` | `0.65` | Score minimal routage sémantique |
| `ROUTING_MIN_GAP` | `0.04` | Gap minimal top1/top2 |

`app.py` charge `env.sh` puis `env.secret.sh`. Les secrets écrasent les valeurs non secrètes.

## Lancement

```bash
cd /workspace/deployment_v1
bash start.sh
```

Le script :

1. active `/workspace/venv` s'il existe;
2. source `env.sh`;
3. source `env.secret.sh`;
4. vérifie les trois variables obligatoires;
5. lance `python app/app.py`.

Logs du serveur actif :

```text
logs/web.log
```

## Debug Et Observabilité

Chaque génération grounded écrit un JSON dans `logs/requests/`, nommé par `REQUEST_ID` quand disponible :

```json
{
  "pipeline": "grounded",
  "question": "...",
  "modules": {
    "matched_files": [{"file": "technologie.txt"}],
    "semantic": {
      "available": true,
      "ranking": [{"file": "technologie.txt", "score": 0.8123}],
      "selected": [{"file": "technologie.txt", "score": 0.8123}]
    }
  },
  "grounding": {
    "enabled": true,
    "candidates": [{"name": "Hannah Arendt", "status": "grounded"}]
  },
  "tokens": {
    "prompt": 2500,
    "completion": 160,
    "total": 2660
  },
  "output": {
    "chunks_count": 5,
    "chunks": ["..."]
  }
}
```

Le fichier `logs/token_usage.log` reçoit aussi une ligne par appel Mistral principal.

## Sécurité Et Limites

- Les clés API ne sont pas dans le code, mais `env.secret.sh` existe localement et doit rester privé.
- Les noms d'audio sont validés côté GET : pas de `/`, pas de `..`, extensions `.wav`/`.mp3` uniquement.
- CORS autorise `foukenstein.lol`, `www.foukenstein.lol` et les domaines proxy RunPod.
- Le serveur tourne actuellement en Python stdlib, sans reverse proxy applicatif interne dédié.
- Le stockage audio et mémoire est local au volume RunPod; pas de S3/R2/Redis.
- Le routeur sémantique dépend de l'API Mistral embeddings; en cas d'échec, le pipeline revient au keyword-only.
- Wikipedia FR peut rater des personnes étrangères ou des homonymes; les modules dédiés servent de source prioritaire quand ils matchent.

## TODO Connus

- Réactiver `/api/generate` uniquement si un vrai besoin de test texte revient.
- Réactiver `/test.html` uniquement si un vrai besoin de test texte revient.
- Décider quoi faire de `/api/token_usage`.
- Harmoniser les commentaires anciens qui parlent encore du pipeline light.
- Gérer plus proprement les `BrokenPipeError` après timeout côté client.
- Ajouter éventuellement un cache Wikipedia si le volume augmente.
