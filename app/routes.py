from datetime import datetime, timedelta

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import func, or_, select

from .coach import COACH_QUICK_STARTS, process_coach_message
from .extensions import db
from .models import CoachMessage, ScheduledWorkout, User, WeightLog
from .validation import validate_login_form, validate_onboarding_form, validate_signup_form

main_bp = Blueprint("main", __name__)


def _flash_errors(errors: list[str]) -> None:
    for error in errors:
        flash(error, "error")


def _build_trend_data() -> tuple[list[float], list[str]]:
    logs = sorted(current_user.logs, key=lambda log: log.date or datetime.utcnow())
    recent_logs = logs[-7:]

    trend_values = [round(log.weight, 1) for log in recent_logs]
    trend_labels = [log.date.strftime("%d %b") for log in recent_logs]

    if not trend_values and current_user.weight is not None:
        trend_values = [round(current_user.weight, 1)]
        trend_labels = [datetime.now().strftime("%d %b")]

    if trend_values:
        while len(trend_values) < 7:
            trend_values.insert(0, trend_values[0])
            trend_labels.insert(0, trend_labels[0])
    else:
        today = datetime.now().strftime("%d %b")
        trend_values = [0.0] * 7
        trend_labels = [today] * 7

    return trend_values, trend_labels


def _format_workout_label(value: datetime, now: datetime) -> str:
    if value.date() == now.date():
        day = "Today"
    elif value.date() == (now + timedelta(days=1)).date():
        day = "Tomorrow"
    else:
        day = value.strftime("%a, %d %b")
    return f"{day} at {value.strftime('%I:%M %p').lstrip('0')}"


def _get_upcoming_workouts(now: datetime) -> list[ScheduledWorkout]:
    return db.session.execute(
        select(ScheduledWorkout)
        .where(
            ScheduledWorkout.user_id == current_user.id,
            ScheduledWorkout.status == "scheduled",
            ScheduledWorkout.scheduled_for >= now,
        )
        .order_by(ScheduledWorkout.scheduled_for.asc())
        .limit(5)
    ).scalars().all()


def _get_recent_messages() -> list[CoachMessage]:
    messages = db.session.execute(
        select(CoachMessage)
        .where(CoachMessage.user_id == current_user.id)
        .order_by(CoachMessage.created_at.desc())
        .limit(6)
    ).scalars().all()
    return list(reversed(messages))


def _get_recent_weight_logs() -> list[WeightLog]:
    return db.session.execute(
        select(WeightLog)
        .where(WeightLog.user_id == current_user.id)
        .order_by(WeightLog.date.desc())
        .limit(5)
    ).scalars().all()


