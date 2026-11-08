"""Food-matcher eval: top-1/top-5 accuracy over tests/food_eval.json against
the full catalogue, plus the < 2 ms per-message latency budget.

Prints a per-case table and the aggregate scores; fails below the GOAL
targets (top-1 >= 85 %, top-5 >= 95 %). Also scores a reference
implementation of the OLD substring matcher on the same catalogue so the
improvement is visible in the output.
"""
import json
import re
import time
import unittest
from pathlib import Path

from app.extensions import db
from sqlalchemy import select

from tests.base import AppContextTestCase

EVAL_PATH = Path(__file__).parent / "food_eval.json"

TOP1_TARGET = 0.85
TOP5_TARGET = 0.95
LATENCY_BUDGET_MS = 2.0


def _legacy_keys(name: str, aliases: list[str]) -> list[str]:
    """Key derivation of the OLD matcher (comma heads, brackets, slashes)."""
    lowered = " ".join(name.lower().split())
    keys = {lowered}
    head = re.split(r"[,(]", lowered)[0].strip()
    if head:
        keys.add(head)
    for side in re.split(r"[/()]", lowered):
        if side.strip():
            keys.add(side.strip())
    for match in re.findall(r"\(([^()]+)\)", lowered):
        if " " in match:
            keys.add(match.strip())
    keys.update(" ".join(str(a).lower().split()) for a in aliases)
    return [key for key in keys if len(key) >= 3]


def _legacy_matches(message: str, rows: list[dict], limit: int = 8) -> list[dict]:
    """The OLD food_db_matches: unbounded substring scan, longest key wins."""
    message = " ".join(message.lower().split())
    matched = []
    for food in rows:
        hit = max((len(key) for key in food["keys"] if key in message), default=0)
        if hit:
            matched.append((hit, food))
    matched.sort(key=lambda item: -item[0])
    return [food for _, food in matched[:limit]]


