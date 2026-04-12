from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from sqlalchemy import select

from .extensions import db
from .models import CoachMessage, ScheduledWorkout, User, WeightLog

COACH_QUICK_STARTS = [
    "Log my weight as 82.4 kg today",
    "Log my meal: oats, banana, and milk for breakfast",
    "Schedule legs tomorrow at 7 am for 60 min",
    "Log my workout: completed upper body for 45 min today",
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
    ("run", "Run"),
    ("running", "Run"),
    ("walk", "Walk"),
    ("yoga", "Yoga"),
    ("mobility", "Mobility"),
    ("stretch", "Stretch"),
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


@dataclass
class CoachOutcome:
    reply: str
    action: str = "general"


def process_coach_message(user: User, message_text: str, now: datetime | None = None) -> CoachOutcome:
    now = now or datetime.now()
    cleaned_message = " ".join((message_text or "").split())

    if not cleaned_message:
        raise ValueError("Type something for the coach to work with.")

    normalized = cleaned_message.lower()

    db.session.add(CoachMessage(role="user", content=cleaned_message, user=user))

    if _looks_like_help(normalized):
        outcome = CoachOutcome(
            reply=(
                "I can log weight, queue meal notes, schedule workouts, and keep workout notes in your activity feed. "
                "Try: "
                + "; ".join(COACH_QUICK_STARTS)
                + "."
            ),
            action="help",
        )
    elif _looks_like_schedule_lookup(normalized):
        outcome = _build_schedule_lookup(user, now)
    elif _looks_like_progress_lookup(normalized):
        outcome = _build_progress_lookup(user, now)
    elif _looks_like_weight_log(normalized):
        outcome = _log_weight(user, cleaned_message, now)
    elif _looks_like_workout_schedule(normalized):
        outcome = _schedule_workout(user, cleaned_message, now)
    elif _looks_like_meal_log(normalized):
        outcome = _capture_meal_note()
    elif _looks_like_workout_log(normalized):
        outcome = _capture_workout_note()
    else:
        outcome = CoachOutcome(
            reply=(
                "I did not fully catch that yet. Try something like "
                "'Log my weight as 81.9 kg', 'Log my meal: paneer wrap for lunch', "
                "or 'Schedule cardio tomorrow at 6:30 pm for 45 min'."
            ),
            action="fallback",
        )

    db.session.add(CoachMessage(role="assistant", content=outcome.reply, user=user))
    return outcome


def _looks_like_help(message: str) -> bool:
    help_keywords = ("help", "what can you do", "examples", "how do i", "what should i say")
    return any(keyword in message for keyword in help_keywords)


def _looks_like_weight_log(message: str) -> bool:
    if "weight" in message or "weigh" in message:
        return True
    return bool(re.search(r"\b\d{2,3}(?:\.\d+)?\s*(kg|kgs|kilograms?|lb|lbs|pounds?)\b", message))


def _looks_like_workout_schedule(message: str) -> bool:
    schedule_keywords = ("schedule", "plan", "book", "set up", "create", "add")
    workout_keywords = ("workout", "session", "train", "gym", "run", "cardio", "yoga", "legs", "chest")
    return any(keyword in message for keyword in schedule_keywords) and any(
        keyword in message for keyword in workout_keywords
    )


def _looks_like_meal_log(message: str) -> bool:
    meal_keywords = ("meal", "breakfast", "lunch", "dinner", "snack", "ate", "eating", "protein", "calories")
    log_keywords = ("log", "track", "add", "had", "ate")
    return any(keyword in message for keyword in meal_keywords) and any(
        keyword in message for keyword in log_keywords
    )


def _looks_like_workout_log(message: str) -> bool:
    workout_keywords = ("workout", "session", "training", "trained", "completed", "finished", "lifted")
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
    weight_kg, source_unit = _extract_weight_value(message)
    logged_at = _extract_log_datetime(message, now)

    if logged_at > now:
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
        f"Logged {weight_kg:.1f} kg for {_format_date_label(logged_at, now)}."
        f"{delta_text}{unit_note} Your weight chart is updated."
    )
    return CoachOutcome(reply=reply, action="weight_logged")


def _schedule_workout(user: User, message: str, now: datetime) -> CoachOutcome:
    scheduled_for, auto_shifted = _extract_schedule_datetime(message, now)
    title = _extract_workout_title(message)
    duration_minutes = _extract_duration_minutes(message)
    notes = _extract_workout_notes(message, title)

    db.session.add(
        ScheduledWorkout(
            title=title,
            scheduled_for=scheduled_for,
            duration_minutes=duration_minutes,
            notes=notes,
            status="scheduled",
            user=user,
        )
    )

    shift_note = ""
    if auto_shifted:
        shift_note = " The requested time had already passed, so I moved it to the next available slot."

    reply = (
        f"Scheduled {title} for {_format_datetime_label(scheduled_for, now)} "
        f"for {duration_minutes} min.{shift_note}"
    )
    return CoachOutcome(reply=reply, action="workout_scheduled")


def _capture_meal_note() -> CoachOutcome:
    return CoachOutcome(
        reply=(
            "I saved that meal note in your Activity Feed. "
            "Macro totals and nutrition search are the next upgrade, but your note is stored here already."
        ),
        action="meal_logged",
    )


def _capture_workout_note() -> CoachOutcome:
    return CoachOutcome(
        reply=(
            "I saved that workout update in your Activity Feed. "
            "Structured workout completion tracking is the next upgrade, but your note is stored here already."
        ),
        action="workout_logged",
    )


def _build_schedule_lookup(user: User, now: datetime) -> CoachOutcome:
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

    if not recent_logs and next_workout is None:
        return CoachOutcome(
            reply=(
                "We are just getting started. Log a weight check-in or schedule a workout and I will keep score from there."
            ),
            action="progress_lookup",
        )

    parts: list[str] = []
    if recent_logs:
        latest = recent_logs[0]
        parts.append(f"Latest weight: {latest.weight:.1f} kg on {_format_date_label(latest.date, now)}")
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

    meridiem_match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", message)
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


def _sync_current_weight(user: User) -> None:
    latest = db.session.execute(
        select(WeightLog)
        .where(WeightLog.user_id == user.id)
        .order_by(WeightLog.date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is not None:
        user.weight = latest.weight


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
