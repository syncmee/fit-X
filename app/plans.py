"""Workout plan builder: maps a scheduled workout title to a structured session plan.

Plans are assembled from the bundled free-exercise-db dataset (app/data/exercises.json,
public domain / Unlicense) so reminder emails can attach a "here is what you should
hit" section without a live API call. build_plan() resolves in three steps: modality
plans (boxing, running) for sport titles, then a gym composer that parses the title's
muscle vocabulary (push/pull/upper/back + biceps/chest + triceps/...) and builds one
block per requested group from curated per-muscle picks. Titles matching nothing
return None and the email simply renders without a plan section.
"""

import json
import re
from functools import lru_cache
from pathlib import Path

_IMAGE_BASE = "https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/exercises/"

# Muscles that count as "lower body" when choosing the warm-up/cool-down frame.
_LOWER_GROUPS = {"legs", "glutes", "quads", "hamstrings", "calves"}


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


# Deterministic fallback when a curated pick is unavailable: dataset order with
# barbell/dumbbell/cable movements preferred over machine/bodyweight.
_EQUIPMENT_ORDER = {
    "barbell": 0, "dumbbell": 1, "cable": 2, "e-z curl bar": 3, "kettlebells": 4,
    "body only": 5, "machine": 6, "bands": 7, "medicine ball": 7, "exercise ball": 7,
    "foam roll": 8, "other": 6, None: 9,
}


def _db_pick(muscles: tuple[str, ...], used: set) -> dict | None:
    def rank(e):
        return (_EQUIPMENT_ORDER.get(e["equipment"], 9), e["name"])
    for e in sorted(_exercises(), key=rank):
        if e["name"] not in used and set(e["primaryMuscles"]) & set(muscles):
            return e
    return None


def _pick_line(candidates: tuple[str, ...], dose: str, muscles: tuple[str, ...], used: set) -> dict | None:
    e = _resolve(*candidates)
    if e is None or e["name"] in used:
        e = _db_pick(muscles, used)
    if e is None:
        return None
    used.add(e["name"])
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
            _pick_line(("Rope Jumping",), "3 x 2 min", ("calves",), set()),
            _pick_line(("Star Jump",), "2 x 30 sec", ("quadriceps",), set()),
            _pick_line(("Mountain Climbers",), "2 x 45 sec", ("abdominals",), set()),
        ]),
        _block("Technique", "shadow work, stay light on your feet", [
            {"name": "Shadow Boxing", "dose": "3 x 2 min rounds", "source": "coach"},
        ]),
        _block("Main work", "heavy bag — 1 min rest between rounds", [
            _pick_line(("Battling Ropes",), "6 x 3 min rounds", ("shoulders",), set()),
        ]),
        _block("Finisher", "3 rounds, minimal rest", [
            _pick_line(("Mountain Climbers",), "45 sec", ("abdominals",), set()),
            _pick_line(("Freehand Jump Squat",), "12 reps", ("quadriceps",), set()),
            _pick_line(("Plank", "Front Plank"), "45-60 sec", ("abdominals",), set()),
        ]),
        _block("Cool-down", "stretch what you hit", [
            _pick_line(("Chest And Front Of Shoulder Stretch",), "2 x 30 sec each side", ("chest",), set()),
            _pick_line(("Overhead Triceps",), "2 x 30 sec each side", ("triceps",), set()),
            {"name": "Diaphragmatic Breathing", "dose": "8-10 slow breaths", "source": "coach"},
        ]),
    ]


