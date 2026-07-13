#!/usr/bin/env python3
"""
Test local du handler XTTS serverless (exécuté dans le pod, sans Runpod).

Bypass runpod.serverless.start() en stubant le module `runpod` avant import.
Pointe les env vars vers les vrais fichiers locaux du pod (pas /runpod-volume).

Lancement :
    /workspace/venv/bin/python /workspace/deployment_v1/runpod_handler/test_local.py
"""
from __future__ import annotations

import base64
import os
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))

# ── Env vars pointant sur les assets LOCAUX du pod (pas /runpod-volume) ──────
os.environ["FTCKPT"] = (
    "/workspace/runs/run_20260210_110029_ft_v2_like_ref_mixed_25ep/run/training/"
    "GPT_XTTS_FT-February-10-2026_11+02AM-0000000/best_model_19875.pth"
)
os.environ["ORIG"] = (
    "/workspace/runs/run_20260210_110029_ft_v2_like_ref_mixed_25ep/run/training/"
    "XTTS_v2.0_original_model_files"
)
os.environ["SPEAKERS_PTH"] = os.path.join(os.environ["ORIG"], "speakers_xtts.pth")
os.environ["SPEAKER_WAV"] = "/workspace/test_speaker.wav"
os.environ["LANG"] = "fr"

# ── Stub du module `runpod` (pas installé dans le venv — on s'en fiche, on
#    ne veut pas lancer serverless.start(), juste appeler handler() direct) ─
if "runpod" not in sys.modules:
    fake = types.ModuleType("runpod")
    fake_serverless = types.ModuleType("runpod.serverless")
    fake_serverless.start = lambda *a, **kw: None  # no-op
    fake.serverless = fake_serverless
    sys.modules["runpod"] = fake
    sys.modules["runpod.serverless"] = fake_serverless

# ── Import du handler (charge le modèle au passage — ~15-30s sur GPU) ───────
sys.path.insert(0, HERE)
print("[test] importing handler.py (loads XTTS model)...", flush=True)
t0 = time.time()
import handler  # noqa: E402
print(f"[test] handler imported in {time.time()-t0:.1f}s", flush=True)

# ── Appel handler() avec un event factice ───────────────────────────────────
event = {
    "input": {
        "chunks":   ["Bonjour, ceci est un test local du handler.",
                     "Deuxième phrase pour valider le stitching audio."],
        "language": "fr",
    }
}

print("[test] calling handler(event)...", flush=True)
t1 = time.time()
out = handler.handler(event)
print(f"[test] handler returned in {time.time()-t1:.1f}s", flush=True)

if "error" in out:
    print(f"[test] KO — handler returned error: {out['error']}")
    sys.exit(1)

b64 = out.get("audio_base64")
if not b64:
    print(f"[test] KO — no audio_base64 in output: {out}")
    sys.exit(1)

audio = base64.b64decode(b64)
out_path = os.path.join(HERE, "_local_test.wav")
with open(out_path, "wb") as f:
    f.write(audio)

print("")
print("=== RESULT ===")
print(f"  format      : {out.get('format')}")
print(f"  chunks      : {out.get('chunks')}")
print(f"  duration_ms : {out.get('duration_ms')}")
print(f"  elapsed_ms  : {out.get('elapsed_ms')}")
print(f"  wav file    : {out_path}")
print(f"  wav bytes   : {len(audio)}")
print("  OK")
