"""
scripts/notification_state.py — Per-user notification state persisted in S3.

Key: notifications/{user_id}/state.json
Schema:
  {
    "researching_nudges": {
      "{app_id}": {
        "tier": 1 | 2,          # which nudge has been sent (1=2-day, 2=7-day)
        "sent_at": "<ISO>",     # when the last nudge was sent
        "snoozed_until": "<ISO>" | null
      }
    }
  }
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import storage


def _key(user_id: str) -> str:
    return f"notifications/{user_id}/state.json"


def _load(user_id: str) -> dict[str, Any]:
    raw = storage.get_text(_key(user_id))
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _save(user_id: str, state: dict[str, Any]) -> None:
    storage.put_text(_key(user_id), json.dumps(state))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Researching nudge state
# ---------------------------------------------------------------------------

def get_researching_state(user_id: str, app_id: str) -> dict[str, Any]:
    """Return the nudge state for one app, or {} if none."""
    state = _load(user_id)
    return state.get("researching_nudges", {}).get(app_id, {})


def record_nudge_sent(user_id: str, app_id: str, tier: int) -> None:
    """Record that nudge tier (1 or 2) was sent for this app."""
    state = _load(user_id)
    state.setdefault("researching_nudges", {}).setdefault(app_id, {})
    state["researching_nudges"][app_id]["tier"] = tier
    state["researching_nudges"][app_id]["sent_at"] = _now_iso()
    state["researching_nudges"][app_id].pop("snoozed_until", None)
    _save(user_id, state)


def snooze_researching(user_id: str, app_id: str, days: int) -> None:
    """Suppress nudges for this app for `days` days."""
    state = _load(user_id)
    state.setdefault("researching_nudges", {}).setdefault(app_id, {})
    until_ts = time.time() + days * 86400
    until_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until_ts))
    state["researching_nudges"][app_id]["snoozed_until"] = until_iso
    _save(user_id, state)


def clear_researching(user_id: str, app_id: str) -> None:
    """Remove nudge state for an app (called when status changes away from Researching)."""
    state = _load(user_id)
    nudges = state.get("researching_nudges", {})
    nudges.pop(app_id, None)
    state["researching_nudges"] = nudges
    _save(user_id, state)


def is_snoozed(app_state: dict[str, Any]) -> bool:
    until = app_state.get("snoozed_until")
    if not until:
        return False
    try:
        import time as _t
        until_ts = _t.mktime(_t.strptime(until, "%Y-%m-%dT%H:%M:%SZ"))
        return _t.time() < until_ts
    except Exception:
        return False
