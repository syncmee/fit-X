import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"
DEFAULT_SQLITE_PATH = BASE_DIR / "instance" / "userdata.db"

load_dotenv(ENV_FILE)

# Some hosts (Render, older Heroku-style tooling) hand out postgres:// URIs;
# SQLAlchemy's psycopg2 dialect needs postgresql://.
_database_url = os.getenv("DATABASE_URL")
if _database_url and _database_url.startswith("postgres://"):
    _database_url = _database_url.replace("postgres://", "postgresql://", 1)


class Config:
    DEBUG = os.getenv("FLASK_ENV", "development").lower() == "development"
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-me")
    SQLALCHEMY_DATABASE_URI = _database_url or f"sqlite:///{DEFAULT_SQLITE_PATH.as_posix()}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
    SESSION_COOKIE_SECURE = os.getenv("FLASK_ENV", "development").lower() == "production"
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    # fiT-X AI coach (Google AI Studio / Gemini). Leave GEMINI_API_KEY empty to
    # fall back to the built-in rule-based coach.
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    # Workout reminder emails. Sending priority: Gmail/SMTP -> Resend -> dry-run
    # (logged, not sent). For Gmail: enable 2-Step Verification on the account,
    # then create an App Password at https://myaccount.google.com/apppasswords —
    # the normal login password will NOT work for SMTP.
    SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER = os.getenv("SMTP_USER", "")
    SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "")
    SMTP_FROM = os.getenv("SMTP_FROM", "")
    RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
    RESEND_FROM = os.getenv("RESEND_FROM", "fiT-X <onboarding@resend.dev>")
    # Shared secret between the scheduler (GitHub Actions / Render Cron) and
    # POST/GET /cron/send-reminders. Requests without it are rejected.
    CRON_SECRET = os.getenv("CRON_SECRET", "")
