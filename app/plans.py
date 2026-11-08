"""Workout plan builder: maps a scheduled workout title to a structured session plan.

Plans are assembled from the bundled Fit-X exercise catalog (app/data/exercises.json)
so reminder emails can attach a "here is what you should hit" section without a live
API call. build_plan() resolves in three steps: modality plans (boxing, running) that
pull real sport drills from the dataset, then a gym composer that parses the title's
muscle vocabulary (push/pull/upper/back + biceps/chest + triceps/...) and builds one
block per requested group from curated per-muscle picks. Titles matching nothing
return None and the email simply renders without a plan section.

Muscle fields hold anatomical names (gluteus_maximus, anterior_deltoid, ...); the
group tables below map the composer's muscle words onto them. Every plan line
carries the exercise's MET value so sessions can estimate calorie burn.
"""

import json
import re
from functools import lru_cache
from pathlib import Path

_SOURCE = "fitx-catalog"

# Muscles that count as "lower body" when choosing the warm-up/cool-down frame.
_LOWER_GROUPS = {"legs", "glutes", "quads", "hamstrings", "calves"}

# Where the session happens decides which equipment groups may appear in the
# plan: gym = anything, home = bodyweight plus a doorway pull-up bar / dip
# station (standard home rigging, matching how the old catalog treated
# pull-ups and dips as "body only"), home_gym = small home kit.
_ENVIRONMENT_EQUIPMENT = {
    "gym": None,
    "home": {"bodyweight", "pull_up_bar", "dip_station"},
    "home_gym": {
        "bodyweight", "dumbbell", "kettlebell", "bands", "medicine_ball",
        "stability_ball", "bosu_ball", "pull_up_bar", "dip_station", "ab_wheel",
        "jump_rope", "ez_bar",
    },
}
# Beginner-first ordering for dataset fallbacks — the tab plan audience is
# people who don't know movements yet, so easy variations win ties.
_LEVEL_ORDER = {"beginner": 0, "intermediate": 1, "expert": 2}


def _env_ok(exercise: dict, environment: str | None) -> bool:
    allowed = _ENVIRONMENT_EQUIPMENT.get(environment)
    if allowed is None:
        return True
    return (exercise.get("equipment_group") or "bodyweight") in allowed


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
    "barbell": 0, "dumbbell": 1, "cable": 2, "ez_bar": 3, "kettlebell": 4,
    "bodyweight": 5, "machine": 6, "bands": 7, "medicine_ball": 7,
    "stability_ball": 7, "bosu_ball": 7, "other": 6, None: 9,
}

# Muscle-based fallbacks pick training movements only — sports activities and
# cardio entries carry muscles too but must never surface in a gym plan.
_PICK_CATEGORIES = {"strength", "olympic weightlifting", "olympic", "plyometrics"}


def _rank(exercise: dict, environment: str | None) -> tuple:
    return (
        0 if _env_ok(exercise, environment) else 1,
        _LEVEL_ORDER.get(exercise.get("level"), 1),
        _EQUIPMENT_ORDER.get(exercise.get("equipment_group"), 9),
        exercise["name"],
    )


def _db_pick(
    muscles: tuple[str, ...], used: set, environment: str | None = None,
    categories: set[str] | None = None,
) -> dict | None:
    pool = [e for e in _exercises() if e.get("category") in (categories or _PICK_CATEGORIES)]
    for e in sorted(pool, key=lambda e: _rank(e, environment)):
        if e["name"] not in used and set(e["primaryMuscles"]) & set(muscles) and _env_ok(e, environment):
            return e
    return None


def _alternate_for(exercise: dict, used: set, environment: str | None) -> dict | None:
    """Best same-variationGroup swap for an exercise (env-compatible, unused)."""
    group = exercise.get("variationGroup")
    if not group:
        return None
    pool = [
        e for e in _exercises()
        if e.get("variationGroup") == group and e["name"] != exercise["name"]
    ]
    for e in sorted(pool, key=lambda e: _rank(e, environment)):
        if e["name"] not in used and _env_ok(e, environment):
            return e
    return None


