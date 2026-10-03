"""Shared test plumbing: hermetic app instance + helpers."""
import re
import tempfile
import unittest
from datetime import datetime

from app.config import Config


def make_config() -> type[Config]:
    class TestConfig(Config):
        SQLALCHEMY_DATABASE_URI = "sqlite:///" + tempfile.mktemp(suffix=".db").replace("\\", "/")
        SECRET_KEY = "test-secret"
        TESTING = True

    return TestConfig


def make_app():
    from app import create_app

    return create_app(make_config())


def make_user(app, name="joe", email=None, admin=False, **fields):
    """Create a user (with profile + password) inside an app context."""
    from app.extensions import db
    from app.models import User

    with app.app_context():
        user = User(
            name=name,
            email=email or f"{name}@test.dev",
            is_admin=admin,
            onboarding=True,
            status="active",
            created_at=datetime.utcnow(),
            **fields,
        )
        user.set_password("pw123456")
        db.session.add(user)
        db.session.commit()
        return user.id


def login(client, app, email):
    from app.extensions import db
    from app.models import User
    from sqlalchemy import select

    client.get("/logout")
    with app.app_context():
        user = db.session.scalars(select(User).where(User.email == email)).one()
        user.set_password("pw123456")
        db.session.commit()
    html = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html).group(1)
    return client.post("/login", data={"email": email, "password": "pw123456", "csrf_token": token})


def admin_client(app):
    """Client logged in as an admin user (created if missing)."""
    make_user(app, name="adminuser", email="admin@test.dev", admin=True)
    client = app.test_client()
    login(client, app, "admin@test.dev")
    return client


class AppContextTestCase(unittest.TestCase):
    """Base for tests that touch the DB directly: pushes an application
    context for every test method."""

    def setUp(self):
        self.app = make_app()
        from app.extensions import db

        with self.app.app_context():
            db.create_all()
        self._ctx = self.app.app_context()
        self._ctx.push()

    def tearDown(self):
        from app.extensions import db

        db.session.remove()
        self._ctx.pop()


def csrf_token(client, path="/admin/users"):
    html = client.get(path).get_data(as_text=True)
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    assert match, f"no csrf token on {path}"
    return match.group(1)


def post(client, url, data, token_path="/admin/users"):
    return client.post(url, data={**data, "csrf_token": csrf_token(client, token_path)})
