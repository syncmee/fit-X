"""Prototype: session title -> workout plan from the local free-exercise-db.

Stands in for the plan step of the reminder-email feature: given what the user
scheduled ("boxing tomorrow", "running", "upper body"), resolve a structured
plan against exercises.json, marking anything the DB cannot cover as
coach-layer content. Output is JSON-serializable so it can later be cached on
the ScheduledWorkout row and rendered into the email.
"""

import json
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "app" / "data" / "exercises.json"
IMAGE_BASE = "https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/exercises/"

# Muscles that count as "upper body" for the gym session.
UPPER_MUSCLES = {"chest", "lats", "middle back", "lower back", "shoulders", "triceps", "biceps", "traps", "forearms"}


def load_db():
    with open(DB_PATH, encoding="utf-8") as f:
        return json.load(f)


def resolve(db, *candidates):
    """Return the first exercise whose name matches any candidate, else None."""
    for want in candidates:
        for e in db:
            if e["name"].lower() == want.lower():
                return e
        for e in db:
            if want.lower() in e["name"].lower():
                return e
    return None


def slot(db, dose, *candidates):
    """Build one plan line: DB entry + coach-prescribed dose."""
    e = resolve(db, *candidates)
    if e is None:
        return {"name": candidates[0], "dose": dose, "source": "coach (not in exercise DB)"}
    return {
        "name": e["name"],
        "dose": dose,
        "source": "free-exercise-db",
        "primaryMuscles": e["primaryMuscles"],
        "secondaryMuscles": e["secondaryMuscles"],
        "level": e["level"],
        "equipment": e["equipment"],
        "image": IMAGE_BASE + e["images"][0] if e["images"] else None,
    }


def block(title, focus, lines):
    return {"block": title, "focus": focus, "exercises": lines}


def boxing_plan(db):
    """DB covers jump rope, plyo, ropes, core, stretches; bag/shadow work is coach layer."""
    return {
        "title": "Boxing",
        "duration_minutes": 60,
        "hits": ["shoulders", "triceps", "core", "calves", "cardio"],
        "blocks": [
            block("Warm-up", "raise the pulse, loosen shoulders", [
                slot(db, "3 x 2 min", "Rope Jumping"),
                slot(db, "2 x 30 sec", "Star Jump"),
                slot(db, "2 x 45 sec", "Mountain Climbers"),
            ]),
            block("Technique", "shadow work - coach layer (no boxing entries in the DB)", [
                slot(db, "3 x 2 min rounds", "Shadow Boxing"),
            ]),
            block("Main work", "heavy bag - coach layer round plan on DB bag-work base", [
                slot(db, "6 x 3 min rounds, 1 min rest", "Battling Ropes"),
            ]),
            block("Conditioning finisher", "3 rounds, minimal rest", [
                slot(db, "45 sec", "Mountain Climbers"),
                slot(db, "12 reps", "Freehand Jump Squat"),
                slot(db, "45-60 sec", "Front Plank", "Plank"),
            ]),
            block("Cool-down", "stretch what you hit", [
                slot(db, "2 x 30 sec each side", "Chest And Front Of Shoulder Stretch"),
                slot(db, "2 x 30 sec each side", "Overhead Triceps"),
                slot(db, "8-10 slow breaths", "Diaphragmatic Breathing"),
            ]),
        ],
    }


def running_plan(db):
    """DB covers drills, treadmill/cardio entries, core, stretches; interval structure is coach layer."""
    return {
        "title": "Running",
        "duration_minutes": 45,
        "hits": ["quads", "hamstrings", "calves", "core", "cardio"],
        "blocks": [
            block("Warm-up", "dynamic, no static stretching yet", [
                slot(db, "10 each way", "Ankle Circles"),
                slot(db, "10 each way", "Knee Circles"),
                slot(db, "10 each way", "Arm Circles"),
                slot(db, "2 x 20 sec", "Fast Skipping"),
            ]),
            block("Main work", "intervals - coach layer; DB has the cardio entries", [
                slot(db, "10 min easy jog", "Jogging, Treadmill", "Trail Running/Walking"),
                {"name": "Intervals", "dose": "6 x 400 m fast / 200 m walk", "source": "coach"},
                slot(db, "10 min easy jog", "Jogging, Treadmill"),
            ]),
            block("Core finisher", "runner stability", [
                slot(db, "3 x 45 sec", "Front Plank", "Plank"),
                slot(db, "3 x 30 sec each side", "Side Plank"),
            ]),
            block("Cool-down", "static, post-run", [
                slot(db, "2 x 30 sec each leg", "90/90 Hamstring"),
                slot(db, "2 x 30 sec each leg", "Calf Stretch Hands Against Wall"),
                slot(db, "2 x 30 sec each leg", "All Fours Quad Stretch"),
                slot(db, "2 x 30 sec each side", "Kneeling Hip Flexor"),
            ]),
        ],
    }


def upper_body_plan(db):
    """Fully DB-backed: push/pull pairs across chest, back, shoulders, arms."""
    return {
        "title": "Gym - Upper Body",
        "duration_minutes": 60,
        "hits": ["chest", "lats", "shoulders", "triceps", "biceps"],
        "blocks": [
            block("Warm-up", "get the shoulders moving", [
                slot(db, "2 x 15", "Arm Circles"),
                slot(db, "2 x 10", "Dynamic Chest Stretch"),
                slot(db, "1 x 15 light", "Wide-Grip Lat Pulldown"),
            ]),
            block("Main work", "push/pull pairs, 3 sets each", [
                slot(db, "4 x 6-8", "Barbell Bench Press - Medium Grip"),
                slot(db, "4 x 6-8", "Pullups", "Wide-Grip Lat Pulldown"),
                slot(db, "3 x 8-10", "Seated Dumbbell Press", "Dumbbell Shoulder Press"),
                slot(db, "3 x 10", "Face Pull"),
            ]),
            block("Accessories", "arms, 2-3 sets", [
                slot(db, "3 x 10-12", "Dumbbell Bicep Curl"),
                slot(db, "3 x 10-12", "Triceps Pushdown - Rope Attachment"),
            ]),
            block("Cool-down", "stretch the presses and pulls away", [
                slot(db, "2 x 30 sec", "Behind Head Chest Stretch"),
                slot(db, "2 x 30 sec each side", "Overhead Lat"),
            ]),
        ],
    }


def print_plan(p):
    print(f"\n{'=' * 62}\n{p['title']} - tomorrow ({TOMORROW}) - {p['duration_minutes']} min\nhits: {', '.join(p['hits'])}")
    for b in p["blocks"]:
        print(f"\n  {b['block']} - {b['focus']}")
        for ex in b["exercises"]:
            mark = "" if ex["source"] == "free-exercise-db" else "  [coach layer]"
            print(f"    - {ex['name']:<42} {ex['dose']}{mark}")


TOMORROW = str(date.today() + timedelta(days=1))

if __name__ == "__main__":
    db = load_db()
    plans = [boxing_plan(db), running_plan(db), upper_body_plan(db)]
    for p in plans:
        print_plan(p)
    out = Path(__file__).parent / "plan_prototype_output.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"date": TOMORROW, "plans": plans}, f, indent=2)
    n_db = sum(1 for p in plans for b in p["blocks"] for e in b["exercises"] if e["source"] == "free-exercise-db")
    n_coach = sum(1 for p in plans for b in p["blocks"] for e in b["exercises"] if e["source"] != "free-exercise-db")
    print(f"\n{'=' * 62}\n{len(plans)} plans written to {out.name}: {n_db} exercises from DB, {n_coach} coach-layer lines")
