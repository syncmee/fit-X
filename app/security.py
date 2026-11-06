import threading
from secrets import compare_digest, token_urlsafe
from time import monotonic

from flask import flash, redirect, request, session, url_for


def generate_csrf_token() -> str:
    token = session.get("_csrf_token")
    if token is None:
        token = token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def generate_idempotency_key() -> str:
    """Return an opaque, one-use token for a state-changing HTML form."""
    return token_urlsafe(32)


def validate_csrf_request() -> None:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return

    session_token = session.get("_csrf_token")
    request_token = request.form.get("csrf_token") or request.headers.get("X-CSRFToken")

    if not session_token or not request_token or not compare_digest(request_token, session_token):
        # An expired iOS PWA session used to produce an opaque 400. Starting a
        # new login flow is actionable and generates a fresh token.
        flash("Your session expired. Please sign in again and retry.", "warning")
        return redirect(url_for("main.login"))


# Per-email login failure counting. This complements the IP-based limiter on
# POST /login: an attacker cycling IPs still trips a per-account counter, while
# one user's typos do not lock an IP shared by an office. Counters are
# per-process (each Gunicorn worker counts independently), so the effective
# limit is limits-per-worker; sharing them through Redis can come later if
# credential-stuffing becomes a real signal.
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60

_login_failures: dict[str, list[float]] = {}
_login_failure_lock = threading.Lock()


def _failure_key(email: str) -> str:
    return (email or "").strip().lower()


def login_blocked_for_email(email: str) -> bool:
    """True when this email recently accumulated too many failed attempts."""
    now = monotonic()
    key = _failure_key(email)
    with _login_failure_lock:
        attempts = _login_failures.get(key)
        if not attempts:
            return False
        while attempts and now - attempts[0] > LOGIN_FAILURE_WINDOW_SECONDS:
            attempts.pop(0)
        if not attempts:
            del _login_failures[key]
            return False
        return len(attempts) >= LOGIN_FAILURE_LIMIT


def record_login_failure(email: str) -> None:
    now = monotonic()
    key = _failure_key(email)
    with _login_failure_lock:
        attempts = _login_failures.setdefault(key, [])
        attempts.append(now)
        while attempts and now - attempts[0] > LOGIN_FAILURE_WINDOW_SECONDS:
            attempts.pop(0)


def clear_login_failures(email: str) -> None:
    with _login_failure_lock:
        _login_failures.pop(_failure_key(email), None)
