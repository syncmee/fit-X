"""Admin panel: /admin/* routes and the audit trail.

The blueprint owns the admin chrome (templates/admin/base.html: sidebar,
dark minimal theme) and the read/write screens added across Steps 1-3.
Every route is gated by admin_required (session auth + User.is_admin), and
every admin write action must call log_admin_action() so the AuditLog stays
complete. Admin data is global: timestamps here are UTC, not the user's
wall clock (app/timeutil.py).
"""
from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from functools import wraps
from math import ceil
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, render_template_string, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import delete, func, or_, select, update

from .extensions import db
from .challenges import (
    CHALLENGE_METRICS,
    CHALLENGE_STATUSES,
    CHALLENGE_TYPES,
    METRIC_LABELS,
    SEGMENTS,
    TYPE_LABELS,
    users_in_segment,
)
from .models import (
    AuditLog,
    Badge,
    Challenge,
    ChallengeDayLog,
    CoachMessage,
    Exercise,
    Food,
    GoogleIdentity,
    MealEntry,
    PushSubscription,
    ScheduledWorkout,
    User,
    UserBadge,
    UserChallenge,
    WaterLog,
    WeightLog,
)
from .plans import _IMAGE_BASE as EXERCISE_IMAGE_BASE

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def admin_required(view):
    """Gate every admin route: session auth plus User.is_admin. Anonymous
    users get the standard login redirect; signed-in non-admins get a bare
    403 with no hints about what exists behind /admin."""

    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def log_admin_action(admin: User, action: str, target: str = "") -> None:
    """Record an admin write action. Adds the row to the session — the
    caller commits it together with (or right after) the change itself.
    `action` is a short stable verb ("user.suspend"); `target` names what
    changed ("user:3" / "challenge:12")."""
    db.session.add(
        AuditLog(admin_id=admin.id, action=action, target=target, timestamp=datetime.utcnow())
    )


def _count_by_day(stamps, days: int, now: datetime) -> tuple[list[str], list[int]]:
    """Bucket naive datetimes by calendar day over the last `days` days,
    oldest first. Stamps are user wall clocks compared against UTC `now`,
    so far-timezone users can land a bucket a few hours early/late — fine
    for coarse admin trends."""
    counts = Counter(s.date() for s in stamps)
    labels, values = [], []
    for offset in range(days - 1, -1, -1):
        day = (now - timedelta(days=offset)).date()
        labels.append(day.strftime("%b %d"))
        values.append(counts.get(day, 0))
    return labels, values


def _build_dashboard_context() -> dict:
    now = datetime.utcnow()
    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=29)

    def _count(model_column, *conditions) -> int:
        return db.session.scalar(select(func.count(model_column)).where(*conditions)) or 0

    stats = {
        "total_users": _count(User.id),
        "new_users_7d": _count(User.id, User.created_at >= week_ago),
        "new_users_30d": _count(User.id, User.created_at >= month_ago),
        "dau": _count(User.id, User.last_active_at >= day_ago),
        "wau": _count(User.id, User.last_active_at >= week_ago),
        "workouts_24h": _count(
            ScheduledWorkout.id,
            ScheduledWorkout.status == "completed",
            ScheduledWorkout.scheduled_for >= day_ago,
        ),
        "meals_24h": _count(MealEntry.id, MealEntry.logged_at >= day_ago),
    }

    signup_days, signup_counts = _count_by_day(
        db.session.scalars(select(User.created_at).where(User.created_at >= month_ago)).all(),
        days=30,
        now=now,
    )
    workout_days, workout_counts = _count_by_day(
        db.session.scalars(
            select(ScheduledWorkout.scheduled_for).where(
                ScheduledWorkout.status == "completed",
                ScheduledWorkout.scheduled_for >= month_ago,
            )
        ).all(),
        days=30,
        now=now,
    )
    _, meal_counts = _count_by_day(
        db.session.scalars(select(MealEntry.logged_at).where(MealEntry.logged_at >= month_ago)).all(),
        days=30,
        now=now,
    )

    return {
        "stats": stats,
        # NB: series keys avoid "values"/"items"/"keys" — Jinja's attribute
        # lookup would resolve those to the dict's own methods.
        "signup_series": {"labels": signup_days, "counts": signup_counts},
        "activity_series": {"labels": workout_days, "workouts": workout_counts, "meals": meal_counts},
    }


@admin_bp.route("/")
@admin_required
def dashboard():
    return render_template("admin/dashboard.html", **_build_dashboard_context())


# ============================================================
# User management (Step 2)
# ============================================================

USERS_PER_PAGE = LIST_PER_PAGE = 25
_DETAIL_ROWS = 20  # recent rows shown per history table on the user page

