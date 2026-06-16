"""
api.py — FastAPI backend for the Job Application Agent.

Auth:    Session cookie (HMAC-signed JWT-style token, stateless — works across machines).
Storage: Tigris S3 for user accounts, resumes, and profiles (see scripts/storage.py).

Public endpoints (no session required):
  POST /api/auth/register              Create account + upload resume + profile
  POST /api/auth/login                 Returns session cookie
  GET  /api/health

Auth endpoints:
  POST /api/auth/logout
  GET  /api/auth/verify-email          Email verification link handler
  POST /api/auth/resend-verification   Re-send verification email
  GET  /api/auth/me                    Current user info
  GET  /api/audit/me                   Caller's own audit log

Profile:
  GET  /api/profile
  PUT  /api/profile
  POST /api/profile/resume             Upload new master resume
  POST /api/profile/password           Change password
  POST /api/profile/email              Change email (triggers re-verification)

Application Runs:
  POST /api/run                        Start a tailoring run (returns machine_id)
  GET  /api/run/{id}/stream            SSE event stream for run progress
  GET  /api/run/{id}/status            Poll run status
  GET  /api/run/{id}/files/{name}      Download a run output file
  GET  /api/runs                       List all runs for the current user

Interview Prep Runs:
  POST /api/prep                       Start a prep run (returns machine_id)
  GET  /api/prep/{id}/stream           SSE event stream for prep progress
  GET  /api/prep/{id}/status           Poll prep status
  GET  /api/prep/{id}/files/{name}     Download a prep output file

Google Drive:
  GET  /api/gdrive/runs                List run folders from Drive
  GET  /api/gdrive/runs/{id}/job_posting   Fetch stored job posting from Drive
  PUT  /api/gdrive/runs/{id}/job_posting   Save job posting to Drive

Job Description:
  POST /api/jd/format                  AI-format a raw job description

Model Config (admin):
  GET  /api/config/model               Current active model
  PUT  /api/config/model               Set active model
  GET  /api/config/models              List available models

Knowledge Base (KB):
  GET  /api/kb/articles                List all articles + categories
  GET  /api/kb/articles/{id}           Get one article
  GET  /api/kb/categories              List categories only

Admin — KB:
  POST   /api/admin/kb/articles              Create article
  PUT    /api/admin/kb/articles/{id}         Update article
  DELETE /api/admin/kb/articles/{id}         Delete article
  POST   /api/admin/kb/categories            Create category
  PUT    /api/admin/kb/categories/{id}       Update category
  DELETE /api/admin/kb/categories/{id}       Delete category
  POST   /api/admin/kb/seed                  Replace KB from JSON payload
  POST   /api/admin/kb/seed-from-file        Re-extract KB from frontend/kb.html via Node.js

Admin — Users, Applications, Runs, Audit Log, Webhooks:
  (see routers/admin.py)

Calendar:
  (see routers/calendar.py)

Application Tracker + Companies:
  (see routers/applications.py, routers/companies.py)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
import time
import urllib.request
import uuid

logger = logging.getLogger(__name__)
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from scripts import storage
from scripts import user_audit
from scripts import email_verification as ev
from scripts import calendar as cal_store
from scripts import applications as app_store
from scripts import notification_state as notif_state
from scripts.notification_tokens import create_token as _create_notif_token
from scripts.session import SESSION_DAYS as _SESSION_DAYS_SHARED
from scripts.session import create_session_token, verify_session_token
from routers.applications import router as applications_router
from routers.companies import router as companies_router
from routers.auth_google import router as auth_google_router
from routers.admin import router as admin_router
from routers.calendar import router as calendar_router
from routers.kb import router as kb_router
from routers.notifications import router as notifications_router
try:
    from apply import (
        DEFAULT_MODEL,
        MASTER_RESUME,
        OUTPUT_DIR,
        PROFILE_FILE,
        ROUND_TYPES,
        InterviewPrepConfig,
        InterviewPrepResult,
        OptimizeConfig,
        OptimizeResult,
        WorkflowConfig,
        WorkflowError,
        WorkflowResult,
        generate_interview_prep,
        optimize_run,
        claude,
        get_gdrive_job_posting,
        get_latest_gdrive_resume_text,
        save_gdrive_job_posting,
        list_gdrive_run_folders,
        run_workflow,
        safe_filename,
    )
except Exception as _apply_import_err:
    logger.critical("Failed to import apply.py — run/prep endpoints will be unavailable: %s", _apply_import_err)
    raise

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FLY_MACHINE_ID = os.environ.get("FLY_MACHINE_ID", "")
FLY_APP_NAME   = os.environ.get("FLY_APP_NAME", "job-apply-corey")

# Signing secret for session tokens.  Generated fresh on each deploy if unset
# (means all existing sessions are invalidated on restart) — set SESSION_SECRET
# as a Fly.io secret to get persistent sessions across restarts.
_SESSION_SECRET  = os.environ.get("SESSION_SECRET", "")
if not _SESSION_SECRET:
    logger.warning(
        "SESSION_SECRET is not set. Using a random secret — all sessions will be "
        "invalidated on every restart. Set SESSION_SECRET as a Fly.io secret."
    )
    _SESSION_SECRET = secrets.token_hex(32)
_SESSION_COOKIE  = "session"
_SESSION_DAYS    = _SESSION_DAYS_SHARED
# Bearer token for the Slack bot — set BOT_API_KEY as a Fly.io secret.
# Requests carrying this token skip cookie auth and run as the primary user account.
_BOT_API_KEY     = os.environ.get("BOT_API_KEY", "")

_NOTIFY_EMAIL = os.environ.get("APP_USER_EMAIL", "")
if not _NOTIFY_EMAIL:
    logger.warning("APP_USER_EMAIL is not set. Bot API key auth will not resolve a primary user.")

_MODEL_CONFIG_KEY = "config/active_model.txt"


def _get_active_model() -> str:
    """Return the active Claude model — from Tigris config if set, else DEFAULT_MODEL."""
    try:
        if storage.is_configured():
            override = storage.get_text(_MODEL_CONFIG_KEY)
            if override:
                return override.strip()
    except Exception:
        pass
    return DEFAULT_MODEL


def _set_active_model(model: str) -> None:
    """Persist the active model override to Tigris."""
    storage.put_text(_MODEL_CONFIG_KEY, model)

# ---------------------------------------------------------------------------
# Session helpers (stateless HMAC — works across both Fly.io machines)
# ---------------------------------------------------------------------------

def _create_session(user_id: str, email: str, role: str = "user", password_hash: str = "") -> str:
    return create_session_token(user_id, email, _SESSION_SECRET, role=role, password_hash=password_hash)


def _verify_session(token: str) -> dict | None:
    return verify_session_token(token, _SESSION_SECRET)


def _bot_user(request: Request) -> dict | None:
    """Return a synthetic user dict if the request carries a valid bot API key."""
    if not _BOT_API_KEY:
        return None
    auth = request.headers.get("Authorization", "")
    if not (auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], _BOT_API_KEY)):
        return None
    # Resolve the primary user account so the bot runs as a real user with
    # a real resume and profile stored in Tigris.
    primary = storage.get_user_by_email(_NOTIFY_EMAIL)
    if not primary:
        return None
    return {"user_id": primary["user_id"], "email": primary["email"]}


def _current_user(request: Request) -> dict | None:
    bot = _bot_user(request)
    if bot:
        return bot
    token = request.cookies.get(_SESSION_COOKIE, "")
    return _verify_session(token)


def _require_user(request: Request) -> dict:
    from scripts.session import pw_version as _pw_version  # avoid circular at module level
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # Check active flag and password version — deactivated accounts and sessions
    # issued before a password change are rejected immediately.
    # Uses a 30-second cache to avoid an S3 round-trip on every request.
    record = _get_cached_user(user["user_id"])
    if record and record.get("active") is False:
        raise HTTPException(status_code=401, detail="Account deactivated")
    if record and user.get("pwv") and user["pwv"] != _pw_version(record.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Session invalidated — please log in again")
    return user


def _require_admin(request: Request) -> dict:
    user = _require_user(request)
    # Re-read role from the cached record (cache TTL is 30s, acceptable for role checks).
    record = _get_cached_user(user["user_id"])
    if not record or record.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    user["role"] = record["role"]  # keep the returned dict consistent
    return user

# ---------------------------------------------------------------------------
# Password hashing (stdlib scrypt — no extra deps)
# ---------------------------------------------------------------------------

def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk   = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt:{salt.hex()}:{dk.hex()}"


# Pre-computed dummy hash used in login to keep miss-path timing consistent
# with a real scrypt verification. Generated once at startup.
_DUMMY_HASH = _hash_password("dummy-constant-time-sentinel")


def _link_run_to_app(
    user_id: str,
    app_id: str,
    run_type: str,
    result_dir,          # Path
    folder_url: str,
) -> None:
    """Best-effort: link a completed run to an application tracker record."""
    try:
        from scripts.applications import link_run as _link, get_application, save_application
        gdrive_id = folder_url.rstrip("/").split("/")[-1] if folder_url else ""
        run_id = str(uuid.uuid4())
        folder_name = result_dir.name if result_dir else ""
        _link(user_id, app_id, {
            "id":               run_id,
            "type":             run_type,
            "folder_name":      folder_name,
            "folder_url":       folder_url,
            "gdrive_folder_id": gdrive_id,
            "linked_at":        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "linked_by":        "system",
        })
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        record = get_application(user_id, app_id)
        if record:
            record.setdefault("audit_log", []).append({
                "id":        str(uuid.uuid4()),
                "action":    "run_linked",
                "actor":     "system",
                "timestamp": now,
                "changes":   {"run_id": run_id, "type": run_type, "folder_name": folder_name},
            })
            save_application(user_id, record)
    except Exception:
        pass  # never let linking failure break the run response


def _trigger_match_scoring(
    user_id: str,
    app_id: str,
    job_posting: str,
    resume_path,           # Path to the resume docx already on disk for this run
    profile_text: str,
    user_label: str,
    folder_id: str = "",   # Drive folder to fetch the latest resume from
) -> None:
    """Best-effort, async: score how well this run's resume/profile matched the
    job posting it was tailored against, and store the result on the application
    record. When folder_id is supplied the latest Drive resume is preferred over
    the local resume_path, so the score always reflects what is in Drive.
    Never raises — failures here must never affect the run response."""
    try:
        from apply import (score_application_match, extract_resume_text,
                           get_latest_gdrive_resume_text, WorkflowConfig)
        from scripts.applications import save_match_score, get_application, save_application

        def _run():
            try:
                config = WorkflowConfig(progress=lambda _: None, user_label=user_label,
                                        master_resume=resume_path)
                if folder_id:
                    resume_text = get_latest_gdrive_resume_text(folder_id, config)
                    if not resume_text:
                        resume_text = extract_resume_text(config)
                else:
                    resume_text = extract_resume_text(config)
                match_score = score_application_match(job_posting, resume_text, profile_text, config)
                match_score["scored_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                match_score["scored_by"] = "system"
                record = save_match_score(user_id, app_id, match_score)
                if record:
                    record.setdefault("audit_log", []).append({
                        "id":        str(uuid.uuid4()),
                        "action":    "match_scored",
                        "actor":     "system",
                        "timestamp": match_score["scored_at"],
                        "changes":   {"score": match_score["score"], "category": match_score["category"]},
                    })
                    save_application(user_id, record)
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        pass


def _client_ip(request: Request) -> str | None:
    """Best-effort client IP — uses the rightmost X-Forwarded-For entry set by
    Fly.io's trusted proxy, which cannot be spoofed by the client."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    if request.client:
        return request.client.host
    return None


def _verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split(":")
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=16384, r=8, p=1)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Email helper (Resend)
# ---------------------------------------------------------------------------

_FROM_ADDRESS = os.environ.get("RESEND_FROM", "Job Apply <onboarding@resend.dev>")
_APP_URL = os.environ.get("APP_URL", "https://job-apply-corey.fly.dev")
_LOGO_URL = f"{_APP_URL}/img/logo.png"


