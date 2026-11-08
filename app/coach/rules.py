"""Rule-based coach: natural-language parsing for weight, water, meal and
workout logging, lookups, and challenge joins.

This is the always-available fallback when both AI providers fail, and the
handler for structured chip answers (training environment). Meal/week context
totals still live with the dashboard context builders and are late-imported
here to avoid a circular import.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta

from sqlalchemy import func, select

from app.extensions import db
from app.models import (
    Challenge,
    CoachMessage,
    MealEntry,
    ScheduledWorkout,
    User,
    UserChallenge,
    WaterLog,
    WeightLog,
)
from app.timeutil import day_bounds, effective_date, to_user_clock
from app.challenges import update_challenges
from app import activities

from . import CoachOutcome, ENVIRONMENT_ASK
from .actions import default_meal_type as _default_meal_type
from .actions import sync_current_weight as _sync_current_weight


def _meal_totals_between(user, start, end):
    """Today's meal totals via the dashboard context builders."""
    from app import dashboard

    return dashboard._sum_meal_entries(
        dashboard._get_meal_entries_between(user, start, end))


def _week_activity(user, local_now):
    from app import dashboard

    return dashboard._build_week_activity(user, local_now)


COACH_QUICK_STARTS = [
    "Log my weight as 82.4 kg today",
    "I had 2 eggs and toast for breakfast, roughly 350 kcal with 20g protein",
    "Schedule yoga tomorrow at 7 am for 60 min",
    "Just finished a 30 minute run, sweating hard",
]

WORKOUT_LABELS = [
    ("full body", "Full Body"),
    ("upper body", "Upper Body"),
    ("lower body", "Lower Body"),
    ("push day", "Push Day"),
    ("pull day", "Pull Day"),
    ("leg day", "Leg Day"),
    ("legs", "Leg Day"),
    ("chest", "Chest"),
    ("back", "Back"),
    ("arms", "Arms"),
    ("shoulders", "Shoulders"),
    ("core", "Core"),
    ("hiit", "HIIT"),
    ("cardio", "Cardio"),
    ("boxing", "Boxing"),
    ("combat", "Combat Sports"),
    ("mma", "MMA"),
    ("run", "Run"),
    ("running", "Run"),
    ("walk", "Walk"),
    ("yoga", "Yoga"),
    ("pilates", "Pilates"),
    ("mobility", "Mobility"),
    ("stretch", "Stretch"),
    ("swim", "Swim"),
    ("swimming", "Swim"),
    ("cycle", "Cycling"),
    ("cycling", "Cycling"),
]

WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

MEAL_TYPE_KEYWORDS = {
    "breakfast": "breakfast",
    "lunch": "lunch",
    "dinner": "dinner",
    "snack": "snack",
}

KCAL_PATTERN = re.compile(r"\b(\d{2,4})\s*(?:k?calories|k?calorie|kcal|cals|cal)\b", re.IGNORECASE)
PROTEIN_PATTERN = re.compile(r"\b(\d{1,3})\s*(?:g\s*)?(?:protein|prot|p)\b", re.IGNORECASE)
CARBS_PATTERN = re.compile(r"\b(\d{1,3})\s*(?:g\s*)?(?:carbs|carbohydrates|carb|c)\b", re.IGNORECASE)
FATS_PATTERN = re.compile(r"\b(\d{1,3})\s*(?:g\s*)?(?:fats|fat|f)\b", re.IGNORECASE)