# POST action name -> the User.status it sets.
_STATUS_ACTIONS = {"suspend": "suspended", "ban": "banned", "reactivate": "active"}

# Tables referencing user.id without an ON DELETE cascade — cleared manually
# in delete_user before the user row goes. challenge_day_log has no user_id
# (it hangs off user_challenge), so it goes first, by subquery.
_USER_DEPENDENT_TABLES = (
    WeightLog,
    MealEntry,
    WaterLog,
    ScheduledWorkout,
    CoachMessage,
    PushSubscription,
    GoogleIdentity,
    UserBadge,
    UserChallenge,
)


def _audit_target(user: User) -> str:
    """Audit target string; includes the email because user.delete removes
    the row it points at."""
    return f"user:{user.id} {user.email}"


def _search_filter(q: str):
    """Case-insensitive email/name substring filter, or None when q is blank."""
    q = q.strip().lower()
    if not q:
        return None
    pattern = f"%{q}%"
    return or_(
        func.lower(User.email).like(pattern),
        func.lower(User.name).like(pattern),
    )


def _page_param() -> int:
    try:
        return max(int(request.args.get("page", "1")), 1)
    except ValueError:
        return 1


@admin_bp.route("/users")
@admin_required
def users():
    q = request.args.get("q", "").strip()
    page = _page_param()
    condition = _search_filter(q)

    total = db.session.scalar(
        select(func.count(User.id)).where(condition) if condition is not None else select(func.count(User.id))
    ) or 0
    pages = max(ceil(total / LIST_PER_PAGE), 1)

    stmt = select(User).order_by(User.id.desc())
    if condition is not None:
        stmt = stmt.where(condition)
    stmt = stmt.limit(LIST_PER_PAGE).offset((page - 1) * LIST_PER_PAGE)
    rows = db.session.scalars(stmt).all()

    return render_template("admin/users.html", users=rows, q=q, page=page, pages=pages, total=total)


@admin_bp.route("/users/<int:user_id>")
@admin_required
def user_detail(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)

    weights = db.session.scalars(
        select(WeightLog).where(WeightLog.user_id == user.id).order_by(WeightLog.date.desc()).limit(_DETAIL_ROWS)
    ).all()
    meals = db.session.scalars(
        select(MealEntry).where(MealEntry.user_id == user.id).order_by(MealEntry.logged_at.desc()).limit(_DETAIL_ROWS)
    ).all()
    workouts = db.session.scalars(
        select(ScheduledWorkout)
        .where(ScheduledWorkout.user_id == user.id)
        .order_by(ScheduledWorkout.scheduled_for.desc())
        .limit(_DETAIL_ROWS)
    ).all()

    counts = {
        "weight_logs": db.session.scalar(
            select(func.count(WeightLog.id)).where(WeightLog.user_id == user.id)
        ) or 0,
        "meals": db.session.scalar(
            select(func.count(MealEntry.id)).where(MealEntry.user_id == user.id)
        ) or 0,
        "workouts": db.session.scalar(
            select(func.count(ScheduledWorkout.id)).where(ScheduledWorkout.user_id == user.id)
        ) or 0,
        "workouts_done": db.session.scalar(
            select(func.count(ScheduledWorkout.id)).where(
                ScheduledWorkout.user_id == user.id, ScheduledWorkout.status == "completed"
            )
        ) or 0,
    }

    return render_template(
        "admin/user_detail.html", user=user, weights=weights, meals=meals,
        workouts=workouts, counts=counts,
    )


@admin_bp.route("/users/<int:user_id>/status", methods=["POST"])
@admin_required
def set_user_status(user_id: int):
    action = request.form.get("action", "")
    if action not in _STATUS_ACTIONS:
        abort(400)
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    if user.id == current_user.id:
        flash("You can't change your own status.", "error")
        return redirect(url_for("admin.users"))

    user.status = _STATUS_ACTIONS[action]
    log_admin_action(current_user, f"user.{action}", _audit_target(user))
    db.session.commit()
    flash(f"{user.email} is now {user.status}.", "success")
    return redirect(request.referrer or url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    if user.id == current_user.id:
        flash("You can't delete your own account from the admin panel.", "error")
        return redirect(url_for("admin.users"))

    user_challenge_ids = (
        select(UserChallenge.id).where(UserChallenge.user_id == user.id).scalar_subquery()
    )
    db.session.execute(delete(ChallengeDayLog).where(ChallengeDayLog.user_challenge_id.in_(user_challenge_ids)))
    for model in _USER_DEPENDENT_TABLES:
        db.session.execute(delete(model).where(model.user_id == user.id))
    # Keep the audit history, drop the (now dangling) attribution.
    db.session.execute(update(AuditLog).where(AuditLog.admin_id == user.id).values(admin_id=None))
    log_admin_action(current_user, "user.delete", _audit_target(user))
    db.session.delete(user)
    db.session.commit()
    flash(f"Deleted {user.email} and all of their data.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/export.csv")
@admin_required
def export_users():
    """Account data only — no health data in the export. Honors the current
    search term (?q=) so admins export what they are looking at."""
    condition = _search_filter(request.args.get("q", ""))
    stmt = select(User).order_by(User.id.desc())
    if condition is not None:
        stmt = stmt.where(condition)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "name", "email", "status", "is_admin", "onboarding",
                     "created_at", "last_active_at"])
    for user in db.session.scalars(stmt).all():
        writer.writerow([
            user.id, user.name, user.email, user.status,
            "yes" if user.is_admin else "no", "yes" if user.onboarding else "no",
            user.created_at.strftime("%Y-%m-%d %H:%M") if user.created_at else "",
            user.last_active_at.strftime("%Y-%m-%d %H:%M") if user.last_active_at else "",
        ])

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=fit-x-users.csv"},
    )


