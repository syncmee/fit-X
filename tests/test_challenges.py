"""The challenge engine: contributions, all four types, idempotency,
badges, windows, audience segments, and the daily maintenance job."""
import unittest
from datetime import datetime, timedelta

from tests.base import make_app

MIDDAY = datetime(2026, 10, 4, 12, 0)  # fixed wall clock, safely inside a fiT-X day


def _at(days_ago: int = 0) -> datetime:
    """A wall-clock timestamp `days_ago` days before the user's CURRENT
    fiT-X day (4:30 AM local shift), pinned to midday so it never straddles
    the day boundary."""
    from app.extensions import db
    from app.models import User
    from sqlalchemy import select
    from app.timeutil import effective_today

    user = db.session.scalars(select(User).where(User.email == "joe@test.dev")).one()
    day = effective_today(user) - timedelta(days=days_ago)
    return datetime.combine(day, datetime.min.time()).replace(hour=12)


class ChallengeEngineTestCase(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        from app.extensions import db
        from app.models import User

        with self.app.app_context():
            db.create_all()
            user = User(name="joe", email="joe@test.dev", onboarding=True, status="active",
                        last_active_at=datetime.utcnow())
            user.set_password("pw123456")
            db.session.add(user)
            db.session.commit()
            self.user_id = user.id
        self._ctx = self.app.app_context()
        self._ctx.push()

    def tearDown(self):
        from app.extensions import db

        db.session.remove()
        self._ctx.pop()

    def _user(self):
        from app.extensions import db
        from app.models import User
        from sqlalchemy import select

        return db.session.scalars(select(User).where(User.id == self.user_id)).one()

    def _challenge(self, title, ctype, metric, target, daily=None, badge=None, **kw):
        from app.extensions import db
        from app.models import Badge, Challenge, UserChallenge

        b = None
        if badge:
            b = Badge(name=badge, icon="🏅")
            db.session.add(b)
            db.session.flush()
        fields = dict(title=title, type=ctype, metric=metric, target_value=target,
                      daily_target=daily, status="active", rolling=True,
                      badge_id=b.id if b else None)
        fields.update(kw)
        c = Challenge(**fields)
        db.session.add(c)
        db.session.flush()
        # Joined five days ago, so backdated log events (yesterday etc.) fall
        # inside the rolling window.
        db.session.add(UserChallenge(user_id=self.user_id, challenge_id=c.id,
                                     joined_at=datetime.utcnow() - timedelta(days=5)))
        db.session.commit()
        return c

    def _participation(self, challenge):
        from app.extensions import db
        from app.models import UserChallenge
        from sqlalchemy import select

        return db.session.scalars(
            select(UserChallenge).where(UserChallenge.challenge_id == challenge.id)
        ).one()

    def _log(self, event):
        from app.challenges import update_challenges
        from app.extensions import db

        notes = update_challenges(self._user(), event)
        db.session.commit()
        return notes

    # --- contributions ---

    def test_distance_parsing(self):
        from app.challenges import workout_distance_km

        self.assertEqual(workout_distance_km("just did a 10k run"), 10.0)
        self.assertEqual(workout_distance_km("5.5 km trail"), 5.5)
        self.assertEqual(workout_distance_km("21km long run"), 21.0)
        self.assertIsNone(workout_distance_km("push day"))

    def test_steps_have_no_source(self):
        from app.challenges import _event_contribution

        self.assertEqual(_event_contribution({"kind": "workout", "title": "walk"}, "steps"), 0.0)

    # --- cumulative ---

    def test_cumulative_completes_and_awards_badge(self):
        c = self._challenge("Cum3", "cumulative", "workout_count", 3, badge="Cum Badge")
        for offset in (2, 1, 0):
            self._log({"kind": "workout", "at": _at(offset),
                       "title": "Push", "calories_burned": 200})
        uc = self._participation(c)
        self.assertEqual(uc.status, "completed")
        self.assertEqual(uc.progress_value, 3.0)

        from app.extensions import db
        from app.models import UserBadge
        from sqlalchemy import select

        self.assertEqual(db.session.scalar(select(UserBadge.badge_id).where(
            UserBadge.user_id == self.user_id)), c.badge_id)

    def test_streak_ignores_duplicate_same_day_logs(self):
        c = self._challenge("Streak", "streak", "workout_count", 5)
        self._log({"kind": "workout", "at": _at(0), "title": "A", "calories_burned": 100})
        self._log({"kind": "workout", "at": _at(0), "title": "B", "calories_burned": 100})
        self._log({"kind": "workout", "at": _at(1), "title": "C", "calories_burned": 100})
        uc = self._participation(c)
        self.assertEqual(uc.current_streak, 2)  # two days qualify, not three logs

    def test_count_counts_qualifying_days(self):
        c = self._challenge("Days", "count", "workout_count", 2)
        self._log({"kind": "workout", "at": _at(0), "title": "A", "calories_burned": 100})
        self._log({"kind": "workout", "at": _at(1), "title": "B", "calories_burned": 100})
        self._log({"kind": "workout", "at": _at(1), "title": "C", "calories_burned": 100})
        uc = self._participation(c)
        self.assertEqual(uc.status, "completed")  # 2 distinct days, 3 workouts

    def test_single_goal_uses_best_single_log(self):
        c = self._challenge("Run5", "single_goal", "distance_km", 5)
        self._log({"kind": "workout", "at": _at(0), "title": "3k jog", "calories_burned": 100})
        uc = self._participation(c)
        self.assertNotEqual(uc.status, "completed")
        self._log({"kind": "workout", "at": _at(0), "title": "10k run", "calories_burned": 500})
        uc = self._participation(c)
        self.assertEqual(uc.status, "completed")
        self.assertEqual(uc.progress_value, 10.0)

    def test_wrong_kind_event_is_ignored(self):
        c = self._challenge("Meals", "cumulative", "meals_logged", 1)
        notes = self._log({"kind": "weight", "at": _at(0)})
        self.assertEqual(notes, [])
        self.assertEqual(self._participation(c).progress_value, 0.0)

    # --- windows ---

    def test_closed_fixed_window_skips_events(self):
        from datetime import date

        c = self._challenge("Old", "cumulative", "workout_count", 1, rolling=False,
                            start_date=date(2026, 9, 1), end_date=date(2026, 9, 20))
        self._log({"kind": "workout", "at": _at(0), "title": "A", "calories_burned": 100})
        self.assertEqual(self._participation(c).progress_value, 0.0)

    # --- maintenance ---

    def test_maintenance_activates_ends_resets_and_fails(self):
        from app.challenges import run_daily_maintenance
        from app.extensions import db
        from app.models import Challenge, ChallengeDayLog, UserChallenge
        from app.timeutil import effective_today

        today = datetime.utcnow().date()
        sched = Challenge(title="Sched", type="cumulative", metric="workout_count",
                          target_value=2, status="scheduled", start_date=today - timedelta(days=1))
        expired = Challenge(title="Expired", type="cumulative", metric="workout_count",
                            target_value=2, status="active", rolling=False,
                            start_date=today - timedelta(days=10), end_date=today - timedelta(days=1))
        streak = Challenge(title="Streak", type="streak", metric="workout_count",
                           target_value=10, status="active", rolling=True)
        window = Challenge(title="Window", type="cumulative", metric="workout_count",
                           target_value=99, status="active", rolling=True, duration_days=7)
        db.session.add_all([sched, expired, streak, window])
        db.session.flush()
        db.session.add(UserChallenge(user_id=self.user_id, challenge_id=expired.id))
        uc_streak = UserChallenge(user_id=self.user_id, challenge_id=streak.id,
                                  current_streak=3, joined_at=datetime.utcnow() - timedelta(days=5))
        db.session.add(uc_streak)
        db.session.flush()  # id needed for the day log below
        db.session.add(ChallengeDayLog(user_challenge_id=uc_streak.id,
                                       date=effective_today(self._user()) - timedelta(days=3), value=1))
        db.session.add(UserChallenge(user_id=self.user_id, challenge_id=window.id,
                                     joined_at=datetime.utcnow() - timedelta(days=9)))
        db.session.commit()

        stats = run_daily_maintenance()
        db.session.expire_all()
        self.assertEqual(stats["activated"], 1)
        self.assertEqual(stats["ended"], 1)
        self.assertEqual(stats["streaks_reset"], 1)
        self.assertEqual(stats["windows_failed"], 1)
        self.assertEqual(db.session.get(Challenge, sched.id).status, "active")
        self.assertEqual(db.session.get(Challenge, expired.id).status, "ended")
        self.assertEqual(self._participation(streak).current_streak, 0)
        self.assertEqual(self._participation(streak).status, "active")  # reset, not failed
        self.assertEqual(self._participation(window).status, "failed")

    # --- audience segments ---

    def test_segment_visibility_gates_join(self):
        from app.challenges import challenge_visible_to
        from app.extensions import db
        from app.models import Challenge, UserChallenge
        from sqlalchemy import select

        from tests.base import login, post

        c = Challenge(title="Comeback", type="cumulative", metric="workout_count",
                      target_value=2, status="active", rolling=True,
                      audience="segment", audience_segment="inactive_7d")
        db.session.add(c)
        db.session.commit()
        cid = c.id

        # joe was active seconds ago -> not in inactive_7d
        with self.app.app_context():
            self.assertFalse(challenge_visible_to(c, self._user()))

        client = self.app.test_client()
        login(client, self.app, "joe@test.dev")
        r = post(client, f"/challenges/{cid}/join", {}, token_path="/dashboard")
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            self.assertEqual(
                db.session.scalar(select(UserChallenge.id).where(
                    UserChallenge.challenge_id == cid)), None)


if __name__ == "__main__":
    unittest.main()
