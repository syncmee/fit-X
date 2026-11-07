"""Regression tests that EXECUTE the cron job bodies with due data.

The Phase C module split left two undefined names that only fired when a
reminder/nudge path actually ran with real candidates (cron-job.org got 500s
while the sparse GitHub runs happened to miss those paths). These tests run
the jobs end-to-end with the delivery primitives patched, so a broken job
path fails the suite.
"""
import unittest
from datetime import datetime, timedelta

from tests.base import AppContextTestCase, login, make_user

from app import cron
from app import emails as emails_mod
from app import push as push_mod
from app.extensions import db
from app.models import ScheduledWorkout, User, WeightLog


class CronJobsTestCase(AppContextTestCase):
    def setUp(self):
        super().setUp()
        # Dead local port: any SMTP attempt refuses instantly and is caught
        # by send_via_smtp, keeping the tests hermetic and fast.
        self.joe_id = make_user(self.app, name="joe", email="joe@test.dev")
        # Patch the delivery primitives (restored after each test) so the
        # jobs run their full paths deterministically and hermetically.
        self.smtp_calls = []
        self._orig_smtp = emails_mod.send_via_smtp
        emails_mod.send_via_smtp = (
            lambda user, subject, plain, html, su, sp:
            (self.smtp_calls.append(subject), (True, "ok"))[1])
        self.addCleanup(setattr, emails_mod, "send_via_smtp", self._orig_smtp)
        self._orig_push = push_mod.send_web_push
        push_mod.send_web_push = lambda user, **kw: True
        self.addCleanup(setattr, push_mod, "send_web_push", self._orig_push)

    def _user(self):
        return db.session.scalars(select(User).where(User.id == self.joe_id)).one()

    def test_workout_reminder_path_executes_for_due_session(self):
        """A due session with reminders on must reach the email builder (the
        Phase C refactor 500-ed here: _logging_streak was undefined)."""
        from sqlalchemy import select

        user = self._user()
        workout = ScheduledWorkout(
            title="Push Day",
            scheduled_for=datetime.utcnow() + timedelta(minutes=20),
            duration_minutes=60,
            status="scheduled",
            user=user,
            created_at=datetime.utcnow(),
        )
        db.session.add(workout)
        db.session.commit()

        # The jobs build absolute email links, which needs a request context
        # (in production the /cron route supplies one).
        with self.app.test_request_context(base_url="https://test.local"):
            result = cron.send_workout_reminders()

        self.assertEqual(result["checked"], 1)
        # The email builder ran (regression: it used to NameError) and the
        # successful send stamped the workout one-shot.
        self.assertEqual(len(self.smtp_calls), 1)
        # Any successful channel stamps the workout (one-shot reminder).
        self.assertIsNotNone(
            db.session.get(ScheduledWorkout, workout.id).reminder_sent_at)

    def test_engagement_nudge_path_executes_for_considered_user(self):
        """A considered user (silent today, active recently) must reach the
        streak/copy computation (the second undefined name 500-ed here)."""
        from sqlalchemy import select

        user = self._user()
        user.last_nudge_sent_on = None
        db.session.add(WeightLog(
            weight=80.0, date=datetime.utcnow() - timedelta(days=2), user=user))
        db.session.commit()

        with self.app.test_request_context(base_url="https://test.local"):
            result = cron.send_engagement_nudges(ignore_hour=True)

        self.assertEqual(result["candidates"], 1)
        self.assertEqual(result["considered"], 1)
        # Delivered via email and stamped once (regression: used to NameError).
        self.assertEqual(result["nudged"], 1)
        self.assertEqual(len(self.smtp_calls), 1)
        self.assertIsNotNone(self._user().last_nudge_sent_on)

    def test_profile_timezone_route_clamps_offset(self):
        """The dashboard calls this on every load; the Phase C refactor
        500-ed here (_clamp_int moved out of dashboard)."""
        from tests.base import login as base_login

        client = self.app.test_client()
        base_login(client, self.app, "joe@test.dev")
        token = None
        import re

        page = client.get("/dashboard").get_data(as_text=True)
        match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page)
        token = match.group(1) if match else None
        # The route reads JSON, and validate_csrf_request needs the header.
        r = client.post(
            "/profile/timezone",
            json={"offset": 330},
            headers={"X-CSRFToken": token} if token else {},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["tz_offset_minutes"], 330)


from sqlalchemy import select  # noqa: E402  (used inside test methods)

if __name__ == "__main__":
    unittest.main()
