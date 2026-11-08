"""One-time normalizer: Fit-X English exercise dataset -> app catalogs.

Reads the user-provided dataset (fitx_exercises_english.json) and writes:
  app/data/exercises.json.new  - normalized full catalog (review, then swap)
  app/data/activities.json     - the loggable sports activities (activity_log tag)

Run with an isolated interpreter:  python -I scripts/normalize_exercises.py
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = Path(r"C:\Users\syNc\Downloads\fitx_exercises_english.json")
OUT_EXERCISES = REPO / "app" / "data" / "exercises.json.new"
OUT_ACTIVITIES = REPO / "app" / "data" / "activities.json"

# ---------------------------------------------------------------------------
# Vocabulary maps
# ---------------------------------------------------------------------------

# Fold loose muscle strays onto their anatomical parent (kept: gluteus_medius
# etc. are already anatomical). Anything not listed here must be an accepted
# canonical name. "full_body" is dropped when other muscles exist but is kept
# when it is the only muscle (whole-body activities like Foam Rolling).
MUSCLE_FOLD = {
    "glutes": "gluteus_maximus",
    "hips": "hip_flexors",
    "shoulders": "anterior_deltoid",
    "arms": "biceps_brachii",
    "back": "latissimus_dorsi",
    "chest": "pectoralis_major",
    "core": "rectus_abdominis",
    "calves": "gastrocnemius",
    "forearms": "forearm_flexors",
}
MUSCLE_DROP = set()

LEVEL_MAP = {"beginner": "beginner", "intermediate": "intermediate", "advanced": "expert"}

CATEGORY_MAP = {
    "strength": "strength",
    "stretching": "stretching",
    "cardio": "cardio",
    "olympic": "olympic",
    "plyometrics": "plyometrics",
    "sports": "sports",
    "flexibility": "stretching",
    "recovery": "stretching",
}

# Fine-grained equipment -> coarse group used for environment gating.
# The original equipment value is preserved on the record for search/RAG.
EQUIP_GROUP = {
    # free weights
    "barbell": "barbell", "trap_bar": "barbell", "plates": "barbell",
    "dumbbell": "dumbbell",
    "kettlebell": "kettlebell",
    "ez_bar": "ez_bar",
    "cable": "cable",
    "flat_bench": "flat_bench",
    # bands / balls
    "loop_band": "bands", "resistance_band": "bands",
    "medicine_ball": "medicine_ball", "slam_ball": "medicine_ball", "wall_ball": "medicine_ball",
    "stability_ball": "stability_ball",
    "bosu_ball": "bosu_ball",
    # machines
    "machine": "machine",
    "smith_machine": "machine", "leg_press": "machine", "leg_curl": "machine",
    "leg_extension": "machine", "dip_machine": "machine", "lat_pulldown_machine": "machine",
    "hack_squat": "machine", "standing_calf_raise_machine": "machine",
    "shoulder_press_machine": "machine", "plate_loaded_lateral_raise_machine": "machine",
    "assisted_pullup_machine": "machine", "chest_press_machine": "machine",
    "seated_row_machine": "machine", "hip_abduction_machine": "machine",
    "hip_adduction_machine": "machine", "back_extension_machine": "machine",
    "bicep_curl_machine": "machine", "chest_fly_machine": "machine",
    "preacher_curl_machine": "machine", "reverse_fly_machine": "machine",
    "ab_crunch_machine": "machine", "tricep_extension_machine": "machine",
    "glute_ham_developer": "machine", "pec_deck": "machine",
    "donkey_calf_raise_machine": "machine", "hip_thrust_machine": "machine",
    "plate_loaded_dip_machine": "machine", "shrug_machine": "machine",
    "seated_calf_raise_machine": "machine",
    # bodyweight rigging
    "pull_up_bar": "pull_up_bar", "climbing_rope": "pull_up_bar",
    "dip_station": "dip_station",
    "ab_wheel": "ab_wheel",
    "jump_rope": "jump_rope",
    # cardio
    "treadmill": "cardio_machine", "elliptical": "cardio_machine",
    "stationary_bike": "cardio_machine", "air_bike": "cardio_machine",
    "spin_bike": "cardio_machine", "recumbent_bike": "cardio_machine",
    "bicycle_or_stationary_bike": "cardio_machine",
    "rower": "cardio_machine", "rowing_machine": "cardio_machine",
    "ski_erg": "cardio_machine", "stair_climber": "cardio_machine",
    # sports / outdoor gear
    "running_shoes": "sports_gear", "walking_shoes": "sports_gear",
    "hiking_shoes": "sports_gear", "boxing_shoes": "sports_gear",
    "pool": "sports_gear", "kickboard": "sports_gear", "swim_goggles_and_buoy": "sports_gear",
    "football": "sports_gear", "basketball": "sports_gear", "volleyball": "sports_gear",
    "tennis_racket": "sports_gear", "badminton_racket": "sports_gear",
    "pickleball_paddle": "sports_gear", "table_tennis_paddle": "sports_gear",
    "cones": "sports_gear", "agility_ladder": "sports_gear",
    "bicycle": "sports_gear", "mountain_bike": "sports_gear", "stairs": "sports_gear",
    "boxing_heavy_bag": "sports_gear", "speed_bag": "sports_gear", "focus_mitts": "sports_gear",
    "boxing_equipment": "sports_gear", "boxing_protective_gear": "sports_gear",
    "kickboxing_equipment": "sports_gear", "muay_thai_equipment": "sports_gear",
    "mma_protective_gear": "sports_gear",
    "sledgehammer_and_tire": "sports_gear", "kayak_and_paddle": "sports_gear",
    "rowing_boat": "sports_gear", "roller_skates": "sports_gear", "ice_skates": "sports_gear",
    # bodyweight
    "none": "bodyweight", "bodyweight": "bodyweight",
    "bodyweight_or_equipment": "bodyweight", "yoga_mat": "bodyweight",
    # misc gym gear
    "battle_rope": "other", "plyo_box": "other", "sled": "other", "sandbag": "other",
    "suspension_trainer": "other", "rings": "other", "step_platform": "other",
    "foam_roller": "other", "wrist_roller": "other", "varied_equipment": "other",
}

# Exercises / activities that keep a NULL equipment value.
def null_equipment_group(ex: dict) -> str:
    if ex.get("isBodyweight") or ex.get("category") in {"stretching", "flexibility", "recovery"}:
        return "bodyweight"
    return "other"


KEEP_FIELDS = [
    "id", "name", "description", "force", "level", "mechanic", "equipment",
    "primaryMuscles", "secondaryMuscles", "instructions", "tips", "category",
    "bodyPart", "goals", "tags", "met", "variationGroup", "isUnilateral",
    "isBodyweight", "source",
]

warnings: list[str] = []


def normalize_muscles(raw: list | None, ex_name: str) -> list[str]:
    out: list[str] = []
    for m in raw or []:
        if m in MUSCLE_DROP:
            warnings.append(f"{ex_name}: dropped muscle '{m}'")
            continue
        m2 = MUSCLE_FOLD.get(m, m)
        if m2 not in out:
            out.append(m2)
    if "full_body" in out and len(out) > 1:
        out = [m for m in out if m != "full_body"]
    return out


def normalize_exercise(ex: dict) -> dict:
    name = ex.get("name") or "?"
    out = {k: ex.get(k) for k in KEEP_FIELDS}
    out["level"] = LEVEL_MAP.get(ex.get("level"), ex.get("level"))
    out["category"] = CATEGORY_MAP.get(ex.get("category"), ex.get("category"))
    out["primaryMuscles"] = normalize_muscles(ex.get("primaryMuscles"), name)
    out["secondaryMuscles"] = normalize_muscles(ex.get("secondaryMuscles"), name)
    eq = ex.get("equipment")
    if eq is None:
        group = null_equipment_group(ex)
        warnings.append(f"{name}: NULL equipment -> group '{group}' (isBodyweight={ex.get('isBodyweight')})")
    else:
        group = EQUIP_GROUP.get(eq)
        if group is None:
            group = "other"
            warnings.append(f"{name}: unknown equipment '{eq}' -> 'other'")
    out["equipment_group"] = group
    out.pop("images", None)
    return out


def main() -> int:
    data = json.loads(SRC.read_text(encoding="utf-8"))
    raw = data["exercises"]
    print(f"source: {len(raw)} entries")

    seen_names: set[str] = set()
    exercises: list[dict] = []
    skipped_dupe = 0
    for ex in raw:
        key = (ex.get("name") or "").strip().lower()
        if key in seen_names:
            skipped_dupe += 1
            warnings.append(f"duplicate name skipped: {ex.get('name')!r} (id={ex.get('id')})")
            continue
        seen_names.add(key)
        exercises.append(normalize_exercise(ex))

    # Fill the missing primaryMuscles from the first secondary muscle, else bodyPart.
    for ex in exercises:
        if not ex["primaryMuscles"]:
            fallback = (ex["secondaryMuscles"] or [None])[0]
            if fallback is None:
                fallback = MUSCLE_FOLD.get(ex.get("bodyPart"), ex.get("bodyPart"))
            ex["primaryMuscles"] = [fallback]
            warnings.append(f"{ex['name']}: missing primaryMuscles -> filled with ['{fallback}']")

    # ------------------------------------------------------------------
    # Validation gate
    # ------------------------------------------------------------------
    errors: list[str] = []
    for ex in exercises:
        for key in ("id", "name", "level", "category", "equipment_group", "met"):
            if ex.get(key) in (None, ""):
                errors.append(f"{ex.get('name')}: required field '{key}' is null")
        if not ex.get("primaryMuscles"):
            errors.append(f"{ex.get('name')}: primaryMuscles empty")
        if not ex.get("instructions"):
            errors.append(f"{ex.get('name')}: instructions empty")
        met = ex.get("met")
        if not isinstance(met, (int, float)) or not (1.0 <= met <= 12.0):
            errors.append(f"{ex.get('name')}: met out of range: {met}")
        if ex.get("level") not in set(LEVEL_MAP.values()):
            errors.append(f"{ex.get('name')}: unmapped level {ex.get('level')!r}")
        if ex.get("category") not in set(CATEGORY_MAP.values()):
            errors.append(f"{ex.get('name')}: unmapped category {ex.get('category')!r}")

    if errors:
        print("\nVALIDATION FAILED:")
        for e in errors:
            print(f"  ERROR {e}")
        return 1

    # ------------------------------------------------------------------
    # Outputs
    # ------------------------------------------------------------------
    OUT_EXERCISES.write_text(json.dumps(exercises, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT_EXERCISES} ({len(exercises)} exercises)")

    activities = [
        {
            "id": ex["id"],
            "name": ex["name"],
            "aliases": [],
            "category": ex["category"],
            "equipment": ex["equipment"],
            "equipment_group": ex["equipment_group"],
            "met": ex["met"],
            "description": ex["description"],
            "level": ex["level"],
        }
        for ex in exercises
        if "activity_log" in (ex.get("tags") or [])
    ]
    OUT_ACTIVITIES.write_text(json.dumps(activities, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT_ACTIVITIES} ({len(activities)} activities)")

    # ------------------------------------------------------------------
    # Review report
    # ------------------------------------------------------------------
    import collections

    print("\n--- report ---")
    print(f"duplicates skipped: {skipped_dupe}")
    for field in ("category", "level", "equipment_group"):
        print(field, dict(collections.Counter(e[field] for e in exercises).most_common()))
    print("muscles(primary)", dict(collections.Counter(m for e in exercises for m in e["primaryMuscles"]).most_common()))
    print(f"\nwarnings ({len(warnings)}):")
    for w in warnings:
        print(f"  {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
