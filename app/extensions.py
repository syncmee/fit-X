from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
except ImportError:  # pragma: no cover - local imports remain usable before install
    Limiter = None
    get_remote_address = None

try:
    from flask_migrate import Migrate
except ImportError:  # pragma: no cover - graceful fallback for environments without Flask-Migrate
    Migrate = None

try:
    from authlib.integrations.flask_client import OAuth
except ImportError:  # pragma: no cover - Google sign-in stays disabled without authlib
    OAuth = None


db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "main.login"
login_manager.login_message = "Please sign in to continue."
login_manager.login_message_category = "warning"
migrate = Migrate() if Migrate is not None else None
oauth = OAuth() if OAuth is not None else None
# No storage_uri here: Flask-Limiter reads RATELIMIT_STORAGE_URI from app
# config, so Render's Redis URI takes effect and memory:// remains the local
# fallback via Config's default.
limiter = Limiter(key_func=get_remote_address, default_limits=[]) if Limiter else None