COACH_CHAT_HISTORY_LIMIT = 12
def process_coach_message_rules(user: User, message_text: str, now: datetime | None = None) -> CoachOutcome:
    now = now or datetime.utcnow()
    cleaned_message = " ".join((message_text or "").split())

    if not cleaned_message:
        raise ValueError("Type something for the coach to work with.")

    normalized = cleaned_message.lower()

    db.session.add(CoachMessage(role="user", content=cleaned_message, user=user, created_at=to_user_clock(user, now)))

    if _looks_like_help(normalized):
        outcome = CoachOutcome(
            reply=(
                "I can log food with calories and macros, log weight, track water, schedule or complete "
                "workouts, join challenges, and report your progress. Try: "
                + "; ".join(COACH_QUICK_STARTS)
                + "."
            ),
            action="help",
        )
    elif _looks_like_environment_answer(normalized):
        outcome = _answer_environment(user, normalized, now)
    elif _looks_like_challenge_join(normalized):
        outcome = _join_challenge_rule(user, normalized, now)
    elif _looks_like_challenge_lookup(normalized):
        outcome = _build_challenge_lookup(user, now)
    elif _looks_like_schedule_lookup(normalized):
        outcome = _build_schedule_lookup(user, now)
    elif _looks_like_progress_lookup(normalized):
        outcome = _build_progress_lookup(user, now)
    elif _looks_like_weight_log(normalized):
        outcome = _log_weight(user, cleaned_message, now)
    elif _looks_like_water_log(normalized):
        outcome = _log_water(user, cleaned_message, now)
    elif _looks_like_workout_schedule(normalized):
        outcome = _schedule_workout(user, cleaned_message, now)
    elif _looks_like_meal_log(normalized):
        outcome = _log_meal(user, cleaned_message, now)
    elif _looks_like_workout_log(normalized):
        outcome = _log_workout_completion(user, cleaned_message, now)
    else:
        outcome = CoachOutcome(
            reply=(
                "I did not fully catch that yet. Try something like "
                "'Log breakfast: oats and banana - 420 kcal 20p 60c 8f', "
                "'Log my weight as 81.9 kg', 'Log 500 ml water', "
                "or 'Schedule yoga tomorrow at 6:30 pm for 45 min'."
            ),
            action="fallback",
        )

    db.session.add(CoachMessage(role="assistant", content=outcome.reply, user=user,
                                created_at=to_user_clock(user, now), provider="rules"))
    return outcome


def _looks_like_help(message: str) -> bool:
    help_keywords = ("help", "what can you do", "examples", "how do i", "what should i say")
    return any(keyword in message for keyword in help_keywords)


def _looks_like_weight_log(message: str) -> bool:
    if "weight" in message or "weigh" in message:
        return True
    return bool(re.search(r"\b\d{2,3}(?:\.\d+)?\s*(kg|kgs|kilograms?|lb|lbs|pounds?)\b", message))


def _looks_like_water_log(message: str) -> bool:
    if not ("water" in message or "hydrat" in message):
        return False
    return bool(re.search(r"\d", message))


# Sport/activity words that count as a "workout" mention for the rules
# predicates — the schedule/log keyword lists predate the sports catalogue.
_ACTIVITY_KEYWORDS = (
    "swim", "boxing", "box", "cycle", "cycling", "walk", "hik", "hiit",
    "pilates", "football", "soccer", "basketball", "tennis", "badminton",
    "volleyball", "mma", "karate", "taekwondo",
)


def _looks_like_workout_schedule(message: str) -> bool:
    schedule_keywords = ("schedule", "plan", "book", "set up", "create", "add")
    workout_keywords = ("workout", "session", "train", "gym", "run", "cardio", "yoga", "legs", "chest") + _ACTIVITY_KEYWORDS
    return any(keyword in message for keyword in schedule_keywords) and any(
        keyword in message for keyword in workout_keywords
    )


# --- Training environment (gym / home / home with equipment) ----------------

# The chat answer must be essentially ONLY the environment, so a normal
# schedule request mentioning "gym" can never be mistaken for the reply.
_ENV_ANSWER_RE = re.compile(
    r"^(?:the\s+)?(?:at\s+the\s+|in\s+the\s+|at\s+|in\s+)?"
    r"(gym|home(?:\s+gym)?|home\s+(?:with\s+)?equipment|equipment)\W*$"
)



def _looks_like_environment_answer(message: str) -> bool:
    return bool(_ENV_ANSWER_RE.match(message.strip()))


