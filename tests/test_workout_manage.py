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

    def test_edit_updates_title_time_and_duration(self):
        workout = self._make_session()
        new_day = (datetime.utcnow() + timedelta(days=3)).strftime("%Y-%m-%d")
        r = self._post(f"/workouts/{workout.id}/edit", {
            "title": "Pull Day", "date": new_day, "time": "07:30", "duration": "45",
        })
        self.assertEqual(r.status_code, 302)
        updated = db.session.get(ScheduledWorkout, workout.id)
        self.assertEqual(updated.title, "Pull Day")
        self.assertEqual(updated.duration_minutes, 45)
        self.assertEqual(updated.scheduled_for.strftime("%H:%M"), "07:30")

    def test_edit_to_past_is_rejected(self):
        workout = self._make_session()
        past = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        self._post(f"/workouts/{workout.id}/edit", {
            "title": "Push Day", "date": past, "time": "07:30", "duration": "60",
        })
        updated = db.session.get(ScheduledWorkout, workout.id)
        self.assertEqual(updated.scheduled_for, self.future)

    def test_edit_invalid_date_is_rejected(self):
        workout = self._make_session()
        self._post(f"/workouts/{workout.id}/edit", {
            "title": "Push Day", "date": "not-a-date", "time": "07:30", "duration": "60",
        })
        updated = db.session.get(ScheduledWorkout, workout.id)
        self.assertEqual(updated.scheduled_for, self.future)

    def test_edit_someone_elses_session_is_rejected(self):
        workout = self._make_session(user=self._user("mallory@test.dev"))
        new_day = (datetime.utcnow() + timedelta(days=3)).strftime("%Y-%m-%d")
        self._post(f"/workouts/{workout.id}/edit", {
            "title": "Hacked", "date": new_day, "time": "07:30", "duration": "60",
        })
        updated = db.session.get(ScheduledWorkout, workout.id)
        self.assertEqual(updated.title, "Push Day")

    def test_edit_completed_is_rejected(self):
        workout = self._make_session(status="completed")
        new_day = (datetime.utcnow() + timedelta(days=3)).strftime("%Y-%m-%d")
        self._post(f"/workouts/{workout.id}/edit", {
            "title": "Rewrite history", "date": new_day, "time": "07:30", "duration": "60",
        })
        updated = db.session.get(ScheduledWorkout, workout.id)
        self.assertEqual(updated.title, "Push Day")

    # --- card UI ----------------------------------------------------------

    def test_card_shows_edit_and_cancel_actions(self):
        self._make_session()
        page = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("EDIT", page)
        self.assertIn("CANCEL", page)
        self.assertIn(f"/workouts/{db.session.scalar(select(func.max(ScheduledWorkout.id)))}/edit", page)


if __name__ == "__main__":
    unittest.main()
