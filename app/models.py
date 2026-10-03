from datetime import datetime

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

    # Admin panel (app/admin.py). created_at is stamped server-UTC: signup
    # happens before the browser reports its offset, and it only feeds coarse
    # daily stats. last_active_at is the user's wall clock (dashboard_bp
    # refreshes it, throttled) and drives the DAU/WAU stats. status is the
    # account state — non-active accounts fail load_user and both login paths.
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, index=True)
    last_active_at = db.Column(db.DateTime, index=True)
    status = db.Column(db.String(20), nullable=False, default="active", index=True)  # active | suspended | banned

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
    role = db.Column(db.String(20), nullable=False)  # user | assistant | error
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    # Telemetry for the admin AI-coach monitor (Step 10). Rows with
    # role="error" record failed AI attempts (had_error=True) and are hidden
    # from the chat feed and AI history.
    provider = db.Column(db.String(20))  # gemini | groq | rules
    latency_ms = db.Column(db.Integer)
    had_error = db.Column(db.Boolean, default=False)
    prompt_tokens = db.Column(db.Integer)
    completion_tokens = db.Column(db.Integer)

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


class Exercise(db.Model):
    """One exercise in the shared content library. Seeded from the bundled
    free-exercise-db (app/data/exercises.json) by `flask seed-content` and
    editable in the admin panel (app/admin.py). Muscle fields hold
    comma-joined muscle names; plans.py still reads the JSON directly, so
    edits here feed future consumers (coach suggestions, challenge content),
    not reminder plans."""

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, unique=True)
    force = db.Column(db.String(20))      # push / pull / static
    level = db.Column(db.String(20))      # beginner / intermediate / expert
    mechanic = db.Column(db.String(20))   # compound / isolation
    equipment = db.Column(db.String(60))
    category = db.Column(db.String(40))   # strength / stretching / plyometrics / ...
    primary_muscles = db.Column(db.String(200))
    secondary_muscles = db.Column(db.String(200))
    instructions = db.Column(db.Text)     # one step per line
    image_url = db.Column(db.String(300))
    source = db.Column(db.String(20), nullable=False, default="free-exercise-db")  # or "custom"

    def __repr__(self) -> str:
        return f"<Exercise {self.name}>"


class Food(db.Model):
    """One entry in the shared food database — nutrition for the listed
    serving (e.g. '100 g' or '1 medium'). Editable in the admin panel; feeds
    future meal lookup (the roadmap's meal database)."""

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, unique=True)
    serving = db.Column(db.String(60), nullable=False, default="100 g")
    calories = db.Column(db.Integer, nullable=False, default=0)
    protein = db.Column(db.Integer, nullable=False, default=0)
    carbs = db.Column(db.Integer, nullable=False, default=0)
    fats = db.Column(db.Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<Food {self.name} {self.calories}kcal>"


class Badge(db.Model):
    """Awarded automatically when a user completes the challenge that links
    to it (app/challenges.py, Step 7). icon holds an emoji."""

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    description = db.Column(db.String(200))
    icon = db.Column(db.String(10))

    def __repr__(self) -> str:
        return f"<Badge {self.name}>"


class Challenge(db.Model):
    """A challenge definition (app/challenges.py documents the type/metric
    semantics). status: draft | scheduled | active | ended — drafts act as
    templates for the admin's create form (Step 5). rolling challenges let
    users join anytime (their window starts at the join); fixed ones run
    between start_date and end_date. daily_target is the per-day threshold
    for streak/count challenges (e.g. 10000 steps)."""

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False, index=True)
    description = db.Column(db.Text)
    banner_url = db.Column(db.String(300))
    type = db.Column(db.String(20), nullable=False)  # streak | cumulative | single_goal | count
    metric = db.Column(db.String(20), nullable=False)  # workout_count | distance_km | calories | meals_logged | weight_logged | steps
    target_value = db.Column(db.Float, nullable=False)
    daily_target = db.Column(db.Float)
    duration_days = db.Column(db.Integer)
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    rolling = db.Column(db.Boolean, nullable=False, default=False)
    status = db.Column(db.String(20), nullable=False, default="draft", index=True)
    audience = db.Column(db.String(20), nullable=False, default="all")  # all | segment
    audience_segment = db.Column(db.String(20))  # which SEGMENTS key, when audience == "segment"
    badge_id = db.Column(db.Integer, db.ForeignKey("badge.id"))
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)  # UTC (admin data)

    badge = db.relationship("Badge")
    participants = db.relationship(
        "UserChallenge",
        backref="challenge",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="UserChallenge.joined_at",
    )

    def __repr__(self) -> str:
        return f"<Challenge {self.title} ({self.type}/{self.metric}, {self.status})>"


