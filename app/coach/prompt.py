"""Coach prompt and conversation-memory construction.

The prompt needs dashboard-derived context (today's meals, week activity,
upcoming workouts). Those query helpers remain in dashboard — they are the
same context the dashboard page builds — and are late-imported here to avoid
a circular import while the module split settles.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy import delete, select

from app.extensions import db
from app.models import Challenge, CoachMessage, User, UserChallenge
from app.challenges import challenge_visible_to
from app.health import DEFAULT_CALORIE_TARGET, estimate_calorie_target
from app.foods import food_db_matches
from app.timeutil import day_bounds, to_user_clock, user_now, DAY_START_SHIFT

from . import CoachAIError

AI_HISTORY_LIMIT = 6


def challenges_context(user: User) -> list[dict]:
    """Active challenges with the user's participation state, so the coach
    can suggest and join them."""
    out = []
    joined = {
        uc.challenge_id: uc
        for uc in db.session.scalars(
            select(UserChallenge).where(
                UserChallenge.user_id == user.id, UserChallenge.status == "active",
            )
        ).all()
    }
    for c in db.session.scalars(
        select(Challenge).where(Challenge.status == "active").order_by(Challenge.title)
    ).all():
        if not challenge_visible_to(c, user):
            continue
        uc = joined.get(c.id)
        out.append({
            "title": c.title,
            "type": c.type,
            "metric": c.metric,
            "target": c.target_value,
            "daily_target": c.daily_target,
            "joined": uc is not None,
            "progress": (uc.progress_value if uc else None),
            "current_streak": (uc.current_streak if uc else None),
        })
        if len(out) >= 10:
            break
    return out


def build_ai_context(user: User, now: datetime, message_text: str = "") -> str:
    from app import dashboard  # late import: meal/week/water context helpers

    local_now = to_user_clock(user, now)
    today_start, today_end = day_bounds(local_now)
    today_totals = dashboard._sum_meal_entries(
        dashboard._get_meal_entries_between(user, today_start, today_end))
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
            pace=user.pace or 2,
        )

    week_activity = dashboard._build_week_activity(user, local_now)
    upcoming = dashboard._get_upcoming_workouts(user, local_now)
    next_workout = upcoming[0] if upcoming else None
    water_l = dashboard._get_water_today_liters(user, local_now)

    context = {
        "user_local_today": (local_now - DAY_START_SHIFT).strftime("%Y-%m-%d (%A)"),
            "user_local_time": local_now.strftime("%H:%M"),
            "timezone_note": (
                "All dates/times above are the USER'S LOCAL wall clock, and the fiT-X day "
                "runs 4:30 AM to 4:30 AM — a log at 1 AM still counts for the previous day. "
                "Interpret 'today', 'tomorrow', 'tonight', 'morning' etc. using user_local_today, "
                "and put scheduled times in this same local wall clock."
            ),
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
            "active_challenges": challenges_context(user),
        }
    food_matches = food_db_matches(message_text)
    if food_matches:
        context["food_database_matches"] = food_matches
    return json.dumps(context, ensure_ascii=False)


def build_system_prompt(user: User, now: datetime, message_text: str = "") -> str:
    return f"""You are fiT-X, the AI coach inside a fitness web app. You understand natural language in any phrasing and turn it into structured logging actions for the user's account.

Current date and time on the user's device: {build_ai_context(user, now, message_text)}

You reply with ONLY one JSON object, no markdown fences, matching exactly:
{{
  "reply": "short friendly confirmation or answer (max 3 sentences)",
  "actions": []
}}

