"""
scripts/user_audit.py — Append-only user action audit log.

Key layout (current — per-event files):
  audit/users/{user_id}/events/{timestamp_ms}-{uuid}.json  — one file per event
  audit/login_failures/{email_sha256}/{timestamp_ms}-{uuid}.json

Legacy key (read-only backward compat):
  audit/users/{user_id}/events.json          — old monolithic array
  audit/login_failures/{email_sha256}.json

Writing individual objects per event makes appends atomic and concurrent-safe
across multiple Fly.io machines (no read-modify-write race).

Event shape:
  {
    "id":        "<uuid>",
    "action":    "<verb>",
    "actor":     "<email or user_id>",
    "timestamp": "<ISO-8601 UTC>",
    "ip":        "<client IP or null>",
    "details":   { ... }
  }
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from typing import Any

from . import storage

# _audit_locks guards the _sweep of the legacy monolithic file (one-time migration).
# Individual per-event writes are atomic (put_object) and need no lock.
_audit_locks: dict[str, threading.Lock] = {}
_audit_locks_mu = threading.Lock()
# Track which legacy files have already been migrated this process lifetime
# so we don't re-scan on every log() call.
_migrated: set[str] = set()
_migrated_mu = threading.Lock()

_MAX_EVENTS = 500  # cap returned to callers; storage is unbounded (cheap S3 objects)


def _audit_lock(key: str) -> threading.Lock:
    with _audit_locks_mu:
        if key not in _audit_locks:
            _audit_locks[key] = threading.Lock()
        lk = _audit_locks[key]
    # Prune stale locks periodically to prevent unbounded growth
    if len(_audit_locks) > 200:
        with _audit_locks_mu:
            # Keep only locks that are currently held (in-use); discard the rest
            _audit_locks.clear()
            _audit_locks[key] = lk
    return lk


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------

def _event_key(user_id: str, event_id: str, ts_ms: int) -> str:
    return f"audit/users/{user_id}/events/{ts_ms:016d}-{event_id}.json"


def _events_prefix(user_id: str) -> str:
    return f"audit/users/{user_id}/events/"


def _legacy_events_key(user_id: str) -> str:
    return f"audit/users/{user_id}/events.json"


def _failure_event_key(email: str, event_id: str, ts_ms: int) -> str:
    h = hashlib.sha256(email.strip().lower().encode()).hexdigest()
    return f"audit/login_failures/{h}/{ts_ms:016d}-{event_id}.json"


def _failure_prefix(email: str) -> str:
    h = hashlib.sha256(email.strip().lower().encode()).hexdigest()
    return f"audit/login_failures/{h}/"


def _legacy_failure_key(email: str) -> str:
    h = hashlib.sha256(email.strip().lower().encode()).hexdigest()
    return f"audit/login_failures/{h}.json"


# ---------------------------------------------------------------------------
# Write (atomic per-event put)
# ---------------------------------------------------------------------------

def _write_event(key: str, event: dict[str, Any]) -> None:
    storage.put_text(key, json.dumps(event))


# ---------------------------------------------------------------------------
# Read (merge legacy + per-event files, newest first, capped)
# ---------------------------------------------------------------------------

def _read_legacy(legacy_key: str) -> list[dict[str, Any]]:
    raw = storage.get_text(legacy_key)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _read_all_events(prefix: str, legacy_key: str) -> list[dict[str, Any]]:
    """Return all events newest-first, merging legacy monolithic file and per-event files."""
    per_event: list[dict[str, Any]] = []
    for key in sorted(storage.list_keys(prefix), reverse=True)[:_MAX_EVENTS]:
        raw = storage.get_text(key)
        if not raw:
            continue
        try:
            per_event.append(json.loads(raw))
        except Exception:
            pass

    legacy = _read_legacy(legacy_key)

    # Merge: combine, deduplicate by id, sort newest-first, cap
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for ev in per_event + list(reversed(legacy)):
        eid = ev.get("id", "")
        if eid and eid in seen:
            continue
        seen.add(eid)
        merged.append(ev)

    # Sort by timestamp descending (ISO strings sort lexicographically)
    merged.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return merged[:_MAX_EVENTS]


# ---------------------------------------------------------------------------
# Build event dict
# ---------------------------------------------------------------------------

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

_UNVERIFIED_ACTIONS = frozenset({"user_registered", "user_registered_google"})


def log(user_id: str, action: str, actor: str, ip: str | None = None, **details: Any) -> None:
    """Append an event to a user's audit log and dispatch to any matching webhooks."""
    if not storage.is_configured():
        return
    event = _build(action, actor, ip, details or None)
    ts_ms = int(time.time() * 1000)
    key   = _event_key(user_id, event["id"], ts_ms)
    try:
        _write_event(key, event)
    except Exception:
        pass  # never let audit failure break the request
    from . import cache  # noqa: PLC0415
    cache.invalidate(f"audit_events:{user_id}")
    # Dispatch to webhooks asynchronously (best-effort, never raises)
    try:
        from . import webhooks  # noqa: PLC0415 — lazy to avoid circular import
        payload = {"user_id": user_id, "user_email": actor, **event}
        if action in _UNVERIFIED_ACTIONS:
            payload["email_verified"] = False
        webhooks.dispatch_async(payload)
    except Exception:
        pass


def log_login_failure(email: str, ip: str | None = None) -> None:
    """Record a failed login attempt keyed by email hash (no user_id known)."""
    if not storage.is_configured():
        return
    event = _build("login_failed", email, ip, {"email": email})
    ts_ms = int(time.time() * 1000)
    key   = _failure_event_key(email, event["id"], ts_ms)
    try:
        _write_event(key, event)
    except Exception:
        pass


def get_events(user_id: str) -> list[dict[str, Any]]:
    """Return audit events for a user, newest first (capped at _MAX_EVENTS)."""
    from . import cache
    key = f"audit_events:{user_id}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    result = _read_all_events(_events_prefix(user_id), _legacy_events_key(user_id))
    return cache.put(key, result)


def get_last_login(user_id: str) -> str | None:
    """Return ISO timestamp of the most recent login event, or None."""
    for event in get_events(user_id):
        if event.get("action") in ("login_success", "login_google"):
            return event.get("timestamp")
    return None
