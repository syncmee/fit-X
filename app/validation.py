import re

EMAIL_PATTERN = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)

ALLOWED_GENDERS = {"male", "female"}
ALLOWED_ACTIVITY_LEVELS = {"sedentary", "light", "moderate", "athlete"}
ALLOWED_GOALS = {"lose", "gain", "maintain"}


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
    }, errors