def _email_html(body_html: str) -> str:
    """Wrap body_html in the branded email shell."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#F9FAFB;font-family:system-ui,-apple-system,sans-serif">
  <table role="presentation" cellpadding="0" cellspacing="0" width="100%"
         style="background:#F9FAFB;padding:2rem 1rem">
    <tr><td align="center">
      <table role="presentation" cellpadding="0" cellspacing="0" width="100%"
             style="max-width:520px;background:#FFFFFF;border-radius:10px;
                    border:1px solid #E5E7EB;overflow:hidden">
        <!-- Header -->
        <tr>
          <td style="background:#1A3C5E;padding:1.25rem 1.75rem">
            <img src="{_LOGO_URL}" alt="Job Apply" height="32"
                 style="display:block;border:0">
          </td>
        </tr>
        <!-- Body -->
        <tr>
          <td style="padding:2rem 1.75rem;color:#111827">
            {body_html}
          </td>
        </tr>
        <!-- Footer -->
        <tr>
          <td style="background:#F3F4F6;padding:.875rem 1.75rem;
                     border-top:1px solid #E5E7EB">
            <p style="margin:0;font-size:.75rem;color:#6B7280">
              You're receiving this because you have an account at
              <a href="{_APP_URL}" style="color:#1A3C5E;text-decoration:none">Job Apply</a>.
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _send_email(to: str, subject: str, body: str, html: str | None = None) -> bool:
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        logger.warning("_send_email: RESEND_API_KEY not set — email to %r not sent", to)
        return False
    payload: dict = {
        "from":    _FROM_ADDRESS,
        "to":      [to],
        "subject": subject,
        "text":    body,
    }
    if html:
        payload["html"] = html
    try:
        import requests as _requests
        resp = _requests.post(
            "https://api.resend.com/emails",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        ok = 200 <= resp.status_code < 300
        if not ok:
            logger.warning("_send_email: Resend returned %d to=%r body=%s",
                           resp.status_code, to, resp.text[:200])
        return ok
    except Exception:
        logger.exception("_send_email: request failed to=%r", to)
        return False


def _send_verification_email(to: str, display_name: str, token: str) -> bool:
    """Send the email-verification email via Resend."""
    verify_url = f"{_APP_URL}/api/auth/verify-email?token={token}"
    text = (
        f"Hi {display_name},\n\n"
        f"Please verify your email address by visiting:\n{verify_url}\n\n"
        f"This link expires in 72 hours.\n\n"
        f"If you didn't create this account, you can ignore this email."
    )
    body_html = f"""
    <h2 style="color:#1A3C5E;margin:0 0 .75rem;font-size:1.25rem">Verify your email</h2>
    <p style="margin:0 0 1.5rem;color:#374151">
      Hi {display_name}, click the button below to verify your email address.
    </p>
    <a href="{verify_url}"
       style="display:inline-block;background:#1A3C5E;color:#FFFFFF;text-decoration:none;
              padding:.75rem 1.5rem;border-radius:6px;font-weight:600;font-size:.95rem">
      Verify Email &rarr;
    </a>
    <p style="margin:1.5rem 0 0;font-size:.8rem;color:#6B7280">
      This link expires in 72 hours. If you didn't create an account, you can ignore this email.
    </p>"""
    return _send_email(to, "Verify your email — Job Apply", text, html=_email_html(body_html))

# ---------------------------------------------------------------------------
# App + auth middleware
# ---------------------------------------------------------------------------

app = FastAPI(title="Job Application Agent")
app.include_router(applications_router)
app.include_router(companies_router)
app.include_router(auth_google_router)
app.include_router(admin_router)
app.include_router(calendar_router)
app.include_router(kb_router)
app.include_router(notifications_router)


@app.on_event("startup")
async def _on_startup():
    _start_reminder_scheduler()
    # Surface missing env vars immediately in logs
    if not os.environ.get("RESEND_API_KEY"):
        logger.warning("STARTUP: RESEND_API_KEY is not set — all email sending is disabled")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning("STARTUP: ANTHROPIC_API_KEY is not set")
    if not os.environ.get("SESSION_SECRET"):
        logger.warning("STARTUP: SESSION_SECRET is not set — sessions will not survive restarts")


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https:; "
            "connect-src 'self'; "
            "frame-ancestors 'none';"
        ),
    )
    return response

_PUBLIC_PATHS = frozenset({
    "/login.html", "/register.html",
    "/forgot-password.html", "/reset-password.html",
    "/api/auth/login", "/api/auth/register",
    "/api/auth/google", "/api/auth/google/callback",
    "/api/auth/verify-email",
    "/api/auth/forgot-password", "/api/auth/reset-password",
    "/api/companies/search",
    "/api/health",
    "/favicon.ico",
})


_PUBLIC_PREFIXES = ("/img/", "/js/", "/css/", "/fonts/")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES):
        return await call_next(request)

    user = _current_user(request)
    if not user:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return RedirectResponse("/login.html", status_code=302)

    request.state.user = user
    return await call_next(request)

# ---------------------------------------------------------------------------
# Rate limiter (in-memory, per-IP sliding window — no external deps)
# ---------------------------------------------------------------------------

_rl_mu: threading.Lock = threading.Lock()
_rl_buckets: dict[str, list[float]] = {}   # key → list of hit timestamps

def _rate_limit(key: str, max_hits: int, window_secs: int) -> bool:
    """Return True if the key is within limits, False if the limit is exceeded.
    Uses a sliding window. Thread-safe."""
    now = time.time()
    with _rl_mu:
        hits = _rl_buckets.get(key, [])
        hits = [t for t in hits if now - t < window_secs]
        if len(hits) >= max_hits:
            _rl_buckets[key] = hits
            return False
        hits.append(now)
        _rl_buckets[key] = hits
        return True

def _sweep_rate_limit_buckets() -> None:
    """Evict all rate-limit entries whose window has fully expired.
    Call periodically (e.g. from the reminder scheduler loop) to prevent
    unbounded growth when rotating IPs hit endpoints and never come back."""
    now = time.time()
    # Use a generous window ceiling — 1 hour covers all configured windows
    MAX_WINDOW = 3600
    with _rl_mu:
        stale_keys = [k for k, hits in _rl_buckets.items()
                      if not any(now - t < MAX_WINDOW for t in hits)]
        for k in stale_keys:
            del _rl_buckets[k]


def _check_rate_limit(request: Request, bucket: str, max_hits: int, window_secs: int) -> None:
    """Raise 429 if the per-IP rate limit for bucket is exceeded."""
    ip = _client_ip(request) or "unknown"
    if not _rate_limit(f"{bucket}:{ip}", max_hits, window_secs):
        raise HTTPException(429, f"Too many requests. Try again in {window_secs} seconds.")

# ---------------------------------------------------------------------------
# Reminder scheduler — fires calendar reminders via email and/or Slack DM
# ---------------------------------------------------------------------------

_REMINDER_POLL_INTERVAL = 60  # seconds
_SLACK_NOTIFY_USER_ID   = os.environ.get("SLACK_NOTIFY_USER_ID", "")  # Slack user DM ID


def _send_slack_dm(text: str) -> None:
    """Post a Slack DM to SLACK_NOTIFY_USER_ID if configured.

    Uses conversations.open to resolve the DM channel ID first — posting
    directly to a user ID via chat.postMessage is unreliable without it.
    """
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    user_id   = _SLACK_NOTIFY_USER_ID
    if not bot_token or not user_id:
        return
    try:
        import requests as _requests
        headers = {"Authorization": f"Bearer {bot_token}"}
        # Open (or retrieve) the DM channel with this user
        open_resp = _requests.post(
            "https://slack.com/api/conversations.open",
            headers=headers,
            json={"users": user_id},
            timeout=10,
        )
        open_data = open_resp.json()
        if not open_data.get("ok"):
            logger.warning("Slack conversations.open failed: %s", open_data.get("error"))
            return
        channel_id = open_data["channel"]["id"]
        # Post the message to the resolved DM channel
        post_resp = _requests.post(
            "https://slack.com/api/chat.postMessage",
            headers=headers,
            json={"channel": channel_id, "text": text},
            timeout=10,
        )
        post_data = post_resp.json()
        if not post_data.get("ok"):
            logger.warning("Slack chat.postMessage failed: %s", post_data.get("error"))
    except Exception:
        logger.exception("_send_slack_dm failed")


def _fire_reminder(user_id: str, reminder: dict, event: dict) -> None:
    title    = event.get("title", "Event")
    dt_iso   = event.get("datetime", "")
    tz_label = event.get("timezone", "UTC")
    dt_label = dt_iso[:16].replace("T", " ") + " UTC" if dt_iso else "—"
    channels = reminder.get("channels", [])
    offset   = reminder.get("offset_minutes", 0)

    if offset == 0:
        time_label = "now"
    elif offset < 60:
        time_label = f"in {offset} minutes"
    elif offset < 1440:
        h = offset // 60
        time_label = f"in {h} hour{'s' if h > 1 else ''}"
    else:
        d = offset // 1440
        time_label = f"in {d} day{'s' if d > 1 else ''}"

    subject = f"Reminder: {title} ({time_label})"
    body    = f"You have an upcoming event: {title}\nTime: {dt_label} ({tz_label})\n\nView your calendar: {_APP_URL}/calendar.html"

    if "email" in channels and _NOTIFY_EMAIL:
        body_html = f"""
        <h2 style="color:#1A3C5E;margin:0 0 .75rem;font-size:1.25rem">
          &#128197; {title}
        </h2>
        <p style="margin:0 0 .5rem;color:#374151">
          <strong>Time:</strong> {dt_label} ({tz_label})
        </p>
        <p style="margin:0 0 1.5rem;color:#374151">
          This event is {time_label}.
        </p>
        <a href="{_APP_URL}/calendar.html"
           style="display:inline-block;background:#1A3C5E;color:#FFFFFF;text-decoration:none;
                  padding:.75rem 1.5rem;border-radius:6px;font-weight:600;font-size:.95rem">
          Open Calendar &rarr;
        </a>"""
        _send_email(_NOTIFY_EMAIL, subject, body, html=_email_html(body_html))

    if "slack" in channels:
        slack_text = f":calendar: *Reminder: {title}*\n{dt_label} ({tz_label})  —  {time_label}\n<{_APP_URL}/calendar.html|Open Calendar →>"
        _send_slack_dm(slack_text)


_RL_SWEEP_INTERVAL    = 600    # sweep rate-limit buckets every 10 minutes
_EVICT_INTERVAL       = 600    # evict stale runs/preps every 10 minutes
_NOTIF_SCAN_INTERVAL  = 3600   # scan for notification triggers every hour
_last_rl_sweep        = 0.0
_last_evict           = 0.0
_last_notif_scan      = 0.0

# Days-since-status-changed thresholds for Researching nudges
_RESEARCHING_TIER1_DAYS = 2
_RESEARCHING_TIER2_DAYS = 7

# Follow-up reminder thresholds (days since date_applied, still "Applied")
_FOLLOW_UP_TIER1_DAYS = 7
_FOLLOW_UP_TIER2_DAYS = 14

# Gone-silent threshold (days since status_changed_at for active statuses)
_GONE_SILENT_DAYS = 21
_GONE_SILENT_ACTIVE_STATUSES = {"Applied", "Phone Screen", "Interviewing", "On Hold", "Offer"}

# Digest send hours (UTC)
_DAILY_DIGEST_HOUR  = 8   # 8am UTC
_WEEKLY_DIGEST_HOUR = 18  # 6pm UTC, Sundays only


def _iso_to_ts(iso: str) -> float:
    """Parse an ISO-8601 UTC string to a Unix timestamp. Returns 0.0 on failure."""
    try:
        import calendar as _cal
        t = time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
        return float(_cal.timegm(t))
    except Exception:
        return 0.0


def _days_since(iso: str) -> float:
    ts = _iso_to_ts(iso)
    if not ts:
        return 0.0
    return (time.time() - ts) / 86400


def _researching_nudge_email(
    user_email: str, user_id: str, app: dict, tier: int
) -> None:
    """Send the 'Did you apply?' nudge email for one Researching application."""
    app_id  = app["id"]
    company = app["company"]
    role    = app.get("role_title", "")
    days    = int(_days_since(app.get("status_changed_at") or app.get("created_at", "")))

    # Build action tokens
    tok_applied = _create_notif_token(user_id, app_id, "status", {"status": "Applied"})
    tok_no      = _create_notif_token(user_id, app_id, "status", {"status": "Not Applying"})
    tok_snooze  = _create_notif_token(user_id, app_id, "snooze", {"days": 5})

    base = _APP_URL
    url_applied = f"{base}/api/notifications/action?token={tok_applied}"
    url_confirm = f"{base}/api/notifications/confirm-applied?token={tok_applied}"
    url_no      = f"{base}/api/notifications/action?token={tok_no}"
    url_snooze  = f"{base}/api/notifications/action?token={tok_snooze}"

    subject = f"Did you apply to {company}?"

    text = (
        f"You've been researching {role} at {company} for {days} day(s).\n\n"
        f"Did you end up applying?\n\n"
        f"Yes, I applied today: {url_applied}\n"
        f"Yes, but on a different date: {url_confirm}\n"
        f"Not applying: {url_no}\n"
        f"Still researching (remind me in 5 days): {url_snooze}\n\n"
        f"View tracker: {base}/tracking.html"
    )

    body_html = f"""
    <h2 style="color:#1A3C5E;margin:0 0 .375rem;font-size:1.1rem">
      Did you apply to {company}?
    </h2>
    <p style="color:#6B7280;font-size:.875rem;margin:0 0 1.25rem">
      {role} &mdash; in Researching for {days} day{'s' if days != 1 else ''}
    </p>
    <p style="color:#374151;margin:0 0 1.5rem">
      You&rsquo;ve had this one open for a while. What&rsquo;s the status?
    </p>

    <table role="presentation" cellpadding="0" cellspacing="0" width="100%"
           style="margin-bottom:1rem">
      <tr>
        <td style="padding-bottom:.625rem">
          <a href="{url_applied}"
             style="display:block;background:#1A3C5E;color:#fff;text-decoration:none;
                    padding:.65rem 1rem;border-radius:6px;font-weight:600;
                    font-size:.9rem;text-align:center">
            &#10003;&nbsp; Yes, I applied today
          </a>
        </td>
      </tr>
      <tr>
        <td style="padding-bottom:.625rem">
          <a href="{url_confirm}"
             style="display:block;background:#1A3C5E;color:#fff;text-decoration:none;
                    padding:.65rem 1rem;border-radius:6px;font-weight:600;
                    font-size:.9rem;text-align:center">
            &#128197;&nbsp; Yes, but on a different date&hellip;
          </a>
        </td>
      </tr>
      <tr>
        <td style="padding-bottom:.625rem">
          <a href="{url_no}"
             style="display:block;background:#F3F4F6;color:#374151;text-decoration:none;
                    padding:.65rem 1rem;border-radius:6px;font-weight:600;
                    font-size:.9rem;text-align:center;border:1px solid #D1D5DB">
            &#10007;&nbsp; Not applying
          </a>
        </td>
      </tr>
      <tr>
        <td>
          <a href="{url_snooze}"
             style="display:block;background:#F9FAFB;color:#6B7280;text-decoration:none;
                    padding:.5rem 1rem;border-radius:6px;font-size:.85rem;
                    text-align:center;border:1px solid #E5E7EB">
            &#128337;&nbsp; Still researching &mdash; remind me in 5 days
          </a>
        </td>
      </tr>
    </table>
    """

    _send_email(user_email, subject, text, html=_email_html(body_html))
    notif_state.record_nudge_sent(user_id, app_id, tier)
    logger.info("Researching nudge tier %d sent for user=%s app=%s", tier, user_id, app_id)
    user_audit.log(user_id, "notification_sent", "system",
                   notification_type="researching_nudge", tier=tier,
                   app_id=app_id, company=app.get("company"), role_title=app.get("role_title"))


# ---------------------------------------------------------------------------
# Follow-up reminder email
# ---------------------------------------------------------------------------

def _follow_up_reminder_email(
    user_email: str, user_id: str, app: dict, tier: int
) -> None:
    app_id    = app["id"]
    company   = app.get("company", "Unknown")
    role      = app.get("role_title", "Unknown")
    ref_ts    = _iso_to_ts(app.get("date_applied") or app.get("status_changed_at") or "")
    days      = int((time.time() - ref_ts) / 86400) if ref_ts else 0
    base      = _APP_URL

    tok_follow  = _create_notif_token(user_id, app_id, "status",
                                      {"status": "Applied"})  # mark follow-up sent → stay Applied (status stays)
    tok_no_resp = _create_notif_token(user_id, app_id, "status", {"status": "No Response"})
    tok_snooze  = _create_notif_token(user_id, app_id, "snooze_follow_up", {"days": 7})

    url_follow  = f"{base}/api/notifications/action?token={tok_follow}"
    url_no_resp = f"{base}/api/notifications/action?token={tok_no_resp}"
    url_snooze  = f"{base}/api/notifications/action?token={tok_snooze}"

    subject = f"Have you heard back from {company}?"

    text = (
        f"It's been {days} day(s) since you applied for {role} at {company}.\n\n"
        f"Have you followed up or heard anything?\n\n"
        f"Mark follow-up sent (still waiting): {url_follow}\n"
        f"Mark as No Response: {url_no_resp}\n"
        f"Snooze 7 days: {url_snooze}\n\n"
        f"View tracker: {base}/tracking.html"
    )

    body_html = f"""
    <h2 style="color:#1A3C5E;margin:0 0 .375rem;font-size:1.1rem">
      Have you heard back from {company}?
    </h2>
    <p style="color:#6B7280;font-size:.875rem;margin:0 0 1.25rem">
      {role} &mdash; applied {days} day{'s' if days != 1 else ''} ago
    </p>
    <p style="color:#374151;margin:0 0 1.5rem">
      No movement yet. Time to follow up or move on?
    </p>

    <table role="presentation" cellpadding="0" cellspacing="0" width="100%"
           style="margin-bottom:1rem">
      <tr>
        <td style="padding-bottom:.625rem">
          <a href="{url_follow}"
             style="display:block;background:#1A3C5E;color:#fff;text-decoration:none;
                    padding:.65rem 1rem;border-radius:6px;font-weight:600;
                    font-size:.9rem;text-align:center">
            &#128233;&nbsp; I followed up &mdash; still waiting
          </a>
        </td>
      </tr>
      <tr>
        <td style="padding-bottom:.625rem">
          <a href="{url_no_resp}"
             style="display:block;background:#F3F4F6;color:#374151;text-decoration:none;
                    padding:.65rem 1rem;border-radius:6px;font-weight:600;
                    font-size:.9rem;text-align:center;border:1px solid #D1D5DB">
            &#128683;&nbsp; Mark as No Response
          </a>
        </td>
      </tr>
      <tr>
        <td>
          <a href="{url_snooze}"
             style="display:block;background:#F9FAFB;color:#6B7280;text-decoration:none;
                    padding:.5rem 1rem;border-radius:6px;font-size:.85rem;
                    text-align:center;border:1px solid #E5E7EB">
            &#128337;&nbsp; Remind me again in 7 days
          </a>
        </td>
      </tr>
    </table>
    """

    _send_email(user_email, subject, text, html=_email_html(body_html))
    notif_state.record_follow_up_sent(user_id, app_id, tier)
    logger.info("Follow-up reminder tier %d sent for user=%s app=%s", tier, user_id, app_id)
    user_audit.log(user_id, "notification_sent", "system",
                   notification_type="follow_up_reminder", tier=tier,
                   app_id=app_id, company=company, role_title=role)


# ---------------------------------------------------------------------------
# Gone-silent alert email
# ---------------------------------------------------------------------------

def _gone_silent_email(user_email: str, user_id: str, app: dict) -> None:
    app_id  = app["id"]
    company = app.get("company", "Unknown")
    role    = app.get("role_title", "Unknown")
    status  = app.get("status", "")
    ref_ts  = _iso_to_ts(app.get("status_changed_at") or app.get("updated_at") or "")
    days    = int((time.time() - ref_ts) / 86400) if ref_ts else 0
    base    = _APP_URL

    tok_no_resp = _create_notif_token(user_id, app_id, "status", {"status": "No Response"})
    tok_archive = _create_notif_token(user_id, app_id, "status", {"status": "Not Applying"})
    tok_snooze  = _create_notif_token(user_id, app_id, "snooze_gone_silent", {"days": 14})

    url_no_resp = f"{base}/api/notifications/action?token={tok_no_resp}"
    url_archive = f"{base}/api/notifications/action?token={tok_archive}"
    url_snooze  = f"{base}/api/notifications/action?token={tok_snooze}"

    subject = f"No update on {company} in {days} days"

    text = (
        f"{role} at {company} has been {status} for {days} days with no activity.\n\n"
        f"Mark as No Response: {url_no_resp}\n"
        f"Archive (Not Applying): {url_archive}\n"
        f"Snooze 2 weeks: {url_snooze}\n\n"
        f"View tracker: {base}/tracking.html"
    )

    body_html = f"""
    <h2 style="color:#1A3C5E;margin:0 0 .375rem;font-size:1.1rem">
      Gone quiet: {company}
    </h2>
    <p style="color:#6B7280;font-size:.875rem;margin:0 0 1.25rem">
      {role} &mdash; {status} for {days} day{'s' if days != 1 else ''} with no update
    </p>
    <p style="color:#374151;margin:0 0 1.5rem">
      No activity in a while. Time to close this one out or snooze it?
    </p>

    <table role="presentation" cellpadding="0" cellspacing="0" width="100%"
           style="margin-bottom:1rem">
      <tr>
        <td style="padding-bottom:.625rem">
          <a href="{url_no_resp}"
             style="display:block;background:#1A3C5E;color:#fff;text-decoration:none;
                    padding:.65rem 1rem;border-radius:6px;font-weight:600;
                    font-size:.9rem;text-align:center">
            &#128683;&nbsp; Mark as No Response
          </a>
        </td>
      </tr>
      <tr>
        <td style="padding-bottom:.625rem">
          <a href="{url_archive}"
             style="display:block;background:#F3F4F6;color:#374151;text-decoration:none;
                    padding:.65rem 1rem;border-radius:6px;font-weight:600;
                    font-size:.9rem;text-align:center;border:1px solid #D1D5DB">
            &#128465;&nbsp; Archive (Not Applying)
          </a>
        </td>
      </tr>
      <tr>
        <td>
          <a href="{url_snooze}"
             style="display:block;background:#F9FAFB;color:#6B7280;text-decoration:none;
                    padding:.5rem 1rem;border-radius:6px;font-size:.85rem;
                    text-align:center;border:1px solid #E5E7EB">
            &#128337;&nbsp; Snooze 2 weeks
          </a>
        </td>
      </tr>
    </table>
    """

    _send_email(user_email, subject, text, html=_email_html(body_html))
    notif_state.record_gone_silent_sent(user_id, app_id)
    logger.info("Gone-silent alert sent for user=%s app=%s", user_id, app_id)
    user_audit.log(user_id, "notification_sent", "system",
                   notification_type="gone_silent",
                   app_id=app_id, company=company, role_title=role)


# ---------------------------------------------------------------------------
# Daily digest email
# ---------------------------------------------------------------------------

def _daily_digest_email(user_email: str, user_id: str, apps: list[dict]) -> None:
    base = _APP_URL

    active_statuses = {"Applied", "Phone Screen", "Interviewing", "On Hold", "Offer"}
    active = [a for a in apps if a.get("status") in active_statuses]
    researching = [a for a in apps if a.get("status") == "Researching"]

    # Follow-ups due today (Applied > 7 days)
    follow_ups_due = []
    for a in active:
        if a.get("status") == "Applied":
            ref = a.get("date_applied") or a.get("status_changed_at")
            if ref and _days_since(ref) >= _FOLLOW_UP_TIER1_DAYS:
                follow_ups_due.append(a)

    subject = f"Job Apply daily — {len(active)} active, {len(follow_ups_due)} follow-up{'s' if len(follow_ups_due) != 1 else ''} due"

    def _app_row(a: dict) -> str:
        return (
            f"<tr>"
            f"<td style='padding:.375rem .5rem;color:#374151;font-size:.875rem'>{a.get('company','')}</td>"
            f"<td style='padding:.375rem .5rem;color:#6B7280;font-size:.875rem'>{a.get('role_title','')}</td>"
            f"<td style='padding:.375rem .5rem;color:#6B7280;font-size:.875rem'>{a.get('status','')}</td>"
            f"</tr>"
        )

    active_rows = "".join(_app_row(a) for a in active[:20])
    followup_rows = "".join(_app_row(a) for a in follow_ups_due[:10])

    followup_section = ""
    if follow_ups_due:
        followup_section = f"""
        <h3 style="color:#B45309;font-size:.9rem;margin:1.25rem 0 .5rem">
          &#9888;&nbsp; Follow-ups due ({len(follow_ups_due)})
        </h3>
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;margin-bottom:.75rem">
          <thead>
            <tr style="background:#FEF3C7">
              <th style="padding:.375rem .5rem;text-align:left;font-size:.8rem;color:#92400E">Company</th>
              <th style="padding:.375rem .5rem;text-align:left;font-size:.8rem;color:#92400E">Role</th>
              <th style="padding:.375rem .5rem;text-align:left;font-size:.8rem;color:#92400E">Status</th>
            </tr>
          </thead>
          <tbody>{followup_rows}</tbody>
        </table>"""

    body_html = f"""
    <h2 style="color:#1A3C5E;margin:0 0 .375rem;font-size:1.1rem">
      Your daily job tracker summary
    </h2>
    <p style="color:#6B7280;font-size:.875rem;margin:0 0 1.25rem">
      {len(active)} active application{'s' if len(active) != 1 else ''} &bull;
      {len(researching)} researching &bull;
      {len(follow_ups_due)} follow-up{'s' if len(follow_ups_due) != 1 else ''} due
    </p>
    {followup_section}
    <h3 style="color:#1A3C5E;font-size:.9rem;margin:1.25rem 0 .5rem">
      Active applications ({len(active)})
    </h3>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border-collapse:collapse;margin-bottom:1.25rem">
      <thead>
        <tr style="background:#EFF6FF">
          <th style="padding:.375rem .5rem;text-align:left;font-size:.8rem;color:#1E40AF">Company</th>
          <th style="padding:.375rem .5rem;text-align:left;font-size:.8rem;color:#1E40AF">Role</th>
          <th style="padding:.375rem .5rem;text-align:left;font-size:.8rem;color:#1E40AF">Status</th>
        </tr>
      </thead>
      <tbody>{active_rows}</tbody>
    </table>
    <a href="{base}/tracking.html"
       style="display:inline-block;background:#1A3C5E;color:#fff;text-decoration:none;
              padding:.625rem 1.25rem;border-radius:6px;font-weight:600;font-size:.9rem">
      Open Tracker &rarr;
    </a>
    """

    text = (
        f"Daily summary: {len(active)} active, {len(researching)} researching, "
        f"{len(follow_ups_due)} follow-ups due.\n\n"
        f"View tracker: {base}/tracking.html"
    )

    _send_email(user_email, subject, text, html=_email_html(body_html))
    notif_state.record_digest_sent(user_id, "daily")
    logger.info("Daily digest sent for user=%s (%d active)", user_id, len(active))
    user_audit.log(user_id, "notification_sent", "system",
                   notification_type="daily_digest", active_count=len(active))


# ---------------------------------------------------------------------------
# Weekly digest email
# ---------------------------------------------------------------------------

def _weekly_digest_email(user_email: str, user_id: str, apps: list[dict]) -> None:
    base = _APP_URL

    from collections import Counter
    status_counts = Counter(a.get("status", "Unknown") for a in apps)

    active_statuses = {"Applied", "Phone Screen", "Interviewing", "On Hold", "Offer"}
    active = [a for a in apps if a.get("status") in active_statuses]

    # Silent apps: active but no update in 14+ days
    silent = []
    for a in active:
        ref = a.get("status_changed_at") or a.get("updated_at")
        if ref and _days_since(ref) >= 14:
            silent.append(a)

    subject = f"Job Apply weekly — {len(active)} active, {len(silent)} gone quiet"

    def _status_row(status: str, count: int) -> str:
        return (
            f"<tr>"
            f"<td style='padding:.375rem .5rem;color:#374151;font-size:.875rem'>{status}</td>"
            f"<td style='padding:.375rem .5rem;color:#374151;font-size:.875rem;text-align:right'>{count}</td>"
            f"</tr>"
        )

    ordered_statuses = ["Researching", "Applied", "Phone Screen", "Interviewing",
                        "On Hold", "Offer", "No Response", "Not Applying"]
    status_rows = "".join(
        _status_row(s, status_counts[s])
        for s in ordered_statuses if status_counts.get(s)
    )
    other_statuses = set(status_counts) - set(ordered_statuses)
    for s in sorted(other_statuses):
        status_rows += _status_row(s, status_counts[s])

    silent_section = ""
    if silent:
        silent_rows = "".join(
            f"<tr>"
            f"<td style='padding:.375rem .5rem;color:#374151;font-size:.875rem'>{a.get('company','')}</td>"
            f"<td style='padding:.375rem .5rem;color:#6B7280;font-size:.875rem'>{a.get('role_title','')}</td>"
            f"<td style='padding:.375rem .5rem;color:#6B7280;font-size:.875rem'>{a.get('status','')}</td>"
            f"</tr>"
            for a in silent[:10]
        )
        silent_section = f"""
        <h3 style="color:#B45309;font-size:.9rem;margin:1.25rem 0 .5rem">
          &#128276;&nbsp; Gone quiet ({len(silent)})
        </h3>
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;margin-bottom:.75rem">
          <thead>
            <tr style="background:#FEF3C7">
              <th style="padding:.375rem .5rem;text-align:left;font-size:.8rem;color:#92400E">Company</th>
              <th style="padding:.375rem .5rem;text-align:left;font-size:.8rem;color:#92400E">Role</th>
              <th style="padding:.375rem .5rem;text-align:left;font-size:.8rem;color:#92400E">Status</th>
            </tr>
          </thead>
          <tbody>{silent_rows}</tbody>
        </table>"""

    body_html = f"""
    <h2 style="color:#1A3C5E;margin:0 0 .375rem;font-size:1.1rem">
      Your weekly pipeline overview
    </h2>
    <p style="color:#6B7280;font-size:.875rem;margin:0 0 1.25rem">
      {len(apps)} total application{'s' if len(apps) != 1 else ''} tracked
    </p>

    <h3 style="color:#1A3C5E;font-size:.9rem;margin:0 0 .5rem">Pipeline by status</h3>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border-collapse:collapse;margin-bottom:1rem">
      <thead>
        <tr style="background:#EFF6FF">
          <th style="padding:.375rem .5rem;text-align:left;font-size:.8rem;color:#1E40AF">Status</th>
          <th style="padding:.375rem .5rem;text-align:right;font-size:.8rem;color:#1E40AF">Count</th>
        </tr>
      </thead>
      <tbody>{status_rows}</tbody>
    </table>
    {silent_section}
    <a href="{base}/tracking.html"
       style="display:inline-block;background:#1A3C5E;color:#fff;text-decoration:none;
              padding:.625rem 1.25rem;border-radius:6px;font-weight:600;font-size:.9rem;
              margin-top:.75rem">
      Open Tracker &rarr;
    </a>
    """

    text = (
        f"Weekly summary: {len(apps)} total, {len(active)} active, {len(silent)} gone quiet.\n\n"
        + "\n".join(f"  {s}: {c}" for s, c in status_counts.most_common())
        + f"\n\nView tracker: {base}/tracking.html"
    )

    _send_email(user_email, subject, text, html=_email_html(body_html))
    notif_state.record_digest_sent(user_id, "weekly")
    logger.info("Weekly digest sent for user=%s (%d total)", user_id, len(apps))
    user_audit.log(user_id, "notification_sent", "system",
                   notification_type="weekly_digest", total_count=len(apps))


def _scan_notifications(user_id: str, user_email: str) -> None:
    """Check one user's applications and fire any due notifications."""
    user_record = storage.get_user_by_id(user_id) or {}
    prefs = {**_default_notif_prefs(), **user_record.get("notification_prefs", {})}

    # Fetch all apps once for scans that need multiple statuses
    try:
        all_apps = app_store.list_applications(user_id).get("items", [])
    except Exception:
        logger.exception("_scan_notifications: failed to list apps for user %s", user_id)
        return

    # ------------------------------------------------------------------
    # Researching nudge
    # ------------------------------------------------------------------
    if prefs.get("researching_nudge", True):
        for app in all_apps:
            if app.get("status") != "Researching":
                continue
            app_id = app["id"]
            ref_ts = app.get("status_changed_at") or app.get("created_at")
            if not ref_ts:
                continue

            days_elapsed = _days_since(ref_ts)
            ns = notif_state.get_researching_state(user_id, app_id)

            if notif_state.is_snoozed(ns):
                continue

            tier_sent = ns.get("tier", 0)

            if days_elapsed >= _RESEARCHING_TIER2_DAYS and tier_sent < 2:
                _researching_nudge_email(user_email, user_id, app, tier=2)
            elif days_elapsed >= _RESEARCHING_TIER1_DAYS and tier_sent < 1:
                _researching_nudge_email(user_email, user_id, app, tier=1)

    # ------------------------------------------------------------------
    # Follow-up reminder (Applied with no progression)
    # ------------------------------------------------------------------
    if prefs.get("follow_up_reminder", True):
        for app in all_apps:
            if app.get("status") != "Applied":
                continue
            app_id = app["id"]
            ref = app.get("date_applied") or app.get("status_changed_at")
            if not ref:
                continue

            days_elapsed = _days_since(ref)
            ns = notif_state.get_follow_up_state(user_id, app_id)

            if notif_state.is_snoozed(ns):
                continue

            tier_sent = ns.get("tier", 0)

            if days_elapsed >= _FOLLOW_UP_TIER2_DAYS and tier_sent < 2:
                _follow_up_reminder_email(user_email, user_id, app, tier=2)
            elif days_elapsed >= _FOLLOW_UP_TIER1_DAYS and tier_sent < 1:
                _follow_up_reminder_email(user_email, user_id, app, tier=1)

    # ------------------------------------------------------------------
    # Gone-silent alert (active apps with no status change in 21 days)
    # ------------------------------------------------------------------
    if prefs.get("gone_silent", True):
        for app in all_apps:
            if app.get("status") not in _GONE_SILENT_ACTIVE_STATUSES:
                continue
            app_id = app["id"]
            ref = app.get("status_changed_at") or app.get("updated_at")
            if not ref:
                continue

            if _days_since(ref) < _GONE_SILENT_DAYS:
                continue

            ns = notif_state.get_gone_silent_state(user_id, app_id)
            if notif_state.is_snoozed(ns):
                continue

            # Only send once per silence period (cleared when status changes)
            if ns.get("sent_at"):
                # Don't re-alert until status changes and comes back silent
                continue

            _gone_silent_email(user_email, user_id, app)

    # ------------------------------------------------------------------
    # Daily digest (once per day at ~8am UTC)
    # ------------------------------------------------------------------
    if prefs.get("daily_digest", True):
        utc_now = time.gmtime()
        if utc_now.tm_hour >= _DAILY_DIGEST_HOUR:
            today = time.strftime("%Y-%m-%d", utc_now)
            last_sent = notif_state.get_last_digest_date(user_id, "daily")
            if last_sent != today:
                _daily_digest_email(user_email, user_id, all_apps)

    # ------------------------------------------------------------------
    # Weekly digest (once per week on Sunday at ~6pm UTC)
    # ------------------------------------------------------------------
    if prefs.get("weekly_digest", True):
        utc_now = time.gmtime()
        # tm_wday: 0=Mon … 6=Sun
        if utc_now.tm_wday == 6 and utc_now.tm_hour >= _WEEKLY_DIGEST_HOUR:
            today = time.strftime("%Y-%m-%d", utc_now)
            last_sent = notif_state.get_last_digest_date(user_id, "weekly")
            if last_sent != today:
                _weekly_digest_email(user_email, user_id, all_apps)