def _pick_line(
    candidates: tuple[str, ...], dose: str, muscles: tuple[str, ...], used: set,
    environment: str | None = None, categories: set[str] | None = None,
) -> dict | None:
    # Walk candidates in order until one resolves, is unused, and fits the
    # environment — so "Lat Pulldown, Pull-Up" serves gyms the pulldown and
    # homes the pull-up instead of both falling to the muscle fallback.
    e = None
    for candidate in candidates:
        hit = _resolve(candidate)
        if hit and hit["name"] not in used and _env_ok(hit, environment):
            e = hit
            break
    if e is None:
        e = _db_pick(muscles, used, environment, categories)
    if e is None:
        return None
    used.add(e["name"])
    alt = _alternate_for(e, used, environment)
    return {
        "name": e["name"],
        "dose": dose,
        "source": _SOURCE,
        "met": e.get("met"),
        "level": e.get("level"),
        "instructions": list((e.get("instructions") or [])[:3]),
        "alternate": alt["name"] if alt else None,
    }


def _block(title: str, focus: str, lines: list[dict]) -> dict:
    return {"block": title, "focus": focus, "exercises": lines}


# ---------------------------------------------------------------------------
# Modality plans: real sport drills pulled from the dataset's sports/cardio
# entries. These ignore the environment filter — a boxing session needs boxing
# gear regardless of where the user trains, and falling back to gym moves here
# would produce the inaccurate plans the old templates suffered from.
# ---------------------------------------------------------------------------

def _boxing_plan() -> list[dict]:
    used: set = set()
    return [
        _block("Warm-up", "raise the pulse, loosen shoulders", [
            _pick_line(("Jump Rope",), "3 x 2 min", ("gastrocnemius",), used),
            _pick_line(("Torso Twists",), "2 x 30 sec", ("obliques",), used),
            _pick_line(("High Knees",), "2 x 45 sec", ("quadriceps",), used),
        ]),
        _block("Technique", "shadow work, stay light on your feet", [
            _pick_line(("Shadow Boxing",), "3 x 2 min rounds", ("full_body",), used),
            _pick_line(("Boxing Footwork Drills",), "3 x 2 min", ("full_body",), used),
        ]),
        _block("Main work", "bag and mitts — 1 min rest between rounds", [
            _pick_line(("Heavy Bag Boxing", "Boxing Bag Rounds"), "6 x 3 min rounds", ("full_body",), used),
            _pick_line(("Boxing Focus-Mitt Drills",), "4 x 3 min", ("full_body",), used),
        ]),
        _block("Finisher", "3 rounds, minimal rest", [
            _pick_line(("Mountain Climbers",), "45 sec", ("rectus_abdominis",), used),
            _pick_line(("Jump Squat",), "12 reps", ("quadriceps",), used),
            _pick_line(("Plank",), "45-60 sec", ("rectus_abdominis",), used),
        ]),
        _block("Cool-down", "stretch what you hit", [
            _pick_line(("Doorway Chest Stretch",), "2 x 30 sec", ("pectoralis_major",), used),
            _pick_line(("Overhead Triceps Stretch",), "2 x 30 sec each side", ("triceps_brachii",), used),
            {"name": "Diaphragmatic Breathing", "dose": "8-10 slow breaths", "source": "coach"},
        ]),
    ]


