from secrets import compare_digest, token_urlsafe

from flask import abort, request, session


def generate_csrf_token() -> str:
    token = session.get("_csrf_token")
    if token is None:
        token = token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def validate_csrf_request() -> None:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return

    session_token = session.get("_csrf_token")
    request_token = request.form.get("csrf_token") or request.headers.get("X-CSRFToken")

    if not session_token or not request_token or not compare_digest(request_token, session_token):
        abort(400, description="Invalid CSRF token.")
