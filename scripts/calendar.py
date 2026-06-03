"""
scripts/calendar.py — Tigris S3 storage for calendar events and reminders.

Key layout:
  calendar/{user_id}/{event_id}.json    — full event record
  calendar/{user_id}/_index.json        — summary list for fast listing
  reminders/{user_id}/{reminder_id}.json — reminder fire records
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from . import storage

_user_locks: dict[str, threading.Lock] = {}
_user_locks_mu = threading.Lock()


def _user_lock(user_id: str) -> threading.Lock:
    with _user_locks_mu:
        if user_id not in _user_locks:
            _user_locks[user_id] = threading.Lock()
        return _user_locks[user_id]


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------

def _event_key(user_id: str, event_id: str) -> str:
    return f"calendar/{user_id}/{event_id}.json"


def _index_key(user_id: str) -> str:
    return f"calendar/{user_id}/_index.json"


def _reminder_key(user_id: str, reminder_id: str) -> str:
    return f"reminders/{user_id}/{reminder_id}.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# Fields mirrored into the index for fast listing
_INDEX_FIELDS = {
    "id", "title", "event_type", "datetime", "timezone",
    "duration_minutes", "app_id", "created_at", "updated_at",
}

VALID_EVENT_TYPES = {
    "interview", "phone_screen", "deadline", "follow_up",
    "offer_deadline", "prep", "custom",
}

# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

def _read_index(user_id: str) -> list[dict[str, Any]]:
    raw = storage.get_text(_index_key(user_id))
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _write_index(user_id: str, entries: list[dict[str, Any]]) -> None:
    storage.put_text(_index_key(user_id), json.dumps(entries))


def _upsert_index(user_id: str, record: dict[str, Any]) -> None:
    entries = _read_index(user_id)
    entry = {k: record[k] for k in _INDEX_FIELDS if k in record}
    for i, e in enumerate(entries):
        if e["id"] == record["id"]:
            entries[i] = entry
            _write_index(user_id, entries)
            return
    entries.append(entry)
    _write_index(user_id, entries)


def _remove_from_index(user_id: str, event_id: str) -> None:
    entries = _read_index(user_id)
    entries = [e for e in entries if e["id"] != event_id]
    _write_index(user_id, entries)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_events(user_id: str, from_dt: str | None = None, to_dt: str | None = None) -> list[dict[str, Any]]:
    """Return index entries, optionally bounded by ISO datetime strings."""
    entries = _read_index(user_id)
    if from_dt:
        entries = [e for e in entries if e.get("datetime", "") >= from_dt]
    if to_dt:
        entries = [e for e in entries if e.get("datetime", "") <= to_dt]
    return sorted(entries, key=lambda e: e.get("datetime", ""))


def get_event(user_id: str, event_id: str) -> dict[str, Any] | None:
    raw = storage.get_text(_event_key(user_id, event_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def create_event(user_id: str, data: dict[str, Any]) -> dict[str, Any]:
    with _user_lock(user_id):
        record = {**data, "created_at": _now(), "updated_at": _now()}
        storage.put_text(_event_key(user_id, record["id"]), json.dumps(record))
        _upsert_index(user_id, record)
    return record


def update_event(user_id: str, event_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    with _user_lock(user_id):
        record = get_event(user_id, event_id)
        if not record:
            return None
        record.update(updates)
        record["updated_at"] = _now()
        storage.put_text(_event_key(user_id, event_id), json.dumps(record))
        _upsert_index(user_id, record)
    return record


def delete_event(user_id: str, event_id: str) -> bool:
    with _user_lock(user_id):
        key = _event_key(user_id, event_id)
        try:
            storage.delete_text(key)
        except Exception:
            return False
        _remove_from_index(user_id, event_id)
    return True


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

def save_reminder(user_id: str, reminder: dict[str, Any]) -> None:
    storage.put_text(_reminder_key(user_id, reminder["id"]), json.dumps(reminder))


def get_reminder(user_id: str, reminder_id: str) -> dict[str, Any] | None:
    raw = storage.get_text(_reminder_key(user_id, reminder_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def delete_reminder(user_id: str, reminder_id: str) -> None:
    try:
        storage.delete_text(_reminder_key(user_id, reminder_id))
    except Exception:
        pass


def list_due_reminders(user_id: str) -> list[dict[str, Any]]:
    """List unsent reminders with fire_at <= now for one user."""
    now_epoch = time.time()
    prefix = f"reminders/{user_id}/"
    keys = storage.list_keys(prefix)
    due = []
    for key in keys:
        raw = storage.get_text(key)
        if not raw:
            continue
        try:
            r = json.loads(raw)
        except Exception:
            continue
        if not r.get("sent") and r.get("fire_at", 0) <= now_epoch:
            due.append(r)
    return due


def mark_reminder_sent(user_id: str, reminder_id: str) -> None:
    rec = get_reminder(user_id, reminder_id)
    if rec:
        rec["sent"] = True
        rec["sent_at"] = _now()
        save_reminder(user_id, rec)


def delete_event_reminders(user_id: str, event_id: str) -> None:
    """Delete all reminders associated with a specific event."""
    prefix = f"reminders/{user_id}/"
    keys = storage.list_keys(prefix)
    for key in keys:
        raw = storage.get_text(key)
        if not raw:
            continue
        try:
            r = json.loads(raw)
        except Exception:
            continue
        if r.get("event_id") == event_id:
            try:
                storage.delete_text(key)
            except Exception:
                pass


def list_all_user_ids_with_reminders() -> list[str]:
    """Return unique user_ids that have reminder objects (for the scheduler)."""
    keys = storage.list_keys("reminders/")
    user_ids: set[str] = set()
    for key in keys:
        # reminders/{user_id}/{reminder_id}.json
        parts = key.split("/")
        if len(parts) >= 2:
            user_ids.add(parts[1])
    return list(user_ids)
