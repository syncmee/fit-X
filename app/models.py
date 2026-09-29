from flask_login import UserMixin
from sqlalchemy import event
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db, login_manager
from .timeutil import user_now


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    email = db.Column(db.String(120), nullable=False, unique=True)
    password = db.Column(db.String(255), nullable=False)
    onboarding = db.Column(db.Boolean, default=False)

    gender = db.Column(db.String(20))
    age = db.Column(db.Integer)
    height = db.Column(db.Float)
    weight = db.Column(db.Float)
    start_weight = db.Column(db.Float)
    target_weight = db.Column(db.Float)
    activity_level = db.Column(db.String(50))
    goal = db.Column(db.String(50))
    pace = db.Column(db.Integer, default=2)  # 1 slow, 2 balanced, 3 aggressive
    # Browser-reported UTC offset in minutes (JS getTimezoneOffset, e.g. -330 for
    # IST). Lets every stored timestamp and the reminder cron use the user's
    # local wall clock.
    tz_offset_minutes = db.Column(db.Integer)
    email_reminders_enabled = db.Column(db.Boolean, default=True)

    logs = db.relationship("WeightLog", backref="user", lazy=True, order_by="WeightLog.date")
    push_subscriptions = db.relationship(
        "PushSubscription",
        backref="user",
        lazy=True,
        cascade="all, delete-orphan",
    )
    scheduled_workouts = db.relationship(
        "ScheduledWorkout",
        backref="user",
        lazy=True,
        order_by="ScheduledWorkout.scheduled_for",
    )
    coach_messages = db.relationship(
        "CoachMessage",
        backref="user",
        lazy=True,
        order_by="CoachMessage.created_at",
    )

    def set_password(self, password: str) -> None:
        self.password = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password, password)

    def __repr__(self) -> str:
        return f"<User {self.name}>"


class WeightLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    weight = db.Column(db.Float, nullable=False)
    date = db.Column(db.DateTime, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    def __repr__(self) -> str:
        return f"<WeightLog {self.weight}kg on {self.date}>"


class ScheduledWorkout(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    scheduled_for = db.Column(db.DateTime, nullable=False, index=True)
    duration_minutes = db.Column(db.Integer, nullable=False, default=60)
    status = db.Column(db.String(20), nullable=False, default="scheduled", index=True)
    notes = db.Column(db.Text)
    calories_burned = db.Column(db.Integer)
    reminder_sent_at = db.Column(db.DateTime)  # set once the reminder email has gone out
    created_at = db.Column(db.DateTime, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    def __repr__(self) -> str:
        return f"<ScheduledWorkout {self.title} at {self.scheduled_for}>"


class CoachMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    def __repr__(self) -> str:
        return f"<CoachMessage {self.role} {self.created_at}>"


class MealEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    meal_type = db.Column(db.String(20), nullable=False, default="meal")  # breakfast/lunch/dinner/snack
    calories = db.Column(db.Integer, nullable=False, default=0)
    protein = db.Column(db.Integer, nullable=False, default=0)
    carbs = db.Column(db.Integer, nullable=False, default=0)
    fats = db.Column(db.Integer, nullable=False, default=0)
    logged_at = db.Column(db.DateTime, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    user = db.relationship("User", backref="meal_entries")

    def __repr__(self) -> str:
        return f"<MealEntry {self.name} {self.calories}kcal {self.logged_at}>"


class WaterLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    amount_ml = db.Column(db.Integer, nullable=False)
    logged_at = db.Column(db.DateTime, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    user = db.relationship("User", backref="water_logs")

    def __repr__(self) -> str:
        return f"<WaterLog {self.amount_ml}ml {self.logged_at}>"


class PushSubscription(db.Model):
    """A browser push subscription (one per device where the user enabled
    notifications). endpoint is the unique push-service URL; p256dh/auth are
    the per-subscription encryption keys."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    endpoint = db.Column(db.String(500), nullable=False, unique=True)
    p256dh = db.Column(db.String(120), nullable=False)
    auth = db.Column(db.String(60), nullable=False)
    user_agent = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, nullable=False)

    def __repr__(self) -> str:
        return f"<PushSubscription user={self.user_id} {self.endpoint[:40]}...>"


class GoogleIdentity(db.Model):
    """Links a Google account to a fiT-X user. Kept in its own table so the
    existing user table stays untouched; created automatically by create_all()."""

    __tablename__ = "google_identity"
    id = db.Column(db.Integer, primary_key=True)
    google_id = db.Column(db.String(64), nullable=False, unique=True, index=True)
    email = db.Column(db.String(120), nullable=False)
    picture_url = db.Column(db.String(500))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    user = db.relationship("User", backref=db.backref("google_identity", uselist=False))

    def __repr__(self) -> str:
        return f"<GoogleIdentity {self.email}>"


@login_manager.user_loader
def load_user(user_id: str):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


# Columns stamped with the user's wall clock when a write leaves them empty.
_LOCAL_NOW_COLUMNS = {
    WeightLog: "date",
    ScheduledWorkout: "created_at",
    CoachMessage: "created_at",
    MealEntry: "logged_at",
    WaterLog: "logged_at",
    PushSubscription: "created_at",
}


@event.listens_for(Session, "before_flush")
def _stamp_user_local_times(session, flush_context, instances):
    """Every stored datetime is the user's wall clock (app/timeutil.py). Rows
    inserted without an explicit timestamp get one here, so no code path can
    fall back to server/UTC time by accident."""
    for obj in session.new:
        field = _LOCAL_NOW_COLUMNS.get(type(obj))
        if field is not None and getattr(obj, field) is None:
            setattr(obj, field, user_now(getattr(obj, "user", None)))
