"""The Indian food nutrition dataset and the coach's food matcher."""
import unittest
from datetime import datetime

from app.extensions import db
from app.models import Food
from sqlalchemy import func, select

from tests.base import AppContextTestCase, make_user


class FoodSeedTestCase(AppContextTestCase):
    def setUp(self):
        super().setUp()
        make_user(self.app, name="joe", email="joe@test.dev")

    def test_seed_indian_foods_is_bulk_and_idempotent(self):
        from app.admin import seed_indian_foods_from_csv

        added = seed_indian_foods_from_csv()
        self.assertGreater(added, 900)
        self.assertEqual(seed_indian_foods_from_csv(), 0)  # re-run adds nothing
        total = db.session.scalar(select(func.count(Food.id)))
        self.assertEqual(total, added)

    def test_seed_values_land_per_100g(self):
        from app.admin import seed_indian_foods_from_csv

        seed_indian_foods_from_csv()
        dosa = db.session.scalars(select(Food).where(Food.name == "Masala dosa")).one()
        self.assertEqual(dosa.calories, 165)
        self.assertEqual(dosa.serving, "100 g")
        self.assertEqual(dosa.protein, 3)

    def test_matcher_uses_dataset_aliases(self):
        from app.admin import seed_indian_foods_from_csv
        from app.dashboard import _food_db_matches

        seed_indian_foods_from_csv()
        names = {m["name"] for m in _food_db_matches("2 roti and masala dosa")}
        self.assertIn("Masala dosa", names)      # full-name hit
        self.assertTrue(names & {"Chapati/Roti", "Roti"})  # alias or starter hit

        chai = {m["name"] for m in _food_db_matches("a cup of garam chai")}
        self.assertIn("Hot tea (Garam Chai)", chai)  # parenthesised alias

        dal = {m["name"] for m in _food_db_matches("mixed dal and chawal")}
        self.assertIn("Mixed dal", dal)

    def test_matcher_results_are_most_specific_first(self):
        from app.admin import seed_indian_foods_from_csv
        from app.dashboard import _food_db_matches

        seed_indian_foods_from_csv()
        matches = _food_db_matches("plain dosa with sambar")
        self.assertEqual(matches[0]["name"], "Plain dosa")  # longer alias beats shorter


if __name__ == "__main__":
    unittest.main()
