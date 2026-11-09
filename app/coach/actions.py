"""Structured AI-action application: turning the coach model's JSON actions
into persisted rows, guarded by the food sanity limits.

The model describes the food; this module decides the numbers. Catalogue
values win for matched foods, per-unit ceilings and the per-entry calorie cap
reject-or-confirm instead of clamping, and every accepted write flows through
the same SQLAlchemy session as the caller's request.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from sqlalchemy import func, select

from app.extensions import db
from app.models import (
    Challenge,
    MealEntry,
    MEAL_TYPE_ORDER,
    ScheduledWorkout,
    User,
    UserChallenge,
    WaterLog,
    WeightLog,
)
from app.timeutil import to_user_clock
from app.validation import validate_environment
from app import foods as foods_module
from app.challenges import update_challenges


def default_meal_type(now: datetime) -> str:
    hour = now.hour
    if hour < 11:
        return "breakfast"
    if hour < 16:
        return "lunch"
    if hour < 19:
        return "snack"
    return "dinner"


def sync_current_weight(user: User) -> None:
    latest = db.session.execute(
        select(WeightLog)
        .where(WeightLog.user_id == user.id)
        .order_by(WeightLog.date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is not None:
        user.weight = latest.weight


def _clamp_int(value, minimum: int, maximum: int, default: int = 0) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _parse_quantity(value, default: float = 1.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed


# Entries in apply_actions' applied list that were NOT logged and instead
# need the user's confirmation. process_coach_message_ai moves them into the
# reply; every other entry stays an internal action record.
CONFIRM_PREFIX = "needs confirmation: "


def _meal_confirm_question(name: str, quantity: float, unit: str, kcal: int | None,
                           ceiling: int | None = None) -> str:
    shown_qty = f"{quantity:g}" if quantity == int(quantity) else f"{quantity:.1f}"
    if ceiling:
        # The quantity tripped the per-item ceiling: the ask is about the
        # count, not the size of the entry.
        return (
            f"{CONFIRM_PREFIX}I did not log {shown_qty} × {name} — that's more than "
            f"{ceiling} {name} in one go. Say 'yes, log it' to confirm, or tell me the correct amount."
        )
    # The calorie cap fired: only call the entry "unusually large" when it
    # really is a big number; small-but-over-cap entries skip the kcal clause.
    kcal_part = f" — that is about {kcal} kcal" if kcal and kcal >= 300 else ""
    return (
        f"{CONFIRM_PREFIX}I did not log {shown_qty} × {name}{kcal_part}, which is unusually "
        "large for one meal. Say 'yes, log it' to confirm, or tell me the correct amount."
    )


def apply_actions(user: User, actions: list, now: datetime) -> tuple[list[str], list[str]]:
    applied: list[str] = []
    challenge_notes: list[str] = []
    for action in actions[:6]:
        if not isinstance(action, dict):
            continue
        action_type = action.get("type")

        if action_type == "log_weight":
            try:
                weight_kg = round(float(action.get("weight_kg")), 1)
            except (TypeError, ValueError):
                continue
            if not 30 <= weight_kg <= 350:
                continue
            days_ago = _clamp_int(action.get("days_ago"), 0, 7, default=0)
            logged_at = to_user_clock(user, now) - timedelta(days=days_ago)
            db.session.add(WeightLog(weight=weight_kg, date=logged_at, user=user))
            if user.start_weight is None:
                user.start_weight = weight_kg
            sync_current_weight(user)
            challenge_notes.extend(update_challenges(user, {"kind": "weight", "at": logged_at}))
            applied.append(f"weight {weight_kg:.1f} kg")

        elif action_type == "log_meal":
            name = " ".join(str(action.get("name") or "Meal").split())[:60]
            meal_type = str(action.get("meal_type") or "").lower()
            if meal_type not in MEAL_TYPE_ORDER:
                meal_type = default_meal_type(to_user_clock(user, now))
            logged_at = to_user_clock(user, now)
            confirmed = bool(action.get("confirmed"))
            # The model describes the food; the app decides the numbers.
            quantity = _parse_quantity(action.get("quantity"))
            unit = " ".join(str(action.get("unit") or "serving").split()).lower()[:24]
            catalog_food = foods_module.resolve_food(name)
            computed = foods_module.compute_meal_from_catalogue(catalog_food, quantity, unit)
            ceiling = (
                foods_module.unit_ceiling_for(name)
                or (catalog_food and foods_module.unit_ceiling_for(catalog_food["name"]))
            )
            # Per-unit ceiling: reject and ask instead of logging, and never
            # accept a "confirmed" quantity beyond triple the ceiling. It only
            # guards counted items — "350 ml juice" or "1 glass of milk" is a
            # volume, not N pieces, so mass/container units skip it. The
            # per-entry calorie cap below still applies to every entry.
            count_unit = (
                foods_module.is_count_unit(unit, name)
                or (catalog_food and foods_module.is_count_unit(unit, catalog_food["name"]))
            )
            if ceiling and count_unit and quantity > ceiling:
                if not confirmed or quantity > ceiling * 3:
                    applied.append(_meal_confirm_question(
                        name, quantity, unit, computed and computed["calories"], ceiling=ceiling))
                    continue
            if computed:
                # Catalogue values override the model's own estimate (same
                # precedence the prompt states for food_database_matches).
                calories = computed["calories"]
                protein = computed["protein"]
                carbs = computed["carbs"]
                fats = computed["fats"]
            else:
                protein = _clamp_int(action.get("protein"), 0, 500)
                carbs = _clamp_int(action.get("carbs"), 0, 800)
                fats = _clamp_int(action.get("fats"), 0, 300)
                calories = _clamp_int(action.get("calories"), 0, 10000)
                if calories == 0 and (protein or carbs or fats):
                    calories = protein * 4 + carbs * 4 + fats * 9
                if calories <= 0:
                    continue
            # Per-entry calorie cap: reject or confirm, never clamp.
            if calories > foods_module.MEAL_ENTRY_CALORIE_CAP:
                if not confirmed or calories > foods_module.MEAL_ENTRY_HARD_CAP:
                    applied.append(_meal_confirm_question(name, quantity, unit, calories))
                    continue
            db.session.add(
                MealEntry(
                    name=name,
                    meal_type=meal_type,
                    calories=calories,
                    protein=protein,
                    carbs=carbs,
                    fats=fats,
                    logged_at=logged_at,
                    user=user,
                )
            )
            challenge_notes.extend(update_challenges(user, {"kind": "meal", "at": logged_at}))
            applied.append(f"{name} ({calories} kcal)")

        elif action_type == "log_water":
            amount_ml = _clamp_int(action.get("amount_ml"), 100, 5000, default=0)
            if not amount_ml:
                continue
            db.session.add(WaterLog(amount_ml=amount_ml, logged_at=to_user_clock(user, now), user=user))
            applied.append(f"{amount_ml} ml water")

        elif action_type in {"schedule_workout", "complete_workout"}:
            title = " ".join(str(action.get("title") or "Workout").split())[:80]
            duration_minutes = _clamp_int(action.get("duration_minutes"), 5, 300, default=30)
            calories_burned = _clamp_int(action.get("calories_burned"), 0, 4000, default=0) or None
            # The model may echo an environment it saw in the user's words
            # ("home workout"); anything else falls back to the profile default.
            environment = validate_environment(action.get("environment")) or user.training_environment

            if action_type == "schedule_workout":
                days_ahead = _clamp_int(action.get("days_ahead"), 0, 30, default=0)
                raw_time = str(action.get("time_24h") or "18:00")
                try:
                    hour, minute = (int(part) for part in raw_time.split(":")[:2])
                    scheduled_time = time(hour=max(0, min(23, hour)), minute=max(0, min(59, minute)))
                except ValueError:
                    scheduled_time = time(hour=18, minute=0)
                local_now = to_user_clock(user, now)
                scheduled_for = datetime.combine((local_now + timedelta(days=days_ahead)).date(), scheduled_time)
                if scheduled_for <= local_now:
                    scheduled_for = datetime.combine((local_now + timedelta(days=1)).date(), scheduled_time)
                db.session.add(
                    ScheduledWorkout(
                        title=title,
                        scheduled_for=scheduled_for,
                        duration_minutes=duration_minutes,
                        calories_burned=calories_burned,
                        environment=environment,
                        status="scheduled",
                        user=user,
                        created_at=local_now,
                    )
                )
                applied.append(f"scheduled {title}")
            else:
                days_ago = _clamp_int(action.get("days_ago"), 0, 7, default=0)
                # scheduled_for is the user's wall clock like every stored
                # datetime; deriving it from the UTC instant would land the
                # row in the wrong week/recent windows for non-UTC users.
                completed_at = to_user_clock(user, now) - timedelta(days=days_ago)
                db.session.add(
                    ScheduledWorkout(
                        title=title,
                        scheduled_for=completed_at,
                        duration_minutes=duration_minutes,
                        calories_burned=calories_burned,
                        status="completed",
                        user=user,
                        created_at=to_user_clock(user, now),
                    )
                )
                challenge_notes.extend(update_challenges(user, {
                    "kind": "workout", "at": completed_at,
                    "title": title, "calories_burned": calories_burned,
                }))
                applied.append(f"completed {title}")

        elif action_type == "join_challenge":
            title = " ".join(str(action.get("challenge_title") or "").split())
            challenge = None
            if title:
                challenge = db.session.scalar(
                    select(Challenge).where(
                        func.lower(Challenge.title) == title.lower(),
                        Challenge.status == "active",
                    )
                ) or db.session.scalar(
                    select(Challenge)
                    .where(
                        Challenge.title.ilike(f"%{title}%"),
                        Challenge.status == "active",
                    )
                    .limit(1)
                )
            if challenge is None:
                applied.append(f"no open challenge named {title!r}")
                continue
            already = db.session.scalar(
                select(UserChallenge.id).where(
                    UserChallenge.user_id == user.id,
                    UserChallenge.challenge_id == challenge.id,
                )
            )
            if already:
                applied.append(f"already in {challenge.title}")
                continue
            db.session.add(UserChallenge(user_id=user.id, challenge_id=challenge.id))
            applied.append(f"joined {challenge.title}")

    return applied, challenge_notes
