import re

EMAIL_PATTERN = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)

ALLOWED_GENDERS = {"male", "female"}
ALLOWED_ACTIVITY_LEVELS = {"sedentary", "light", "moderate", "athlete"}
ALLOWED_GOALS = {"lose", "gain", "maintain"}
ALLOWED_ENVIRONMENTS = {"gym", "home", "home_gym"}
ENVIRONMENT_LABELS = {
    "gym": "Gym",
    "home": "Home",
    "home_gym": "Home + equipment",
}


def _normalize_text(value: str | None) -> str:
    return " ".join((value or "").strip().split())


def _normalize_email(value: str | None) -> str:
    return _normalize_text(value).lower()


def _parse_int(value: str | None, field_name: str, minimum: int, maximum: int) -> int:
    try:
        parsed_value = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a whole number.")

    if parsed_value < minimum or parsed_value > maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}.")

    return parsed_value


def _parse_float(value: str | None, field_name: str, minimum: float, maximum: float) -> float:
    try:
        parsed_value = float(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a number.")

    if parsed_value < minimum or parsed_value > maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}.")

    return round(parsed_value, 1)


def validate_signup_form(form) -> tuple[dict, list[str]]:
    errors: list[str] = []

    # Support both the legacy login-old.html field names (name-signup/email-signup/password-signup)
    # and the unified auth UI field names (name/email/password).
    name = _normalize_text(form.get("name-signup") or form.get("name"))
    email = _normalize_email(form.get("email-signup") or form.get("email"))
    password = form.get("password-signup") or form.get("password") or ""

    if len(name) < 2 or len(name) > 80:
        errors.append("Name must be between 2 and 80 characters.")

    if not EMAIL_PATTERN.match(email):
        errors.append("Please enter a valid email address.")

    if len(password) < 8:
        errors.append("Password must be at least 8 characters long.")

    if password and (password.lower() == password or password.upper() == password or not any(ch.isdigit() for ch in password)):
        errors.append("Password must include a mix of letters and numbers.")

    return {
        "name": name,
        "email": email,
        "password": password,
    }, errors


def validate_login_form(form) -> tuple[dict, list[str]]:
    errors: list[str] = []

    email = _normalize_email(form.get("email"))
    password = form.get("password", "")
    remember = form.get("remember") == "on"

    if not EMAIL_PATTERN.match(email):
        errors.append("Please enter the email address you signed up with.")

    if not password:
        errors.append("Password is required.")

    return {
        "email": email,
        "password": password,
        "remember": remember,
    }, errors


def validate_environment(value: str | None) -> str | None:
    env = _normalize_text(value).lower().replace(" ", "_")
    env = {"home_equipment": "home_gym", "gym": "gym", "home": "home", "home_gym": "home_gym"}.get(env)
    return env if env in ALLOWED_ENVIRONMENTS else None


def validate_onboarding_form(form) -> tuple[dict, list[str]]:
    errors: list[str] = []

    gender = _normalize_text(form.get("gender")).lower()
    activity_level = _normalize_text(form.get("activity-level")).lower()
    goal = _normalize_text(form.get("goal")).lower()
    try:
        pace = int(form.get("pace") or 2)
    except (TypeError, ValueError):
        pace = 2
    pace = max(1, min(3, pace))

    try:
        age = _parse_int(form.get("age"), "Age", 13, 100)
    except ValueError as exc:
        errors.append(str(exc))
        age = None

    try:
        height = _parse_float(form.get("height"), "Height", 100, 260)
    except ValueError as exc:
        errors.append(str(exc))
        height = None

    try:
        weight = _parse_float(form.get("weight"), "Weight", 30, 350)
    except ValueError as exc:
        errors.append(str(exc))
        weight = None

    try:
        target_weight = _parse_float(form.get("target_weight"), "Target weight", 30, 350)
    except ValueError as exc:
        errors.append(str(exc))
        target_weight = None

    if gender not in ALLOWED_GENDERS:
        errors.append("Please choose your gender.")

    if activity_level not in ALLOWED_ACTIVITY_LEVELS:
        errors.append("Please choose your activity level.")

    if goal not in ALLOWED_GOALS:
        errors.append("Please choose your primary goal.")

    if weight is not None and target_weight is not None:
        if goal == "lose" and target_weight >= weight:
            errors.append("For a weight-loss goal, target weight should be lower than your current weight.")
        if goal == "gain" and target_weight <= weight:
            errors.append("For a muscle-gain goal, target weight should be higher than your current weight.")

    return {
        "gender": gender,
        "age": age,
        "height": height,
        "weight": weight,
        "target_weight": target_weight,
        "activity_level": activity_level,
        "goal": goal,
        "pace": pace,
        # Optional: unset means the coach asks after the first schedule.
        "training_environment": validate_environment(form.get("training_environment")),
    }, errors


def validate_profile_form(form) -> tuple[dict, list[str]]:
    """Settings -> Biometrics/Goals save: the editable subset of the profile.
    Everything is optional so each tab can post only its own fields."""
    errors: list[str] = []
    data: dict = {"training_environment": validate_environment(form.get("training_environment"))}

    try:
        data["weight"] = _parse_float(form.get("weight"), "Weight", 30, 350) if (form.get("weight") or "").strip() else None
    except ValueError as exc:
        errors.append(str(exc))
        data["weight"] = None

    try:
        data["height"] = _parse_float(form.get("height"), "Height", 100, 260) if (form.get("height") or "").strip() else None
    except ValueError as exc:
        errors.append(str(exc))
        data["height"] = None

    try:
        data["age"] = _parse_int(form.get("age"), "Age", 13, 100) if (form.get("age") or "").strip() else None
    except ValueError as exc:
        errors.append(str(exc))
        data["age"] = None

    activity_level = _normalize_text(form.get("activity_level")).lower()
    data["activity_level"] = activity_level if activity_level in ALLOWED_ACTIVITY_LEVELS else None

    goal = _normalize_text(form.get("goal")).lower()
    data["goal"] = goal if goal in ALLOWED_GOALS else None

    try:
        data["pace"] = _parse_int(form.get("pace"), "Pace", 1, 3) if (form.get("pace") or "").strip() else None
    except ValueError as exc:
        errors.append(str(exc))
        data["pace"] = None

    try:
        data["target_weight"] = (
            _parse_float(form.get("target_weight"), "Target weight", 30, 350) if (form.get("target_weight") or "").strip() else None
        )
    except ValueError as exc:
        errors.append(str(exc))
        data["target_weight"] = None

    weight = data["weight"] or None
    target_weight = data["target_weight"] or None
    goal = data["goal"]
    if weight is not None and target_weight is not None:
        if goal == "lose" and target_weight >= weight:
            errors.append("For a weight-loss goal, target weight should be lower than your current weight.")
        if goal == "gain" and target_weight <= weight:
            errors.append("For a muscle-gain goal, target weight should be higher than your current weight.")

    def _macro(name: str) -> int | None:
        raw = (form.get(name) or "").strip()
        if not raw:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            errors.append(f"Macro {name.replace('macro_', '')} must be a whole number.")
            return None
        if not 0 <= value <= 1000:
            errors.append(f"Macro {name.replace('macro_', '')} must be between 0 and 1000 g.")
            return None
        return value

    # Macro overrides ride the Goals tab form: all three saved together when
    # the toggle is on, cleared together when it's off.
    if form.get("macro_overrides_enabled") == "true":
        data["macro_overrides"] = {
            "protein": _macro("macro_protein"),
            "carbs": _macro("macro_carbs"),
            "fats": _macro("macro_fats"),
        }
    else:
        data["macro_overrides"] = None

    return data, errors
