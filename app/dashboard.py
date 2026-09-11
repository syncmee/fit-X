"""Single home for everything the fiT-X dashboard needs.

This module owns:
- Health math: BMI (adult + CDC age-adjusted) and EER-based calorie targets.
- The fiT-X coach: rule-based natural-language processing for weight check-ins,
  meal logging (with kcal/macros), water, workout scheduling/completion, and
  progress lookups.
- The dashboard context builder that feeds templates/dashboard.html.
- The dashboard blueprint routes (/dashboard, /coach/message, /meals/*,
  /water/add, /workouts/<id>/done).

Nothing else in the app should import health or coach helpers directly; if a
feature needs them, expose it through this module.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from functools import lru_cache
from math import log
from pathlib import Path
from statistics import NormalDist

import requests
from flask import Blueprint, current_app, flash, has_request_context, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, select

from .extensions import db
from .models import CoachMessage, MealEntry, ScheduledWorkout, User, WaterLog, WeightLog

dashboard_bp = Blueprint("dashboard", __name__)

# ============================================================
# Health math (BMI + calorie targets)
# ============================================================

BMI_DATA_PATH = Path(__file__).resolve().parent / "data" / "bmiagerev.csv"
NORMAL_DISTRIBUTION = NormalDist()

ACTIVITY_LEVEL_LABELS = {
    "sedentary": "Inactive",
    "light": "Low Active",
    "moderate": "Active",
    "athlete": "Very Active",
}

ADULT_EER_COEFFICIENTS = {
    "male": {
        "sedentary": (753.07, -10.83, 6.50, 14.10),
        "light": (581.47, -10.83, 8.30, 14.94),
        "moderate": (1004.82, -10.83, 6.52, 15.91),
        "athlete": (-517.88, -10.83, 15.61, 19.11),
    },
    "female": {
        "sedentary": (584.90, -7.01, 5.72, 11.71),
        "light": (575.77, -7.01, 6.60, 12.14),
        "moderate": (710.25, -7.01, 6.54, 12.34),
        "athlete": (511.83, -7.01, 9.07, 12.56),
    },
}

TEEN_EER_COEFFICIENTS = {
    "male": {
        "younger": {
            "sedentary": (-447.51, 3.68, 13.01, 13.15, 25),
            "light": (19.12, 3.68, 8.62, 20.28, 25),
            "moderate": (-388.19, 3.68, 12.66, 20.46, 25),
            "athlete": (-671.75, 3.68, 15.38, 23.25, 25),
        },
        "older": {
            "sedentary": (-447.51, 3.68, 13.01, 13.15, 20),
            "light": (19.12, 3.68, 8.62, 20.28, 20),
            "moderate": (-388.19, 3.68, 12.66, 20.46, 20),
            "athlete": (-671.75, 3.68, 15.38, 23.25, 20),
        },
    },
    "female": {
        "younger": {
            "sedentary": (55.59, -22.25, 8.43, 17.07, 30),
            "light": (-297.54, -22.25, 12.77, 14.73, 30),
            "moderate": (-189.55, -22.25, 11.74, 18.34, 30),
            "athlete": (-709.59, -22.25, 18.22, 14.25, 30),
        },
        "older": {
            "sedentary": (55.59, -22.25, 8.43, 17.07, 20),
            "light": (-297.54, -22.25, 12.77, 14.73, 20),
            "moderate": (-189.55, -22.25, 11.74, 18.34, 20),
            "athlete": (-709.59, -22.25, 18.22, 14.25, 20),
        },
    },
}

DEFAULT_CALORIE_TARGET = {
    "target": 2000,
    "maintenance": 2000,
    "activity_label": "Profile needed",
    "note": "Complete your profile for an age-aware calorie estimate.",
}

DEFAULT_BMI_SUMMARY = {
    "value": 0.0,
    "status_label": "Complete profile",
    "status_class": "text-gray-400",
    "detail": "Add height, weight, age, and sex for an age-aware BMI estimate.",
}


def calculate_bmi(weight_kg: float, height_cm: float) -> float:
    height_in_meters = height_cm / 100
    if height_in_meters <= 0:
        return 0.0
    return round(weight_kg / (height_in_meters**2), 1)


def estimate_calorie_target(
    *,
    gender: str,
    age: int,
    height_cm: float,
    weight_kg: float,
    activity_level: str,
    goal: str,
) -> dict[str, int | str]:
    maintenance = _calculate_eer(
        gender=gender,
        age=age,
        height_cm=height_cm,
        weight_kg=weight_kg,
        activity_level=activity_level,
    )
    target = maintenance

    if goal == "lose":
        adjustment = _goal_adjustment(maintenance=maintenance, age=age, goal=goal)
        target = maintenance - adjustment
        note = (
            f"Age-aware estimate with {ACTIVITY_LEVEL_LABELS[activity_level]} activity. "
            f"Maintenance is about {maintenance} kcal; target uses a gentle {adjustment} kcal deficit."
        )
    elif goal == "gain":
        adjustment = _goal_adjustment(maintenance=maintenance, age=age, goal=goal)
        target = maintenance + adjustment
        note = (
            f"Age-aware estimate with {ACTIVITY_LEVEL_LABELS[activity_level]} activity. "
            f"Maintenance is about {maintenance} kcal; target uses a steady {adjustment} kcal surplus."
        )
    else:
        note = (
            f"Age-aware estimate with {ACTIVITY_LEVEL_LABELS[activity_level]} activity. "
            f"Maintenance lands around {maintenance} kcal."
        )

    return {
        "target": int(round(target)),
        "maintenance": int(round(maintenance)),
        "activity_label": ACTIVITY_LEVEL_LABELS[activity_level],
        "note": note,
    }


def build_bmi_summary(*, gender: str, age: int, weight_kg: float, height_cm: float) -> dict[str, float | str]:
    bmi = calculate_bmi(weight_kg, height_cm)
    if bmi <= 0:
        return DEFAULT_BMI_SUMMARY.copy()

    if age >= 20:
        return _build_adult_bmi_summary(bmi)

    return _build_child_bmi_summary(gender=gender, age=age, bmi=bmi)


def _calculate_eer(
    *,
    gender: str,
    age: int,
    height_cm: float,
    weight_kg: float,
    activity_level: str,
) -> int:
    if age >= 19:
        baseline, age_coeff, height_coeff, weight_coeff = ADULT_EER_COEFFICIENTS[gender][activity_level]
        value = baseline + (age_coeff * age) + (height_coeff * height_cm) + (weight_coeff * weight_kg)
        return int(round(value))

    teen_group = "younger" if age < 14 else "older"
    baseline, age_coeff, height_coeff, weight_coeff, growth_allowance = TEEN_EER_COEFFICIENTS[gender][teen_group][activity_level]
    value = (
        baseline
        + (age_coeff * age)
        + (height_coeff * height_cm)
        + (weight_coeff * weight_kg)
        + growth_allowance
    )
    return int(round(value))


def _goal_adjustment(*, maintenance: int, age: int, goal: str) -> int:
    # The official equations estimate maintenance/EER. Goal pacing stays
    # intentionally gentler for teens because their estimates already include
    # growth needs, and the dashboard should avoid pushing aggressive cuts.
    if goal == "lose":
        if age < 19:
            return min(300, max(150, int(round(maintenance * 0.10))))
        return min(500, max(200, int(round(maintenance * 0.15))))

    if goal == "gain":
        if age < 19:
            return min(250, max(150, int(round(maintenance * 0.08))))
        return min(350, max(150, int(round(maintenance * 0.10))))

    return 0


def _build_adult_bmi_summary(bmi: float) -> dict[str, float | str]:
    if bmi < 18.5:
        label = "Below Range"
        css_class = "text-yellow"
    elif bmi < 25:
        label = "Healthy Range"
        css_class = "text-green"
    elif bmi < 30:
        label = "Above Range"
        css_class = "text-yellow"
    else:
        label = "High Range"
        css_class = "text-red-400"

    return {
        "value": bmi,
        "status_label": label,
        "status_class": css_class,
        "detail": "Adult BMI estimate.",
    }


def _build_child_bmi_summary(*, gender: str, age: int, bmi: float) -> dict[str, float | str]:
    reference = _lookup_bmi_reference(gender=gender, age=age)
    if reference is None:
        return _build_adult_bmi_summary(bmi)

    percentile = _calculate_percentile(bmi=bmi, reference=reference)
    percentile_label = f"{_format_percentile(percentile)} percentile"
    severe_obesity_threshold = max(35.0, reference["P95"] * 1.2)

    if bmi < reference["P5"]:
        label = "Below Range"
        css_class = "text-yellow"
    elif bmi < reference["P85"]:
        label = "Healthy Range"
        css_class = "text-green"
    else:
        label = "High Range"
        css_class = "text-red-400"

    return {
        "value": bmi,
        "status_label": label,
        "status_class": css_class,
        "detail": f"CDC age-adjusted BMI - {percentile_label}.",
    }


def _calculate_percentile(*, bmi: float, reference: dict[str, float]) -> int:
    if reference["L"] == 0:
        z_score = 0.0 if bmi <= 0 else log(bmi / reference["M"]) / reference["S"]
    else:
        z_score = (((bmi / reference["M"]) ** reference["L"]) - 1) / (reference["L"] * reference["S"])
    percentile = max(1, min(99, int(round(NORMAL_DISTRIBUTION.cdf(z_score) * 100))))
    return percentile


def _format_percentile(percentile: int) -> str:
    if 10 <= percentile % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(percentile % 10, "th")
    return f"{percentile}{suffix}"


def _lookup_bmi_reference(*, gender: str, age: int) -> dict[str, float] | None:
    if gender not in {"male", "female"}:
        return None

    # Onboarding currently captures whole years only, so use the mid-year point
    # for the closest CDC BMI-for-age reference row.
    age_in_months = (age * 12) + 6
    sex_key = 1 if gender == "male" else 2
    candidates = _load_bmi_reference_rows().get(sex_key, [])
    if not candidates:
        return None

    return min(candidates, key=lambda row: abs(row["Agemos"] - age_in_months))


@lru_cache(maxsize=1)
def _load_bmi_reference_rows() -> dict[int, list[dict[str, float]]]:
    rows: dict[int, list[dict[str, float]]] = {1: [], 2: []}

    with BMI_DATA_PATH.open(newline="", encoding="utf-8") as csv_file:
        reader = csv.DictReader(csv_file)
        for raw_row in reader:
            try:
                sex = int(raw_row["Sex"])
            except (TypeError, ValueError):
                continue
            rows[sex].append(
                {
                    "Agemos": float(raw_row["Agemos"]),
                    "L": float(raw_row["L"]),
                    "M": float(raw_row["M"]),
                    "S": float(raw_row["S"]),
                    "P5": float(raw_row["P5"]),
                    "P85": float(raw_row["P85"]),
                    "P95": float(raw_row["P95"]),
                }
            )

    return rows


# ============================================================
# fiT-X coach (rule-based natural language logging)
# ============================================================

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


@dataclass
class CoachOutcome:
    reply: str
    action: str = "general"


def process_coach_message(user: User, message_text: str, now: datetime | None = None) -> CoachOutcome:
    """Dispatch to the AI coach when a Gemini key is configured, else to the
    rule-based parser. The rule parser is always the safety net."""
    now = now or datetime.now()
    cleaned_message = " ".join((message_text or "").split())

    if not cleaned_message:
        raise ValueError("Type something for the coach to work with.")

    api_key = current_app.config.get("GEMINI_API_KEY", "")
    if api_key:
        try:
            return process_coach_message_ai(user, cleaned_message, now)
        except Exception:
            current_app.logger.warning("Gemini coach failed; falling back to rules.", exc_info=True)
            if has_request_context():
                flash("fiT-X AI could not be reached — used the built-in parser for that message. Check GEMINI_API_KEY.", "error")

    return process_coach_message_rules(user, cleaned_message, now)


def process_coach_message_rules(user: User, message_text: str, now: datetime | None = None) -> CoachOutcome:
    now = now or datetime.now()
    cleaned_message = " ".join((message_text or "").split())

    if not cleaned_message:
        raise ValueError("Type something for the coach to work with.")

    normalized = cleaned_message.lower()

    db.session.add(CoachMessage(role="user", content=cleaned_message, user=user))

    if _looks_like_help(normalized):
        outcome = CoachOutcome(
            reply=(
                "I can log food with calories and macros, log weight, track water, schedule or complete "
                "workouts, and report your progress. Try: "
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

    db.session.add(CoachMessage(role="assistant", content=outcome.reply, user=user))
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


def _looks_like_workout_schedule(message: str) -> bool:
    schedule_keywords = ("schedule", "plan", "book", "set up", "create", "add")
    workout_keywords = ("workout", "session", "train", "gym", "run", "cardio", "yoga", "legs", "chest")
    return any(keyword in message for keyword in schedule_keywords) and any(
        keyword in message for keyword in workout_keywords
    )


def _looks_like_meal_log(message: str) -> bool:
    meal_keywords = ("meal", "breakfast", "lunch", "dinner", "snack", "ate", "eating", "protein", "calories", "kcal")
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


def _log_water(user: User, message: str, now: datetime) -> CoachOutcome:
    lower = message.lower()
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

    db.session.add(WaterLog(amount_ml=amount_ml, logged_at=now, user=user))
    db.session.flush()

    today_total = (
        db.session.execute(
            select(func.coalesce(func.sum(WaterLog.amount_ml), 0)).where(
                WaterLog.user_id == user.id,
                func.date(WaterLog.logged_at) == now.date(),
            )
        ).scalar_one()
        or 0
    )

    reply = f"Logged {amount_ml} ml water. Today: {today_total / 1000:.1f} L total."
    return CoachOutcome(reply=reply, action="water_logged")


def _default_meal_type(now: datetime) -> str:
    hour = now.hour
    if hour < 11:
        return "breakfast"
    if hour < 16:
        return "lunch"
    if hour < 19:
        return "snack"
    return "dinner"


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
    parsed = _parse_meal(message, now)
    if parsed is None:
        return CoachOutcome(
            reply=(
                "I saved that meal note in your Activity Feed. Add calories or macros "
                "(for example '487 kcal 38p 68c 9f') and it will count toward your daily totals."
            ),
            action="meal_logged",
        )

    db.session.add(MealEntry(logged_at=now, user=user, **parsed))
    db.session.flush()

    today_totals = _sum_meal_entries(_get_meal_entries(now.date(), now.date()))
    macro_text = f"{parsed['protein']}P/{parsed['carbs']}C/{parsed['fats']}F"
    reply = (
        f"Logged {parsed['name']} ({parsed['meal_type']}) — {parsed['calories']} kcal · {macro_text}. "
        f"Today: {today_totals['calories']} kcal."
    )
    return CoachOutcome(reply=reply, action="meal_logged")


def _log_workout_completion(user: User, message: str, now: datetime) -> CoachOutcome:
    completed_at = _extract_log_datetime(message, now)
    if completed_at > now:
        completed_at = now

    title = _extract_workout_title(message)
    duration_minutes = _extract_duration_minutes(message)

    db.session.add(
        ScheduledWorkout(
            title=title,
            scheduled_for=completed_at,
            duration_minutes=duration_minutes,
            notes=_extract_workout_notes(message, title),
            status="completed",
            user=user,
        )
    )
    db.session.flush()

    week_stats = _build_week_activity(now)
    reply = (
        f"Logged {title} ({duration_minutes} min) as completed. "
        f"This week: {week_stats['done_count']} session(s) done, {week_stats['total_minutes']} min total."
    )
    return CoachOutcome(reply=reply, action="workout_logged")


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

    meal_totals = _sum_meal_entries(_get_meal_entries(now.date(), now.date()))

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


# ============================================================
# fiT-X AI coach (Gemini)
# ============================================================

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
AI_HISTORY_LIMIT = 6
AI_TIMEOUT_SECONDS = 45


class CoachAIError(Exception):
    """Raised when the Gemini call fails or returns unusable output."""


def _build_ai_context(user: User, now: datetime) -> str:
    today_totals = _sum_meal_entries(_get_meal_entries(now.date(), now.date()))
    calorie_target = DEFAULT_CALORIE_TARGET.copy()
    if (
        user.weight is not None
        and user.height is not None
        and user.age is not None
        and user.gender is not None
        and user.activity_level is not None
        and user.goal is not None
    ):
        calorie_target = estimate_calorie_target(
            gender=user.gender,
            age=user.age,
            height_cm=user.height,
            weight_kg=user.weight,
            activity_level=user.activity_level,
            goal=user.goal,
        )

    week_activity = _build_week_activity(now)
    upcoming = _get_upcoming_workouts(now)
    next_workout = upcoming[0] if upcoming else None
    water_l = _get_water_today_liters(now)

    return json.dumps(
        {
            "today": now.strftime("%Y-%m-%d (%A)"),
            "local_time": now.strftime("%H:%M"),
            "user": {
                "name": user.name,
                "weight_kg": user.weight,
                "height_cm": user.height,
                "age": user.age,
                "gender": user.gender,
                "goal": user.goal,
                "target_weight_kg": user.target_weight,
                "activity_level": user.activity_level,
                "daily_calorie_target": calorie_target["target"],
                "maintenance_calories": calorie_target["maintenance"],
            },
            "today_logged": {
                "calories": today_totals["calories"],
                "protein_g": today_totals["protein"],
                "carbs_g": today_totals["carbs"],
                "fats_g": today_totals["fats"],
                "water_liters": water_l,
            },
            "this_week": {
                "sessions_done": week_activity["done_count"],
                "sessions_planned": week_activity["session_count"],
                "active_minutes": week_activity["total_minutes"],
                "kcal_burned": week_activity.get("total_burned", 0),
            },
            "next_workout": (
                f"{next_workout.title} at {next_workout.scheduled_for.strftime('%a %Y-%m-%d %H:%M')}"
                if next_workout
                else None
            ),
        },
        ensure_ascii=False,
    )


def _ai_system_prompt(user: User, now: datetime) -> str:
    return f"""You are fiT-X, the AI coach inside a fitness web app. You understand natural language in any phrasing and turn it into structured logging actions for the user's account.