# ============================================================
# Content management (Step 3): exercises + food database
# ============================================================

# Starter food database. Nutrition is per the listed serving; values are
# round approximations meant to be edited by admins, not lab data.
STARTER_FOODS = (    ("Roti", "1 medium (40 g)", 100, 3, 20, 1),
    ("White Rice, cooked", "100 g", 130, 3, 28, 0),
    ("Brown Rice, cooked", "100 g", 112, 3, 24, 1),
    ("Dal, cooked", "100 g", 116, 9, 20, 1),
    ("Rajma, cooked", "100 g", 127, 9, 22, 1),
    ("Chole, cooked", "100 g", 164, 9, 27, 2),
    ("Paneer", "100 g", 265, 18, 3, 21),
    ("Idli", "1 piece", 58, 2, 12, 0),
    ("Dosa", "1 piece", 133, 3, 22, 4),
    ("Poha", "100 g", 130, 3, 27, 1),
    ("Curd, whole milk", "100 g", 61, 3, 5, 3),
    ("Ghee", "1 tsp", 45, 0, 0, 5),
    ("Samosa", "1 piece", 262, 4, 28, 15),
    ("Chicken Breast", "100 g", 165, 31, 0, 4),
    ("Salmon", "100 g", 208, 20, 0, 13),
    ("Egg", "1 large", 72, 6, 1, 5),
    ("Oats, dry", "100 g", 389, 17, 66, 7),
    ("Whey Protein", "1 scoop (30 g)", 120, 24, 3, 1),
    ("Banana", "1 medium", 105, 1, 27, 0),
    ("Apple", "1 medium", 95, 0, 25, 0),
    ("Milk, whole", "250 ml", 155, 8, 12, 8),
    ("Almonds", "28 g", 164, 6, 6, 14),
    ("Peanut Butter", "1 tbsp (15 g)", 90, 4, 3, 8),
    ("Bread, white", "1 slice", 75, 3, 14, 1),
    ("Potato, boiled", "100 g", 87, 2, 20, 0),
    ("Sweet Potato", "100 g", 86, 2, 20, 0),
    ("Greek Yogurt", "100 g", 59, 10, 4, 1),
    ("Olive Oil", "1 tbsp", 119, 0, 0, 14),
    ("Pizza", "1 slice", 285, 12, 36, 10),
)


def seed_exercises_from_json() -> int:
    """One-time seed of the exercise library from the bundled free-exercise-db
    (app/data/exercises.json). Skips names already present; returns rows added."""
    path = Path(__file__).parent / "data" / "exercises.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    existing = set(db.session.scalars(select(Exercise.name)).all())
    added = 0
    for e in data:
        if e["name"] in existing:
            continue
        db.session.add(Exercise(
            name=e["name"],
            force=e.get("force"),
            level=e.get("level"),
            mechanic=e.get("mechanic"),
            equipment=e.get("equipment"),
            category=e.get("category"),
            primary_muscles=", ".join(e.get("primaryMuscles") or []),
            secondary_muscles=", ".join(e.get("secondaryMuscles") or []),
            instructions="\n".join(e.get("instructions") or []),
            image_url=(EXERCISE_IMAGE_BASE + e["images"][0]) if e.get("images") else None,
        ))
        added += 1
    db.session.commit()
    return added


def seed_starter_foods() -> int:
    """Seed the food database with STARTER_FOODS; skips existing names."""
    existing = set(db.session.scalars(select(Food.name)).all())
    added = 0
    for name, serving, kcal, p, c, f in STARTER_FOODS:
        if name in existing:
            continue
        db.session.add(Food(name=name, serving=serving, calories=kcal,
                            protein=p, carbs=c, fats=f))
        added += 1
    db.session.commit()
    return added


