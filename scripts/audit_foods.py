"""Flag suspicious rows in the food nutrition CSV — no corrections.

Rules (per GOAL):
  - macro imbalance: |4P + 4C + 9F - kcal| / kcal > 0.35
  - energy density: kcal > 600 per 100 g

Writes audit_foods.csv (repo root) with the offending rows and the reason(s).
Run:  python -I scripts/audit_foods.py
"""

import csv
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "app" / "data" / "indian_food_nutrition_processed.csv"
OUT = REPO / "audit_foods.csv"

MACRO_IMBALANCE = 0.35
KCAL_DENSITY_CAP = 600.0


def _num(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def main() -> int:
    flagged: list[dict] = []
    total = 0
    with open(SRC, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            total += 1
            name = (row.get("Dish Name") or "").strip()
            kcal = _num(row, "Calories (kcal)")
            protein = _num(row, "Protein (g)")
            carbs = _num(row, "Carbohydrates (g)")
            fats = _num(row, "Fats (g)")

            reasons = []
            macro_kcal = 4 * protein + 4 * carbs + 9 * fats
            if kcal > 0:
                drift = abs(macro_kcal - kcal) / kcal
                if drift > MACRO_IMBALANCE:
                    reasons.append(f"macro_drift={drift:.0%} (4P+4C+9F={macro_kcal:.0f} vs kcal={kcal:.0f})")
            if kcal > KCAL_DENSITY_CAP:
                reasons.append(f"kcal_density={kcal:.0f}/100g > {KCAL_DENSITY_CAP:.0f}")

            if reasons:
                flagged.append({
                    "Dish Name": name,
                    "Calories (kcal)": kcal,
                    "Protein (g)": protein,
                    "Carbohydrates (g)": carbs,
                    "Fats (g)": fats,
                    "computed_4P4C9F": round(macro_kcal, 1),
                    "reasons": "; ".join(reasons),
                })

    with open(OUT, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(flagged[0].keys()) if flagged else
                                ["Dish Name", "Calories (kcal)", "Protein (g)",
                                 "Carbohydrates (g)", "Fats (g)", "computed_4P4C9F", "reasons"])
        writer.writeheader()
        writer.writerows(flagged)

    macro_only = sum(1 for r in flagged if r["reasons"].startswith("macro"))
    density = sum(1 for r in flagged if "kcal_density" in r["reasons"])
    both = sum(1 for r in flagged if "macro_drift" in r["reasons"] and "kcal_density" in r["reasons"])
    print(f"scanned {total} rows -> flagged {len(flagged)} "
          f"(macro drift: {macro_only}, density: {density}, both: {both})")
    print(f"wrote {OUT}")
    for r in flagged[:15]:
        print(f"  {r['Dish Name']}: {r['reasons']}")
    if len(flagged) > 15:
        print(f"  ... and {len(flagged) - 15} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