def _build_dashboard_context() -> dict:
    daily_calories = 2000
    bmi = 0
    progress_percentage = 0
    current_hour = datetime.now().hour
    greeting = "Welcome"
    meal_suggestion = "Meal"
    now = datetime.now()

    if 5 <= current_hour < 11:
        greeting = "Good Morning"
        meal_suggestion = "Breakfast"
    elif 11 <= current_hour < 16:
        greeting = "Good Afternoon"
        meal_suggestion = "Lunch"
    elif 16 <= current_hour < 19:
        greeting = "Good Evening"
        meal_suggestion = "Snacks"
    else:
        greeting = "Good Night"
        meal_suggestion = "Dinner"

    if (
        current_user.weight is not None
        and current_user.height is not None
        and current_user.age is not None
    ):
        height_in_meters = current_user.height / 100
        bmi = round(current_user.weight / (height_in_meters**2), 1)

        val_weight = 10 * current_user.weight
        val_height = 6.25 * current_user.height
        val_age = 5 * current_user.age

        if current_user.gender == "male":
            bmr = val_weight + val_height - val_age + 5
        else:
            bmr = val_weight + val_height - val_age - 161

        multipliers = {
            "sedentary": 1.2,
            "light": 1.375,
            "moderate": 1.55,
            "athlete": 1.725,
        }
        activity_factor = multipliers.get(current_user.activity_level, 1.2)
        tdee = bmr * activity_factor

        if current_user.goal == "lose":
            daily_calories = int(tdee - 500)
        elif current_user.goal == "gain":
            daily_calories = int(tdee + 500)
        else:
            daily_calories = int(tdee)

        if current_user.start_weight is not None and current_user.target_weight is not None:
            total_change_needed = abs(current_user.start_weight - current_user.target_weight)
            change_achieved = abs(current_user.start_weight - current_user.weight)

            if total_change_needed > 0:
                progress_percentage = int((change_achieved / total_change_needed) * 100)
                progress_percentage = max(0, min(100, progress_percentage))
            else:
                progress_percentage = 100

    trend_values, trend_labels = _build_trend_data()
    recent_weight_logs = _get_recent_weight_logs()
    recent_messages = _get_recent_messages()
    upcoming_workouts = _get_upcoming_workouts(now)
    next_workout = upcoming_workouts[0] if upcoming_workouts else None

    weekly_workout_count = db.session.execute(
        select(func.count(ScheduledWorkout.id)).where(
            ScheduledWorkout.user_id == current_user.id,
            ScheduledWorkout.status == "scheduled",
            ScheduledWorkout.scheduled_for >= now,
            ScheduledWorkout.scheduled_for < now + timedelta(days=7),
        )
    ).scalar_one()

    latest_delta = None
    if len(recent_weight_logs) >= 2:
        latest_delta = round(recent_weight_logs[0].weight - recent_weight_logs[1].weight, 1)

    if latest_delta is None:
        weight_status = "First check-in"
    elif latest_delta < 0:
        weight_status = f"Down {abs(latest_delta):.1f} kg"
    elif latest_delta > 0:
        weight_status = f"Up {abs(latest_delta):.1f} kg"
    else:
        weight_status = "No change"

    if bmi == 0:
        bmi_status_label = "Complete profile"
        bmi_status_class = "text-gray-400"
    elif bmi < 18.5:
        bmi_status_label = "Below Range"
        bmi_status_class = "text-fitYellow"
    elif bmi < 25:
        bmi_status_label = "Healthy Range"
        bmi_status_class = "text-fitGreen"
    elif bmi < 30:
        bmi_status_label = "Above Range"
        bmi_status_class = "text-fitYellow"
    else:
        bmi_status_label = "High Range"
        bmi_status_class = "text-red-300"

    goal_anchor_title = current_user.goal.capitalize() if current_user.goal else "Goal"
    if current_user.target_weight is not None:
        goal_anchor_detail = f"{current_user.target_weight:.1f} kg target"
    else:
        goal_anchor_detail = "Set your target"

    change_since_start_display = "--"
    change_since_start_caption = "Add a starting weight"
    change_since_start_class = "text-white"
    if current_user.start_weight is not None and current_user.weight is not None:
        change_since_start = round(current_user.weight - current_user.start_weight, 1)
        change_since_start_display = f"{change_since_start:+.1f} kg"
        change_since_start_caption = "since day one"
        aligned_with_goal = (
            (current_user.goal == "lose" and change_since_start < 0)
            or (current_user.goal == "gain" and change_since_start > 0)
        )
        if change_since_start == 0:
            change_since_start_class = "text-white"
        elif aligned_with_goal:
            change_since_start_class = "text-fitGreen"
        else:
            change_since_start_class = "text-fitYellow"

    coach_mode_label = "fiT-X AI"

    return {
        "user": current_user,
        "calories": daily_calories,
        "bmi": bmi,
        "trends": trend_values,
        "trend_labels": trend_labels,
        "progress": progress_percentage,
        "greeting": greeting,
        "meal": meal_suggestion,
        "chat_messages": recent_messages,
        "coach_examples": COACH_QUICK_STARTS,
        "coach_mode_label": coach_mode_label,
        "upcoming_workouts": upcoming_workouts,
        "next_workout": next_workout,
        "next_workout_label": _format_workout_label(next_workout.scheduled_for, now) if next_workout else None,
        "recent_log_count": len(current_user.logs),
        "weekly_workout_count": weekly_workout_count,
        "weight_status": weight_status,
        "bmi_status_label": bmi_status_label,
        "bmi_status_class": bmi_status_class,
        "goal_anchor_title": goal_anchor_title,
        "goal_anchor_detail": goal_anchor_detail,
        "change_since_start_display": change_since_start_display,
        "change_since_start_caption": change_since_start_caption,
        "change_since_start_class": change_since_start_class,
    }


