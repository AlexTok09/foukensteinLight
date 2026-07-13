#!/usr/bin/env python3
"""
CLI pour (re)construire le cache d'embeddings de routage.

Idempotent : ne re-embed que les modules dont la section [ROUTING] a changé.
À relancer à la main après toute modification d'un [ROUTING] dans
/workspace/deployment_v1/modules/.

Usage :
    source /workspace/deployment_v1/env.secret.sh
    python /workspace/deployment_v1/app/build_routing_index.py
"""
import os
import sys

# S'assurer que le module semantic_router est importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from semantic_router import (
    build_or_refresh_index,
    CACHE_PATH,
    MODULES_DIR,
    RoutingError,
)


def main() -> int:
    print(f"[build] modules_dir = {MODULES_DIR}")
    print(f"[build] cache_path  = {CACHE_PATH}")

    try:
        cache = build_or_refresh_index(verbose=True)
    except RoutingError as e:
        print(f"[build] ERREUR : {e}", file=sys.stderr)
        return 1

    print(f"\n[build] {len(cache)} modules indexés :")
    for fname in sorted(cache):
        emb = cache[fname].get("embedding") or []
        print(f"  - {fname}  (dim={len(emb)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
