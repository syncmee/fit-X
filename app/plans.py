"""Workout plan builder: maps a scheduled workout title to a structured session plan.

Plans are assembled from the bundled free-exercise-db dataset (app/data/exercises.json,
public domain / Unlicense) so reminder emails can attach a "here is what you should
hit" section without a live API call. Modality lines the dataset cannot supply —
shadow-boxing rounds, run intervals — come back as source="coach" and are the AI
coach's domain. build_plan() returns None for titles it cannot map, in which case
the email simply renders without a plan section.
"""

import json
from functools import lru_cache
from pathlib import Path

_IMAGE_BASE = "https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/exercises/"

# Muscles that count as "upper body" when picking gym exercises.
_UPPER_MUSCLES = {"chest", "lats", "middle back", "lower back", "shoulders", "triceps", "biceps", "traps", "forearms"}
# ...and "lower body".
_LOWER_MUSCLES = {"quadriceps", "hamstrings", "calves", "glutes", "adductors", "abductors"}


@lru_cache(maxsize=1)
def _exercises() -> list[dict]:
    with open(Path(__file__).parent / "data" / "exercises.json", encoding="utf-8") as f:
        return json.load(f)


def _resolve(*candidates: str) -> dict | None:
    """First exercise whose name equals a candidate, else the first name containing one."""
    names = {e["name"].lower(): e for e in _exercises()}
    for want in candidates:
        hit = names.get(want.lower())
        if hit:
            return hit
    for want in candidates:
        w = want.lower()
        for e in _exercises():
            if w in e["name"].lower():
                return e
    return None


def _slot(dose: str, *candidates: str) -> dict:
    e = _resolve(*candidates)
    if e is None:
        return {"name": candidates[0], "dose": dose, "source": "coach"}
    return {
        "name": e["name"],
        "dose": dose,
        "source": "free-exercise-db",
        "image": _IMAGE_BASE + e["images"][0] if e["images"] else None,
    }


def _block(title: str, focus: str, lines: list[dict]) -> dict:
    return {"block": title, "focus": focus, "exercises": lines}


def _boxing_plan() -> list[dict]:
    return [
        _block("Warm-up", "raise the pulse, loosen shoulders", [
            _slot("3 x 2 min", "Rope Jumping"),
            _slot("2 x 30 sec", "Star Jump"),
            _slot("2 x 45 sec", "Mountain Climbers"),
        ]),
        _block("Technique", "shadow work, stay light on your feet", [
            _slot("3 x 2 min rounds", "Shadow Boxing"),
        ]),
        _block("Main work", "heavy bag — 1 min rest between rounds", [
            _slot("6 x 3 min rounds", "Battling Ropes"),
        ]),
        _block("Finisher", "3 rounds, minimal rest", [
            _slot("45 sec", "Mountain Climbers"),
            _slot("12 reps", "Freehand Jump Squat"),
            _slot("45-60 sec", "Front Plank", "Plank"),
        ]),
        _block("Cool-down", "stretch what you hit", [
            _slot("2 x 30 sec each side", "Chest And Front Of Shoulder Stretch"),
            _slot("2 x 30 sec each side", "Overhead Triceps"),
            _slot("8-10 slow breaths", "Diaphragmatic Breathing"),
        ]),
    ]


def _running_plan() -> list[dict]:
    return [
        _block("Warm-up", "dynamic — save static stretching for after", [
            _slot("10 each way", "Ankle Circles"),
            _slot("10 each way", "Knee Circles"),
            _slot("10 each way", "Arm Circles"),
            _slot("2 x 20 sec", "Fast Skipping"),
        ]),
        _block("Main work", "intervals", [
            _slot("10 min easy", "Jogging, Treadmill", "Trail Running/Walking"),
            _slot("6 x 400 m fast / 200 m walk", "Intervals"),
            _slot("10 min easy", "Jogging, Treadmill"),
        ]),
        _block("Finisher", "runner stability", [
            _slot("3 x 45 sec", "Front Plank", "Plank"),
            _slot("3 x 30 sec each side", "Side Plank"),
        ]),
        _block("Cool-down", "static, post-run", [
            _slot("2 x 30 sec each leg", "90/90 Hamstring"),
            _slot("2 x 30 sec each leg", "Calf Stretch Hands Against Wall"),
            _slot("2 x 30 sec each leg", "All Fours Quad Stretch"),
            _slot("2 x 30 sec each side", "Kneeling Hip Flexor"),
        ]),
    ]


def _upper_body_plan() -> list[dict]:
    return [
        _block("Warm-up", "get the shoulders moving", [
            _slot("2 x 15", "Arm Circles"),
            _slot("2 x 10", "Dynamic Chest Stretch"),
            _slot("1 x 15 light", "Wide-Grip Lat Pulldown"),
        ]),
        _block("Main work", "push/pull pairs", [
            _slot("4 x 6-8", "Barbell Bench Press - Medium Grip"),
            _slot("4 x 6-8", "Pullups", "Wide-Grip Lat Pulldown"),
            _slot("3 x 8-10", "Seated Dumbbell Press", "Dumbbell Shoulder Press"),
            _slot("3 x 10", "Face Pull"),
        ]),
        _block("Accessories", "arms", [
            _slot("3 x 10-12", "Dumbbell Bicep Curl"),
            _slot("3 x 10-12", "Triceps Pushdown - Rope Attachment"),
        ]),
        _block("Cool-down", "stretch the presses and pulls away", [
            _slot("2 x 30 sec", "Behind Head Chest Stretch"),
            _slot("2 x 30 sec each side", "Overhead Lat"),
        ]),
    ]


def _lower_body_plan() -> list[dict]:
    return [
        _block("Warm-up", "wake the hips up", [
            _slot("2 x 10 each leg", "Bodyweight Walking Lunge"),
            _slot("10 each way", "Knee Circles"),
        ]),
        _block("Main work", "compound lifts first", [
            _slot("4 x 6-8", "Barbell Squat"),
            _slot("3 x 10", "Leg Press"),
            _slot("3 x 10 each leg", "Dumbbell Lunges"),
        ]),
        _block("Accessories", "hamstrings and calves", [
            _slot("3 x 10", "Lying Leg Curls"),
            _slot("3 x 15", "Barbell Seated Calf Raise"),
        ]),
        _block("Cool-down", "the walk-home will thank you", [
            _slot("2 x 30 sec each leg", "90/90 Hamstring"),
            _slot("2 x 30 sec each leg", "Lying Prone Quadriceps"),
            _slot("2 x 30 sec each side", "Kneeling Hip Flexor"),
        ]),
    ]


# Title-keyword → plan. First match wins; order matters ("kickboxing" → boxing, not legs).
_PLAN_MATCHERS = [
    (("boxing", "box", "bag", "sparring", "mma", "kickbox", "muay thai"), "Boxing", _boxing_plan),
    (("run", "jog", "sprint", "treadmill", "5k", "10k", "interval"), "Running", _running_plan),
    (("upper", "chest", "back", "shoulder", "arm", "bench", "push", "pull"), "Upper Body", _upper_body_plan),
    (("leg", "lower", "squat", "glute", "quad", "calf", "deadlift"), "Lower Body", _lower_body_plan),
]


def build_plan(title: str) -> dict | None:
    """Map a scheduled-workout title to {"name", "blocks"}, or None when no plan fits."""
    t = (title or "").lower()
    if not t.strip():
        return None
    for keywords, name, builder in _PLAN_MATCHERS:
        if any(k in t for k in keywords):
            return {"name": name, "blocks": builder()}
    return None