def _running_plan() -> list[dict]:
    return [
        _block("Warm-up", "dynamic — save static stretching for after", [
            _pick_line(("Ankle Circles",), "10 each way", ("calves",), set()),
            _pick_line(("Knee Circles",), "10 each way", ("quadriceps",), set()),
            _pick_line(("Arm Circles",), "10 each way", ("shoulders",), set()),
            _pick_line(("Fast Skipping",), "2 x 20 sec", ("calves",), set()),
        ]),
        _block("Main work", "intervals", [
            _pick_line(("Jogging, Treadmill", "Trail Running/Walking"), "10 min easy", ("quadriceps",), set()),
            {"name": "Intervals", "dose": "6 x 400 m fast / 200 m walk", "source": "coach"},
            _pick_line(("Jogging, Treadmill", "Trail Running/Walking"), "10 min easy", ("quadriceps",), set()),
        ]),
        _block("Finisher", "runner stability", [
            _pick_line(("Plank", "Front Plank"), "3 x 45 sec", ("abdominals",), set()),
            _pick_line(("Push Up to Side Plank", "Side Plank"), "3 x 30 sec each side", ("abdominals",), set()),
        ]),
        _block("Cool-down", "static, post-run", [
            _pick_line(("90/90 Hamstring",), "2 x 30 sec each leg", ("hamstrings",), set()),
            _pick_line(("Calf Stretch Hands Against Wall",), "2 x 30 sec each leg", ("calves",), set()),
            _pick_line(("All Fours Quad Stretch",), "2 x 30 sec each leg", ("quadriceps",), set()),
            _pick_line(("Kneeling Hip Flexor",), "2 x 30 sec each side", ("quadriceps",), set()),
        ]),
    ]


# ---------------------------------------------------------------------------
# Gym composer: title muscle vocabulary -> one block per requested group.
# ---------------------------------------------------------------------------

# Word -> group, matched with word boundaries in title order of appearance.
_DIRECT_WORDS = {
    "chest": "chest", "pecs": "chest", "pec": "chest",
    "back": "back", "lats": "back", "lat": "back", "traps": "back", "trap": "back",
    "shoulders": "shoulders", "shoulder": "shoulders", "delts": "shoulders", "delt": "shoulders",
    "biceps": "biceps", "bicep": "biceps",
    "triceps": "triceps", "tricep": "triceps",
    "arms": "arms", "arm": "arms",
    "legs": "legs", "leg": "legs",
    "glutes": "glutes", "glute": "glutes",
    "quads": "quads", "quad": "quads",
    "hamstrings": "hamstrings", "hamstring": "hamstrings",
    "calves": "calves", "calf": "calves",
    "core": "core", "abs": "core", "abdominals": "core",
}
_DIRECT_RE = re.compile(r"\b(" + "|".join(_DIRECT_WORDS) + r")\b")

# Split keywords first: they define canonical group combinations.
_SPLITS = [
    ("full body", ("chest", "back", "shoulders", "legs", "core"), "Full Body"),
    ("push", ("chest", "shoulders", "triceps"), "Push"),
    ("pull", ("back", "biceps"), "Pull"),
    ("upper", ("chest", "back", "shoulders", "arms"), "Upper Body"),
    ("lower", ("legs",), "Lower Body"),
]

_GROUP_LABELS = {
    "chest": "Chest", "back": "Back", "shoulders": "Shoulders", "biceps": "Biceps",
    "triceps": "Triceps", "arms": "Arms", "core": "Core", "legs": "Legs",
    "glutes": "Glutes", "quads": "Quads", "hamstrings": "Hamstrings", "calves": "Calves",
}

