#!/usr/bin/env python3
"""
Pipeline GROUNDED — Foukenstein
Persona + modules thématiques + grounding Wikipedia sur les personnes citées.

Routage hybride des modules :
  - Modules avec [ROUTING]   → sélection sémantique (embeddings Mistral)
  - Modules sans [ROUTING]   → keywords (noms propres : philosophes, penseurs)
Voir semantic_router.py pour la logique et les seuils.

Après le chargement des modules, wikipedia_grounding.get_grounding_text() est
appelé pour compléter la réponse avec des faits vérifiés.
"""
import os, sys, json, urllib.request, re, datetime as _dt

# Grounding local (import par chemin absolu pour éviter les soucis quand
# generate_grounded.py est lancé en subprocess depuis pipeline_grounded.py)
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)
import wikipedia_grounding  # noqa: E402
try:
    import semantic_router
    _ROUTER_AVAILABLE = True
except Exception as _e:
    print(f"[generate_grounded] semantic_router indisponible : {_e}",
          file=sys.stderr)
    _ROUTER_AVAILABLE = False

API_KEY = os.environ.get("MISTRAL_API_KEY")
if not API_KEY:
    print("ERROR: MISTRAL_API_KEY not set", file=sys.stderr)
    sys.exit(1)

MODEL              = os.environ.get("MISTRAL_MODEL", "mistral-large-latest")
SYSTEM_PROMPT_PATH = os.environ.get("SYSTEM_PROMPT_PATH")
PERSONA_PATH       = os.environ.get("PERSONA_PATH", "/workspace/IA/persona/foukenstein_light.txt")
MODULES_DIR        = os.environ.get("MODULES_DIR", "/workspace/IA/modules")
MODULES_MAX        = int(os.environ.get("MODULES_MAX", "2"))
MODULES_MAX_CHARS  = int(os.environ.get("MODULES_MAX_CHARS", "17000"))
TEMPERATURE        = float(os.environ.get("TEMPERATURE", "0.7"))

# Grounding Wikipedia ON/OFF (ON par défaut dans ce pipeline)
GROUNDING_ENABLED  = os.environ.get("GROUNDING_ENABLED", "1") == "1"

# Mémoire conversationnelle
_conv_history_raw = os.environ.get("CONV_HISTORY", "")
conv_history = []
if _conv_history_raw:
    try:
        conv_history = json.loads(_conv_history_raw)
    except Exception:
        conv_history = []

# ── Logging ──────────────────────────────────────────────────────────────────
DEBUG_LOG_DIR = os.environ.get("DEBUG_LOG_DIR", "/workspace/IA/logs/requests_grounded")

def _question_slug(q, maxlen=60):
    slug = re.sub(r"[^\w\s-]", "", q.lower())
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    return slug[:maxlen]

_ts_now = _dt.datetime.now()
_ts_str = _ts_now.strftime("%Y%m%d_%H%M%S")
_ts_iso = _ts_now.strftime("%Y-%m-%dT%H:%M:%S")

_debug = {
    "pipeline":   "grounded",
    "timestamp":  _ts_iso,
    "question":   None,
    "model":      MODEL,
    "temperature": TEMPERATURE,
    "modules": {
        "matched_files": [],
        "text_chars":    0,
        "semantic": {
            "available": _ROUTER_AVAILABLE,
            "ranking":   [],
            "selected":  [],
            "fallback":  False,
            "error":     None,
        },
        "keywords": {
            "selected":  [],
        },
    },
    "grounding": {
        "enabled":   GROUNDING_ENABLED,
        "candidates": [],
        "text_chars": 0,
    },
    "prompt": {
        "system_chars": 0,
        "user_chars":   0,
    },
    "tokens": {
        "prompt":     None,
        "completion": None,
        "total":      None,
    },
    "output": {
        "chunks_count": 0,
        "chunks":       [],
    },
}

def _write_debug_log():
    try:
        os.makedirs(DEBUG_LOG_DIR, exist_ok=True)
        req_id = os.environ.get("REQUEST_ID", "").strip()
        if req_id:
            fname = f"{req_id}.debug.json"
            _debug["request_id"] = req_id
        else:
            slug  = _question_slug(_debug["question"] or "unknown")
            fname = f"{_ts_str}_{slug}.debug.json"
        path = os.path.join(DEBUG_LOG_DIR, fname)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_debug, f, ensure_ascii=False, indent=2)
        print(f"[DEBUG] log → {path}", file=sys.stderr)
    except Exception as e:
        print(f"[DEBUG] log write failed: {e}", file=sys.stderr)