def _extract_environment(message: str) -> str | None:
    """Environment mentioned inside a longer message ("home push day") —
    per-session override. home+equipment must win over plain home."""
    m = message.lower()
    if re.search(r"\bhome\b", m) and re.search(r"\b(equipment|equipment[- ]?free|dumbbells?|weights?|bench|kettlebells?|bands?)\b", m):
        return "home_gym"
    if re.search(r"\bhome\b", m):
        return "home"
    if re.search(r"\bgym\b", m):
        return "gym"
    return None


def _answer_environment(user: User, message: str, now: datetime) -> CoachOutcome:
    env = _extract_environment(message) or ("home_gym" if "equipment" in message else "home" if "home" in message else "gym")
    user.training_environment = env
    # Backfill the session that triggered the question, so its plan re-renders
    # for the right environment immediately.
    pending = db.session.scalars(
        select(ScheduledWorkout)
        .where(
            ScheduledWorkout.user_id == user.id,
            ScheduledWorkout.status == "scheduled",
            ScheduledWorkout.environment.is_(None),
        )
        .order_by(ScheduledWorkout.created_at.desc())
        .limit(1)
    ).first()
    label = {"gym": "the gym", "home": "home", "home_gym": "home with your equipment"}[env]
    if pending is not None:
        pending.environment = env
        reply = f"Noted — plans will use {label}. I updated your {pending.title} session too."
    else:
        reply = f"Noted — plans will use {label} from now on. Change it anytime in Settings."
    return CoachOutcome(reply=reply, action="environment_set")


def _looks_like_meal_log(message: str) -> bool:
    meal_keywords = ("meal", "breakfast", "lunch", "dinner", "snack", "ate", "eating", "protein", "calories", "kcal")
    log_keywords = ("log", "track", "add", "had", "ate")
    return any(keyword in message for keyword in meal_keywords) and any(
        keyword in message for keyword in log_keywords
    )


def _looks_like_workout_log(message: str) -> bool:
    workout_keywords = ("workout", "session", "training", "trained", "completed", "finished", "lifted") + _ACTIVITY_KEYWORDS
    log_keywords = ("log", "track", "completed", "finished", "done", "did")
    return any(keyword in message for keyword in workout_keywords) and any(
        keyword in message for keyword in log_keywords
    )


def _looks_like_schedule_lookup(message: str) -> bool:
    lookup_markers = ("next workout", "workout schedule", "my schedule", "upcoming workout", "upcoming session")
    question_words = ("what", "when", "show", "do i have")
    return any(marker in message for marker in lookup_markers) or (
        "workout" in message and any(question in message for question in question_words)
    )


def _looks_like_progress_lookup(message: str) -> bool:
    progress_markers = ("how am i doing", "progress", "latest weight", "status", "check in", "check-in")
    return any(marker in message for marker in progress_markers)


def _log_weight(user: User, message: str, now: datetime) -> CoachOutcome:
    local_now = to_user_clock(user, now)
    weight_kg, source_unit = _extract_weight_value(message)
    logged_at = _extract_log_datetime(message, local_now)

    if logged_at > local_now:
        raise ValueError("Weight check-ins can only be logged for today or earlier.")

    previous_latest = db.session.execute(
        select(WeightLog)
        .where(WeightLog.user_id == user.id)
        .order_by(WeightLog.date.desc())
        .limit(1)
    ).scalar_one_or_none()

    db.session.add(WeightLog(weight=weight_kg, date=logged_at, user=user))

    if user.start_weight is None:
        user.start_weight = weight_kg

    _sync_current_weight(user)
    challenge_notes = update_challenges(user, {"kind": "weight", "at": logged_at})

    delta_text = ""
    if previous_latest is not None:
        delta = round(weight_kg - previous_latest.weight, 1)
        if delta < 0:
            delta_text = f" That is down {abs(delta):.1f} kg from your last check-in."
        elif delta > 0:
            delta_text = f" That is up {abs(delta):.1f} kg from your last check-in."
        else:
            delta_text = " That matches your last check-in."

    unit_note = ""
    if source_unit in {"lb", "lbs", "pound", "pounds"}:
        unit_note = " I converted it to kilograms for your dashboard."

    reply = (
        f"Logged {weight_kg:.1f} kg for {_format_date_label(logged_at, local_now)}."
        f"{delta_text}{unit_note} Your weight chart is updated."
    )
    if challenge_notes:
        reply += " " + " · ".join(challenge_notes) + "."
    return CoachOutcome(reply=reply, action="weight_logged")


