"""Generate app/data/food_data_fixes.json from audit_foods.csv — reviewed,
class-based corrections for the flagged rows. kcal/macros stay per 100 g.

Classes (pattern -> treatment):
  Soups/consommes   fat is inflated (9-17 g in clear soups); trust kcal,
                    cap protein, recompute fat from kcal.
  Fried snacks      pakora/poori/vada/... carry 65-90 g fat/100 g (real
                    15-30 g); cap fat by class, recompute kcal from macros.
  Chai              'Hot tea (Garam Chai)' is plain-tea values (16 kcal);
                    fix to milk tea (~55 kcal/100 g).
Everything else in the audit (oils, ghee, nuts, seeds, dry coconut) is
genuinely energy-dense and stays untouched.

Run:  python -I scripts/apply_audit_fixes.py
"""

import csv
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AUDIT = REPO / "audit_foods.csv"
OUT = REPO / "app" / "data" / "food_data_fixes.json"

# Deep-fried / rich classes: (pattern, fat cap g/100 g)
FAT_CAPS = [
    (r"\bmayonnaise\b|\bmayo\b", 76.0),
    (r"\bfrench dressing\b", 28.0),
    (r"\btartare\b", 46.0),
    (r"\bbanana chips\b", 30.0),
    (r"\bfried fish\b|\bfish cutlet\b|\bfish and chips\b", 15.0),
    (r"\bkebab\b|\bseekh\b|\bshammi\b|\bchops\b|\bscotch egg\b", 20.0),
    (r"\bcheese toast\b|\bcheese balls\b", 20.0),
    (r"\bbonda\b", 18.0),
    (r"\bkofta\b|\bdum aloo\b|\bcrispy okra\b|\bkathal\b|\blauki kofta\b", 16.0),
    (r"\bbhature\b|\bbhatura\b", 25.0),
    (r"\bpoori\b|\bpakora\b|\bpakoda\b|\bvada\b|\bkachori\b|\bsamosa\b"
     r"|\bmathri\b|\bnamak para\b|\bnamak paras\b|\bpapdi\b|\bsev\b"
     r"|\bcutlet\b|\btikki\b|\bcheela\b|\bchilla\b|\bmuthias\b|\bghujia\b"
     r"|\bgunjia\b|\blatika\b|\bspring roll\b|\bbaghar\b|\bbonda\b", 22.0),
]

SOUP_RE = r"\bsoup\b|\bconsomme\b|\bshorba\b"
SAUCE_RE = r"\bsauce\b|\bdressing\b|\btartare\b"
RAITA_RE = r"\braita\b"

# Manual fixes outside the audit's two rules (kcal consistent but wrong
# semantics, e.g. plain-tea values on a milk-tea entry).
MANUAL_UPDATES = {
    "Hot tea (Garam Chai)": {
        "calories": 55, "protein": 1.5, "carbs": 7.0, "fats": 1.8,
        "reason": "was plain-tea values; entry means milk tea",
    },
    # Spot-checked against reference values (USDA/IFCT-class): the CSV's
    # fried-class rows carry PER-PIECE macros mislabeled as per-100 g
    # (Poori carbs 8.2 g/100 g is impossible for fried dough). These four
    # are set to realistic per-100 g compositions; kcal recomputed.
    "Poori": {
        "calories": 392, "protein": 7.0, "carbs": 46.0, "fats": 20.0,
        "reason": "spot-check: CSV macros were per-piece; per-100 g set (piece 35 g ~ 137 kcal)",
    },
    "Bhatura": {
        "calories": 390, "protein": 6.0, "carbs": 42.0, "fats": 22.0,
        "reason": "spot-check: per-piece macros; per-100 g set",
    },
    "Banana chips (Kele ke chips)": {
        "calories": 469, "protein": 0.5, "carbs": 56.0, "fats": 27.0,
        "reason": "spot-check: banana is carb; CSV C was per-piece",
    },
    "Chicken kebab": {
        "calories": 206, "protein": 18.0, "carbs": 2.0, "fats": 14.0,
        "reason": "spot-check: chicken protein per-piece in CSV; per-100 g set",
    },
}


def is_soup(name: str) -> bool:
    import re
    return bool(re.search(SOUP_RE, name.lower()))


def is_cream_soup(name: str) -> bool:
    return "cream of" in name.lower()


