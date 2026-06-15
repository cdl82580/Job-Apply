"""
routers/notifications.py — Email notification action endpoint.

GET  /api/notifications/action?token=...
     Verifies a signed notification token and executes the action.
     Actions:
       status  — PATCH application status (and optionally date_applied)
       snooze  — suppress notifications for this app for N days

     For "status=Applied" without a date_applied in the token payload,
     redirects to /confirm-applied.html?token=... so the user can pick
     the date in a small form. All other actions execute immediately and
     redirect to /index.html#tracker.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from scripts import applications as app_store
from scripts import notification_state as notif_state
from scripts.notification_tokens import verify_token

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

_TRACKER_URL = "/index.html#tracker"


def _app_url(app_id: str) -> str:
    return f"/index.html#tracker"


# ---------------------------------------------------------------------------
# Action endpoint
# ---------------------------------------------------------------------------

@router.get("/action")
async def notification_action(token: str = Query(...)):
    data = verify_token(token)
    if not data:
        raise HTTPException(400, "This link has expired or is invalid.")

    user_id = data["user_id"]
    app_id  = data["app_id"]
    action  = data["action"]
    payload = data.get("payload", {})

    record = app_store.get_application(user_id, app_id)
    if not record:
        raise HTTPException(404, "Application not found.")

    if action == "snooze":
        days = int(payload.get("days", 5))
        notif_state.snooze_researching(user_id, app_id, days)
        return _ok_page(
            f"Got it — we'll check back in {days} days.",
            record["company"], record["role_title"],
        )

    if action == "status":
        new_status   = payload.get("status")
        date_applied = payload.get("date_applied")

        if not new_status:
            raise HTTPException(400, "Missing status in token payload.")

        # For "Applied" without a pre-set date, send to the date-picker page
        if new_status == "Applied" and not date_applied:
            return RedirectResponse(f"/api/notifications/confirm-applied?token={token}", status_code=302)

        _apply_status(user_id, app_id, record, new_status, date_applied)
        notif_state.clear_researching(user_id, app_id)

        msg = f"Status updated to <strong>{new_status}</strong>."
        if date_applied:
            msg += f" Applied date set to {date_applied}."
        return _ok_page(msg, record["company"], record["role_title"])

    raise HTTPException(400, f"Unknown action: {action!r}")


# ---------------------------------------------------------------------------
# Confirm-applied form (date picker)
# ---------------------------------------------------------------------------

@router.get("/confirm-applied", response_class=HTMLResponse)
async def confirm_applied_page(token: str = Query(...)):
    data = verify_token(token)
    if not data:
        return HTMLResponse(_error_page("This link has expired or is invalid."), status_code=400)

    record = app_store.get_application(data["user_id"], data["app_id"])
    if not record:
        return HTMLResponse(_error_page("Application not found."), status_code=404)

    today = time.strftime("%Y-%m-%d", time.gmtime())
    company   = record["company"]
    role      = record["role_title"]

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Confirm Applied Date — Job Apply</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, -apple-system, sans-serif; background: #F9FAFB;
            display: flex; align-items: center; justify-content: center;
            min-height: 100vh; padding: 1.5rem; }}
    .card {{ background: #fff; border: 1px solid #E5E7EB; border-radius: 10px;
             padding: 2rem 2.25rem; max-width: 440px; width: 100%; }}
    h1 {{ color: #1A3C5E; font-size: 1.25rem; margin-bottom: .5rem; }}
    .sub {{ color: #6B7280; font-size: .9rem; margin-bottom: 1.5rem; }}
    label {{ display: block; font-size: .875rem; font-weight: 600;
             color: #374151; margin-bottom: .375rem; }}
    input[type=date] {{ width: 100%; padding: .5rem .75rem; border: 1px solid #D1D5DB;
                        border-radius: 6px; font-size: 1rem; color: #111827; }}
    .actions {{ display: flex; gap: .75rem; margin-top: 1.25rem; }}
    .btn {{ flex: 1; padding: .625rem 1rem; border: none; border-radius: 6px;
            font-size: .9rem; font-weight: 600; cursor: pointer; text-align: center;
            text-decoration: none; }}
    .btn-primary {{ background: #1A3C5E; color: #fff; }}
    .btn-secondary {{ background: #F3F4F6; color: #374151;
                      border: 1px solid #D1D5DB; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>When did you apply?</h1>
    <p class="sub">{company} — {role}</p>
    <form method="POST" action="/api/notifications/confirm-applied">
      <input type="hidden" name="token" value="{token}">
      <label for="date">Applied date</label>
      <input type="date" id="date" name="date_applied" value="{today}" max="{today}" required>
      <div class="actions">
        <button type="submit" class="btn btn-primary">Confirm</button>
        <a href="/index.html#tracker" class="btn btn-secondary">Cancel</a>
      </div>
    </form>
  </div>
</body>
</html>"""
    return HTMLResponse(html)


