from datetime import timedelta
from pathlib import Path

from flask import Flask
from sqlalchemy import select

from .config import Config
from .dashboard import dashboard_bp
from .extensions import db, login_manager, migrate, oauth
from .models import CoachMessage, MealEntry, ScheduledWorkout, User, WaterLog, WeightLog
from .routes import main_bp
from .security import generate_csrf_token, validate_csrf_request


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
    app.jinja_env.globals["csrf_token"] = generate_csrf_token

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


app = create_app()
