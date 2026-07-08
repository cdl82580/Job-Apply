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
import os
import secrets
import time


SESSION_DAYS = 30

_resolved_secret: str | None = None


def resolve_session_secret() -> str:
    """Return SESSION_SECRET from the environment.

    Memoized so that if the env var is unset, every caller in this process
    (api.py, routers/auth_google.py) agrees on the same random fallback
    instead of each independently generating its own — which would silently
    make tokens signed by one unverifiable by the other."""
    global _resolved_secret
    if _resolved_secret is None:
        _resolved_secret = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
    return _resolved_secret


def verify_bot_key(auth_header: str, expected_key: str) -> bool:
    """Verify a Bearer token against a shared bot API key (constant-time)."""
    if not expected_key or not auth_header.startswith("Bearer "):
        return False
    return hmac.compare_digest(auth_header[7:], expected_key)


def pw_version(password_hash: str) -> str:
    """Return a short fingerprint of the stored password hash.
    Embedded in session tokens so a password change immediately invalidates
    all tokens that carry the old fingerprint."""
    return hashlib.sha256(password_hash.encode()).hexdigest()[:8]


def create_session_token(
    user_id: str,
    email: str,
    secret: str,
    role: str = "user",
    password_hash: str = "",
) -> str:
    """Return a signed session token string."""
    payload_data: dict = {
        "user_id": user_id,
        "email":   email,
        "role":    role,
        "exp":     int(time.time()) + 86400 * SESSION_DAYS,
    }
    if password_hash:
        payload_data["pwv"] = pw_version(password_hash)
    payload = base64.urlsafe_b64encode(json.dumps(payload_data).encode()).rstrip(b"=").decode()
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session_token(token: str, secret: str) -> dict | None:
    """Verify and decode a session token. Returns payload dict or None.
    Tokens issued before the role field existed default to role='user'."""
    try:
        payload_b64, sig = token.rsplit(".", 1)
        expected = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        padding = 4 - len(payload_b64) % 4
        data = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * padding))
        if data.get("exp", 0) < time.time():
            return None
        data.setdefault("role", "user")   # backward-compat with pre-role tokens
        return data
    except Exception:
        return None
