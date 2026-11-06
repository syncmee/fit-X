"""Cron jobs: workout reminders, challenge reminders, engagement nudges,
and idempotency-key housekeeping.

Driven by an external scheduler (GitHub Actions / Render Cron) via the
/cron/* routes in dashboard.py. Emails go through app.emails, pushes through
app.push — both referenced as modules (not from-imports) so tests can patch
the authoritative call targets.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import current_app, render_template, url_for
from sqlalchemy import delete, select

from app import emails as emails_module
from app import push as push_module
from app.challenges import _day_qualifies
from app.extensions import db
from app.models import (
    Challenge,
    MealEntry,
    IdempotencyKey,
    ScheduledWorkout,
    User,
    UserChallenge,
    WaterLog,
    WeightLog,
)
from app.plans import build_plan
from app.timeutil import APP_DAY_START, effective_date, effective_today, to_user_clock, user_now


_idempotency_cleanup_last_date: date | None = None



IDEMPOTENCY_KEY_RETENTION_DAYS = 7

# Per-worker daily gate: the two Gunicorn workers may each run the delete
# once a day, which is harmless — the second pass finds nothing to delete.
_idempotency_cleanup_last_date: date | None = None


def cleanup_old_idempotency_keys() -> int | None:
    """Delete accepted dedupe tokens older than the retention window, at most
    once per day. Tokens are never read after their request completes, so
    deletion cannot weaken double-submit protection."""
    global _idempotency_cleanup_last_date
    today = datetime.utcnow().date()
    if _idempotency_cleanup_last_date == today:
        return None
    _idempotency_cleanup_last_date = today
    cutoff = datetime.utcnow() - timedelta(days=IDEMPOTENCY_KEY_RETENTION_DAYS)
    deleted = db.session.execute(delete(IdempotencyKey).where(IdempotencyKey.created_at < cutoff))
    db.session.commit()
    return deleted.rowcount or 0


def cleanup_old_idempotency_keys() -> int | None:
    """Delete accepted dedupe tokens older than the retention window, at most
    once per day (per worker; a second worker's pass finds nothing)."""
    global _idempotency_cleanup_last_date
    today = datetime.utcnow().date()
    if _idempotency_cleanup_last_date == today:
        return None
    _idempotency_cleanup_last_date = today
    now = datetime.utcnow()
    """Delete accepted dedupe tokens older than the retention window.

    The table only grows when users submit meal/water/coach forms, but
    without a cleanup it grows forever. Tokens are never read after their
    request completes, so deletion cannot affect live double-submit
    protection (a 7-day window still covers every realistic retry)."""
    cutoff = now - timedelta(days=IDEMPOTENCY_KEY_RETENTION_DAYS)
    deleted = db.session.execute(delete(IdempotencyKey).where(IdempotencyKey.created_at < cutoff))
    db.session.commit()
    return deleted.rowcount or 0


# Rides the same 10-minute cron as workout reminders. Nudges go out in the
# user's local evening (18:00–21:59) once per participation per ~20h.
CHALLENGE_REMINDER_MIN_HOUR = 18
CHALLENGE_REMINDER_MAX_HOUR = 22
CHALLENGE_REMINDER_THROTTLE = timedelta(hours=20)


def send_challenge_reminders() -> dict:
    """Nudge users who joined a challenge but haven't logged a qualifying
    entry for it today (their fiT-X day)."""
    sent, failed = [], 0
    participations = db.session.scalars(
        select(UserChallenge)
        .join(User, UserChallenge.user_id == User.id)
        .where(
            UserChallenge.status == "active",
            Challenge.status == "active",
            User.status == "active",
        )
    ).all()

    for uc in participations:
        user = uc.user
        challenge = uc.challenge
        if user is None or challenge is None:
            continue
        local_now = user_now(user)
        if not CHALLENGE_REMINDER_MIN_HOUR <= local_now.hour < CHALLENGE_REMINDER_MAX_HOUR:
            continue
        if uc.last_reminded_at and local_now - uc.last_reminded_at < CHALLENGE_REMINDER_THROTTLE:
            continue
        today = effective_today(user)
        if any(
            _day_qualifies(d.value, challenge.daily_target)
            for d in uc.day_logs if d.date == today
        ):
            continue  # already logged for this challenge today

        delivered = []
        if user.push_subscriptions:
            if push_module.send_web_push(
                user,
                title=f"fiT-X · {challenge.title}",
                body="You haven't logged today — keep the streak alive!",
                url="/challenges",
                tag=f"challenge-{challenge.id}-{today.isoformat()}",
            ):
                delivered.append("push")
        if user.email_reminders_enabled:
            ok, _detail = _send_challenge_reminder_email(user, challenge)
            if ok:
                delivered.append("email")

        if delivered:
            uc.last_reminded_at = local_now
            sent.append({"user": user.email, "challenge": challenge.title, "via": delivered})
        else:
            failed += 1

    db.session.commit()
    return {"reminded": len(sent), "skipped": failed, "detail": sent}


def _send_challenge_reminder_email(user: User, challenge: Challenge) -> tuple[bool, str]:
    smtp_user = current_app.config.get("SMTP_USER", "")
    smtp_password = current_app.config.get("SMTP_APP_PASSWORD", "")
    if not smtp_user:
        return False, "smtp not configured"
    dashboard_url = url_for("dashboard.dashboard", _external=True)
    subject = f"fiT-X · {challenge.title} is waiting"
    plain = (
        f"Hi {user.name},\n\n"
        f"You joined {challenge.title} but haven't logged for it today. "
        f"Log it in the coach or on the dashboard to keep your progress:\n{dashboard_url}\n\n"
        "— fiT-X"
    )
    html = render_template(
        "emails/challenge_reminder.html",
        user_name=user.name,
        challenge_title=challenge.title,
        challenge_description=challenge.description,
        dashboard_url=dashboard_url,
    )
    return emails_module.send_via_smtp(user, subject, plain, html, smtp_user, smtp_password)


# Rides the same cron as workout/challenge reminders. A user who hasn't logged
# anything today gets one ping in their local 15:00-16:59 window: push if the
# device allows it, email only as the fallback — never both on the same day.
ENGAGEMENT_NUDGE_MIN_HOUR = 15
ENGAGEMENT_NUDGE_MAX_HOUR = 17
ENGAGEMENT_NUDGE_ACTIVE_DAYS = 90  # stop pinging accounts silent for a quarter — lapsed-but-winnable users still count

NUDGE_COPY_STREAK = [
    ("Your streak is waiting", "Day {streak} is on the board — one log today keeps it alive. Weight, a meal, anything counts."),
    ("Don't break the chain", "{streak} days in a row so far. It takes 10 seconds to keep it going."),
    ("The streak believes in you", "Day {streak} ends tonight. Log your weight or a meal and it rolls into tomorrow."),
]
NUDGE_COPY_FRESH = [
    ("Today's page is still blank", "Your 04:30 recalc uses every log — today's is empty. Start with a weigh-in, it takes 10 seconds."),
    ("Small log, big trend", "One weight check-in today keeps your trend line honest. That's it, that's the task."),
    ("The coach noticed", "Nothing logged today. Log a meal or your weight and the coach adjusts tonight's plan around it."),
]


def _fiT_x_day_bounds(user: User) -> tuple[datetime, datetime]:
    """[start, end) of the user's current fiT-X day (04:30 local reset)."""
    start = datetime.combine(effective_today(user), APP_DAY_START)
    return start, start + timedelta(days=1)


def _has_row_between(model, user: User, start: datetime, end: datetime, date_field: str, extra=None) -> bool:
    stmt = select(model.id).where(
        getattr(model, "user_id") == user.id,
        getattr(model, date_field) >= start,
        getattr(model, date_field) < end,
    )
    if extra is not None:
        stmt = stmt.where(extra)
    # scalar() yields False (not None) on an empty EXISTS — bool() it.
    return bool(db.session.scalar(select(stmt.exists())))


def _user_logged_today(user: User) -> bool:
    start, end = _fiT_x_day_bounds(user)
    return (
        _has_row_between(WeightLog, user, start, end, "date")
        or _has_row_between(MealEntry, user, start, end, "logged_at")
        or _has_row_between(WaterLog, user, start, end, "logged_at")
        or _has_row_between(
            ScheduledWorkout, user, start, end, "scheduled_for", ScheduledWorkout.status == "completed"
        )
    )


def _user_active_recently(user: User, days: int = ENGAGEMENT_NUDGE_ACTIVE_DAYS) -> bool:
    end = datetime.combine(effective_today(user), APP_DAY_START) + timedelta(days=1)
    start = end - timedelta(days=days)
    return (
        _has_row_between(WeightLog, user, start, end, "date")
        or _has_row_between(MealEntry, user, start, end, "logged_at")
        or _has_row_between(WaterLog, user, start, end, "logged_at")
        or _has_row_between(ScheduledWorkout, user, start, end, "created_at")
    )


def _nudge_copy(user: User, streak_days: int) -> tuple[str, str]:
    today_ordinal = effective_today(user).toordinal()
    pool = NUDGE_COPY_STREAK if streak_days >= 2 else NUDGE_COPY_FRESH
    headline, body = pool[(today_ordinal + user.id) % len(pool)]
    return headline, body.replace("{streak}", str(streak_days))


def send_engagement_nudges(ignore_hour: bool = False) -> dict:
    """One ping per day for active users who haven't logged anything today.
    ignore_hour waives only the 15:00-16:59 window (manual catch-up, e.g. the
    day the feature shipped); every other guard and the once-per-day stamp
    still apply, so a forced run can never double-send."""
    sent, failed = [], 0
    candidates = 0
    considered = 0
    skip_reasons = {
        "outside_window": 0, "opted_out": 0, "already_nudged": 0, "logged_today": 0,
        "inactive": 0, "imminent_workout": 0, "no_channel": 0,
    }
    users = db.session.scalars(
        select(User).where(User.status == "active", User.onboarding.is_(True))
    ).all()
    candidates = len(users)

    for user in users:
        local_now = user_now(user)
        if not ignore_hour and not ENGAGEMENT_NUDGE_MIN_HOUR <= local_now.hour < ENGAGEMENT_NUDGE_MAX_HOUR:
            skip_reasons["outside_window"] += 1
            continue
        if not user.nudges_enabled:
            skip_reasons["opted_out"] += 1
            continue
        today = effective_today(user)
        if user.last_nudge_sent_on == today:
            skip_reasons["already_nudged"] += 1
            continue
        if _user_logged_today(user):
            skip_reasons["logged_today"] += 1
            continue
        if not _user_active_recently(user):
            skip_reasons["inactive"] += 1
            continue
        # A session starting soon already has its own reminder — never double-ping.
        soon_cutoff = local_now + timedelta(hours=2)
        has_soon_workout = db.session.scalar(
            select(
                select(ScheduledWorkout.id)
                .where(
                    ScheduledWorkout.user_id == user.id,
                    ScheduledWorkout.status == "scheduled",
                    ScheduledWorkout.scheduled_for >= local_now,
                    ScheduledWorkout.scheduled_for <= soon_cutoff,
                )
                .exists()
            )
        )
        if has_soon_workout:
            skip_reasons["imminent_workout"] += 1
            continue
        considered += 1

        streak_days, _ = _logging_streak(list(user.logs), user)
        headline, body = _nudge_copy(user, streak_days)

        delivered = []
        if user.push_subscriptions:
            if push_module.send_web_push(user, title=f"fiT-X · {headline}", body=body, url="/dashboard", tag=f"nudge-{today.isoformat()}"):
                delivered.append("push")
        elif user.email_reminders_enabled:
            ok, _detail = _send_engagement_nudge_email(user, headline, body)
            if ok:
                delivered.append("email")

        if delivered:
            user.last_nudge_sent_on = today
            sent.append({"user": user.email, "via": delivered, "streak": streak_days})
        else:
            skip_reasons["no_channel"] += 1
            failed += 1

    db.session.commit()
    return {
        "nudged": len(sent), "skipped": failed, "detail": sent,
        "candidates": candidates, "considered": considered, "skip_reasons": skip_reasons,
    }


def _send_engagement_nudge_email(user: User, headline: str, body: str) -> tuple[bool, str]:
    smtp_user = current_app.config.get("SMTP_USER", "")
    smtp_password = current_app.config.get("SMTP_APP_PASSWORD", "")
    if not smtp_user:
        return False, "smtp not configured"
    dashboard_url = url_for("dashboard.dashboard", _external=True)
    subject = f"fiT-X · {headline}"
    plain = f"Hi {user.name},\n\n{body}\n\nOpen fiT-X: {dashboard_url}\n\n— fiT-X"
    html = render_template(
        "emails/daily_nudge.html",
        user_name=user.name,
        headline=headline,
        body=body,
        dashboard_url=dashboard_url,
    )
    return emails_module.send_via_smtp(user, subject, plain, html, smtp_user, smtp_password)


REMINDER_WINDOW_MINUTES = 45


def send_workout_reminders() -> dict:
    now_utc = datetime.utcnow()
    # Loose SQL prefilter so the per-user local window check in Python only sees
    # a handful of rows. Stored times are the user's wall clocks, so the bounds
    # must cover the largest UTC offsets (±14h) plus the window.
    candidates = db.session.execute(
        select(ScheduledWorkout)
        .join(User, ScheduledWorkout.user_id == User.id)
        .where(
            ScheduledWorkout.status == "scheduled",
            ScheduledWorkout.reminder_sent_at.is_(None),
            User.status == "active",  # suspended/banned users get no reminders
            ScheduledWorkout.scheduled_for >= now_utc - timedelta(hours=26),
            ScheduledWorkout.scheduled_for <= now_utc + timedelta(hours=14) + timedelta(minutes=REMINDER_WINDOW_MINUTES),
        )
    ).scalars().all()

    sent, failed = [], 0
    for workout in candidates:
        user = workout.user
        local_now = to_user_clock(user, now_utc)
        starts_in_min = (workout.scheduled_for - local_now).total_seconds() / 60
        if not -15 <= starts_in_min <= REMINDER_WINDOW_MINUTES:
            continue

        delivered = []
        if user.email_reminders_enabled:
            ok, detail = _send_reminder_email(user, workout, starts_in_min)
            if ok:
                delivered.append(
                    {
                        "workout": workout.title,
                        "to": user.email,
                        "starts_in_min": round(starts_in_min),
                        "detail": detail,
                    }
                )
            else:
                failed += 1

        if user.push_subscriptions:
            push_ok = push_module.send_web_push(
                user,
                title=f"fiT-X · {workout.title}",
                body="Starts now" if starts_in_min <= 0 else f"Starts in ~{round(starts_in_min)} min",
                url="/dashboard",
                tag=f"workout-{workout.id}",
            )
            if push_ok:
                delivered.append(
                    {
                        "workout": workout.title,
                        "to": "push",
                        "starts_in_min": round(starts_in_min),
                        "detail": "web push",
                    }
                )

        if delivered:
            # Any successful channel stamps the workout: the cron rechecks
            # every 10 minutes and must not resend.
            workout.reminder_sent_at = local_now
            sent.extend(delivered)

    db.session.commit()
    return {"checked": len(candidates), "sent": sent, "failed": failed}


def _send_reminder_email(user: User, workout: ScheduledWorkout, starts_in_min: float) -> tuple[bool, str]:
    when = workout.scheduled_for.strftime("%a, %d %b · %I:%M %p").lstrip("0")
    if starts_in_min <= 0:
        horizon = "starts now"
    else:
        horizon = f"starts in ~{round(starts_in_min)} min"
    subject = f"fiT-X · {workout.title} {horizon}"
    dashboard_url = url_for("dashboard.dashboard", _external=True)
    # The session plan lives in the Workouts tab now — deep-link the CTAs there.
    session_url = url_for("dashboard.dashboard", _external=True, _anchor="workouts")
    # Same builder the tab uses, environment included, so email and tab agree.
    plan = build_plan(workout.title, workout.environment or user.training_environment)

    # Week calendar for the email: Mon-Sun of the user's current local week,
    # with each day's weight-log state (done / today / missed / future).
    today = effective_today(user)
    logged_dates = {effective_date(log.date) for log in user.logs if log.date is not None}
    monday = today - timedelta(days=today.weekday())
    week_cells = []
    for offset in range(7):
        day = monday + timedelta(days=offset)
        if day == today:
            state = "today"
        elif day in logged_dates:
            state = "done"
        elif day > today:
            state = "future"
        else:
            state = "missed"
        week_cells.append({"num": day.day, "label": day.strftime("%a").upper(), "state": state})
    streak_days, _ = _logging_streak(list(user.logs), user)

    html = render_template(
        "emails/workout_reminder.html",
        user_name=user.name,
        user_email=user.email,
        workout_title=workout.title,
        when_label=when,
        duration_minutes=workout.duration_minutes,
        horizon=horizon,
        start_time=workout.scheduled_for.strftime("%H:%M"),
        start_weekday=workout.scheduled_for.strftime("%A").upper(),
        start_date=f"{workout.scheduled_for.strftime('%B')} {workout.scheduled_for.day}",
        workout_notes=(workout.notes or "").strip() or None,
        dashboard_url=dashboard_url,
        session_url=session_url,
        plan_blocks=plan["blocks"] if plan else None,
        plan_name=plan["name"] if plan else None,
        week_cells=week_cells,
        streak_days=streak_days,
        streak_today_logged=today in logged_dates,
    )
    plain = (
        f"{workout.title}\n{when} · {workout.duration_minutes} min\n{horizon}.\n\nOpen fiT-X: {dashboard_url}"
    )
    if plan:
        lines = [f"\n{plan['name'].upper()} PLAN"]
        for block in plan["blocks"]:
            lines.append(f"\n{block['block']} — {block['focus']}")
            lines.extend(f"  - {e['name']} · {e['dose']}" for e in block["exercises"])
        plain += "\n".join(lines)

    smtp_user = current_app.config.get("SMTP_USER", "")
    smtp_password = current_app.config.get("SMTP_APP_PASSWORD", "")
    if not (smtp_user and smtp_password):
        current_app.logger.info("[dry-run] reminder '%s' -> %s", subject, user.email)
        return True, "dry-run (SMTP not configured)"

    ok, detail = emails_module.send_via_smtp(user, subject, plain, html, smtp_user, smtp_password)
    if ok:
        return True, detail
    # Left unstamped so the next cron run retries.
    return False, detail
