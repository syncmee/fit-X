"""One-off integration check: reminder emails render the session-plan section.

Patches out the SMTP send, creates due test workouts for each mapped title,
runs the real cron logic, and saves the rendered HTML so it can be inspected
in a browser. Rows are cleaned up afterwards; nothing leaves the machine.
"""
import os
import sys
from datetime import datetime, timedelta

# Pin to the local sqlite DB: this harness writes real ScheduledWorkout rows.
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")

import app.dashboard as dash
from app import create_app
from app.extensions import db
from app.models import ScheduledWorkout, User

TITLES = ["Boxing", "Running", "Upper Body", "Leg Day", "Yoga"]
captured = []


def fake_send(user, subject, plain, html, smtp_user, smtp_password):
    captured.append({"title": subject, "plain": plain, "html": html})
    return True, "captured (not sent)"


app = create_app()
with app.app_context(), app.test_request_context("/"):
    user = db.session.get(User, 6)
    user.email_reminders_enabled = True
    user.tz_offset_minutes = 0

    rows = []
    for i, title in enumerate(TITLES):
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

    print("cron result:", result)

    # Sanity checks on the captured emails.
    def which_title(subject: str) -> str | None:
        for t in TITLES:
            if subject.startswith(f"fiT-X · {t} "):
                return t
        return None

    for title in TITLES:
        c = next((x for x in captured if which_title(x["title"]) == title), None)
        if not c:
            print(f"!! no email captured for {title!r}")
            continue
        has_plan = "SESSION PLAN" in c["html"]
        rows_n = c["html"].count('letter-spacing:0.4px; color:#4ADE80;')
        plain_has = "PLAN" in c["plain"]
        print(f"{title:<11} html-plan-section={has_plan!s:<5} exercise-rows={rows_n:<3} plain-plan={plain_has}")
        safe = title.lower().replace(" ", "_")
        with open(f"previews/email_{safe}.html", "w", encoding="utf-8") as f:
            f.write(c["html"])

    for r in rows:
        db.session.delete(r)
    db.session.commit()
    print("test rows cleaned up")
