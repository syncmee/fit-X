"""Challenges domain: vocabulary, type semantics, and seed templates.

Type semantics (the contract Step 7's update_challenges implements):

- streak      — consecutive days, each meeting the daily qualification (a log
                matching the metric, at or above daily_target when set).
                target_value = number of consecutive days to reach. A missed
                day resets the streak to 0 (longest_streak keeps the record).
- cumulative  — progress_value sums the metric across every qualifying log
                while active (workout_count sums 1 per completed workout,
                distance_km sums km, calories sums burn, meals_logged /
                weight_logged count entries). target_value = total.
- single_goal — one single log entry meets or exceeds target_value
                (e.g. a run of 5 km or more).
- count       — N distinct days meeting the daily qualification within the
                challenge window (duration_days if set). target_value =
                number of days.

Metrics map to log sources: workout_count / distance_km / calories come from
completed ScheduledWorkouts (distance parsed from titles like "5k run" in
Step 7 — the workout model has no distance column), meals_logged from
MealEntry, weight_logged from WeightLog, steps from future device syncs
(no source exists yet — the 10K Steps template is seeded ready).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from flask import Blueprint, flash, redirect, render_template, url_for
from flask_login import current_user, login_required
from sqlalchemy import select

from .extensions import db
from .models import Badge, Challenge, ChallengeDayLog, User, UserBadge, UserChallenge
from .timeutil import effective_date, effective_today, user_now

challenges_bp = Blueprint("challenges", __name__, url_prefix="/challenges")

CHALLENGE_TYPES = ("streak", "cumulative", "single_goal", "count")
CHALLENGE_METRICS = ("workout_count", "distance_km", "calories", "meals_logged", "weight_logged", "steps")
CHALLENGE_STATUSES = ("draft", "scheduled", "active", "ended")

TYPE_LABELS = {
    "streak": "Streak — consecutive days",
    "cumulative": "Cumulative — total",
    "single_goal": "Single goal — one log",
    "count": "Count — N days in the window",
}

METRIC_LABELS = {
    "workout_count": "Workouts completed",
    "distance_km": "Distance (km)",
    "calories": "Calories burned",
    "meals_logged": "Meals logged",
    "weight_logged": "Weight check-ins",
    "steps": "Steps",
}

METRIC_UNITS = {
    "workout_count": "workouts",
    "distance_km": "km",
    "calories": "kcal",
    "meals_logged": "meals",
    "weight_logged": "check-ins",
    "steps": "steps",
}

# Shared audience segments (challenges + the announcement tool). "all" is the
# whole active user base; the rest are derived from account data.
SEGMENTS = {
    "all": "All active users",
    "inactive_7d": "Inactive 7+ days",
    "new_users": "New users (7 days)",
    "challenge_participants": "Challenge participants",
}


def user_in_segment(user: User, segment: str) -> bool:
    """Does this user belong to the named audience segment?"""
    if segment == "all":
        return user.status == "active"
    week_ago = datetime.utcnow() - timedelta(days=7)
    if segment == "inactive_7d":
        return user.status == "active" and (
            user.last_active_at is None or user.last_active_at < week_ago
        )
    if segment == "new_users":
        return user.status == "active" and user.created_at is not None and user.created_at >= week_ago
    if segment == "challenge_participants":
        return user.status == "active" and db.session.scalar(
            select(UserChallenge.id).where(
                UserChallenge.user_id == user.id, UserChallenge.status == "active",
            )
        ) is not None
    return False


def users_in_segment(segment: str) -> list[User]:
    """Every active user in the segment (for announcements and previews)."""
    return [u for u in db.session.scalars(
        select(User).where(User.status == "active").order_by(User.id)
    ).all() if user_in_segment(u, segment)]


def challenge_visible_to(challenge: Challenge, user: User) -> bool:
    """Audience gate for the user-facing page and the coach: 'all' is open to
    everyone; a segment challenge only shows to members of its segment."""
    if challenge.audience == "all":
        return True
    return bool(challenge.audience_segment) and user_in_segment(user, challenge.audience_segment)

# Starter templates seeded as status="draft"; the admin (Step 5) copies one
# into the create form, leaving the template in place. Each links a badge
# that Step 7 awards on completion.
_CHALLENGE_TEMPLATES = (
    {
        "title": "30-Day Streak",
        "description": "Log a workout every day for 30 days in a row. Miss a day and the streak resets.",
        "type": "streak",
        "metric": "workout_count",
        "target_value": 30,
        "daily_target": None,
        "duration_days": None,
        "rolling": True,
        "badge": {"name": "30-Day Streak", "description": "Logged workouts 30 days in a row.", "icon": "🔥"},
    },
    {
        "title": "Run 5K",
        "description": "Complete a single run of 5 km or more.",
        "type": "single_goal",
        "metric": "distance_km",
        "target_value": 5.0,
        "daily_target": None,
        "duration_days": None,
        "rolling": True,
        "badge": {"name": "5K Runner", "description": "Ran 5 km in a single session.", "icon": "🏃"},
    },
    {
        "title": "100 Workouts",
        "description": "Log 100 completed workouts. Every session counts.",
        "type": "cumulative",
        "metric": "workout_count",
        "target_value": 100,
        "daily_target": None,
        "duration_days": None,
        "rolling": True,
        "badge": {"name": "Century Club", "description": "Completed 100 workouts.", "icon": "💯"},
    },
    {
        "title": "10K Steps × 7 Days",
        "description": "Hit 10,000 steps on 7 days in a row. (Steps sync arrives with device integrations — template ready.)",
        "type": "streak",
        "metric": "steps",
        "target_value": 7,
        "daily_target": 10000,
        "duration_days": None,
        "rolling": True,
        "badge": {"name": "Step Master", "description": "Hit 10,000 steps 7 days in a row.", "icon": "👟"},
    },
)


def seed_challenge_templates() -> int:
    """Create the starter challenge templates and their badges. Idempotent —
    matches on challenge title (badges ride along with their challenge)."""
    added = 0
    for t in _CHALLENGE_TEMPLATES:
        if db.session.scalar(select(Challenge.id).where(Challenge.title == t["title"])):
            continue
        badge = db.session.scalar(select(Badge).where(Badge.name == t["badge"]["name"]))
        if badge is None:
            badge = Badge(
                name=t["badge"]["name"],
                description=t["badge"]["description"],
                icon=t["badge"]["icon"],
            )
            db.session.add(badge)
            db.session.flush()
        db.session.add(Challenge(
            title=t["title"],
            description=t["description"],
            type=t["type"],
            metric=t["metric"],
            target_value=t["target_value"],
            daily_target=t.get("daily_target"),
            duration_days=t.get("duration_days"),
            rolling=t.get("rolling", False),
            status="draft",
            badge_id=badge.id,
        ))
        added += 1
    db.session.commit()
    return added


# ============================================================
# User-facing challenges (Step 6)
# ============================================================

@challenges_bp.before_request
def require_completed_onboarding():
    """Same gate as the dashboard — challenge pages are part of the app."""
    from flask import redirect, url_for

    if current_user.is_authenticated and not current_user.onboarding:
        return redirect(url_for("main.onboarding"))
    return None


def _day_qualifies(value: float, daily_target: float | None) -> bool:
    """A logged day counts toward streak/count challenges when it clears the
    per-day threshold (any log counts when none is set)."""
    return value >= daily_target if daily_target else value > 0


def progress_view(uc: UserChallenge) -> dict:
    """What the challenges page shows for one participation: current value,
    goal, percent, and the unit label — computed per challenge type."""
    c = uc.challenge
    unit = METRIC_UNITS.get(c.metric, c.metric)
    goal = c.target_value or 1
    if c.type == "streak":
        current = float(uc.current_streak)
        caption = f"{uc.current_streak} day streak · best {uc.longest_streak}"
    elif c.type == "count":
        current = float(sum(1 for d in uc.day_logs if _day_qualifies(d.value, c.daily_target)))
        caption = f"{int(current)} qualifying days"
    else:  # cumulative + single_goal both read progress_value
        current = float(uc.progress_value or 0)
        caption = "best single log" if c.type == "single_goal" else "total"
    return {
        "current": current,
        "goal": goal,
        "pct": min(int(current / goal * 100), 100) if goal else 0,
        "unit": unit,
        "caption": caption,
        "done": uc.status == "completed" or current >= goal,
    }


def _joinable(challenge: Challenge, today) -> bool:
    """A challenge accepts joins while active; fixed-window challenges also
    require the window to still be open."""
    if challenge.status != "active":
        return False
    if not challenge.rolling and challenge.end_date is not None and challenge.end_date < today:
        return False
    return True


@challenges_bp.route("/")
@login_required
def index():
    today = user_now(current_user).date()

    participations = db.session.scalars(
        select(UserChallenge)
        .where(UserChallenge.user_id == current_user.id)
        .order_by(UserChallenge.joined_at.desc())
    ).all()
    joined_ids = {uc.challenge_id for uc in participations if uc.status == "active"}

    available = [
        c for c in db.session.scalars(
            select(Challenge).where(Challenge.status == "active").order_by(Challenge.title)
        ).all()
        if c.id not in joined_ids
        and _joinable(c, today)
        and challenge_visible_to(c, current_user)
    ]
    joined = [uc for uc in participations if uc.status == "active"]
    finished = [uc for uc in participations if uc.status in {"completed", "failed"}]
    badges = db.session.scalars(
        select(UserBadge).where(UserBadge.user_id == current_user.id).order_by(UserBadge.awarded_at.desc())
    ).all()

    return render_template(
        "challenges.html",
        available=available,
        joined=[(uc, progress_view(uc)) for uc in joined],
        finished=[(uc, progress_view(uc)) for uc in finished],
        badges=badges,
    )


@challenges_bp.route("/<int:challenge_id>/join", methods=["POST"])
@login_required
def join(challenge_id: int):
    challenge = db.session.get(Challenge, challenge_id)
    today = user_now(current_user).date()
    if challenge is None or not _joinable(challenge, today) or not challenge_visible_to(challenge, current_user):
        flash("That challenge isn't open for joining.", "error")
        return redirect(url_for("challenges.index"))
    existing = db.session.scalar(
        select(UserChallenge.id).where(
            UserChallenge.user_id == current_user.id,
            UserChallenge.challenge_id == challenge.id,
        )
    )
    if existing:
        flash("You already joined this challenge.", "error")
        return redirect(url_for("challenges.index"))

    db.session.add(UserChallenge(user_id=current_user.id, challenge_id=challenge.id))
    db.session.commit()
    flash(f"Joined {challenge.title} — every qualifying log now counts.", "success")
    return redirect(url_for("challenges.index"))


@challenges_bp.route("/<int:challenge_id>/leave", methods=["POST"])
@login_required
def leave(challenge_id: int):
    uc = db.session.scalar(
        select(UserChallenge).where(
            UserChallenge.user_id == current_user.id,
            UserChallenge.challenge_id == challenge_id,
            UserChallenge.status == "active",
        )
    )
    if uc is None:
        flash("You aren't in that challenge.", "error")
        return redirect(url_for("challenges.index"))
    title = uc.challenge.title
    db.session.delete(uc)  # cascades to the participation's day logs
    db.session.commit()
    flash(f"Left {title} — join again anytime to restart.", "success")
    return redirect(url_for("challenges.index"))


# ============================================================
# Progress tracking (Step 7)
# ============================================================

# "5k run", "10K", "5.5 km", "21kilometers" — distance lives in the title
# because ScheduledWorkout has no distance column.
_DISTANCE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*k(?:m|ms|ilomet\w*)?\b", re.IGNORECASE)


def workout_distance_km(title: str) -> float | None:
    m = _DISTANCE_RE.search(title or "")
    return float(m.group(1).replace(",", ".")) if m else None


def _event_contribution(event: dict, metric: str) -> float:
    """How much one log event adds to a day's value for the given metric.
    Zero means the event doesn't feed that metric at all."""
    if metric == "steps":
        return 0.0  # no source yet; device syncs will feed this later
    if event["kind"] == "workout":
        if metric == "workout_count":
            return 1.0
        if metric == "distance_km":
            return workout_distance_km(event.get("title") or "") or 0.0
        if metric == "calories":
            return float(event.get("calories_burned") or 0)
    elif event["kind"] == "meal" and metric == "meals_logged":
        return 1.0
    elif event["kind"] == "weight" and metric == "weight_logged":
        return 1.0
    return 0.0


