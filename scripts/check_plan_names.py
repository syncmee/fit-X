"""Validate that every curated name literal in app/plans.py resolves in the
bundled exercise catalog, and that generated plans only reference real entries.

Run:  python -I scripts/check_plan_names.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.plans import (  # noqa: E402
    _GROUP_TABLE,
    _boxing_plan,
    _cooldown_block,
    _exercises,
    _generic_plan,
    _resolve,
    _running_plan,
    _warmup_block,
    build_plan,
)

failures: list[str] = []


def check_candidates(where: str, candidates: tuple[str, ...]) -> None:
    e = _resolve(*candidates)
    if e is None:
        failures.append(f"{where}: NONE of {candidates} resolve")
    elif e["name"].lower() not in {c.lower() for c in candidates}:
        # resolved via substring; flag so the literal can be tightened
        failures.append(f"{where}: {candidates} only substring-resolves to '{e['name']}'")


def check_lines(where: str, lines: list[dict], names: set) -> None:
    for line in lines:
        if line.get("source") == "coach":
            continue
        if line["name"] not in names:
            failures.append(f"{where}: line '{line['name']}' is not in the dataset")
        if line.get("met") is None:
            failures.append(f"{where}: line '{line['name']}' has no met")
        if "image" in line:
            failures.append(f"{where}: line '{line['name']}' still carries an image key")


def main() -> int:
    names = {e["name"] for e in _exercises()}

    # 1. every curated literal in the gym group table
    for group, table in _GROUP_TABLE.items():
        for candidates, _dose in table["picks"]:
            check_candidates(f"group {group}", candidates)

    # 2. modality + warmup/cooldown/generic blocks
    for title, plan in (
        ("Boxing", {"name": "Boxing", "blocks": _boxing_plan()}),
        ("Running", {"name": "Running", "blocks": _running_plan()}),
        ("Generic", _generic_plan("gym")),
        ("Generic home", _generic_plan("home")),
    ):
        for block in plan["blocks"]:
            check_lines(f"{title}/{block['block']}", block["exercises"], names)

    # 3. full build_plan sweep: titles x environments
    envs = [None, "gym", "home", "home_gym"]
    titles = [
        "Full Body", "Push Day", "Pull Day", "Upper Body", "Lower Body",
        "Chest + Triceps", "Back and Biceps", "Legs", "Glutes", "Quads",
        "Hamstrings", "Calves", "Core", "Shoulders", "Arms", "Boxing Session",
        "Morning Run", "Yoga", "random gibberish xyz",
    ]
    for env in envs:
        for title in titles:
            plan = build_plan(title, env, generic_fallback=True)
            if plan is None:
                failures.append(f"build_plan({title!r}, {env!r}) returned None")
                continue
            for block in plan["blocks"]:
                check_lines(f"{title!r}/{env}", block["exercises"], names)
            if not plan["blocks"]:
                failures.append(f"build_plan({title!r}, {env!r}) has no blocks")

    # 4. warmup/cooldown group paths directly
    for groups in (["legs"], ["glutes"], ["chest", "back"], ["core"]):
        for env in envs:
            for block in (_warmup_block(groups, env, set()), _cooldown_block(groups, env, set())):
                check_lines(f"warm/cooldown {groups}/{env}", block["exercises"], names)

    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  {f}")
        return 1
    print("OK: all curated name literals resolve; all plan lines exist, carry met, and have no image key")
    return 0


if __name__ == "__main__":
    sys.exit(main())