def _log_water(user: User, message: str, now: datetime) -> CoachOutcome:
    lower = message.lower()
    local_now = to_user_clock(user, now)
    amount_ml = None

    ml_match = re.search(r"\b(\d{2,5})\s*(?:ml|millilitres?|milliliters?)\b", lower)
    if ml_match:
        amount_ml = int(ml_match.group(1))
    else:
        liter_match = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:l|liters?|litres?)\b", lower)
        if liter_match:
            amount_ml = round(float(liter_match.group(1)) * 1000)

    if amount_ml is None or amount_ml <= 0:
        raise ValueError("Add an amount, for example 'Log 500 ml water'.")

    db.session.add(WaterLog(amount_ml=amount_ml, logged_at=local_now, user=user))
    db.session.flush()

    day_start, day_end = day_bounds(local_now)
    today_total = (
        db.session.execute(
            select(func.coalesce(func.sum(WaterLog.amount_ml), 0)).where(
                WaterLog.user_id == user.id,
                WaterLog.logged_at >= day_start,
                WaterLog.logged_at < day_end,
            )
        ).scalar_one()
        or 0
    )

    reply = f"Logged {amount_ml} ml water. Today: {today_total / 1000:.1f} L total."
    return CoachOutcome(reply=reply, action="water_logged")