Current date and time on the user's device: {_build_ai_context(user, now)}

You reply with ONLY one JSON object, no markdown fences, matching exactly:
{{
  "reply": "short friendly confirmation or answer (max 3 sentences)",
  "actions": []
}}

ACTION TYPES (each an object inside "actions"; use an empty array for pure questions):
1. {{"type":"log_weight","weight_kg":number,"days_ago":0}}  — weight check-in; days_ago 0-7 (1 means yesterday). Valid weight 30-350 kg.
2. {{"type":"log_meal","name":"short food name","meal_type":"breakfast|lunch|dinner|snack","calories":int,"protein":int,"carbs":int,"fats":int}}  — grams of protein/carbs/fat. If the user gives only macros, set calories = protein*4 + carbs*4 + fats*9. If only calories are given, leave macros 0. Pick meal_type from context (time of day or the user's words).
3. {{"type":"log_water","amount_ml":int}}  — 100-5000 ml. "a glass" ≈ 250, "a bottle" ≈ 500.
4. {{"type":"schedule_workout","title":"Activity","days_ahead":0,"time_24h":"18:30","duration_minutes":int,"calories_burned":int}}  — ANY activity counts: gym, running, cycling, swimming, yoga, pilates, boxing, martial arts, sports, walking. days_ahead 0-30 (0 = today; if that time already passed, use tomorrow). time_24h defaults to the user's words or "18:00". duration 5-240 min. Estimate calories_burned from the user's weight, duration and intensity (light yoga ≈ 3 kcal/kg/h, brisk walk ≈ 4.3, cycling ≈ 6, running ≈ 10, HIIT/boxing ≈ 9; round to the nearest 10).
5. {{"type":"complete_workout","title":"Activity","days_ago":0,"duration_minutes":int,"calories_burned":int}}  — the user already finished it. days_ago 0-7. Estimate burn the same way.

RULES:
- The user may phrase anything casually ("had 2 rotis and dal", "did 40 min zone 2", "leg day tomorrow 7am"). Interpret sensibly and fill every field; if a required value is genuinely unknown or implausible, do NOT invent it — ask one short question in "reply" with empty actions.
- For progress questions ("how am i doing", "what did I eat today"), answer from the context data above with empty actions.
- Reply in the language the user writes in.
- Never wrap the JSON in markdown fences."""


def _gemini_generate(user: User, message: str, now: datetime) -> dict:
    api_key = current_app.config.get("GEMINI_API_KEY", "")
    model = current_app.config.get("GEMINI_MODEL", "gemini-2.5-flash")

    history = db.session.execute(
        select(CoachMessage)
        .where(CoachMessage.user_id == user.id)
        .order_by(CoachMessage.id.desc())
        .limit(AI_HISTORY_LIMIT)
    ).scalars().all()
    contents = []
    for row in reversed(history):
        contents.append(
            {"role": "user" if row.role == "user" else "model", "parts": [{"text": row.content}]}
        )
    contents.append({"role": "user", "parts": [{"text": message}]})

    payload = {
        "system_instruction": {"parts": [{"text": _ai_system_prompt(user, now)}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 800,
            "responseMimeType": "application/json",
        },
    }

    try:
        response = requests.post(
            GEMINI_API_URL.format(model=model),
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=AI_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise CoachAIError(f"Gemini request failed: {exc}") from exc

    if response.status_code != 200:
        raise CoachAIError(f"Gemini returned {response.status_code}: {response.text[:300]}")

    try:
        text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, ValueError) as exc:
        raise CoachAIError(f"Gemini response shape unexpected: {response.text[:300]}") from exc

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise CoachAIError(f"Gemini returned non-JSON: {cleaned[:300]}") from exc

    if not isinstance(parsed, dict) or not isinstance(parsed.get("reply"), str):
        raise CoachAIError(f"Gemini JSON missing reply: {cleaned[:300]}")

    actions = parsed.get("actions", [])
    return {"reply": parsed["reply"].strip(), "actions": actions if isinstance(actions, list) else []}


def _clamp_int(value, minimum: int, maximum: int, default: int = 0) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _apply_ai_actions(user: User, actions: list, now: datetime) -> list[str]:
    applied: list[str] = []
    for action in actions[:6]:
        if not isinstance(action, dict):
            continue
        action_type = action.get("type")

        if action_type == "log_weight":
            weight_kg = _clamp_int(action.get("weight_kg"), 30, 350, default=0)
            if not weight_kg:
                continue
            days_ago = _clamp_int(action.get("days_ago"), 0, 7, default=0)
            logged_at = now - timedelta(days=days_ago)
            db.session.add(WeightLog(weight=float(weight_kg), date=logged_at, user=user))
            if user.start_weight is None:
                user.start_weight = float(weight_kg)
            _sync_current_weight(user)
            applied.append(f"weight {weight_kg} kg")

        elif action_type == "log_meal":
            name = " ".join(str(action.get("name") or "Meal").split())[:60]
            meal_type = str(action.get("meal_type") or "").lower()
            if meal_type not in MEAL_TYPE_ORDER:
                meal_type = _default_meal_type(now)
            protein = _clamp_int(action.get("protein"), 0, 500)
            carbs = _clamp_int(action.get("carbs"), 0, 800)
            fats = _clamp_int(action.get("fats"), 0, 300)
            calories = _clamp_int(action.get("calories"), 0, 10000)
            if calories == 0 and (protein or carbs or fats):
                calories = protein * 4 + carbs * 4 + fats * 9
            if calories <= 0:
                continue
            db.session.add(
                MealEntry(
                    name=name,
                    meal_type=meal_type,
                    calories=calories,
                    protein=protein,
                    carbs=carbs,
                    fats=fats,
                    logged_at=now,
                    user=user,
                )
            )
            applied.append(f"{name} ({calories} kcal)")

        elif action_type == "log_water":
            amount_ml = _clamp_int(action.get("amount_ml"), 100, 5000, default=0)
            if not amount_ml:
                continue
            db.session.add(WaterLog(amount_ml=amount_ml, logged_at=now, user=user))
            applied.append(f"{amount_ml} ml water")

        elif action_type in {"schedule_workout", "complete_workout"}:
            title = " ".join(str(action.get("title") or "Workout").split())[:80]
            duration_minutes = _clamp_int(action.get("duration_minutes"), 5, 300, default=30)
            calories_burned = _clamp_int(action.get("calories_burned"), 0, 4000, default=0) or None

            if action_type == "schedule_workout":
                days_ahead = _clamp_int(action.get("days_ahead"), 0, 30, default=0)
                raw_time = str(action.get("time_24h") or "18:00")
                try:
                    hour, minute = (int(part) for part in raw_time.split(":")[:2])
                    scheduled_time = time(hour=max(0, min(23, hour)), minute=max(0, min(59, minute)))
                except ValueError:
                    scheduled_time = time(hour=18, minute=0)
                scheduled_for = datetime.combine((now + timedelta(days=days_ahead)).date(), scheduled_time)
                if scheduled_for <= now:
                    scheduled_for = datetime.combine((now + timedelta(days=1)).date(), scheduled_time)
                db.session.add(
                    ScheduledWorkout(
                        title=title,
                        scheduled_for=scheduled_for,
                        duration_minutes=duration_minutes,
                        calories_burned=calories_burned,
                        status="scheduled",
                        user=user,
                    )
                )
                applied.append(f"scheduled {title}")
            else:
                days_ago = _clamp_int(action.get("days_ago"), 0, 7, default=0)
                completed_at = now - timedelta(days=days_ago)
                db.session.add(
                    ScheduledWorkout(
                        title=title,
                        scheduled_for=completed_at,
                        duration_minutes=duration_minutes,
                        calories_burned=calories_burned,
                        status="completed",
                        user=user,
                    )
                )
                applied.append(f"completed {title}")

    return applied


def process_coach_message_ai(user: User, message_text: str, now: datetime) -> CoachOutcome:
    result = _gemini_generate(user, message_text, now)
    applied = _apply_ai_actions(user, result["actions"], now)
    db.session.add(CoachMessage(role="user", content=message_text, user=user))
    db.session.add(CoachMessage(role="assistant", content=result["reply"], user=user))
    return CoachOutcome(reply=result["reply"], action="ai")


# ============================================================
# Meal / water / activity queries
# ============================================================

MEAL_TYPE_ORDER = ("breakfast", "lunch", "dinner", "snack")


def _get_meal_entries(date_from, date_to) -> list[MealEntry]:
    return list(
        db.session.execute(
            select(MealEntry)
            .where(
                MealEntry.user_id == current_user.id,
                func.date(MealEntry.logged_at) >= date_from,
                func.date(MealEntry.logged_at) <= date_to,
            )
            .order_by(MealEntry.logged_at.asc())
        ).scalars().all()
    )


def _sum_meal_entries(entries: list[MealEntry]) -> dict[str, int]:
    return {
        "calories": sum(e.calories for e in entries),
        "protein": sum(e.protein for e in entries),
        "carbs": sum(e.carbs for e in entries),
        "fats": sum(e.fats for e in entries),
    }


def _build_macro_targets(weight_kg: float | None, target_kcal: int) -> dict[str, int]:
    if weight_kg:
        protein_g = round(weight_kg * 1.8)
        fat_g = round(weight_kg * 0.9)
    else:
        protein_g = round(target_kcal * 0.3 / 4)
        fat_g = round(target_kcal * 0.25 / 9)
    carbs_g = max(0, round((target_kcal - protein_g * 4 - fat_g * 9) / 4))
    return {"protein": protein_g, "carbs": carbs_g, "fats": fat_g}


def _build_week_net(now: datetime, target_kcal: int) -> tuple[list[str], list[int]]:
    labels: list[str] = []
    nets: list[int] = []

    per_day = dict(
        db.session.execute(
            select(func.date(MealEntry.logged_at), func.sum(MealEntry.calories))
            .where(
                MealEntry.user_id == current_user.id,
                func.date(MealEntry.logged_at) >= (now - timedelta(days=6)).date(),
            )
            .group_by(func.date(MealEntry.logged_at))
        ).all()
    )

    for offset in range(6, -1, -1):
        day = (now - timedelta(days=offset)).date()
        intake = int(per_day.get(day, 0) or 0)
        labels.append(day.strftime("%a")[0])
        nets.append(intake - target_kcal)

    return labels, nets


def _get_water_today_liters(now: datetime) -> float:
    total_ml = (
        db.session.execute(
            select(func.coalesce(func.sum(WaterLog.amount_ml), 0)).where(
                WaterLog.user_id == current_user.id,
                func.date(WaterLog.logged_at) == now.date(),
            )
        ).scalar_one()
        or 0
    )
    return round(total_ml / 1000, 2)


def _build_week_activity(now: datetime) -> dict:
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)

    workouts = db.session.execute(
        select(ScheduledWorkout)
        .where(
            ScheduledWorkout.user_id == current_user.id,
            ScheduledWorkout.scheduled_for >= week_start,
            ScheduledWorkout.scheduled_for < week_end,
        )
    ).scalars().all()

    minutes_by_day = {i: 0 for i in range(7)}
    for workout in workouts:
        minutes_by_day[workout.scheduled_for.weekday()] += workout.duration_minutes or 0

    labels = ["M", "T", "W", "T", "F", "S", "S"]
    minutes = [minutes_by_day[i] for i in range(7)]
    total_minutes = sum(minutes)
    done_count = sum(1 for w in workouts if w.status == "completed")
    session_count = len(workouts)
    total_burned = sum(w.calories_burned or 0 for w in workouts if w.status == "completed")

    return {
        "labels": labels,
        "minutes": minutes,
        "total_minutes": total_minutes,
        "session_count": session_count,
        "done_count": done_count,
        "avg_duration": round(total_minutes / session_count) if session_count else 0,
        "total_burned": total_burned,
    }


def _build_weeks_minutes_history(weeks: int = 8) -> tuple[list[str], list[int]]:
    now = datetime.now()
    this_week_monday = (now - timedelta(days=now.weekday())).date()

    rows = db.session.execute(
        select(ScheduledWorkout.scheduled_for, ScheduledWorkout.duration_minutes, ScheduledWorkout.status).where(
            ScheduledWorkout.user_id == current_user.id,
            ScheduledWorkout.status == "completed",
        )
    ).all()

    per_week: dict[int, int] = {}
    for scheduled_for, duration, _status in rows:
        if scheduled_for is None:
            continue
        week_index = (this_week_monday - (scheduled_for.date() - timedelta(days=scheduled_for.weekday()))).days // 7
        if 0 <= week_index < weeks:
            per_week[week_index] = per_week.get(week_index, 0) + (duration or 0)

    labels = [f"W-{weeks - 1 - i}" if i < weeks - 1 else "Now" for i in range(weeks)]
    minutes = [per_week.get(weeks - 1 - i, 0) for i in range(weeks)]
    return labels, minutes


def _build_heatmap_levels(now: datetime, days: int = 84) -> list[int]:
    weight_dates = {
        row[0]
        for row in db.session.execute(
            select(func.date(WeightLog.date)).where(WeightLog.user_id == current_user.id)
        ).all()
    }
    meal_dates = {
        row[0]
        for row in db.session.execute(
            select(func.date(MealEntry.logged_at)).where(MealEntry.user_id == current_user.id)
        ).all()
    }

    levels: list[int] = []
    for offset in range(days - 1, -1, -1):
        day = (now - timedelta(days=offset)).date()
        level = 0
        if day in meal_dates:
            level += 1
        if day in weight_dates:
            level += 2
        levels.append(level)

    return levels


# ============================================================
# Dashboard context
# ============================================================

GOAL_PHASE_LABELS = {"lose": "Cut Phase", "gain": "Bulk Phase", "maintain": "Maintain Phase"}
WEIGHT_SERIES_LIMIT = 90
RING_CIRCUMFERENCE = 97.3  # matches the r=15.5 SVG ring in the template
HEATMAP_COLORS = ["bg-white/5", "bg-yellow/30", "bg-green/50", "bg-green"]


def _calculate_goal_progress(*, start_weight: float, current_weight: float | None, target_weight: float) -> int:
    """Direction-independent goal progress in percent.

    Uses a signed projection instead of abs(): any movement toward the target
    counts up (cut or gain alike), movement away from it counts down. E.g.
    80 -> 85 kg (gain) currently at 82 reads 40%; a 85 -> 80 kg cut at 83
    also reads 40%.
    """
    if current_weight is None:
        return 0

    total_change_needed = target_weight - start_weight
    if total_change_needed == 0:
        # Start and target are the same weight: on it, or not.
        return 100 if abs(current_weight - start_weight) < 0.05 else 0

    direction = 1 if total_change_needed > 0 else -1
    traveled = (current_weight - start_weight) * direction
    return max(0, min(100, round(traveled / abs(total_change_needed) * 100)))


def _user_initials(name: str) -> str:
    parts = [part for part in name.split() if part]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _logging_streak(logs: list[WeightLog]) -> tuple[int, list[bool]]:
    dates = {log.date.date() for log in logs if log.date is not None}
    today = datetime.now().date()
    if not dates:
        return 0, [False] * 7

    day = today if today in dates else today - timedelta(days=1)
    streak = 0
    while day in dates:
        streak += 1
        day -= timedelta(days=1)

    week = [(today - timedelta(days=offset)) in dates for offset in range(6, -1, -1)]
    return streak, week


def _get_upcoming_workouts(now: datetime) -> list[ScheduledWorkout]:
    return list(
        db.session.execute(
            select(ScheduledWorkout)
            .where(
                ScheduledWorkout.user_id == current_user.id,
                ScheduledWorkout.status == "scheduled",
                ScheduledWorkout.scheduled_for >= now,
            )
            .order_by(ScheduledWorkout.scheduled_for.asc())
            .limit(5)
        ).scalars().all()
    )


def _get_recent_completed(now: datetime, limit: int = 5) -> list[ScheduledWorkout]:
    return list(
        db.session.execute(
            select(ScheduledWorkout)
            .where(
                ScheduledWorkout.user_id == current_user.id,
                ScheduledWorkout.status == "completed",
                ScheduledWorkout.scheduled_for < now + timedelta(minutes=30),
            )
            .order_by(ScheduledWorkout.scheduled_for.desc())
            .limit(limit)
        ).scalars().all()
    )


def _build_week_strip(now: datetime) -> list[dict]:
    week_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)
    scheduled = db.session.execute(
        select(ScheduledWorkout)
        .where(
            ScheduledWorkout.user_id == current_user.id,
            ScheduledWorkout.scheduled_for >= week_start,
            ScheduledWorkout.scheduled_for < week_end,
        )
    ).scalars().all()
    by_date: dict = {}
    for workout in scheduled:
        by_date.setdefault(workout.scheduled_for.date(), []).append(workout)

    strip = []
    for offset in range(7):
        day = (week_start + timedelta(days=offset)).date()
        day_workouts = by_date.get(day, [])
        is_today = offset == 0
        if is_today and day_workouts:
            state = "today"
        elif day_workouts:
            state = "planned"
        else:
            state = "open"
        strip.append(
            {
                "day_label": day.strftime("%a").upper(),
                "date_label": day.strftime("%d"),
                "title": day_workouts[0].title if day_workouts else "Rest",
                "extra_count": len(day_workouts) - 1,
                "state": state,
            }
        )
    return strip


def _format_workout_label(value: datetime, now: datetime) -> str:
    if value.date() == now.date():
        day = "Today"
    elif value.date() == (now + timedelta(days=1)).date():
        day = "Tomorrow"
    else:
        day = value.strftime("%a, %d %b")
    return f"{day} at {value.strftime('%I:%M %p').lstrip('0')}"


def _build_weight_data() -> dict:
    logs = list(current_user.logs)
    latest_delta = None
    if len(logs) >= 2:
        latest_delta = round(logs[-1].weight - logs[-2].weight, 1)

    if latest_delta is None:
        delta_display = "First check-in"
        delta_class = "text-gray-500"
    elif latest_delta < 0:
        delta_display = f"▼ {abs(latest_delta):.1f} kg"
        delta_class = "text-green"
    elif latest_delta > 0:
        delta_display = f"▲ {latest_delta:.1f} kg"
        delta_class = "text-yellow"
    else:
        delta_display = "No change"
        delta_class = "text-gray-500"

    recent = logs[-7:]
    trend_avg = round(sum(log.weight for log in recent) / len(recent), 1) if recent else None

    values = [round(log.weight, 1) for log in logs[-WEIGHT_SERIES_LIMIT:]]
    labels = [log.date.strftime("%d %b") if log.date else "" for log in logs[-WEIGHT_SERIES_LIMIT:]]
    if len(values) == 1:
        values = values * 2
        labels = [labels[0], "Now"]
    elif not values and current_user.weight is not None:
        values = [round(current_user.weight, 1)] * 2
        labels = ["Start", "Now"]

    return {
        "values": values,
        "labels": labels,
        "trend_avg": trend_avg,
        "delta_display": delta_display,
        "delta_class": delta_class,
    }


def _build_dashboard_context() -> dict:
    now = datetime.now()
    calorie_target = DEFAULT_CALORIE_TARGET.copy()
    bmi_summary = DEFAULT_BMI_SUMMARY.copy()
    goal_progress = 0

    if (
        current_user.weight is not None
        and current_user.height is not None
        and current_user.age is not None
        and current_user.gender is not None
    ):
        bmi_summary = build_bmi_summary(
            gender=current_user.gender,
            age=current_user.age,
            weight_kg=current_user.weight,
            height_cm=current_user.height,
        )

    if (
        current_user.weight is not None
        and current_user.height is not None
        and current_user.age is not None
        and current_user.gender is not None
        and current_user.activity_level is not None
        and current_user.goal is not None
    ):
        calorie_target = estimate_calorie_target(
            gender=current_user.gender,
            age=current_user.age,
            height_cm=current_user.height,
            weight_kg=current_user.weight,
            activity_level=current_user.activity_level,
            goal=current_user.goal,
        )

        if current_user.start_weight is not None and current_user.target_weight is not None:
            goal_progress = _calculate_goal_progress(
                start_weight=current_user.start_weight,
                current_weight=current_user.weight,
                target_weight=current_user.target_weight,
            )

    target_kcal = calorie_target["target"]

    # --- Food / hydration (today) ---
    today_meals = _get_meal_entries(now.date(), now.date())
    meals_by_type: dict[str, list[MealEntry]] = {meal_type: [] for meal_type in MEAL_TYPE_ORDER}
    for entry in today_meals:
        meals_by_type.setdefault(entry.meal_type, []).append(entry)
    meal_type_totals = {
        meal_type: sum(e.calories for e in entries) for meal_type, entries in meals_by_type.items()
    }
    today_totals = _sum_meal_entries(today_meals)

    intake_pct = min(100, round(today_totals["calories"] / target_kcal * 100)) if target_kcal else 0
    intake_remaining = max(0, target_kcal - today_totals["calories"])

    macro_targets = _build_macro_targets(current_user.weight, target_kcal)

    week_net_labels, week_net_values = _build_week_net(now, target_kcal)

    water_goal_l = round((current_user.weight or 85) * 0.035, 1)
    water_today_l = _get_water_today_liters(now)
    water_pct = min(100, round(water_today_l / water_goal_l * 100)) if water_goal_l else 0
    water_segments = min(12, round(water_pct / 100 * 12))

    # --- Weight ---
    weight_data = _build_weight_data()
    streak, streak_week = _logging_streak(list(current_user.logs))

    # --- Workouts ---
    upcoming_workouts = _get_upcoming_workouts(now)
    next_workout = upcoming_workouts[0] if upcoming_workouts else None
    recent_completed = _get_recent_completed(now)
    week_activity = _build_week_activity(now)
    weeks_history_labels, weeks_history_minutes = _build_weeks_minutes_history()

    weekly_workout_count = db.session.execute(
        select(func.count(ScheduledWorkout.id)).where(
            ScheduledWorkout.user_id == current_user.id,
            ScheduledWorkout.status == "scheduled",
            ScheduledWorkout.scheduled_for >= now,
            ScheduledWorkout.scheduled_for < now + timedelta(days=7),
        )
    ).scalar_one()

    chat_messages = list(
        reversed(
            db.session.execute(
                select(CoachMessage)
                .where(CoachMessage.user_id == current_user.id)
                .order_by(CoachMessage.id.desc())
                .limit(COACH_CHAT_HISTORY_LIMIT)
            ).scalars().all()
        )
    )

    first_log = current_user.logs[0] if current_user.logs else None
    day_count = (now.date() - first_log.date.date()).days + 1 if first_log and first_log.date else 1

    phase_label = GOAL_PHASE_LABELS.get(current_user.goal or "", "Set your goal")

    return {
        "user": current_user,
        "user_initials": _user_initials(current_user.name),
        "phase_label": phase_label,
        "day_count": day_count,
        "now_label": f"{now.strftime('%a · %b %d · ')}{now.strftime('%I:%M %p').lstrip('0')}",
        # Calories / metabolism
        "calories": target_kcal,
        "maintenance_calories": calorie_target["maintenance"],
        "activity_level_label": calorie_target["activity_label"],
        "calorie_target_note": calorie_target["note"],
        # Today's intake (drives the daily ring)
        "intake_today": today_totals["calories"],
        "intake_pct": intake_pct,
        "intake_remaining": intake_remaining,
        "intake_over": today_totals["calories"] > target_kcal,
        "ring_offset": round(RING_CIRCUMFERENCE * (1 - intake_pct / 100), 1),
        # Weight-goal progress lives on the weight card now
        "goal_progress": goal_progress,
        # Meals
        "meals_by_type": meals_by_type,
        "meal_type_totals": meal_type_totals,
        "today_totals": today_totals,
        "macro_targets": macro_targets,
        "week_net_labels": week_net_labels,
        "week_net_values": week_net_values,
        # Water
        "water_today_l": f"{water_today_l:.1f}",
        "water_goal_l": f"{water_goal_l:.1f}",
        "water_pct": water_pct,
        "water_segments": water_segments,
        # Weight
        "weight_display": f"{current_user.weight:.1f}" if current_user.weight is not None else "--",
        "weight_delta_display": weight_data["delta_display"],
        "weight_delta_class": weight_data["delta_class"],
        "trend_avg": weight_data["trend_avg"],
        "weight_values": weight_data["values"],
        "weight_labels": weight_data["labels"],
        "goal_weight": current_user.target_weight,
        # BMI
        "bmi": bmi_summary["value"],
        "bmi_status_label": bmi_summary["status_label"],
        "bmi_status_class": bmi_summary["status_class"],
        "bmi_detail": bmi_summary["detail"],
        # Streak
        "streak": streak,
        "streak_week": streak_week,
        # Workouts
        "upcoming_workouts": upcoming_workouts,
        "recent_completed": recent_completed,
        "next_workout": next_workout,
        "next_workout_label": _format_workout_label(next_workout.scheduled_for, now) if next_workout else None,
        "weekly_workout_count": weekly_workout_count,
        "recent_log_count": len(current_user.logs),
        "week_strip": _build_week_strip(now),
        "week_activity": week_activity,
        "weeks_history_labels": weeks_history_labels,
        "weeks_history_minutes": weeks_history_minutes,
        # Progress / adherence
        "heatmap_levels": _build_heatmap_levels(now),
        "heatmap_colors": HEATMAP_COLORS,
        # Coach
        "chat_messages": chat_messages,
        "coach_examples": COACH_QUICK_STARTS,
        "coach_mode_label": "fiT-X AI",
    }


# ============================================================
# Routes
# ============================================================

def _parse_nonnegative_int(value: str | None, default: int = 0, maximum: int = 10000) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(0, min(parsed, maximum))


@dashboard_bp.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", **_build_dashboard_context())


@dashboard_bp.route("/coach/message", methods=["POST"])
@login_required
def coach_message():
    raw_message = request.form.get("message", "")
    try:
        process_coach_message(current_user, raw_message)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    except Exception:
        db.session.rollback()
        flash("The coach could not process that request. Please try again.", "error")

    return redirect(url_for("dashboard.dashboard", _anchor="coach"))


@dashboard_bp.route("/meals/add", methods=["POST"])
@login_required
def meals_add():
    name = " ".join((request.form.get("name") or "").split())[:60]
    meal_type = request.form.get("meal_type", "meal")
    if meal_type not in MEAL_TYPE_ORDER:
        meal_type = _default_meal_type(datetime.now())

    calories = _parse_nonnegative_int(request.form.get("calories"), default=0, maximum=10000)
    protein = _parse_nonnegative_int(request.form.get("protein"), maximum=1000)
    carbs = _parse_nonnegative_int(request.form.get("carbs"), maximum=1000)
    fats = _parse_nonnegative_int(request.form.get("fats"), maximum=1000)

    if calories == 0 and (protein or carbs or fats):
        calories = protein * 4 + carbs * 4 + fats * 9

    if not name or calories <= 0:
        flash("Give the food a name and its calories (or macros).", "error")
        return redirect(url_for("dashboard.dashboard", _anchor="nutrition"))

    db.session.add(
        MealEntry(
            name=name,
            meal_type=meal_type,
            calories=calories,
            protein=protein,
            carbs=carbs,
            fats=fats,
            user=current_user,
        )
    )
    db.session.commit()
    flash(f"Logged {name} — {calories} kcal.", "success")
    return redirect(url_for("dashboard.dashboard", _anchor="nutrition"))


@dashboard_bp.route("/meals/<int:entry_id>/delete", methods=["POST"])
@login_required
def meals_delete(entry_id: int):
    entry = db.session.get(MealEntry, entry_id)
    if entry is None or entry.user_id != current_user.id:
        flash("That entry does not exist.", "error")
        return redirect(url_for("dashboard.dashboard", _anchor="nutrition"))

    db.session.delete(entry)
    db.session.commit()
    return redirect(url_for("dashboard.dashboard", _anchor="nutrition"))


@dashboard_bp.route("/water/add", methods=["POST"])
@login_required
def water_add():
    amount_ml = _parse_nonnegative_int(request.form.get("amount_ml"), default=0, maximum=5000)
    if amount_ml <= 0:
        flash("Pick an amount to log.", "error")
        return redirect(url_for("dashboard.dashboard", _anchor="nutrition"))

    db.session.add(WaterLog(amount_ml=amount_ml, user=current_user))
    db.session.commit()
    flash(f"Logged {amount_ml} ml water.", "success")
    return redirect(url_for("dashboard.dashboard", _anchor="nutrition"))


@dashboard_bp.route("/workouts/<int:workout_id>/done", methods=["POST"])
@login_required
def workout_done(workout_id: int):
    workout = db.session.get(ScheduledWorkout, workout_id)
    if workout is None or workout.user_id != current_user.id:
        flash("That workout does not exist.", "error")
        return redirect(url_for("dashboard.dashboard", _anchor="workouts"))

    workout.status = "completed"
    if workout.scheduled_for > datetime.now() + timedelta(minutes=5):
        workout.scheduled_for = datetime.now()

    db.session.commit()
    flash(f"{workout.title} marked as done — nice work.", "success")
    return redirect(url_for("dashboard.dashboard", _anchor="workouts"))
