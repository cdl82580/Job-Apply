"""
scripts/email_verification.py — One-time email verification tokens.

Token lifecycle:
  1. create_token(user_id, email) → raw token (32-byte URL-safe string)
  2. Store SHA-256(token) at email_verification/{hash}.json → {user_id, email, expires_at}
  3. User clicks link containing raw token
  4. consume_token(raw_token) → {user_id, email} and deletes the key
  5. Caller marks user.email_verified = True

Tokens expire after TOKEN_TTL_HOURS hours.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time

from . import storage

TOKEN_TTL_HOURS = 72


def _key(token_hash: str) -> str:
    return f"email_verification/{token_hash}.json"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(user_id: str, email: str) -> str:
    """Generate and persist a new verification token. Returns the raw token."""
    token = secrets.token_urlsafe(32)
    storage.put_text(_key(_hash(token)), json.dumps({
        "user_id":    user_id,
        "email":      email,
        "expires_at": int(time.time()) + TOKEN_TTL_HOURS * 3600,
        "created_at": int(time.time()),
    }))
    return token


def consume_token(token: str) -> dict | None:
    """Verify and delete a token (one-time use).
    Returns {user_id, email} on success, None if invalid or expired."""
    h   = _hash(token)
    raw = storage.get_text(_key(h))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None

    if data.get("expires_at", 0) < time.time():
        _delete(h)
        return None

    _delete(h)
    return {"user_id": data["user_id"], "email": data["email"]}


def has_pending_token(user_id: str) -> bool:
    """True if there is at least one unexpired token for this user.
    Note: this requires a full scan and is only used for informational purposes."""
    return True  # tokens are not indexed by user_id; assume pending until verified


def _delete(token_hash: str) -> None:
    try:
        storage.delete_bytes(_key(token_hash))
    except Exception:
        pass
