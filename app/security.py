from secrets import compare_digest, token_urlsafe

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
