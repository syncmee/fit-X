"""Sports-activity catalogue matching and MET-based calorie estimation.

The catalogue lives in app/data/activities.json (the dataset's activity_log
entries: runs, swims, boxing drills, court sports, ...). The coach prompt
receives matched entries the same way it receives food-database matches, and
the rules fallback uses them to estimate calories_burned instead of logging
None. Kept isolated from the coach package so the future RAG layer can
replace the matcher, not the callers.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _activities() -> list[dict]:
    with open(Path(__file__).parent / "data" / "activities.json", encoding="utf-8") as f:
        return json.load(f)


# Everyday words -> catalogue entry. Longest catalogue-name match always
# wins, so "running intervals" resolves specifically while a bare "run"
# falls back to the generic jog.
WORD_ALIASES = {
    "run": "Jogging", "runs": "Jogging", "ran": "Jogging", "running": "Jogging",
    "treadmill": "Jogging",
    "sprint": "Sprint Training", "sprints": "Sprint Training", "sprinting": "Sprint Training",
    "walk": "Brisk Walking", "walks": "Brisk Walking", "walked": "Brisk Walking",
    "walking": "Brisk Walking",
    "hike": "Hiking", "hiked": "Hiking", "hiking": "Hiking", "trek": "Hiking",
    "swim": "Swimming Laps", "swims": "Swimming Laps", "swam": "Swimming Laps",
    "swimming": "Swimming Laps", "laps": "Swimming Laps",
    "cycle": "Road Cycling", "cycled": "Road Cycling", "cycling": "Road Cycling",
    "bike": "Road Cycling", "biking": "Road Cycling", "ride": "Road Cycling",
    "box": "Boxing Conditioning", "boxing": "Boxing Conditioning", "boxed": "Boxing Conditioning",
    "bag work": "Heavy Bag Boxing", "punching bag": "Heavy Bag Boxing",
    "mma": "Mixed Martial Arts (MMA) Drills",
    "muay thai": "Muay Thai Training",
    "karate": "Karate Kata Practice",
    "taekwondo": "Taekwondo Drills",
    "football": "Football (Soccer)", "soccer": "Football (Soccer)",
    "basketball": "Basketball", "tennis": "Tennis", "badminton": "Badminton",
    "volleyball": "Volleyball", "pickleball": "Pickleball", "table tennis": "Table Tennis",
    "hiit": "High-Intensity Interval Training (HIIT)",
    "yoga": "Yoga Flow", "pilates": "Mat Pilates",
    "mobility": "Full-Body Mobility Flow", "stretching": "Full-Body Mobility Flow",
    "foam roll": "Foam Rolling", "foam rolling": "Foam Rolling",
    "jump rope": "Jump Rope Intervals", "skipping": "Jump Rope Intervals",
    "row": "Rowing Intervals", "rowing": "Rowing Intervals", "rowed": "Rowing Intervals",
    "stairs": "Stair Climbing", "stair climbing": "Stair Climbing",
    "zumba": "Zumba", "dance": "Dance Aerobics", "dancing": "Dance Aerobics",
    "kayak": "Kayaking", "kayaking": "Kayaking",
    "skating": "Roller Skating", "ice skating": "Ice Skating",
    "trail run": "Trail Running", "trail running": "Trail Running",
    "hill run": "Hill Running", "hill sprints": "Hill Running",
}


def _name_keys(entry: dict) -> list[str]:
    keys = {" ".join(entry["name"].lower().split())}
    for alias in entry.get("aliases") or []:
        keys.add(" ".join(str(alias).lower().split()))
    return [k for k in keys if len(k) >= 3]


def match_activity(message_text: str) -> dict | None:
    """Best catalogue activity named (or aliased) in the message text.

    Longest match wins so "running intervals" beats a bare "run"; returns
    the catalogue dict {name, met, ...} or None.
    """
    if not message_text:
        return None
    message = " ".join(message_text.lower().split())
    best: tuple[int, dict] | None = None
    for entry in _activities():
        for key in _name_keys(entry):
            if key in message and (best is None or len(key) > best[0]):
                best = (len(key), entry)
    for alias, name in WORD_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", message):
            entry = next((e for e in _activities() if e["name"] == name), None)
            if entry and (best is None or len(alias) > best[0]):
                best = (len(alias), entry)
    return best[1] if best else None


def estimate_calories(met: float, minutes: int, weight_kg: float | None) -> int | None:
    """MET-based burn: MET x bodyweight x hours. Returns None without a weight."""
    if not met or not minutes or minutes <= 0 or not weight_kg or weight_kg <= 0:
        return None
    return max(1, round(float(met) * float(weight_kg) * minutes / 60))


_DURATION_RE_HOUR = re.compile(r"\b(\d+(?:\.\d+)?)\s*(hour|hours|hr|hrs)\b")
_DURATION_RE_MIN = re.compile(r"\b(\d+)\s*(minute|minutes|min|mins)\b")


def extract_duration_minutes(text: str, default: int = 30) -> int:
    """Duration in minutes from phrases like '45 min' or '1 hour' (capped at 240)."""
    total = 0
    hour = _DURATION_RE_HOUR.search(text or "")
    minute = _DURATION_RE_MIN.search(text or "")
    if hour:
        total += int(float(hour.group(1)) * 60)
    if minute:
        total += int(minute.group(1))
    return min(total or default, 240)


def estimate_for_message(message_text: str, weight_kg: float | None, default_minutes: int = 30) -> dict | None:
    """Matched activity + MET burn estimate for a coach message, or None.

    Shape: {name, met, minutes, calories} — calories is None when the user
    has no weight on file, in which case the caller falls back to its own
    estimate.
    """
    entry = match_activity(message_text)
    if not entry:
        return None
    minutes = extract_duration_minutes(message_text, default=default_minutes)
    return {
        "name": entry["name"],
        "met": entry["met"],
        "minutes": minutes,
        "calories": estimate_calories(entry["met"], minutes, weight_kg),
    }
