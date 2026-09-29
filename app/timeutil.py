"""Single home for fiT-X time conventions.

One rule: every datetime stored in the database is the USER'S LOCAL WALL
CLOCK (naive), stamped from their browser-reported UTC offset
(User.tz_offset_minutes, JS getTimezoneOffset()). Nothing in the DB is UTC,
so queries, charts, and templates use stored values as-is — all conversions
live in this module.

The fiT-X day runs 04:30 -> 04:30 local, so a log at 1 AM still counts for
the previous day (late-night logging).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

# The fiT-X day rolls over at 4:30 AM local — a 1 AM snack still belongs to
# yesterday's macros, and daily resets happen while the user sleeps.
APP_DAY_START = time(hour=4, minute=30)
DAY_START_SHIFT = timedelta(hours=4, minutes=30)


def to_user_clock(user, now: datetime | None = None) -> datetime:
    """A UTC instant (default: now) as the user's local wall clock."""
    minutes = getattr(user, "tz_offset_minutes", None) or 0
    return (now or datetime.utcnow()) - timedelta(minutes=minutes)


def user_now(user) -> datetime:
    """The user's current wall clock — the stamp for new rows."""
    return to_user_clock(user)


def effective_date(dt: datetime) -> date:
    """Which fiT-X day a stored wall-clock timestamp belongs to: a 01:00 log
    belongs to the day that started at 04:30 the previous morning."""
    return (dt - DAY_START_SHIFT).date()


def effective_today(user) -> date:
    """The fiT-X date the user is currently 'on' (shifts at 4:30 AM local)."""
    return effective_date(user_now(user))


def day_bounds(local_now: datetime, day_offset: int = 0) -> tuple[datetime, datetime]:
    """[start, end) of the user's fiT-X day (04:30 to 04:30 local wall clock)."""
    logical_date = (local_now - DAY_START_SHIFT + timedelta(days=day_offset)).date()
    start = datetime.combine(logical_date, APP_DAY_START)
    return start, start + timedelta(days=1)