INDIAN_FOOD_CSV = Path(__file__).parent / "data" / "indian_food_nutrition_processed.csv"


def seed_indian_foods_from_csv() -> int:
    """Seed the food database from the Indian food nutrition dataset
    (app/data/indian_food_nutrition_processed.csv — ~1,014 dishes, per-100 g
    values). Skips names already present; returns rows added."""
    if not INDIAN_FOOD_CSV.exists():
        return 0
    existing = {n.lower() for n in db.session.scalars(select(Food.name)).all()}
    added = 0
    with open(INDIAN_FOOD_CSV, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get("Dish Name") or "").strip()
            if not name or name.lower() in existing:
                continue
            try:
                kcal = int(round(float(row.get("Calories (kcal)") or 0)))
                protein = int(round(float(row.get("Protein (g)") or 0)))
                carbs = int(round(float(row.get("Carbohydrates (g)") or 0)))
                fats = int(round(float(row.get("Fats (g)") or 0)))
            except (TypeError, ValueError):
                continue
            db.session.add(Food(name=name, serving="100 g", calories=kcal,
                                protein=protein, carbs=carbs, fats=fats))
            existing.add(name.lower())
            added += 1
    db.session.commit()
    return added


def _content_page(stmt, model) -> tuple:
    """Search-aware paginate for the content lists (q matches the model's
    name); returns (rows, page, pages, q, total)."""
    q = request.args.get("q", "").strip()
    if q:
        stmt = stmt.where(func.lower(model.name).like(f"%{q.lower()}%"))
    total = db.session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    pages = max(ceil(total / LIST_PER_PAGE), 1)
    page = _page_param()
    rows = db.session.scalars(stmt.limit(LIST_PER_PAGE).offset((page - 1) * LIST_PER_PAGE)).all()
    return rows, page, pages, q, total


def _clamp_nonnegative_int(raw: str) -> int | None:
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _exercise_from_form(exercise: Exercise, form) -> list[str]:
    """Fill an Exercise from a form; returns human-readable errors."""
    errors = []
    name = (form.get("name") or "").strip()
    if not name:
        errors.append("Name is required.")
    exercise.name = name
    exercise.force = (form.get("force") or "").strip() or None
    exercise.level = (form.get("level") or "").strip() or None
    exercise.mechanic = (form.get("mechanic") or "").strip() or None
    exercise.equipment = (form.get("equipment") or "").strip() or None
    exercise.category = (form.get("category") or "").strip() or None

    def muscles(field: str) -> str:
        return ", ".join(m.strip().lower() for m in (form.get(field) or "").split(",") if m.strip())

    exercise.primary_muscles = muscles("primary_muscles")
    exercise.secondary_muscles = muscles("secondary_muscles")
    exercise.instructions = (form.get("instructions") or "").strip() or None
    exercise.image_url = (form.get("image_url") or "").strip() or None
    exercise.source = "custom" if exercise.source != "free-exercise-db" else exercise.source
    return errors


def _food_from_form(food: Food, form) -> list[str]:
    errors = []
    name = (form.get("name") or "").strip()
    if not name:
        errors.append("Name is required.")
    food.name = name
    food.serving = (form.get("serving") or "").strip() or "100 g"
    for field, target in (("calories", "calories"), ("protein", "protein"),
                          ("carbs", "carbs"), ("fats", "fats")):
        value = _clamp_nonnegative_int(form.get(field, ""))
        if value is None:
            errors.append(f"{field.capitalize()} must be a non-negative number.")
            value = 0
        setattr(food, target, value)
    return errors


