from datetime import timedelta
from pathlib import Path

import click
from flask import Flask, current_app
from sqlalchemy import func, select

from .config import Config
from .admin import admin_bp
from .challenges import challenges_bp
from .dashboard import dashboard_bp
from .extensions import db, login_manager, migrate, oauth
from .models import CoachMessage, MealEntry, ScheduledWorkout, User, WaterLog, WeightLog
from .routes import main_bp
from .security import generate_csrf_token, validate_csrf_request
from .seo import JSONLD_GRAPH, SITE_URL


def add_security_headers(response):
    """Baseline hardening without touching rendering. HSTS is prod-only so
    local http:// development doesn't get pinned to https in the browser."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if not current_app.config["DEBUG"]:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


def inject_seo_globals():
    """Canonical origin + homepage JSON-LD graph for the templates' <head>."""
    return {"site_url": SITE_URL, "seo_jsonld_graph": JSONLD_GRAPH}


def create_app(config_class: type[Config] = Config) -> Flask:
    root_dir = Path(__file__).resolve().parent.parent
    app = Flask(
        __name__,
        instance_relative_config=True,
        template_folder=str(root_dir / "templates"),
        static_folder=str(root_dir / "static"),
    )
    app.config.from_object(config_class)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)

    if migrate is not None:
        migrate.init_app(app, db)

    app.before_request(validate_csrf_request)
    app.after_request(add_security_headers)
    app.jinja_env.globals["csrf_token"] = generate_csrf_token
    app.context_processor(inject_seo_globals)

    if oauth is not None:
        oauth.init_app(app)
        if app.config.get("GOOGLE_CLIENT_ID"):
            oauth.register(
                name="google",
                client_id=app.config["GOOGLE_CLIENT_ID"],
                client_secret=app.config["GOOGLE_CLIENT_SECRET"],
                server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
                client_kwargs={"scope": "openid email profile"},
                overwrite=True,
            )

    app.register_blueprint(main_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(challenges_bp)
    app.register_blueprint(admin_bp)
    register_cli_commands(app)

    # create_all is idempotent; guarantees MealEntry/WaterLog exist in prod,
    # where gunicorn never runs main.py's __main__ block.
    with app.app_context():
        db.create_all()
        # No Alembic migrations in this project, so new columns ship as guarded
        # ALTERs (they no-op once applied).
        for statement in (
            "ALTER TABLE scheduled_workout ADD COLUMN calories_burned INTEGER",
            "ALTER TABLE scheduled_workout ADD COLUMN reminder_sent_at TIMESTAMP",
            'ALTER TABLE "user" ADD COLUMN pace INTEGER',
            'ALTER TABLE "user" ADD COLUMN tz_offset_minutes INTEGER',
            'ALTER TABLE "user" ADD COLUMN email_reminders_enabled BOOLEAN DEFAULT TRUE',
            # Admin foundation (also shipped as migration 0001_admin_foundation)
            'ALTER TABLE "user" ADD COLUMN is_admin BOOLEAN DEFAULT FALSE',
            'ALTER TABLE "user" ADD COLUMN created_at TIMESTAMP',
            'ALTER TABLE "user" ADD COLUMN last_active_at TIMESTAMP',
        ):
            try:
                db.session.execute(db.text(statement))
                db.session.commit()
            except Exception:
                db.session.rollback()

        try:
            _shift_stored_utc_rows_to_local()
        except Exception:
            db.session.rollback()
            app.logger.exception("UTC-to-wall-clock migration failed; will retry next boot")

    return app


# Tables whose event timestamps were stored naive-UTC before everything moved
# to the user's wall clock. scheduled_for was always wall clock, and
# reminder_sent_at is only ever compared to NULL, so neither shifts.
_UTC_TO_LOCAL_TARGETS = (
    (WeightLog, "date"),
    (MealEntry, "logged_at"),
    (WaterLog, "logged_at"),
    (CoachMessage, "created_at"),
    (ScheduledWorkout, "created_at"),
)


def _shift_stored_utc_rows_to_local() -> None:
    """One-time data migration: event timestamps used to be stored naive-UTC;
    they are now the user's wall clock (app/timeutil.py). Each user's rows
    shift by their browser offset, and the marker row commits in the same
    transaction, so a crash or a second gunicorn worker can never
    double-shift. Users with no synced offset are skipped and retried on a
    later boot — their rows are still UTC and their zone is unknown."""
    db.session.execute(
        db.text("CREATE TABLE IF NOT EXISTS tz_local_migration (user_id INTEGER PRIMARY KEY)")
    )
    db.session.commit()

    for user in db.session.scalars(select(User)).all():
        if user.tz_offset_minutes is None:
            continue
        already_done = db.session.execute(
            db.text("SELECT 1 FROM tz_local_migration WHERE user_id = :uid"), {"uid": user.id}
        ).scalar()
        if already_done:
            continue

        # local = utc - offset; the browser offset is negative east of UTC
        # (e.g. -330 for IST), so this is a +5:30 shift for that user.
        shift = timedelta(minutes=-user.tz_offset_minutes)
        if shift:
            for model, field in _UTC_TO_LOCAL_TARGETS:
                rows = db.session.scalars(
                    select(model).where(
                        getattr(model, "user_id") == user.id,
                        getattr(model, field).is_not(None),
                    )
                ).all()
                for row in rows:
                    setattr(row, field, getattr(row, field) + shift)
        db.session.execute(
            db.text("INSERT INTO tz_local_migration (user_id) VALUES (:uid)"), {"uid": user.id}
        )
        db.session.commit()


def register_cli_commands(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db_command() -> None:
        """Create the database tables for local development."""
        with app.app_context():
            db.create_all()
        print("Database initialized.")

    @app.cli.command("promote-user")
    @click.argument("email")
    @click.option("--revoke", is_flag=True, help="Remove admin rights instead of granting them.")
    def promote_user_command(email: str, revoke: bool) -> None:
        """Grant (or with --revoke, remove) admin rights for a user's email."""
        with app.app_context():
            user = db.session.execute(
                select(User).where(func.lower(User.email) == email.strip().lower())
            ).scalar_one_or_none()
            if user is None:
                print(f"No user with email {email!r}.")
                raise SystemExit(1)
            user.is_admin = not revoke
            db.session.commit()
            print(f"{'Revoked admin from' if revoke else 'Promoted'} {user.email} ({user.name}).")

    @app.cli.command("seed-content")
    def seed_content_command() -> None:
        """Seed the exercise library (bundled free-exercise-db) and the
        starter food database. Idempotent — existing names are skipped."""
        from .admin import seed_exercises_from_json, seed_starter_foods

        with app.app_context():
            exercises = seed_exercises_from_json()
            foods = seed_starter_foods()
        print(f"Exercises added: {exercises}. Foods added: {foods}.")

    @app.cli.command("seed-challenges")
    def seed_challenges_command() -> None:
        """Seed the starter challenge templates (draft status) and their
        badges. Idempotent — existing titles are skipped."""
        from .challenges import seed_challenge_templates

        with app.app_context():
            challenges = seed_challenge_templates()
        print(f"Challenge templates added: {challenges}.")


app = create_app()