def update_challenges(user, event: dict) -> list[str]:
    """Fold one freshly-created log event into every active challenge the
    user has joined. Called from each log-creation site (routes and both
    coach paths) BEFORE the surrounding commit; this function only flushes.

    event = {"kind": "workout" | "meal" | "weight",
             "at": <user wall clock of the activity>,
             "title": str, "calories_burned": int|None}   # workouts only

    Idempotent by construction: one day-log row per participation per day
    (unique constraint, upserted), and streaks/progress are recomputed from
    the stored day logs instead of incremented blindly. Returns short
    completion notes for reply/flash text.
    """
    event_date = effective_date(event["at"])
    notes: list[str] = []

    participations = db.session.scalars(
        select(UserChallenge).where(
            UserChallenge.user_id == user.id,
            UserChallenge.status == "active",
        )
    ).all()

    for uc in participations:
        c = uc.challenge
        if c is None or c.status != "active":
            continue

        # Window check: rolling participations start at their join; fixed
        # challenges run between their dates.
        if c.rolling:
            if uc.joined_at is not None and event_date < effective_date(uc.joined_at):
                continue
            if c.duration_days and uc.joined_at is not None:
                window_end = effective_date(uc.joined_at) + timedelta(days=c.duration_days)
                if event_date >= window_end:
                    continue
        else:
            if c.start_date is not None and event_date < c.start_date:
                continue
            if c.end_date is not None and event_date > c.end_date:
                continue

        contribution = _event_contribution(event, c.metric)
        if contribution <= 0:
            continue

        # Upsert the participation's day log (unique per day).
        day = db.session.scalar(
            select(ChallengeDayLog).where(
                ChallengeDayLog.user_challenge_id == uc.id,
                ChallengeDayLog.date == event_date,
            )
        )
        if day is None:
            db.session.add(ChallengeDayLog(
                user_challenge_id=uc.id, date=event_date, value=contribution,
            ))
        else:
            day.value += contribution
        db.session.flush()

        logs = db.session.scalars(
            select(ChallengeDayLog).where(ChallengeDayLog.user_challenge_id == uc.id)
        ).all()

        if c.type == "streak":
            qualifying = {d.date for d in logs if _day_qualifies(d.value, c.daily_target)}
            today = effective_today(user)
            streak = 0
            cursor = today if today in qualifying else today - timedelta(days=1)
            while cursor in qualifying:
                streak += 1
                cursor -= timedelta(days=1)
            uc.current_streak = streak
            uc.longest_streak = max(uc.longest_streak or 0, streak)
            uc.progress_value = float(streak)
            reached = streak >= c.target_value
        elif c.type == "count":
            qualifying_days = sum(1 for d in logs if _day_qualifies(d.value, c.daily_target))
            uc.progress_value = float(qualifying_days)
            reached = qualifying_days >= c.target_value
        elif c.type == "cumulative":
            uc.progress_value = sum(d.value for d in logs)
            reached = uc.progress_value >= c.target_value
        else:  # single_goal: best single event wins
            uc.progress_value = max(uc.progress_value or 0, contribution)
            reached = uc.progress_value >= c.target_value

        if reached:
            uc.status = "completed"
            uc.completed_at = user_now(user)
            note = f"Completed {c.title}"
            if c.badge and not db.session.scalar(
                select(UserBadge.id).where(
                    UserBadge.user_id == user.id,
                    UserBadge.badge_id == c.badge_id,
                )
            ):
                db.session.add(UserBadge(
                    user_id=user.id, badge_id=c.badge_id, challenge_id=c.id,
                ))
                note += f" — {c.badge.icon} {c.badge.name} badge earned"
            notes.append(note)
        db.session.flush()

    return notes


