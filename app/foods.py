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