def _content_routes(resource: str, model):
    """Register list/new/edit/delete routes for one content model. Both
    resources share the same shape, so the handlers are built once here."""

    list_endpoint = f"admin.{resource}"
    label = resource.capitalize()

    def _name_taken(name: str, exclude_id: int | None = None) -> bool:
        stmt = select(func.count(model.id)).where(func.lower(model.name) == name.lower())
        if exclude_id is not None:
            stmt = stmt.where(model.id != exclude_id)
        return bool(db.session.scalar(stmt))

    @admin_bp.route(f"/{resource}", endpoint=resource)
    @admin_required
    def _list():
        stmt = select(model).order_by(model.name)
        rows, page, pages, q, total = _content_page(stmt, model)
        return render_template(f"admin/{resource}.html", rows=rows, q=q, page=page,
                               pages=pages, total=total)

    @admin_bp.route(f"/{resource}/new", methods=["GET", "POST"], endpoint=f"{resource}_new")
    @admin_required
    def _new():
        item = model()
        fill = _exercise_from_form if model is Exercise else _food_from_form
        if request.method == "POST":
            errors = fill(item, request.form)
            if _name_taken(item.name):
                errors.append("That name already exists.")
            if errors:
                for e in errors:
                    flash(e, "error")
            else:
                db.session.add(item)
                db.session.flush()  # id for the audit target
                log_admin_action(current_user, f"{resource}.create", f"{resource}:{item.id} {item.name}")
                db.session.commit()
                flash(f"{label} created: {item.name}.", "success")
                return redirect(url_for(list_endpoint))
        return render_template(f"admin/{resource}_form.html", item=item)

    @admin_bp.route(f"/{resource}/<int:item_id>/edit", methods=["GET", "POST"],
                    endpoint=f"{resource}_edit")
    @admin_required
    def _edit(item_id: int):
        item = db.session.get(model, item_id)
        if item is None:
            abort(404)
        fill = _exercise_from_form if model is Exercise else _food_from_form
        if request.method == "POST":
            errors = fill(item, request.form)
            if _name_taken(item.name, exclude_id=item.id):
                errors.append("That name already exists.")
            if errors:
                for e in errors:
                    flash(e, "error")
            else:
                log_admin_action(current_user, f"{resource}.edit", f"{resource}:{item.id} {item.name}")
                db.session.commit()
                flash(f"{label} saved: {item.name}.", "success")
                return redirect(url_for(list_endpoint))
        return render_template(f"admin/{resource}_form.html", item=item)

    @admin_bp.route(f"/{resource}/<int:item_id>/delete", methods=["POST"],
                    endpoint=f"{resource}_delete")
    @admin_required
    def _delete(item_id: int):
        item = db.session.get(model, item_id)
        if item is None:
            abort(404)
        log_admin_action(current_user, f"{resource}.delete", f"{resource}:{item.id} {item.name}")
        db.session.delete(item)
        db.session.commit()
        flash(f"{label} deleted: {item.name}.", "success")
        return redirect(url_for(list_endpoint))


_content_routes("exercises", Exercise)
_content_routes("foods", Food)


# ============================================================
# Challenge admin (Step 5)
# ============================================================

# POST action name -> (allowed current statuses, new status).
_CHALLENGE_STATUS_ACTIONS = {
    "publish": ({"draft", "scheduled"}, "active"),
    "schedule": ({"draft"}, "scheduled"),
    "end": ({"active", "scheduled"}, "ended"),
    "redraft": ({"scheduled"}, "draft"),
}


def _challenge_from_form(challenge: Challenge, form) -> list[str]:
    """Fill a Challenge from the admin form; returns human-readable errors."""
    errors = []
    title = (form.get("title") or "").strip()
    if not title:
        errors.append("Title is required.")
    challenge.title = title
    challenge.description = (form.get("description") or "").strip() or None
    challenge.banner_url = (form.get("banner_url") or "").strip() or None

    ctype = form.get("type") or ""
    metric = form.get("metric") or ""
    if ctype not in CHALLENGE_TYPES:
        errors.append("Unknown challenge type.")
    if metric not in CHALLENGE_METRICS:
        errors.append("Unknown metric.")
    challenge.type, challenge.metric = ctype, metric

    try:
        target = float(form.get("target_value") or 0)
    except ValueError:
        target = 0
    if target <= 0:
        errors.append("Target value must be a positive number.")
    challenge.target_value = target

    try:
        daily = float(form.get("daily_target") or 0)
    except ValueError:
        daily = 0
    challenge.daily_target = daily if daily > 0 else None

    duration = _clamp_nonnegative_int(form.get("duration_days") or "")
    challenge.duration_days = duration or None

    def parse_date(field: str):
        raw = (form.get(field) or "").strip()
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            errors.append(f"{field.replace('_', ' ').capitalize()} must be a valid date.")
            return None

    challenge.start_date = parse_date("start_date")
    challenge.end_date = parse_date("end_date")
    if (challenge.start_date and challenge.end_date
            and challenge.end_date < challenge.start_date):
        errors.append("End date can't be before the start date.")

    challenge.rolling = form.get("rolling") == "on"
    audience = form.get("audience") or "all"
    if audience not in {"all", "segment"}:
        errors.append("Unknown audience.")
    challenge.audience = audience

    segment = form.get("audience_segment") or ""
    if audience == "segment":
        if segment not in SEGMENTS or segment == "all":
            errors.append("Pick a segment for the audience.")
        else:
            challenge.audience_segment = segment
    else:
        challenge.audience_segment = None

    badge_raw = form.get("badge_id") or ""
    if badge_raw.isdigit():
        badge = db.session.get(Badge, int(badge_raw))
        challenge.badge_id = badge.id if badge else None
    else:
        challenge.badge_id = None
    return errors


