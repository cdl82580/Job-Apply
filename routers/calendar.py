"""
routers/calendar.py — CRUD endpoints for calendar events.

Endpoints:
  GET    /api/calendar                  list events (optional ?from=&to= ISO range)
  POST   /api/calendar                  create event
  GET    /api/calendar/upcoming         next 7 days (used by Slack home tab)
  GET    /api/calendar/{id}             get single event
  PUT    /api/calendar/{id}             update event
  DELETE /api/calendar/{id}             delete event + its reminders
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

import re as _re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

_UUID_RE = _re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', _re.IGNORECASE)


def _validate_event_id(event_id: str) -> None:
    """Reject event IDs that aren't UUIDs — prevents path traversal in storage keys."""
    if not _UUID_RE.match(event_id):
        raise HTTPException(400, "Invalid event ID format")

from scripts import calendar as cal_store
from scripts import applications as app_store
from scripts import user_audit

router = APIRouter(prefix="/api/calendar", tags=["calendar"])

_MAX_TITLE_LEN   = 200
_MAX_NOTES_LEN   = 5_000
_MAX_EVENTS      = 1_000   # per user
_MAX_REMINDERS   = 10      # per event


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _user_id_from_request(request: Request) -> str:
    """Pull user_id injected by api.py's _require_user."""
    return request.state.user["user_id"]


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ReminderSpec(BaseModel):
    offset_minutes: int      # minutes before event; must be >= 0
    channels: list[str]      # ["email", "slack"] — validated below

    @field_validator("offset_minutes")
    @classmethod
    def _offset_positive(cls, v: int) -> int:
        if v < 0:
            raise ValueError("offset_minutes must be >= 0")
        if v > 525_600:  # 1 year
            raise ValueError("offset_minutes too large (max 1 year)")
        return v

    @field_validator("channels")
    @classmethod
    def _valid_channels(cls, v: list[str]) -> list[str]:
        allowed = {"email", "slack"}
        bad = [c for c in v if c not in allowed]
        if bad:
            raise ValueError(f"Invalid channels: {bad}. Allowed: email, slack")
        return list(set(v))  # deduplicate


