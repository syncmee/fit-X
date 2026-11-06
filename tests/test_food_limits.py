"""Phase B protections: AI meal-action sanity limits (per-unit ceilings,
calorie cap, catalogue-derived calories), daily idempotency-key cleanup,
and the chain-wide AI deadline."""
import unittest
from datetime import datetime, timedelta

from tests.base import AppContextTestCase, make_user

from app.extensions import db
from app.models import Food, IdempotencyKey, MealEntry
from sqlalchemy import select, func

NOW = datetime(2026, 11, 7, 12, 0, 0)


class FoodLimitsTestCase(AppContextTestCase):
    def setUp(self):
        super().setUp()
        db.session.add(Food(name="Egg", serving="1 egg (50 g)",
                            calories=78, protein=6, carbs=1, fats=5))
        db.session.add(Food(name="Roti", serving="1 medium (40 g)",
                            calories=100, protein=3, carbs=20, fats=1))
        db.session.add(Food(name="Dal, cooked", serving="100 g",
                            calories=116, protein=9, carbs=20, fats=1))
        db.session.commit()
        self.joe_id = make_user(self.app, name="joe", email="joe@test.dev")

    def _user(self):
        from app.models import User

        return db.session.scalars(select(User).where(User.id == self.joe_id)).one()

    def _apply(self, actions):
        from app.coach.actions import apply_actions as _apply_ai_actions

        return _apply_ai_actions(self._user(), actions, NOW)

    # --- the regression the whole phase exists for -----------------------

    def test_two_eggs_became_20_is_not_logged(self):
        """The model misreads "2 eggs" as 20: nothing is persisted and the
        applied record asks for confirmation instead of clamping."""
        applied, _ = self._apply([
            {"type": "log_meal", "name": "eggs", "quantity": 20, "unit": "pieces",
             "calories": 1560, "protein": 120, "carbs": 20, "fats": 100},
        ])
        db.session.flush()
        self.assertEqual(db.session.scalar(select(func.count(MealEntry.id))), 0)
        self.assertEqual(len(applied), 1)
        self.assertTrue(applied[0].startswith("needs confirmation: "), applied)
        self.assertIn("20", applied[0])
        self.assertIn("eggs", applied[0])

    def test_confirmation_via_reply_flow(self):
        """process_coach_message_ai moves the guard question into the reply."""
        from app.coach import providers as providers_mod
        from app.dashboard import process_coach_message_ai

        def fake_provider(user, message, now, deadline=None):
            return {"reply": "Okay!", "actions": [
                {"type": "log_meal", "name": "eggs", "quantity": 20, "unit": "pieces",
                 "calories": 1560, "protein": 0, "carbs": 0, "fats": 0},
            ], "usage": {}}

        providers_mod.gemini_generate = fake_provider
        self.app.config["GEMINI_API_KEY"] = "fake"
        outcome = process_coach_message_ai(self._user(), "had 20 eggs", NOW)
        db.session.flush()
        self.assertIn("did not log 20 × eggs", outcome.reply)
        self.assertEqual(db.session.scalar(select(func.count(MealEntry.id))), 0)

    def test_confirmed_quantity_within_hard_limit_is_logged(self):
        applied, _ = self._apply([
            {"type": "log_meal", "name": "eggs", "quantity": 20, "unit": "pieces",
             "calories": 1560, "confirmed": True},
        ])
        db.session.flush()
        self.assertEqual(applied, ["eggs (1560 kcal)"])
        self.assertEqual(db.session.scalar(select(MealEntry.calories)), 1560)

    def test_confirmed_quantity_beyond_triple_ceiling_is_rejected(self):
        applied, _ = self._apply([
            {"type": "log_meal", "name": "eggs", "quantity": 100, "unit": "pieces",
             "calories": 7800, "confirmed": True},
        ])
        db.session.flush()
        self.assertEqual(db.session.scalar(select(func.count(MealEntry.id))), 0)

    # --- catalogue-derived numbers ---------------------------------------

    def test_calories_computed_from_catalogue_override_model(self):
        """2 rotis: the DB per-serving value doubles; the model's own number
        (123) must not win."""
        applied, _ = self._apply([
            {"type": "log_meal", "name": "roti", "quantity": 2, "unit": "pieces",
             "calories": 123, "protein": 10, "carbs": 10, "fats": 10},
        ])
        db.session.flush()
        self.assertEqual(applied, ["roti (200 kcal)"])
        entry = db.session.scalars(select(MealEntry)).one()
        self.assertEqual(entry.calories, 200)
        self.assertEqual(entry.protein, 6)

    def test_mass_unit_scales_by_serving_grams(self):
        applied, _ = self._apply([
            {"type": "log_meal", "name": "dal", "quantity": 200, "unit": "grams",
             "calories": 999},
        ])
        db.session.flush()
        self.assertEqual(db.session.scalar(select(MealEntry.calories)), 232)

    def test_kg_unit_converted(self):
        applied, _ = self._apply([
            {"type": "log_meal", "name": "dal", "quantity": 0.2, "unit": "kg",
             "calories": 999},
        ])
        db.session.flush()
        self.assertEqual(db.session.scalar(select(MealEntry.calories)), 232)

    def test_unmatched_food_falls_back_to_model_calories(self):
        applied, _ = self._apply([
            {"type": "log_meal", "name": "chicken momos", "quantity": 7,
             "unit": "pieces", "calories": 380, "protein": 21, "carbs": 42, "fats": 14},
        ])
        db.session.flush()
        self.assertEqual(db.session.scalar(select(MealEntry.calories)), 380)

    # --- per-entry calorie cap -------------------------------------------

    def test_calorie_cap_asks_for_confirmation(self):
        applied, _ = self._apply([
            {"type": "log_meal", "name": "birthday cake slice", "quantity": 5,
             "unit": "slices", "calories": 2000},
        ])
        db.session.flush()
        self.assertEqual(db.session.scalar(select(func.count(MealEntry.id))), 0)
        self.assertTrue(applied[0].startswith("needs confirmation: "), applied)

    def test_confirmed_over_cap_but_under_hard_cap_is_logged(self):
        applied, _ = self._apply([
            {"type": "log_meal", "name": "birthday cake slice", "quantity": 5,
             "unit": "slices", "calories": 2000, "confirmed": True},
        ])
        db.session.flush()
        self.assertEqual(db.session.scalar(select(MealEntry.calories)), 2000)

    def test_hard_cap_rejects_even_confirmed(self):
        applied, _ = self._apply([
            {"type": "log_meal", "name": "birthday cake slice", "quantity": 50,
             "unit": "slices", "calories": 9000, "confirmed": True},
        ])
        db.session.flush()
        self.assertEqual(db.session.scalar(select(func.count(MealEntry.id))), 0)