class FoodEvalTestCase(AppContextTestCase):
    def test_eval_scores_and_latency(self):
        from app.admin import (
            seed_food_aliases_from_json,
            seed_indian_foods_from_csv,
            seed_staple_foods_from_csv,
            seed_starter_foods,
        )
        seed_starter_foods()
        seed_staple_foods_from_csv()
        seed_indian_foods_from_csv()
        seed_food_aliases_from_json()

        from app.admin import apply_default_unit_grams, seed_food_data_fixes
        apply_default_unit_grams()
        seed_food_data_fixes()

        from app.foods import food_db_matches, try_fast_meal

        eval_data = json.loads(EVAL_PATH.read_text(encoding="utf-8"))
        cases = eval_data["cases"]

        # Reference rows for the legacy matcher (same catalogue, old keys).
        from app.models import Food
        rows = []
        for food in db.session.scalars(select(Food)).all():
            rows.append({"name": food.name,
                         "keys": _legacy_keys(food.name, json.loads(food.aliases or "[]"))})

        top1 = top5 = 0
        failures = []
        for case in cases:
            got = [m["name"] for m in food_db_matches(case["phrase"], limit=5)]
            legacy = [m["name"] for m in _legacy_matches(case["phrase"], rows, limit=5)]
            hit1 = bool(got) and got[0] == case["top1"]
            hit5 = any(name in got for name in case["top5"])
            top1 += hit1
            top5 += hit5
            if not (hit1 and hit5):
                failures.append((case["phrase"], case["top1"], got[:3], legacy[0] if legacy else None))

        # Warm latency over a realistic mixed batch.
        phrases = [c["phrase"] for c in cases]
        for _ in range(10):
            for phrase in phrases:
                food_db_matches(phrase)
        start = time.perf_counter()
        rounds = 20
        for _ in range(rounds):
            for phrase in phrases:
                food_db_matches(phrase)
        avg_ms = (time.perf_counter() - start) / (rounds * len(phrases)) * 1000

        total = len(cases)
        p1, p5 = top1 / total, top5 / total
        print(f"\nfood eval (tuned set): {total} cases")
        print(f"  top-1: {top1}/{total} = {p1:.0%} (target >= {TOP1_TARGET:.0%})")
        print(f"  top-5: {top5}/{total} = {p5:.0%} (target >= {TOP5_TARGET:.0%})")

        # Held-out phrases: authored separately, never tuned against.
        heldout = eval_data.get("heldout", [])
        h_top1 = h_top5 = 0
        h_failures = []
        for case in heldout:
            got = [m["name"] for m in food_db_matches(case["phrase"], limit=5)]
            hit1 = bool(got) and got[0] == case["top1"]
            hit5 = any(name in got for name in case["top5"])
            h_top1 += hit1
            h_top5 += hit5
            if not (hit1 and hit5):
                h_failures.append((case["phrase"], case["top1"], got[:3]))
        h_total = len(heldout)
        hp1 = h_top1 / h_total if h_total else 1.0
        hp5 = h_top5 / h_total if h_total else 1.0
        print(f"food eval (held-out): {h_total} cases")
        print(f"  top-1: {h_top1}/{h_total} = {hp1:.0%} (target >= {TOP1_TARGET:.0%})")
        print(f"  top-5: {h_top5}/{h_total} = {hp5:.0%} (target >= {TOP5_TARGET:.0%})")
        for phrase, want, got in h_failures:
            print(f"    held-out miss: {phrase!r} want {want!r} got {got}")

        # Portion cases: the fast path must fire, land inside the range and,
        # when the case names a food, match it.
        portions = eval_data.get("portion_cases", [])
        p_ok = 0
        p_failures = []
        for case in portions:
            result = try_fast_meal(case["phrase"])
            lo, hi = case["range"]
            in_range = result and lo <= result["calories"] <= hi
            food_ok = result and (not case.get("food") or result["name"] == case["food"])
            if in_range and food_ok:
                p_ok += 1
            else:
                got = f"{result['calories']} kcal ({result['name']})" if result else "no fast-path result"
                p_failures.append((case["phrase"], f"{case.get('food', 'any food')} {lo}-{hi}", got))
        p_total = len(portions)
        pp = p_ok / p_total if p_total else 1.0
        print(f"portion cases (fast path): {p_total}")
        print(f"  in range + right food: {p_ok}/{p_total} = {pp:.0%}")
        for phrase, want, got in p_failures:
            print(f"    portion miss: {phrase!r} want {want} got {got}")

        print(f"  latency: {avg_ms:.3f} ms/message (budget < {LATENCY_BUDGET_MS} ms)")
        legacy_top1 = sum(
            1 for c in cases
            if (l := _legacy_matches(c["phrase"], rows, limit=1)) and l[0]["name"] == c["top1"])
        legacy_top5 = sum(
            1 for c in cases
            if any(n in [m["name"] for m in _legacy_matches(c["phrase"], rows)]
                   for n in c["top5"]))
        print(f"  legacy matcher on the same catalogue: top-1 {legacy_top1 / total:.0%}, top-5 {legacy_top5 / total:.0%}")
        if failures:
            print("  failures (phrase | want top-1 | got | legacy top-1):")
            for phrase, want, got, legacy in failures:
                print(f"    {phrase!r} | {want!r} | {got} | {legacy!r}")

        self.assertGreaterEqual(p1, TOP1_TARGET, f"top-1 {p1:.0%} below target")
        self.assertGreaterEqual(p5, TOP5_TARGET, f"top-5 {p5:.0%} below target")
        self.assertGreaterEqual(hp1, TOP1_TARGET, f"held-out top-1 {hp1:.0%} below target")
        self.assertGreaterEqual(hp5, TOP5_TARGET, f"held-out top-5 {hp5:.0%} below target")
        self.assertEqual(p_failures, [], f"portion cases out of range: {p_failures}")
        self.assertLess(avg_ms, LATENCY_BUDGET_MS, "matcher over latency budget")


if __name__ == "__main__":
    unittest.main()
