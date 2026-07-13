#!/usr/bin/env python3
"""
Serveur HTTP local — interface de test XTTS Foukenstein.
GET  /           → interface.html
POST /synthesize → {chunks, params…} → {audio_base64, duration_ms, chunks_log}

Lancement :
    /workspace/venv/bin/python /workspace/deployment_v1/runpod_handler/server_tts.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("FTCKPT",       "/workspace/xtts/best_model_19875.pth")
os.environ.setdefault("ORIG",         "/workspace/xtts/XTTS_v2.0_original_model_files")
os.environ.setdefault("SPEAKERS_PTH", "/workspace/xtts/XTTS_v2.0_original_model_files/speakers_xtts.pth")
os.environ.setdefault("SPEAKER_WAV",  "/workspace/xtts/test_speaker.wav")
os.environ.setdefault("LANG",         "fr")

if "runpod" not in sys.modules:
    fake = types.ModuleType("runpod")
    fake_serverless = types.ModuleType("runpod.serverless")
    fake_serverless.start = lambda *a, **kw: None
    fake.serverless = fake_serverless
    sys.modules["runpod"] = fake
    sys.modules["runpod.serverless"] = fake_serverless

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

print("[server] Chargement du modele XTTS...", flush=True)
t0 = time.time()
import handler
print(f"[server] Modele pret en {time.time()-t0:.1f}s — ecoute sur :8080", flush=True)

HTML_PATH = os.path.join(HERE, "interface.html")


def _patch(body: dict) -> None:
    handler.XFADE_MS        = int(body.get("xfade_ms",        320))
    handler.EDGE_FADE_MS    = int(body.get("edge_fade_ms",    80))
    handler.PAUSE_MS        = int(body.get("pause_ms",        160))
    handler.MICRO_PAUSE_MS  = int(body.get("micro_pause_ms",  40))
    handler.TAIL_SILENCE_MS = int(body.get("tail_silence_ms", 700))
    handler.FADE_OUT_MS     = int(body.get("fade_out_ms",     80))
    handler.FADE_SAMPLES    = int(handler.SR * handler.XFADE_MS / 1000)
    handler.TEMPERATURE        = float(body.get("temperature",        0.65))
    handler.LENGTH_PENALTY     = float(body.get("length_penalty",     1.0))
    handler.REPETITION_PENALTY = float(body.get("repetition_penalty", 2.0))
    handler.TOP_K              = int(body.get("top_k", 50))
    handler.TOP_P              = float(body.get("top_p", 0.85))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence default access log

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        if self.path in ("/", "/interface.html"):
            try:
                with open(HTML_PATH, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self._cors()
                self.end_headers()
                self.wfile.write(data)
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"interface.html not found")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/synthesize":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except Exception as e:
            self._json({"error": f"invalid JSON: {e}"}, 400)
            return

        chunks = body.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            self._json({"error": "missing or empty 'chunks'"}, 400)
            return

        _patch(body)
        event = {"input": {"chunks": chunks, "language": body.get("language", "fr")}}

        t1  = time.time()
        try:
            out = handler.handler(event)
        except Exception as e:
            self._json({"error": f"handler failed: {e}"}, 500)
            return
        elapsed = int((time.time() - t1) * 1000)
        out["elapsed_ms"] = elapsed
        print(f"[server] /synthesize — {len(chunks)} chunks — {elapsed}ms", flush=True)
        self._json(out)

    def _json(self, obj: dict, code: int = 200) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[server] Listening on http://0.0.0.0:{port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] Bye", flush=True)
