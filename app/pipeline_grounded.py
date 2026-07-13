#!/usr/bin/env python3
"""
Pipeline orchestrator GROUNDED — Foukenstein light (text-only).

  1. Appelle generate_grounded.py (subprocess) pour obtenir {"chunks":[...]}
  2. Valide le JSON, retente jusqu'à 3 fois
  3. Retourne la liste de chunks (pas de TTS)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
GENERATE_SCRIPT = os.path.join(APP_DIR, "generate_grounded.py")


class PipelineError(RuntimeError):
    """Erreur dans la génération."""


def _run_generate(question: str, conv_history: list[dict],
                  request_id: str | None = None) -> str:
    env = os.environ.copy()
    env["CONV_HISTORY"] = json.dumps(conv_history or [], ensure_ascii=False)
    env.setdefault("PYTHONUNBUFFERED", "1")
    if request_id:
        env["REQUEST_ID"] = request_id

    try:
        cp = subprocess.run(
            [sys.executable, GENERATE_SCRIPT, question],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
    except subprocess.TimeoutExpired as e:
        raise PipelineError(f"generate_grounded timeout: {e}") from e
    except Exception as e:
        raise PipelineError(f"generate_grounded failed to start: {e}") from e

    if cp.returncode != 0:
        raise PipelineError(
            f"generate_grounded exit={cp.returncode} stderr={(cp.stderr or '')[-500:]}"
        )

    return (cp.stdout or "").strip()


def _parse_chunks(raw: str) -> list[str]:
    if not raw:
        raise PipelineError("empty output from generate_grounded")
    r = raw
    if r.startswith("```"):
        r = r.strip("`")
        if "\n" in r:
            r = r.split("\n", 1)[1]
    try:
        obj = json.loads(r)
    except Exception as e:
        raise PipelineError(f"invalid JSON: {e} raw={raw[:200]}") from e

    chunks = obj.get("chunks") if isinstance(obj, dict) else None
    if not isinstance(chunks, list) or not chunks:
        raise PipelineError(f"no chunks in JSON: {raw[:200]}")

    clean: list[str] = []
    for c in chunks:
        s = str(c).strip()
        if s:
            clean.append(s)
    if not clean:
        raise PipelineError("all chunks empty after cleanup")
    return clean


def generate_chunks(question: str, conv_history: list[dict] | None = None,
                    request_id: str | None = None) -> list[str]:
    """
    Génère des chunks texte via Mistral, avec grounding Wikipedia.
    Jusqu'à 3 tentatives.
    """
    conv_history = conv_history or []
    last_error: Exception | None = None

    for _ in range(3):
        try:
            raw = _run_generate(question, conv_history, request_id=request_id)
            return _parse_chunks(raw)
        except PipelineError as e:
            last_error = e
            continue

    raise PipelineError(f"generate_chunks failed after 3 attempts: {last_error}")


def run(question: str, conv_history: list[dict] | None = None,
        request_id: str | None = None) -> list[str]:
    """
    Pipeline complet : question → chunks texte (avec grounding).
    """
    return generate_chunks(question, conv_history, request_id=request_id)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: pipeline_grounded.py \"ta question\"", file=sys.stderr)
        sys.exit(1)
    q = sys.argv[1]
    try:
        chks = run(q)
        print(json.dumps({"chunks": chks}, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