# ============================================================
# Scheduled maintenance (Step 8) — runs once a day via cron
# ============================================================

def run_daily_maintenance(now: datetime | None = None) -> dict:
    """Daily challenge upkeep, called by /cron/daily-challenges:

    - activate scheduled challenges whose start_date has arrived
    - end expired fixed-window challenges (and fail their open participations)
    - reset broken streaks (missed a day -> current_streak back to 0;
      longest_streak keeps the record; the challenge itself stays open)
    - fail rolling+duration participations whose window ran out

    Challenge windows are site-global dates, so they compare against the UTC
    date; per-user streak checks use each user's fiT-X day (4:30 AM local).
    """
    now = now or datetime.utcnow()
    today = now.date()
    stats = {"activated": 0, "ended": 0, "streaks_reset": 0, "windows_failed": 0}

    for c in db.session.scalars(select(Challenge).where(Challenge.status == "scheduled")):
        if c.start_date is None or c.start_date <= today:
            c.status = "active"
            stats["activated"] += 1

    for c in db.session.scalars(select(Challenge).where(Challenge.status == "active")):
        if not c.rolling and c.end_date is not None and c.end_date < today:
            c.status = "ended"
            stats["ended"] += 1
            for uc in db.session.scalars(
                select(UserChallenge).where(
                    UserChallenge.challenge_id == c.id, UserChallenge.status == "active",
                )
            ):
                uc.status = "failed"

    for uc in db.session.scalars(
        select(UserChallenge).where(UserChallenge.status == "active")
    ):
        c = uc.challenge
        user = uc.user
        if c is None or user is None or c.status != "active":
            continue

        if c.type == "streak":
            qualifying = {
                d.date for d in uc.day_logs
                if _day_qualifies(d.value, c.daily_target)
            }
            fitx_today = effective_today(user)
            if fitx_today not in qualifying and (fitx_today - timedelta(days=1)) not in qualifying:
                if uc.current_streak:
                    uc.current_streak = 0
                    stats["streaks_reset"] += 1

        # Rolling windows with a duration expire per-user, off their join date.
        if c.rolling and c.duration_days and uc.joined_at is not None:
            window_end = effective_date(uc.joined_at) + timedelta(days=c.duration_days)
            if effective_today(user) >= window_end:
                uc.status = "failed"
                stats["windows_failed"] += 1

    db.session.commit()
    return stats
