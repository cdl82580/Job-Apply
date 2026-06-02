"""
scripts/applications.py — Tigris S3 storage for job application tracking.

Key layout:
  applications/{user_id}/{app_id}.json   — full application record
  applications/{user_id}/_index.json     — summary list for fast listing
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _app_key(user_id: str, app_id: str) -> str:
    return f"applications/{user_id}/{app_id}.json"


def _index_key(user_id: str) -> str:
    return f"applications/{user_id}/_index.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


_INDEX_FIELDS = {
    "id", "company", "domain", "company_logo_url", "role_title",
    "status", "date_applied", "last_updated", "created_at", "priority", "dua",
}


def _to_index_entry(record: dict[str, Any]) -> dict[str, Any]:
    return {k: record[k] for k in _INDEX_FIELDS if k in record}


# ---------------------------------------------------------------------------
# Index operations
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
    entry = _to_index_entry(record)
    for i, e in enumerate(entries):
        if e["id"] == record["id"]:
            entries[i] = entry
            break
    else:
        entries.append(entry)
    _write_index(user_id, entries)


def _remove_from_index(user_id: str, app_id: str) -> None:
    entries = _read_index(user_id)
    entries = [e for e in entries if e["id"] != app_id]
    _write_index(user_id, entries)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_applications(
    user_id: str,
    status: str | None = None,
    priority: str | None = None,
) -> list[dict[str, Any]]:
    entries = _read_index(user_id)
    if status:
        entries = [e for e in entries if e.get("status") == status]
    if priority:
        entries = [e for e in entries if e.get("priority") == priority]
    return sorted(entries, key=lambda e: e.get("last_updated", ""), reverse=True)


def get_application(user_id: str, app_id: str) -> dict[str, Any] | None:
    raw = storage.get_text(_app_key(user_id, app_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def save_application(user_id: str, record: dict[str, Any]) -> None:
    record["last_updated"] = _now()
    storage.put_text(_app_key(user_id, record["id"]), json.dumps(record))
    _upsert_index(user_id, record)


def link_run(user_id: str, app_id: str, run_info: dict[str, Any]) -> dict[str, Any] | None:
    """Append a run link to an application. Returns updated record or None if not found."""
    record = get_application(user_id, app_id)
    if not record:
        return None
    record.setdefault("linked_runs", []).append(run_info)
    save_application(user_id, record)
    return record


def unlink_run(user_id: str, app_id: str, link_id: str) -> bool:
    """Remove a run link from an application. Returns True if removed."""
    record = get_application(user_id, app_id)
    if not record:
        return False
    before = len(record.get("linked_runs", []))
    record["linked_runs"] = [r for r in record.get("linked_runs", []) if r["id"] != link_id]
    if len(record["linked_runs"]) == before:
        return False
    save_application(user_id, record)
    return True


def save_deleted_tombstone(user_id: str, record: dict[str, Any]) -> None:
    """Persist a deleted application under a separate key for audit purposes."""
    key = f"applications/{user_id}/_deleted/{record['id']}.json"
    storage.put_text(key, json.dumps(record))


def delete_application(user_id: str, app_id: str) -> bool:
    if not storage.exists(_app_key(user_id, app_id)):
        return False
    try:
        storage._client().delete_object(Bucket=storage.BUCKET, Key=_app_key(user_id, app_id))
    except Exception:
        return False
    _remove_from_index(user_id, app_id)
    return True