class FoodsHelperTestCase(AppContextTestCase):
    def test_unit_ceiling_lookup(self):
        from app.foods import unit_ceiling_for

        self.assertEqual(unit_ceiling_for("eggs"), 10)
        self.assertEqual(unit_ceiling_for("3 boiled eggs"), 10)
        self.assertEqual(unit_ceiling_for("chapatis"), 12)
        self.assertIsNone(unit_ceiling_for("eggplant"))
        self.assertIsNone(unit_ceiling_for("chicken curry"))

    def test_compute_meal_from_catalogue_variants(self):
        from app.foods import compute_meal_from_catalogue

        food = {"name": "Rice", "serving": "100 g cooked", "calories": 130,
                "protein_g": 3, "carbs_g": 28, "fats_g": 0}
        self.assertEqual(compute_meal_from_catalogue(food, 200, "grams")["calories"], 260)
        self.assertEqual(compute_meal_from_catalogue(food, 0.2, "kg")["calories"], 260)
        self.assertEqual(compute_meal_from_catalogue(food, 2, "bowls")["calories"], 260)
        self.assertEqual(compute_meal_from_catalogue(food, 300, "grams")["calories"], 390)
        # A portion beyond ~50 servings is not derivable as a real meal.
        self.assertIsNone(compute_meal_from_catalogue(food, 6000, "grams"))
        self.assertIsNone(compute_meal_from_catalogue(None, 2, "pieces"))
        self.assertIsNone(compute_meal_from_catalogue(food, 0, "pieces"))


class IdempotencyCleanupTestCase(AppContextTestCase):
    def test_cleanup_deletes_only_old_keys(self):
        from datetime import datetime as dt

        from app import cron as cron_mod

        # Reset the per-day gate so this test's run is not skipped.
        cron_mod._idempotency_cleanup_last_date = None
        self.addCleanup(setattr, cron_mod, "_idempotency_cleanup_last_date", None)

        self.joe_id = make_user(self.app, name="joe", email="joe@test.dev")
        now = dt.utcnow()
        db.session.add(IdempotencyKey(user_id=self.joe_id, key="old",
                                      created_at=now - timedelta(days=8)))
        db.session.add(IdempotencyKey(user_id=self.joe_id, key="recent",
                                      created_at=now - timedelta(days=1)))
        db.session.commit()

        deleted = cron_mod.cleanup_old_idempotency_keys()

        self.assertEqual(deleted, 1)
        remaining = {k.key for k in db.session.scalars(select(IdempotencyKey)).all()}
        self.assertEqual(remaining, {"recent"})


class ProviderDeadlineTestCase(AppContextTestCase):
    def test_remaining_timeout_variants(self):
        from time import monotonic

        from app.coach.providers import remaining_provider_timeout

        self.assertEqual(remaining_provider_timeout(None, 12), 12)
        self.assertEqual(remaining_provider_timeout(monotonic() + 100, 12), 12)
        # Not an exact equality: monotonic() resolution differs per platform
        # (ns on Linux, ~15 ms on Windows), so the remaining budget is a hair
        # under the requested 5 s.
        bounded = remaining_provider_timeout(monotonic() + 5, 45)
        self.assertGreater(bounded, 4.0)
        self.assertLessEqual(bounded, 5.0)
        self.assertEqual(remaining_provider_timeout(monotonic() + 0.5, 45), 0.0)
        self.assertEqual(remaining_provider_timeout(monotonic() - 10, 45), 0.0)

    def test_deadline_check_raises_coach_ai_error(self):
        from time import monotonic

        from app.coach.providers import _deadline_check

        with self.assertRaises(Exception):
            _deadline_check(monotonic() - 5, "Gemini")
        _deadline_check(None, "Gemini")  # no deadline: no-op
        _deadline_check(monotonic() + 100, "Gemini")  # plenty left: no-op


if __name__ == "__main__":
    unittest.main()
