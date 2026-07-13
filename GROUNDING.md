# Grounding Wikipedia — Foukenstein

Journal technique des décisions prises pour éliminer les hallucinations sur les personnes nommées. Document vivant : chaque itération importante est consignée ici.

---

## 1. Problème initial

Foukenstein hallucinait systématiquement sur les personnes nommées :

- **Clément Sénéchal** (ex-porte-parole Greenpeace, critique de l'écologie bourgeoise) était décrit comme "gauche télévisée, radicalité de salon, figure utile au système" — inversion politique complète.
- **Jacques Derrida** → scènes fabriquées à Cerisy en 1968, "trois bouteilles de vin rouge", citations inventées.
- **Bertrand Russell** → biographie fabriquée ("m'a accompagné pendant mes années de formation à la Sorbonne").
- **Merleau-Ponty** → cours sur la Nature placé à la Sorbonne au lieu du Collège de France, chronologie incompatible avec la biographie de Foucault.
- **Romain Huet** → mélange entre le sociologue (module projet) et le rugbyman (homonyme Wikipedia).

Diagnostic : la persona fournissait des catégories méprisées prêtes à l'emploi (`intellectuels de salon`, `gauche social-démocrate`), le modèle slottait les figures vaguement connues dans ces catégories, et la règle [HONNÊTETÉ] ne se déclenchait que sur l'ignorance déclarée — jamais sur l'ignorance réelle.

---

## 2. Évolution de la persona

### 2.1 Suppression de l'échappatoire "figure publique documentée"

La persona contenait une ligne qui forçait l'analyse sur toute "figure publique documentée" et qualifiait la prudence de "paresse". Retirée.

### 2.2 Règle anti-fabulation d'événements

Ajoutée dans `[HONNÊTETÉ ET INCERTITUDE]` :
> Pas d'événements fabriqués. Tu n'inventes jamais une date, un lieu, un colloque, une rencontre, une lecture de jeunesse, une anecdote biographique précise.

Complétée par un EXEMPLE INTERDIT (Russell Sorbonne fabriqué) et un EXEMPLE CORRECT (analyse conceptuelle sans biographie inventée).

### 2.3 Gradient de mépris dans [POSTURE]

Avant : mépris uniforme sur droite + extrême droite + gauche social-démocrate + bourgeois.
Après : mépris fort sur droite + extrême droite, **critique sévère mais mesurée** sur gauche social-démocrate et bourgeois, avec reconnaissance explicite des acquis. Résultat concret : Badinter passe de "figure utile au système" à "homme de l'institution, avancée humaniste incontestable et limite politique".

---

## 3. Architecture de grounding

### 3.1 Fichiers créés (aucun fichier existant cassé)

```
app/wikipedia_grounding.py    # Le module : extraction, Wikipedia, Wikidata
app/generate_grounded.py      # Duplique generate_light.py + injection du grounding
app/pipeline_grounded.py      # Duplique pipeline.py, invoque generate_grounded
```

`pipeline.py` et `generate_light.py` restent intacts → rollback possible en changeant deux lignes dans `app.py`.

### 3.2 Flux par question

```
Question utilisateur
  │
  ├─► Modules : matching mots-clés (MODULES_MAX=1)
  │     Retourne : texte_modules + set_de_stems_matchés
  │
  ├─► Grounding Wikipedia (si GROUNDING_ENABLED=1)
  │     1. Extraction LLM (Mistral small) → liste de noms propres
  │     2. Pour chaque nom :
  │        a. Si couvert par un module dédié → skip (module = source de vérité)
  │        b. Sinon : appel Wikipedia REST FR
  │        c. Check Wikidata P31=Q5 (humain)
  │        d. Si OK : extract tronqué à 800 chars, ajouté au bloc [FAITS VÉRIFIÉS]
  │        e. Si KO (no_page, disambig, not_human) : ajouté au bloc [PERSONNES NON IDENTIFIÉES]
  │
  ├─► System prompt final = system_base + persona + modules + grounding
  │
  └─► Appel Mistral principal → chunks JSON
```

### 3.3 Extraction des noms — pourquoi LLM et pas regex

D'abord tentée en regex (pattern sur séquences capitalisées multi-mots). Échec sur :

- Minuscules : "tu connais **clément sénéchal** ?" → zéro détection
- Coquilles : "Anna Harendt" pour Hannah Arendt
- Noms simples : "Macron" (non multi-mot)
- Formulations variées : "et Sénéchal ?", "ton avis sur X"

Choix final : **Mistral small** (`mistral-small-latest`) en préprocessing, appelé avec `response_format=json_object`. Il extrait, corrige la casse, corrige les typos évidentes, distingue personne/concept. Regex conservée en filet de sécurité si l'API Mistral échoue.

Config : `GROUNDING_EXTRACT_MODEL` (défaut `mistral-small-latest`), `GROUNDING_EXTRACT_TIMEOUT` (défaut 8s).

### 3.4 Couverture par module dédié (anti-homonymie)

Problème détecté sur Romain Huet : Wikipedia renvoyait le rugbyman, le module `huet.txt` injectait le sociologue, le modèle fusionnait les deux.

Règle ajoutée : si un module matché a un **nom de fichier** dont un token apparaît dans le nom de la personne (par sous-chaîne), le module est considéré autoritaire et Wikipedia est sauté pour cette personne.

Exemples :
- `huet.txt` (stem `huet`) couvre "Romain Huet" → Wikipedia sauté ✓
- `letexier.txt` (stem `letexier`) couvre "Thibault Le Texier" via sous-chaîne `texier` ⊂ `letexier` ✓
- `dardot-laval.txt` (stem `dardot-laval`) couvre "Pierre Dardot" via `dardot` ⊂ `dardot-laval` ✓
- `politique.txt` (stem `politique`) ne couvre **pas** "Emmanuel Macron" → Wikipedia utilisé ✓

Distinction module dédié à une personne vs module thématique : préservée via les stems.

### 3.5 Cas "personne non identifiée"

Si le LLM extrait un nom et que ni module ni Wikipedia ne le couvrent (ex : Clément Sénéchal, pas de page WP, pas de module dédié), le grounding injecte un bloc explicite :

```
[PERSONNES NON IDENTIFIÉES — WIKIPEDIA VIDE]
Les personnes suivantes citées dans la question n'ont pas de page Wikipedia
exploitable : Clément Sénéchal.
Tu ne les connais pas. Applique strictement la règle [HONNÊTETÉ ET INCERTITUDE] :
un chunk sec du type {"chunks":["Ce nom ne me dit rien..."]}.
Pas d'analyse, pas de vibe, pas de gabarit politique.
```

Force le refus propre là où le modèle aurait hallucine.

---

## 4. Wiring production

### 4.1 Endpoints

| Endpoint | Pipeline | Grounding |
|---|---|---|
| `/api/ask` (audio production) | `pipeline_grounded.run()` | ✓ activé |
| `/api/generate` (test.html) | `pipeline_grounded.generate_chunks()` | ✓ activé |
| `/api/tts` (slave mode texte brut) | inchangé | — |

### 4.2 Variables d'environnement

| Var | Défaut | Effet |
|---|---|---|
| `GROUNDING_ENABLED` | `1` | `0` → pipeline grounded tourne comme light, pas d'appel Wikipedia |
| `GROUNDING_EXTRACT_MODEL` | `mistral-small-latest` | Modèle pour l'extraction des noms |
| `GROUNDING_EXTRACT_TIMEOUT` | `8.0` | Timeout de l'appel d'extraction |
| `DEBUG_LOG_DIR` | `logs/requests_grounded` | Répertoire des debug JSON du pipeline grounded |

### 4.3 Rollback

Deux lignes à changer dans `app.py` pour revenir au pipeline light :

```python
# /api/ask :
pipeline.run(...)            # au lieu de pipeline_grounded.run(...)
except pipeline.PipelineError # au lieu de pipeline_grounded.PipelineError

# /api/generate :
pipeline.generate_chunks(...)  # au lieu de pipeline_grounded.generate_chunks(...)
```

Puis `kill` + relance `start.sh`. Aucun fichier à supprimer.

---

## 5. Observabilité

Debug logs dans `logs/requests_grounded/<request_id>.debug.json` :

```json
{
  "pipeline": "grounded",
  "question": "...",
  "modules": {
    "matched_files": [{"file": "huet.txt", "hits": 2}],
    "text_chars": 1820
  },
  "grounding": {
    "enabled": true,
    "candidates": [
      {"name": "Romain Huet", "status": "covered_by_module"}
    ],
    "text_chars": 0
  },
  "tokens": {"prompt": 3218, "completion": 196, "total": 3414},
  "output": {"chunks_count": 4, "chunks": ["...", "..."]}
}
```

Le bloc `grounding.candidates` donne le statut de chaque nom détecté :
- `grounded` — faits Wikipedia injectés
- `covered_by_module` — couvert par module, Wikipedia sauté
- `no_page` — pas de page Wikipedia → refus forcé
- `disambiguation` — page d'homonymie → refus forcé
- `not_human` — entité non humaine → ignoré (pas de refus)
- `no_wikidata_id` / `no_extract` — cas limites → refus forcé

---

## 6. Coûts et latence

- **Extraction LLM** : ~200 input + 50 output tokens sur `mistral-small-latest`, ~0,005-0,01 centime par requête, ~300-500 ms
- **Wikipedia REST** : gratuit, ~200-500 ms par nom
- **Wikidata** : gratuit, ~100-300 ms par nom si humain
- **Latence cumulée** : +500 ms à +1,5 s sur une question avec 1 nom ; +1 à +3 s avec 3 noms
- **Coût marginal total** : négligeable, dominé par l'appel Mistral principal

---

## 7. Limites connues

1. **Pas de cache.** Une question répétée sur Macron refait deux appels Wikipedia + un Wikidata. À ajouter si le volume augmente (SQLite ou JSON local).
2. **Homonymes sur Wikipedia.** Si la personne cherchée a une page WP mais que c'est le mauvais homonyme (cas "Romain Huet rugbyman" sans le module), on peut injecter de mauvais faits. Mitigé par la couverture module, pas éliminé.
3. **Désambiguation.** Les pages de désambiguation sont traitées comme `unresolved` → refus forcé. Parfois trop strict (ex : "Macron" pourrait pointer vers une page de désambiguation selon l'état de WP).
4. **Noms non français.** Wikipedia FR peut ne pas avoir la page d'une personne étrangère peu connue en France. À étendre vers WP EN en fallback si besoin.
5. **Coquille non évidente.** Mistral-small corrige "Anna Harendt" → "Hannah Arendt", mais sur une coquille ambiguë, il peut laisser la forme d'origine → refus au lieu d'analyse correcte.

---

## 8. Historique des décisions

- **Avant grounding** : tentatives prompt-only (suppression de l'échappatoire, ajout de règles anti-fabulation, exemples positifs). Ont réduit les hallucinations dramatiques (Cerisy, trois bouteilles de vin) mais laissé passer les fabulations fines (Sorbonne vs CdF pour M-P).
- **Grounding booléen Wikipedia** (rejeté) : "la page existe → passe". Trop permissif : Sénéchal aurait une page donc passerait, et l'hallucination reviendrait. Rejeté au profit de l'injection des faits comme ancrage.
- **Whitelist** (considéré, rejeté) : maintenir une liste de personnes autorisées. Plus déterministe mais maintenance manuelle, oublis injustes. Rejeté au profit d'une approche dynamique (WP + module).
- **Détection par regex** (abandonné) : patterns sur noms capitalisés + cues de question françaises. Trop fragile sur les variations de formulation. Remplacé par extraction LLM.
- **Branchement production** : `/api/generate` d'abord (pour test isolé), puis `/api/ask` une fois les comportements validés sur Sénéchal, Badinter, Le Texier, Huet, Arendt, Derrida.
