from pathlib import Path

from flask import Flask

from .config import Config
from .dashboard import dashboard_bp
from .extensions import db, login_manager, migrate
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

    return app


def register_cli_commands(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db_command() -> None:
        """Create the database tables for local development."""
        with app.app_context():
            db.create_all()
        print("Database initialized.")


app = create_app()
