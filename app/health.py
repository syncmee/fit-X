from __future__ import annotations

import csv
from functools import lru_cache
from math import log
from pathlib import Path
from statistics import NormalDist


BMI_DATA_PATH = Path(__file__).resolve().parent / "data" / "bmiagerev.csv"
NORMAL_DISTRIBUTION = NormalDist()

ACTIVITY_LEVEL_LABELS = {
    "sedentary": "Inactive",
    "light": "Low Active",
    "moderate": "Active",
    "athlete": "Very Active",
}

ADULT_EER_COEFFICIENTS = {
    "male": {
        "sedentary": (753.07, -10.83, 6.50, 14.10),
        "light": (581.47, -10.83, 8.30, 14.94),
        "moderate": (1004.82, -10.83, 6.52, 15.91),
        "athlete": (-517.88, -10.83, 15.61, 19.11),
    },
    "female": {
        "sedentary": (584.90, -7.01, 5.72, 11.71),
        "light": (575.77, -7.01, 6.60, 12.14),
        "moderate": (710.25, -7.01, 6.54, 12.34),
        "athlete": (511.83, -7.01, 9.07, 12.56),
    },
}

TEEN_EER_COEFFICIENTS = {
    "male": {
        "younger": {
            "sedentary": (-447.51, 3.68, 13.01, 13.15, 25),
            "light": (19.12, 3.68, 8.62, 20.28, 25),
            "moderate": (-388.19, 3.68, 12.66, 20.46, 25),
            "athlete": (-671.75, 3.68, 15.38, 23.25, 25),
        },
        "older": {
            "sedentary": (-447.51, 3.68, 13.01, 13.15, 20),
            "light": (19.12, 3.68, 8.62, 20.28, 20),
            "moderate": (-388.19, 3.68, 12.66, 20.46, 20),
            "athlete": (-671.75, 3.68, 15.38, 23.25, 20),
        },
    },
    "female": {
        "younger": {
            "sedentary": (55.59, -22.25, 8.43, 17.07, 30),
            "light": (-297.54, -22.25, 12.77, 14.73, 30),
            "moderate": (-189.55, -22.25, 11.74, 18.34, 30),
            "athlete": (-709.59, -22.25, 18.22, 14.25, 30),
        },
        "older": {
            "sedentary": (55.59, -22.25, 8.43, 17.07, 20),
            "light": (-297.54, -22.25, 12.77, 14.73, 20),
            "moderate": (-189.55, -22.25, 11.74, 18.34, 20),
            "athlete": (-709.59, -22.25, 18.22, 14.25, 20),
        },
    },
}

DEFAULT_CALORIE_TARGET = {
    "target": 2000,
    "maintenance": 2000,
    "activity_label": "Profile needed",
    "note": "Complete your profile for an age-aware calorie estimate.",
}

DEFAULT_BMI_SUMMARY = {
    "value": 0.0,
    "status_label": "Complete profile",
    "status_class": "text-gray-400",
    "detail": "Add height, weight, age, and sex for an age-aware BMI estimate.",
}


def calculate_bmi(weight_kg: float, height_cm: float) -> float:
    height_in_meters = height_cm / 100
    if height_in_meters <= 0:
        return 0.0
    return round(weight_kg / (height_in_meters**2), 1)


def estimate_calorie_target(
    *,
    gender: str,
    age: int,
    height_cm: float,
    weight_kg: float,
    activity_level: str,
    goal: str,
) -> dict[str, int | str]:
    maintenance = _calculate_eer(
        gender=gender,
        age=age,
        height_cm=height_cm,
        weight_kg=weight_kg,
        activity_level=activity_level,
    )
    target = maintenance

    if goal == "lose":
        adjustment = _goal_adjustment(maintenance=maintenance, age=age, goal=goal)
        target = maintenance - adjustment
        note = (
            f"Age-aware estimate with {ACTIVITY_LEVEL_LABELS[activity_level]} activity. "
            f"Maintenance is about {maintenance} kcal; target uses a gentle {adjustment} kcal deficit."
        )
    elif goal == "gain":
        adjustment = _goal_adjustment(maintenance=maintenance, age=age, goal=goal)
        target = maintenance + adjustment
        note = (
            f"Age-aware estimate with {ACTIVITY_LEVEL_LABELS[activity_level]} activity. "
            f"Maintenance is about {maintenance} kcal; target uses a steady {adjustment} kcal surplus."
        )
    else:
        note = (
            f"Age-aware estimate with {ACTIVITY_LEVEL_LABELS[activity_level]} activity. "
            f"Maintenance lands around {maintenance} kcal."
        )

    return {
        "target": int(round(target)),
        "maintenance": int(round(maintenance)),
        "activity_label": ACTIVITY_LEVEL_LABELS[activity_level],
        "note": note,
    }


def build_bmi_summary(*, gender: str, age: int, weight_kg: float, height_cm: float) -> dict[str, float | str]:
    bmi = calculate_bmi(weight_kg, height_cm)
    if bmi <= 0:
        return DEFAULT_BMI_SUMMARY.copy()

    if age >= 20:
        return _build_adult_bmi_summary(bmi)

    return _build_child_bmi_summary(gender=gender, age=age, bmi=bmi)


