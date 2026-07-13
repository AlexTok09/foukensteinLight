#!/usr/bin/env python3
"""
Wikipedia grounding — Foukenstein.

Extrait les personnes citées dans la question via un appel Mistral dédié,
interroge l'API REST de Wikipedia FR, filtre via Wikidata (uniquement les
humains, Q5), et retourne un bloc de faits vérifiés à injecter dans le
system_prompt.

Philosophie : fournir des faits au modèle plutôt que d'interdire
l'hallucination. La persona reste au-dessus — les faits sont ancrage, pas
script.

Dépendances : urllib uniquement. Extraction des noms par LLM (Mistral small),
avec regex en filet de sécurité si l'appel échoue.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from typing import Any

# ── Config ───────────────────────────────────────────────────────────────────

USER_AGENT       = "Foukenstein/1.0 (contact@distributedgallery.com)"
WIKI_BASE        = "https://fr.wikipedia.org/api/rest_v1/page/summary/"
WIKIDATA_BASE    = "https://www.wikidata.org/wiki/Special:EntityData/"
HTTP_TIMEOUT     = 3.0
MAX_NAMES        = 3
EXTRACT_MAX_CHARS = 800

# Modèle d'extraction des noms. Rapide et bon marché, pas besoin de la force
# d'un mistral-large juste pour lire une question.
MISTRAL_URL          = "https://api.mistral.ai/v1/chat/completions"
EXTRACT_MODEL        = os.environ.get("GROUNDING_EXTRACT_MODEL", "mistral-small-latest")
EXTRACT_TIMEOUT      = float(os.environ.get("GROUNDING_EXTRACT_TIMEOUT", "8.0"))
EXTRACT_MAX_TOKENS   = 150

# Mots à deux capitales qui ne sont pas des personnes. Défense en profondeur :
# la plupart seraient filtrés par le check Wikidata (pas humain), mais on évite
# les appels inutiles.
STOPLIST = {
    "Je", "Tu", "Il", "Elle", "Nous", "Vous", "Ils", "Elles", "On",
    "La Sorbonne", "Le Monde", "La République", "L'État", "L'Etat",
    "La France", "La Bretagne", "La Russie", "Les États-Unis",
    "BFM TV", "Grand Continent", "Lundi Matin",
    "Collège International", "Collège de France",
    "Parti Socialiste", "Rassemblement National", "La République En Marche",
    "Europe Écologie", "Europe Ecologie",
    # Foukenstein lui-même ne doit jamais être traité comme une personne à
    # rechercher sur Wikipedia. Son identité est gérée par le module identite.txt.
    "Foukenstein", "Foukenstein 2026",
}

# Regex de secours : séquence de ≥ 2 mots commençant par majuscule, séparés par
# espace ou tiret. Utilisée uniquement si l'extraction LLM échoue.
_CAP_PATTERN = re.compile(
    r'\b[A-ZÀÁÂÄÆÇÈÉÊËÌÍÎÏÑÒÓÔÖØÙÚÛÜÝ][a-zà-ÿ\']+'
    r'(?:[\s\-][A-ZÀÁÂÄÆÇÈÉÊËÌÍÎÏÑÒÓÔÖØÙÚÛÜÝ][a-zà-ÿ\']+)+\b'
)


# ── Extraction des noms candidats ────────────────────────────────────────────

_EXTRACT_SYSTEM_PROMPT = (
    "Tu extrais uniquement les noms de personnes humaines réelles (vivantes "
    "ou historiques) mentionnées dans une question. Tu ignores les concepts, "
    "les institutions, les lieux, les œuvres, les titres de livres, les noms "
    "de marques ou de médias. "
    "Tu corriges la casse si nécessaire (par exemple 'clément sénéchal' devient "
    "'Clément Sénéchal'). Tu corriges aussi les coquilles évidentes uniquement "
    "quand tu es certain de la personne visée (par exemple 'Anna Harendt' → "
    "'Hannah Arendt'). En cas de doute sur la coquille, tu laisses la forme "
    "d'origine. "
    "Si aucune personne n'est mentionnée, tu retournes une liste vide. "
    "Format de sortie OBLIGATOIRE : JSON strict de la forme "
    '{"persons": ["Nom Prénom", "Autre Nom"]}. Rien d\'autre, pas de '
    "commentaire, pas de markdown."
)


def _extract_via_llm(question: str) -> list[str] | None:
    """
    Appelle Mistral pour extraire les noms de personnes. Retourne une liste
    (potentiellement vide) en cas de succès, ou None en cas d'échec. Dans ce
    dernier cas, l'appelant doit tomber sur un fallback.
    """
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        return None

    payload = {
        "model":       EXTRACT_MODEL,
        "messages": [
            {"role": "system", "content": _EXTRACT_SYSTEM_PROMPT},
            {"role": "user",   "content": question},
        ],
        "temperature": 0,
        "max_tokens":  EXTRACT_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }

    req = urllib.request.Request(
        MISTRAL_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
            "User-Agent":    USER_AGENT,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=EXTRACT_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None

    try:
        content = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError):
        return None

    # Retirer un éventuel fencing markdown si le modèle en ajoute
    if content.startswith("```"):
        content = re.sub(r'^```[^\n]*\n?', '', content)
        content = re.sub(r'\n?```$', '', content).strip()

    try:
        parsed = json.loads(content)
    except Exception:
        return None

    persons = parsed.get("persons") if isinstance(parsed, dict) else None
    if not isinstance(persons, list):
        return None

    out: list[str] = []
    seen: set[str] = set()
    for p in persons:
        if not isinstance(p, str):
            continue
        name = p.strip()
        if not name or name in seen or name in STOPLIST:
            continue
        seen.add(name)
        out.append(name)
    return out


def _extract_via_regex(question: str) -> list[str]:
    """
    Fallback : regex sur les séquences multi-mots capitalisées. Utilisé
    uniquement si l'extraction LLM renvoie None (erreur, pas de clé API, etc.).
    """
    seen: set[str] = set()
    out: list[str] = []
    for name in _CAP_PATTERN.findall(question):
        if name in STOPLIST or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def extract_candidate_names(question: str) -> list[str]:
    """
    Retourne la liste (ordonnée, dédoublonnée) des noms de personnes détectés.
    Priorité à l'extraction LLM (Mistral small) : elle gère la casse, les
    typos, et tous les phrasés naturels français sans nécessiter de patterns.
    Fallback sur regex si l'appel échoue.
    """
    llm_names = _extract_via_llm(question)
    if llm_names is not None:
        return llm_names
    return _extract_via_regex(question)


# ── Appels API ───────────────────────────────────────────────────────────────

def _http_get_json(url: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept":     "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())


def _fetch_wiki_summary(name: str) -> dict | None:
    """
    REST API Wikipedia FR. Retourne le dict complet, ou None en cas d'échec.
    Le champ `type` peut être 'standard', 'disambiguation', 'no-extract', etc.
    """
    encoded = urllib.parse.quote(name.replace(" ", "_"), safe="")
    url = WIKI_BASE + encoded
    try:
        return _http_get_json(url)
    except Exception:
        return None


def _is_human(wikibase_item: str) -> bool:
    """
    Vérifie via Wikidata que l'entité est une instance of humain (Q5).
    Garde-fou contre les faux positifs : groupes, villes, personnages de fiction,
    etc. qui auraient passé la regex et la page Wikipedia.
    """
    if not wikibase_item:
        return False
    url = WIKIDATA_BASE + wikibase_item + ".json"
    try:
        data = _http_get_json(url)
    except Exception:
        return False

    entity = data.get("entities", {}).get(wikibase_item, {})
    claims = entity.get("claims", {}).get("P31", [])  # P31 = instance of
    for claim in claims:
        value = (
            claim.get("mainsnak", {})
            .get("datavalue", {})
            .get("value", {})
        )
        if isinstance(value, dict) and value.get("id") == "Q5":
            return True
    return False


# ── API publique ─────────────────────────────────────────────────────────────

def _name_covered_by_modules(name: str, module_file_stems: set[str]) -> bool:
    """
    Vrai si un des tokens significatifs du nom apparaît dans (ou comme) le stem
    d'un fichier module matché.

    Match par sous-chaîne : gère les noms de fichier concaténés sans séparateur
    (ex : stem "letexier" couvre "Thibault Le Texier" via son token "texier").
    Gère aussi les stems avec tiret (ex : "dardot-laval" couvre "Pierre Dardot"
    via "dardot").

    Exemples :
      - name="Romain Huet", stems={"huet"} → True
      - name="Thibault Le Texier", stems={"letexier"} → True (texier ⊂ letexier)
      - name="Pierre Dardot", stems={"dardot-laval"} → True
      - name="Emmanuel Macron", stems={"politique"} → False
    """
    if not module_file_stems:
        return False
    name_low = name.lower().strip()
    tokens = [t for t in re.split(r'[\s\-]+', name_low) if len(t) >= 4]
    if not tokens:
        return False
    for stem in module_file_stems:
        stem_low = stem.lower()
        for tok in tokens:
            if tok in stem_low:
                return True
    return False


def get_grounding_text(
    question: str,
    module_file_stems: set[str] | None = None,
) -> tuple[str, list[dict]]:
    """
    Retourne (texte_à_injecter_dans_le_system_prompt, debug_info).

    `module_file_stems` : set des stems (noms de fichier sans extension) des
    modules matchés pour cette question. Si un candidat partage un token avec
    un stem, le module est considéré comme dédié à la personne et Wikipedia
    est sauté.

    Pour chaque nom candidat :
      1. Extraction LLM (ou regex en fallback)
      2. Skip si un module dédié couvre le nom
      3. Stoplist filter
      4. Appel Wikipedia REST : page existe ? type = standard ?
      5. Appel Wikidata : instance of Q5 (humain) ?
      6. Si OK : tronquer l'extract à ~800 chars, ajouter au bloc "faits vérifiés"
      7. Si non OK (no_page, disambiguation, not_human) : ajouter au bloc "non
         identifiés" pour forcer le refus propre de la persona sur ces noms.
    """
    debug: list[dict] = []
    candidates = extract_candidate_names(question)[:MAX_NAMES]
    module_file_stems = module_file_stems or set()

    if not candidates:
        return "", debug

    facts: list[str] = []
    unresolved: list[str] = []  # noms candidats non groundés → refus explicite

    for name in candidates:
        entry: dict = {"name": name}

        # 0. Couvert par un module dédié → skip Wikipedia, le module gère.
        if _name_covered_by_modules(name, module_file_stems):
            entry["status"] = "covered_by_module"
            debug.append(entry)
            continue

        summary = _fetch_wiki_summary(name)
        if summary is None:
            entry["status"] = "no_page"
            unresolved.append(name)
            debug.append(entry)
            continue

        page_type = summary.get("type")
        if page_type == "disambiguation":
            entry["status"] = "disambiguation"
            unresolved.append(name)
            debug.append(entry)
            continue

        wikibase = summary.get("wikibase_item", "")
        if not wikibase:
            entry["status"] = "no_wikidata_id"
            unresolved.append(name)
            debug.append(entry)
            continue

        if not _is_human(wikibase):
            entry["status"] = "not_human"
            entry["wikibase_item"] = wikibase
            # pas humain → pas un nom de personne dans la question ; on
            # n'ajoute pas aux unresolved (évite un refus injuste sur ex: "BFM")
            debug.append(entry)
            continue

        extract = (summary.get("extract") or "").strip()
        if not extract:
            entry["status"] = "no_extract"
            unresolved.append(name)
            debug.append(entry)
            continue

        # Tronquer à la dernière phrase complète sous EXTRACT_MAX_CHARS
        if len(extract) > EXTRACT_MAX_CHARS:
            cut = extract.rfind(".", 0, EXTRACT_MAX_CHARS)
            extract = extract[:cut + 1] if cut != -1 else extract[:EXTRACT_MAX_CHARS]

        canonical = summary.get("titles", {}).get("normalized") or name
        facts.append(f"[Faits vérifiés — {canonical}]\n{extract}")
        entry["status"] = "grounded"
        entry["chars"]  = len(extract)
        entry["canonical"] = canonical
        debug.append(entry)

    if not facts and not unresolved:
        return "", debug

    parts: list[str] = []

    if facts:
        parts.append(
            "[FAITS VÉRIFIÉS — BLOC WIKIPEDIA]\n"
            "Ces faits sont ton ancrage, pas ton script. Tu les interprètes avec ta voix, "
            "tes concepts, tes affects. Tu peux être d'accord, en désaccord, méprisant, "
            "enthousiaste selon où la personne se situe politiquement. Tu ne paraphrases "
            "jamais en mode encyclopédique. Si un fait contredit ton intuition, tu te "
            "corriges sur le fait — ta voix reste la tienne, mais tu ne falsifies pas "
            "le CV de la personne.\n\n" + "\n\n".join(facts)
        )

    if unresolved:
        names_str = ", ".join(unresolved)
        parts.append(
            "[PERSONNES NON IDENTIFIÉES — WIKIPEDIA VIDE]\n"
            f"Les personnes suivantes citées dans la question n'ont pas de page "
            f"Wikipedia exploitable : {names_str}.\n"
            f"Tu ne les connais pas. Applique strictement la règle "
            f"[HONNÊTETÉ ET INCERTITUDE] : un chunk sec du type "
            f'{{"chunks":["Ce nom ne me dit rien. Je n\'invente pas ce que je ne '
            f'connais pas."]}}. Pas d\'analyse, pas de vibe, pas de gabarit politique. '
            f"Si la question contient aussi un sujet autre que ces personnes "
            f"(concept, événement), tu peux répondre sur ce sujet, mais jamais "
            f"en caractérisant les personnes non identifiées."
        )

    return "\n\n".join(parts), debug


# ── CLI de test ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: wikipedia_grounding.py 'question avec des noms propres'")
        sys.exit(1)
    q = sys.argv[1]
    text, dbg = get_grounding_text(q)
    print("=== DEBUG ===")
    print(json.dumps(dbg, ensure_ascii=False, indent=2))
    print("=== GROUNDING TEXT ===")
    print(text if text else "(vide)")