def _parse_meal(message: str, now: datetime) -> dict | None:
    """Extract meal name/type/calories/macros from a coach message.

    Returns None when no calorie or macro information is present, so the
    caller can fall back to storing a plain note.
    """
    kcal_match = KCAL_PATTERN.search(message)
    protein_match = PROTEIN_PATTERN.search(message)
    carbs_match = CARBS_PATTERN.search(message)
    fats_match = FATS_PATTERN.search(message)

    calories = int(kcal_match.group(1)) if kcal_match else None
    protein = int(protein_match.group(1)) if protein_match else 0
    carbs = int(carbs_match.group(1)) if carbs_match else 0
    fats = int(fats_match.group(1)) if fats_match else 0

    if calories is None:
        if not (protein or carbs or fats):
            return None
        calories = protein * 4 + carbs * 4 + fats * 9

    lowered = message.lower()
    meal_type = next(
        (label for keyword, label in MEAL_TYPE_KEYWORDS.items() if keyword in lowered),
        _default_meal_type(now),
    )

    name = message
    if ":" in name:
        name = name.split(":", 1)[1]
    for pattern in (KCAL_PATTERN, PROTEIN_PATTERN, CARBS_PATTERN, FATS_PATTERN):
        name = pattern.sub(" ", name)
    name = re.sub(r"\b\d{1,3}\s*g\b", " ", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip(" .,:;-–")
    if not name:
        name = meal_type.title()
    name = name[:60]

    return {
        "name": name,
        "meal_type": meal_type,
        "calories": calories,
        "protein": protein,
        "carbs": carbs,
        "fats": fats,
    }


def _log_meal(user: User, message: str, now: datetime) -> CoachOutcome:
    local_now = to_user_clock(user, now)
    parsed = _parse_meal(message, local_now)
    if parsed is None:
        return CoachOutcome(
            reply=(
                "I saved that meal note in your Activity Feed. Add calories or macros "
                "(for example '487 kcal 38p 68c 9f') and it will count toward your daily totals."
            ),
            action="meal_logged",
        )

    db.session.add(MealEntry(logged_at=local_now, user=user, **parsed))
    db.session.flush()

    day_start, day_end = day_bounds(local_now)
    today_totals = _meal_totals_between(user, day_start, day_end)
    macro_text = f"{parsed['protein']}P/{parsed['carbs']}C/{parsed['fats']}F"
    reply = (
        f"Logged {parsed['name']} ({parsed['meal_type']}) — {parsed['calories']} kcal · {macro_text}. "
        f"Today: {today_totals['calories']} kcal."
    )
    challenge_notes = update_challenges(user, {"kind": "meal", "at": local_now})
    if challenge_notes:
        reply += " " + " · ".join(challenge_notes) + "."
    return CoachOutcome(reply=reply, action="meal_logged")


def _log_workout_completion(user: User, message: str, now: datetime) -> CoachOutcome:
    local_now = to_user_clock(user, now)
    completed_at = _extract_log_datetime(message, local_now)
    if completed_at > local_now:
        completed_at = local_now

    title = _extract_workout_title(message)
    duration_minutes = _extract_duration_minutes(message)
    # MET-based burn from the activity catalogue when the title matches one
    # (e.g. "did a 45 min run") — None if the user has no weight on file.
    estimate = activities.estimate_for_message(message, user.weight, default_minutes=duration_minutes)
    calories_burned = estimate["calories"] if estimate else None

    db.session.add(
        ScheduledWorkout(
            title=title,
            scheduled_for=completed_at,
            duration_minutes=duration_minutes,
            notes=_extract_workout_notes(message, title),
            calories_burned=calories_burned,
            status="completed",
            user=user,
            created_at=local_now,
        )
    )
    db.session.flush()

    challenge_notes = update_challenges(user, {
        "kind": "workout", "at": completed_at, "title": title, "calories_burned": calories_burned,
    })

    week_stats = _week_activity(user, local_now)
    reply = (
        f"Logged {title} ({duration_minutes} min) as completed. "
        f"This week: {week_stats['done_count']} session(s) done, {week_stats['total_minutes']} min total."
    )
    if calories_burned:
        reply = (
            f"Logged {title} ({duration_minutes} min, ≈{calories_burned} kcal). "
            f"This week: {week_stats['done_count']} session(s) done, {week_stats['total_minutes']} min total."
        )
    if challenge_notes:
        reply += " " + " · ".join(challenge_notes) + "."
    return CoachOutcome(reply=reply, action="workout_logged")


def _schedule_workout(user: User, message: str, now: datetime) -> CoachOutcome:
    # Day words ("today"/"tomorrow") and the passed-time check run on the
    # user's local clock; the result stays in their wall clock.
    local_now = to_user_clock(user, now)
    scheduled_for, auto_shifted = _extract_schedule_datetime(message, local_now)
    title = _extract_workout_title(message)
    duration_minutes = _extract_duration_minutes(message)
    notes = _extract_workout_notes(message, title)
    # Per-session override from the words ("home push day"), else the profile
    # default. Plans render from this, so it wants to be right at insert time.
    environment = _extract_environment(message) or user.training_environment
    # MET-based burn estimate when the title matches a catalogue activity;
    # the session card shows it and workout_done forwards it to challenges.
    estimate = activities.estimate_for_message(message, user.weight, default_minutes=duration_minutes)

    db.session.add(
        ScheduledWorkout(
            title=title,
            scheduled_for=scheduled_for,
            duration_minutes=duration_minutes,
            notes=notes,
            calories_burned=(estimate["calories"] if estimate else None),
            environment=environment,
            status="scheduled",
            user=user,
            created_at=local_now,
        )
    )

    shift_note = ""
    if auto_shifted:
        shift_note = " The requested time had already passed, so I moved it to the next available slot."

    reply = (
        f"Scheduled {title} for {_format_datetime_label(scheduled_for, local_now)} "
        f"for {duration_minutes} min.{shift_note}"
    )
    if estimate and estimate["calories"]:
        reply += f" Estimated burn ≈{estimate['calories']} kcal."
    if environment is None:
        reply += f" {ENVIRONMENT_ASK}"
    return CoachOutcome(reply=reply, action="workout_scheduled")


def _build_schedule_lookup(user: User, now: datetime) -> CoachOutcome:
    now = to_user_clock(user, now)
    upcoming = db.session.execute(
        select(ScheduledWorkout)
        .where(
            ScheduledWorkout.user_id == user.id,
            ScheduledWorkout.status == "scheduled",
            ScheduledWorkout.scheduled_for >= now,
        )
        .order_by(ScheduledWorkout.scheduled_for.asc())
        .limit(3)
    ).scalars().all()

    if not upcoming:
        return CoachOutcome(
            reply=(
                "You do not have any workouts scheduled yet. Try "
                "'Schedule full body tomorrow at 7 am for 50 min'."
            ),
            action="schedule_lookup",
        )

    summary = "; ".join(
        f"{workout.title} on {_format_datetime_label(workout.scheduled_for, now)}"
        for workout in upcoming
    )
    return CoachOutcome(reply=f"Here is what is lined up: {summary}.", action="schedule_lookup")


def _build_progress_lookup(user: User, now: datetime) -> CoachOutcome:
    now = to_user_clock(user, now)
    recent_logs = db.session.execute(
        select(WeightLog)
        .where(WeightLog.user_id == user.id)
        .order_by(WeightLog.date.desc())
        .limit(2)
    ).scalars().all()
    next_workout = db.session.execute(
        select(ScheduledWorkout)
        .where(
            ScheduledWorkout.user_id == user.id,
            ScheduledWorkout.status == "scheduled",
            ScheduledWorkout.scheduled_for >= now,
        )
        .order_by(ScheduledWorkout.scheduled_for.asc())
        .limit(1)
    ).scalar_one_or_none()

    meal_totals = _meal_totals_between(user, *day_bounds(now))

    if not recent_logs and next_workout is None and meal_totals["calories"] == 0:
        return CoachOutcome(
            reply=(
                "We are just getting started. Log a weight check-in, a meal, or schedule a workout "
                "and I will keep score from there."
            ),
            action="progress_lookup",
        )

    parts: list[str] = []
    if meal_totals["calories"]:
        parts.append(f"Today: {meal_totals['calories']} kcal logged")
    if recent_logs:
        latest = recent_logs[0]
        parts.append(f"Latest weight: {latest.weight:.1f} kg on {_format_date_label(effective_date(latest.date), now)}")
        if len(recent_logs) > 1:
            delta = round(latest.weight - recent_logs[1].weight, 1)
            if delta < 0:
                parts.append(f"down {abs(delta):.1f} kg from the previous check-in")
            elif delta > 0:
                parts.append(f"up {abs(delta):.1f} kg from the previous check-in")
            else:
                parts.append("unchanged from the previous check-in")
    if next_workout is not None:
        parts.append(f"Next workout: {next_workout.title} at {_format_datetime_label(next_workout.scheduled_for, now)}")

    return CoachOutcome(reply=". ".join(parts) + ".", action="progress_lookup")


def _looks_like_challenge_join(message: str) -> bool:
    return re.search(r"\b(join|sign me up for|put me in)\b", message) is not None


def _looks_like_challenge_lookup(message: str) -> bool:
    return re.search(r"\bchallenge", message) is not None


def _match_challenge_title(message: str) -> Challenge | None:
    """Active challenge whose title words appear in the message; the most
    word matches wins."""
    best, best_score = None, 0
    for c in db.session.scalars(
        select(Challenge).where(Challenge.status == "active")
    ).all():
        words = [w for w in re.split(r"[^a-z0-9]+", c.title.lower()) if len(w) > 2]
        score = sum(1 for w in words if w in message)
        if score > best_score:
            best, best_score = c, score
    return best


def _join_challenge_rule(user: User, message: str, now: datetime) -> CoachOutcome:
    challenge = _match_challenge_title(message)
    if challenge is None:
        return _build_challenge_lookup(user, now)
    exists = db.session.scalar(
        select(UserChallenge.id).where(
            UserChallenge.user_id == user.id,
            UserChallenge.challenge_id == challenge.id,
        )
    )
    if exists:
        return CoachOutcome(
            reply=f"You're already in {challenge.title} — keep logging to move the needle.",
            action="challenge",
        )
    db.session.add(UserChallenge(user_id=user.id, challenge_id=challenge.id))
    return CoachOutcome(
        reply=f"Joined {challenge.title}! Every qualifying log counts from now on.",
        action="challenge_joined",
    )


def _build_challenge_lookup(user: User, now: datetime) -> CoachOutcome:
    active = db.session.scalars(
        select(Challenge).where(Challenge.status == "active").order_by(Challenge.title)
    ).all()
    mine = {
        uc.challenge_id: uc
        for uc in db.session.scalars(
            select(UserChallenge).where(
                UserChallenge.user_id == user.id, UserChallenge.status == "active",
            )
        ).all()
    }
    if not active:
        return CoachOutcome(
            reply="There are no open challenges right now — check the Challenges page soon.",
            action="challenge",
        )
    lines = []
    for c in active:
        uc = mine.get(c.id)
        if uc is not None:
            lines.append(
                f"{c.title} — joined, progress {uc.progress_value:g}/{c.target_value:g}, streak {uc.current_streak}"
            )
        else:
            lines.append(f"{c.title} — {c.description or c.type}")
    return CoachOutcome(
        reply="Open challenges: " + " · ".join(lines) + ". Say 'join <name>' and you're in.",
        action="challenge",
    )


def _extract_weight_value(message: str) -> tuple[float, str]:
    pattern = re.search(
        r"\b(?P<value>\d{2,3}(?:\.\d+)?)\s*(?P<unit>kg|kgs|kilograms?|lb|lbs|pounds?)\b",
        message.lower(),
    )
    if pattern:
        value = float(pattern.group("value"))
        unit = pattern.group("unit")
        if unit.startswith("lb") or unit.startswith("pound"):
            return round(value * 0.45359237, 1), unit
        return round(value, 1), unit

    fallback = re.search(r"\b(?:weight|weigh|weighed)\D{0,12}(?P<value>\d{2,3}(?:\.\d+)?)\b", message.lower())
    if fallback:
        return round(float(fallback.group("value")), 1), "kg"

    raise ValueError("Please include your weight in the message, for example 'Log my weight as 82.4 kg'.")


def _extract_log_datetime(message: str, now: datetime) -> datetime:
    lower = message.lower()

    if "yesterday" in lower:
        return now - timedelta(days=1)

    explicit_date = _extract_explicit_date(lower)
    if explicit_date is not None:
        return datetime.combine(explicit_date, now.time().replace(second=0, microsecond=0))

    return now


def _extract_schedule_datetime(message: str, now: datetime) -> tuple[datetime, bool]:
    lower = message.lower()
    explicit_date = _extract_explicit_date(lower)
    scheduled_date = explicit_date
    explicit_day_reference = explicit_date is not None
    auto_shifted = False

    if scheduled_date is None:
        if "day after tomorrow" in lower:
            scheduled_date = (now + timedelta(days=2)).date()
            explicit_day_reference = True
        elif "tomorrow" in lower:
            scheduled_date = (now + timedelta(days=1)).date()
            explicit_day_reference = True
        elif "today" in lower:
            scheduled_date = now.date()
            explicit_day_reference = True
        else:
            for weekday_name, weekday_index in WEEKDAY_INDEX.items():
                if weekday_name in lower:
                    delta_days = (weekday_index - now.weekday()) % 7
                    if delta_days == 0:
                        delta_days = 7
                    scheduled_date = (now + timedelta(days=delta_days)).date()
                    explicit_day_reference = True
                    break

    parsed_time = _extract_time(lower)
    if parsed_time is None:
        if "morning" in lower:
            parsed_time = time(hour=7, minute=0)
        elif "afternoon" in lower:
            parsed_time = time(hour=15, minute=0)
        else:
            parsed_time = time(hour=18, minute=0)

    if scheduled_date is None:
        candidate = datetime.combine(now.date(), parsed_time)
        if candidate <= now:
            candidate = datetime.combine((now + timedelta(days=1)).date(), parsed_time)
            auto_shifted = True
        return candidate, auto_shifted

    candidate = datetime.combine(scheduled_date, parsed_time)
    if candidate <= now and "today" in lower:
        candidate = datetime.combine((now + timedelta(days=1)).date(), parsed_time)
        auto_shifted = True
    elif candidate <= now and not explicit_day_reference:
        candidate = datetime.combine((now + timedelta(days=1)).date(), parsed_time)
        auto_shifted = True

    return candidate, auto_shifted


def _extract_explicit_date(message: str):
    for pattern in (r"\b(\d{4})-(\d{2})-(\d{2})\b", r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"):
        match = re.search(pattern, message)
        if match is None:
            continue
        try:
            if pattern.startswith(r"\b(\d{4})"):
                year, month, day = map(int, match.groups())
            else:
                day, month, year = map(int, match.groups())
            return datetime(year=year, month=month, day=day).date()
        except ValueError as exc:
            raise ValueError("I could not understand that date. Try 2026-04-12 or 12/04/2026.") from exc
    return None


def _extract_time(message: str) -> time | None:
    if "noon" in message:
        return time(hour=12, minute=0)
    if "midnight" in message:
        return time(hour=0, minute=0)

    # "7:30 pm", "7.30 pm" and "7pm" all count — people type dots for colons.
    meridiem_match = re.search(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)\b", message)
    if meridiem_match:
        hour = int(meridiem_match.group(1))
        minute = int(meridiem_match.group(2) or 0)
        meridiem = meridiem_match.group(3)
        if hour == 12:
            hour = 0
        if meridiem == "pm":
            hour += 12
        return time(hour=hour, minute=minute)

    twenty_four_match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", message)
    if twenty_four_match:
        return time(hour=int(twenty_four_match.group(1)), minute=int(twenty_four_match.group(2)))

    relaxed_match = re.search(r"\bat\s+(\d{1,2})\b", message)
    if relaxed_match:
        hour = int(relaxed_match.group(1))
        if 0 <= hour <= 23:
            return time(hour=hour, minute=0)

    return None


def _extract_duration_minutes(message: str) -> int:
    lower = message.lower()
    total_minutes = 0

    hour_match = re.search(r"\b(\d+)\s*(hour|hours|hr|hrs)\b", lower)
    minute_match = re.search(r"\b(\d+)\s*(minute|minutes|min|mins)\b", lower)

    if hour_match:
        total_minutes += int(hour_match.group(1)) * 60
    if minute_match:
        total_minutes += int(minute_match.group(1))

    return min(total_minutes or 60, 240)


def _extract_workout_title(message: str) -> str:
    lower = message.lower()
    for alias, label in WORKOUT_LABELS:
        if alias in lower:
            return label

    captured = re.search(
        r"(?:schedule|plan|book|set up|create|add)\s+(?:a\s+)?(?P<title>.+?)\s+(?:for|at|on|tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        lower,
    )
    if captured:
        raw_title = captured.group("title").replace("workout", "").replace("session", "").strip(" -")
        if raw_title:
            return raw_title.title()

    return "Workout Session"


def _extract_workout_notes(message: str, title: str) -> str | None:
    lowered = message.lower()
    cleaned = lowered.replace(title.lower(), "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or None


def _format_date_label(value: datetime, now: datetime) -> str:
    if value.date() == now.date():
        return "today"
    if value.date() == (now - timedelta(days=1)).date():
        return "yesterday"
    return value.strftime("%d %b")


def _format_datetime_label(value: datetime, now: datetime) -> str:
    day_label = value.strftime("%d %b")
    if value.date() == now.date():
        day_label = "today"
    elif value.date() == (now + timedelta(days=1)).date():
        day_label = "tomorrow"
    return f"{day_label} at {value.strftime('%I:%M %p').lstrip('0')}"


