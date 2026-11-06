"""Food catalogue matching used by the coach prompt.

SQLite remains the fallback matcher.  Keeping it isolated makes the later
PostgreSQL trigram/full-text implementation a replacement of this module,
rather than another dashboard concern.
"""
from __future__ import annotations

import json
import re
from time import monotonic

from flask import current_app
from sqlalchemy import select

from .extensions import db
from .models import Food


_CACHE: dict = {"app": None, "entries": None, "loaded": 0.0}
_CACHE_TTL_SECONDS = 300


def food_name_keys(name: str, aliases: str | list[str] | None = None) -> list[str]:
    """Return safe, human-readable aliases derived from a catalogue name."""
    lowered = " ".join(name.lower().split())
    keys = {lowered}
    head = re.split(r"[,(]", lowered)[0].strip()
    if head:
        keys.add(head)
    for side in re.split(r"[/()]", lowered):
        if side.strip():
            keys.add(side.strip())
    for match in re.findall(r"\(([^()]+)\)", lowered):
        if " " in match:
            keys.add(match.strip())
    if isinstance(aliases, str):
        try:
            aliases = json.loads(aliases)
        except json.JSONDecodeError:
            aliases = []
    if isinstance(aliases, list):
        keys.update(" ".join(str(alias).lower().split()) for alias in aliases)
    return [key for key in keys if len(key) >= 3]


def _catalogue_entries() -> list[tuple[list[str], dict]]:
    app_ref = current_app._get_current_object()
    now = monotonic()
    if _CACHE["app"] is not app_ref or _CACHE["entries"] is None or now - _CACHE["loaded"] > _CACHE_TTL_SECONDS:
        _CACHE.update(
            app=app_ref,
            entries=[
                (food_name_keys(food.name, food.aliases), {
                    "name": food.name, "serving": food.serving, "calories": food.calories,
                    "protein_g": food.protein, "carbs_g": food.carbs, "fats_g": food.fats,
                })
                for food in db.session.scalars(select(Food).order_by(Food.name)).all()
            ],
            loaded=now,
        )
    return _CACHE["entries"]


def food_db_matches(message_text: str, limit: int = 8) -> list[dict]:
    """Match catalogue aliases in message text, preferring longest matches."""
    if not message_text:
        return []
    message = " ".join(message_text.lower().split())
    matched = []
    for keys, food in _catalogue_entries():
        hit = max((len(key) for key in keys if key in message), default=0)
        if hit:
            matched.append((hit, food))
    matched.sort(key=lambda item: -item[0])
    return [food for _, food in matched[:limit]]


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


def resolve_food(name: str | None) -> dict | None:
    """Best catalogue match for a coach action's food name."""
    needle = " ".join((name or "").lower().split())
    if len(needle) < 3:
        return None
    best = None
    best_len = 0
    for keys, food in _catalogue_entries():
        for key in keys:
            if key == needle:
                return food
            if (key in needle or needle in key) and len(key) > best_len:
                best = food
                best_len = len(key)
    return best


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


def compute_meal_from_catalogue(food: dict | None, quantity: float, unit: str) -> dict | None:
    """Scale a catalogue food to the portion the coach action describes.

    Mass/volume units scale by parsed serving weight; countable units
    ("2 rotis", "3 idli") treat one unit as one catalogue serving, which
    matches how the curated catalogue is authored. Returns None when the
    portion cannot be derived, so the caller falls back to the model's
    own estimate under the calorie cap.
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