def _reminder_scheduler_loop() -> None:
    """Background thread: poll every 60s and fire due reminders.
    Also does periodic housekeeping (rate-limit sweep, stale run eviction)."""
    global _last_rl_sweep, _last_evict, _last_notif_scan
    time.sleep(15)  # brief startup delay
    while True:
        now = time.time()

        # Periodic housekeeping (piggy-back on this thread to avoid extra threads)
        if now - _last_rl_sweep >= _RL_SWEEP_INTERVAL:
            try:
                _sweep_rate_limit_buckets()
            except Exception:
                logger.exception("Rate-limit bucket sweep error")
            _last_rl_sweep = now

        if now - _last_evict >= _EVICT_INTERVAL:
            try:
                _evict_stale()
            except Exception:
                logger.exception("Stale run eviction error")
            _last_evict = now

        try:
            user_ids = cal_store.list_all_user_ids_with_reminders()
            for uid in user_ids:
                try:
                    due = cal_store.list_due_reminders(uid)
                    for reminder in due:
                        event_id = reminder.get("event_id", "")
                        event    = cal_store.get_event(uid, event_id) if event_id else None
                        if event:
                            _fire_reminder(uid, reminder, event)
                        cal_store.mark_reminder_sent(uid, reminder["id"])
                except Exception:
                    logger.exception("Reminder scheduler error for user %s", uid)
        except Exception:
            logger.exception("Reminder scheduler top-level error")

        # Application notification scanner (runs once per hour)
        if now - _last_notif_scan >= _NOTIF_SCAN_INTERVAL:
            _last_notif_scan = now
            if not os.environ.get("RESEND_API_KEY"):
                logger.warning("notification scanner: RESEND_API_KEY not set — skipping (no email will be sent)")
            try:
                if _NOTIFY_EMAIL:
                    primary = storage.get_user_by_email(_NOTIFY_EMAIL)
                    if primary:
                        _scan_notifications(primary["user_id"], _NOTIFY_EMAIL)
            except Exception:
                logger.exception("Notification scanner error")

        time.sleep(_REMINDER_POLL_INTERVAL)


