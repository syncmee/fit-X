from flask import Blueprint, current_app, flash, redirect, render_template, request, send_from_directory, url_for
from flask_login import current_user, login_required, login_user, logout_user
from secrets import token_urlsafe
from sqlalchemy import func, or_, select

from .extensions import db, oauth
from .models import GoogleIdentity, User, WeightLog
from .validation import validate_login_form, validate_onboarding_form, validate_signup_form

main_bp = Blueprint("main", __name__)


def _flash_errors(errors: list[str]) -> None:
    for error in errors:
        flash(error, "error")


@main_bp.route("/")
def homepage():
    return render_template("homepage.html")


@main_bp.route("/google841e75738b84838b.html")
def google_site_verification():
    # Search Console ownership-proof file — must stay at the site root,
    # served verbatim, forever (removing it un-verifies the domain).
    return send_from_directory(current_app.static_folder, "google841e75738b84838b.html")


@main_bp.route("/privacy-policy")
def privacy_policy():
    return render_template("policy.html")


@main_bp.route("/terms-and-conditions")
def terms_and_conditions():
    return render_template("terms-condition.html")


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


@main_bp.route("/auth/google")
def google_login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.dashboard"))
    if oauth is None or not current_app.config.get("GOOGLE_CLIENT_ID"):
        flash("Google sign-in isn't configured yet.", "error")
        return redirect(url_for("main.login"))
    redirect_uri = url_for("main.google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@main_bp.route("/auth/google/callback")
def google_callback():
    if oauth is None or not current_app.config.get("GOOGLE_CLIENT_ID"):
        return redirect(url_for("main.login"))
    try:
        token = oauth.google.authorize_access_token()
    except Exception:
        flash("Google sign-in was cancelled or failed. Try again.", "error")
        return redirect(url_for("main.login"))

    profile = token.get("userinfo") or {}
    user = _find_or_create_google_user(profile)
    if user is None:
        flash("Your Google account's email isn't verified, so it can't be linked.", "error")
        return redirect(url_for("main.login"))

    login_user(user, remember=True)
    if not user.onboarding:
        return redirect(url_for("main.onboarding"))
    return redirect(url_for("dashboard.dashboard"))


def _find_or_create_google_user(profile: dict) -> User | None:
    """Match a verified Google profile to a fiT-X account.

    Known Google identity -> that user. Known email (password account) -> link
    the identity to it, so existing users keep all their data. Otherwise a new
    account is created and goes through onboarding like everyone else.
    """
    google_id = str(profile.get("sub") or "")
    if not google_id:
        return None

    identity = db.session.execute(
        select(GoogleIdentity).where(GoogleIdentity.google_id == google_id)
    ).scalar_one_or_none()
    if identity:
        picture = profile.get("picture") or ""
        if picture and identity.picture_url != picture:
            identity.picture_url = picture
            db.session.commit()
        return identity.user

    email = (profile.get("email") or "").strip().lower()
    if not email or not profile.get("email_verified"):
        return None  # only verified Google emails may claim an account

    user = db.session.execute(
        select(User).where(func.lower(User.email) == email)
    ).scalar_one_or_none()

    if user is None:
        # Brand-new member: unique name derived from the email, onboarding pending.
        base_name = email.split("@", 1)[0][:70] or "member"
        name, suffix = base_name, 1
        while db.session.execute(select(User.id).where(User.name == name)).scalar_one_or_none() is not None:
            suffix += 1
            name = f"{base_name}-{suffix}"
        user = User(name=name, email=email, onboarding=False)
        # Unusable-by-design password: Google members sign in with Google.
        user.set_password(token_urlsafe(24))
        db.session.add(user)
        db.session.flush()

    db.session.add(
        GoogleIdentity(
            google_id=google_id,
            email=email,
            picture_url=profile.get("picture") or "",
            user_id=user.id,
        )
    )
    db.session.commit()
    return user


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
