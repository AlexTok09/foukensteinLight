#!/usr/bin/env python3
"""
Runpod Serverless TTS client — Foukenstein deployment_v1.

Appelle un endpoint Runpod Serverless (GPU XTTS) en runsync et retourne
les bytes WAV du fichier synthétisé.

Contrat attendu côté handler serverless
----------------------------------------
Input (ce que le client envoie) :
    {
      "input": {
        "chunks":          ["chunk 1 ...", "chunk 2 ...", ...],
        "voice_settings":  { ... optionnel ... },
        "language":        "fr",
        "out_format":      "wav"          # "wav" | "mp3"
      }
    }

Output attendu (réponse Runpod /runsync) :
    {
      "status": "COMPLETED",
      "output": {
        "audio_base64": "UklGR...",       # base64 du fichier audio complet
        "format":       "wav",            # "wav" | "mp3"
        "chunks":       <int>,            # nombre de chunks synthétisés
        "duration_ms":  <int>             # optionnel
      }
    }

Variables d'environnement requises
----------------------------------
    RUNPOD_SERVERLESS_ENDPOINT_ID   ID de l'endpoint serverless (ex: abc123xyz)
    RUNPOD_API_KEY                  clé API Runpod (Bearer)
    RUNPOD_TTS_TIMEOUT              timeout runsync en secondes (défaut 180)
    RUNPOD_TTS_LANGUAGE             langue (défaut "fr")

Aucune clé n'est stockée en clair : tout passe par l'environnement.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any


class RunpodTTSError(RuntimeError):
    """Erreur de synthèse côté Runpod Serverless."""


class RunpodTTSBusy(RunpodTTSError):
    """Trop d'appels Runpod TTS sont déjà en cours."""


_tts_slots_lock = threading.Lock()
_tts_slots: threading.BoundedSemaphore | None = None


def _get_tts_slots() -> threading.BoundedSemaphore:
    global _tts_slots
    if _tts_slots is not None:
        return _tts_slots
    with _tts_slots_lock:
        if _tts_slots is None:
            try:
                limit = int(os.environ.get("RUNPOD_TTS_CONCURRENCY_LIMIT", "3"))
            except ValueError:
                limit = 3
            _tts_slots = threading.BoundedSemaphore(max(1, limit))
        return _tts_slots


def _endpoint_id() -> str:
    eid = os.environ.get("RUNPOD_SERVERLESS_ENDPOINT_ID", "").strip()
    if not eid:
        raise RunpodTTSError("RUNPOD_SERVERLESS_ENDPOINT_ID not set")
    return eid


def _endpoint_url() -> str:
    return f"https://api.runpod.ai/v2/{_endpoint_id()}/runsync"


def _status_url(job_id: str) -> str:
    return f"https://api.runpod.ai/v2/{_endpoint_id()}/status/{job_id}"