@main_bp.route("/")
def homepage():
    return render_template("homepage.html")


@main_bp.route("/test")
def legacy_test_page():
    return redirect(url_for("main.homepage"))


@main_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    active_section = "signin"

    if request.method == "POST":
        form_type = request.form.get("form_type", "signin")

        if form_type == "signup":
            active_section = "signup"
            cleaned_data, errors = validate_signup_form(request.form)
            if errors:
                _flash_errors(errors)
                return render_template("login.html", active_section=active_section), 400

            existing_user = db.session.execute(
                select(User).where(
                    or_(
                        func.lower(User.email) == cleaned_data["email"],
                        func.lower(User.name) == cleaned_data["name"].lower(),
                    )
                )
            ).scalar_one_or_none()

            if existing_user is not None:
                flash("That email or username is already registered.", "error")
                return render_template("login.html", active_section=active_section), 409

            new_user = User(
                name=cleaned_data["name"],
                email=cleaned_data["email"],
                onboarding=False,
            )
            new_user.set_password(cleaned_data["password"])
            db.session.add(new_user)
            db.session.commit()

            flash("Account created. Please sign in to continue.", "success")
            return redirect(url_for("main.login"))

        cleaned_data, errors = validate_login_form(request.form)
        if errors:
            _flash_errors(errors)
            return render_template("login.html", active_section=active_section), 400

        user = db.session.execute(
            select(User).where(func.lower(User.email) == cleaned_data["email"])
        ).scalar_one_or_none()

        if user is None or not user.check_password(cleaned_data["password"]):
            flash("Invalid email or password.", "error")
            return render_template("login.html", active_section=active_section), 401

        login_user(user, remember=cleaned_data["remember"])

        if not user.onboarding:
            return redirect(url_for("main.onboarding"))

        return redirect(url_for("main.dashboard"))

    return render_template("login.html", active_section=active_section)


@main_bp.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", **_build_dashboard_context())


@main_bp.route("/coach/message", methods=["POST"])
@login_required
def coach_message():
    raw_message = request.form.get("message", "")
    try:
        process_coach_message(current_user, raw_message)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    except Exception:
        db.session.rollback()
        flash("The coach could not process that request. Please try again.", "error")

    return redirect(url_for("main.dashboard", _anchor="coach-chat"))


@main_bp.route("/dashboard/<user>")
@login_required
def legacy_dashboard(user: str):
    return redirect(url_for("main.dashboard"))


@main_bp.route("/onboarding", methods=["GET", "POST"])
@login_required
def onboarding():
    if request.method == "GET" and current_user.onboarding:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        cleaned_data, errors = validate_onboarding_form(request.form)
        if errors:
            _flash_errors(errors)
            return redirect(url_for("main.onboarding"))

        current_user.gender = cleaned_data["gender"]
        current_user.age = cleaned_data["age"]
        current_user.height = cleaned_data["height"]
        current_user.weight = cleaned_data["weight"]
        current_user.start_weight = cleaned_data["weight"]
        current_user.target_weight = cleaned_data["target_weight"]
        current_user.activity_level = cleaned_data["activity_level"]
        current_user.diet = cleaned_data["diet"]
        current_user.goal = cleaned_data["goal"]
        current_user.onboarding = True

        if not current_user.logs:
            db.session.add(WeightLog(weight=cleaned_data["weight"], user=current_user))

        db.session.commit()
        flash("Your profile is ready. Welcome to your dashboard.", "success")
        return redirect(url_for("main.dashboard"))

    return render_template("onboarding.html")


@main_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("main.login"))
