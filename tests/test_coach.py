"""Coach integration: food-DB matching, challenge intents, join actions,
and the telemetry trail (provider, latency, errors)."""
import re
import unittest
from datetime import datetime

from tests.base import AppContextTestCase, make_user

from app.extensions import db
from app.models import CoachMessage, User, UserChallenge
from sqlalchemy import select


def _csrf(client, path):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html).group(1)


class CoachTestCase(AppContextTestCase):
    def setUp(self):
        super().setUp()
        from app.challenges import seed_challenge_templates
        from app.models import Challenge, Food

        db.session.add(Food(name="Roti", serving="1 medium (40 g)",
                            calories=100, protein=3, carbs=20, fats=1))
        db.session.add(Food(name="Dal, cooked", serving="100 g",
                            calories=116, protein=9, carbs=20, fats=1))
        db.session.add(Challenge(title="30-Day Streak", type="streak",
                                 metric="workout_count", target_value=30,
                                 status="active", rolling=True))
        db.session.commit()
        seed_challenge_templates()
        self.joe_id = make_user(self.app, name="joe", email="joe@test.dev")

    def _user(self):
        return db.session.scalars(select(User).where(User.id == self.joe_id)).one()

    def test_food_db_matches_include_comma_heads(self):
        from app.dashboard import _food_db_matches

        matches = {m["name"]: m["calories"] for m in _food_db_matches("had two rotis with dal")}
        self.assertEqual(matches.get("Roti"), 100)
        self.assertEqual(matches.get("Dal, cooked"), 116)
        self.assertEqual(_food_db_matches("nothing relevant"), [])

    def test_rule_coach_join_updates_participations(self):
        from app.dashboard import process_coach_message

        outcome = process_coach_message(self._user(), "join 30 day streak", datetime.utcnow())
        db.session.commit()  # the route commits between requests; mirror that
        self.assertIn("Joined 30-Day Streak", outcome.reply)
        self.assertIsNotNone(db.session.scalar(select(UserChallenge.id)))
        outcome2 = process_coach_message(self._user(), "join 30 day streak", datetime.utcnow())
        self.assertIn("already in", outcome2.reply)

    def test_rules_assistant_rows_are_stamped(self):
        from app.dashboard import process_coach_message

        process_coach_message(self._user(), "what challenges are there?", datetime.utcnow())
        assistant = db.session.scalars(select(CoachMessage).where(
            CoachMessage.role == "assistant")).all()
        self.assertTrue(assistant)
        self.assertTrue(all(m.provider == "rules" for m in assistant))

    def test_fallback_leaves_telemetry_and_hides_errors(self):
        from app import dashboard as dash_mod
        from app.dashboard import process_coach_message

        self.app.config["GEMINI_API_KEY"] = "fake"
        self.app.config["GROQ_API_KEY"] = "fake"
        dash_mod._gemini_generate = self._boom
        dash_mod._groq_generate = self._boom

        process_coach_message(self._user(), "how am i doing", datetime.utcnow())
        roles = {r.role for r in db.session.scalars(select(CoachMessage)).all()}
        self.assertIn("error", roles)
        errors = db.session.scalars(select(CoachMessage).where(CoachMessage.role == "error")).all()
        self.assertEqual({e.provider for e in errors}, {"gemini", "groq"})
        self.assertTrue(all(e.had_error for e in errors))

    def _boom(self, *a, **k):
        from app.dashboard import CoachAIError

        raise CoachAIError("simulated outage")

    def test_apply_ai_actions_join(self):
        from app.dashboard import _apply_ai_actions

        applied, _ = _apply_ai_actions(
            self._user(), [{"type": "join_challenge", "challenge_title": "30-Day Streak"}],
            datetime.utcnow())
        self.assertEqual(applied, ["joined 30-Day Streak"])
        applied2, _ = _apply_ai_actions(
            self._user(), [{"type": "join_challenge", "challenge_title": "30-Day Streak"}],
            datetime.utcnow())
        self.assertEqual(applied2, ["already in 30-Day Streak"])
        self.assertIsNotNone(db.session.scalar(select(UserChallenge.id)))


if __name__ == "__main__":
    unittest.main()