# ── Modules — routage hybride (keywords noms propres + sémantique) ────────────

def _extract_positions(content: str) -> str:
    m = re.search(
        r'\[(?:POSITIONS|MODULE)\]\s*\n(.*?)(?:\n\[|\Z)',
        content, re.DOTALL | re.IGNORECASE
    )
    text = m.group(1).strip() if m else ""
    if len(text) > MODULES_MAX_CHARS:
        cut = text.rfind(".", 0, MODULES_MAX_CHARS)
        text = text[:cut + 1] if cut != -1 else text[:MODULES_MAX_CHARS]
    return text


def _keyword_hits(content: str, q_lower: str) -> int:
    kw_match = re.search(r'\[KEYWORDS\]\s*\n(.*?)(?:\n\[|\Z)', content,
                         re.DOTALL | re.IGNORECASE)
    if not kw_match:
        return 0
    keywords_raw = kw_match.group(1)
    keywords = [k.strip().lower() for k in keywords_raw.split(",") if k.strip()]

    def _kw_ok(kw):
        if " " in kw:
            return True
        orig = next((k.strip() for k in keywords_raw.split(",")
                     if k.strip().lower() == kw), kw)
        if orig == orig.upper() and len(orig) >= 2:
            return True
        return len(kw) >= 4

    return sum(
        1 for kw in keywords
        if kw and _kw_ok(kw) and re.search(rf'(?<!\w){re.escape(kw)}(?!\w)', q_lower)
    )


def _partition_modules(modules_dir: str) -> tuple[list[str], list[str]]:
    """Sépare les modules : avec [ROUTING] (thématiques) vs sans (noms propres)."""
    thematic:    list[str] = []
    proper_noun: list[str] = []
    for fname in sorted(os.listdir(modules_dir)):
        if not fname.endswith(".txt"):
            continue
        try:
            content = open(os.path.join(modules_dir, fname),
                           encoding="utf-8").read()
        except Exception:
            continue
        if re.search(r'\[ROUTING\]', content, re.IGNORECASE):
            thematic.append(fname)
        else:
            proper_noun.append(fname)
    return thematic, proper_noun


def _legacy_keyword_fallback(question: str) -> list[str]:
    """Ancien comportement keyword-only, utilisé si le routeur sémantique échoue."""
    q_lower = question.lower()
    scored: list[tuple[int, str]] = []
    for fname in sorted(os.listdir(MODULES_DIR)):
        if not fname.endswith(".txt"):
            continue
        try:
            content = open(os.path.join(MODULES_DIR, fname),
                           encoding="utf-8").read()
        except Exception:
            continue
        h = _keyword_hits(content, q_lower)
        if h > 0:
            scored.append((h, fname))
    scored.sort(key=lambda x: -x[0])
    return [fname for _, fname in scored[:MODULES_MAX]]