def _http_get_json(url: str, timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {_api_key()}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RunpodTTSError(f"Runpod status HTTP {e.code}: {err_body[:500]}") from e
    except urllib.error.URLError as e:
        raise RunpodTTSError(f"Runpod status unreachable: {e}") from e


def _poll_until_done(job_id: str, deadline: float, poll_every: float = 3.0) -> dict[str, Any]:
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise RunpodTTSError(f"Runpod job {job_id} polling timeout")
        data = _http_get_json(_status_url(job_id), timeout=min(30.0, max(5.0, remaining)))
        status = str(data.get("status", "")).upper()
        if status == "COMPLETED":
            return data
        if status in ("FAILED", "CANCELLED", "TIMED_OUT"):
            raise RunpodTTSError(
                f"Runpod job {job_id} status={status} error={data.get('error') or data.get('output')}"
            )
        time.sleep(min(poll_every, max(0.5, deadline - time.time())))


def _api_key() -> str:
    key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not key:
        raise RunpodTTSError("RUNPOD_API_KEY not set")
    return key


def synthesize(
    chunks: list[str],
    voice_settings: dict[str, Any] | None = None,
    out_format: str = "wav",
    language: str | None = None,
    timeout: float | None = None,
) -> tuple[bytes, str]:
    """
    Synthétise une liste de chunks texte en un fichier audio complet
    via l'endpoint Runpod Serverless XTTS.

    Returns:
        (audio_bytes, format)  où format ∈ {"wav", "mp3"}.
    """
    slots = _get_tts_slots()
    if not slots.acquire(blocking=False):
        raise RunpodTTSBusy("runpod_tts_busy")

    try:
        return _synthesize_locked(
            chunks,
            voice_settings=voice_settings,
            out_format=out_format,
            language=language,
            timeout=timeout,
        )
    finally:
        slots.release()


def _synthesize_locked(
    chunks: list[str],
    voice_settings: dict[str, Any] | None = None,
    out_format: str = "wav",
    language: str | None = None,
    timeout: float | None = None,
) -> tuple[bytes, str]:
    """Implémentation appelée uniquement après acquisition du slot global."""
    chunks = [str(c).strip() for c in (chunks or []) if str(c).strip()]
    if not chunks:
        raise RunpodTTSError("empty chunks")

    if out_format not in ("wav", "mp3"):
        raise RunpodTTSError(f"unsupported out_format: {out_format}")

    if timeout is None:
        timeout = float(os.environ.get("RUNPOD_TTS_TIMEOUT", "180"))
    if language is None:
        language = os.environ.get("RUNPOD_TTS_LANGUAGE", "fr")

    payload: dict[str, Any] = {
        "input": {
            "chunks":     chunks,
            "language":   language,
            "out_format": out_format,
        }
    }
    if voice_settings:
        payload["input"]["voice_settings"] = voice_settings

    deadline = time.time() + timeout

    req = urllib.request.Request(
        _endpoint_url(),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RunpodTTSError(f"Runpod HTTP {e.code}: {err_body[:500]}") from e
    except urllib.error.URLError as e:
        raise RunpodTTSError(f"Runpod unreachable: {e}") from e

    try:
        data = json.loads(body)
    except Exception as e:
        raise RunpodTTSError(f"Runpod returned non-JSON: {body[:200]}") from e

    status = str(data.get("status", "")).upper()
    if status in ("IN_QUEUE", "IN_PROGRESS"):
        job_id = data.get("id") or ""
        if not job_id:
            raise RunpodTTSError(f"Runpod {status} without job id: {body[:200]}")
        data = _poll_until_done(job_id, deadline=deadline)
        status = str(data.get("status", "")).upper()
    if status and status != "COMPLETED":
        raise RunpodTTSError(
            f"Runpod job status={status} error={data.get('error') or data.get('output')}"
        )

    output = data.get("output") or {}
    if not isinstance(output, dict):
        raise RunpodTTSError(f"Runpod output is not an object: {output!r}")

    b64 = output.get("audio_base64") or output.get("wav_base64") or output.get("mp3_base64")
    if not b64:
        raise RunpodTTSError(f"Runpod output missing audio_base64: keys={list(output.keys())}")

    fmt = str(output.get("format") or out_format).lower()
    if fmt not in ("wav", "mp3"):
        fmt = out_format

    try:
        audio_bytes = base64.b64decode(b64)
    except Exception as e:
        raise RunpodTTSError(f"cannot decode audio_base64: {e}") from e

    if not audio_bytes:
        raise RunpodTTSError("decoded audio is empty")

    return audio_bytes, fmt


if __name__ == "__main__":
    import sys
    test_chunks = [
        "Ceci est un test de synthèse vocale.",
        "Le serveur Runpod doit renvoyer un fichier audio.",
    ]
    try:
        audio, fmt = synthesize(test_chunks)
        out = f"/tmp/runpod_tts_test.{fmt}"
        with open(out, "wb") as f:
            f.write(audio)
        print(f"OK {len(audio)} bytes → {out}")
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