def _running_plan() -> list[dict]:
    used: set = set()
    return [
        _block("Warm-up", "dynamic — save static stretching for after", [
            _pick_line(("Front-to-Back Leg Swing",), "10 each way", ("hip_flexors",), used),
            _pick_line(("Lateral Leg Swing",), "10 each way", ("hip_flexors",), used),
            _pick_line(("High Knees",), "2 x 20 sec", ("quadriceps",), used),
            _pick_line(("Jump Rope",), "2 x 20 sec", ("gastrocnemius",), used),
        ]),
        _block("Main work", "intervals", [
            _pick_line(("Jogging",), "10 min easy", ("quadriceps",), used),
            {"name": "Intervals", "dose": "6 x 400 m fast / 200 m walk", "source": "coach"},
            _pick_line(("Running Intervals", "Treadmill Running"), "10 min cooldown", ("quadriceps",), used),
        ]),
        _block("Finisher", "runner stability", [
            _pick_line(("Plank",), "3 x 45 sec", ("rectus_abdominis",), used),
            _pick_line(("Side Plank",), "3 x 30 sec each side", ("obliques",), used),
        ]),
        _block("Cool-down", "static, post-run", [
            _pick_line(("Banded Hamstring Stretch", "Wall Hamstring Stretch"), "2 x 30 sec each leg", ("hamstrings",), used),
            _pick_line(("Standing Calf Stretch",), "2 x 30 sec each leg", ("gastrocnemius",), used),
            _pick_line(("Lying Quad Stretch",), "2 x 30 sec each leg", ("quadriceps",), used),
            _pick_line(("Kneeling Hip Flexor Stretch",), "2 x 30 sec each side", ("hip_flexors",), used),
        ]),
    ]


def _swimming_plan() -> list[dict]:
    used: set = set()
    return [
        _block("Warm-up", "on land — wake up shoulders, hips and ankles", [
            _pick_line(("Torso Twists",), "2 x 30 sec", ("obliques",), used),
            _pick_line(("Cat-Cow",), "2 x 10 slow", ("erector_spinae",), used),
            _pick_line(("World's Greatest Stretch",), "30 sec each side", ("hip_flexors",), used),
        ]),
        _block("Technique", "drills before distance — full rest between reps", [
            _pick_line(("Kickboard Swim",), "4 x 25 m kick", ("full_body",), used),
            _pick_line(("Freestyle Swimming", "Swimming Laps"), "4 x 50 m — long strokes, breathe every 3", ("full_body",), used),
            _pick_line(("Backstroke Swimming",), "4 x 25 m — steady head position", ("full_body",), used),
            _pick_line(("Breaststroke Swimming",), "4 x 25 m — glide each stroke", ("full_body",), used),
        ]),
        _block("Main work", "endurance — steady pace, 30 sec rest per rep", [
            _pick_line(("Swimming Laps", "Open-Water Swimming"), "10 x 50 m steady", ("full_body",), used),
            _pick_line(("Water Jogging",), "5 min easy recovery", ("full_body",), used),
        ]),
        _block("Cool-down", "stretch what swimming tightened", [
            _pick_line(("Cross-Body Shoulder Stretch",), "2 x 30 sec each side", ("anterior_deltoid",), used),
            _pick_line(("Doorway Chest Stretch",), "2 x 30 sec", ("pectoralis_major",), used),
            _pick_line(("Child's Pose",), "45 sec", ("erector_spinae",), used),
        ]),
    ]