ACTION TYPES (each an object inside "actions"; use an empty array for pure questions):
1. {{"type":"log_weight","weight_kg":number,"days_ago":0}}  — weight check-in; days_ago 0-7 (1 means yesterday). Valid weight 30-350 kg. Keep the exact value the user said, decimals included (they said 81.9 → send 81.9, never round to 82).
2. {{"type":"log_meal","name":"short food name","quantity":number,"unit":"pieces|medium|bowl|cup|grams|ml|serving","meal_type":"breakfast|lunch|dinner|snack","calories":int,"protein":int,"carbs":int,"fats":int,"confirmed":false}}  — quantity is how many of the unit the user ate (default 1): "2 rotis" → quantity 2, unit "pieces"; "200 g rice" → quantity 200, unit "grams". calories/macros are still your per-portion estimate; the app recomputes calories from its own food database when the food matches it, and those values win. If the user gives only macros, set calories = protein*4 + carbs*4 + fats*9. If only calories are given, leave macros 0. Pick meal_type from context (time of day or the user's words). Never set "confirmed" yourself — see RULES.
3. {{"type":"log_water","amount_ml":int}}  — 100-5000 ml. "a glass" ≈ 250, "a bottle" ≈ 500.
4. {{"type":"schedule_workout","title":"Activity","days_ahead":0,"time_24h":"18:30","duration_minutes":int,"calories_burned":int,"environment":"gym|home|home_gym"}}  — ANY activity counts: gym, running, cycling, swimming, yoga, pilates, boxing, martial arts, sports, walking. days_ahead 0-30 (0 = today; if that time already passed, use tomorrow). time_24h defaults to the user's words or "18:00". duration 5-240 min. environment is OPTIONAL — set it only when the user's words say where they train ("home workout", "at the gym"); omit otherwise. Estimate calories_burned from the user's weight, duration and intensity (light yoga ≈ 3 kcal/kg/h, brisk walk ≈ 4.3, cycling ≈ 6, running ≈ 10, HIIT/boxing ≈ 9; round to the nearest 10).
5. {{"type":"complete_workout","title":"Activity","days_ago":0,"duration_minutes":int,"calories_burned":int}}  — the user already finished it. days_ago 0-7. Estimate burn the same way.
6. {{"type":"join_challenge","challenge_title":"exact title from active_challenges"}}  — the user wants to join one of the app's active challenges (not a workout). Only for titles in the active_challenges list, and only ones with "joined": false. If they are already joined, just say so with empty actions.

RULES:
- The app already computes the user's official daily calorie target (daily_calorie_target in the context). Treat it as the single source of truth for intake advice — never present a different daily number as the plan. If the user asks for more aggressive or faster pacing, explain the current target and why it is conservative; suggest talking to a professional for anything beyond it.
- Sanity guard on quantities: the app rejects implausibly large meals (per-food quantity ceilings, a per-entry calorie cap) and asks the user to confirm instead of logging. If your reply was preceded by such a rejection, do not guess a smaller number on your own — the user was asked to confirm or correct the amount. When the user now clearly confirms the original amount ("yes, log it"), repeat the SAME log_meal action unchanged with "confirmed": true. Only an explicit user confirmation sets confirmed to true.
- The user may phrase anything casually ("had 2 rotis and dal", "did 40 min zone 2", "leg day tomorrow 7am"). Interpret sensibly and fill every field.
- For any food you can recognize, ESTIMATE the calories and macros yourself from your nutrition knowledge ("7 steamed chicken momos" ≈ 40 kcal / 3g protein each, "1 plain dosa", "a bowl of dal") and log it — never ask the user for calories or macros on recognizable food. Note the estimate briefly in the reply (e.g. "Logged 7 steamed momos, roughly 380 kcal"). Only ask a clarifying question when you genuinely cannot tell what or how much they ate.
- The user may give partial numbers ("300 kcal rotis", "20g protein shake") — use what they gave and estimate only the missing pieces.
- For progress questions ("how am i doing", "what did I eat today"), answer from the context data above with empty actions.
- food_database_matches lists entries from the app's curated food database whose names appear in the user's message. When logging one of those foods, base calories/macros on the listed per-serving values scaled to the portion the user describes ("2 rotis" with a 1-roti serving → double it). These values override your own estimates.
- active_challenges lists the app's open challenges with the user's participation state. If the user asks which challenge to do or for a recommendation, suggest one from that list that fits their goal and this week's activity, and mention their progress if already joined. Use the join_challenge action only when they clearly want to join.
- Reply in the language the user writes in.
- Never wrap the JSON in markdown fences."""


def chat_day_start(user: User) -> datetime:
    # Chat memory resets with the fiT-X day at 4:30 AM local (see APP_DAY_START),
    # not the calendar midnight.
    return day_bounds(user_now(user))[0]


def prune_old_chat_messages(user: User) -> None:
    # Chat memory is per-fiT-X-day: anything from before 4:30 AM local is
    # deleted, not just hidden.
    db.session.execute(
        delete(CoachMessage).where(
            CoachMessage.user_id == user.id,
            CoachMessage.created_at < chat_day_start(user),
        )
    )


def recent_history(user: User) -> list[CoachMessage]:
    """The day's recent chat turns (newest first) shared by all AI providers."""
    return list(
        db.session.execute(
            select(CoachMessage)
            .where(
                CoachMessage.user_id == user.id,
                CoachMessage.created_at >= chat_day_start(user),
                CoachMessage.role != "error",  # failed AI attempts are telemetry only
            )
            .order_by(CoachMessage.id.desc())
            .limit(AI_HISTORY_LIMIT)
        ).scalars().all()
    )


def parse_coach_json(text: str, provider: str) -> dict:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise CoachAIError(f"{provider} returned non-JSON: {cleaned[:300]}") from exc

    if not isinstance(parsed, dict) or not isinstance(parsed.get("reply"), str):
        raise CoachAIError(f"{provider} JSON missing reply: {cleaned[:300]}")

    actions = parsed.get("actions", [])
    return {"reply": parsed["reply"].strip(), "actions": actions if isinstance(actions, list) else []}
