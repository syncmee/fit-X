"""Generate app/data/indian_food_units.json: per-unit gram weights for the
Indian nutrition CSV dishes, derived from name patterns so 1,000 rows don't
need hand-editing.

Scope (per the portion-patch GOAL): roti/paratha/puri/idli/dosa (and close
relatives) per piece, biryani/pulao/khichdi per plate. Snack mixes that
merely contain the pattern word (bhel puri) are excluded.

Run:  python -I scripts/generate_indian_food_units.py
"""

import csv
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "app" / "data" / "indian_food_nutrition_processed.csv"
OUT = REPO / "app" / "data" / "indian_food_units.json"

# (pattern, exclusions, unit_grams) — first match wins, so most specific first.
PATTERNS = [
    (r"\bbhel\b.*\bpuri\b|\bsev puri\b|\bpani puri\b", None, None),  # snack mixes, not fried puri
    (r"\bpapad\b", None, None),
    (r"\bparatha\b|\bparantha\b", None, {"paratha": 80, "piece": 80}),
    (r"\broti\b|\bchapati\b|\bphulka\b", None, {"roti": 40, "chapati": 40, "piece": 40}),
    (r"\bpuri\b", None, {"puri": 35, "piece": 35}),
    (r"\bnaan\b|\bkulcha\b", None, {"naan": 90, "piece": 90}),
    (r"\bidli\b", None, {"idli": 45, "piece": 45}),
    (r"\bdosa\b|\bdose\b", None, {"dosa": 85, "piece": 85}),
    (r"\buttapam\b", None, {"uttapam": 110, "piece": 110}),
    (r"\bvada\b|\bwada\b", None, {"vada": 35, "piece": 35}),
    (r"\bbiryani\b|\bbiriyani\b|\bpulao\b|\bpilaf\b", None,
     {"plate": 280, "bowl": 250, "katori": 150}),
    (r"\bkhichdi\b|\bkhichri\b|\bkhitchdi\b", None,
     {"plate": 280, "bowl": 250, "katori": 150}),
]


def main() -> int:
    units: dict[str, dict] = {}
    with open(SRC, encoding="utf-8", newline="") as f:
        names = [row["Dish Name"].strip() for row in csv.DictReader(f)]
    for name in names:
        lowered = name.lower()
        for pattern, _exclusions, grams in PATTERNS:
            if re.search(pattern, lowered):
                if grams:
                    units[name] = grams
                break
    OUT.write_text(json.dumps(units, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    by_group: dict[str, int] = {}
    for grams in units.values():
        key = grams["piece"] if "piece" in grams else "plate"
        by_group[key] = by_group.get(key, 0) + 1
    print(f"{len(names)} CSV dishes -> {len(units)} got default unit weights")
    for key, count in sorted(by_group.items(), key=lambda kv: -kv[1]):
        print(f"  {key} g: {count} dishes")
    for name in list(units)[:8]:
        print(f"  e.g. {name}: {units[name]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