def _start_reminder_scheduler() -> None:
    t = threading.Thread(target=_reminder_scheduler_loop, daemon=True, name="reminder-scheduler")
    t.start()


# ---------------------------------------------------------------------------
# Short-lived user-record cache (avoids an S3 round-trip on every request)
# ---------------------------------------------------------------------------

_USER_CACHE_TTL = 30  # seconds — max staleness for deactivated-account / role checks
_user_cache: dict[str, tuple[float, dict | None]] = {}   # user_id → (expires_at, record)
_user_cache_mu = threading.Lock()


def _get_cached_user(user_id: str) -> dict | None:
    now = time.time()
    with _user_cache_mu:
        entry = _user_cache.get(user_id)
        if entry and entry[0] > now:
            return entry[1]
    record = storage.get_user_by_id(user_id)
    with _user_cache_mu:
        _user_cache[user_id] = (now + _USER_CACHE_TTL, record)
    return record


def _invalidate_user_cache(user_id: str) -> None:
    """Call after any write that changes active, role, or password_hash."""
    with _user_cache_mu:
        _user_cache.pop(user_id, None)


# ---------------------------------------------------------------------------
# In-memory stores
# ---------------------------------------------------------------------------

_runs:          dict[str, dict[str, Any]] = {}
_preps:         dict[str, dict[str, Any]] = {}
_optimizations: dict[str, dict[str, Any]] = {}
_MAX_ACTIVE_RUNS_PER_USER = 5  # cap in-flight + queued entries per user

