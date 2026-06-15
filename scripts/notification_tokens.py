"""
scripts/notification_tokens.py — Short-lived HMAC-signed tokens for email action links.

Token format (URL-safe base64 of JSON):
  { user_id, app_id, action, payload, expires_at }

Actions:
  "status"   — set application status to payload["status"]
                optional payload["date_applied"] for Applied transitions
  "snooze"   — suppress notifications for this app until payload["until"] (ISO string)

Tokens are stateless (no S3 write) — signed with NOTIFICATION_TOKEN_SECRET env var,
falling back to SESSION_SECRET. TTL is 7 days by default.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

TOKEN_TTL_SECS = 7 * 24 * 3600  # 7 days


def _secret() -> bytes:
    s = os.environ.get("NOTIFICATION_TOKEN_SECRET") or os.environ.get("SESSION_SECRET", "")
    if not s:
        raise RuntimeError("Neither NOTIFICATION_TOKEN_SECRET nor SESSION_SECRET is set")
    return s.encode()


def _sign(payload_b64: str) -> str:
    return hmac.new(_secret(), payload_b64.encode(), hashlib.sha256).hexdigest()


def create_token(
    user_id: str,
    app_id: str,
    action: str,
    payload: dict[str, Any] | None = None,
    ttl: int = TOKEN_TTL_SECS,
) -> str:
    """Return a signed URL-safe token string."""
    data = {
        "user_id":    user_id,
        "app_id":     app_id,
        "action":     action,
        "payload":    payload or {},
        "expires_at": int(time.time()) + ttl,
    }
    b64 = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
    sig = _sign(b64)
    return f"{b64}.{sig}"


def verify_token(token: str) -> dict[str, Any] | None:
    """Verify signature and expiry. Returns the inner data dict or None."""
    try:
        b64, sig = token.rsplit(".", 1)
    except ValueError:
        return None

    expected = _sign(b64)
    if not hmac.compare_digest(expected, sig):
        return None

    try:
        data = json.loads(base64.urlsafe_b64decode(b64 + "=="))
    except Exception:
        return None

    if data.get("expires_at", 0) < time.time():
        return None

    return data
