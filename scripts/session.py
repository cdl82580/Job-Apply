"""
scripts/session.py — Shared session token helpers.

Keeps the HMAC token format in one place so api.py and auth_google.py
both use the same implementation.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


SESSION_DAYS = 30


def create_session_token(user_id: str, email: str, secret: str) -> str:
    """Return a signed session token string."""
    payload = base64.urlsafe_b64encode(json.dumps({
        "user_id": user_id,
        "email":   email,
        "exp":     int(time.time()) + 86400 * SESSION_DAYS,
    }).encode()).rstrip(b"=").decode()
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session_token(token: str, secret: str) -> dict | None:
    """Verify and decode a session token. Returns payload dict or None."""
    try:
        payload_b64, sig = token.rsplit(".", 1)
        expected = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        padding = 4 - len(payload_b64) % 4
        data = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * padding))
        if data.get("exp", 0) < time.time():
            return None
        return data
    except Exception:
        return None
