from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import func, or_, select

from .extensions import db
from .models import User, WeightLog
from .validation import validate_login_form, validate_onboarding_form, validate_signup_form

main_bp = Blueprint("main", __name__)


def _flash_errors(errors: list[str]) -> None:
    for error in errors:
        flash(error, "error")


@main_bp.route("/")
def homepage():
    return render_template("homepage.html")


@main_bp.route("/test")
def legacy_test_page():
    return redirect(url_for("main.homepage"))


@main_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.dashboard"))

    active_section = "signin"

    if request.method == "POST":
        # Unified auth UI: action=login|signup (legacy form_type=signin|signup also supported)
        action = request.form.get("action")
        form_type = request.form.get("form_type", "signin")

        if action in {"login", "signin"}:
            form_type = "signin"
        elif action == "signup":
            form_type = "signup"

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

            login_user(new_user)
            return redirect(url_for("main.onboarding"))

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

        return redirect(url_for("dashboard.dashboard"))

    return render_template("login.html", active_section=active_section)


@main_bp.route("/onboarding", methods=["GET", "POST"])
@login_required
def onboarding():
    if request.method == "GET" and current_user.onboarding:
        return redirect(url_for("dashboard.dashboard"))

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
        current_user.goal = cleaned_data["goal"]
        current_user.pace = cleaned_data["pace"]
        current_user.onboarding = True

        if not current_user.logs:
            db.session.add(WeightLog(weight=cleaned_data["weight"], user=current_user))

        db.session.commit()
        flash("Your profile is ready. Welcome to your dashboard.", "success")
        return redirect(url_for("dashboard.dashboard"))

    return render_template("onboarding.html")


@main_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("main.login"))
