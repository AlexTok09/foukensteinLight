#!/usr/bin/env python3
"""
Web server — Foukenstein light (text-only).

Serveur HTTP autonome :
  * route /api/ask vers pipeline_grounded.run() (Mistral + grounding Wikipedia)
  * réponse JSON: {ok, chunks: [...]}  — aucun audio, aucun RunPod

Pipeline :
    Client → app.py → pipeline_grounded.py → generate_grounded.py (Mistral)
                              ↓
                       { chunks: [...] }
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# ── Imports locaux (app/) ─────────────────────────────────────────────────────
APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import pipeline_grounded  # noqa: E402


# ── Env loader (pour usage stand-alone) ───────────────────────────────────────
def _load_env_file(path: str, override: bool = False) -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if override or (k not in os.environ):
                os.environ[k] = v


DEPLOY_ROOT = os.path.abspath(os.path.join(APP_DIR, ".."))
_load_env_file(os.path.join(DEPLOY_ROOT, "env.sh"), override=False)
_load_env_file(os.path.join(DEPLOY_ROOT, "env.secret.sh"), override=True)


# ── Auto-defaults portables (tout dérivé de DEPLOY_ROOT) ──────────────────────
os.environ.setdefault("SYSTEM_PROMPT_PATH",
                      os.path.join(DEPLOY_ROOT, "prompts", "system_foucault.txt"))
os.environ.setdefault("PERSONA_PATH",
                      os.path.join(DEPLOY_ROOT, "persona", "foukenstein_light.txt"))
os.environ.setdefault("MODULES_DIR",
                      os.path.join(DEPLOY_ROOT, "modules"))
os.environ.setdefault("DEBUG_LOG_DIR",
                      os.path.join(DEPLOY_ROOT, "logs", "requests"))
os.environ.setdefault("TOKEN_LOG",
                      os.path.join(DEPLOY_ROOT, "logs", "token_usage.log"))


# ── Config ────────────────────────────────────────────────────────────────────
SITE_PUBLIC = os.environ.get("WEB_PUBLIC_DIR", os.path.join(DEPLOY_ROOT, "web"))
MEMORY_DIR  = os.environ.get("MEMORY_DIR",     os.path.join(DEPLOY_ROOT, "data", "memory"))

HOST = os.environ.get("WEB_HOST", "0.0.0.0")
# Render/Fly/Heroku injectent PORT ; fallback WEB_PORT puis 9999.
PORT = int(os.environ.get("PORT") or os.environ.get("WEB_PORT") or "9999")

MEMORY_MAX = 2

RATE_LIMIT_SECONDS = int(os.environ.get("RATE_LIMIT_SECONDS", "40"))
MAX_QUESTION_CHARS = int(os.environ.get("MAX_QUESTION_CHARS", "160"))
ASK_CONCURRENCY_LIMIT = int(os.environ.get("ASK_CONCURRENCY_LIMIT", "4"))

ALLOWED_ORIGINS = {
    "https://foukenstein.lol",
    "https://www.foukenstein.lol",
    "https://foukenstein.com",
    "https://www.foukenstein.com",
}
DEFAULT_ORIGIN = "https://foukenstein.lol"

os.makedirs(MEMORY_DIR, exist_ok=True)

# ── Rate limiting par IP ─────────────────────────────────────────────────────
_rate_lock = threading.Lock()
_rate_last: dict[str, float] = {}
_ask_slots = threading.BoundedSemaphore(max(1, ASK_CONCURRENCY_LIMIT))


def _check_rate_limit(ip_hash: str, consume: bool = True) -> float:
    now = time.time()
    with _rate_lock:
        last = _rate_last.get(ip_hash, 0.0)
        remaining = RATE_LIMIT_SECONDS - (now - last)
        if remaining > 0:
            return remaining
        if consume:
            _rate_last[ip_hash] = now
        return 0.0


# ── Helpers mémoire ───────────────────────────────────────────────────────────
def _get_client_ip(handler) -> str:
    forwarded = handler.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return handler.client_address[0]


def _ip_key(ip: str) -> str:
    return hashlib.sha256(ip.encode()).hexdigest()[:12]


def _load_memory(ip_key: str) -> list:
    path = os.path.join(MEMORY_DIR, f"{ip_key}.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_memory(ip_key: str, history: list) -> None:
    path = os.path.join(MEMORY_DIR, f"{ip_key}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(history[-MEMORY_MAX:], f, ensure_ascii=False)
    except Exception:
        pass


# ── Session par cookie (isole la mémoire entre users partageant une IP) ──────
SESSION_COOKIE = "fk_session"
SESSION_TTL    = 86400 * 7


def _get_session_cookie(handler) -> str | None:
    raw = handler.headers.get("Cookie", "")
    if not raw:
        return None
    for part in raw.split(";"):
        part = part.strip()
        if part.startswith(SESSION_COOKIE + "="):
            val = part[len(SESSION_COOKIE) + 1:].strip()
            if len(val) == 32 and all(c in "0123456789abcdef" for c in val):
                return val
    return None


def _session_key(handler) -> tuple[str, str | None]:
    sid = _get_session_cookie(handler)
    set_cookie = None
    if not sid:
        sid = uuid.uuid4().hex
        set_cookie = (f"{SESSION_COOKIE}={sid}; Path=/; HttpOnly; "
                      f"SameSite=Lax; Max-Age={SESSION_TTL}")
    return hashlib.sha256(sid.encode()).hexdigest()[:12], set_cookie


# ── MIME map ──────────────────────────────────────────────────────────────────
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js":   "text/javascript",
    ".css":  "text/css",
    ".glb":  "model/gltf-binary",
    ".ico":  "image/x-icon",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".svg":  "image/svg+xml",
}


def read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


# ── HTTP handler ──────────────────────────────────────────────────────────────
class H(BaseHTTPRequestHandler):
    def _cors_origin(self) -> str:
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            return origin
        return DEFAULT_ORIGIN

    def _json(self, code: int, obj, extra_headers=None) -> None:
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", self._cors_origin())
        self.send_header("Vary", "Origin")
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self._cors_origin())
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── GET ───────────────────────────────────────────────────────────────────
    def do_GET(self):
        p = urlparse(self.path).path

        if p == "/health":
            return self._json(200, {"ok": True})

        if p == "/":
            p = "/index.html"
        if ".." in p:
            return self._json(403, {"ok": False, "error": "forbidden"})

        file_path = os.path.join(SITE_PUBLIC, p.lstrip("/"))
        if not os.path.isfile(file_path):
            return self._json(404, {"ok": False, "error": "not found"})

        ext  = os.path.splitext(file_path)[1].lower()
        mime = MIME.get(ext, "application/octet-stream")
        try:
            data = read_file(file_path)
        except Exception as e:
            return self._json(500, {"ok": False, "error": str(e)})

        self.send_response(200)
        self.send_header("Content-Type", mime)
        if "text/html" in mime:
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ── POST ──────────────────────────────────────────────────────────────────
    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/ask":
            return self._handle_ask()
        return self._json(404, {"ok": False, "error": "not found"})

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw    = self.rfile.read(length).decode("utf-8") if length else ""
        return json.loads(raw) if raw else {}

    def _handle_ask(self):
        """Pipeline complet : question → chunks texte (Mistral + grounding)."""
        try:
            obj      = self._read_json_body()
            question = (obj.get("question") or "").strip()
        except Exception as e:
            return self._json(400, {"ok": False, "error": f"bad json: {e}"})

        if not question:
            return self._json(400, {"ok": False, "error": "missing question"})

        if len(question) > MAX_QUESTION_CHARS:
            return self._json(400, {
                "ok": False,
                "error": "question_too_long",
                "max_chars": MAX_QUESTION_CHARS,
            })

        ip_key = _ip_key(_get_client_ip(self))
        wait = _check_rate_limit(ip_key, consume=False)
        if wait > 0:
            return self._json(429, {
                "ok": False,
                "error": "rate_limit",
                "retry_after": round(wait),
            })

        if not _ask_slots.acquire(blocking=False):
            return self._json(503, {
                "ok": False,
                "error": "busy",
                "message": "Foukenstein est très sollicité. Essayez plus tard.",
            })

        cookie_headers = None
        request_id = uuid.uuid4().hex

        try:
            _check_rate_limit(ip_key, consume=True)

            sess_key, set_cookie = _session_key(self)
            cookie_headers = [("Set-Cookie", set_cookie)] if set_cookie else None

            history = _load_memory(sess_key)
            last = history[-1] if history else None
            if last and last.get("q") and last.get("a"):
                conv = [
                    {"role": "user",      "content": last["q"]},
                    {"role": "assistant", "content": last["a"]},
                ]
            else:
                conv = []

            chunks = pipeline_grounded.run(
                question, conv_history=conv, request_id=request_id,
            )
        except pipeline_grounded.PipelineError as e:
            return self._json(502, {
                "ok": False,
                "error": f"pipeline: {e}",
                "request_id": request_id,
            }, extra_headers=cookie_headers)
        except Exception as e:
            return self._json(500, {
                "ok": False,
                "error": f"internal: {e}",
                "request_id": request_id,
            }, extra_headers=cookie_headers)
        finally:
            _ask_slots.release()

        try:
            response_text = " ".join(chunks)
            history.append({"q": question, "a": response_text, "ts": int(time.time())})
            _save_memory(sess_key, history)
        except Exception:
            pass

        return self._json(200, {
            "ok":         True,
            "request_id": request_id,
            "chunks":     chunks,
        }, extra_headers=cookie_headers)

    def log_message(self, *args, **kwargs):
        return


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(
        f"WEB READY on http://{HOST}:{PORT}  "
        f"(GET /, POST /api/ask, GET /health)",
        flush=True,
    )
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()


if __name__ == "__main__":
    main()
