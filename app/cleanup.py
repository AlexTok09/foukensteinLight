#!/usr/bin/env python3
"""
TTL cleanup — Foukenstein deployment_v1.

Thread de fond qui supprime les fichiers audio plus vieux que TTL heures.
Démarré au boot de app.py.
"""
from __future__ import annotations

import os
import threading
import time


def _sweep(directory: str, ttl_seconds: float, extensions: tuple[str, ...]) -> int:
    if not os.path.isdir(directory):
        return 0
    now = time.time()
    removed = 0
    try:
        entries = os.listdir(directory)
    except Exception:
        return 0
    for name in entries:
        if not name.lower().endswith(extensions):
            continue
        path = os.path.join(directory, name)
        try:
            age = now - os.path.getmtime(path)
        except Exception:
            continue
        if age > ttl_seconds:
            try:
                os.remove(path)
                removed += 1
            except Exception:
                pass
    return removed


def start_cleanup_thread(
    directory: str,
    ttl_hours: float = 24.0,
    interval_minutes: float = 30.0,
    extensions: tuple[str, ...] = (".wav", ".mp3"),
) -> threading.Thread:
    ttl_seconds = ttl_hours * 3600.0
    interval_seconds = max(30.0, interval_minutes * 60.0)

    def _loop():
        while True:
            try:
                n = _sweep(directory, ttl_seconds, extensions)
                if n:
                    print(f"[cleanup] removed {n} file(s) > {ttl_hours}h in {directory}", flush=True)
            except Exception as e:
                print(f"[cleanup] error: {e}", flush=True)
            time.sleep(interval_seconds)

    t = threading.Thread(target=_loop, name="audio-cleanup", daemon=True)
    t.start()
    print(
        f"[cleanup] started — dir={directory} ttl={ttl_hours}h interval={interval_minutes}min",
        flush=True,
    )
    return t