# Per-user locks so concurrent runs from different users don't block each other.
_user_locks: dict[str, threading.Lock] = {}
_user_locks_mu = threading.Lock()


def _get_user_lock(user_id: str) -> threading.Lock:
    with _user_locks_mu:
        if user_id not in _user_locks:
            _user_locks[user_id] = threading.Lock()
        return _user_locks[user_id]

_RUN_TTL = 3600 * 4  # evict terminal runs/preps after 4 hours


def _worker_thread(
    store:        dict,
    entry_id:     str,
    user_id:      str,
    user_email:   str,
    resume_bytes: bytes,
    run_fn,               # callable(resume_path, progress_cb) → result
    done_payload_fn,      # callable(result) → dict  (merged into the SSE done event)
    audit_success: str,
    audit_failure: str,
    audit_kwargs:  dict,
) -> None:
    """Generic worker thread: write temp resume, acquire user lock, call run_fn,
    update the store entry, and put SSE events onto the queue. Handles both
    WorkflowError and unexpected exceptions uniformly."""
    q = store[entry_id]["queue"]
    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False, dir="/tmp")
    tmp.write(resume_bytes)
    tmp.close()
    resume_path = Path(tmp.name)

    try:
        with _get_user_lock(user_id):
            store[entry_id]["status"] = "running"

            def progress(msg: str):
                q.put({"type": "progress", "message": msg})

            try:
                result = run_fn(resume_path, progress)
                store[entry_id]["result"]       = result
                store[entry_id]["status"]       = "done"
                store[entry_id]["_finished_at"] = time.time()
                kw = audit_kwargs(result) if callable(audit_kwargs) else audit_kwargs
                user_audit.log(user_id, audit_success, user_email, **kw)
                payload = {"type": "done", entry_id.split("_")[0] + "_id": entry_id}
                payload.update(done_payload_fn(result))
                q.put(payload)
            except WorkflowError as exc:
                store[entry_id]["status"]       = "error"
                store[entry_id]["error"]        = str(exc)
                store[entry_id]["_finished_at"] = time.time()
                kw = audit_kwargs(None) if callable(audit_kwargs) else audit_kwargs
                user_audit.log(user_id, audit_failure, user_email,
                               error=str(exc), **kw)
                q.put({"type": "error", "message": str(exc)})
            except Exception as exc:
                msg = f"Unexpected error: {type(exc).__name__}: {exc}"
                logger.exception("Unexpected error in %s %s", audit_success, entry_id)
                store[entry_id]["status"]       = "error"
                store[entry_id]["error"]        = msg
                store[entry_id]["_finished_at"] = time.time()
                kw = audit_kwargs(None) if callable(audit_kwargs) else audit_kwargs
                user_audit.log(user_id, audit_failure, user_email,
                               error=msg, **kw)
                q.put({"type": "error", "message": "An unexpected error occurred. Please try again."})
            finally:
                q.put(None)
    finally:
        resume_path.unlink(missing_ok=True)


def _evict_stale() -> None:
    """Remove completed/errored runs and preps older than _RUN_TTL."""
    cutoff = time.time() - _RUN_TTL
    for store in (_runs, _preps, _optimizations):
        stale = [
            k for k, v in store.items()
            if v.get("status") in ("done", "error")
            and v.get("_finished_at", 0) < cutoff
        ]
        for k in stale:
            del store[k]

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: str
    password: str

class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str

class EmailChangeRequest(BaseModel):
    new_email: str
    current_password: str

_MAX_DISPLAY_NAME_LEN = 100
_MAX_PROFILE_TEXT_LEN = 50_000

_NOTIF_PREF_KEYS = {
    "researching_nudge",
    "follow_up_reminder",
    "gone_silent",
    "status_changed",
    "new_application",
    "daily_digest",
    "weekly_digest",
}

def _default_notif_prefs() -> dict:
    return {k: True for k in _NOTIF_PREF_KEYS}


class ProfileUpdateRequest(BaseModel):
    display_name: str | None = None
    profile_text: str | None = None
    notification_prefs: dict | None = None

class RunRequest(BaseModel):
    job_posting: str = ""
    company: str
    role: str
    contact: str | None = None
    model: str | None = None
    app_id: str | None = None   # optional: link to application tracker record
    jd_folder_id: str | None = None  # load JD from this Drive folder instead of job_posting

class PrepRequest(BaseModel):
    job_posting: str
    company: str
    role: str
    round_type: str
    focus: str | None = None
    interviewer: str | None = None
    model: str | None = None
    app_id: str | None = None   # optional: link to application tracker record

_MAX_OPTIMIZE_INSTRUCTION_LEN = 4000

class OptimizeRequest(BaseModel):
    app_id: str                 # application tracker record owning the run
    folder_id: str              # Drive run folder to optimize
    instruction: str            # user's free-text optimization prompt
    company: str
    role: str
    optimize_resume: bool = True
    optimize_cover_letter: bool = True
    model: str | None = None

# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health(request: Request):
    checks: dict = {}

    # Storage (Tigris S3)
    try:
        checks["storage"] = "ok" if storage.is_configured() else "not_configured"
        if storage.is_configured():
            storage.get_text("config/health_probe.txt")  # lightweight read probe
    except Exception as exc:
        checks["storage"] = f"error: {exc}"

    # Email (Resend) — just check key presence, don't send
    checks["email"] = "configured" if os.environ.get("RESEND_API_KEY") else "not_configured"

    # Google Drive credentials on disk
    checks["gdrive"] = "configured" if os.path.exists("gdrive_credentials.json") else "not_configured"

    # Active model
    checks["model"] = _get_active_model()

    # Anthropic API key presence
    checks["anthropic"] = "configured" if os.environ.get("ANTHROPIC_API_KEY") else "not_configured"

    # Fly machine info
    checks["fly_machine"] = FLY_MACHINE_ID or "local"
    checks["fly_app"]     = FLY_APP_NAME

    overall = "ok" if all(
        (v in ("ok", "configured", "not_configured")) or (not str(v).startswith("error"))
        for v in checks.values()
    ) else "degraded"

    # Unauthenticated callers get only the top-level status — no internal details
    if not _current_user(request):
        return {"status": overall}
    return {"status": overall, **checks}

# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

@app.post("/api/auth/register")
async def register(
    request:      Request,
    display_name: str      = Form(...),
    email:        str      = Form(...),
    password:     str      = Form(...),
    profile_text: str      = Form(...),
    resume:       UploadFile = File(...),
):
    _check_rate_limit(request, "register", max_hits=5, window_secs=3600)
    email = email.strip().lower()

    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, "Invalid email address.")
    if len(display_name.strip()) > _MAX_DISPLAY_NAME_LEN:
        raise HTTPException(400, f"Display name must be {_MAX_DISPLAY_NAME_LEN} characters or fewer.")
    if len(profile_text) > _MAX_PROFILE_TEXT_LEN:
        raise HTTPException(400, f"Profile text must be {_MAX_PROFILE_TEXT_LEN} characters or fewer.")
    if storage.get_user_by_email(email):
        raise HTTPException(400, "An account with that email already exists.")
    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")
    if not resume.filename.lower().endswith(".docx"):
        raise HTTPException(400, "Resume must be a .docx file.")

    resume_data = await resume.read()
    if len(resume_data) < 1000:
        raise HTTPException(400, "Resume file appears to be empty or invalid.")
    if len(resume_data) > 10 * 1024 * 1024:
        raise HTTPException(400, "Resume file must be under 10 MB.")

    user_id = str(uuid.uuid4())
    user = {
        "user_id":         user_id,
        "email":           email,
        "display_name":    display_name.strip(),
        "password_hash":   _hash_password(password),
        "created_at":      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "resume_filename": resume.filename,
        "email_verified":  False,
    }

    storage.save_user(user)
    storage.save_resume(user_id, resume_data)
    storage.save_profile(user_id, profile_text)
    user_audit.log(user_id, "user_registered", email, _client_ip(request),
                   display_name=display_name.strip(), resume_filename=resume.filename)

    # Send verification email (best-effort — don't block registration if it fails)
    token = ev.create_token(user_id, email)
    _send_verification_email(_NOTIFY_EMAIL or email, display_name.strip(), token)

    response = JSONResponse({"ok": True, "display_name": user["display_name"],
                             "email_verified": False})
    token = _create_session(user_id, email, role=user.get("role", "user"),
                            password_hash=user["password_hash"])
    response.set_cookie(_SESSION_COOKIE, token, max_age=86400 * _SESSION_DAYS,
                        httponly=True, samesite="lax", secure=True)
    if FLY_MACHINE_ID:
        response.set_cookie("fly-force-instance-id", FLY_MACHINE_ID, path="/", samesite="lax", httponly=True)
    return response


@app.post("/api/auth/login")
async def login(req: LoginRequest, request: Request):
    _check_rate_limit(request, "login", max_hits=10, window_secs=60)
    email = req.email.strip().lower()
    user  = storage.get_user_by_email(email)

    # Constant-time check even on miss to prevent user enumeration timing
    stored = user["password_hash"] if user else _DUMMY_HASH
    ok = _verify_password(req.password, stored) and user is not None

    if not ok:
        user_audit.log_login_failure(email, _client_ip(request))
        raise HTTPException(401, "Incorrect email or password.")

    user_audit.log(user["user_id"], "login_success", email, _client_ip(request))

    response = JSONResponse({"ok": True, "display_name": user["display_name"]})
    token = _create_session(user["user_id"], email, role=user.get("role", "user"),
                            password_hash=user.get("password_hash", ""))
    response.set_cookie(_SESSION_COOKIE, token, max_age=86400 * _SESSION_DAYS,
                        httponly=True, samesite="lax", secure=True)
    if FLY_MACHINE_ID:
        response.set_cookie("fly-force-instance-id", FLY_MACHINE_ID, path="/", samesite="lax", httponly=True)
    return response


