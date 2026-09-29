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
    # SEO canonical origin (see app/seo.py) — set SITE_URL when a custom domain
    # takes over; canonical/OG/schema URLs and the sitemap all build from it.
    SITE_URL = os.getenv("SITE_URL", "https://fitness-app-b0wl.onrender.com").rstrip("/")
    # Static assets change rarely; let browsers cache them instead of
    # revalidating on every visit. sw.js update checks cap at 24h regardless.
    SEND_FILE_MAX_AGE_DEFAULT = 60 * 60 * 24 * 30
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
    SESSION_COOKIE_SECURE = os.getenv("FLASK_ENV", "development").lower() == "production"
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    # fiT-X AI coach (Google AI Studio / Gemini). Leave GEMINI_API_KEY empty to
    # fall back to the built-in rule-based coach.
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    # Secondary AI coach provider (Groq, OpenAI-compatible API). Used when the
    # Gemini call fails (overloaded model, quota, outage). Leave empty to skip.
    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    # Workout reminder emails via SMTP (Brevo relay, Gmail, or any provider).
    # Brevo: verify the sender email, generate an SMTP key at app.brevo.com, and
    # use smtp-relay.brevo.com port 2525 from Render (free Render blocks 587).
    # Leave SMTP_USER empty to run the reminder cron in dry-run (logged, not sent).
    SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER = os.getenv("SMTP_USER", "")
    # Google shows app passwords grouped in fours ("abcd efgh ijkl mnop");
    # accept either form.
    SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "").replace(" ", "")
    SMTP_FROM = os.getenv("SMTP_FROM", "")
    # Google OAuth (sign in with Google). Client creds from Google Cloud
    # Console; leave empty to hide/disable the Google button flow.
    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
    # Shared secret between the scheduler (GitHub Actions / Render Cron) and
    # POST/GET /cron/send-reminders. Requests without it are rejected.
    CRON_SECRET = os.getenv("CRON_SECRET", "")
    # Web push (workout reminders): VAPID identity. Generate once with
    # cryptography (P-256, base64url raw scalars). Empty = push disabled and
    # the dashboard hides its enable button.
    VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
    VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
    # Contact required in VAPID claims; falls back to the SMTP sender.
    VAPID_CONTACT = os.getenv("VAPID_CONTACT", "mailto:coach.fitx.app@gmail.com")
