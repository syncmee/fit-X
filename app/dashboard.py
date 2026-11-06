"""Single home for everything the fiT-X dashboard needs.

This module owns:
- Health math: BMI (adult + CDC age-adjusted) and EER-based calorie targets.
- The fiT-X coach: rule-based natural-language processing for weight check-ins,
  meal logging (with kcal/macros), water, workout scheduling/completion, and
  progress lookups.
- The dashboard context builder that feeds templates/dashboard.html.
- The dashboard blueprint routes (/dashboard, /coach/message, /meals/*,
  /water/add, /workouts/<id>/done).

Time conventions (everything is stored as the user's wall clock) live in
app/timeutil.py.

Nothing else in the app should import health or coach helpers directly; if a
feature needs them, expose it through this module.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from math import log
from pathlib import Path
from secrets import compare_digest
from statistics import NormalDist
from time import monotonic, sleep

from flask import Blueprint, current_app, flash, has_request_context, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from .extensions import db, limiter
from . import foods as foods_module
from .coach import ENVIRONMENT_ASK, CoachOutcome
from .coach import providers as providers_module
from . import cron as cron_module
from .cron import cleanup_old_idempotency_keys
from .cron import send_challenge_reminders as send_due_challenge_reminders
from .cron import send_engagement_nudges as send_due_engagement_nudges
from .cron import send_workout_reminders as _send_due_workout_reminders
from .coach.rules import COACH_QUICK_STARTS, _looks_like_environment_answer, process_coach_message_rules
from .coach.actions import apply_actions as _apply_ai_actions
from .coach.actions import CONFIRM_PREFIX as _CONFIRM_PREFIX
from .coach.actions import default_meal_type as _default_meal_type
from .coach.actions import sync_current_weight as _sync_current_weight
from .coach.prompt import chat_day_start as _chat_day_start
from .coach.prompt import prune_old_chat_messages as _prune_old_chat_messages
from .challenges import _day_qualifies, _joinable, challenge_visible_to, progress_view, run_daily_maintenance, update_challenges
from .health import DEFAULT_BMI_SUMMARY, DEFAULT_CALORIE_TARGET, build_bmi_summary, estimate_calorie_target
from .models import (
    MEAL_TYPE_ORDER,
    Challenge,
    CoachMessage,
    Food,
    IdempotencyKey,
    MealEntry,
    PushSubscription,
    ScheduledWorkout,
    User,
    UserBadge,
    UserChallenge,
    WaterLog,
    WeightLog,
)
from .plans import build_plan
from .validation import validate_environment, validate_profile_form
from .timeutil import (
    APP_DAY_START,
    DAY_START_SHIFT,
    day_bounds,
    effective_date,
    effective_today,
    to_user_clock,
    user_now,
)

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.before_request
def require_completed_onboarding():
    # Someone who closed the browser mid-onboarding still authenticates on
    # their next visit (remembered/restored session, PWA start_url is
    # /dashboard) — send them back to finish the form instead of serving a
    # dashboard full of null-profile defaults. Anonymous users fall through to
    # each view's @login_required; /cron/send-reminders authenticates by
    # secret header and has no session, so it passes the same check.
    if current_user.is_authenticated and not current_user.onboarding:
        return redirect(url_for("main.onboarding"))
    return None


# Throttle window for last_active_at refreshes — one write per user per 5
# minutes of activity, which is plenty for the admin DAU/WAU stats.
_LAST_ACTIVE_INTERVAL = timedelta(minutes=5)


@dashboard_bp.before_request
def touch_last_active():
    """Keep User.last_active_at fresh for the admin panel's DAU/WAU stats.
    User wall clock (app/timeutil.py), refreshed at most every 5 minutes."""
    if current_user.is_authenticated:
        now = user_now(current_user)
        last = current_user.last_active_at
        if last is None or (now - last) >= _LAST_ACTIVE_INTERVAL:
            current_user.last_active_at = now
            db.session.commit()
    return None

# ============================================================
# fiT-X coach (rule-based natural language logging)
# ============================================================

COACH_CHAT_HISTORY_LIMIT = 12

_AI_CIRCUIT_FAILURES: dict[str, tuple[int, float]] = {}
_AI_CIRCUIT_COOLDOWN_SECONDS = 60


def _provider_available(name: str) -> bool:
    """Open a short circuit after repeated provider failures.

    This prevents every request from waiting through an already-known provider
    outage; the rules coach remains available immediately.
    """
    failures, opened_at = _AI_CIRCUIT_FAILURES.get(name, (0, 0.0))
    return not (failures >= 2 and monotonic() - opened_at < _AI_CIRCUIT_COOLDOWN_SECONDS)


def _record_provider_failure(name: str) -> None:
    failures, _opened_at = _AI_CIRCUIT_FAILURES.get(name, (0, 0.0))
    _AI_CIRCUIT_FAILURES[name] = (failures + 1, monotonic())


def _record_provider_success(name: str) -> None:
    _AI_CIRCUIT_FAILURES.pop(name, None)


def process_coach_message(user: User, message_text: str, now: datetime | None = None) -> CoachOutcome:
    """Dispatch to the AI coach (Gemini first, then Groq as fallback), else to
    the rule-based parser. The rule parser is always the safety net."""
    now = now or datetime.utcnow()
    cleaned_message = " ".join((message_text or "").split())

    if not cleaned_message:
        raise ValueError("Type something for the coach to work with.")

    ai_attempted = False
    # A bare "gym" / "home with equipment" reply is a structured chip answer —
    # route it straight to the rules layer even when AI is configured, or the
    # model would answer it conversationally and the setting would never save.
    if _looks_like_environment_answer(cleaned_message.lower()):
        return process_coach_message_rules(user, cleaned_message, now)
    # One wall-clock budget for the whole Gemini -> Groq chain (providers.py
    # caps each per-request timeout by what is left of it).
    ai_deadline = monotonic() + providers_module.TOTAL_AI_DEADLINE_SECONDS
    if current_app.config.get("GEMINI_API_KEY", "") and _provider_available("gemini"):
        ai_attempted = True
        started = monotonic()
        try:
            outcome = process_coach_message_ai(
                user, cleaned_message, now, provider_name="gemini", deadline=ai_deadline)
            _record_provider_success("gemini")
            return outcome
        except Exception as exc:
            _record_provider_failure("gemini")
            # Drop any partial writes from the failed attempt so the fallback
            # starts from a clean session; keep the attempt as telemetry
            # (committed immediately so later rollbacks can't drop it).
            db.session.rollback()
            db.session.add(CoachMessage(
                role="error", content=str(exc)[:200], user=user,
                created_at=to_user_clock(user, now), provider="gemini",
                latency_ms=int((monotonic() - started) * 1000), had_error=True,
            ))
            db.session.commit()
            current_app.logger.warning("Gemini coach failed; trying Groq fallback.", exc_info=True)
    if current_app.config.get("GROQ_API_KEY", "") and _provider_available("groq"):
        ai_attempted = True
        started = monotonic()
        try:
            outcome = process_coach_message_ai(
                user, cleaned_message, now, provider=providers_module.groq_generate, provider_name="groq",
                deadline=ai_deadline,
            )
            _record_provider_success("groq")
            return outcome
        except Exception as exc:
            _record_provider_failure("groq")
            db.session.rollback()
            db.session.add(CoachMessage(
                role="error", content=str(exc)[:200], user=user,
                created_at=to_user_clock(user, now), provider="groq",
                latency_ms=int((monotonic() - started) * 1000), had_error=True,
            ))
            db.session.commit()
            current_app.logger.warning("Groq coach failed; falling back to rules.", exc_info=True)

    if ai_attempted and has_request_context():
        flash(
            "fiT-X AI could not be reached — used the built-in parser for that message. "
            "Check GEMINI_API_KEY / GROQ_API_KEY.",
            "error",
        )

    return process_coach_message_rules(user, cleaned_message, now)


# ============================================================
# fiT-X AI coach (Gemini)
# ============================================================

def process_coach_message_ai(
    user: User, message_text: str, now: datetime, provider=None, provider_name: str = "gemini",
    deadline: float | None = None,
) -> CoachOutcome:
    provider = provider or providers_module.gemini_generate
    started = monotonic()
    result = provider(user, message_text, now, deadline=deadline)
    latency_ms = int((monotonic() - started) * 1000)
    applied, challenge_notes = _apply_ai_actions(user, result["actions"], now)
    # Sanity-guarded actions were not logged; the guard question becomes part
    # of the reply so the user can confirm or correct the amount.
    confirmations = [a[len(_CONFIRM_PREFIX):] for a in applied if a.startswith(_CONFIRM_PREFIX)]
    applied = [a for a in applied if not a.startswith(_CONFIRM_PREFIX)]
    reply = result["reply"]
    if confirmations:
        reply += " " + " ".join(confirmations)
    if challenge_notes:
        reply += " " + " · ".join(challenge_notes) + "."
    if user.training_environment is None and any(a.startswith("scheduled") for a in applied):
        reply += f" {ENVIRONMENT_ASK}"
    usage = result.get("usage") or {}
    db.session.add(CoachMessage(role="user", content=message_text, user=user, created_at=to_user_clock(user, now)))
    db.session.add(CoachMessage(
        role="assistant",
        content=reply,
        user=user,
        created_at=to_user_clock(user, now),
        provider=provider_name,
        latency_ms=latency_ms,
        had_error=False,
        prompt_tokens=usage.get("prompt"),
        completion_tokens=usage.get("completion"),
        raw_model_json=json.dumps(result, ensure_ascii=False),
    ))
    return CoachOutcome(reply=reply, action="ai")


# ============================================================
# Meal / water / activity queries
# ============================================================



def _get_meal_entries_between(user: User, start: datetime, end: datetime) -> list[MealEntry]:
    """Meals whose logged_at (user wall clock) falls inside the fiT-X-day
    window [start, end) — pass bounds from day_bounds()."""
    return list(
        db.session.execute(
            select(MealEntry)
            .where(
                MealEntry.user_id == user.id,
                MealEntry.logged_at >= start,
                MealEntry.logged_at < end,
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


def _build_macro_targets(weight_kg: float | None, target_kcal: int, goal: str | None) -> dict[str, int]:
    """Same split as the onboarding preview: protein per kg by goal, fats at
    25% of calories, carbs fill the remainder."""
    protein_per_kg = {"lose": 2.2, "gain": 1.8, "maintain": 2.0}.get(goal or "maintain", 2.0)
    if weight_kg:
        protein_g = round(weight_kg * protein_per_kg)
    else:
        protein_g = round(target_kcal * 0.30 / 4)
    fat_kcal = target_kcal * 0.25
    fat_g = round(fat_kcal / 9)
    carbs_g = max(0, round((target_kcal - protein_g * 4 - fat_kcal) / 4))
    return {"protein": protein_g, "carbs": carbs_g, "fats": fat_g}


def _build_week_net(user: User, local_now: datetime, target_kcal: int) -> tuple[list[str], list]:
    """Net calories for the current Monday–Sunday week. Days run on the fiT-X
    clock (they roll over at 04:30 local), and days after today stay None so
    the chart never renders a bar for a future date."""
    today = effective_date(local_now)
    week_start = today - timedelta(days=today.weekday())

    # [start, end) of the week on the fiT-X clock: Monday 04:30 local.
    span_start = datetime.combine(week_start, APP_DAY_START)
    span_end = span_start + timedelta(days=7)
    per_day: dict = {}
    for entry in _get_meal_entries_between(user, span_start, span_end):
        day = effective_date(entry.logged_at)
        per_day[day] = per_day.get(day, 0) + entry.calories

    labels = ["M", "T", "W", "T", "F", "S", "S"]
    nets: list = []
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        if day > today:
            nets.append(None)
        else:
            nets.append(int(per_day.get(day, 0) or 0) - target_kcal)
    return labels, nets


def _get_water_today_liters(user: User, local_now: datetime) -> float:
    start, end = day_bounds(local_now)
    total_ml = (
        db.session.execute(
            select(func.coalesce(func.sum(WaterLog.amount_ml), 0)).where(
                WaterLog.user_id == user.id,
                WaterLog.logged_at >= start,
                WaterLog.logged_at < end,
            )
        ).scalar_one()
        or 0
    )
    return round(total_ml / 1000, 2)


def _build_week_activity(user: User, local_now: datetime) -> dict:
    # Weeks run on the fiT-X clock too: Monday 04:30 to Monday 04:30, matching
    # the net-calorie chart, so a 1 AM Monday session counts as Sunday.
    today = effective_date(local_now)
    week_start = datetime.combine(today - timedelta(days=today.weekday()), APP_DAY_START)
    week_end = week_start + timedelta(days=7)

    workouts = db.session.execute(
        select(ScheduledWorkout)
        .where(
            ScheduledWorkout.user_id == user.id,
            ScheduledWorkout.scheduled_for >= week_start,
            ScheduledWorkout.scheduled_for < week_end,
        )
    ).scalars().all()

    # Stats count only sessions the user actually did — a scheduled workout
    # isn't effort yet, so it must not log minutes or calories.
    completed = [w for w in workouts if w.status == "completed"]

    minutes_by_day = {i: 0 for i in range(7)}
    for workout in completed:
        minutes_by_day[effective_date(workout.scheduled_for).weekday()] += workout.duration_minutes or 0

    labels = ["M", "T", "W", "T", "F", "S", "S"]
    minutes = [minutes_by_day[i] for i in range(7)]
    total_minutes = sum(minutes)
    done_count = len(completed)
    session_count = len(workouts)
    total_burned = sum(w.calories_burned or 0 for w in completed)

    return {
        "labels": labels,
        "minutes": minutes,
        "total_minutes": total_minutes,
        "session_count": session_count,
        "done_count": done_count,
        "avg_duration": round(total_minutes / done_count) if done_count else 0,
        "total_burned": total_burned,
    }


def _build_weeks_minutes_history(user: User, local_now: datetime, weeks: int = 8) -> tuple[list[str], list[int]]:
    today = effective_date(local_now)
    this_week_monday = today - timedelta(days=today.weekday())

    earliest = datetime.combine(this_week_monday - timedelta(weeks=weeks - 1), time.min)
    rows = db.session.execute(
        select(ScheduledWorkout.scheduled_for, ScheduledWorkout.duration_minutes, ScheduledWorkout.status).where(
            ScheduledWorkout.user_id == user.id,
            ScheduledWorkout.status == "completed",
            ScheduledWorkout.scheduled_for >= earliest,
        )
    ).all()

    per_week: dict[int, int] = {}
    for scheduled_for, duration, _status in rows:
        if scheduled_for is None:
            continue
        day = effective_date(scheduled_for)
        week_index = (this_week_monday - (day - timedelta(days=day.weekday()))).days // 7
        if 0 <= week_index < weeks:
            per_week[week_index] = per_week.get(week_index, 0) + (duration or 0)

    labels = [f"W-{weeks - 1 - i}" if i < weeks - 1 else "Now" for i in range(weeks)]
    minutes = [per_week.get(weeks - 1 - i, 0) for i in range(weeks)]
    return labels, minutes


def _build_heatmap_levels(user: User, local_now: datetime, days: int = 84) -> list[int]:
    today = effective_date(local_now)
    start = datetime.combine(today - timedelta(days=days - 1), time.min)
    weight_dates = {
        effective_date(row[0])
        for row in db.session.execute(
            select(WeightLog.date).where(WeightLog.user_id == user.id, WeightLog.date >= start)
        ).all()
        if row[0] is not None
    }
    meal_dates = {
        effective_date(row[0])
        for row in db.session.execute(
            select(MealEntry.logged_at).where(MealEntry.user_id == user.id, MealEntry.logged_at >= start)
        ).all()
        if row[0] is not None
    }

    levels: list[int] = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
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


def _logging_streak(logs: list[WeightLog], user: User) -> tuple[int, list[bool]]:
    dates = {effective_date(log.date) for log in logs if log.date is not None}
    today = effective_today(user)
    if not dates:
        return 0, [False] * 7

    day = today if today in dates else today - timedelta(days=1)
    streak = 0
    while day in dates:
        streak += 1
        day -= timedelta(days=1)

    week = [(today - timedelta(days=offset)) in dates for offset in range(6, -1, -1)]
    return streak, week


def _get_upcoming_workouts(user: User, now: datetime) -> list[ScheduledWorkout]:
    # A session stays listed for a day past its slot. Without the grace window
    # a not-yet-completed session vanishes the minute its start time passes —
    # it leaves Upcoming (scheduled_for >= now) but Recent Sessions only shows
    # completed rows — so there is no way left to mark it done.
    return list(
        db.session.execute(
            select(ScheduledWorkout)
            .where(
                ScheduledWorkout.user_id == user.id,
                ScheduledWorkout.status == "scheduled",
                ScheduledWorkout.scheduled_for >= now - timedelta(hours=24),
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
            ScheduledWorkout.status == "scheduled",
            ScheduledWorkout.scheduled_for >= week_start,
            ScheduledWorkout.scheduled_for < week_end,
        )
    ).scalars().all()
    by_date: dict = {}
    for workout in scheduled:
        by_date.setdefault(workout.scheduled_for.date(), []).append(workout)

    # Days with a completed session show as "done" on the strip — today keeps
    # its live pulse while something is still queued.
    completed = db.session.execute(
        select(ScheduledWorkout.scheduled_for).where(
            ScheduledWorkout.user_id == current_user.id,
            ScheduledWorkout.status == "completed",
            ScheduledWorkout.scheduled_for >= week_start,
            ScheduledWorkout.scheduled_for < week_end,
        )
    ).scalars().all()
    done_dates = {d.date() for d in completed}

    strip = []
    for offset in range(7):
        day = (week_start + timedelta(days=offset)).date()
        day_workouts = by_date.get(day, [])
        is_today = offset == 0
        if is_today and day_workouts:
            state = "today"
        elif day in done_dates:
            state = "done"
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
    # Label by fiT-X day (04:30 shift) so a 1 AM weigh-in shows yesterday,
    # matching the streak and heatmap.
    labels = [effective_date(log.date).strftime("%d %b") if log.date else "" for log in logs[-WEIGHT_SERIES_LIMIT:]]
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


def _build_challenges_view(user: User, local_now: datetime) -> dict:
    """Everything the Challenges tab on the dashboard shows: open challenges,
    the user's participations with computed progress, and earned badges."""
    today = local_now.date()
    participations = db.session.scalars(
        select(UserChallenge)
        .where(UserChallenge.user_id == user.id)
        .order_by(UserChallenge.joined_at.desc())
    ).all()
    joined_ids = {uc.challenge_id for uc in participations if uc.status == "active"}
    available = [
        c for c in db.session.scalars(
            select(Challenge).where(Challenge.status == "active").order_by(Challenge.title)
        ).all()
        if c.id not in joined_ids
        and _joinable(c, today)
        and challenge_visible_to(c, user)
    ]
    joined = [(uc, progress_view(uc)) for uc in participations if uc.status == "active"]
    finished = [(uc, progress_view(uc)) for uc in participations if uc.status in {"completed", "failed"}]
    badges = db.session.scalars(
        select(UserBadge).where(UserBadge.user_id == user.id).order_by(UserBadge.awarded_at.desc())
    ).all()
    return {"available": available, "joined": joined, "finished": finished, "badges": badges}