class EventCreate(BaseModel):
    title: str
    event_type: str = "custom"
    datetime: str          # ISO 8601 UTC, e.g. "2026-06-10T14:00:00Z"
    timezone: str = "UTC"
    duration_minutes: int = 60
    notes: str = ""
    app_id: str | None = None
    run_ids: list[str] = []
    reminders: list[ReminderSpec] = []

    @field_validator("title")
    @classmethod
    def _title_len(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("title is required")
        if len(v) > _MAX_TITLE_LEN:
            raise ValueError(f"title must be <= {_MAX_TITLE_LEN} characters")
        return v

    @field_validator("event_type")
    @classmethod
    def _valid_type(cls, v: str) -> str:
        if v not in cal_store.VALID_EVENT_TYPES:
            raise ValueError(f"event_type must be one of: {', '.join(sorted(cal_store.VALID_EVENT_TYPES))}")
        return v

    @field_validator("datetime")
    @classmethod
    def _valid_datetime(cls, v: str) -> str:
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("datetime must be a valid ISO 8601 string")
        return v

    @field_validator("duration_minutes")
    @classmethod
    def _valid_duration(cls, v: int) -> int:
        if v < 0 or v > 1440:
            raise ValueError("duration_minutes must be 0–1440")
        return v

    @field_validator("notes")
    @classmethod
    def _notes_len(cls, v: str) -> str:
        if len(v) > _MAX_NOTES_LEN:
            raise ValueError(f"notes must be <= {_MAX_NOTES_LEN} characters")
        return v

    @field_validator("reminders")
    @classmethod
    def _reminders_count(cls, v: list) -> list:
        if len(v) > _MAX_REMINDERS:
            raise ValueError(f"max {_MAX_REMINDERS} reminders per event")
        return v

    @field_validator("run_ids")
    @classmethod
    def _run_ids_len(cls, v: list[str]) -> list[str]:
        if len(v) > 20:
            raise ValueError("max 20 run_ids per event")
        return v


class EventUpdate(BaseModel):
    title: str | None = None
    event_type: str | None = None
    datetime: str | None = None
    timezone: str | None = None
    duration_minutes: int | None = None
    notes: str | None = None
    app_id: str | None = None
    run_ids: list[str] | None = None
    reminders: list[ReminderSpec] | None = None

    @field_validator("title")
    @classmethod
    def _title_len(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("title cannot be empty")
            if len(v) > _MAX_TITLE_LEN:
                raise ValueError(f"title must be <= {_MAX_TITLE_LEN} characters")
        return v

    @field_validator("event_type")
    @classmethod
    def _valid_type(cls, v: str | None) -> str | None:
        if v is not None and v not in cal_store.VALID_EVENT_TYPES:
            raise ValueError(f"event_type must be one of: {', '.join(sorted(cal_store.VALID_EVENT_TYPES))}")
        return v

    @field_validator("datetime")
    @classmethod
    def _valid_datetime(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("datetime must be a valid ISO 8601 string")
        return v

    @field_validator("notes")
    @classmethod
    def _notes_len(cls, v: str | None) -> str | None:
        if v is not None and len(v) > _MAX_NOTES_LEN:
            raise ValueError(f"notes must be <= {_MAX_NOTES_LEN} characters")
        return v

    @field_validator("reminders")
    @classmethod
    def _reminders_count(cls, v: list | None) -> list | None:
        if v is not None and len(v) > _MAX_REMINDERS:
            raise ValueError(f"max {_MAX_REMINDERS} reminders per event")
        return v


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_reminders(user_id: str, event_id: str, event_dt_iso: str,
                     reminder_specs: list[ReminderSpec]) -> list[dict]:
    """Persist reminder records and return the list of dicts stored on the event."""
    try:
        event_epoch = datetime.fromisoformat(event_dt_iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        event_epoch = time.time()

    stored = []
    for spec in reminder_specs:
        fire_at = event_epoch - (spec.offset_minutes * 60)
        rem = {
            "id":             str(uuid.uuid4()),
            "user_id":        user_id,
            "event_id":       event_id,
            "offset_minutes": spec.offset_minutes,
            "channels":       spec.channels,
            "fire_at":        fire_at,
            "sent":           False,
        }
        cal_store.save_reminder(user_id, rem)
        stored.append({
            "id":             rem["id"],
            "offset_minutes": spec.offset_minutes,
            "channels":       spec.channels,
        })
    return stored


def _verify_app_id(user_id: str, app_id: str | None) -> None:
    """Raise 422 if app_id is provided but doesn't belong to this user."""
    if not app_id:
        return
    record = app_store.get_application(user_id, app_id)
    if not record:
        raise HTTPException(422, f"app_id '{app_id}' not found in your application tracker")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("")
async def list_calendar_events(
    request: Request,
    from_dt: str | None = None,
    to_dt: str | None = None,
):
    user_id = _user_id_from_request(request)
    # Validate date params to prevent path injection / weird queries
    for param, name in ((from_dt, "from"), (to_dt, "to")):
        if param:
            try:
                datetime.fromisoformat(param.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(400, f"Invalid '{name}' datetime format")
    events = cal_store.list_events(user_id, from_dt=from_dt, to_dt=to_dt)
    return {"events": events}


@router.get("/upcoming")
async def upcoming_events(request: Request):
    """Return events in the next 7 days — used by Slack home tab."""
    user_id = _user_id_from_request(request)
    now = datetime.now(timezone.utc)
    from_dt = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    to_dt   = (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    events  = cal_store.list_events(user_id, from_dt=from_dt, to_dt=to_dt)
    return {"events": events[:20]}  # cap at 20 for Slack display


def _actor(request: Request) -> str:
    return request.state.user.get("email", request.state.user.get("user_id", "unknown"))


def _audit_entry(action: str, actor: str, changes: dict | None = None) -> dict[str, Any]:
    return {"id": str(uuid.uuid4()), "action": action, "actor": actor,
            "timestamp": _now_iso(), "changes": changes}


def _write_app_audit(user_id: str, app_id: str | None, entry: dict[str, Any]) -> None:
    """Best-effort: append an audit entry to the linked application record."""
    if not app_id:
        return
    try:
        record = app_store.get_application(user_id, app_id)
        if record:
            record.setdefault("audit_log", []).append(entry)
            app_store.save_application(user_id, record)
    except Exception:
        pass


@router.post("")
async def create_calendar_event(req: EventCreate, request: Request):
    user_id = _user_id_from_request(request)
    actor   = _actor(request)

    # Check per-user cap
    existing = cal_store.list_events(user_id)
    if len(existing) >= _MAX_EVENTS:
        raise HTTPException(429, f"Event limit reached ({_MAX_EVENTS}). Delete some events first.")

    _verify_app_id(user_id, req.app_id)

    event_id = str(uuid.uuid4())
    reminder_list = _build_reminders(user_id, event_id, req.datetime, req.reminders)

    record: dict[str, Any] = {
        "id":               event_id,
        "title":            req.title,
        "event_type":       req.event_type,
        "datetime":         req.datetime,
        "timezone":         req.timezone,
        "duration_minutes": req.duration_minutes,
        "notes":            req.notes,
        "app_id":           req.app_id,
        "run_ids":          req.run_ids,
        "reminders":        reminder_list,
    }
    result = cal_store.create_event(user_id, record)
    _write_app_audit(user_id, req.app_id, _audit_entry("calendar_event_created", actor, {
        "event_id": event_id, "title": req.title, "event_type": req.event_type, "datetime": req.datetime,
    }))
    user_audit.log(user_id, "calendar_event_created", actor,
                   event_id=event_id, title=req.title, event_type=req.event_type,
                   app_id=req.app_id)
    return result


@router.get("/{event_id}")
async def get_calendar_event(event_id: str, request: Request):
    _validate_event_id(event_id)
    user_id = _user_id_from_request(request)
    event = cal_store.get_event(user_id, event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    return event


@router.put("/{event_id}")
async def update_calendar_event(event_id: str, req: EventUpdate, request: Request):
    _validate_event_id(event_id)
    user_id = _user_id_from_request(request)
    actor   = _actor(request)
    existing = cal_store.get_event(user_id, event_id)
    if not existing:
        raise HTTPException(404, "Event not found")

    if req.app_id is not None:
        _verify_app_id(user_id, req.app_id)

    updates: dict[str, Any] = {
        k: v for k, v in req.model_dump(exclude_none=True).items()
        if k != "reminders"
    }

    # Rebuild reminders if they changed
    if req.reminders is not None:
        cal_store.delete_event_reminders(user_id, event_id)
        new_dt = req.datetime or existing.get("datetime", "")
        reminder_list = _build_reminders(user_id, event_id, new_dt, req.reminders)
        updates["reminders"] = reminder_list
    elif req.datetime is not None and existing.get("reminders"):
        # Datetime changed but reminders not re-specified — recompute fire_at
        cal_store.delete_event_reminders(user_id, event_id)
        specs = [
            ReminderSpec(offset_minutes=r["offset_minutes"], channels=r["channels"])
            for r in existing["reminders"]
        ]
        reminder_list = _build_reminders(user_id, event_id, req.datetime, specs)
        updates["reminders"] = reminder_list

    record = cal_store.update_event(user_id, event_id, updates)
    if not record:
        raise HTTPException(404, "Event not found")

    linked_app = record.get("app_id") or existing.get("app_id")
    changed = {k: {"from": existing.get(k), "to": v} for k, v in updates.items()
               if k not in ("reminders",) and existing.get(k) != v}
    _write_app_audit(user_id, linked_app, _audit_entry("calendar_event_updated", actor, {
        "event_id": event_id, "title": record.get("title"), **changed,
    }))
    user_audit.log(user_id, "calendar_event_updated", actor,
                   event_id=event_id, title=record.get("title"), app_id=linked_app,
                   fields=list(changed.keys()) if changed else [])
    return record


@router.delete("/{event_id}")
async def delete_calendar_event(event_id: str, request: Request):
    _validate_event_id(event_id)
    user_id = _user_id_from_request(request)
    actor   = _actor(request)
    existing = cal_store.get_event(user_id, event_id)
    if not existing:
        raise HTTPException(404, "Event not found")
    linked_app = existing.get("app_id")
    cal_store.delete_event_reminders(user_id, event_id)
    cal_store.delete_event(user_id, event_id)
    _write_app_audit(user_id, linked_app, _audit_entry("calendar_event_deleted", actor, {
        "event_id": event_id, "title": existing.get("title"), "event_type": existing.get("event_type"),
    }))
    user_audit.log(user_id, "calendar_event_deleted", actor,
                   event_id=event_id, title=existing.get("title"),
                   event_type=existing.get("event_type"), app_id=linked_app)
    return {"ok": True}