@app.post("/api/auth/logout")
async def logout(request: Request):
    user = _current_user(request)
    if user:
        user_audit.log(user["user_id"], "logout", user["email"], _client_ip(request))
    response = JSONResponse({"ok": True})
    response.delete_cookie(_SESSION_COOKIE)
    response.delete_cookie("fly-force-instance-id")
    return response


@app.get("/api/auth/verify-email")
async def verify_email(token: str = ""):
    """Public — clicked from email link. Marks user verified and redirects."""
    app_url = os.environ.get("APP_URL", "https://job-apply-corey.fly.dev")
    fail_url = f"{app_url}/login.html?auth_error="

    if not token:
        return RedirectResponse(f"{fail_url}Invalid+verification+link", status_code=302)

    data = ev.consume_token(token)
    if not data:
        return RedirectResponse(
            f"{fail_url}Verification+link+is+invalid+or+has+expired.+Please+request+a+new+one.",
            status_code=302,
        )

    user = storage.get_user_by_id(data["user_id"])
    if not user:
        return RedirectResponse(f"{fail_url}Account+not+found", status_code=302)

    user["email_verified"] = True
    storage.save_user(user)
    user_audit.log(data["user_id"], "email_verified", data["email"])

    return RedirectResponse(f"{app_url}/login.html?verified=1", status_code=302)


@app.post("/api/auth/resend-verification")
async def resend_verification(request: Request):
    """Protected — resend verification email to the current user."""
    _check_rate_limit(request, "resend_verification", max_hits=3, window_secs=3600)
    user_data = _require_user(request)
    user = storage.get_user_by_id(user_data["user_id"])
    if not user:
        raise HTTPException(404, "User not found")

    if user.get("email_verified", True):
        return JSONResponse({"ok": True, "already_verified": True})

    token = ev.create_token(user_data["user_id"], user_data["email"])
    sent  = _send_verification_email(_NOTIFY_EMAIL or user_data["email"], user.get("display_name", "there"), token)
    user_audit.log(user_data["user_id"], "verification_email_resent", user_data["email"],
                   _client_ip(request))
    return JSONResponse({"ok": True, "sent": sent})


@app.post("/api/auth/forgot-password")
async def forgot_password(request: Request):
    """Unauthenticated. Sends a password-reset link to the account email.
    Always returns 200 to avoid user-enumeration."""
    _check_rate_limit(request, "forgot_password", max_hits=3, window_secs=3600)

    # Single-user: look up by APP_USER_EMAIL, or by posted email
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    email = (body.get("email") or "").strip().lower() or _NOTIFY_EMAIL

    user = storage.get_user_by_email(email) if email else None
    if not user:
        logger.warning("forgot_password: no account found for email=%r", email)
    else:
        try:
            token = _create_notif_token(
                user["user_id"], "password_reset", "password_reset",
                payload={"email": email}, ttl=3600,
            )
            reset_url = f"{_APP_URL}/reset-password.html?token={token}"

            text = (
                f"Hi {user.get('display_name', 'there')},\n\n"
                f"Click the link below to reset your Job Apply password. "
                f"This link expires in 1 hour.\n\n{reset_url}\n\n"
                f"If you didn't request this, ignore this email."
            )
            body_html = f"""
            <h2 style="color:#1A3C5E;margin:0 0 .75rem;font-size:1.1rem">Reset your password</h2>
            <p style="margin:0 0 1rem;color:#374151">
              Click the button below to choose a new password.
              This link expires in <strong>1 hour</strong>.
            </p>
            <a href="{reset_url}"
               style="display:inline-block;background:#1A3C5E;color:#fff;text-decoration:none;
                      padding:.65rem 1.5rem;border-radius:6px;font-weight:600;font-size:.9rem;
                      margin-bottom:1.25rem">
              Reset password &rarr;
            </a>
            <p style="margin:0;color:#6B7280;font-size:.825rem">
              If you didn't request this, you can safely ignore this email.
            </p>"""

            # Always deliver to the Resend-verified address (_NOTIFY_EMAIL / APP_USER_EMAIL).
            # +alias variants stored on user records are rejected by Resend in test mode.
            send_to = _NOTIFY_EMAIL or email
            sent = _send_email(send_to, "Reset your Job Apply password", text, html=_email_html(body_html))
            logger.info("forgot_password: reset email sent=%s to=%r user_id=%s", sent, send_to, user["user_id"])
            user_audit.log(user["user_id"], "password_reset_requested", email, _client_ip(request))
        except Exception:
            logger.exception("forgot_password: failed for email=%r", email)

    return JSONResponse({"ok": True})


@app.post("/api/auth/reset-password")
async def reset_password(request: Request):
    """Unauthenticated. Verifies reset token and sets a new password."""
    _check_rate_limit(request, "reset_password", max_hits=5, window_secs=3600)
    body = await request.json()
    token       = (body.get("token") or "").strip()
    new_password = (body.get("new_password") or "").strip()

    from scripts.notification_tokens import verify_token as _verify_reset_token
    data = _verify_reset_token(token)
    if not data or data.get("action") != "password_reset":
        raise HTTPException(400, "This reset link has expired or is invalid.")

    if len(new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")

    user_id = data["user_id"]
    record  = storage.get_user_by_id(user_id)
    if not record:
        raise HTTPException(404, "Account not found.")

    record["password_hash"] = _hash_password(new_password)
    storage.save_user(record)
    _invalidate_user_cache(user_id)
    user_audit.log(user_id, "password_reset_completed", record.get("email", ""), _client_ip(request))
    return JSONResponse({"ok": True})


@app.get("/api/audit/me")
async def my_audit_log(request: Request):
    """Return the current user's full action audit log, newest first."""
    user_data = _require_user(request)
    return user_audit.get_events(user_data["user_id"])


@app.get("/api/auth/me")
async def me(request: Request):
    user_data = _require_user(request)
    record = storage.get_user_by_id(user_data["user_id"]) or {}
    return {
        "user_id":        user_data["user_id"],
        "email":          user_data["email"],
        "display_name":   record.get("display_name", user_data["email"]),
        "role":           record.get("role", "user"),
        "email_verified": record.get("email_verified", True),  # legacy accounts default to True
        "has_resume":     storage.has_resume(user_data["user_id"]),
        "has_profile":    bool(storage.get_profile(user_data["user_id"])),
        "model":          _get_active_model(),
    }

# ---------------------------------------------------------------------------
# Profile endpoints
# ---------------------------------------------------------------------------

@app.get("/api/profile")
async def get_profile(request: Request):
    user_data = _require_user(request)
    record = storage.get_user_by_id(user_data["user_id"]) or {}
    profile_text = storage.get_profile(user_data["user_id"]) or ""
    prefs = {**_default_notif_prefs(), **record.get("notification_prefs", {})}
    return {
        "display_name":       record.get("display_name", ""),
        "email":              user_data["email"],
        "role":               record.get("role", "user"),
        "email_verified":     record.get("email_verified", True),
        "profile_text":       profile_text,
        "has_resume":           storage.has_resume(user_data["user_id"]),
        "resume_filename":      record.get("resume_filename"),
        "resume_uploaded_at":   record.get("resume_uploaded_at"),
        "notification_prefs":   prefs,
    }


@app.put("/api/profile")
async def update_profile(req: ProfileUpdateRequest, request: Request):
    user_data = _require_user(request)
    record = storage.get_user_by_id(user_data["user_id"])
    if not record:
        raise HTTPException(404, "User not found.")

    changes = {}
    if req.display_name is not None:
        if len(req.display_name.strip()) > _MAX_DISPLAY_NAME_LEN:
            raise HTTPException(400, f"Display name must be {_MAX_DISPLAY_NAME_LEN} characters or fewer.")
        old_name = record.get("display_name", "")
        record["display_name"] = req.display_name.strip()
        storage.save_user(record)
        if old_name != record["display_name"]:
            changes["display_name"] = {"from": old_name, "to": record["display_name"]}

    if req.profile_text is not None:
        if len(req.profile_text) > _MAX_PROFILE_TEXT_LEN:
            raise HTTPException(400, f"Profile text must be {_MAX_PROFILE_TEXT_LEN} characters or fewer.")
        storage.save_profile(user_data["user_id"], req.profile_text)
        changes["profile_text"] = "updated"

    if req.notification_prefs is not None:
        unknown = set(req.notification_prefs.keys()) - _NOTIF_PREF_KEYS
        if unknown:
            raise HTTPException(400, f"Unknown notification pref keys: {', '.join(sorted(unknown))}")
        existing = {**_default_notif_prefs(), **record.get("notification_prefs", {})}
        merged   = {**existing, **{k: bool(v) for k, v in req.notification_prefs.items()}}
        record["notification_prefs"] = merged
        storage.save_user(record)
        changes["notification_prefs"] = "updated"

    if changes:
        user_audit.log(user_data["user_id"], "profile_updated", user_data["email"],
                       _client_ip(request), changes=changes)

    return {"ok": True}


@app.post("/api/profile/resume")
async def upload_resume(request: Request, resume: UploadFile = File(...)):
    _check_rate_limit(request, "upload_resume", max_hits=10, window_secs=3600)
    user_data = _require_user(request)
    if not resume.filename.lower().endswith(".docx"):
        raise HTTPException(400, "Resume must be a .docx file.")
    data = await resume.read()
    if len(data) < 1000:
        raise HTTPException(400, "File appears empty or invalid.")
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, "Resume file must be under 10 MB.")
    record = storage.get_user_by_id(user_data["user_id"])
    if record:
        record["resume_filename"] = resume.filename
        record["resume_uploaded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        storage.save_user(record)
    storage.save_resume(user_data["user_id"], data)
    user_audit.log(user_data["user_id"], "resume_uploaded", user_data["email"],
                   _client_ip(request), filename=resume.filename, size_bytes=len(data))
    return {"ok": True}


@app.post("/api/profile/password")
async def change_password(req: PasswordChangeRequest, request: Request):
    _check_rate_limit(request, "change_password", max_hits=5, window_secs=3600)
    user_data = _require_user(request)
    record = storage.get_user_by_id(user_data["user_id"])
    if not record:
        raise HTTPException(404, "User not found.")
    if not _verify_password(req.current_password, record["password_hash"]):
        raise HTTPException(401, "Current password is incorrect.")
    if len(req.new_password) < 8:
        raise HTTPException(400, "New password must be at least 8 characters.")

    record["password_hash"] = _hash_password(req.new_password)
    storage.save_user(record)
    _invalidate_user_cache(user_data["user_id"])
    user_audit.log(user_data["user_id"], "password_changed", user_data["email"], _client_ip(request))

    _pw_text = (
        f"Your Job Apply password was just changed.\n\n"
        f"If this was you, no action is needed.\n"
        f"If you didn't do this, reset your password at {_APP_URL}/"
    )
    _pw_html = _email_html(f"""
    <h2 style="color:#1A3C5E;margin:0 0 .75rem;font-size:1.25rem">Password changed</h2>
    <p style="margin:0 0 1rem;color:#374151">
      Your Job Apply password was just changed.
    </p>
    <p style="margin:0 0 1.5rem;color:#374151">
      If this was you, no action is needed. If you didn't make this change,
      reset your password immediately.
    </p>
    <a href="{_APP_URL}/"
       style="display:inline-block;background:#1A3C5E;color:#FFFFFF;text-decoration:none;
              padding:.75rem 1.5rem;border-radius:6px;font-weight:600;font-size:.95rem">
      Go to Job Apply &rarr;
    </a>""")
    emailed = _send_email(
        to=user_data["email"],
        subject="Job Apply — Password Changed",
        body=_pw_text,
        html=_pw_html,
    )
    return {"ok": True, "emailed": emailed}


@app.post("/api/profile/email")
async def change_email(req: EmailChangeRequest, request: Request):
    _check_rate_limit(request, "change_email", max_hits=5, window_secs=3600)
    user_data = _require_user(request)
    record = storage.get_user_by_id(user_data["user_id"])
    if not record:
        raise HTTPException(404, "User not found.")

    # Password auth required for email changes
    if not _verify_password(req.current_password, record.get("password_hash", "")):
        raise HTTPException(401, "Current password is incorrect.")

    new_email = req.new_email.strip().lower()
    if new_email == user_data["email"].lower():
        raise HTTPException(400, "New email is the same as your current email.")

    # Check if the new address is already in use
    if storage.get_user_by_email(new_email):
        raise HTTPException(409, "That email address is already registered.")

    old_email = user_data["email"]

    # Update the record and mark as unverified — save_user re-indexes by new email
    record["email"] = new_email
    record["email_verified"] = False
    storage.save_user(record)
    _invalidate_user_cache(record["user_id"])

    user_audit.log(record["user_id"], "email_changed", old_email,
                   _client_ip(request), new_email=new_email)

    # Send verification to the new address
    token = ev.create_token(record["user_id"], new_email)
    _send_verification_email(_NOTIFY_EMAIL or new_email, record.get("display_name", new_email), token)

    # Invalidate the current session — the email embedded in it is now stale
    response = JSONResponse({"ok": True, "message": "Email updated. Please verify your new address and log in again."})
    response.delete_cookie(_SESSION_COOKIE)
    response.delete_cookie("fly-force-instance-id")
    return response


@app.get("/api/config/model")
async def get_model(request: Request):
    _require_user(request)
    return {"model": _get_active_model(), "default": DEFAULT_MODEL}


_ALLOWED_MODELS: frozenset[str] = frozenset({
    "claude-opus-4-8",
    "claude-opus-4-5",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-haiku-4-5-20251001",
    "claude-haiku-4-5",
})


@app.put("/api/config/model")
async def set_model(request: Request):
    _require_admin(request)
    body = await request.json()
    model = body.get("model", "").strip()
    if not model:
        raise HTTPException(400, "model is required.")
    if model not in _ALLOWED_MODELS:
        raise HTTPException(400, f"Unknown model. Allowed: {', '.join(sorted(_ALLOWED_MODELS))}")
    _set_active_model(model)
    return {"ok": True, "model": model}


@app.get("/api/config/models")
async def list_models(request: Request):
    _require_admin(request)
    return {"models": sorted(_ALLOWED_MODELS), "active": _get_active_model()}


# ---------------------------------------------------------------------------
# Run endpoints
# ---------------------------------------------------------------------------

@app.post("/api/run")
async def create_run(req: RunRequest, request: Request, response: Response):
    user_data = _require_user(request)
    if user_data.get("role") == "admin":
        raise HTTPException(403, "Admin accounts cannot create runs")
    user_id   = user_data["user_id"]

    # Fetch user's resume and profile from Tigris
    resume_bytes = storage.get_resume(user_id)
    if not resume_bytes:
        raise HTTPException(400, "No master resume uploaded. Add one in your profile.")

    profile_text = storage.get_profile(user_id)
    if not profile_text:
        raise HTTPException(400, "No profile guide saved. Add one in your profile.")

    _evict_stale()

    active_count = sum(1 for r in _runs.values()
                       if r.get("user_id") == user_id and r.get("status") not in ("done", "error"))
    if active_count >= _MAX_ACTIVE_RUNS_PER_USER:
        raise HTTPException(429, "Too many active runs. Wait for an existing run to finish.")

    run_id = str(uuid.uuid4())
    q: Queue[dict | None] = Queue()
    _runs[run_id] = {"queue": q, "status": "queued", "result": None, "error": None,
                     "user_id": user_id}
    user_audit.log(user_id, "run_started", user_data["email"], _client_ip(request),
                   run_id=run_id, company=req.company, role=req.role)

    # Pin this browser session to the machine that owns this run's state
    if FLY_MACHINE_ID:
        response.set_cookie("fly-force-instance-id", FLY_MACHINE_ID, path="/", samesite="lax", httponly=True)

    def _run_fn(resume_path: Path, progress) -> WorkflowResult:
        config = WorkflowConfig(
            model=req.model or _get_active_model(),
            progress=progress,
            master_resume=resume_path,
            profile_text=profile_text[:_MAX_PROFILE_TEXT_LEN],
            user_id=user_id,
            user_label=user_data["email"],
        )
        job_posting = req.job_posting
        if req.jd_folder_id and not job_posting:
            job_posting = get_gdrive_job_posting(req.jd_folder_id, config) or ""
        result = run_workflow(
            job_posting=job_posting,
            company=req.company,
            role=req.role,
            contact=req.contact,
            config=config,
        )
        if req.app_id:
            _link_run_to_app(user_id=user_id, app_id=req.app_id, run_type="resume",
                             result_dir=result.run_dir, folder_url=result.folder_url or "")
            run_folder_id = (result.folder_url or "").rstrip("/").split("/")[-1]
            _trigger_match_scoring(user_id=user_id, app_id=req.app_id, job_posting=job_posting,
                                   resume_path=result.resume_path, profile_text=profile_text,
                                   user_label=user_data["email"], folder_id=run_folder_id)
            # If the JD was pasted (not loaded from a Drive folder), persist it as
            # job_description.md in the run's output folder and register a jd run link.
            if job_posting and not req.jd_folder_id and result.folder_url:
                if run_folder_id:
                    save_gdrive_job_posting(run_folder_id, job_posting, config)
                    _link_run_to_app(user_id=user_id, app_id=req.app_id, run_type="job_description",
                                     result_dir=result.run_dir, folder_url=result.folder_url)
        return result

    def _done_payload(result: WorkflowResult) -> dict:
        return {
            "run_id":               run_id,
            "framing_angle":        result.framing_angle,
            "folder_url":           result.folder_url,
            "app_id":               req.app_id,
            "replacements_warning": result.replacements_warning,
            "files": {
                "resume":       result.resume_path.name,
                "ats":          result.ats_path.name,
                "cover_letter": result.cover_letter_path.name,
            },
        }

    threading.Thread(
        target=_worker_thread,
        args=(_runs, run_id, user_id, user_data["email"], resume_bytes,
              _run_fn, _done_payload,
              "run_completed", "run_failed",
              lambda result, _rid=run_id, _co=req.company, _ro=req.role: {
                  "run_id": _rid, "company": _co, "role": _ro,
                  "folder_url": (result.folder_url if result else "") or "",
              }),
        daemon=True,
    ).start()
    return {"run_id": run_id, "machine_id": FLY_MACHINE_ID or None}


@app.get("/api/run/{run_id}/stream")
async def stream_run(run_id: str, request: Request):
    user_data = _require_user(request)
    if run_id not in _runs:
        raise HTTPException(404, "Run not found")
    if _runs[run_id].get("user_id") != user_data["user_id"] and user_data.get("role") != "admin":
        raise HTTPException(403, "Access denied")

    q    = _runs[run_id]["queue"]
    loop = asyncio.get_running_loop()

    async def generate():
        while True:
            try:
                msg = await loop.run_in_executor(None, lambda: q.get(timeout=30))
            except Empty:
                yield ": keepalive\n\n"
                continue
            if msg is None:
                break
            yield f"data: {json.dumps(msg)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/run/{run_id}/status")
