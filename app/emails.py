"""SMTP delivery primitive for all fiT-X email notifications."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from flask import current_app


def send_via_smtp(user, subject: str, plain: str, html: str, smtp_user: str, smtp_password: str) -> tuple[bool, str]:
    """Send multipart mail; callers decide whether failed work is retried."""
    msg = EmailMessage()
    display_from = current_app.config.get("SMTP_FROM") or smtp_user
    msg["From"] = formataddr(("fiT-X Coach", display_from))
    msg["To"] = user.email
    msg["Subject"] = subject
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(current_app.config.get("SMTP_HOST", "smtp.gmail.com"), int(current_app.config.get("SMTP_PORT", "587")), timeout=25) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        current_app.logger.error("SMTP send to %s failed: %s", user.email, exc)
        return False, "smtp error"
    return True, f"sent via {smtp_user}"
