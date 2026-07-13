#!/usr/bin/env python3
"""
Web server — Foukenstein deployment_v1.

Serveur HTTP autonome :
  * route /api/ask vers pipeline_grounded.run() (Mistral + grounding + RunPod TTS)
  * route /api/tts vers RunPod TTS sans Mistral
  * nomme les fichiers audio par request_id pour éviter les collisions
  * expose une route /audio/<uuid>.<ext> pour la lecture (inline)
  * expose une route /download/<uuid>.<ext> pour le téléchargement (attachment)
  * lance un thread de cleanup TTL au boot

Pipeline actif (mode /api/ask) :
    Client → app.py → pipeline_grounded.py → generate_grounded.py (Mistral)
                              ↓
                    runpod_tts_client.py → Runpod Serverless /runsync
                              ↓
                         audio bytes → data/audio/<uuid>.wav
                              ↓
                       { audio_url, download_url }
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# ── Imports locaux (app/) ─────────────────────────────────────────────────────
APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import pipeline_grounded  # noqa: E402
import runpod_tts_client  # noqa: E402
from cleanup import start_cleanup_thread  # noqa: E402


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
# Déplacer deployment_v1/ ailleurs n'exige plus d'éditer env.sh : tant qu'on
# n'override rien, tout est relatif au dossier courant.
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
AUDIO_DIR   = os.environ.get("AUDIO_DIR",      os.path.join(DEPLOY_ROOT, "data", "audio"))
MEMORY_DIR  = os.environ.get("MEMORY_DIR",     os.path.join(DEPLOY_ROOT, "data", "memory"))

HOST = os.environ.get("WEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("WEB_PORT", "9999"))

AUDIO_TTL_HOURS     = float(os.environ.get("AUDIO_TTL_HOURS", "24"))
CLEANUP_INTERVAL_MIN = float(os.environ.get("CLEANUP_INTERVAL_MIN", "30"))

OUT_FORMAT = os.environ.get("OUT_FORMAT", "wav").lower()
if OUT_FORMAT not in ("wav", "mp3"):
    OUT_FORMAT = "wav"

MEMORY_MAX = 2  # on garde 2 tours, on n'injecte que le dernier

RATE_LIMIT_SECONDS     = int(os.environ.get("RATE_LIMIT_SECONDS", "40"))
TTS_RATE_LIMIT_SECONDS = int(os.environ.get("TTS_RATE_LIMIT_SECONDS", "30"))
MAX_QUESTION_CHARS     = int(os.environ.get("MAX_QUESTION_CHARS", "160"))
ASK_CONCURRENCY_LIMIT  = int(os.environ.get("ASK_CONCURRENCY_LIMIT", "4"))
TTS_CONCURRENCY_LIMIT  = int(os.environ.get("TTS_CONCURRENCY_LIMIT", str(ASK_CONCURRENCY_LIMIT)))

ALLOWED_ORIGINS = {
    "https://foukenstein.lol",
    "https://www.foukenstein.lol",
    "https://foukenstein.com",
    "https://www.foukenstein.com",
}
DEFAULT_ORIGIN = "https://foukenstein.lol"

os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(MEMORY_DIR, exist_ok=True)

# ── Rate limiting par IP ─────────────────────────────────────────────────────
import threading
_rate_lock = threading.Lock()
_rate_last: dict[str, float] = {}
_rate_last_tts: dict[str, float] = {}
_ask_slots = threading.BoundedSemaphore(max(1, ASK_CONCURRENCY_LIMIT))
_tts_slots = threading.BoundedSemaphore(max(1, TTS_CONCURRENCY_LIMIT))


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


def _check_tts_rate_limit(ip_hash: str) -> float:
    now = time.time()
    with _rate_lock:
        last = _rate_last_tts.get(ip_hash, 0.0)
        remaining = TTS_RATE_LIMIT_SECONDS - (now - last)
        if remaining > 0:
            return remaining
        _rate_last_tts[ip_hash] = now
        return 0.0


# ── Runpod worker status (cached, server-side) ────────────────────────────────
# Polled by the frontend every ~10s; the cache keeps the load on Runpod's
# /health endpoint to one call every 3s regardless of visitor count.
_WORKER_STATUS_CACHE: dict = {"data": None, "ts": 0.0}
_WORKER_STATUS_LOCK = threading.Lock()
_WORKER_STATUS_TTL = 3.0


def _fetch_runpod_endpoint_config(eid: str, key: str) -> dict:
    import urllib.request
    query = """
    query getEndpointWorkerConfig($id: String!) {
      myself {
        endpoint(id: $id) {
          workersMin
          workersMax
          pods { id desiredStatus uptimeSeconds lastStartedAt }
        }
      }
    }
    """
    payload = json.dumps({
        "query": query,
        "variables": {"id": eid},
        "operationName": "getEndpointWorkerConfig",
    }).encode()
    req = urllib.request.Request(
        "https://api.runpod.io/graphql",
        data=payload,
        headers={
            "Authorization": key,
            "Content-Type": "application/json",
            "User-Agent": "foukenstein-ui/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode())
    if data.get("errors"):
        raise RuntimeError("RunPod GraphQL endpoint config error")
    return ((data.get("data") or {}).get("myself") or {}).get("endpoint") or {}


def _get_worker_status() -> dict:
    """Return {worker_active, principal_cold_starting, ...} from Runpod /health.

    Fail-safe: on any error, returns worker_active=true so the UI keeps the
    existing 'réfléchit' message instead of falsely claiming the bot is asleep.
    """
    import urllib.request
    now = time.time()
    with _WORKER_STATUS_LOCK:
        cached = _WORKER_STATUS_CACHE["data"]
        if cached is not None and (now - _WORKER_STATUS_CACHE["ts"]) < _WORKER_STATUS_TTL:
            return cached

    eid = os.environ.get("RUNPOD_SERVERLESS_ENDPOINT_ID", "").strip()
    key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not eid or not key:
        out = {"worker_active": True, "principal_cold_starting": False, "error": "env"}
    else:
        try:
            req = urllib.request.Request(
                f"https://api.runpod.ai/v2/{eid}/health",
                headers={"Authorization": f"Bearer {key}",
                         "User-Agent": "foukenstein-ui/1.0"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                h = json.loads(resp.read().decode())
            w = h.get("workers", {}) or {}
            j = h.get("jobs", {}) or {}
            health_running = int(w.get("running", 0))
            warming  = int(w.get("initializing", 0))
            in_queue = int(j.get("inQueue", 0))
            in_prog  = int(j.get("inProgress", 0))
            config_error = None
            try:
                config = _fetch_runpod_endpoint_config(eid, key)
            except Exception as e:
                config = {}
                config_error = str(e)[:200]
            pods = config.get("pods") or []
            running_pods = [p for p in pods if str(p.get("desiredStatus") or "").upper() == "RUNNING"]
            worker_active = bool(running_pods) or health_running > 0
            workers_min = config.get("workersMin")
            workers_max = config.get("workersMax")
            scheduled_sleep = (workers_min == 0) and not worker_active
            # Principal cold start = no worker is up yet, but something is
            # being warmed or already queued. If there's already a ready
            # worker AND a second one is initializing, that's a max-worker
            # add-on, not a principal cold start → not flagged.
            principal_cold_starting = (not worker_active) and (warming > 0 or in_queue > 0 or in_prog > 0)
            out = {
                "worker_active": worker_active,
                "principal_cold_starting": principal_cold_starting,
                "scheduled_sleep": scheduled_sleep,
                "config": {"workersMin": workers_min, "workersMax": workers_max},
                "config_error": config_error,
                "workers": w,
                "jobs": j,
                "pods": pods,
            }
        except Exception as e:
            out = {"worker_active": True, "principal_cold_starting": False,
                   "error": str(e)[:200]}

    with _WORKER_STATUS_LOCK:
        _WORKER_STATUS_CACHE["data"] = out
        _WORKER_STATUS_CACHE["ts"] = now
    return out


# ── Helpers mémoire (inchangés vs foukenstein_light) ──────────────────────────
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
SESSION_TTL    = 86400 * 7  # 7 jours


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
    """Renvoie (memory_key, set_cookie_value_or_None)."""
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
    ".wav":  "audio/wav",
    ".mp3":  "audio/mpeg",
    ".ico":  "image/x-icon",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".svg":  "image/svg+xml",
}


def read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


# ── Validation noms de fichier audio ──────────────────────────────────────────
def _is_safe_audio_name(name: str) -> bool:
    if not name or "/" in name or "\\" in name or ".." in name:
        return False
    if not (name.endswith(".wav") or name.endswith(".mp3")):
        return False
    return True


def _audio_mime(name: str) -> str:
    return "audio/mpeg" if name.endswith(".mp3") else "audio/wav"


# ── HTTP handler ──────────────────────────────────────────────────────────────
class H(BaseHTTPRequestHandler):
    def _cors_origin(self) -> str:
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            return origin
        if re.match(r"^https://[a-z0-9-]+\.proxy\.runpod\.net$", origin):
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

        if p == "/api/worker_status":
            return self._json(200, {"ok": True, **_get_worker_status()})

        if p == "/api/token_usage":
            return self._json(404, {"ok": False, "error": "not found"})

        # Audio : lecture inline
        if p.startswith("/audio/"):
            return self._serve_audio(p[len("/audio/"):], disposition="inline")

        # Audio : téléchargement forcé
        if p.startswith("/download/"):
            return self._serve_audio(p[len("/download/"):], disposition="attachment")

        # Assets statiques
        if p == "/":
            p = "/index.html"
        if p == "/test.html":
            return self._json(404, {"ok": False, "error": "not found"})
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

    def _serve_audio(self, name: str, disposition: str) -> None:
        if not _is_safe_audio_name(name):
            return self._json(400, {"ok": False, "error": "bad audio name"})
        path = os.path.join(AUDIO_DIR, name)
        if not os.path.exists(path):
            return self._json(404, {"ok": False, "error": "audio not found"})
        try:
            data = read_file(path)
        except Exception as e:
            return self._json(500, {"ok": False, "error": str(e)})

        self.send_response(200)
        self.send_header("Content-Type", _audio_mime(name))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        if disposition == "attachment":
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        else:
            self.send_header("Content-Disposition", f'inline; filename="{name}"')
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", self._cors_origin())
        self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(data)

    # ── POST ──────────────────────────────────────────────────────────────────
    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/ask":
            return self._handle_ask()
        if p == "/api/tts":
            return self._handle_tts()
        if p == "/api/generate":
            return self._json(404, {"ok": False, "error": "not found"})
        return self._json(404, {"ok": False, "error": "not found"})

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw    = self.rfile.read(length).decode("utf-8") if length else ""
        return json.loads(raw) if raw else {}

    def _save_audio(self, audio_bytes: bytes, fmt: str,
                    request_id: str | None = None) -> tuple[str, str, str]:
        rid  = request_id or uuid.uuid4().hex
        name = f"{rid}.{fmt}"
        path = os.path.join(AUDIO_DIR, name)
        with open(path, "wb") as f:
            f.write(audio_bytes)
        return name, f"/audio/{name}", f"/download/{name}"

    def _handle_ask(self):
        """Pipeline complet : Mistral (chunks) → Runpod Serverless (audio)."""
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

        # Rate limit par IP (l'IP reste la bonne granularité pour l'anti-abus)
        ip_key  = _ip_key(_get_client_ip(self))
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

        _slot_acquired = True
        cookie_headers = None
        # Request ID unifié : debug log Mistral + fichier audio partagent ce UUID
        request_id = uuid.uuid4().hex

        # Mémoire conversationnelle par session (cookie) — isole les users
        # partageant une IP (NAT, wifi public, etc.)
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

            # Pipeline principal : grounded (persona + modules + grounding Wikipedia).
            # Cold-start aware TTS timeout: when no Runpod worker is up, the
            # request includes a worker spawn (often 30-90s, more under GPU
            # throttling). The default 180s budget often expires before the
            # job completes — extending here means the user actually gets
            # their audio instead of a timeout error.
            ws = _get_worker_status()
            tts_timeout = (
                float(os.environ.get("RUNPOD_TTS_TIMEOUT_COLD", "360"))
                if not ws.get("worker_active", True) else None
            )
            audio_bytes, fmt, chunks = pipeline_grounded.run(
                question, conv_history=conv, out_format=OUT_FORMAT,
                request_id=request_id,
                tts_timeout=tts_timeout,
            )
        except pipeline_grounded.PipelineError as e:
            return self._json(502, {"ok": False, "error": f"pipeline: {e}", "request_id": request_id}, extra_headers=cookie_headers)
        except runpod_tts_client.RunpodTTSBusy:
            return self._json(503, {
                "ok": False,
                "error": "busy",
                "message": "Les workers voix sont déjà occupés, tentez plus tard.",
                "request_id": request_id,
            }, extra_headers=cookie_headers)
        except runpod_tts_client.RunpodTTSError as e:
            return self._json(502, {"ok": False, "error": f"runpod: {e}", "request_id": request_id}, extra_headers=cookie_headers)
        except Exception as e:
            return self._json(500, {"ok": False, "error": f"internal: {e}", "request_id": request_id}, extra_headers=cookie_headers)
        finally:
            if _slot_acquired:
                _ask_slots.release()

        name, audio_url, download_url = self._save_audio(audio_bytes, fmt, request_id=request_id)

        # Mise à jour mémoire (silencieux si erreur)
        try:
            response_text = " ".join(chunks)
            history.append({"q": question, "a": response_text, "ts": int(time.time())})
            _save_memory(sess_key, history)
        except Exception:
            pass

        return self._json(200, {
            "ok":           True,
            "request_id":   request_id,
            "audio_url":    audio_url,
            "download_url": download_url,
            "format":       fmt,
            "chunks":       len(chunks),
        }, extra_headers=cookie_headers)

    def _handle_tts(self):
        """Slave mode : texte brut → Runpod Serverless (sans Mistral)."""
        try:
            obj  = self._read_json_body()
            text = (obj.get("text") or "").strip()
        except Exception as e:
            return self._json(400, {"ok": False, "error": f"bad json: {e}"})

        if not text:
            return self._json(400, {"ok": False, "error": "missing text"})

        if len(text) > MAX_QUESTION_CHARS:
            return self._json(400, {
                "ok": False,
                "error": "text_too_long",
                "max_chars": MAX_QUESTION_CHARS,
            })

        # Rate limit spécifique TTS (plus large, coût Runpod Serverless plus élevé)
        ip_key = _ip_key(_get_client_ip(self))
        wait = _check_tts_rate_limit(ip_key)
        if wait > 0:
            return self._json(429, {
                "ok": False,
                "error": "rate_limit",
                "retry_after": round(wait),
            })

        if not _tts_slots.acquire(blocking=False):
            return self._json(503, {
                "ok": False,
                "error": "busy",
                "message": "Vous êtes nombreux à parler à Foukenstein, tentez plus tard.",
            })

        # Chunk naïf — le handler serverless fera de toute façon ses propres splits.
        # On envoie le texte en un seul chunk si court, sinon on le découpe en phrases.
        import re
        sentences = [s.strip() for s in re.split(r'(?<=[.!?…])\s+', text) if s.strip()]
        chunks = sentences or [text]

        request_id = uuid.uuid4().hex

        try:
            audio_bytes, fmt = runpod_tts_client.synthesize(chunks, out_format=OUT_FORMAT)
        except runpod_tts_client.RunpodTTSBusy:
            return self._json(503, {
                "ok": False,
                "error": "busy",
                "message": "Les workers voix sont déjà occupés, tentez plus tard.",
                "request_id": request_id,
            })
        except runpod_tts_client.RunpodTTSError as e:
            return self._json(502, {"ok": False, "error": f"runpod: {e}", "request_id": request_id})
        except Exception as e:
            return self._json(500, {"ok": False, "error": f"internal: {e}", "request_id": request_id})
        finally:
            _tts_slots.release()

        name, audio_url, download_url = self._save_audio(audio_bytes, fmt, request_id=request_id)
        return self._json(200, {
            "ok":           True,
            "request_id":   request_id,
            "audio_url":    audio_url,
            "download_url": download_url,
            "format":       fmt,
            "chunks":       len(chunks),
        })

    def log_message(self, *args, **kwargs):
        return


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    start_cleanup_thread(
        directory=AUDIO_DIR,
        ttl_hours=AUDIO_TTL_HOURS,
        interval_minutes=CLEANUP_INTERVAL_MIN,
    )
    print(
        f"WEB READY on http://{HOST}:{PORT}  "
        f"(GET /, POST /api/ask, POST /api/tts, GET /audio/<uuid>.{OUT_FORMAT}, "
        f"GET /download/<uuid>.{OUT_FORMAT}, GET /health)",
        flush=True,
    )
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()


if __name__ == "__main__":
    main()