@router.post("/confirm-applied", response_class=HTMLResponse)
async def confirm_applied_submit(request: Request):
    form        = await request.form()
    token       = str(form.get("token", ""))
    date_applied = str(form.get("date_applied", "")).strip()

    data = verify_token(token)
    if not data:
        return HTMLResponse(_error_page("This link has expired or is invalid."), status_code=400)

    user_id = data["user_id"]
    app_id  = data["app_id"]
    record  = app_store.get_application(user_id, app_id)
    if not record:
        return HTMLResponse(_error_page("Application not found."), status_code=404)

    if not date_applied:
        date_applied = time.strftime("%Y-%m-%d", time.gmtime())

    _apply_status(user_id, app_id, record, "Applied", date_applied)
    notif_state.clear_researching(user_id, app_id)

    return _ok_page(
        f"Status updated to <strong>Applied</strong>. Applied date set to {date_applied}.",
        record["company"], record["role_title"],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apply_status(
    user_id: str, app_id: str, record: dict[str, Any],
    new_status: str, date_applied: str | None,
) -> None:
    now        = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    old_status = record.get("status")
    record["status"]            = new_status
    record["status_changed_at"] = now
    record["updated_at"]        = now
    record["updated_by"]        = "notification_action"
    if date_applied:
        record["date_applied"] = date_applied
    elif new_status == "Applied" and not record.get("date_applied"):
        record["date_applied"] = now[:10]
    record.setdefault("audit_log", []).append({
        "action":    "updated",
        "actor":     "notification_action",
        "timestamp": now,
        "changes":   {"status": {"from": old_status, "to": new_status}},
    })
    app_store.save_application(user_id, record)


def _ok_page(message: str, company: str, role: str) -> HTMLResponse:
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Done — Job Apply</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, -apple-system, sans-serif; background: #F9FAFB;
            display: flex; align-items: center; justify-content: center;
            min-height: 100vh; padding: 1.5rem; }}
    .card {{ background: #fff; border: 1px solid #E5E7EB; border-radius: 10px;
             padding: 2rem 2.25rem; max-width: 440px; width: 100%; text-align: center; }}
    .icon {{ font-size: 2.5rem; margin-bottom: 1rem; }}
    h1 {{ color: #1A3C5E; font-size: 1.1rem; margin-bottom: .5rem; }}
    .sub {{ color: #6B7280; font-size: .875rem; margin-bottom: 1.5rem; }}
    .msg {{ color: #374151; font-size: .9rem; margin-bottom: 1.5rem; }}
    .btn {{ display: inline-block; background: #1A3C5E; color: #fff;
            text-decoration: none; padding: .625rem 1.5rem; border-radius: 6px;
            font-size: .9rem; font-weight: 600; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">&#10003;</div>
    <h1>{company} — {role}</h1>
    <p class="msg">{message}</p>
    <a href="/index.html#tracker" class="btn">Open Tracker &rarr;</a>
  </div>
</body>
</html>"""
    return HTMLResponse(html)


def _error_page(message: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Error — Job Apply</title>
<style>body{{font-family:system-ui,sans-serif;display:flex;align-items:center;
justify-content:center;min-height:100vh;background:#F9FAFB}}
.card{{background:#fff;border:1px solid #E5E7EB;border-radius:10px;
padding:2rem 2.25rem;max-width:440px;text-align:center}}
h1{{color:#991B1B;margin-bottom:.75rem}}p{{color:#6B7280;margin-bottom:1.5rem}}
a{{color:#1A3C5E}}</style></head>
<body><div class="card"><h1>Link invalid</h1>
<p>{message}</p><a href="/index.html">Go home &rarr;</a></div></body></html>"""
