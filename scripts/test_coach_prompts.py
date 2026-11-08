"""Run the 10 acceptance prompts through the real coach pipeline and print
the full output: coach reply, actions applied, and the session plan the app
generates for every scheduled workout (title -> environment -> build_plan).

Uses the dev .env config, so the AI providers are live — same as production.
Run:  python scripts/test_coach_prompts.py [test_id ...]
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from app.extensions import db
from app.models import ScheduledWorkout, User
from app.plans import build_plan
from sqlalchemy import select

PROMPTS = [
    {"id": "sched_01", "prompt": "Schedule boxing for today."},
    {"id": "sched_02", "prompt": "Plan a running workout for tomorrow."},
    {"id": "sched_03", "prompt": "Schedule gym push day for today."},
    {"id": "sched_04", "prompt": "Plan a pull workout for tomorrow."},
    {"id": "sched_05", "prompt": "Schedule leg day at the gym for today."},
    {"id": "sched_06", "prompt": "Create a home push workout for today."},
    {"id": "sched_07", "prompt": "Schedule a home pull workout for tomorrow."},
    {"id": "sched_08", "prompt": "Plan swimming for today."},
    {"id": "sched_09", "prompt": "Schedule a full-body workout for tomorrow."},
    {"id": "sched_10", "prompt": "Plan a cycling session for today."},
]


def show_plan(workout, user_env):
    plan = build_plan(workout.title, workout.environment or user_env, generic_fallback=True)
    print(f"    PLAN for {workout.title!r} (env={workout.environment or user_env}):")
    if not plan:
        print("      (no plan generated)")
        return
    for block in plan["blocks"]:
        print(f"      [{block['block']}] {block['focus']}")
        for line in block["exercises"]:
            met = f" met={line['met']}" if line.get("met") else ""
            alt = f" | alt: {line['alternate']}" if line.get("alternate") else ""
            print(f"        - {line['name']} — {line['dose']}{met}{alt}")


def main() -> int:
    only = set(sys.argv[1:])
    app = create_app()
    with app.app_context():
        db.create_all()
        user = db.session.scalars(select(User).where(User.email == "prompttest@fitx.dev")).one_or_none()
        if user is None:
            user = User(name="Prompt Tester", email="prompttest@fitx.dev",
                        weight=75, created_at=datetime.utcnow(), onboarding=True, status="active")
            user.set_password("pw123456")
            db.session.add(user)
            db.session.commit()

        for item in PROMPTS:
            if only and item["id"] not in only:
                continue
            print(f"\n{'=' * 78}\n{item['id']}: {item['prompt']}\n{'-' * 78}")
            now = datetime(2026, 11, 8, 10, 0, 0)  # a Sunday, so 'next Monday' etc. resolve
            outcome = None
            from app.dashboard import process_coach_message
            try:
                outcome = process_coach_message(user, item["prompt"], now)
                db.session.commit()
            except Exception as exc:  # report, keep going
                print(f"  ERROR: {type(exc).__name__}: {exc}")
                db.session.rollback()
                continue
            print(f"  PROVIDER/ACTION: {outcome.action}")
            print(f"  REPLY: {outcome.reply}")
            # Re-read everything the user now has scheduled (the run cleans up after itself).
            sessions = db.session.scalars(
                select(ScheduledWorkout).where(ScheduledWorkout.user_id == user.id)
                .order_by(ScheduledWorkout.id.asc())
            ).all()
            for w in sessions:
                print(f"    SESSION: {w.title} | {w.scheduled_for.strftime('%a %d %b %H:%M')} | "
                      f"{w.duration_minutes} min | {w.calories_burned or '–'} kcal | status={w.status}")
                if w.status == "scheduled":
                    show_plan(w, user.training_environment)
            for w in sessions:
                db.session.delete(w)
            db.session.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
