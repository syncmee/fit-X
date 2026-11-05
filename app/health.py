"""Health calculations shared by onboarding and the dashboard.

This module has no Flask/database dependency, which keeps calorie and BMI
calculations deterministic and easy to test independently of routes.
"""
import csv
from functools import lru_cache
from math import log
from pathlib import Path
from statistics import NormalDist


BMI_DATA_PATH = Path(__file__).resolve().parent / "data" / "bmiagerev.csv"
NORMAL_DISTRIBUTION = NormalDist()

ACTIVITY_LEVEL_LABELS = {
    "sedentary": "Inactive", "light": "Low Active", "moderate": "Active", "athlete": "Very Active",
}
ACTIVITY_MULTIPLIERS = {"sedentary": 1.2, "light": 1.375, "moderate": 1.55, "athlete": 1.9}
PACE_KG_PER_WEEK = {
    "lose": {1: -0.25, 2: -0.5, 3: -0.75},
    "gain": {1: 0.125, 2: 0.25, 3: 0.4},
    "maintain": {1: 0.0, 2: 0.0, 3: 0.0},
}
DEFAULT_CALORIE_TARGET = {
    "target": 2000, "maintenance": 2000, "activity_label": "Profile needed",
    "note": "Complete your profile for a personal calorie estimate.",
}
DEFAULT_BMI_SUMMARY = {
    "value": 0.0, "status_label": "Complete profile", "status_class": "text-gray-400",
    "detail": "Add height, weight, age, and sex for an age-aware BMI estimate.",
}


def calculate_bmi(weight_kg: float, height_cm: float) -> float:
    height_in_meters = height_cm / 100
    if height_in_meters <= 0:
        return 0.0
    return round(weight_kg / (height_in_meters**2), 1)


def estimate_calorie_target(*, gender: str, age: int, height_cm: float, weight_kg: float,
                            activity_level: str, goal: str, pace: int = 2) -> dict[str, int | str]:
    """Calculate a Mifflin-St Jeor TDEE target using the selected pace."""
    bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + (5 if gender == "male" else -161)
    maintenance = bmr * ACTIVITY_MULTIPLIERS.get(activity_level, 1.55)
    kg_per_week = PACE_KG_PER_WEEK.get(goal, PACE_KG_PER_WEEK["maintain"]).get(pace, 0.0)
    target = int(round(maintenance + kg_per_week * 7700 / 7))
    sign = "+" if kg_per_week > 0 else ("−" if kg_per_week < 0 else "±")
    pace_label = {1: "slow", 2: "balanced", 3: "aggressive"}.get(pace, "balanced")
    activity_label = ACTIVITY_LEVEL_LABELS.get(activity_level, "Active")
    if kg_per_week == 0:
        note = f"Your maintenance (TDEE) is about {round(maintenance)} kcal with {activity_label} activity — you chose to maintain."
    else:
        note = f"Your TDEE is about {round(maintenance)} kcal with {activity_label} activity; target uses your {pace_label} pace ({sign}{abs(kg_per_week)} kg/week)."
    return {"target": target, "maintenance": int(round(maintenance)), "activity_label": activity_label, "note": note}


def build_bmi_summary(*, gender: str, age: int, weight_kg: float, height_cm: float) -> dict[str, float | str]:
    bmi = calculate_bmi(weight_kg, height_cm)
    if bmi <= 0:
        return DEFAULT_BMI_SUMMARY.copy()
    return _build_adult_bmi_summary(bmi) if age >= 20 else _build_child_bmi_summary(gender=gender, age=age, bmi=bmi)


def _build_adult_bmi_summary(bmi: float) -> dict[str, float | str]:
    if bmi < 18.5:
        label, css_class = "Below Range", "text-yellow"
    elif bmi < 25:
        label, css_class = "Healthy Range", "text-green"
    elif bmi < 30:
        label, css_class = "Above Range", "text-yellow"
    else:
        label, css_class = "High Range", "text-red-400"
    return {"value": bmi, "status_label": label, "status_class": css_class, "detail": "Adult BMI estimate."}


def _build_child_bmi_summary(*, gender: str, age: int, bmi: float) -> dict[str, float | str]:
    reference = _lookup_bmi_reference(gender=gender, age=age)
    if reference is None:
        return _build_adult_bmi_summary(bmi)
    percentile = _calculate_percentile(bmi=bmi, reference=reference)
    if bmi < reference["P5"]:
        label, css_class = "Below Range", "text-yellow"
    elif bmi < reference["P85"]:
        label, css_class = "Healthy Range", "text-green"
    else:
        label, css_class = "High Range", "text-red-400"
    return {"value": bmi, "status_label": label, "status_class": css_class,
            "detail": f"CDC age-adjusted BMI - {_format_percentile(percentile)} percentile."}


def _calculate_percentile(*, bmi: float, reference: dict[str, float]) -> int:
    if reference["L"] == 0:
        z_score = 0.0 if bmi <= 0 else log(bmi / reference["M"]) / reference["S"]
    else:
        z_score = (((bmi / reference["M"]) ** reference["L"]) - 1) / (reference["L"] * reference["S"])
    return max(1, min(99, int(round(NORMAL_DISTRIBUTION.cdf(z_score) * 100))))


def _format_percentile(percentile: int) -> str:
    suffix = "th" if 10 <= percentile % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(percentile % 10, "th")
    return f"{percentile}{suffix}"


def _lookup_bmi_reference(*, gender: str, age: int) -> dict[str, float] | None:
    if gender not in {"male", "female"}:
        return None
    candidates = _load_bmi_reference_rows().get(1 if gender == "male" else 2, [])
    return min(candidates, key=lambda row: abs(row["Agemos"] - (age * 12 + 6))) if candidates else None


@lru_cache(maxsize=1)
def _load_bmi_reference_rows() -> dict[int, list[dict[str, float]]]:
    rows: dict[int, list[dict[str, float]]] = {1: [], 2: []}
    with BMI_DATA_PATH.open(newline="", encoding="utf-8") as csv_file:
        for raw_row in csv.DictReader(csv_file):
            try:
                sex = int(raw_row["Sex"])
                rows[sex].append({key: float(raw_row[key]) for key in ("Agemos", "L", "M", "S", "P5", "P85", "P95")})
            except (KeyError, TypeError, ValueError):
                continue
    return rows