def _load_modules_light(question: str) -> tuple[str, set[str]]:
    """
    Routage hybride :
      1. Modules noms propres (sans [ROUTING])  → keyword match (priorité)
      2. Modules thématiques (avec [ROUTING])   → embedding Mistral + double-gate
      3. Combine, cap à MODULES_MAX.

    Retourne (texte_modules, set_de_stems). Le set de stems est consommé par
    wikipedia_grounding pour éviter de re-couvrir une personne déjà traitée par
    un module dédié.

    Fallback : si l'API embed Mistral plante, on retombe sur keywords-tout-module.
    """
    if not MODULES_DIR or not os.path.isdir(MODULES_DIR):
        return "", set()

    thematic_files, proper_noun_files = _partition_modules(MODULES_DIR)
    q_lower = question.lower()

    # 1. Noms propres (signal discret fort, priorité)
    pn_scored: list[tuple[int, str]] = []
    for fname in proper_noun_files:
        try:
            content = open(os.path.join(MODULES_DIR, fname),
                           encoding="utf-8").read()
        except Exception:
            continue
        h = _keyword_hits(content, q_lower)
        if h > 0:
            pn_scored.append((h, fname))
    pn_scored.sort(key=lambda x: -x[0])

    selected_files: list[str] = [fname for _, fname in pn_scored[:MODULES_MAX]]
    _debug["modules"]["keywords"]["selected"] = [
        {"file": f, "hits": h} for h, f in pn_scored[:MODULES_MAX]
    ]

    # 2. Sémantique pour les slots restants
    remaining = MODULES_MAX - len(selected_files)
    if remaining > 0:
        if _ROUTER_AVAILABLE:
            try:
                ranked = semantic_router.rank(question)
                _debug["modules"]["semantic"]["ranking"] = [
                    {"file": f, "score": round(s, 4)} for f, s in ranked[:5]
                ]
                sem = semantic_router.select(question, max_k=MODULES_MAX,
                                             _precomputed=ranked)

                # Tiebreaker keyword : si le sémantique seul n'a rien retenu mais
                # que le top1 passe le plancher, et qu'un de ses keywords est
                # présent dans la question, on le prend quand même. Couvre les
                # acronymes courts (NFT, DAO, ZK…) que l'embedding distingue mal.
                if not sem and ranked:
                    top_fname, top_score = ranked[0]
                    if top_score >= semantic_router.MIN_SCORE:
                        try:
                            top_content = open(os.path.join(MODULES_DIR, top_fname),
                                               encoding="utf-8").read()
                            if _keyword_hits(top_content, q_lower) >= 1:
                                sem = [(top_fname, top_score)]
                                _debug["modules"]["semantic"]["tiebreaker"] = {
                                    "file": top_fname, "score": round(top_score, 4),
                                }
                        except Exception:
                            pass

                _debug["modules"]["semantic"]["selected"] = [
                    {"file": f, "score": round(s, 4)} for f, s in sem
                ]
                for fname, _ in sem[:remaining]:
                    if fname not in selected_files:
                        selected_files.append(fname)
            except Exception as e:
                _debug["modules"]["semantic"]["error"]    = str(e)
                _debug["modules"]["semantic"]["fallback"] = True
                print(f"[generate_grounded] router failed, fallback keywords: {e}",
                      file=sys.stderr)
                legacy = _legacy_keyword_fallback(question)
                for fname in legacy:
                    if fname not in selected_files and len(selected_files) < MODULES_MAX:
                        selected_files.append(fname)
        else:
            _debug["modules"]["semantic"]["fallback"] = True
            legacy = _legacy_keyword_fallback(question)
            for fname in legacy:
                if fname not in selected_files and len(selected_files) < MODULES_MAX:
                    selected_files.append(fname)

    _debug["modules"]["matched_files"] = [{"file": f} for f in selected_files]
    if not selected_files:
        return "", set()

    # 3. Concaténer [POSITIONS] et collecter les stems
    parts: list[str] = []
    file_stems: set[str] = set()
    for fname in selected_files:
        try:
            content = open(os.path.join(MODULES_DIR, fname),
                           encoding="utf-8").read()
        except Exception:
            continue
        positions_text = _extract_positions(content)
        if not positions_text:
            continue
        stem = fname.replace(".txt", "").lower()
        parts.append(f"[Positions — {stem}]\n{positions_text}")
        file_stems.add(stem)

    result = "\n\n".join(parts)
    _debug["modules"]["text_chars"] = len(result)
    return result, file_stems


# ── Lecture des arguments ─────────────────────────────────────────────────────
if len(sys.argv) < 2 or not sys.argv[1].strip():
    print("ERROR: no question provided", file=sys.stderr)
    sys.exit(1)

question = sys.argv[1].strip()
_debug["question"] = question

# ── System prompt ─────────────────────────────────────────────────────────────
if not SYSTEM_PROMPT_PATH:
    print("ERROR: SYSTEM_PROMPT_PATH not set", file=sys.stderr)
    sys.exit(1)

with open(SYSTEM_PROMPT_PATH, encoding="utf-8") as f:
    system_prompt = f.read().strip()

# ── Persona ──────────────────────────────────────────────────────────────────
if PERSONA_PATH and os.path.exists(PERSONA_PATH):
    with open(PERSONA_PATH, encoding="utf-8") as f:
        system_prompt = system_prompt + "\n\n" + f.read().strip()

# ── Modules ───────────────────────────────────────────────────────────────────
modules_text, module_file_stems = _load_modules_light(question)
if modules_text:
    system_prompt = system_prompt + "\n\n" + modules_text