async def run_status(run_id: str, request: Request):
    user_data = _require_user(request)
    run = _runs.get(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if run.get("user_id") != user_data["user_id"] and user_data.get("role") != "admin":
        raise HTTPException(403, "Access denied")
    return {"run_id": run_id, "status": run["status"], "error": run.get("error")}


@app.get("/api/run/{run_id}/files/{filename}")
async def get_file(run_id: str, filename: str, request: Request):
    user_data = _require_user(request)
    run = _runs.get(run_id)
    if not run or run["status"] != "done" or not run.get("result"):
        raise HTTPException(404, "Run not complete")
    if run.get("user_id") != user_data["user_id"] and user_data.get("role") != "admin":
        raise HTTPException(403, "Access denied")

    result: WorkflowResult = run["result"]
    file_path = (result.run_dir / filename).resolve()
    try:
        file_path.relative_to(result.run_dir.resolve())
    except ValueError:
        raise HTTPException(400, "Invalid filename")

    if not file_path.exists():
        raise HTTPException(404, "File not found")

    user_audit.log(user_data["user_id"], "file_downloaded", user_data["email"],
                   _client_ip(request), run_id=run_id, filename=filename, run_type="resume")

    return FileResponse(
        file_path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


# ---------------------------------------------------------------------------
# Google Drive — run listing + job posting fetch
# ---------------------------------------------------------------------------

@app.get("/api/gdrive/runs")
async def gdrive_list_runs(request: Request):
    """List this user's run folders from Google Drive (user subfolder + legacy flat)."""
    user_data  = _require_user(request)
    user_label = user_data["email"]
    config = WorkflowConfig(progress=lambda _: None, user_label=user_label)
    try:
        folders = list_gdrive_run_folders(user_label, config)
        return {"runs": folders, "drive_configured": len(folders) >= 0}
    except Exception as exc:
        return {"runs": [], "drive_configured": False, "error": str(exc)}


@app.get("/api/gdrive/runs/{folder_id}/job_posting")
async def gdrive_get_job_posting(folder_id: str, request: Request):
    """Fetch job_posting from a specific Drive folder (by Drive folder ID)."""
    user_data = _require_user(request)
    user_id   = user_data["user_id"]
    user_label = user_data["email"]
    # Verify the folder belongs to this user by checking their application records.
    # This is fast (Tigris lookup) and handles all linked_run folder types without
    # the 100-folder cap that the Drive listing has.
    from scripts import applications as app_store
    apps_result = app_store.list_applications(user_id)
    allowed_ids = {
        run.get("gdrive_folder_id")
        for app in (apps_result.get("items") or [])
        for run in (app.get("linked_runs") or [])
        if run.get("gdrive_folder_id")
    }
    if folder_id not in allowed_ids:
        raise HTTPException(403, "Access denied to this Drive folder")
    config = WorkflowConfig(progress=lambda _: None, user_label=user_label)
    text = get_gdrive_job_posting(folder_id, config)
    if text is None:
        raise HTTPException(404, "No job posting found in this Drive folder")
    return {"job_posting": text}


@app.post("/api/jd/format")
async def format_job_posting(request: Request):
    """Use Claude to convert a pasted plain-text JD into clean markdown for storage."""
    _require_user(request)
    body = await request.json()
    raw  = (body.get("job_posting") or "").strip()
    if not raw:
        raise HTTPException(400, "job_posting is required")
    system = "You are a document formatter. Convert the raw job description text into clean, well-structured markdown. Use ## for section headings, bullet lists for requirements/responsibilities, and preserve all content faithfully. Return only the markdown, no preamble."
    config = WorkflowConfig(progress=lambda _: None)
    try:
        md = claude(system, raw, max_tokens=4096, config=config)
    except Exception as e:
        raise HTTPException(500, f"Formatting failed: {e}")
    return {"markdown": md}


@app.put("/api/gdrive/runs/{folder_id}/job_posting")
async def gdrive_save_job_posting(folder_id: str, request: Request):
    """Upsert job_description.md in a specific Drive folder."""
    user_data  = _require_user(request)
    user_label = user_data["email"]
    body       = await request.json()
    markdown   = body.get("job_posting", "").strip()
    if not markdown:
        raise HTTPException(400, "job_posting field is required")
    try:
        user_folders = list_gdrive_run_folders(user_label, WorkflowConfig(progress=lambda _: None, user_label=user_label))
    except Exception:
        raise HTTPException(503, "Could not verify Drive folder ownership — please try again")
    allowed_ids = {f.get("id") for f in user_folders if f.get("id")}
    if folder_id not in allowed_ids:
        raise HTTPException(403, "Access denied to this Drive folder")
    config  = WorkflowConfig(progress=lambda _: None, user_label=user_label)
    success = save_gdrive_job_posting(folder_id, markdown, config)
    if not success:
        raise HTTPException(500, "Failed to save job description to Drive")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Run listing (for interview prep dropdown)
# ---------------------------------------------------------------------------

@app.get("/api/runs")
async def list_runs(request: Request):
    user_data = _require_user(request)
    user_dir  = OUTPUT_DIR / safe_filename(user_data["user_id"])
    runs = []
    if user_dir.exists():
        dirs = sorted(user_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        for d in dirs:
            if d.is_dir():
                runs.append({"folder": d.name})
    return {"runs": runs}


# ---------------------------------------------------------------------------
# Interview prep endpoints
# ---------------------------------------------------------------------------

@app.post("/api/prep")
async def create_prep(req: PrepRequest, request: Request, response: Response):
    user_data = _require_user(request)
    if user_data.get("role") == "admin":
        raise HTTPException(403, "Admin accounts cannot create prep runs")
    user_id   = user_data["user_id"]

    _evict_stale()

    if req.round_type not in ROUND_TYPES:
        raise HTTPException(400, f"round_type must be one of: {', '.join(ROUND_TYPES)}")

    resume_bytes = storage.get_resume(user_id)
    if not resume_bytes:
        raise HTTPException(400, "No master resume uploaded. Add one in your profile.")

    profile_text = storage.get_profile(user_id)
    if not profile_text:
        raise HTTPException(400, "No profile guide saved. Add one in your profile.")

    active_prep_count = sum(1 for p in _preps.values()
                            if p.get("user_id") == user_id and p.get("status") not in ("done", "error"))
    if active_prep_count >= _MAX_ACTIVE_RUNS_PER_USER:
        raise HTTPException(429, "Too many active prep runs. Wait for an existing run to finish.")

    prep_id = str(uuid.uuid4())
    q: Queue[dict | None] = Queue()
    _preps[prep_id] = {"queue": q, "status": "queued", "result": None, "error": None,
                       "user_id": user_id}
    user_audit.log(user_id, "prep_started", user_data["email"], _client_ip(request),
                   prep_id=prep_id, company=req.company, role=req.role,
                   round_type=req.round_type)

    if FLY_MACHINE_ID:
        response.set_cookie("fly-force-instance-id", FLY_MACHINE_ID, path="/", samesite="lax", httponly=True)

    def _prep_fn(resume_path: Path, progress) -> InterviewPrepResult:
        config = InterviewPrepConfig(
            round_type=req.round_type,
            focus=req.focus or "",
            interviewer=req.interviewer or "",
            model=req.model or _get_active_model(),
            progress=progress,
            master_resume=resume_path,
            profile_text=profile_text[:_MAX_PROFILE_TEXT_LEN],
            user_id=user_id,
            user_label=user_data["email"],
        )
        result = generate_interview_prep(
            job_posting=req.job_posting,
            company=req.company,
            role=req.role,
            config=config,
        )
        if req.app_id:
            _link_run_to_app(user_id=user_id, app_id=req.app_id, run_type="interview_prep",
                             result_dir=result.run_dir, folder_url=result.folder_url or "")
        return result

    def _prep_done_payload(result: InterviewPrepResult) -> dict:
        return {
            "prep_id":    prep_id,
            "folder_url": result.folder_url,
            "app_id":     req.app_id,
            "files":      {"prep": result.prep_path.name},
        }

    threading.Thread(
        target=_worker_thread,
        args=(_preps, prep_id, user_id, user_data["email"], resume_bytes,
              _prep_fn, _prep_done_payload,
              "prep_completed", "prep_failed",
              lambda result, _pid=prep_id, _co=req.company, _ro=req.role, _rt=req.round_type: {
                  "prep_id": _pid, "company": _co, "role": _ro, "round_type": _rt,
                  "folder_url": (result.folder_url if result else "") or "",
              }),
        daemon=True,
    ).start()
    return {"prep_id": prep_id, "machine_id": FLY_MACHINE_ID or None}


@app.get("/api/prep/{prep_id}/stream")
async def stream_prep(prep_id: str, request: Request):
    user_data = _require_user(request)
    if prep_id not in _preps:
        raise HTTPException(404, "Prep run not found")
    if _preps[prep_id].get("user_id") != user_data["user_id"] and user_data.get("role") != "admin":
        raise HTTPException(403, "Access denied")

    q    = _preps[prep_id]["queue"]
    loop = asyncio.get_running_loop()

    async def generate():
        while True:
            try:
                msg = await loop.run_in_executor(None, lambda: q.get(timeout=30))
            except Empty:
                yield ": keepalive\n\n"
                continue
            if msg is None:
                break
            yield f"data: {json.dumps(msg)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/prep/{prep_id}/status")
async def prep_status(prep_id: str, request: Request):
    user_data = _require_user(request)
    prep = _preps.get(prep_id)
    if not prep:
        raise HTTPException(404, "Prep run not found")
    if prep.get("user_id") != user_data["user_id"] and user_data.get("role") != "admin":
        raise HTTPException(403, "Access denied")
    return {"prep_id": prep_id, "status": prep["status"], "error": prep.get("error")}


@app.get("/api/prep/{prep_id}/files/{filename}")
async def get_prep_file(prep_id: str, filename: str, request: Request):
    user_data = _require_user(request)
    prep = _preps.get(prep_id)
    if not prep or prep["status"] != "done" or not prep.get("result"):
        raise HTTPException(404, "Prep not complete")
    if prep.get("user_id") != user_data["user_id"] and user_data.get("role") != "admin":
        raise HTTPException(403, "Access denied")

    result: InterviewPrepResult = prep["result"]
    file_path = (result.run_dir / filename).resolve()
    try:
        file_path.relative_to(result.run_dir.resolve())
    except ValueError:
        raise HTTPException(400, "Invalid filename")

    if not file_path.exists():
        raise HTTPException(404, "File not found")

    user_audit.log(user_data["user_id"], "file_downloaded", user_data["email"],
                   _client_ip(request), prep_id=prep_id, filename=filename,
                   run_type="interview_prep")

    return FileResponse(
        file_path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


# ---------------------------------------------------------------------------
# Optimize Run endpoints
# ---------------------------------------------------------------------------

@app.post("/api/optimize")
async def create_optimize(req: OptimizeRequest, request: Request, response: Response):
    user_data = _require_user(request)
    if user_data.get("role") == "admin":
        raise HTTPException(403, "Admin accounts cannot create optimize runs")
    user_id = user_data["user_id"]

    _evict_stale()

    instruction = req.instruction.strip()
    if not instruction:
        raise HTTPException(400, "instruction is required")
    if len(instruction) > _MAX_OPTIMIZE_INSTRUCTION_LEN:
        raise HTTPException(400, f"instruction must be at most {_MAX_OPTIMIZE_INSTRUCTION_LEN} characters")
    if not req.optimize_resume and not req.optimize_cover_letter:
        raise HTTPException(400, "Select at least one document to optimize")

    # Verify the folder belongs to this user via their application records —
    # same authorization check as /api/gdrive/runs/{folder_id}/job_posting.
    from scripts import applications as app_store
    apps_result = app_store.list_applications(user_id)
    allowed_ids = {
        run.get("gdrive_folder_id")
        for app_rec in (apps_result.get("items") or [])
        for run in (app_rec.get("linked_runs") or [])
        if run.get("gdrive_folder_id")
    }
    if req.folder_id not in allowed_ids:
        raise HTTPException(403, "Access denied to this Drive folder")

    active_count = sum(1 for o in _optimizations.values()
                       if o.get("user_id") == user_id and o.get("status") not in ("done", "error"))
    if active_count >= _MAX_ACTIVE_RUNS_PER_USER:
        raise HTTPException(429, "Too many active optimize runs. Wait for an existing run to finish.")

    optimize_id = str(uuid.uuid4())
    q: Queue[dict | None] = Queue()
    _optimizations[optimize_id] = {"queue": q, "status": "queued", "result": None,
                                   "error": None, "user_id": user_id}
    user_audit.log(user_id, "optimize_started", user_data["email"], _client_ip(request),
                   optimize_id=optimize_id, company=req.company, role=req.role,
                   folder_id=req.folder_id)

    if FLY_MACHINE_ID:
        response.set_cookie("fly-force-instance-id", FLY_MACHINE_ID, path="/", samesite="lax", httponly=True)

    def _opt_fn(resume_path: Path, progress) -> OptimizeResult:
        config = OptimizeConfig(
            folder_id=req.folder_id,
            instruction=instruction,
            company=req.company,
            role=req.role,
            optimize_resume=req.optimize_resume,
            optimize_cover_letter=req.optimize_cover_letter,
            model=req.model or _get_active_model(),
            progress=progress,
            user_id=user_id,
            user_label=user_data["email"],
        )
        result = optimize_run(config)
        _link_run_to_app(user_id=user_id, app_id=req.app_id, run_type="optimize",
                         result_dir=result.run_dir, folder_url=result.folder_url or "")
        # Re-score the application against its job posting using the optimized
        # resume, mirroring the post-run scoring on /api/run. Only meaningful when
        # the optimize touched the resume (cover-letter-only runs leave it None).
        if result.resume_path:
            profile_text = storage.get_profile(user_id) or ""
            jd_text = get_gdrive_job_posting(req.folder_id, config) or ""
            if profile_text and jd_text:
                _trigger_match_scoring(
                    user_id=user_id, app_id=req.app_id, job_posting=jd_text,
                    resume_path=result.resume_path, profile_text=profile_text,
                    user_label=user_data["email"], folder_id=req.folder_id,
                )
        return result

    def _opt_done_payload(result: OptimizeResult) -> dict:
        files = {}
        if result.resume_path:
            files["resume"] = result.resume_path.name
        if result.ats_path:
            files["ats"] = result.ats_path.name
        if result.cover_letter_path:
            files["cover_letter"] = result.cover_letter_path.name
        return {
            "optimize_id":          optimize_id,
            "folder_url":           result.folder_url,
            "app_id":               req.app_id,
            "change_summary":       result.change_summary,
            "replacements_warning": result.replacements_warning,
            "files":                files,
        }

    threading.Thread(
        target=_worker_thread,
        args=(_optimizations, optimize_id, user_id, user_data["email"], b"",
              _opt_fn, _opt_done_payload,
              "optimize_completed", "optimize_failed",
              lambda result, _oid=optimize_id, _co=req.company, _ro=req.role: {
                  "optimize_id": _oid, "company": _co, "role": _ro,
                  "folder_url": (result.folder_url if result else "") or "",
              }),
        daemon=True,
    ).start()
    return {"optimize_id": optimize_id, "machine_id": FLY_MACHINE_ID or None}


@app.get("/api/optimize/{optimize_id}/stream")
async def stream_optimize(optimize_id: str, request: Request):
    user_data = _require_user(request)
    if optimize_id not in _optimizations:
        raise HTTPException(404, "Optimize run not found")
    if _optimizations[optimize_id].get("user_id") != user_data["user_id"] and user_data.get("role") != "admin":
        raise HTTPException(403, "Access denied")

    q    = _optimizations[optimize_id]["queue"]
    loop = asyncio.get_running_loop()

    async def generate():
        while True:
            try:
                msg = await loop.run_in_executor(None, lambda: q.get(timeout=30))
            except Empty:
                yield ": keepalive\n\n"
                continue
            if msg is None:
                break
            yield f"data: {json.dumps(msg)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/optimize/{optimize_id}/status")
async def optimize_status(optimize_id: str, request: Request):
    user_data = _require_user(request)
    opt = _optimizations.get(optimize_id)
    if not opt:
        raise HTTPException(404, "Optimize run not found")
    if opt.get("user_id") != user_data["user_id"] and user_data.get("role") != "admin":
        raise HTTPException(403, "Access denied")
    return {"optimize_id": optimize_id, "status": opt["status"], "error": opt.get("error")}


@app.get("/api/optimize/{optimize_id}/files/{filename}")
async def get_optimize_file(optimize_id: str, filename: str, request: Request):
    user_data = _require_user(request)
    opt = _optimizations.get(optimize_id)
    if not opt or opt["status"] != "done" or not opt.get("result"):
        raise HTTPException(404, "Optimize run not complete")
    if opt.get("user_id") != user_data["user_id"] and user_data.get("role") != "admin":
        raise HTTPException(403, "Access denied")

    result: OptimizeResult = opt["result"]
    file_path = (result.run_dir / filename).resolve()
    try:
        file_path.relative_to(result.run_dir.resolve())
    except ValueError:
        raise HTTPException(400, "Invalid filename")

    if not file_path.exists():
        raise HTTPException(404, "File not found")

    user_audit.log(user_data["user_id"], "file_downloaded", user_data["email"],
                   _client_ip(request), optimize_id=optimize_id, filename=filename,
                   run_type="optimize")

    return FileResponse(
        file_path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


# ---------------------------------------------------------------------------
# Postman collection — serve live JSON for the API docs page
# ---------------------------------------------------------------------------
@app.get("/api/postman")
async def get_postman_collection():
    import json as _json
    path = Path("JobApply.postman_collection.json")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Collection not found")
    return JSONResponse(content=_json.loads(path.read_text()))


# ---------------------------------------------------------------------------
# Static frontend — mounted last; auth middleware handles redirects
# ---------------------------------------------------------------------------
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
