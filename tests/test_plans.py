"""Plan builder against the Fit-X catalog: modality plans pull real sport
drills, the gym composer resolves across environments, and no line carries
an image (the catalog is image-free)."""
import unittest

from app.plans import (
    _GROUP_TABLE,
    _exercises,
    _resolve,
    build_plan,
)


class PlansTestCase(unittest.TestCase):
    def _lines(self, plan):
        return [line for block in plan["blocks"] for line in block["exercises"]]

    def test_catalog_loads_with_expected_fields(self):
        exs = _exercises()
        self.assertGreater(len(exs), 700)
        for e in exs:
            self.assertIn("equipment_group", e)
            self.assertNotIn("images", e)
            self.assertIsNotNone(e.get("met"))

    def test_boxing_plan_uses_dataset_drills(self):
        plan = build_plan("Boxing Session")
        names = {line["name"] for line in self._lines(plan)}
        self.assertIn("Shadow Boxing", names)
        self.assertIn("Heavy Bag Boxing", names)
        self.assertNotIn("Battling Ropes", names)  # the old generic template

    def test_running_plan_uses_dataset_drills(self):
        plan = build_plan("Morning Run")
        names = {line["name"] for line in self._lines(plan)}
        self.assertIn("Jogging", names)
        self.assertIn("Banded Hamstring Stretch", names)

    def test_word_boundary_title_matching(self):
        # "run" must not substring-match inside "crunch" (previously a
        # core session titled "Crunch Day" produced a Running plan).
        self.assertIsNone(build_plan("Crunch Day"))

    def test_gym_plan_resolves_for_all_environments(self):
        names = {e["name"] for e in _exercises()}
        for env in (None, "gym", "home", "home_gym"):
            for title in ("Full Body", "Push Day", "Pull Day", "Upper Body",
                          "Lower Body", "Legs", "Glutes", "Core"):
                plan = build_plan(title, env, generic_fallback=True)
                self.assertIsNotNone(plan, f"{title}/{env}")
                lines = self._lines(plan)
                self.assertTrue(lines, f"{title}/{env} produced no lines")
                for line in lines:
                    self.assertIn(line["name"], names, f"{title}/{env}: {line['name']}")
                    self.assertIn("met", line)
                    self.assertNotIn("image", line)

    def test_home_plan_is_bodyweight_only(self):
        from app.plans import _ENVIRONMENT_EQUIPMENT

        plan = build_plan("Push Day", "home")
        groups = {(e.get("equipment_group") or "bodyweight") for e in _exercises()
                  if e["name"] in {line["name"] for line in self._lines(plan)}}
        self.assertTrue(groups <= _ENVIRONMENT_EQUIPMENT["home"], groups)

    def test_home_pull_gets_vertical_pulling(self):
        plan = build_plan("Pull Workout", "home")
        names = {line["name"] for line in self._lines(plan)}
        self.assertIn("Pull-Up", names)
        self.assertNotIn("Lat Pulldown", names)  # gym-only, must not leak

    def test_punching_title_gets_boxing_plan(self):
        plan = build_plan("Punching Combinations & Defense")
        names = {line["name"] for line in self._lines(plan)}
        self.assertIn("Heavy Bag Boxing", names)

    def test_swimming_plan_uses_swim_drills(self):
        plan = build_plan("Swimming Laps")
        self.assertEqual(plan["name"], "Swimming")
        names = {line["name"] for line in self._lines(plan)}
        self.assertIn("Freestyle Swimming", names)
        self.assertIn("Kickboard Swim", names)
        self.assertNotIn("Bodyweight Squat", names)  # generic circuit must not leak

    def test_cycling_plan_uses_cycling_drills(self):
        plan = build_plan("Road Cycling")
        self.assertEqual(plan["name"], "Cycling")
        names = {line["name"] for line in self._lines(plan)}
        self.assertIn("Cycling Intervals", names)

    def test_hyphenated_full_body_uses_composer(self):
        plan = build_plan("Full-body workout")
        self.assertEqual(plan["name"], "Full Body")
        block_titles = {b["block"] for b in plan["blocks"]}
        self.assertIn("CHEST", block_titles)  # composer, not the generic circuit

    def test_cooldown_fallbacks_stay_stretching(self):
        # "Banded Lat Stretch" is band equipment; at home the fallback must
        # pick a stretch, never a strength move.
        plan = build_plan("Push Day", "home")
        for block in plan["blocks"]:
            if block["block"] == "Cool-down":
                for line in block["exercises"]:
                    entry = next(e for e in _exercises() if e["name"] == line["name"])
                    self.assertEqual(entry["category"], "stretching", line["name"])

    def test_group_table_candidates_resolve(self):
        for group, table in _GROUP_TABLE.items():
            for candidates, _dose in table["picks"]:
                self.assertIsNotNone(_resolve(*candidates), f"{group}: {candidates}")

    def test_lines_carry_alternates_from_variation_groups(self):
        plan = build_plan("Chest")
        alts = [line.get("alternate") for line in self._lines(plan) if line.get("source") != "coach"]
        self.assertTrue(any(alts), "expected at least one variationGroup alternate")


if __name__ == "__main__":
    unittest.main()