def _challenge_form_context(challenge: Challenge | None, template: Challenge | None = None) -> dict:
    badges = db.session.scalars(select(Badge).order_by(Badge.name)).all()
    source = template or challenge
    # Audience preview: how many users a publish would reach.
    if source is not None and source.audience == "segment" and source.audience_segment:
        audience_count = len(users_in_segment(source.audience_segment))
        audience_label = SEGMENTS.get(source.audience_segment, source.audience_segment)
    else:
        audience_count = len(users_in_segment("all"))
        audience_label = SEGMENTS["all"]
    templates = db.session.scalars(
        select(Challenge).where(Challenge.status == "draft").order_by(Challenge.title)
    ).all()
    return {
        "item": challenge,
        "prefill": source,
        "badges": badges,
        "types": CHALLENGE_TYPES,
        "metrics": CHALLENGE_METRICS,
        "type_labels": TYPE_LABELS,
        "metric_labels": METRIC_LABELS,
        "templates": templates,
        "segments": SEGMENTS,
        "audience_count": audience_count,
        "audience_label": audience_label,
    }


@admin_bp.route("/challenges")
@admin_required
def challenges():
    rows = db.session.scalars(
        select(Challenge).order_by(Challenge.status, Challenge.title)
    ).all()
    counts = dict(db.session.execute(
        select(UserChallenge.challenge_id, func.count(UserChallenge.id))
        .group_by(UserChallenge.challenge_id)
    ).all())
    return render_template(
        "admin/challenges.html", rows=rows, counts=counts,
        type_labels=TYPE_LABELS, metric_labels=METRIC_LABELS,
    )


@admin_bp.route("/challenges/new")
@admin_required
def challenge_new():
    template = None
    template_id = request.args.get("template", "")
    if template_id.isdigit():
        template = db.session.get(Challenge, int(template_id))
    return render_template("admin/challenge_form.html", **_challenge_form_context(None, template))


@admin_bp.route("/challenges/new", methods=["POST"])
@admin_required
def challenge_create():
    challenge = Challenge(created_by=current_user.id)
    errors = _challenge_from_form(challenge, request.form)
    if errors:
        for e in errors:
            flash(e, "error")
        return render_template("admin/challenge_form.html", **_challenge_form_context(challenge)), 400
    db.session.add(challenge)
    db.session.flush()
    log_admin_action(current_user, "challenge.create", f"challenge:{challenge.id} {challenge.title}")
    db.session.commit()
    flash(f"Challenge created as draft: {challenge.title}.", "success")
    return redirect(url_for("admin.challenges"))


@admin_bp.route("/challenges/<int:challenge_id>/edit", methods=["GET", "POST"])
@admin_required
def challenge_edit(challenge_id: int):
    challenge = db.session.get(Challenge, challenge_id)
    if challenge is None:
        abort(404)
    if request.method == "POST":
        errors = _challenge_from_form(challenge, request.form)
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            log_admin_action(current_user, "challenge.edit", f"challenge:{challenge.id} {challenge.title}")
            db.session.commit()
            flash(f"Challenge saved: {challenge.title}.", "success")
            return redirect(url_for("admin.challenges"))
    return render_template("admin/challenge_form.html", **_challenge_form_context(challenge))


@admin_bp.route("/challenges/<int:challenge_id>/status", methods=["POST"])
@admin_required
def challenge_status(challenge_id: int):
    action = request.form.get("action", "")
    allowed, new_status = _CHALLENGE_STATUS_ACTIONS.get(action, (set(), ""))
    challenge = db.session.get(Challenge, challenge_id)
    if challenge is None or not new_status or challenge.status not in allowed:
        abort(400)
    challenge.status = new_status
    log_admin_action(current_user, f"challenge.{action}", f"challenge:{challenge.id} {challenge.title}")
    db.session.commit()
    flash(f"{challenge.title} is now {new_status}.", "success")
    return redirect(url_for("admin.challenges"))


@admin_bp.route("/challenges/<int:challenge_id>/delete", methods=["POST"])
@admin_required
def challenge_delete(challenge_id: int):
    challenge = db.session.get(Challenge, challenge_id)
    if challenge is None:
        abort(404)
    log_admin_action(current_user, "challenge.delete", f"challenge:{challenge.id} {challenge.title}")
    db.session.delete(challenge)  # cascades to participations and day logs
    db.session.commit()
    flash(f"Challenge deleted: {challenge.title}.", "success")
    return redirect(url_for("admin.challenges"))


# ============================================================
# Analytics + announcements (Step 10)
# ============================================================

# Rough USD prices per 1M tokens (prompt, completion) for the cost estimate
# on the coach monitor. Estimates only — update when model pricing changes.
COACH_COST_PER_1M = {
    "gemini": (0.10, 0.40),
    "groq": (0.15, 0.75),
    "rules": (0.0, 0.0),
}

