#!/usr/bin/env python3
"""
Routage sémantique — Foukenstein deployment_v1.

Principe
--------
Chaque module thématique de MODULES_DIR contient une section [ROUTING]
(description dense du domaine couvert). On embed cette section via l'API
Mistral (modèle mistral-embed), on cache le résultat sur disque, et à chaque
question utilisateur on calcule la similarité cosinus pour classer les modules.

Seuls les modules *avec* [ROUTING] entrent dans ce routeur. Les modules sans
[ROUTING] (~60 modules de noms propres : philosophes, penseurs) restent gérés
par la logique keywords du pipeline grounded.

API publique
------------
- build_or_refresh_index(modules_dir=...) -> dict
    Lit tous les .txt du dossier, (re)calcule les embeddings manquants
    ou obsolètes (hash du [ROUTING] changé), persiste le cache.

- rank(question, threshold=..., top_k=...) -> list[(fname, score)]
    Retourne les modules triés par similarité décroissante, filtrés par
    seuil. `top_k=None` → tous les modules au-dessus du seuil.

- load_cache() -> dict
    Charge le cache depuis disque sans toucher à l'API.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request

EMBED_MODEL    = os.environ.get("MISTRAL_EMBED_MODEL", "mistral-embed")
MODULES_DIR    = os.environ.get("MODULES_DIR", "/workspace/deployment_v1/modules")
CACHE_PATH     = os.environ.get(
    "ROUTING_CACHE_PATH",
    "/workspace/deployment_v1/data/routing_embeddings.json",
)
# Les embeddings Mistral ont un plancher cosinus naturellement haut (~0.70 même
# pour des paires hors-sujet). Le signal utile est :
#   1. un plancher absolu — en dessous, on ignore
#   2. un écart (gap) top1 - top2 — un vrai match décolle du bruit
# Ces seuils ont été calibrés empiriquement sur une dizaine de questions.
# Ajustables via env.
MIN_SCORE      = float(os.environ.get("ROUTING_MIN_SCORE", "0.65"))
MIN_GAP        = float(os.environ.get("ROUTING_MIN_GAP", "0.04"))
EMBED_TIMEOUT  = int(os.environ.get("ROUTING_EMBED_TIMEOUT", "30"))


class RoutingError(RuntimeError):
    """Erreur de routage (API indisponible, cache corrompu, etc.)."""


# ── Utilitaires ───────────────────────────────────────────────────────────────

def _extract_routing(content: str) -> str:
    m = re.search(r"\[ROUTING\]\s*\n(.*?)(?:\n\[|\Z)", content,
                  re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    na  = 0.0
    nb  = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na  += x * x
        nb  += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ── API Mistral ───────────────────────────────────────────────────────────────

def _embed(texts: list[str]) -> list[list[float]]:
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise RoutingError("MISTRAL_API_KEY absent")
    if not texts:
        return []

    payload = json.dumps({"model": EMBED_MODEL, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.mistral.ai/v1/embeddings",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RoutingError(f"embed HTTP {e.code}: {body[:300]}") from e
    except Exception as e:
        raise RoutingError(f"embed request failed: {e}") from e

    items = data.get("data")
    if not isinstance(items, list) or len(items) != len(texts):
        raise RoutingError(f"embed response malformed: {str(data)[:200]}")
    return [item["embedding"] for item in items]


# ── Cache disque ──────────────────────────────────────────────────────────────

def load_cache() -> dict:
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[router] cache read failed: {e}", file=sys.stderr)
        return {}


def _save_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, CACHE_PATH)


# ── Construction / rafraîchissement de l'index ───────────────────────────────

def build_or_refresh_index(modules_dir: str = MODULES_DIR,
                           verbose: bool = False) -> dict:
    """
    Parcourt modules_dir, embed tout [ROUTING] manquant ou dont le hash a
    changé, persiste le cache. Retourne le cache à jour.
    """
    if not os.path.isdir(modules_dir):
        raise RoutingError(f"modules_dir introuvable: {modules_dir}")

    cache = load_cache()
    current_files: set[str] = set()
    to_embed: list[str] = []
    meta: list[tuple[str, str]] = []  # (fname, hash)

    for fname in sorted(os.listdir(modules_dir)):
        if not fname.endswith(".txt"):
            continue
        path = os.path.join(modules_dir, fname)
        try:
            content = open(path, encoding="utf-8").read()
        except Exception:
            continue

        routing = _extract_routing(content)
        if not routing:
            continue

        current_files.add(fname)
        h = _sha(routing)
        entry = cache.get(fname)
        if entry and entry.get("hash") == h and entry.get("embedding"):
            continue  # déjà à jour
        to_embed.append(routing)
        meta.append((fname, h))
        if verbose:
            reason = "nouveau" if not entry else "hash changé"
            print(f"[router] à embed : {fname} ({reason})", file=sys.stderr)

    if to_embed:
        embeddings = _embed(to_embed)
        for (fname, h), emb in zip(meta, embeddings):
            cache[fname] = {"hash": h, "embedding": emb}
        if verbose:
            print(f"[router] {len(to_embed)} embeddings mis à jour",
                  file=sys.stderr)

    # purge : modules supprimés du dossier
    removed = [k for k in cache if k not in current_files]
    for k in removed:
        del cache[k]
        if verbose:
            print(f"[router] purge : {k}", file=sys.stderr)

    _save_cache(cache)
    return cache


# ── Routage en requête ────────────────────────────────────────────────────────

def rank(question: str,
         cache: dict | None = None) -> list[tuple[str, float]]:
    """
    Classe TOUS les modules [ROUTING] par similarité à la question, sans filtre.
    Utile pour debug / inspection. La sélection applicative se fait avec select().
    """
    cache = cache if cache is not None else load_cache()
    if not cache:
        return []

    q_emb = _embed([question])[0]
    scored: list[tuple[str, float]] = []
    for fname, entry in cache.items():
        emb = entry.get("embedding")
        if not emb:
            continue
        scored.append((fname, _cosine(q_emb, emb)))
    scored.sort(key=lambda x: -x[1])
    return scored


def select(question: str,
           max_k: int = 2,
           min_score: float = MIN_SCORE,
           min_gap: float = MIN_GAP,
           cache: dict | None = None,
           _precomputed: list[tuple[str, float]] | None = None
           ) -> list[tuple[str, float]]:
    """
    Sélection prudente pour injection : double-gate (plancher + écart).

    Règle :
        - top_i est sélectionné SI score[i] >= min_score
          ET l'écart score[i] - score[i+1] >= min_gap
          (avec score[last+1] = -inf, donc le dernier rang testé passe toujours
           s'il satisfait le plancher).
        - Dès qu'un rang échoue, on s'arrête (pas de trou dans la sélection).
        - Limite dure : max_k modules.

    Rationale : le plancher évite les non-sens totaux, l'écart garantit que
    le module retenu se détache franchement des suivants — pas juste
    marginalement meilleur.

    `_precomputed` : si déjà calculé par rank(), évite un second appel embed.
    """
    ranked = _precomputed if _precomputed is not None else rank(question, cache)
    if not ranked:
        return []

    selected: list[tuple[str, float]] = []
    for i, (fname, score) in enumerate(ranked[:max_k]):
        if score < min_score:
            break
        next_score = ranked[i + 1][1] if i + 1 < len(ranked) else float("-inf")
        if score - next_score < min_gap:
            break
        selected.append((fname, score))
    return selected


if __name__ == "__main__":
    # Usage : python semantic_router.py "ta question"
    if len(sys.argv) < 2:
        print("Usage: semantic_router.py \"ta question\"", file=sys.stderr)
        sys.exit(1)
    cache = load_cache()
    if not cache:
        print("[router] cache vide — lance d'abord build_routing_index.py",
              file=sys.stderr)
        sys.exit(1)
    results = rank(sys.argv[1], cache=cache)
    print("── Ranking complet ──")
    for fname, score in results:
        print(f"  {score:.4f}  {fname}")
    print(f"\n── Sélection (min_score={MIN_SCORE}, min_gap={MIN_GAP}) ──")
    chosen = select(sys.argv[1], cache=cache, _precomputed=results)
    if not chosen:
        print("  (aucun)")
    else:
        for fname, score in chosen:
            print(f"  {score:.4f}  {fname}")