def _build_dashboard_context() -> dict:
    # Every stored datetime is the user's wall clock; `now` is the same clock.
    # Never pass datetime.now() into the time helpers — they expect UTC.
    now = user_now(current_user)
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
            pace=current_user.pace or 2,
        )

        if current_user.start_weight is not None and current_user.target_weight is not None:
            goal_progress = _calculate_goal_progress(
                start_weight=current_user.start_weight,
                current_weight=current_user.weight,
                target_weight=current_user.target_weight,
            )

    target_kcal = calorie_target["target"]

    # --- Food / hydration (today, user's local day) ---
    today_start, today_end = day_bounds(now)
    today_meals = _get_meal_entries_between(current_user, today_start, today_end)
    meals_by_type: dict[str, list[MealEntry]] = {meal_type: [] for meal_type in MEAL_TYPE_ORDER}
    for entry in today_meals:
        meals_by_type.setdefault(entry.meal_type, []).append(entry)
    meal_type_totals = {
        meal_type: sum(e.calories for e in entries) for meal_type, entries in meals_by_type.items()
    }
    today_totals = _sum_meal_entries(today_meals)

    intake_pct = min(100, round(today_totals["calories"] / target_kcal * 100)) if target_kcal else 0
    intake_remaining = max(0, target_kcal - today_totals["calories"])

    macro_targets = _build_macro_targets(current_user.weight, target_kcal, current_user.goal)
    if (
        current_user.macro_protein_override is not None
        and current_user.macro_carbs_override is not None
        and current_user.macro_fats_override is not None
    ):
        macro_targets = {
            "protein": current_user.macro_protein_override,
            "carbs": current_user.macro_carbs_override,
            "fats": current_user.macro_fats_override,
        }

    week_net_labels, week_net_values = _build_week_net(current_user, now, target_kcal)

    water_goal_l = round((current_user.weight or 85) * 0.035, 1)
    water_today_l = _get_water_today_liters(current_user, now)
    water_pct = min(100, round(water_today_l / water_goal_l * 100)) if water_goal_l else 0
    water_segments = min(12, round(water_pct / 100 * 12))

    # --- Weight ---
    weight_data = _build_weight_data()
    streak, streak_week = _logging_streak(list(current_user.logs), current_user)

    # --- Workouts ---
    upcoming_workouts = _get_upcoming_workouts(current_user, now)
    next_workout = upcoming_workouts[0] if upcoming_workouts else None
    recent_completed = _get_recent_completed(now)
    week_activity = _build_week_activity(current_user, now)
    weeks_history_labels, weeks_history_minutes = _build_weeks_minutes_history(current_user, now)
    # Session plans come from the same builder as the reminder email, so the
    # tab and the email can never disagree. Environment falls back per-session
    # to the profile default; unknown environments still get the generic plan.
    upcoming_plans = {
        workout.id: build_plan(
            workout.title,
            workout.environment or current_user.training_environment,
            generic_fallback=True,
        )
        for workout in upcoming_workouts
    }

    weekly_workout_count = db.session.execute(
        select(func.count(ScheduledWorkout.id)).where(
            ScheduledWorkout.user_id == current_user.id,
            ScheduledWorkout.status == "scheduled",
            ScheduledWorkout.scheduled_for >= now,
            ScheduledWorkout.scheduled_for < now + timedelta(days=7),
        )
    ).scalar_one()

    _prune_old_chat_messages(current_user)
    db.session.commit()
    chat_messages = list(
        reversed(
            db.session.execute(
                select(CoachMessage)
                .where(
                    CoachMessage.user_id == current_user.id,
                    CoachMessage.created_at >= _chat_day_start(current_user),
                    CoachMessage.role != "error",  # telemetry rows stay out of the chat feed
                )
                .order_by(CoachMessage.id.desc())
                .limit(COACH_CHAT_HISTORY_LIMIT)
            ).scalars().all()
        )
    )

    first_log = current_user.logs[0] if current_user.logs else None
    day_count = (effective_date(now) - effective_date(first_log.date)).days + 1 if first_log and first_log.date else 1

    phase_label = GOAL_PHASE_LABELS.get(current_user.goal or "", "Set your goal")

    return {
        "user": current_user,
        "user_initials": _user_initials(current_user.name),
        "user_picture": (
            current_user.google_identity.picture_url
            if current_user.google_identity and current_user.google_identity.picture_url
            else None
        ),
        "phase_label": phase_label,
        "day_count": day_count,
        "challenges": _build_challenges_view(current_user, now),
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
        "upcoming_plans": upcoming_plans,
        "needs_training_environment": current_user.training_environment is None,
        "training_environment": current_user.training_environment,
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
        "heatmap_levels": _build_heatmap_levels(current_user, now),
        "heatmap_colors": HEATMAP_COLORS,
        # Coach
        "chat_messages": chat_messages,
        "coach_examples": COACH_QUICK_STARTS,
        "coach_mode_label": "fiT-X AI",
        # Push notifications (bell dropdown); empty key = feature disabled
        "push_public_key": current_app.config.get("VAPID_PUBLIC_KEY", ""),
        "push_configured": bool(current_app.config.get("VAPID_PUBLIC_KEY")),
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


def _claim_idempotency_key(user: User, form_key: str | None) -> str:
    """Reserve a submitted form token in the caller's transaction.

    Returns "claimed" when this request may proceed, "duplicate" when the same
    token was already used (a genuine double submit), and "missing" when the
    form carried no usable token at all — e.g. an open tab rendered before the
    field existed. A unique database constraint, rather than an in-memory
    cache, makes this work across Render's two gunicorn workers and survives a
    process restart.
    """
    key = (form_key or "").strip()
    if not key or len(key) > 64:
        return "missing"
    try:
        with db.session.begin_nested():
            db.session.add(IdempotencyKey(user_id=user.id, key=key))
            db.session.flush()
        return "claimed"
    except IntegrityError:
        return "duplicate"


def _idempotency_redirect(status: str, anchor: str, duplicate_message: str):
    """Turn an idempotency status into a redirect response, or None to proceed."""
    if status == "claimed":
        return None
    if status == "missing":
        # An old open tab submitted a form rendered before the hidden key
        # existed. That is not a duplicate log — say so instead of "already
        # processed", and make the user reload so the new form ships the key.
        flash("This page was open in an old tab. Please refresh the page and try again.", "warning")
    else:
        flash(duplicate_message, "warning")
    return redirect(url_for("dashboard.dashboard", _anchor=anchor))


@dashboard_bp.route("/coach/message", methods=["POST"])
@login_required
@limiter.limit("10 per minute; 100 per day", key_func=lambda: str(current_user.id)) if limiter is not None else (lambda view: view)
def coach_message():
    raw_message = request.form.get("message", "")
    resp = _idempotency_redirect(
        _claim_idempotency_key(current_user, request.form.get("idempotency_key")),
        "coach", "That request was already processed; nothing was added twice.")
    if resp:
        return resp
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
    resp = _idempotency_redirect(
        _claim_idempotency_key(current_user, request.form.get("idempotency_key")),
        "nutrition", "That meal was already logged.")
    if resp:
        return resp
    name = " ".join((request.form.get("name") or "").split())[:60]
    meal_type = request.form.get("meal_type", "meal")
    if meal_type not in MEAL_TYPE_ORDER:
        meal_type = _default_meal_type(user_now(current_user))

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
    challenge_notes = update_challenges(current_user, {"kind": "meal", "at": user_now(current_user)})
    db.session.commit()
    flash(f"Logged {name} — {calories} kcal." + (" " + " · ".join(challenge_notes) + "." if challenge_notes else ""), "success")
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
    resp = _idempotency_redirect(
        _claim_idempotency_key(current_user, request.form.get("idempotency_key")),
        "nutrition", "That water entry was already logged.")
    if resp:
        return resp
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
    # Double-submit guard: a second click must not double-count challenges.
    if workout.status == "completed":
        flash("That workout was already marked as done.", "error")
        return redirect(url_for("dashboard.dashboard", _anchor="workouts"))

    workout.status = "completed"
    if workout.scheduled_for > user_now(workout.user) + timedelta(minutes=5):
        workout.scheduled_for = user_now(workout.user)

    challenge_notes = update_challenges(current_user, {
        "kind": "workout",
        "at": workout.scheduled_for,
        "title": workout.title,
        "calories_burned": workout.calories_burned,
    })
    db.session.commit()
    flash(
        f"{workout.title} marked as done — nice work."
        + (" " + " · ".join(challenge_notes) + "." if challenge_notes else ""),
        "success",
    )
    return redirect(url_for("dashboard.dashboard", _anchor="workouts"))


REMINDER_WINDOW_MINUTES = 45


@dashboard_bp.route("/profile/timezone", methods=["POST"])
@login_required
def profile_timezone():
    # Called once per dashboard load; stores the browser's UTC offset so the
    # reminder cron can convert to the visitor's local clock.
    data = request.get_json(silent=True) or {}
    if "offset" in data:
        current_user.tz_offset_minutes = _clamp_int(data.get("offset"), -840, 840)
        db.session.commit()
    return {"ok": True, "tz_offset_minutes": current_user.tz_offset_minutes}


@dashboard_bp.route("/settings/reminders", methods=["POST"])
@login_required
def settings_reminders():
    current_user.email_reminders_enabled = request.form.get("enabled") == "true"
    db.session.commit()
    return {"enabled": current_user.email_reminders_enabled}


@dashboard_bp.route("/settings/nudges", methods=["POST"])
@login_required
def settings_nudges():
    current_user.nudges_enabled = request.form.get("enabled") == "true"
    db.session.commit()
    return {"enabled": current_user.nudges_enabled}


@dashboard_bp.route("/settings/profile", methods=["POST"])
@login_required
def settings_profile():
    """Settings -> Biometrics / Goals save. Each tab posts only its own fields;
    everything absent stays untouched. A changed weight is a check-in, so it
    also lands a WeightLog like a coach log would."""
    cleaned, errors = validate_profile_form(request.form)
    if errors:
        return {"ok": False, "errors": errors}, 400

    user = current_user
    if cleaned["training_environment"]:
        user.training_environment = cleaned["training_environment"]
    for field in ("height", "age", "activity_level", "goal", "pace", "target_weight"):
        value = cleaned.get(field)
        if value is not None:
            setattr(user, field, value)

    if cleaned["weight"] is not None and cleaned["weight"] != user.weight:
        user.weight = cleaned["weight"]
        db.session.add(WeightLog(weight=cleaned["weight"], user=user))

    overrides = cleaned["macro_overrides"]
    if overrides is None:
        user.macro_protein_override = None
        user.macro_carbs_override = None
        user.macro_fats_override = None
    elif all(v is not None for v in overrides.values()):
        user.macro_protein_override = overrides["protein"]
        user.macro_carbs_override = overrides["carbs"]
        user.macro_fats_override = overrides["fats"]
    else:
        return {"ok": False, "errors": ["Set all three macros (protein, carbs, fats) to save the override."]}, 400

    db.session.commit()
    return {"ok": True}


MAX_PUSH_SUBSCRIPTIONS_PER_USER = 5


@dashboard_bp.route("/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    # Stores a browser push subscription (one row per device). Upserts on the
    # endpoint — a device re-subscribing replaces its row — and caps rows per
    # user so abandoned devices eventually age out.
    if not current_app.config.get("VAPID_PUBLIC_KEY"):
        return {"ok": False, "error": "push not configured"}, 503
    data = request.get_json(silent=True) or {}
    endpoint = str(data.get("endpoint") or "")[:500]
    keys = data.get("keys") or {}
    p256dh = str(keys.get("p256dh") or "")
    auth = str(keys.get("auth") or "")
    if not (endpoint.startswith("https://") and p256dh and auth):
        return {"ok": False, "error": "invalid subscription"}, 400

    sub = db.session.execute(
        select(PushSubscription).where(PushSubscription.endpoint == endpoint)
    ).scalar_one_or_none()
    if sub is None:
        existing = db.session.scalars(
            select(PushSubscription)
            .where(PushSubscription.user_id == current_user.id)
            .order_by(PushSubscription.created_at.asc())
        ).all()
        while len(existing) >= MAX_PUSH_SUBSCRIPTIONS_PER_USER:
            db.session.delete(existing.pop(0))
        sub = PushSubscription(user=current_user)
        db.session.add(sub)
    sub.endpoint = endpoint
    sub.p256dh = p256dh
    sub.auth = auth
    sub.user_agent = (request.user_agent.string or "")[:200]
    db.session.commit()
    return {"ok": True}


@dashboard_bp.route("/push/unsubscribe", methods=["POST"])
@login_required
def push_unsubscribe():
    data = request.get_json(silent=True) or {}
    endpoint = str(data.get("endpoint") or "")
    if endpoint:
        db.session.execute(
            delete(PushSubscription).where(
                PushSubscription.user_id == current_user.id,
                PushSubscription.endpoint == endpoint,
            )
        )
        db.session.commit()
    return {"ok": True}


# Daily housekeeping gate for old idempotency keys. Module-level, therefore
# per-worker: the two Gunicorn workers may each run the delete once a day,
# which is harmless — the second pass finds nothing to delete.


@dashboard_bp.route("/cron/send-reminders", methods=["GET"])
def cron_send_reminders():
    # GET on purpose: the app-wide CSRF check only guards mutating methods, and
    # the cron caller has no session — the secret header is the auth.
    secret = current_app.config.get("CRON_SECRET", "")
    provided = request.headers.get("X-Cron-Secret", "")
    if not secret or not compare_digest(provided, secret):
        return {"error": "unauthorized"}, 403
    result = _send_due_workout_reminders()
    result["challenge_reminders"] = send_due_challenge_reminders()
    result["engagement_nudges"] = send_due_engagement_nudges()
    deleted = cron_module.cleanup_old_idempotency_keys()
    if deleted is not None:
        result["idempotency_keys_deleted"] = deleted
    return result


@dashboard_bp.route("/cron/daily-challenges", methods=["GET"])
def cron_daily_challenges():
    """Daily maintenance: activate scheduled challenges, end expired ones,
    reset broken streaks, fail expired rolling windows."""
    secret = current_app.config.get("CRON_SECRET", "")
    provided = request.headers.get("X-Cron-Secret", "")
    if not secret or not compare_digest(provided, secret):
        return {"error": "unauthorized"}, 403
    return run_daily_maintenance()


@dashboard_bp.route("/cron/send-nudges-now", methods=["GET"])
def cron_send_nudges_now():
    """Manual catch-up for today's engagement nudge outside the 3-4 PM window.
    Same eligibility as the scheduled run; the once-per-day stamp keeps it
    idempotent, and the normal cron window resumes tomorrow."""
    secret = current_app.config.get("CRON_SECRET", "")
    provided = request.headers.get("X-Cron-Secret", "")
    if not secret or not compare_digest(provided, secret):
        return {"error": "unauthorized"}, 403
    return send_due_engagement_nudges(ignore_hour=True)