def _cycling_plan() -> list[dict]:
    used: set = set()
    return [
        _block("Warm-up", "spin easy, build to a conversational pace", [
            _pick_line(("Stationary Cycling", "Road Cycling"), "10 min easy spin", ("quadriceps",), used),
            _pick_line(("Bodyweight Squat",), "2 x 10", ("quadriceps",), used),
        ]),
        _block("Main work", "intervals — 1 min rest between efforts", [
            _pick_line(("Cycling Intervals",), "6 x 1 min hard / 2 min easy", ("quadriceps",), used),
            _pick_line(("Road Cycling", "Stationary Cycling"), "20 min steady, cadence 85-95 rpm", ("quadriceps",), used),
        ]),
        _block("Finisher", "post-ride stability", [
            _pick_line(("Plank",), "3 x 45 sec", ("rectus_abdominis",), used),
        ]),
        _block("Cool-down", "off the bike, stretch the riders' muscles", [
            _pick_line(("Lying Quad Stretch",), "2 x 30 sec each leg", ("quadriceps",), used),
            _pick_line(("Standing Calf Stretch",), "2 x 30 sec each leg", ("gastrocnemius",), used),
            _pick_line(("Banded Hamstring Stretch", "Wall Hamstring Stretch"), "2 x 30 sec each leg", ("hamstrings",), used),
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

# Curated picks per group: (name candidates, dose), compound first. "muscles"
# are anatomical dataset names (primaryMuscles vocabulary). Every group also
# falls back to a deterministic DB pick by muscle, so a table entry that stops
# resolving can never leave a block empty.
_GROUP_TABLE = {
    "chest": {
        "muscles": ("pectoralis_major", "serratus_anterior"), "focus": "pressing movements",
        "picks": [
            (("Barbell Bench Press",), "4 x 6-8"),
            (("Incline Dumbbell Press",), "3 x 8-10"),
            (("Cable Fly",), "3 x 12-15"),
        ],
    },
    "back": {
        "muscles": ("latissimus_dorsi", "trapezius", "rhomboids", "erector_spinae"),
        "focus": "pulls — width and thickness",
        "picks": [
            # Pull-Up is the bodyweight stand-in so home plans still get
            # vertical pulling once the bar group is env-allowed.
            (("Lat Pulldown", "Pull-Up"), "4 x 8-10"),
            (("Bent-Over Barbell Row",), "4 x 6-8"),
            (("Single-Arm Dumbbell Row",), "3 x 10 each side"),
            (("Cable Face Pull",), "3 x 12-15"),
        ],
    },
    "shoulders": {
        "muscles": ("anterior_deltoid", "lateral_deltoid", "posterior_deltoid", "infraspinatus_teres_minor"),
        "focus": "presses and raises",
        "picks": [
            (("Seated Dumbbell Shoulder Press",), "3 x 8-10"),
            (("Dumbbell Lateral Raise",), "3 x 12-15"),
            (("Barbell Rear Delt Row",), "3 x 12"),
            (("Arnold Press",), "3 x 10"),
        ],
    },
    "biceps": {
        "muscles": ("biceps_brachii", "brachialis"), "focus": "curls",
        "picks": [
            (("Dumbbell Bicep Curl", "Chin-Ups"), "3 x 10-12"),
            (("Barbell Curl",), "3 x 8-10"),
            (("Dumbbell Preacher Curl",), "3 x 12"),
        ],
    },
    "triceps": {
        "muscles": ("triceps_brachii",), "focus": "extensions",
        "picks": [
            (("Cable Tricep Pushdown",), "3 x 10-12"),
            (("Bench Dips",), "3 x 8-10"),
            (("Cable Overhead Tricep Extension",), "3 x 12"),
        ],
    },
    "arms": {
        "muscles": ("biceps_brachii", "triceps_brachii"), "focus": "biceps and triceps superset",
        "picks": [
            (("Dumbbell Bicep Curl",), "3 x 10-12"),
            (("Cable Tricep Pushdown",), "3 x 10-12"),
            (("Dumbbell Preacher Curl",), "3 x 12"),
            (("Bench Dips",), "3 x 8-10"),
        ],
    },
    "core": {
        "muscles": ("rectus_abdominis", "transverse_abdominis", "obliques"),
        "focus": "stability and control",
        "picks": [
            (("Plank",), "3 x 45-60 sec"),
            (("Russian Twist",), "3 x 20"),
            (("Cable Crunch",), "3 x 12-15"),
            (("Crunches",), "3 x 15"),
        ],
    },
    "legs": {
        "muscles": ("quadriceps", "hamstrings", "gluteus_maximus", "gastrocnemius", "soleus"),
        "focus": "compound lifts first",
        "picks": [
            (("Barbell Back Squat",), "4 x 6-8"),
            (("Leg Press",), "3 x 10"),
            (("Dumbbell Lunge",), "3 x 10 each leg"),
            (("Lying Leg Curl",), "3 x 10"),
        ],
    },
    "glutes": {
        "muscles": ("gluteus_maximus", "gluteus_medius", "abductors"), "focus": "hip extension",
        "picks": [
            (("Barbell Hip Thrust",), "4 x 8-10"),
            (("Glute Kickback",), "3 x 12 each side"),
            (("Single Leg Glute Bridge",), "3 x 12 each side"),
        ],
    },
    "quads": {
        "muscles": ("quadriceps",), "focus": "squat patterns",
        "picks": [
            (("Barbell Back Squat",), "4 x 6-8"),
            (("Leg Press",), "3 x 10"),
            (("Leg Extension",), "3 x 12-15"),
        ],
    },
    "hamstrings": {
        "muscles": ("hamstrings",), "focus": "hinge and curl",
        "picks": [
            (("Romanian Deadlift",), "4 x 8"),
            (("Lying Leg Curl",), "3 x 10"),
            (("Good Morning",), "3 x 10"),
            (("Seated Leg Curl",), "3 x 12"),
        ],
    },
    "calves": {
        "muscles": ("gastrocnemius", "soleus", "tibialis_anterior"), "focus": "slow, full range",
        "picks": [
            (("Seated Calf Raise",), "4 x 12-15"),
            (("Standing Calf Raise",), "3 x 12 each side"),
            (("Donkey Calf Raise",), "3 x 12"),
        ],
    },
}


def _parse_groups(title: str) -> tuple[list[str], str | None]:
    """Title -> (ordered muscle groups, split label or None). Empty list = no gym vocabulary."""
    # Normalize hyphens so "full-body" matches the "full body" split.
    t = " ".join((title or "").lower().replace("-", " ").split())
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


def _warmup_block(groups: list[str], environment: str | None = None, used: set | None = None) -> dict:
    used = used if used is not None else set()
    if all(g in _LOWER_GROUPS for g in groups):
        return _block("Warm-up", "wake the hips up", [
            _pick_line(("Walking Lunge",), "2 x 10 each leg", ("quadriceps",), used, environment),
            _pick_line(("Torso Twists",), "10 each way", ("obliques",), used, environment),
        ])
    return _block("Warm-up", "get the shoulders moving", [
        _pick_line(("Jumping Jacks",), "2 x 15", ("quadriceps",), used, environment),
        _pick_line(("Cat-Cow",), "2 x 10", ("erector_spinae",), used, environment),
    ])


def _cooldown_block(groups: list[str], environment: str | None = None, used: set | None = None) -> dict:
    used = used if used is not None else set()
    # Fallbacks stay inside the stretching category — a cool-down must never
    # surface a strength move (the old fallback once picked "Mat Pilates"
    # as a lat stretch for home users).
    cooldown_env = dict(environment=environment, categories={"stretching"})
    if all(g in _LOWER_GROUPS for g in groups):
        return _block("Cool-down", "the walk-home will thank you", [
            _pick_line(("Banded Hamstring Stretch", "Wall Hamstring Stretch"), "2 x 30 sec each leg", ("hamstrings",), used, **cooldown_env),
            _pick_line(("Lying Quad Stretch",), "2 x 30 sec each leg", ("quadriceps",), used, **cooldown_env),
            _pick_line(("Kneeling Hip Flexor Stretch",), "2 x 30 sec each side", ("hip_flexors",), used, **cooldown_env),
        ])
    return _block("Cool-down", "stretch the presses and pulls away", [
        _pick_line(("Doorway Chest Stretch",), "2 x 30 sec", ("pectoralis_major",), used, **cooldown_env),
        _pick_line(("Banded Lat Stretch",), "2 x 30 sec each side", ("latissimus_dorsi",), used, **cooldown_env),
    ])


def _gym_plan(title: str, environment: str | None = None) -> dict | None:
    groups, split_label = _parse_groups(title)
    if not groups:
        return None
    used: set = set()
    blocks = [_warmup_block(groups, environment, used)]
    for g, quota in zip(groups, _quotas(len(groups))):
        table = _GROUP_TABLE[g]
        lines = []
        for candidates, dose in table["picks"]:
            if len(lines) >= quota:
                break
            line = _pick_line(candidates, dose, table["muscles"], used, environment)
            if line:
                lines.append(line)
        if lines:
            blocks.append(_block(_GROUP_LABELS[g].upper(), table["focus"], lines))
    blocks.append(_cooldown_block(groups, environment, used))
    if split_label and len(blocks) == len(groups) + 2:
        name = split_label
    else:
        name = " · ".join(_GROUP_LABELS[g] for g in groups)
    return {"name": name, "blocks": blocks}


def _generic_plan(environment: str | None = None) -> dict:
    """Beginner full-body circuit for titles the composer can't parse (yoga,
    swimming, cricket, ...). Bodyweight-first so it works in any environment;
    the environment filter only trims the pool."""
    used: set = set()
    return {
        "name": "Full Body",
        "blocks": [
            _block("Warm-up", "easy pace, get warm before you work", [
                _pick_line(("Jumping Jacks",), "2 x 30 sec", ("quadriceps",), used, environment),
                _pick_line(("Torso Twists",), "2 x 15", ("obliques",), used, environment),
            ]),
            _block("Main work", "beginner circuit — 2 to 3 rounds, rest as needed", [
                _pick_line(("Bodyweight Squat",), "12 reps", ("quadriceps",), used, environment),
                _pick_line(("Push-Up",), "8-12 reps", ("pectoralis_major",), used, environment),
                _pick_line(("Walking Lunge",), "10 each leg", ("quadriceps", "gluteus_maximus"), used, environment),
                _pick_line(("Single Leg Glute Bridge", "Glute Kickback"), "10 each side", ("gluteus_maximus",), used, environment),
                _pick_line(("Plank",), "30-45 sec", ("rectus_abdominis",), used, environment),
            ]),
            _block("Cool-down", "stretch, breathe, done", [
                _pick_line(("Banded Hamstring Stretch", "Wall Hamstring Stretch"), "30 sec each leg", ("hamstrings",), used, environment),
                _pick_line(("Doorway Chest Stretch",), "30 sec", ("pectoralis_major",), used, environment),
                _pick_line(("Child's Pose",), "45 sec", ("erector_spinae",), used, environment),
            ]),
        ],
    }


# Title-keyword → plan. Modality plans first, then the gym composer; a title
# matching nothing (yoga, ...) returns None unless generic_fallback is set, in
# which case a beginner full-body circuit stands in.
# Word-boundary matching — substring "run" would otherwise match inside
# "crunch" and turn a core session into a Running plan. Swim/cycling are
# checked before running: the running "interval" keyword would otherwise
# swallow "Cycling Intervals".
_BOXING_RE = re.compile(
    r"boxing|sparring|mma|kickbox|muay thai|\bbag\b|\bbox\b|punch|jab|\bhook\b"
    r"|uppercut|defen[cs]e|combos?\b|combina|mitt"
)
_SWIMMING_RE = re.compile(r"\b(swim|swims|swam|swimming|freestyle|backstroke|breaststroke|butterfly|laps?|triathlon|pool)\b")
_CYCLING_RE = re.compile(r"\b(cycl\w*|bike|bikes|biking|bicycle|spinning|\bspin\b)\b")
_RUNNING_RE = re.compile(r"\b(run|runs|running|jog|jogging|sprint|sprints|treadmill|5k|10k|interval|intervals)\b")


def build_plan(title: str, environment: str | None = None, generic_fallback: bool = False) -> dict | None:
    t = (title or "").lower()
    if not t.strip():
        return None
    if _BOXING_RE.search(t):
        return {"name": "Boxing", "blocks": _boxing_plan()}
    if _SWIMMING_RE.search(t):
        return {"name": "Swimming", "blocks": _swimming_plan()}
    if _CYCLING_RE.search(t):
        return {"name": "Cycling", "blocks": _cycling_plan()}
    if _RUNNING_RE.search(t):
        return {"name": "Running", "blocks": _running_plan()}
    plan = _gym_plan(title, environment)
    if plan:
        return plan
    if generic_fallback:
        return _generic_plan(environment)
    return None