def _calculate_eer(
    *,
    gender: str,
    age: int,
    height_cm: float,
    weight_kg: float,
    activity_level: str,
) -> int:
    if age >= 19:
        baseline, age_coeff, height_coeff, weight_coeff = ADULT_EER_COEFFICIENTS[gender][activity_level]
        value = baseline + (age_coeff * age) + (height_coeff * height_cm) + (weight_coeff * weight_kg)
        return int(round(value))

    teen_group = "younger" if age < 14 else "older"
    baseline, age_coeff, height_coeff, weight_coeff, growth_allowance = TEEN_EER_COEFFICIENTS[gender][teen_group][activity_level]
    value = (
        baseline
        + (age_coeff * age)
        + (height_coeff * height_cm)
        + (weight_coeff * weight_kg)
        + growth_allowance
    )
    return int(round(value))


def _goal_adjustment(*, maintenance: int, age: int, goal: str) -> int:
    # The official equations estimate maintenance/EER. Goal pacing stays
    # intentionally gentler for teens because their estimates already include
    # growth needs, and the dashboard should avoid pushing aggressive cuts.
    if goal == "lose":
        if age < 19:
            return min(300, max(150, int(round(maintenance * 0.10))))
        return min(500, max(200, int(round(maintenance * 0.15))))

    if goal == "gain":
        if age < 19:
            return min(250, max(150, int(round(maintenance * 0.08))))
        return min(350, max(150, int(round(maintenance * 0.10))))

    return 0


def _build_adult_bmi_summary(bmi: float) -> dict[str, float | str]:
    if bmi < 18.5:
        label = "Below Range"
        css_class = "text-fitYellow"
    elif bmi < 25:
        label = "Healthy Range"
        css_class = "text-fitGreen"
    elif bmi < 30:
        label = "Above Range"
        css_class = "text-fitYellow"
    else:
        label = "High Range"
        css_class = "text-red-300"

    return {
        "value": bmi,
        "status_label": label,
        "status_class": css_class,
        "detail": "Adult BMI estimate.",
    }


def _build_child_bmi_summary(*, gender: str, age: int, bmi: float) -> dict[str, float | str]:
    reference = _lookup_bmi_reference(gender=gender, age=age)
    if reference is None:
        return _build_adult_bmi_summary(bmi)

    percentile = _calculate_percentile(bmi=bmi, reference=reference)
    percentile_label = f"{_format_percentile(percentile)} percentile"
    severe_obesity_threshold = max(35.0, reference["P95"] * 1.2)

    if bmi < reference["P5"]:
        label = "Below Range"
        css_class = "text-fitYellow"
    elif bmi < reference["P85"]:
        label = "Healthy Range"
        css_class = "text-fitGreen"
    elif bmi < reference["P95"]:
        label = "Above Range"
        css_class = "text-fitYellow"
    elif bmi >= severe_obesity_threshold:
        label = "High Range"
        css_class = "text-red-300"
    else:
        label = "High Range"
        css_class = "text-red-300"

    return {
        "value": bmi,
        "status_label": label,
        "status_class": css_class,
        "detail": f"CDC age-adjusted BMI - {percentile_label}.",
    }


def _calculate_percentile(*, bmi: float, reference: dict[str, float]) -> int:
    if reference["L"] == 0:
        z_score = 0.0 if bmi <= 0 else log(bmi / reference["M"]) / reference["S"]
    else:
        z_score = (((bmi / reference["M"]) ** reference["L"]) - 1) / (reference["L"] * reference["S"])
    percentile = max(1, min(99, int(round(NORMAL_DISTRIBUTION.cdf(z_score) * 100))))
    return percentile


def _format_percentile(percentile: int) -> str:
    if 10 <= percentile % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(percentile % 10, "th")
    return f"{percentile}{suffix}"


def _lookup_bmi_reference(*, gender: str, age: int) -> dict[str, float] | None:
    if gender not in {"male", "female"}:
        return None

    # Onboarding currently captures whole years only, so use the mid-year point
    # for the closest CDC BMI-for-age reference row.
    age_in_months = (age * 12) + 6
    sex_key = 1 if gender == "male" else 2
    candidates = _load_bmi_reference_rows().get(sex_key, [])
    if not candidates:
        return None

    return min(candidates, key=lambda row: abs(row["Agemos"] - age_in_months))


@lru_cache(maxsize=1)
def _load_bmi_reference_rows() -> dict[int, list[dict[str, float]]]:
    rows: dict[int, list[dict[str, float]]] = {1: [], 2: []}

    with BMI_DATA_PATH.open(newline="", encoding="utf-8") as csv_file:
        reader = csv.DictReader(csv_file)
        for raw_row in reader:
            try:
                sex = int(raw_row["Sex"])
            except (TypeError, ValueError):
                continue
            rows[sex].append(
                {
                    "Agemos": float(raw_row["Agemos"]),
                    "L": float(raw_row["L"]),
                    "M": float(raw_row["M"]),
                    "S": float(raw_row["S"]),
                    "P5": float(raw_row["P5"]),
                    "P85": float(raw_row["P85"]),
                    "P95": float(raw_row["P95"]),
                }
            )

    return rows
