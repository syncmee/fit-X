"""Web-push delivery (VAPID) for workout and engagement reminders.

Dependencies are looked up late (app config, db session) so importing this
module stays side-effect free.
"""
from __future__ import annotations

import json
from datetime import datetime

from flask import current_app

from app.extensions import db
from app.models import User


def vapid_auth_header(endpoint: str, private_key: str) -> str:
    """Build the VAPID Authorization header for a push endpoint.

    py_vapid's signer emits tokens that Apple (403 BadJwtToken) and Windows
    (401) reject while Google's legacy endpoint tolerates them, so the ES256
    JWS is built here directly: header {typ, alg}, payload {aud, exp, sub},
    raw r||s signature over "header.payload"."""
    import base64
    from urllib.parse import urlparse

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    if private_key.startswith("-----BEGIN"):
        priv = serialization.load_pem_private_key(private_key.encode(), password=None)
    else:
        pad = "=" * (-len(private_key) % 4)
        raw = base64.urlsafe_b64decode(private_key + pad)
        priv = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
    pub_b64 = base64.urlsafe_b64encode(
        priv.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
    ).rstrip(b"=").decode()

    parsed = urlparse(endpoint)
    header = base64.urlsafe_b64encode(
        json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "aud": f"{parsed.scheme}://{parsed.netloc}",
                "exp": int(datetime.utcnow().timestamp()) + 12 * 3600,
                "sub": current_app.config.get("VAPID_CONTACT", "mailto:admin@fitx.app"),
            },
            separators=(",", ":"),
        ).encode()
    ).rstrip(b"=").decode()
    der_sig = priv.sign(f"{header}.{payload}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_sig)
    sig = base64.urlsafe_b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).rstrip(b"=").decode()
    return f"vapid t={header}.{payload}.{sig}, k={pub_b64}"


def send_web_push(user: User, *, title: str, body: str, url: str = "/dashboard", tag: str | None = None) -> bool:
    """Fire a web push to every subscribed device. Returns True when at least
    one device accepted. Dead subscriptions (404/410 — expired or permission
    revoked) are removed so they never retry. The notification tag scopes
    replacement on a device: same tag = replaces (one reminder per workout),
    different tags stack side by side."""
    private_key = current_app.config.get("VAPID_PRIVATE_KEY", "")
    if not (private_key and user.push_subscriptions):
        return False
    sent_any = False
    try:
        from pywebpush import WebPusher

        for sub in user.push_subscriptions:
            try:
                payload = {"title": title, "body": body, "url": url}
                if tag:
                    payload["tag"] = tag
                wp = WebPusher({"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}})
                resp = wp.send(
                    json.dumps(payload),
                    headers={
                        "Authorization": vapid_auth_header(sub.endpoint, private_key),
                        "TTL": "600",
                    },
                )
                if resp.status_code in (404, 410):
                    db.session.delete(sub)
                elif not 200 <= resp.status_code < 300:
                    current_app.logger.warning(
                        "web push to user %s failed: %s %s",
                        user.id,
                        resp.status_code,
                        resp.text[:120],
                    )
                else:
                    sent_any = True
            except Exception as exc:
                # 404/410 = expired or revoked; anything else (network error,
                # malformed endpoint) just skips this device.
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (404, 410):
                    db.session.delete(sub)
                else:
                    current_app.logger.warning("web push to user %s failed: %s", user.id, exc)
    except ImportError:
        current_app.logger.warning("pywebpush unavailable; push skipped")
    return sent_any