def fat_cap(name: str) -> float | None:
    import re
    lowered = name.lower()
    for pattern, cap in FAT_CAPS:
        if re.search(pattern, lowered):
            return cap
    return None


def main() -> int:
    audit = list(csv.DictReader(open(AUDIT, encoding="utf-8")))
    updates: dict[str, dict] = {}
    kept: list[str] = []

    for row in audit:
        name = row["Dish Name"].strip()
        kcal = float(row["Calories (kcal)"])
        protein = float(row["Protein (g)"])
        carbs = float(row["Carbohydrates (g)"])
        fats = float(row["Fats (g)"])

        if name in MANUAL_UPDATES:
            updates[name] = MANUAL_UPDATES[name]
        elif re.search(RAITA_RE, name.lower()):
            # Yogurt dish: fried boondi on top does not make it 74 g fat.
            new_fat = min(fats, 10.0)
            updates[name] = {"calories": round(4 * protein + 4 * carbs + 9 * new_fat, 1),
                             "protein": round(protein, 2), "carbs": round(carbs, 2),
                             "fats": round(new_fat, 2),
                             "reason": "raita: fat capped at 10 g"}
        elif re.search(SAUCE_RE, name.lower()):
            # Sauces/dressings: trust kcal, cap protein and fat.
            protein = min(protein, 6.0)
            new_fat = max(0.5, min((kcal - 4 * protein - 4 * carbs) / 9.0, 8.0))
            new_kcal = round(4 * protein + 4 * carbs + 9 * new_fat, 1)
            updates[name] = {"calories": round(new_kcal), "protein": round(protein, 2),
                             "carbs": round(carbs, 2), "fats": round(new_fat, 2),
                             "reason": "sauce: protein/fat capped, kcal trusted"}
        elif is_soup(name):
            # Trust kcal (plausible), fix fat from kcal; broths stay light.
            cap = 8.0 if is_cream_soup(name) else 6.0
            protein = min(protein, cap)
            new_fat = max(0.5, min((kcal - 4 * protein - 4 * carbs) / 9.0, 12.0))
            new_kcal = round(4 * protein + 4 * carbs + 9 * new_fat, 1)
            updates[name] = {"calories": round(new_kcal), "protein": round(protein, 2),
                             "carbs": round(carbs, 2), "fats": round(new_fat, 2),
                             "reason": "soup: fat recalculated from kcal, protein capped"}
        elif name.lower() == "hot tea (garam chai)":
            # Milk tea, no sugar (sugar is logged separately).
            updates[name] = {"calories": 55, "protein": 1.5, "carbs": 7.0, "fats": 1.8,
                             "reason": "was plain-tea values; entry means milk tea"}
        else:
            cap = fat_cap(name)
            if cap is None:
                # Not a recognized class (e.g. spaghetti bolognese, black
                # beans, brown sauce, pickle): macros are internally
                # consistent while kcal is off — trust the macros.
                new_kcal = round(4 * protein + 4 * carbs + 9 * fats, 1)
                if new_kcal < 5:
                    kept.append(name)
                    continue
                updates[name] = {"calories": round(new_kcal), "protein": round(protein, 2),
                                 "carbs": round(carbs, 2), "fats": round(fats, 2),
                                 "reason": "kcal recomputed from macros"}
            elif fats <= cap:
                kept.append(name)
                continue
            else:
                new_fat = cap
                new_kcal = round(4 * protein + 4 * carbs + 9 * new_fat, 1)
                updates[name] = {"calories": round(new_kcal), "protein": round(protein, 2),
                                 "carbs": round(carbs, 2), "fats": round(new_fat, 2),
                                 "reason": f"fried-class fat capped at {cap:g} g"}

    # Manual fixes apply regardless of audit membership.
    for name, fix in MANUAL_UPDATES.items():
        updates[name] = fix

    OUT.write_text(json.dumps({"updates": updates, "drops": [], "kept": kept},
                              ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(audit)} flagged rows -> {len(updates)} fixes, {len(kept)} kept as-is, 0 dropped")
    print("\nsample fixes:")
    for name, fix in list(updates.items())[:12]:
        print(f"  {name[:44]:44} -> kcal={fix['calories']} P{fix['protein']} C{fix['carbs']} F{fix['fats']}  ({fix['reason'][:44]})")
    print("\nkept (genuinely energy-dense):")
    for name in kept:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
