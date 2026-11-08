"""Scheduled-session management: delete and edit routes, their ownership and
state guards, and the dashboard card actions."""
import re
import unittest
from datetime import datetime, timedelta

from tests.base import AppContextTestCase, login, make_user

from app.extensions import db
from app.models import ScheduledWorkout, User
from sqlalchemy import select, func


def _csrf(client, path="/dashboard"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html).group(1)


class WorkoutManageTestCase(AppContextTestCase):
    def setUp(self):
        super().setUp()
        make_user(self.app, name="joe", email="joe@test.dev")
        make_user(self.app, name="mallory", email="mallory@test.dev")
        self.client = self.app.test_client()
        login(self.client, self.app, "joe@test.dev")
        self.future = datetime.utcnow() + timedelta(days=2)

    def _user(self, email="joe@test.dev"):
        return db.session.scalars(select(User).where(User.email == email)).one()

    def _make_session(self, user=None, status="scheduled", **fields):
        defaults = dict(
            title="Push Day",
            scheduled_for=self.future,
            duration_minutes=60,
            status=status,
            user=user or self._user(),
            created_at=datetime.utcnow(),
        )
        defaults.update(fields)
        workout = ScheduledWorkout(**defaults)
        db.session.add(workout)
        db.session.commit()
        return workout

    def _post(self, url, data=None):
        return self.client.post(url, data={**(data or {}), "csrf_token": _csrf(self.client)})

    # --- delete -----------------------------------------------------------

    def test_delete_removes_scheduled_session(self):
        workout = self._make_session()
        r = self._post(f"/workouts/{workout.id}/delete")
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(db.session.get(ScheduledWorkout, workout.id))

    def test_delete_completed_is_rejected(self):
        workout = self._make_session(status="completed")
        self._post(f"/workouts/{workout.id}/delete")
        self.assertIsNotNone(db.session.get(ScheduledWorkout, workout.id))

    def test_delete_someone_elses_session_is_rejected(self):
        workout = self._make_session(user=self._user("mallory@test.dev"))
        self._post(f"/workouts/{workout.id}/delete")
        self.assertIsNotNone(db.session.get(ScheduledWorkout, workout.id))

    # --- edit -------------------------------------------------------------

    def test_edit_moves_session_date_and_time_only(self):
        """Editing moves the slot; the plan itself (title, duration) is
        immutable — to change exercises the user removes and re-schedules."""
        workout = self._make_session()
        new_day = (datetime.utcnow() + timedelta(days=3)).strftime("%Y-%m-%d")
        r = self._post(f"/workouts/{workout.id}/edit", {
            "title": "Pull Day", "date": new_day, "time": "07:30", "duration": "45",
        })
        self.assertEqual(r.status_code, 302)
        updated = db.session.get(ScheduledWorkout, workout.id)
        # The slot moved...
        self.assertEqual(updated.scheduled_for.strftime("%H:%M"), "07:30")
        # ...the plan did not.
        self.assertEqual(updated.title, "Push Day")
        self.assertEqual(updated.duration_minutes, 60)

    def test_edit_to_past_is_rejected(self):
        workout = self._make_session()
        past = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        self._post(f"/workouts/{workout.id}/edit", {
            "date": past, "time": "07:30",
        })
        updated = db.session.get(ScheduledWorkout, workout.id)
        self.assertEqual(updated.scheduled_for, self.future)

    def test_edit_invalid_date_is_rejected(self):
        workout = self._make_session()
        self._post(f"/workouts/{workout.id}/edit", {
            "date": "not-a-date", "time": "07:30",
        })
        updated = db.session.get(ScheduledWorkout, workout.id)
        self.assertEqual(updated.scheduled_for, self.future)

    def test_edit_someone_elses_session_is_rejected(self):
        workout = self._make_session(user=self._user("mallory@test.dev"))
        new_day = (datetime.utcnow() + timedelta(days=3)).strftime("%Y-%m-%d")
        self._post(f"/workouts/{workout.id}/edit", {
            "date": new_day, "time": "07:30",
        })
        updated = db.session.get(ScheduledWorkout, workout.id)
        self.assertEqual(updated.title, "Push Day")

    def test_edit_completed_is_rejected(self):
        workout = self._make_session(status="completed")
        new_day = (datetime.utcnow() + timedelta(days=3)).strftime("%Y-%m-%d")
        self._post(f"/workouts/{workout.id}/edit", {
            "date": new_day, "time": "07:30",
        })
        updated = db.session.get(ScheduledWorkout, workout.id)
        self.assertEqual(updated.title, "Push Day")

    # --- queue window: sessions list until their fiT-X day ends ------------

    def test_queue_grace_is_the_fitx_day_not_24h_from_slot(self):
        from app.dashboard import _get_upcoming_workouts

        user = self._user()
        # Two days ago, yesterday late evening, tomorrow, and tonight.
        two_days_ago = datetime.utcnow().replace(hour=18, minute=0) - timedelta(days=2)
        yesterday_late = datetime.utcnow().replace(hour=22, minute=0) - timedelta(days=1)
        tomorrow = datetime.utcnow() + timedelta(days=1)
        tonight = datetime.utcnow().replace(hour=23, minute=0)
        self._make_session(title="Two Days Ago", scheduled_for=two_days_ago)
        self._make_session(title="Yesterday Late", scheduled_for=yesterday_late)
        self._make_session(title="Tomorrow", scheduled_for=tomorrow)
        self._make_session(title="Tonight", scheduled_for=tonight)

        # 3 AM: the fiT-X day still covers yesterday evening — mark-done stays
        # reachable through the night.
        now_3am = datetime.utcnow().replace(hour=3, minute=0)
        shown = [w.title for w in _get_upcoming_workouts(user, now_3am)]
        self.assertIn("Yesterday Late", shown)
        self.assertIn("Tomorrow", shown)
        self.assertNotIn("Two Days Ago", shown)

        # 5 AM: the fiT-X day rolled over — yesterday's session is gone even
        # though it is barely 7 hours past its slot.
        now_5am = datetime.utcnow().replace(hour=5, minute=0)
        shown = [w.title for w in _get_upcoming_workouts(user, now_5am)]
        self.assertNotIn("Yesterday Late", shown)
        self.assertIn("Tomorrow", shown)

        # Same-evening grace: tonight's slot already passed, still listable.
        now_2330 = datetime.utcnow().replace(hour=23, minute=30)
        shown = [w.title for w in _get_upcoming_workouts(user, now_2330)]
        self.assertIn("Tonight", shown)


if __name__ == "__main__":
    unittest.main()

    def test_card_shows_edit_and_done_but_no_standalone_cancel(self):
        self._make_session()
        page = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("EDIT", page)
        self.assertIn("MARK DONE", page)
        latest = db.session.scalar(select(func.max(ScheduledWorkout.id)))
        self.assertIn(f"/workouts/{latest}/edit", page)
        # CANCEL is no longer a card-level button; removal lives inside the
        # edit panel as "Remove session".
        self.assertNotIn(">CANCEL<", page)
        self.assertIn("Remove session", page)


if __name__ == "__main__":
    unittest.main()
