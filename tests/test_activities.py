"""Activity catalogue: matching, MET math, and the coach rules path that
estimates calories_burned from it."""
import unittest
from datetime import datetime

from tests.base import AppContextTestCase, make_user

from app.extensions import db
from app.models import ScheduledWorkout, User
from sqlalchemy import select


class ActivityMatchingTestCase(unittest.TestCase):
    def test_word_aliases_match_catalogue(self):
        from app.activities import match_activity

        self.assertEqual(match_activity("did a 45 min run")["name"], "Jogging")
        self.assertEqual(match_activity("30 minutes of swimming laps")["name"], "Swimming Laps")
        self.assertEqual(match_activity("played football")["name"], "Football (Soccer)")
        self.assertEqual(match_activity("hiit session")["name"],
                         "High-Intensity Interval Training (HIIT)")

    def test_longest_match_wins(self):
        from app.activities import match_activity

        self.assertEqual(match_activity("90 minutes of running intervals")["name"],
                         "Running Intervals")

    def test_gym_titles_do_not_match(self):
        from app.activities import match_activity

        self.assertIsNone(match_activity("leg day at the gym"))
        self.assertIsNone(match_activity(""))
        self.assertIsNone(match_activity(None))

    def test_estimate_calories_met_math(self):
        from app.activities import estimate_calories

        # MET x weight x hours: 7 x 75 x 0.75 = 394 (rounded)
        self.assertEqual(estimate_calories(7.0, 45, 75), 394)
        self.assertIsNone(estimate_calories(7.0, 45, None))
        self.assertIsNone(estimate_calories(7.0, 0, 75))

    def test_estimate_for_message_parses_duration(self):
        from app.activities import estimate_for_message

        est = estimate_for_message("did a 45 min run", 75)
        self.assertEqual(est["minutes"], 45)
        self.assertEqual(est["calories"], 394)
        hour = estimate_for_message("played football for 1 hour", 75)
        self.assertEqual(hour["minutes"], 60)


class ActivityCoachRulesTestCase(AppContextTestCase):
    def setUp(self):
        super().setUp()
        # Force the rules fallback: on dev machines TestConfig inherits the
        # .env API keys and the AI providers would otherwise serve these
        # messages (provider choice must not change rules-path assertions).
        self.app.config["GEMINI_API_KEY"] = None
        self.app.config["GROQ_API_KEY"] = None
        self.joe_id = make_user(self.app, name="joe", email="joe@test.dev", weight=75)

    def _user(self):
        return db.session.scalars(select(User).where(User.id == self.joe_id)).one()

    def test_rules_workout_log_estimates_calories(self):
        from app.dashboard import process_coach_message

        outcome = process_coach_message(
            self._user(), "just finished a 30 minute run", datetime.utcnow())
        db.session.commit()
        workout = db.session.scalar(select(ScheduledWorkout))
        self.assertEqual(workout.status, "completed")
        self.assertEqual(workout.title, "Run")
        self.assertEqual(workout.duration_minutes, 30)
        # 7 MET x 75 kg x 0.5 h = 262.5 -> 262
        self.assertEqual(workout.calories_burned, 262)
        self.assertIn("262 kcal", outcome.reply)

    def test_rules_schedule_prefills_estimate_on_card(self):
        from app.dashboard import process_coach_message

        process_coach_message(
            self._user(), "schedule swimming tomorrow at 7 am for 45 min", datetime.utcnow())
        db.session.commit()
        workout = db.session.scalar(select(ScheduledWorkout))
        self.assertEqual(workout.status, "scheduled")
        self.assertEqual(workout.duration_minutes, 45)
        # 7 MET x 75 kg x 0.75 h = 394 kcal
        self.assertEqual(workout.calories_burned, 394)

    def test_estimate_skipped_without_weight(self):
        from app.dashboard import process_coach_message

        user = self._user()
        user.weight = None
        process_coach_message(user, "just finished a 30 minute run", datetime.utcnow())
        db.session.commit()
        workout = db.session.scalar(select(ScheduledWorkout))
        self.assertIsNone(workout.calories_burned)


if __name__ == "__main__":
    unittest.main()
