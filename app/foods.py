"""Food catalogue matching used by the coach prompt and meal logging.

The catalogue is held in a startup-built inverted index (rebuilt only when
the Food table changes) instead of a substring scan: message tokens are
matched with word boundaries, scored BM25-style over IDF weights, plural
forms are stripped, and typos are caught with rapidfuzz. The old 300 s TTL
reload and the bidirectional substring matching in resolve_food are gone —
"eggplant" can no longer match "egg" and "roti" no longer hits inside
"roti filler". Matching stays isolated here so the future PostgreSQL/RAG
implementation is a replacement of this module, not another dashboard
concern.
"""
from __future__ import annotations

import json
import math
import re

from rapidfuzz import fuzz, process
from flask import current_app
from sqlalchemy import func, select

from .extensions import db
from .models import Food

# ============================================================
# Tokenization: word boundaries + plural stripping
# ============================================================

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Filler words that must never anchor a food match ("dosa with sambar").
_STOPWORDS = {
    "a", "an", "the", "of", "with", "and", "for", "had", "have", "has", "ate",
    "eat", "eaten", "some", "my", "in", "on", "at", "to", "today", "now",
    "just", "was", "were", "is", "are", "i", "we", "you", "it", "this", "that",
    "cup", "cups", "glass", "glasses", "bowl", "bowls", "plate", "plates",
    "kcal", "calories", "grams", "gram", "two", "three", "four", "five",
    "one", "half", "small", "large", "medium", "big", "little", "piece",
    "pieces", "scoop", "scoops", "tbsp", "tsp", "bottle", "can",
}


