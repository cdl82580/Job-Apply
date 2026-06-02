"""
scripts/webhooks.py — Webhook storage and delivery.

Storage layout:
  webhooks/_index.json          — summary list
  webhooks/{id}.json            — full webhook record including recent deliveries

Delivery:
  - dispatch_async() is called after every audit event
  - Matching webhooks (by subscribed events) are fired in a background thread
  - HMAC-SHA256 signature sent as X-Hub-Signature-256 header (GitHub format)
  - Last 25 deliveries kept per webhook for the admin dashboard
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import urllib.parse
import uuid
from typing import Any

import requests as _requests

from . import storage

_INDEX_KEY    = "webhooks/_index.json"
_MAX_DELIVERIES = 25
_INDEX_FIELDS = {"id", "name", "url", "events", "active", "created_at",
                 "last_triggered_at", "delivery_stats"}

_index_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _key(webhook_id: str) -> str:
    return f"webhooks/{webhook_id}.json"


def _read_index() -> list[dict[str, Any]]:
    raw = storage.get_text(_INDEX_KEY)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _write_index(entries: list[dict[str, Any]]) -> None:
    storage.put_text(_INDEX_KEY, json.dumps(entries))


def _to_index_entry(w: dict[str, Any]) -> dict[str, Any]:
    return {k: w[k] for k in _INDEX_FIELDS if k in w}


def _upsert_index(webhook: dict[str, Any]) -> None:
    with _index_lock:
        index = _read_index()
        entry = _to_index_entry(webhook)
        for i, e in enumerate(index):
            if e["id"] == webhook["id"]:
                index[i] = entry
                break
        else:
            index.append(entry)
        _write_index(index)


def _remove_index(webhook_id: str) -> None:
    with _index_lock:
        index = [e for e in _read_index() if e["id"] != webhook_id]
        _write_index(index)


# ---------------------------------------------------------------------------
# Public CRUD
# ---------------------------------------------------------------------------

def list_webhooks() -> list[dict[str, Any]]:
    return _read_index()


def get_webhook(webhook_id: str) -> dict[str, Any] | None:
    raw = storage.get_text(_key(webhook_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def save_webhook(webhook: dict[str, Any]) -> None:
    storage.put_text(_key(webhook["id"]), json.dumps(webhook))
    _upsert_index(webhook)


def delete_webhook(webhook_id: str) -> bool:
    if not storage.exists(_key(webhook_id)):
        return False
    storage.delete_bytes(_key(webhook_id))
    _remove_index(webhook_id)
    return True


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def dispatch_async(event: dict[str, Any]) -> None:
    """Fire matching webhooks for this event in a daemon thread. Never raises."""
    if not storage.is_configured():
        return
    try:
        action = event.get("action", "")
        active = [w for w in _read_index() if w.get("active")]
        targets = [
            w for w in active
            if "*" in w.get("events", []) or action in w.get("events", [])
        ]
        if targets:
            threading.Thread(
                target=_deliver_batch,
                args=(targets, event),
                daemon=True,
            ).start()
    except Exception:
        pass


def _deliver_batch(targets: list[dict], event: dict) -> None:
    for t in targets:
        try:
            full = get_webhook(t["id"])
            if full and full.get("active"):
                _deliver(full, event)
        except Exception:
            pass


def _deliver(webhook: dict[str, Any], event: dict[str, Any]) -> None:
    """POST payload to webhook URL; record delivery outcome."""
    wid         = webhook["id"]
    delivery_id = str(uuid.uuid4())
    now_ts      = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    start       = time.time()

    payload_dict = {
        "webhook_id":  wid,
        "delivery_id": delivery_id,
        "timestamp":   now_ts,
        "app":         "job-apply",
        "event":       event,
    }
    body = json.dumps(payload_dict)

    # Build headers
    headers: dict[str, str] = {
        "Content-Type":   "application/json",
        "User-Agent":     "JobApply-Webhook/1.0",
        "X-Webhook-ID":   wid,
        "X-Delivery-ID":  delivery_id,
    }
    headers.update(webhook.get("headers") or {})

    secret = (webhook.get("secret") or "").strip()
    if secret:
        sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={sig}"

    # Build URL + query params
    url = webhook["url"]
    qp  = webhook.get("query_params") or {}
    if qp:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode(qp)

    status_code: int | None = None
    success     = False
    error: str | None = None

    try:
        resp = _requests.post(url, data=body, headers=headers, timeout=10)
        status_code = resp.status_code
        success     = 200 <= status_code < 300
    except Exception as exc:
        error = str(exc)

    duration_ms = int((time.time() - start) * 1000)

    delivery: dict[str, Any] = {
        "id":           delivery_id,
        "timestamp":    now_ts,
        "event_action": event.get("action", ""),
        "status_code":  status_code,
        "success":      success,
        "error":        error,
        "duration_ms":  duration_ms,
    }

    # Persist delivery record + update stats on the webhook
    try:
        w = get_webhook(wid)
        if w:
            stats = w.setdefault("delivery_stats", {"total": 0, "success": 0, "failure": 0})
            stats["total"]   = stats.get("total",   0) + 1
            stats["success"] = stats.get("success", 0) + (1 if success else 0)
            stats["failure"] = stats.get("failure", 0) + (0 if success else 1)
            w["last_triggered_at"] = now_ts
            deliveries = w.setdefault("recent_deliveries", [])
            deliveries.insert(0, delivery)
            w["recent_deliveries"] = deliveries[:_MAX_DELIVERIES]
            save_webhook(w)
    except Exception:
        pass
