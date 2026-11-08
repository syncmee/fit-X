"""Coach integration: food-DB matching, challenge intents, join actions,
and the telemetry trail (provider, latency, errors)."""
import re
import unittest
from datetime import datetime

from tests.base import AppContextTestCase, login, make_user

from app.extensions import db
from app.models import CoachMessage, MealEntry, ScheduledWorkout, User, UserChallenge, WaterLog, WeightLog
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
        from app.foods import food_db_matches

        matches = {m["name"]: m["calories"] for m in food_db_matches("had two rotis with dal")}
        self.assertEqual(matches.get("Roti"), 100)
        self.assertEqual(matches.get("Dal, cooked"), 116)
        self.assertEqual(food_db_matches("nothing relevant"), [])

    def test_food_db_matches_explicit_aliases(self):
        from app.models import Food
        from app.foods import food_db_matches

        db.session.add(Food(name="Whole wheat roti", aliases='["chapati", "phulka"]', serving="1 medium", calories=110))
        db.session.commit()
        self.assertEqual(food_db_matches("two chapatis")[0]["name"], "Whole wheat roti")

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
        from app.coach import providers as providers_mod
        from app.dashboard import process_coach_message

        self.app.config["GEMINI_API_KEY"] = "fake"
        self.app.config["GROQ_API_KEY"] = "fake"
        providers_mod.gemini_generate = self._boom
        providers_mod.groq_generate = self._boom

        process_coach_message(self._user(), "how am i doing", datetime.utcnow())
        roles = {r.role for r in db.session.scalars(select(CoachMessage)).all()}
        self.assertIn("error", roles)
        errors = db.session.scalars(select(CoachMessage).where(CoachMessage.role == "error")).all()
        self.assertEqual({e.provider for e in errors}, {"gemini", "groq"})
        self.assertTrue(all(e.had_error for e in errors))

    def _boom(self, *a, **k):
        from app.coach import CoachAIError

        raise CoachAIError("simulated outage")

    def test_apply_ai_actions_join(self):
        from app.coach.actions import apply_actions as _apply_ai_actions

        applied, _ = _apply_ai_actions(
            self._user(), [{"type": "join_challenge", "challenge_title": "30-Day Streak"}],
            datetime.utcnow())
        self.assertEqual(applied, ["joined 30-Day Streak"])
        applied2, _ = _apply_ai_actions(
            self._user(), [{"type": "join_challenge", "challenge_title": "30-Day Streak"}],
            datetime.utcnow())
        self.assertEqual(applied2, ["already in 30-Day Streak"])
        self.assertIsNotNone(db.session.scalar(select(UserChallenge.id)))

    def test_apply_ai_actions_logs_weight_meal_water_and_workout(self):
        """The structured-action boundary accepts each supported log branch."""
        from app.coach.actions import apply_actions as _apply_ai_actions

        applied, _ = _apply_ai_actions(self._user(), [
            {"type": "log_weight", "weight_kg": 72.4},
            # An unmatched food: the model's own estimate is used (matched
            # foods are computed from the catalogue — see test_food_limits).
            {"type": "log_meal", "name": "Chicken momos", "meal_type": "lunch", "calories": 430,
             "protein": 15, "carbs": 72, "fats": 8},
            {"type": "log_water", "amount_ml": 500},
            {"type": "complete_workout", "title": "Run", "duration_minutes": 30},
        ], datetime.utcnow())
        db.session.flush()
        self.assertEqual(len(applied), 4)
        self.assertEqual(db.session.scalar(select(WeightLog.weight)), 72.4)
        self.assertEqual(db.session.scalar(select(MealEntry.calories)), 430)
        self.assertEqual(db.session.scalar(select(WaterLog.amount_ml)), 500)
        self.assertEqual(db.session.scalar(select(ScheduledWorkout.status)), "completed")


class CoachRouteJsonTestCase(AppContextTestCase):
    """POST /coach/message speaks JSON to the chat's fetch flow and keeps the
    classic flash + redirect for plain form posts."""

    def setUp(self):
        super().setUp()
        make_user(self.app, name="joe", email="joe@test.dev")
        self.client = self.app.test_client()
        login(self.client, self.app, "joe@test.dev")
        # Deterministic dispatch: no AI keys, catalogue seeded so the
        # "log N eggs" fast path answers.
        self.app.config["GEMINI_API_KEY"] = None
        self.app.config["GROQ_API_KEY"] = None
        from app.admin import seed_starter_foods
        seed_starter_foods()

    def _csrf(self):
        html = self.client.get("/dashboard").get_data(as_text=True)
        return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html).group(1)

    def _post(self, message, key=None, json_header=True):
        data = {"message": message, "csrf_token": self._csrf()}
        if key:
            data["idempotency_key"] = key
        headers = {"X-Requested-With": "fetch"} if json_header else {}
        return self.client.post("/coach/message", data=data, headers=headers)

    def test_fetch_valid_message_returns_json_outcome(self):
        resp = self._post("log 3 eggs", key="json-valid")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["reply"])
        self.assertEqual(body["action"], "meal_logged")

    def test_without_header_still_redirects(self):
        resp = self._post("log 3 eggs", key="form-post", json_header=False)
        self.assertEqual(resp.status_code, 302)

    def test_fetch_empty_message_returns_400_json(self):
        resp = self._post("", key="json-empty")
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertFalse(body["ok"])
        self.assertTrue(body["error"])

    def test_fetch_duplicate_idempotency_returns_409_json(self):
        first = self._post("log 1 egg", key="dup-key")
        self.assertEqual(first.status_code, 200)
        dup = self._post("log 1 egg", key="dup-key")
        self.assertEqual(dup.status_code, 409)
        body = dup.get_json()
        self.assertFalse(body["ok"])
        self.assertIn("already processed", body["error"])


if __name__ == "__main__":
    unittest.main()