class UserChallenge(db.Model):
    """One user's participation in a challenge. progress_value accumulates
    the metric (Step 7); streaks are tracked in current_streak/longest_streak.
    The (user_id, challenge_id) pair is unique — one join per user."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenge.id"), nullable=False, index=True)
    joined_at = db.Column(db.DateTime)  # user wall clock, stamped on insert
    progress_value = db.Column(db.Float, nullable=False, default=0)
    current_streak = db.Column(db.Integer, nullable=False, default=0)
    longest_streak = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(20), nullable=False, default="active")  # active | completed | failed
    completed_at = db.Column(db.DateTime)  # user wall clock, set on completion
    # Stamp for the daily not-logged-today reminder (throttle: one per ~20h).
    last_reminded_at = db.Column(db.DateTime)

    user = db.relationship("User", backref="user_challenges")
    day_logs = db.relationship(
        "ChallengeDayLog",
        backref="user_challenge",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="ChallengeDayLog.date",
    )

    __table_args__ = (db.UniqueConstraint("user_id", "challenge_id", name="uq_user_challenge_pair"),)

    def __repr__(self) -> str:
        return f"<UserChallenge user={self.user_id} challenge={self.challenge_id} {self.status}>"


class ChallengeDayLog(db.Model):
    """Per-day progress snapshot inside a UserChallenge. One row per
    (user_challenge, date) — Step 7 upserts the same row when a day gets a
    second qualifying log, which is what keeps progress idempotent."""

    id = db.Column(db.Integer, primary_key=True)
    user_challenge_id = db.Column(db.Integer, db.ForeignKey("user_challenge.id"), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False)
    value = db.Column(db.Float, nullable=False, default=0)

    __table_args__ = (db.UniqueConstraint("user_challenge_id", "date", name="uq_user_challenge_day"),)

    def __repr__(self) -> str:
        return f"<ChallengeDayLog uc={self.user_challenge_id} {self.date} {self.value}>"


class UserBadge(db.Model):
    """A badge a user has earned. Unique per (user, badge) — completing the
    same challenge twice can't double-award."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    badge_id = db.Column(db.Integer, db.ForeignKey("badge.id"), nullable=False)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenge.id"))
    awarded_at = db.Column(db.DateTime)  # user wall clock, stamped on insert

    user = db.relationship("User", backref="badges")
    badge = db.relationship("Badge")

    __table_args__ = (db.UniqueConstraint("user_id", "badge_id", name="uq_user_badge"),)

    def __repr__(self) -> str:
        return f"<UserBadge user={self.user_id} badge={self.badge_id}>"


class AuditLog(db.Model):
    """One row per admin write action (app/admin.py: log_admin_action).
    Timestamps are UTC — admin data is global, unlike user data which uses
    the user's wall clock (app/timeutil.py). admin_id is nullable: deleting
    an admin user drops the attribution instead of the audit history."""

    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    action = db.Column(db.String(80), nullable=False)  # short stable verb, e.g. "user.suspend"
    target = db.Column(db.String(200), nullable=False, default="")
    timestamp = db.Column(db.DateTime, nullable=False, index=True, default=datetime.utcnow)

    admin = db.relationship("User", backref="audit_logs")

    def __repr__(self) -> str:
        return f"<AuditLog admin={self.admin_id} {self.action} {self.target}>"


@login_manager.user_loader
def load_user(user_id: str):
    try:
        user = db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None
    # Suspended/banned users lose their session everywhere, not just at the
    # login form — this is the single choke point every request flows through.
    if user is not None and user.status != "active":
        return None
    return user


# Columns stamped with the user's wall clock when a write leaves them empty.
_LOCAL_NOW_COLUMNS = {
    WeightLog: "date",
    ScheduledWorkout: "created_at",
    CoachMessage: "created_at",
    MealEntry: "logged_at",
    WaterLog: "logged_at",
    PushSubscription: "created_at",
    UserChallenge: "joined_at",
    UserBadge: "awarded_at",
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