# Curated picks per group: (name candidates, dose), compound first. Every group
# also falls back to a deterministic DB pick by dataset muscle name, so a table
# entry that stops resolving can never leave a block empty.
_GROUP_TABLE = {
    "chest": {
        "muscles": ("chest",), "focus": "pressing movements",
        "picks": [
            (("Barbell Bench Press - Medium Grip", "Barbell Bench Press"), "4 x 6-8"),
            (("Incline Dumbbell Press", "Dumbbell Bench Press"), "3 x 8-10"),
            (("Cable Crossover",), "3 x 12-15"),
        ],
    },
    "back": {
        "muscles": ("lats", "middle back", "traps", "lower back"), "focus": "pulls — width and thickness",
        "picks": [
            (("Wide-Grip Lat Pulldown",), "4 x 8-10"),
            (("Bent Over Barbell Row",), "4 x 6-8"),
            (("One-Arm Dumbbell Row",), "3 x 10 each side"),
            (("Face Pull",), "3 x 12-15"),
        ],
    },
    "shoulders": {
        "muscles": ("shoulders",), "focus": "presses and raises",
        "picks": [
            (("Seated Dumbbell Press",), "3 x 8-10"),
            (("Side Lateral Raise",), "3 x 12-15"),
            (("Barbell Rear Delt Row",), "3 x 12"),
            (("Arnold Dumbbell Press",), "3 x 10"),
        ],
    },
    "biceps": {
        "muscles": ("biceps",), "focus": "curls",
        "picks": [
            (("Dumbbell Bicep Curl",), "3 x 10-12"),
            (("Barbell Curl",), "3 x 8-10"),
            (("Preacher Curl",), "3 x 12"),
        ],
    },
    "triceps": {
        "muscles": ("triceps",), "focus": "extensions",
        "picks": [
            (("Triceps Pushdown - Rope Attachment",), "3 x 10-12"),
            (("Dips - Triceps Version",), "3 x 8-10"),
            (("Seated Triceps Press",), "3 x 12"),
        ],
    },
    "arms": {
        "muscles": ("biceps", "triceps"), "focus": "biceps and triceps superset",
        "picks": [
            (("Dumbbell Bicep Curl",), "3 x 10-12"),
            (("Triceps Pushdown - Rope Attachment",), "3 x 10-12"),
            (("Preacher Curl",), "3 x 12"),
            (("Dips - Triceps Version",), "3 x 8-10"),
        ],
    },
    "core": {
        "muscles": ("abdominals",), "focus": "stability and control",
        "picks": [
            (("Plank",), "3 x 45-60 sec"),
            (("Russian Twist",), "3 x 20"),
            (("Cable Crunch",), "3 x 12-15"),
            (("Crunches",), "3 x 15"),
        ],
    },
    "legs": {
        "muscles": ("quadriceps", "hamstrings", "glutes", "calves"), "focus": "compound lifts first",
        "picks": [
            (("Barbell Squat",), "4 x 6-8"),
            (("Leg Press",), "3 x 10"),
            (("Dumbbell Lunges",), "3 x 10 each leg"),
            (("Lying Leg Curls",), "3 x 10"),
        ],
    },
    "glutes": {
        "muscles": ("glutes",), "focus": "hip extension",
        "picks": [
            (("Barbell Hip Thrust",), "4 x 8-10"),
            (("Glute Kickback",), "3 x 12 each side"),
            (("Single Leg Glute Bridge",), "3 x 12 each side"),
        ],
    },
    "quads": {
        "muscles": ("quadriceps",), "focus": "squat patterns",
        "picks": [
            (("Barbell Squat",), "4 x 6-8"),
            (("Leg Press",), "3 x 10"),
            (("Leg Extensions",), "3 x 12-15"),
        ],
    },
    "hamstrings": {
        "muscles": ("hamstrings",), "focus": "hinge and curl",
        "picks": [
            (("Romanian Deadlift",), "4 x 8"),
            (("Lying Leg Curls",), "3 x 10"),
            (("Good Morning",), "3 x 10"),
            (("Seated Leg Curl",), "3 x 12"),
        ],
    },
    "calves": {
        "muscles": ("calves",), "focus": "slow, full range",
        "picks": [
            (("Barbell Seated Calf Raise",), "4 x 12-15"),
            (("Calf Raise On A Dumbbell",), "3 x 12 each side"),
            (("Donkey Calf Raises",), "3 x 12"),
        ],
    },
}


