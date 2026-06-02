"""
scripts/user_audit.py — Append-only user action audit log.

Key layout:
  audit/users/{user_id}/events.json          — per-user event array
  audit/login_failures/{email_sha256}.json   — failed login attempts (no user_id available)

Event shape:
  {
    "id":        "<uuid>",
    "action":    "<verb>",
    "actor":     "<email or user_id>",
    "timestamp": "<ISO-8601 UTC>",
    "ip":        "<client IP or null>",
    "details":   { ... }   # action-specific payload
  }
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from . import storage


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _events_key(user_id: str) -> str:
    return f"audit/users/{user_id}/events.json"


def _failure_key(email: str) -> str:
    h = hashlib.sha256(email.strip().lower().encode()).hexdigest()
    return f"audit/login_failures/{h}.json"


def _read_events(key: str) -> list[dict[str, Any]]:
    raw = storage.get_text(key)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _append(key: str, event: dict[str, Any]) -> None:
    events = _read_events(key)
    events.append(event)
    storage.put_text(key, json.dumps(events))


def _build(action: str, actor: str, ip: str | None, details: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "id":        str(uuid.uuid4()),
        "action":    action,
        "actor":     actor,
        "timestamp": _now(),
        "ip":        ip,
        "details":   details or {},
    }


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def log(user_id: str, action: str, actor: str, ip: str | None = None, **details: Any) -> None:
    """Append an event to a user's audit log."""
    if not storage.is_configured():
        return
    event = _build(action, actor, ip, details or None)
    try:
        _append(_events_key(user_id), event)
    except Exception:
        pass  # never let audit failure break the request


def log_login_failure(email: str, ip: str | None = None) -> None:
    """Record a failed login attempt keyed by email hash (no user_id known)."""
    if not storage.is_configured():
        return
    event = _build("login_failed", email, ip, {"email": email})
    try:
        _append(_failure_key(email), event)
    except Exception:
        pass


def get_events(user_id: str) -> list[dict[str, Any]]:
    """Return all audit events for a user, newest first."""
    events = _read_events(_events_key(user_id))
    return list(reversed(events))
