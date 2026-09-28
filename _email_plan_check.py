"""One-off integration check: reminder emails render the session-plan section.

Patches out the SMTP send, creates due test workouts, runs the real cron logic,
and saves the rendered HTML so it can be inspected in a browser. Rows are cleaned
up afterwards; nothing leaves the machine. Also asserts plan shapes directly via
app.plans.build_plan for every gym title the composer must distinguish.

Pinned to the local sqlite DB: this harness writes real ScheduledWorkout rows.
"""
import os
import sys
from datetime import datetime, timedelta

os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")

import app.dashboard as dash
from app import create_app
from app.extensions import db
from app.models import ScheduledWorkout, User
from app.plans import build_plan

TITLES = ["Boxing", "Running", "Push", "Pull", "Back and Biceps", "Chest and Triceps", "Leg Day", "Yoga"]
captured = []


def fake_send(user, subject, plain, html, smtp_user, smtp_password):
    captured.append({"title": subject, "plain": plain, "html": html})
    return True, "captured (not sent)"


# ---- pure plan-shape assertions (no DB) ----
EXPECT = {
    "Push": ("Push", ["WARM-UP", "CHEST", "SHOULDERS", "TRICEPS", "COOL-DOWN"], [2, 2, 2]),
    "Pull": ("Pull", ["WARM-UP", "BACK", "BICEPS", "COOL-DOWN"], [3, 2]),
    "Back and Biceps": ("Back · Biceps", ["WARM-UP", "BACK", "BICEPS", "COOL-DOWN"], [3, 2]),
    "Chest and Triceps": ("Chest · Triceps", ["WARM-UP", "CHEST", "TRICEPS", "COOL-DOWN"], [3, 2]),
    "Upper Body": ("Upper Body", ["WARM-UP", "CHEST", "BACK", "SHOULDERS", "ARMS", "COOL-DOWN"], [2, 2, 2, 1]),
    "Leg Day": ("Legs", ["WARM-UP", "LEGS", "COOL-DOWN"], [3]),
    "Arm Day": ("Arms", ["WARM-UP", "ARMS", "COOL-DOWN"], [3]),
    "Full Body": ("Full Body", ["WARM-UP", "CHEST", "BACK", "SHOULDERS", "LEGS", "CORE", "COOL-DOWN"], [2, 2, 1, 1, 1]),
    "Boxing": ("Boxing", None, None),
    "Running": ("Running", None, None),
}
failures = []
for title, (want_name, want_blocks, want_counts) in EXPECT.items():
    plan = build_plan(title)
    if title == "Yoga":
        if plan is not None:
            failures.append(f"Yoga: expected no plan, got {plan['name']}")
        continue
    if plan is None:
        failures.append(f"{title}: no plan returned")
        continue
    if plan["name"] != want_name:
        failures.append(f"{title}: name {plan['name']!r} != {want_name!r}")
    if want_blocks:
        labels = [b["block"].upper() for b in plan["blocks"]]
        if labels != want_blocks:
            failures.append(f"{title}: blocks {labels} != {want_blocks}")
        gym = [b for b in plan["blocks"] if b["block"].upper() not in ("WARM-UP", "COOL-DOWN")]
        counts = [len(b["exercises"]) for b in gym]
        if counts != want_counts:
            failures.append(f"{title}: per-block rows {counts} != {want_counts}")
        for b in gym:
            for e in b["exercises"]:
                if e["source"] != "free-exercise-db":
                    failures.append(f"{title}: {b['block']} row not DB-backed: {e}")

print(f"plan-shape assertions: {'ALL PASS' if not failures else 'FAILURES'}")
for f in failures:
    print("  !!", f)

# ---- end-to-end email render through the real cron logic ----
EMAIL_TITLES = ["Boxing", "Back and Biceps", "Yoga"]

app = create_app()
with app.app_context(), app.test_request_context("/"):
    user = db.session.get(User, 6)
    user.email_reminders_enabled = True
    user.tz_offset_minutes = 0

    rows = []
    for i, title in enumerate(EMAIL_TITLES):
        rows.append(
            ScheduledWorkout(
                title=title,
                scheduled_for=datetime.utcnow() + timedelta(minutes=20 + i),
                duration_minutes=60,
                status="scheduled",
                notes=None,
                user_id=user.id,
            )
        )
    db.session.add_all(rows)
    db.session.commit()

    dash._send_via_smtp = fake_send
    try:
        result = dash._send_due_workout_reminders()
    finally:
        from app.dashboard import _send_via_smtp as real_send
        dash._send_via_smtp = real_send

    print("cron result:", {"checked": result["checked"], "sent": len(result["sent"]), "failed": result["failed"]})

    def which_title(subject: str) -> str | None:
        for t in EMAIL_TITLES:
            if subject.startswith(f"fiT-X · {t} "):
                return t
        return None

    for title in EMAIL_TITLES:
        c = next((x for x in captured if which_title(x["title"]) == title), None)
        if not c:
            print(f"!! no email captured for {title!r}")
            continue
        plan = build_plan(title)
        want_rows = sum(len(b["exercises"]) for b in plan["blocks"]) if plan else 0
        rows_n = c["html"].count('letter-spacing:0.4px; color:#4ADE80;')
        has_plan = rows_n == want_rows
        ok = rows_n == want_rows
        print(f"{title:<16} plan-section={has_plan!s:<5} rows={rows_n} (want {want_rows}) {'OK' if ok else '!! MISMATCH'}")
        safe = title.lower().replace(" ", "_")
        with open(f"previews/email_{safe}.html", "w", encoding="utf-8") as f:
            f.write(c["html"])

    for r in rows:
        db.session.delete(r)
    db.session.commit()
    print("test rows cleaned up")