ANNOUNCE_SEGMENTS = SEGMENTS

# Placeholders usable inside a custom announcement email. Everything is
# filled per recipient at send time.
ANNOUNCE_PLACEHOLDERS = {
    "first_name": "recipient's first name",
    "full_name": "recipient's full name",
    "email": "recipient's email address",
    "title": "the announcement title",
    "body": "the announcement message",
    "dashboard_url": "absolute link to the app",
}


def _html_to_plain(html: str) -> str:
    """Rough plain-text alternative for custom-HTML emails: drop tags,
    keep line breaks, unescape entities."""
    import html as html_module

    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</tr>|</h[1-6]>|</li>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text)
    lines = (line.strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _render_announcement_email(user: User, title: str, body: str,
                               template_style: str, custom_html: str,
                               dashboard_url: str) -> str:
    """One recipient's email HTML. 'fitx' uses the branded template;
    'custom' renders the admin's HTML as a Jinja template so their
    {{ first_name }}-style placeholders are filled per recipient."""
    if template_style != "custom":
        return render_template(
            "emails/announcement.html",
            announce_title=title, announce_body=body,
            user_name=user.name, dashboard_url=dashboard_url,
        )
    try:
        return render_template_string(
            custom_html,
            first_name=user.name.split()[0] if user.name else "",
            full_name=user.name,
            email=user.email,
            title=title,
            body=body,
            dashboard_url=dashboard_url,
        )
    except Exception as exc:
        raise ValueError(f"Custom HTML failed to render: {exc}") from exc


def _segment_users(segment: str) -> list[User]:
    if segment not in SEGMENTS:
        abort(400)
    return users_in_segment(segment)


def _announce_form_fields() -> tuple[str, str, str, str]:
    """Parsed announcement form: (title, body, template_style, custom_html)."""
    title = (request.form.get("title") or "").strip()
    body = (request.form.get("body") or "").strip()
    template_style = request.form.get("template_style") or "fitx"
    custom_html = request.form.get("custom_html") or ""
    if template_style not in {"fitx", "custom"}:
        abort(400)
    return title, body, template_style, custom_html


@admin_bp.route("/announce/preview", methods=["POST"])
@admin_required
def announce_preview():
    """Render the announcement for the admin's own data and return it as a
    page, so custom HTML can be designed and checked before sending."""
    from .dashboard import _send_via_smtp, _send_web_push  # noqa: F401 (parity with send path)

    title, body, template_style, custom_html = _announce_form_fields()
    if not title or not body:
        flash("Give the announcement a title and a message first.", "error")
        return redirect(url_for("admin.announce"))
    if template_style == "custom" and not custom_html.strip():
        flash("Paste your custom HTML first.", "error")
        return redirect(url_for("admin.announce"))
    dashboard_url = current_app.config.get("SITE_URL", "").rstrip("/") + "/dashboard"
    try:
        html = _render_announcement_email(current_user, title, body,
                                          template_style, custom_html, dashboard_url)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.announce"))
    return Response(html)


@admin_bp.route("/announce", methods=["GET", "POST"])
@admin_required
def announce():
    from .dashboard import _send_via_smtp, _send_web_push

    if request.method == "POST":
        title, body, template_style, custom_html = _announce_form_fields()
        segment = request.form.get("audience") or "all"
        channels = set(request.form.getlist("channels"))
        if not title or not body:
            flash("Give the announcement a title and a message.", "error")
            return redirect(url_for("admin.announce"))
        if segment not in ANNOUNCE_SEGMENTS:
            abort(400)
        if not channels <= {"push", "email"} or not channels:
            flash("Pick at least one channel.", "error")
            return redirect(url_for("admin.announce"))
        if template_style == "custom" and not custom_html.strip():
            flash("Paste your custom HTML first.", "error")
            return redirect(url_for("admin.announce"))

        users = _segment_users(segment)
        tag = f"announce-{int(datetime.utcnow().timestamp())}"
        pushes = emails = skipped = 0
        smtp_user = current_app.config.get("SMTP_USER", "")
        smtp_password = current_app.config.get("SMTP_APP_PASSWORD", "")
        dashboard_url = current_app.config.get("SITE_URL", "").rstrip("/") + "/dashboard"
        for user in users:
            delivered = False
            if "push" in channels and user.push_subscriptions:
                if _send_web_push(user, title=f"fiT-X · {title}", body=body,
                                  url="/dashboard", tag=tag):
                    pushes += 1
                    delivered = True
            if "email" in channels and smtp_user:
                try:
                    html = _render_announcement_email(user, title, body,
                                                      template_style, custom_html, dashboard_url)
                except ValueError as exc:
                    flash(str(exc), "error")
                    return redirect(url_for("admin.announce"))
                ok, _detail = _send_via_smtp(user, f"fiT-X · {title}",
                                             _html_to_plain(html), html,
                                             smtp_user, smtp_password)
                if ok:
                    emails += 1
                    delivered = True
            if not delivered:
                skipped += 1

        log_admin_action(current_user, "announce.send",
                         f"audience:{segment} push:{pushes} email:{emails}")
        db.session.commit()
        flash(f"Announcement sent to {len(users)} users — {pushes} pushes, {emails} emails"
              + (f", {skipped} unreachable" if skipped else "") + ".", "success")
        return redirect(url_for("admin.announce"))

    sizes = {key: len(_segment_users(key)) for key in ANNOUNCE_SEGMENTS}
    return render_template("admin/announce.html", segments=ANNOUNCE_SEGMENTS, sizes=sizes,
                           placeholders=ANNOUNCE_PLACEHOLDERS)


@admin_bp.route("/challenges/analytics")
@admin_required
def challenge_analytics():
    rows = []
    for c in db.session.scalars(select(Challenge).order_by(Challenge.status, Challenge.title)):
        parts = list(c.participants)
        total = len(parts)
        completed = sum(1 for p in parts if p.status == "completed")
        failed = sum(1 for p in parts if p.status == "failed")
        active = total - completed - failed
        streaks_open = [p.current_streak for p in parts if p.status != "completed"]
        drop_off = Counter(streaks_open).most_common(1)[0][0] if streaks_open else None
        avg_progress = None
        if total and c.target_value:
            avg_progress = round(
                sum(min(p.progress_value / c.target_value, 1.0) for p in parts) / total * 100
            )
        rows.append({
            "challenge": c,
            "joined": total,
            "active": active,
            "completed": completed,
            "failed": failed,
            "completion_rate": round(completed / total * 100) if total else None,
            "avg_progress": avg_progress,
            "drop_off": drop_off,
        })

    return render_template(
        "admin/challenge_analytics.html", rows=rows,
        labels=[r["challenge"].title for r in rows],
        joined=[r["joined"] for r in rows],
        completed=[r["completed"] for r in rows],
        failed=[r["failed"] for r in rows],
    )


@admin_bp.route("/coach")
@admin_required
def coach_monitor():
    now = datetime.utcnow()
    day_ago, week_ago = now - timedelta(hours=24), now - timedelta(days=7)
    fortnight_ago = now - timedelta(days=13)

    rows = db.session.scalars(
        select(CoachMessage).where(CoachMessage.role != "user", CoachMessage.created_at >= fortnight_ago)
    ).all()

    def window(rows_, start):
        return [r for r in rows_ if r.created_at and r.created_at >= start]

    week_rows = window(rows, week_ago)
    day_rows = window(rows, day_ago)
    successes_7d = [r for r in week_rows if not r.had_error]
    errors_7d = [r for r in week_rows if r.had_error]
    latencies = [r.latency_ms for r in successes_7d if r.latency_ms is not None]

    provider_counts = Counter(r.provider or "unknown" for r in week_rows)
    cost = 0.0
    for r in successes_7d:
        per_1m = COACH_COST_PER_1M.get(r.provider or "rules", (0.0, 0.0))
        cost += ((r.prompt_tokens or 0) * per_1m[0] + (r.completion_tokens or 0) * per_1m[1]) / 1_000_000

    per_user = Counter(r.user_id for r in week_rows)
    heavy = []
    for user_id, count in per_user.most_common(10):
        user = db.session.get(User, user_id)
        if user is None:
            continue
        user_latencies = [r.latency_ms for r in week_rows
                          if r.user_id == user_id and r.latency_ms is not None and not r.had_error]
        heavy.append({
            "user": user,
            "requests": count,
            "avg_latency": round(sum(user_latencies) / len(user_latencies)) if user_latencies else None,
        })

    day_rows_all = [r.created_at for r in rows if r.created_at]
    labels, counts = _count_by_day(day_rows_all, days=14, now=now)

    return render_template(
        "admin/coach.html",
        stats={
            "requests_24h": len(day_rows),
            "requests_7d": len(week_rows),
            "errors_7d": len(errors_7d),
            "error_rate": round(len(errors_7d) / len(week_rows) * 100) if week_rows else None,
            "avg_latency": round(sum(latencies) / len(latencies)) if latencies else None,
            "est_cost_7d": round(cost, 4),
            "successes_7d": len(successes_7d),
        },
        provider_counts=dict(provider_counts),
        series={"labels": labels, "counts": counts},
        heavy=heavy,
    )