# ── Grounding Wikipedia ───────────────────────────────────────────────────────
# Les stems de fichier des modules matchés sont passés au grounding : une
# personne couverte par un module dédié (ex: huet.txt) sera sautée côté
# Wikipedia pour éviter les collisions d'homonymes.
if GROUNDING_ENABLED:
    try:
        grounding_text, grounding_debug = wikipedia_grounding.get_grounding_text(
            question, module_file_stems=module_file_stems,
        )
        _debug["grounding"]["candidates"] = grounding_debug
        if grounding_text:
            system_prompt = system_prompt + "\n\n" + grounding_text
            _debug["grounding"]["text_chars"] = len(grounding_text)
    except Exception as e:
        # Jamais casser la génération à cause du grounding. En cas d'erreur,
        # on log et on continue sans grounding.
        _debug["grounding"]["error"] = str(e)
        print(f"[WARN] grounding failed: {e}", file=sys.stderr)

_debug["prompt"]["system_chars"] = len(system_prompt)

# ── Construction des messages ─────────────────────────────────────────────────
messages = [{"role": "system", "content": system_prompt}]

for turn in conv_history:
    role    = turn.get("role", "")
    content = turn.get("content", "")
    if role in ("user", "assistant") and content:
        messages.append({"role": role, "content": content})

messages.append({"role": "user", "content": question})

_debug["prompt"]["user_chars"] = len(question)

# ── Appel API Mistral ─────────────────────────────────────────────────────────
payload = {
    "model":       MODEL,
    "messages":    messages,
    "temperature": TEMPERATURE,
    "max_tokens":  700,
}

req = urllib.request.Request(
    "https://api.mistral.ai/v1/chat/completions",
    data=json.dumps(payload).encode(),
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type":  "application/json",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
except urllib.error.HTTPError as e:
    body = e.read().decode(errors="replace")
    print(f"ERROR: Mistral API {e.code}: {body}", file=sys.stderr)
    sys.exit(1)

# ── Tokens ────────────────────────────────────────────────────────────────────
usage = data.get("usage", {})
_token_line = (
    f"[TOKENS] model={MODEL}"
    f"  prompt={usage.get('prompt_tokens')}"
    f"  completion={usage.get('completion_tokens')}"
    f"  total={usage.get('total_tokens')}"
)
print(_token_line, file=sys.stderr)

_token_log = os.environ.get("TOKEN_LOG", "/workspace/tmp_work/token_usage_grounded.log")
try:
    os.makedirs(os.path.dirname(_token_log), exist_ok=True)
    with open(_token_log, "a", encoding="utf-8") as f:
        f.write(f"{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {_token_line}\n")
except Exception:
    pass

_debug["tokens"]["prompt"]     = usage.get("prompt_tokens")
_debug["tokens"]["completion"] = usage.get("completion_tokens")
_debug["tokens"]["total"]      = usage.get("total_tokens")

# ── Post-traitement ───────────────────────────────────────────────────────────
raw_content = data["choices"][0]["message"]["content"].strip()

if raw_content.startswith("```"):
    raw_content = re.sub(r'^```[^\n]*\n?', '', raw_content)
    raw_content = re.sub(r'\n?```$', '', raw_content).strip()

try:
    parsed = json.loads(raw_content)
    if "chunks" in parsed:
        _QUOTES_TO_STRIP = str.maketrans("", "", "«»\"“”")
        cleaned_chunks = []
        for chunk in parsed["chunks"]:
            chunk = re.sub(r'\*+', '', chunk)
            chunk = re.sub(r'_{2,}', '', chunk)
            chunk = re.sub(r'#+\s*', '', chunk)
            chunk = chunk.translate(_QUOTES_TO_STRIP)
            # Deux-points et points-virgules → point (XTTS les lit mal).
            # On ajoute une majuscule derrière pour préserver la grammaire.
            chunk = re.sub(r'\s*[:;]\s*', '. ', chunk)
            chunk = re.sub(r'\.{2,}', '.', chunk)
            chunk = re.sub(
                r'\.\s+([a-zà-ÿ])',
                lambda m: '. ' + m.group(1).upper(),
                chunk,
            )
            # Filet de sécurité TTS : chaque chunk doit se terminer par un
            # point ou un point d'interrogation. Sinon XTTS ne clôt pas
            # l'intonation et les chunks s'enchaînent en mélasse.
            chunk = chunk.rstrip()
            if chunk and chunk[-1] not in ".?":
                chunk = chunk + "."
            cleaned_chunks.append(chunk)
        parsed["chunks"] = cleaned_chunks
        raw_content = json.dumps(parsed, ensure_ascii=False)
        _debug["output"]["chunks_count"] = len(cleaned_chunks)
        _debug["output"]["chunks"]       = cleaned_chunks
except Exception:
    pass

_write_debug_log()
print(raw_content)