def _parse_groups(title: str) -> tuple[list[str], str | None]:
    """Title -> (ordered muscle groups, split label or None). Empty list = no gym vocabulary."""
    t = (title or "").lower()
    if not t.strip():
        return [], None
    groups: list[str] = []
    split_label = None
    for word, split_groups, label in _SPLITS:
        if re.search(r"\b" + word.replace(" ", r"\s+") + r"\b", t):
            split_label = split_label or label
            for g in split_groups:
                if g not in groups:
                    groups.append(g)
    for m in _DIRECT_RE.finditer(t):
        g = _DIRECT_WORDS[m.group(1)]
        if g not in groups:
            groups.append(g)
    return groups, split_label


def _quotas(n: int) -> list[int]:
    if n == 1:
        return [3]
    if n == 2:
        return [3, 2]
    if n == 3:
        return [2, 2, 2]
    # 4+ groups: two picks for the first groups, one each after, capped at 7 rows.
    quotas = [2] * n
    while sum(quotas) > 7:
        for i in range(n - 1, -1, -1):
            if quotas[i] > 1:
                quotas[i] -= 1
                break
    return quotas


def _warmup_block(groups: list[str]) -> dict:
    if all(g in _LOWER_GROUPS for g in groups):
        return _block("Warm-up", "wake the hips up", [
            _pick_line(("Bodyweight Walking Lunge",), "2 x 10 each leg", ("quadriceps",), set()),
            _pick_line(("Knee Circles",), "10 each way", ("quadriceps",), set()),
        ])
    return _block("Warm-up", "get the shoulders moving", [
        _pick_line(("Arm Circles",), "2 x 15", ("shoulders",), set()),
        _pick_line(("Dynamic Chest Stretch",), "2 x 10", ("chest",), set()),
    ])


def _cooldown_block(groups: list[str]) -> dict:
    if all(g in _LOWER_GROUPS for g in groups):
        return _block("Cool-down", "the walk-home will thank you", [
            _pick_line(("90/90 Hamstring",), "2 x 30 sec each leg", ("hamstrings",), set()),
            _pick_line(("Lying Prone Quadriceps",), "2 x 30 sec each leg", ("quadriceps",), set()),
            _pick_line(("Kneeling Hip Flexor",), "2 x 30 sec each side", ("quadriceps",), set()),
        ])
    return _block("Cool-down", "stretch the presses and pulls away", [
        _pick_line(("Behind Head Chest Stretch",), "2 x 30 sec", ("chest",), set()),
        _pick_line(("Overhead Lat",), "2 x 30 sec each side", ("lats",), set()),
    ])


def _gym_plan(title: str) -> dict | None:
    groups, split_label = _parse_groups(title)
    if not groups:
        return None
    used: set = set()
    blocks = [_warmup_block(groups)]
    for g, quota in zip(groups, _quotas(len(groups))):
        table = _GROUP_TABLE[g]
        lines = []
        for candidates, dose in table["picks"]:
            if len(lines) >= quota:
                break
            line = _pick_line(candidates, dose, table["muscles"], used)
            if line:
                lines.append(line)
        if lines:
            blocks.append(_block(_GROUP_LABELS[g].upper(), table["focus"], lines))
    blocks.append(_cooldown_block(groups))
    if split_label and len(blocks) == len(groups) + 2:
        name = split_label
    else:
        name = " · ".join(_GROUP_LABELS[g] for g in groups)
    return {"name": name, "blocks": blocks}


# Title-keyword → plan. Modality plans first, then the gym composer; a title
# matching nothing (yoga, swimming, ...) gets no plan section.
def build_plan(title: str) -> dict | None:
    t = (title or "").lower()
    if not t.strip():
        return None
    if any(k in t for k in ("boxing", "box", "bag", "sparring", "mma", "kickbox", "muay thai")):
        return {"name": "Boxing", "blocks": _boxing_plan()}
    if any(k in t for k in ("run", "jog", "sprint", "treadmill", "5k", "10k", "interval")):
        return {"name": "Running", "blocks": _running_plan()}
    return _gym_plan(title)