def _raw_tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _singular(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        # "chapaties" -> "chapati" (Indian plurals beat English -y here)
        return token[:-3] + "i"
    if len(token) > 3 and token.endswith("es") and token[:-2].endswith(("s", "x", "z", "ch", "sh")):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _phrase(text: str) -> str:
    """Normalized phrase: lowercase words, plurals stripped."""
    return " ".join(_singular(t) for t in _raw_tokens(text))


def _content_tokens(text: str) -> list[str]:
    """Singularized tokens minus stopwords and bare numbers — the matchable
    part of a text (quantities belong to the meal parser, not the matcher)."""
    return [
        _singular(t)
        for t in _raw_tokens(text)
        if t not in _STOPWORDS and not t.isdigit()
    ]


# ============================================================
# Index: built once, rebuilt only when the Food table changes
# ============================================================

_INDEX: dict = {"app": None, "signature": None, "index": None}

# BM25-lite knobs
_FUZZY_MIN_RATIO = 82
_FUZZY_MIN_TOKEN_LEN = 4
_FUZZY_WEIGHT = 0.75
_MAX_WINDOW = 4
# Minimum score for a window/entry pair to count as a match.
_MATCH_THRESHOLD = 2.5


def invalidate_food_index() -> None:
    """Drop the cached index (call after any Food row create/update/delete)."""
    _INDEX["app"] = None
    _INDEX["signature"] = None
    _INDEX["index"] = None


def _table_signature() -> tuple:
    return db.session.execute(select(func.count(Food.id), func.max(Food.id))).one()


def _entry_from_food(food: Food) -> dict:
    try:
        unit_grams = json.loads(food.unit_grams or "{}")
    except json.JSONDecodeError:
        unit_grams = {}
    return {
        "name": food.name,
        "serving": food.serving,
        "calories": food.calories,
        "protein_g": food.protein,
        "carbs_g": food.carbs,
        "fats_g": food.fats,
        "unit_grams": unit_grams,
    }


def _catalogue_keys(name: str, aliases: list[str]) -> set[str]:
    """Indexable keys for a catalogue entry: the name, its aliases, plus the
    derived alternates the old matcher relied on — comma heads ("Dal, cooked"
    -> "Dal"), slash splits ("Chapati/Roti") and bracket contents
    ("Hot tea (Garam Chai)" -> "Garam Chai")."""
    keys = {name}
    keys.update(a for a in aliases if isinstance(a, str) and a.strip())
    head = re.split(r"[,(]", name)[0].strip()
    if len(head) >= 3:
        keys.add(head)
    for side in re.split(r"[/()]", name):
        side = side.strip()
        if len(side) >= 3:
            keys.add(side)
    return keys


def _build_index() -> dict:
    rows = db.session.scalars(select(Food).order_by(Food.id)).all()
    entries: list[dict] = []
    phrases: list[tuple[str, list[str]]] = []  # (phrase, tokens) per entry key
    phrase_entry: list[int] = []
    exact: dict[str, int] = {}  # normalized phrase -> phrase idx (first wins)
    postings: dict[str, set[int]] = {}  # token -> phrase idxs

    for idx, food in enumerate(rows):
        entries.append(_entry_from_food(food))
        try:
            aliases = json.loads(food.aliases or "[]")
        except json.JSONDecodeError:
            aliases = []
        for key in _catalogue_keys(food.name, aliases):
            phrase = _phrase(key)
            if not phrase:
                continue
            tokens = sorted(set(phrase.split()))
            phrase_idx = len(phrases)
            phrases.append((phrase, tokens))
            phrase_entry.append(idx)
            exact.setdefault(phrase, phrase_idx)
            for token in tokens:
                postings.setdefault(token, set()).add(phrase_idx)

    df: dict[str, int] = {}
    for _, tokens in phrases:
        for token in tokens:
            df[token] = df.get(token, 0) + 1
    n_phrases = max(len(phrases), 1)
    idf = {token: math.log(1 + n_phrases / df_count) for token, df_count in df.items()}
    avg_len = sum(len(tokens) for _, tokens in phrases) / n_phrases

    return {
        "entries": entries,
        "phrases": phrases,
        "phrase_entry": phrase_entry,
        "exact": exact,
        "postings": postings,
        "idf": idf,
        "avg_len": avg_len,
        # Flat token list for rapidfuzz typo correction of unseeded tokens.
        "tokens": sorted(postings.keys()),
    }


def _index() -> dict:
    app_ref = current_app._get_current_object()
    signature = _table_signature()
    if (
        _INDEX["app"] is app_ref
        and _INDEX["signature"] == signature
        and _INDEX["index"] is not None
    ):
        return _INDEX["index"]
    index = _build_index()
    _INDEX.update(app=app_ref, signature=signature, index=index)
    return index


# ============================================================
# Scoring: BM25-lite over window/entry token overlap + rapidfuzz typos
# ============================================================

def _score_window(window: list[str], entry_tokens: list[str], idf: dict[str, float],
                  avg_len: float, allow_fuzzy: bool = True,
                  corrections: dict[str, str] | None = None) -> float:
    if not window:
        return 0.0
    corrections = corrections or {}
    score = 0.0
    hits = 0
    for token in window:
        # Typo corrections from the seeding pass count as exact matches.
        corrected = corrections.get(token)
        if token in entry_tokens or (corrected and corrected in entry_tokens):
            score += idf.get(corrected or token, 1.0)
            hits += 1
            continue
        if allow_fuzzy and len(token) >= _FUZZY_MIN_TOKEN_LEN:
            best_ratio = 0.0
            best_token = None
            for entry_token in entry_tokens:
                if abs(len(entry_token) - len(token)) > 3:
                    continue
                ratio = fuzz.ratio(token, entry_token)
                if ratio >= _FUZZY_MIN_RATIO and ratio > best_ratio:
                    best_ratio = ratio
                    best_token = entry_token
            if best_token:
                score += idf.get(best_token, 1.0) * _FUZZY_WEIGHT * (best_ratio / 100.0)
                hits += 1
    if hits == 0:
        return 0.0
    # BM25 length normalization: longer phrases must earn their score.
    norm = (len(window) + 1.5) / (len(window) + 1.5 + 0.75 * (len(entry_tokens) - len(window)) / max(avg_len, 1.0))
    return score * max(norm, 0.3)


def _corrections_for(tokens: list[str], index: dict, seen: dict) -> dict[str, str]:
    """Typo corrections for tokens that hit no postings: rapidfuzz against
    the catalogue token vocabulary (only for substantial tokens, cached per
    message). Returns {token: corrected_catalogue_token}."""
    out: dict[str, str] = {}
    catalogue = index["tokens"]
    for token in tokens:
        if token in index["postings"] or token in seen:
            continue
        seen[token] = None
        if len(token) < _FUZZY_MIN_TOKEN_LEN:
            continue
        match = process.extractOne(token, catalogue, scorer=fuzz.ratio,
                                   score_cutoff=80)
        if match:
            corrected = match[0]
            out[token] = corrected
            seen[token] = corrected
    return out


def _window_scores(message: str, index: dict) -> list[tuple[float, int, str]]:
    """All (score, phrase_idx, matched_span) pairs above threshold, best first."""
    idf = index["idf"]
    avg_len = index["avg_len"]
    postings = index["postings"]
    exact = index["exact"]
    phrases = index["phrases"]
    tokens = _content_tokens(message)
    if not tokens:
        return []

    # Candidate phrase indices: anything sharing a token with the message,
    # plus rapidfuzz corrections for tokens that hit nothing.
    candidates: set[int] = set()
    corrections = _corrections_for(tokens, index, {})
    for token in tokens:
        candidates.update(postings.get(token, ()))
        corrected = corrections.get(token)
        if corrected:
            candidates.update(postings.get(corrected, ()))
    if not candidates:
        return []

    results: list[tuple[float, int, str]] = []
    seen_exact: set[int] = set()
    exact_spans: list[tuple[int, int]] = []
    n = len(tokens)
    for start in range(n):
        for end in range(start + 1, min(start + _MAX_WINDOW, n) + 1):
            # Skip windows fully contained in an already-exact span ("dosa"
            # inside a matched "masala dosa") — they only cost time.
            if any(s <= start and end <= e for s, e in exact_spans):
                continue
            window = tokens[start:end]
            span = " ".join(window)
            # Exact catalogue phrase mentioned verbatim — near-certain hit.
            hit = exact.get(span)
            if hit is not None and hit not in seen_exact:
                seen_exact.add(hit)
                exact_spans.append((start, end))
                results.append((1000.0 + sum(idf.get(t, 1.0) for t in phrases[hit][1]), hit, span))
            # Union, not intersection: a typo'd token has no postings, so the
            # fuzzy match must be allowed through by the remaining tokens.
            # Longer windows only serve the exact-phrase lookup above —
            # scoring them multiplies the cost for near-zero recall.
            if len(window) > 2:
                continue
            window_union: set[int] = set()
            for token in window:
                window_union.update(postings.get(token, ()))
                corrected = corrections.get(token)
                if corrected:
                    window_union.update(postings.get(corrected, ()))
            relevant = candidates & window_union
            if not relevant:
                continue
            allow_fuzzy = False if corrections else len(window) <= 2
            for phrase_idx in relevant:
                if phrase_idx in seen_exact:
                    continue
                _phrase_text, etokens = phrases[phrase_idx]
                score = _score_window(window, etokens, idf, avg_len, allow_fuzzy, corrections)
                if score >= _MATCH_THRESHOLD:
                    results.append((score, phrase_idx, span))
    results.sort(key=lambda item: -item[0])
    return results


# ============================================================
# Public API (same shapes as before)
# ============================================================

def food_db_matches(message_text: str, limit: int = 8) -> list[dict]:
    """Catalogue entries named in the message text, best first."""
    if not message_text:
        return []
    index = _index()
    pairs = _window_scores(message_text, index)
    if not pairs:
        return []
    out: list[dict] = []
    seen: set[int] = set()
    for _score, phrase_idx, _span in pairs:
        entry_idx = index["phrase_entry"][phrase_idx]
        if entry_idx in seen:
            continue
        seen.add(entry_idx)
        entry = dict(index["entries"][entry_idx])
        entry["score"] = round(_score, 1)
        out.append(entry)
        if len(out) >= limit:
            break
    return out


def resolve_food(name: str | None) -> dict | None:
    """Best catalogue match for a coach action's food name.

    Exact normalized-phrase match wins; otherwise the highest-scoring entry
    over token overlap + typo tolerance. No substring matching either way —
    "chicken momo" will not resolve to "Chicken consomme".
    """
    needle = (name or "").strip()
    if not needle:
        return None
    index = _index()
    phrase = _phrase(needle)
    if not phrase:
        return None
    hit = index["exact"].get(phrase)
    if hit is not None:
        return index["entries"][index["phrase_entry"][hit]]
    pairs = _window_scores(phrase, index)
    if not pairs:
        return None
    return index["entries"][index["phrase_entry"][pairs[0][1]]]


# ============================================================
# AI meal-action sanity limits
#
# The coach model estimates calories itself, and models occasionally
# misread a quantity ("2 eggs" -> 20). The app, not the model, decides
# what gets logged: quantities above a per-food ceiling and entries above
# the calorie cap are rejected and surfaced as a confirmation question
# instead of being clamped to a magic number. An explicit user
# confirmation relaxes the limit up to the hard caps, never past them.

# Per-food ceilings for one meal, keyed by a word in the (lowercased) food
# name. "egg" also matches "eggs" and "boiled eggs" but not "eggplant".
PER_UNIT_CEILINGS = {
    "egg": 10,
    "roti": 12,
    "chapati": 12,
    "phulka": 12,
    "paratha": 8,
    "dosa": 8,
    "idli": 12,
    "puri": 10,
    "banana": 10,
    "bread": 12,
    "momos": 15,
    "apple": 8,
    "orange": 8,
}

# One AI-logged meal entry may not exceed this without explicit user
# confirmation; nothing at all gets past the hard cap.
MEAL_ENTRY_CALORIE_CAP = 1500
MEAL_ENTRY_HARD_CAP = 3000

_MASS_UNIT_WORDS = {"g", "gram", "grams", "ml", "millilitre", "milliliter",
                    "kg", "kilogram", "kilograms", "l", "litre", "liter",
                    "litres", "liters"}
_KG_WORDS = {"kg", "kgs", "kilogram", "kilograms"}
_L_WORDS = {"l", "litre", "liter", "litres", "liters"}


def unit_ceiling_for(name: str | None) -> int | None:
    """Ceiling for a food name, or None when the food has no ceiling."""
    lowered = " ".join((name or "").lower().split())
    if not lowered:
        return None
    for keyword, ceiling in PER_UNIT_CEILINGS.items():
        if re.search(rf"\b{re.escape(keyword)}s?\b", lowered):
            return ceiling
    return None


def compute_meal_from_catalogue(food: dict | None, quantity: float, unit: str) -> dict | None:
    """Scale a catalogue food to the portion the coach action describes.

    Priority: unit_grams ("2 rotis" with {"roti": 40} on a per-100 g entry
    scales by 2 x 40 g). Mass/volume units scale by the parsed serving
    weight; countable units ("3 idli") treat one unit as one catalogue
    serving, which matches how the curated catalogue is authored. Returns
    None when the portion cannot be derived, so the caller falls back to
    the model's own estimate under the calorie cap.
    """
    if not food or quantity is None or quantity <= 0:
        return None
    unit = " ".join((unit or "serving").lower().split()) or "serving"
    if unit in _KG_WORDS:
        quantity *= 1000
        unit = "grams"
    elif unit in _L_WORDS:
        quantity *= 1000
        unit = "ml"

    multiplier = None
    # unit_grams: quantity of a named unit x its gram weight, on top of a
    # per-100 g base ("2 rotis" x 40 g / 100 g).
    unit_grams = food.get("unit_grams") or {}
    if isinstance(unit_grams, str):
        try:
            unit_grams = json.loads(unit_grams)
        except json.JSONDecodeError:
            unit_grams = {}
    if unit_grams and _serving_grams(food.get("serving")) == 100.0:
        singular_unit = _singular(unit)
        for candidate in (unit, singular_unit, singular_unit.rstrip("s")):
            if candidate in unit_grams:
                multiplier = quantity * unit_grams[candidate] / 100.0
                break

    if multiplier is None:
        if unit in _MASS_UNIT_WORDS:
            per_serving = _serving_grams(food.get("serving"))
            if not per_serving:
                return None
            multiplier = quantity / per_serving
        else:
            multiplier = quantity
    if multiplier <= 0 or multiplier > 50:
        return None
    return {
        "calories": max(1, round(food["calories"] * multiplier)),
        "protein": round(food["protein_g"] * multiplier),
        "carbs": round(food["carbs_g"] * multiplier),
        "fats": round(food["fats_g"] * multiplier),
    }


def _serving_grams(serving: str | None) -> float | None:
    """Grams (or ml, treated as grams) in a serving string like '1 medium (40 g)'."""
    if not serving:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*(kg|g|ml|l)\b", serving.lower())
    if not match:
        return None
    value = float(match.group(1))
    if match.group(2) == "kg":
        value *= 1000
    elif match.group(2) == "l":
        value *= 1000
    return value or None
