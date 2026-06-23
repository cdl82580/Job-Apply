"""
routers/admin.py — Admin-only API endpoints.

All endpoints require role == "admin" in the session token.

Endpoints:
  GET  /api/admin/users                                  — list all users
  PUT  /api/admin/users/{user_id}/role                   — set user role
  GET  /api/admin/applications                           — all apps, all users
  GET  /api/admin/users/{user_id}/applications           — one user's apps
  GET  /api/admin/applications/{user_id}/{app_id}        — full record
  PUT  /api/admin/applications/{user_id}/{app_id}        — update
  DELETE /api/admin/applications/{user_id}/{app_id}      — delete
  GET  /api/admin/runs                                   — active in-memory runs
"""

from __future__ import annotations

import ipaddress
import socket
import time
import urllib.parse
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel


_PRIVATE_NETS = [
    ipaddress.ip_network(n) for n in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "127.0.0.0/8", "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10",
    )
]


def _is_ssrf_url(url: str) -> bool:
    """Return True if the URL resolves to a private/loopback/link-local address."""
    try:
        parsed = urllib.parse.urlparse(url)
        host   = parsed.hostname or ""
        addrs  = socket.getaddrinfo(host, None)
        for _, _, _, _, sockaddr in addrs:
            ip = ipaddress.ip_address(sockaddr[0])
            if any(ip in net for net in _PRIVATE_NETS):
                return True
    except Exception:
        pass
    return False

from scripts import storage
from scripts import applications as app_store
from scripts import user_audit
from scripts import email_verification as ev

router = APIRouter(prefix="/api/admin", tags=["admin"])

VALID_ROLES = {"user", "admin"}


# ---------------------------------------------------------------------------
# Auth guard — imported lazily to avoid circular import
# ---------------------------------------------------------------------------

def _admin(request: Request) -> dict:
    from api import _require_admin  # noqa: PLC0415
    return _require_admin(request)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class RoleUpdate(BaseModel):
    role: str


class UserUpdate(BaseModel):
    display_name:   str | None = None
    email:          str | None = None
    role:           str | None = None
    email_verified: bool | None = None
    active:         bool | None = None


class CommentCreate(BaseModel):
    text: str

class CommentUpdate(BaseModel):
    text: str

class AppUpdate(BaseModel):
    company: str | None = None
    domain: str | None = None
    role_title: str | None = None
    status: str | None = None
    priority: str | None = None
    date_applied: str | None = None
    dua: bool | None = None
    job_source: str | None = None
    location: str | None = None
    salary_range: str | None = None
    recruiter_name: str | None = None
    recruiter_email: str | None = None
    url: str | None = None


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

@router.get("/users")
async def list_users(request: Request):
    admin = _admin(request)
    users = storage.list_all_users()
    result = []
    for u in users:
        uid = u["user_id"]
        try:
            apps = app_store.list_applications(uid)
            app_count = apps["total"]
        except Exception:
            app_count = 0

        last_login = user_audit.get_last_login(uid)

        result.append({
            "user_id":        uid,
            "email":          u.get("email", ""),
            "display_name":   u.get("display_name", ""),
            "role":           u.get("role", "user"),
            "active":         u.get("active", True),
            "email_verified": u.get("email_verified", True),
            "created_at":     u.get("created_at", ""),
            "last_login":     last_login,
            "app_count":      app_count,
            "has_resume":     storage.has_resume(uid),
        })

    result.sort(key=lambda u: u.get("created_at", ""))
    return result


@router.put("/users/{user_id}")
async def update_user(user_id: str, body: UserUpdate, request: Request):
    """Edit display name, email, role, email_verified, or active status."""
    admin = _admin(request)
    user  = storage.get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")

    changes: dict = {}

    if body.display_name is not None:
        changes["display_name"] = {"from": user.get("display_name"), "to": body.display_name}
        user["display_name"] = body.display_name.strip()

    if body.email is not None:
        new_email = body.email.strip().lower()
        if new_email != user.get("email", ""):
            # Check email not already taken
            if storage.get_user_by_email(new_email):
                raise HTTPException(400, "That email is already in use by another account")
            changes["email"] = {"from": user.get("email"), "to": new_email}
            storage.update_user_email(user, new_email)
            # user dict now has updated email; skip save_user below for email field
            user["email"] = new_email

    if body.role is not None:
        if body.role not in VALID_ROLES:
            raise HTTPException(400, f"role must be one of: {', '.join(sorted(VALID_ROLES))}")
        changes["role"] = {"from": user.get("role", "user"), "to": body.role}
        user["role"] = body.role

    if body.email_verified is not None:
        changes["email_verified"] = {"from": user.get("email_verified"), "to": body.email_verified}
        user["email_verified"] = body.email_verified

    if body.active is not None:
        changes["active"] = {"from": user.get("active", True), "to": body.active}
        user["active"] = body.active

    storage.save_user(user)
    # Invalidate cached user record so role/active changes take effect immediately
    if changes:
        try:
            from api import _invalidate_user_cache
            _invalidate_user_cache(user_id)
        except Exception:
            pass
    user_audit.log(user_id, "admin_user_updated", admin["email"],
                   changes=changes, changed_by=admin["email"])

    return {
        "ok":             True,
        "user_id":        user_id,
        "email":          user.get("email"),
        "display_name":   user.get("display_name"),
        "role":           user.get("role", "user"),
        "active":         user.get("active", True),
        "email_verified": user.get("email_verified", True),
    }


@router.post("/users/{user_id}/resend-verification")
async def admin_resend_verification(user_id: str, request: Request):
    admin = _admin(request)
    user  = storage.get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if user.get("email_verified", True):
        return {"ok": True, "already_verified": True}

    from api import _send_verification_email  # noqa: PLC0415
    token = ev.create_token(user_id, user["email"])
    sent  = _send_verification_email(user["email"], user.get("display_name", "there"), token)
    user_audit.log(user_id, "admin_verification_resent", admin["email"],
                   sent=sent, target=user["email"])
    return {"ok": True, "sent": sent}


# Keep the old role-only endpoint for backward compat with Slack bot
@router.put("/users/{user_id}/role")
async def set_user_role(user_id: str, body: RoleUpdate, request: Request):
    admin = _admin(request)
    if body.role not in VALID_ROLES:
        raise HTTPException(400, f"role must be one of: {', '.join(sorted(VALID_ROLES))}")
    user = storage.get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")
    old_role = user.get("role", "user")
    user["role"] = body.role
    storage.save_user(user)
    user_audit.log(user_id, "role_changed", admin["email"],
                   old_role=old_role, new_role=body.role, changed_by=admin["email"])
    return {"ok": True, "user_id": user_id, "role": body.role}


# ---------------------------------------------------------------------------
# Cross-user application access
# ---------------------------------------------------------------------------

@router.get("/applications")
async def list_all_applications(request: Request):
    """Return all applications across all users with a '_user_email' field added."""
    _admin(request)
    users = storage.list_all_users()
    all_apps = []
    for u in users:
        uid = u["user_id"]
        try:
            result = app_store.list_applications(uid)
            items  = result.get("items", result) if isinstance(result, dict) else result
            for item in items:
                item["_user_id"]    = uid
                item["_user_email"] = u.get("email", "")
            all_apps.extend(items)
        except Exception:
            pass

    all_apps.sort(key=lambda a: a.get("last_updated", ""), reverse=True)
    return all_apps


@router.get("/users/{user_id}/applications")
async def list_user_applications(user_id: str, request: Request):
    _admin(request)
    if not storage.get_user_by_id(user_id):
        raise HTTPException(404, "User not found")
    result = app_store.list_applications(user_id)
    return result.get("items", result) if isinstance(result, dict) else result


@router.get("/applications/{user_id}/{app_id}")
async def get_application(user_id: str, app_id: str, request: Request):
    admin = _admin(request)
    record = app_store.get_application(user_id, app_id)
    if not record:
        raise HTTPException(404, "Application not found")
    return record


@router.put("/applications/{user_id}/{app_id}")
async def update_application(user_id: str, app_id: str, body: AppUpdate, request: Request):
    admin = _admin(request)
    record = app_store.get_application(user_id, app_id)
    if not record:
        raise HTTPException(404, "Application not found")

    if body.url and not body.url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must start with http:// or https://")

    updates = body.model_dump(exclude_unset=True)
    record.update(updates)
    record["updated_at"] = _now()
    record["updated_by"] = admin["email"]
    record.setdefault("audit_log", []).append({
        "id":        __import__("uuid").uuid4().__str__(),
        "action":    "admin_updated",
        "actor":     admin["email"],
        "timestamp": _now(),
        "ip":        None,
        "changes":   {k: updates[k] for k in updates},
    })

    record = app_store.save_application(user_id, record)
    user_audit.log(user_id, "admin_updated", admin["email"],
                   app_id=app_id, fields=list(updates.keys()))
    return record


@router.delete("/applications/{user_id}/{app_id}", status_code=204)
async def delete_application(user_id: str, app_id: str, request: Request):
    admin = _admin(request)
    record = app_store.get_application(user_id, app_id)
    if not record:
        raise HTTPException(404, "Application not found")

    record.setdefault("audit_log", []).append({
        "id":        __import__("uuid").uuid4().__str__(),
        "action":    "admin_deleted",
        "actor":     admin["email"],
        "timestamp": _now(),
        "ip":        None,
        "changes":   None,
    })
    app_store.save_deleted_tombstone(user_id, record)
    app_store.delete_application(user_id, app_id)
    user_audit.log(user_id, "admin_deleted", admin["email"],
                   app_id=app_id, company=record.get("company"),
                   role_title=record.get("role_title"))


# ---------------------------------------------------------------------------
# Admin comment management (any user's application)
# ---------------------------------------------------------------------------

def _get_app_or_404(user_id: str, app_id: str) -> dict:
    record = app_store.get_application(user_id, app_id)
    if not record:
        raise HTTPException(404, "Application not found")
    return record


@router.post("/applications/{user_id}/{app_id}/comments", status_code=201)
async def add_comment(user_id: str, app_id: str, body: CommentCreate, request: Request):
    admin  = _admin(request)
    record = _get_app_or_404(user_id, app_id)

    if not body.text.strip():
        raise HTTPException(400, "Comment text cannot be empty")

    now = _now()
    comment = {
        "id":         str(uuid.uuid4()),
        "text":       body.text.strip(),
        "created_at": now,
        "updated_at": now,
        "author":     admin["email"],
    }
    record.setdefault("comments", []).append(comment)
    record.setdefault("audit_log", []).append({
        "id": str(uuid.uuid4()), "action": "admin_comment_added",
        "actor": admin["email"], "timestamp": now, "ip": None,
        "details": {"preview": comment["text"][:60]},
    })
    record["updated_at"] = now
    record["updated_by"] = admin["email"]
    app_store.save_application(user_id, record)
    user_audit.log(user_id, "admin_comment_added", admin["email"],
                   app_id=app_id, comment_id=comment["id"],
                   preview=comment["text"][:60])
    return comment


@router.put("/applications/{user_id}/{app_id}/comments/{comment_id}")
async def update_comment(user_id: str, app_id: str, comment_id: str,
                         body: CommentUpdate, request: Request):
    admin  = _admin(request)
    record = _get_app_or_404(user_id, app_id)

    if not body.text.strip():
        raise HTTPException(400, "Comment text cannot be empty")

    for c in record.get("comments", []):
        if c["id"] == comment_id:
            old_text   = c["text"]
            c["text"]       = body.text.strip()
            c["updated_at"] = _now()
            record.setdefault("audit_log", []).append({
                "id": str(uuid.uuid4()), "action": "admin_comment_edited",
                "actor": admin["email"], "timestamp": _now(), "ip": None,
                "details": {"from": old_text[:60], "to": c["text"][:60]},
            })
            record["updated_at"] = _now()
            record["updated_by"] = admin["email"]
            app_store.save_application(user_id, record)
            user_audit.log(user_id, "admin_comment_edited", admin["email"],
                           app_id=app_id, comment_id=comment_id)
            return c

    raise HTTPException(404, "Comment not found")


@router.delete("/applications/{user_id}/{app_id}/comments/{comment_id}", status_code=204)
async def delete_comment(user_id: str, app_id: str, comment_id: str, request: Request):
    admin  = _admin(request)
    record = _get_app_or_404(user_id, app_id)

    before = len(record.get("comments", []))
    record["comments"] = [c for c in record.get("comments", []) if c["id"] != comment_id]
    if len(record["comments"]) == before:
        raise HTTPException(404, "Comment not found")

    record.setdefault("audit_log", []).append({
        "id": str(uuid.uuid4()), "action": "admin_comment_deleted",
        "actor": admin["email"], "timestamp": _now(), "ip": None,
        "details": {"comment_id": comment_id},
    })
    record["updated_at"] = _now()
    record["updated_by"] = admin["email"]
    app_store.save_application(user_id, record)
    user_audit.log(user_id, "admin_comment_deleted", admin["email"],
                   app_id=app_id, comment_id=comment_id)


# ---------------------------------------------------------------------------
# Active in-memory run listing
# ---------------------------------------------------------------------------

@router.get("/runs")
async def list_all_runs(request: Request):
    """Return ALL agent runs across all users, sourced from the audit log.
    Includes resume runs, interview prep runs, and scoring runs. Each audit
    event corresponds to one actual run attempt (no Drive folder deduplication)."""
    _admin(request)

    # Build (user_id, app_id) → {company, role} for enriching match_scored rows
    app_cache: dict[tuple[str, str], dict] = {}
    try:
        for uid_rec in storage.list_all_users():
            uid = uid_rec.get("user_id", "")
            for app_sum in app_store.list_applications(uid).get("items", []):
                full = app_store.get_application(uid, app_sum["id"])
                if not full:
                    continue
                app_cache[(uid, full["id"])] = {
                    "company": full.get("company", ""),
                    "role":    full.get("role_title", ""),
                }
    except Exception:
        pass

    users = storage.list_all_users()

    # run_id / prep_id / "score_<event_id>" → run record
    runs_by_key: dict[str, dict] = {}

    for uid_rec in users:
        uid        = uid_rec.get("user_id", "")
        user_email = uid_rec.get("email", "")
        events     = user_audit.get_events(uid)

        for event in events:
            action  = event.get("action", "")
            details = event.get("details") or {}
            ts      = event.get("timestamp", "")

            if action == "run_started":
                run_id = details.get("run_id", "")
                if not run_id or run_id in runs_by_key:
                    continue
                runs_by_key[run_id] = {
                    "id":           run_id,
                    "type":         "resume",
                    "company":      details.get("company", ""),
                    "role":         details.get("role", ""),
                    "user_id":      uid,
                    "user_email":   user_email,
                    "created_at":   ts,
                    "status":       "running",
                    "web_view_link": "",
                    "app_id":       "",
                    "app_company":  "",
                    "app_role":     "",
                }

            elif action == "prep_started":
                prep_id = details.get("prep_id", "")
                if not prep_id or prep_id in runs_by_key:
                    continue
                runs_by_key[prep_id] = {
                    "id":           prep_id,
                    "type":         "interview_prep",
                    "company":      details.get("company", ""),
                    "role":         details.get("role", ""),
                    "round_type":   details.get("round_type", ""),
                    "user_id":      uid,
                    "user_email":   user_email,
                    "created_at":   ts,
                    "status":       "running",
                    "web_view_link": "",
                    "app_id":       "",
                    "app_company":  "",
                    "app_role":     "",
                }

            elif action == "match_scored":
                score_key = f"score_{event['id']}"
                app_id    = details.get("app_id", "")
                app_info  = app_cache.get((uid, app_id), {})
                runs_by_key[score_key] = {
                    "id":             event["id"],
                    "type":           "scoring",
                    "company":        app_info.get("company", ""),
                    "role":           app_info.get("role", ""),
                    "user_id":        uid,
                    "user_email":     user_email,
                    "created_at":     ts,
                    "status":         "complete",
                    "web_view_link":  "",
                    "app_id":         app_id,
                    "app_company":    app_info.get("company", ""),
                    "app_role":       app_info.get("role", ""),
                    "score":          details.get("score"),
                    "score_category": details.get("category", ""),
                }

            elif action in ("run_completed", "run_failed"):
                run_id = details.get("run_id", "")
                if not run_id:
                    continue
                status = "complete" if action == "run_completed" else "error"
                if run_id in runs_by_key:
                    runs_by_key[run_id]["status"] = status
                    if details.get("folder_url"):
                        runs_by_key[run_id]["web_view_link"] = details["folder_url"]
                    if details.get("error"):
                        runs_by_key[run_id]["error"] = details["error"]
                else:
                    # run_started evicted from capped audit log — reconstruct from end event
                    runs_by_key[run_id] = {
                        "id":           run_id,
                        "type":         "resume",
                        "company":      details.get("company", ""),
                        "role":         details.get("role", ""),
                        "user_id":      uid,
                        "user_email":   user_email,
                        "created_at":   ts,
                        "status":       status,
                        "web_view_link": details.get("folder_url", ""),
                        "app_id":       "",
                        "app_company":  "",
                        "app_role":     "",
                        "error":        details.get("error", ""),
                    }

            elif action in ("prep_completed", "prep_failed"):
                prep_id = details.get("prep_id", "")
                if not prep_id:
                    continue
                status = "complete" if action == "prep_completed" else "error"
                if prep_id in runs_by_key:
                    runs_by_key[prep_id]["status"] = status
                    if details.get("folder_url"):
                        runs_by_key[prep_id]["web_view_link"] = details["folder_url"]
                    if details.get("error"):
                        runs_by_key[prep_id]["error"] = details["error"]
                else:
                    runs_by_key[prep_id] = {
                        "id":           prep_id,
                        "type":         "interview_prep",
                        "company":      details.get("company", ""),
                        "role":         details.get("role", ""),
                        "user_id":      uid,
                        "user_email":   user_email,
                        "created_at":   ts,
                        "status":       status,
                        "web_view_link": details.get("folder_url", ""),
                        "app_id":       "",
                        "app_company":  "",
                        "app_role":     "",
                        "error":        details.get("error", ""),
                    }

            elif action == "optimize_started":
                opt_id = details.get("run_id", "") or f"opt_{event['id']}"
                if opt_id in runs_by_key:
                    continue
                app_id   = details.get("app_id", "")
                app_info = app_cache.get((uid, app_id), {})
                runs_by_key[opt_id] = {
                    "id":           opt_id,
                    "type":         "optimize",
                    "company":      app_info.get("company", details.get("company", "")),
                    "role":         app_info.get("role", details.get("role", "")),
                    "user_id":      uid,
                    "user_email":   user_email,
                    "created_at":   ts,
                    "status":       "running",
                    "web_view_link": "",
                    "app_id":       app_id,
                    "app_company":  app_info.get("company", ""),
                    "app_role":     app_info.get("role", ""),
                }

            elif action in ("optimize_completed", "optimize_failed"):
                opt_id = details.get("run_id", "")
                if not opt_id:
                    continue
                status = "complete" if action == "optimize_completed" else "error"
                if opt_id in runs_by_key:
                    runs_by_key[opt_id]["status"] = status
                    if details.get("folder_url"):
                        runs_by_key[opt_id]["web_view_link"] = details["folder_url"]

            elif action == "aq_started":
                aq_id = details.get("aq_id", "") or f"aq_{event['id']}"
                if aq_id in runs_by_key:
                    continue
                app_id   = details.get("app_id", "")
                app_info = app_cache.get((uid, app_id), {})
                runs_by_key[aq_id] = {
                    "id":           aq_id,
                    "type":         "aq",
                    "company":      app_info.get("company", details.get("company", "")),
                    "role":         app_info.get("role", details.get("role", "")),
                    "user_id":      uid,
                    "user_email":   user_email,
                    "created_at":   ts,
                    "status":       "running",
                    "web_view_link": "",
                    "app_id":       app_id,
                    "app_company":  app_info.get("company", ""),
                    "app_role":     app_info.get("role", ""),
                }

            elif action in ("aq_completed", "aq_failed"):
                aq_id = details.get("aq_id", "")
                if not aq_id:
                    continue
                status = "complete" if action == "aq_completed" else "error"
                if aq_id in runs_by_key:
                    runs_by_key[aq_id]["status"] = status
                    if details.get("folder_url"):
                        runs_by_key[aq_id]["web_view_link"] = details["folder_url"]

            elif action == "thankyou_started":
                ty_id = details.get("ty_id", "") or f"ty_{event['id']}"
                if ty_id in runs_by_key:
                    continue
                app_id   = details.get("app_id", "")
                app_info = app_cache.get((uid, app_id), {})
                runs_by_key[ty_id] = {
                    "id":           ty_id,
                    "type":         "thank_you",
                    "company":      app_info.get("company", details.get("company", "")),
                    "role":         app_info.get("role", details.get("role", "")),
                    "user_id":      uid,
                    "user_email":   user_email,
                    "created_at":   ts,
                    "status":       "running",
                    "web_view_link": details.get("folder_url", ""),
                    "app_id":       app_id,
                    "app_company":  app_info.get("company", ""),
                    "app_role":     app_info.get("role", ""),
                }

            elif action in ("thankyou_completed", "thankyou_failed"):
                ty_id = details.get("ty_id", "")
                if not ty_id:
                    continue
                status = "complete" if action == "thankyou_completed" else "error"
                if ty_id in runs_by_key:
                    runs_by_key[ty_id]["status"] = status
                    if details.get("folder_url"):
                        runs_by_key[ty_id]["web_view_link"] = details["folder_url"]

    # Override status for still-active in-memory runs; surface any not yet in audit
    from api import _runs, _preps  # noqa: PLC0415
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    runs_by_run_id = {r["id"]: r for r in runs_by_key.values()}

    for rid, r in list(_runs.items()):
        if rid in runs_by_run_id:
            runs_by_run_id[rid]["status"] = r.get("status", "running")
        else:
            user = storage.get_user_by_id(r.get("user_id", "")) or {}
            runs_by_key[rid] = {
                "id": rid, "type": "resume", "company": "", "role": "",
                "user_id": r.get("user_id", ""), "user_email": user.get("email", ""),
                "created_at": now_iso, "status": r.get("status", "running"),
                "web_view_link": "", "app_id": "", "app_company": "", "app_role": "",
            }

    for pid, p in list(_preps.items()):
        if pid in runs_by_run_id:
            runs_by_run_id[pid]["status"] = p.get("status", "running")
        else:
            user = storage.get_user_by_id(p.get("user_id", "")) or {}
            runs_by_key[pid] = {
                "id": pid, "type": "interview_prep", "company": "", "role": "",
                "user_id": p.get("user_id", ""), "user_email": user.get("email", ""),
                "created_at": now_iso, "status": p.get("status", "running"),
                "web_view_link": "", "app_id": "", "app_company": "", "app_role": "",
            }

    # ── Drive fallback: pick up historical / JD-capture runs not in audit log ──
    # Drive folder names are {safe_filename(company)}_{safe_filename(role)}.
    # We normalize both sides the same way for matching: strip non-alphanumeric.
    import re as _re  # noqa: PLC0415
    from apply import GDRIVE_PARENT_FOLDER_ID, WorkflowConfig, _gdrive_service, _FOLDER_MIME  # noqa: PLC0415

    def _norm(s: str) -> str:
        return _re.sub(r"[^a-z0-9]", "", s.lower())

    # Set of (user_email_norm, folder_name_norm) already covered by audit events
    covered: set[tuple[str, str]] = set()
    for run in runs_by_key.values():
        ue = _norm(run.get("user_email", ""))
        co = run.get("company", "")
        ro = run.get("role", "")
        if co or ro:
            covered.add((ue, _norm(f"{co}_{ro}")))

    # Build folder_id → app info from linked_runs (for JD folder enrichment)
    folder_meta_map: dict[str, dict] = {}
    try:
        for uid_rec in users:
            uid = uid_rec.get("user_id", "")
            for app_sum in app_store.list_applications(uid).get("items", []):
                full = app_store.get_application(uid, app_sum["id"])
                if not full:
                    continue
                for lrun in full.get("linked_runs", []):
                    fid = lrun.get("gdrive_folder_id")
                    if fid:
                        folder_meta_map[fid] = {
                            "type":        lrun.get("type", ""),
                            "app_id":      full["id"],
                            "app_company": full.get("company", ""),
                            "app_role":    full.get("role_title", ""),
                        }
    except Exception:
        pass

    PREP_KW = {"phonescreen", "hiringmanager", "technical", "executive", "panel", "peer"}

    def _infer_type(name: str) -> str:
        norm = _norm(name)
        return "interview_prep" if any(kw in norm for kw in PREP_KW) else "resume"

    try:
        config  = WorkflowConfig(progress=lambda _: None, user_label="admin")
        service = _gdrive_service(config)
        if service and GDRIVE_PARENT_FOLDER_ID:
            top_items: list[dict] = []
            page_token = None
            while True:
                kwargs: dict = dict(
                    q=f"'{GDRIVE_PARENT_FOLDER_ID}' in parents and trashed=false",
                    fields="nextPageToken, files(id, name, mimeType, webViewLink, createdTime)",
                    orderBy="createdTime desc",
                    pageSize=1000,
                )
                if page_token:
                    kwargs["pageToken"] = page_token
                resp = service.files().list(**kwargs).execute()
                top_items.extend(resp.get("files", []))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

            for item in top_items:
                if item.get("mimeType") != _FOLDER_MIME:
                    continue
                name = item["name"]
                if "@" not in name:
                    # Legacy flat-root folder — user email unknown
                    meta    = folder_meta_map.get(item["id"], {})
                    fn_norm = _norm(name)
                    if not any(fn == fn_norm for (_, fn) in covered):
                        runs_by_key[f"drive_{item['id']}"] = {
                            "id":           item["id"],
                            "type":         meta.get("type") or _infer_type(name),
                            "company":      meta.get("app_company", ""),
                            "role":         meta.get("app_role", ""),
                            "user_id":      "",
                            "user_email":   "",
                            "created_at":   item.get("createdTime", ""),
                            "status":       "complete",
                            "web_view_link": item.get("webViewLink", ""),
                            "app_id":       meta.get("app_id", ""),
                            "app_company":  meta.get("app_company", ""),
                            "app_role":     meta.get("app_role", ""),
                        }
                    continue

                user_email = name
                ue_norm    = _norm(user_email)
                children: list[dict] = []
                child_token = None
                while True:
                    ckwargs: dict = dict(
                        q=f"'{item['id']}' in parents and mimeType='{_FOLDER_MIME}' and trashed=false",
                        fields="nextPageToken, files(id, name, webViewLink, createdTime)",
                        orderBy="createdTime desc",
                        pageSize=1000,
                    )
                    if child_token:
                        ckwargs["pageToken"] = child_token
                    cresp = service.files().list(**ckwargs).execute()
                    children.extend(cresp.get("files", []))
                    child_token = cresp.get("nextPageToken")
                    if not child_token:
                        break

                for child in children:
                    fn_norm = _norm(child["name"])
                    if (ue_norm, fn_norm) in covered:
                        continue  # already represented by one or more audit events
                    meta = folder_meta_map.get(child["id"], {})
                    runs_by_key[f"drive_{child['id']}"] = {
                        "id":           child["id"],
                        "type":         meta.get("type") or _infer_type(child["name"]),
                        "company":      meta.get("app_company", ""),
                        "role":         meta.get("app_role", ""),
                        "user_id":      "",
                        "user_email":   user_email,
                        "created_at":   child.get("createdTime", ""),
                        "status":       "complete",
                        "web_view_link": child.get("webViewLink", ""),
                        "app_id":       meta.get("app_id", ""),
                        "app_company":  meta.get("app_company", ""),
                        "app_role":     meta.get("app_role", ""),
                    }
    except Exception as exc:
        logger.warning("Admin runs Drive fallback failed: %s", exc)

    all_runs = list(runs_by_key.values())
    all_runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return all_runs

# ---------------------------------------------------------------------------
# Unified audit log
# ---------------------------------------------------------------------------

# All known action types across both audit streams
AUDIT_ACTION_TYPES = sorted([
    # Auth / account
    "user_registered", "user_registered_google", "google_account_linked",
    "login_success", "login_google", "login_failed", "logout",
    "email_verified", "verification_email_resent",
    "profile_updated", "resume_uploaded", "password_changed",
    # Agent runs
    "run_started", "run_completed", "run_failed",
    "prep_started", "prep_completed", "prep_failed",
    "file_downloaded",
    # Admin exports
    "admin_csv_export",
    # Webhooks
    "webhook_created", "webhook_updated", "webhook_deleted", "webhook_tested",
    # Admin user management
    "role_changed", "admin_user_updated", "admin_verification_resent",
    # Applications
    "created", "updated", "deleted",
    "comment_added", "comment_edited", "comment_deleted",
    "run_linked", "run_unlinked",
    "match_scored",
    "jd_extracted", "setup_folder_started",
    # Calendar
    "calendar_event_created", "calendar_event_updated", "calendar_event_deleted",
    # Admin application management
    "admin_updated", "admin_deleted",
    "admin_comment_added", "admin_comment_edited", "admin_comment_deleted",
    # Import
    "imported",
])


@router.get("/audit/action-types")
async def get_action_types(request: Request):
    """Return the list of known audit action types."""
    _admin(request)
    return AUDIT_ACTION_TYPES


@router.get("/audit")
async def get_unified_audit_log(
    request: Request,
    page:       int          = Query(1, ge=1),
    per_page:   int          = Query(50, ge=1, le=500),
    action:     str | None   = None,
    actor:      str | None   = None,
    user_id:    str | None   = None,
    source:     str | None   = None,
    event_id:   str | None   = None,
    from_ts:    str | None   = None,
    to_ts:      str | None   = None,
    sort_order: str          = Query("desc", pattern="^(asc|desc)$"),
):
    """Unified audit log across all users and all application records."""
    _admin(request)

    users       = storage.list_all_users()
    user_map    = {u["user_id"]: u.get("email", "") for u in users}
    all_events: list[dict[str, Any]] = []

    # ── 1. User-level audit events ──────────────────────────────────
    if source in (None, "user"):
        for u in users:
            uid   = u["user_id"]
            email = u.get("email", "")
            try:
                events = user_audit.get_events(uid)   # newest-first list
                for e in events:
                    all_events.append({
                        **e,
                        "source":     "user",
                        "user_id":    uid,
                        "user_email": email,
                        "entity_id":  None,
                        "entity_label": None,
                    })
            except Exception:
                pass

    # ── 2. Application-level audit events ───────────────────────────
    if source in (None, "application"):
        for u in users:
            uid = u["user_id"]
            try:
                result = app_store.list_applications(uid)
                items  = result.get("items", result) if isinstance(result, dict) else result
                for item in items:
                    app_id = item["id"]
                    try:
                        full = app_store.get_application(uid, app_id)
                        if not full:
                            continue
                        label = f"{full.get('company','')} · {full.get('role_title','')}"
                        for e in full.get("audit_log", []):
                            all_events.append({
                                **e,
                                "source":       "application",
                                "user_id":      uid,
                                "user_email":   user_map.get(uid, ""),
                                "entity_id":    app_id,
                                "entity_label": label,
                                # Normalise field names to match user events
                                "actor":        e.get("actor", ""),
                                "ip":           e.get("ip"),
                                "details":      e.get("changes") or e.get("details") or {},
                            })
                    except Exception:
                        pass
            except Exception:
                pass

    # ── 3. Filter ───────────────────────────────────────────────────
    if action:
        all_events = [e for e in all_events if e.get("action") == action]
    if actor:
        lc = actor.lower()
        all_events = [e for e in all_events if lc in (e.get("actor") or "").lower()]
    if user_id:
        all_events = [e for e in all_events if e.get("user_id") == user_id]
    if event_id:
        lc = event_id.lower()
        all_events = [e for e in all_events if lc in (e.get("id") or "").lower()]
    if from_ts:
        all_events = [e for e in all_events if (e.get("timestamp") or "") >= from_ts]
    if to_ts:
        all_events = [e for e in all_events if (e.get("timestamp") or "") <= to_ts]

    # ── 4. Sort ─────────────────────────────────────────────────────
    all_events.sort(
        key=lambda e: e.get("timestamp") or "",
        reverse=(sort_order == "desc"),
    )

    # ── 5. Paginate ──────────────────────────────────────────────────
    total  = len(all_events)
    pages  = max(1, (total + per_page - 1) // per_page)
    page   = max(1, min(page, pages))
    start  = (page - 1) * per_page
    items  = all_events[start : start + per_page]

    return {
        "items":    items,
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    pages,
    }


# ---------------------------------------------------------------------------
# Audit log export (no pagination — returns full filtered result)
# ---------------------------------------------------------------------------

@router.get("/audit/export")
async def export_audit_log(
    request: Request,
    action:     str | None = None,
    actor:      str | None = None,
    user_id:    str | None = None,
    source:     str | None = None,
    event_id:   str | None = None,
    from_ts:    str | None = None,
    to_ts:      str | None = None,
    sort_order: str        = Query("desc", pattern="^(asc|desc)$"),
):
    """Return all matching audit events without pagination (for CSV export)."""
    admin = _admin(request)

    users    = storage.list_all_users()
    user_map = {u["user_id"]: u.get("email", "") for u in users}
    all_events: list[dict[str, Any]] = []

    for u in users:
        uid   = u["user_id"]
        email = u.get("email", "")
        try:
            for e in user_audit.get_events(uid):
                all_events.append({**e, "source": "user", "user_id": uid,
                                   "user_email": email, "entity_id": None, "entity_label": None})
        except Exception:
            pass

    for u in users:
        uid = u["user_id"]
        try:
            result = app_store.list_applications(uid)
            items  = result.get("items", result) if isinstance(result, dict) else result
            for item in items:
                try:
                    full = app_store.get_application(uid, item["id"])
                    if not full:
                        continue
                    label = f"{full.get('company','')} · {full.get('role_title','')}"
                    for e in full.get("audit_log", []):
                        all_events.append({**e, "source": "application",
                                           "user_id": uid, "user_email": user_map.get(uid, ""),
                                           "entity_id": item["id"], "entity_label": label,
                                           "actor": e.get("actor", ""), "ip": e.get("ip"),
                                           "details": e.get("changes") or e.get("details") or {}})
                except Exception:
                    pass
        except Exception:
            pass

    if action:    all_events = [e for e in all_events if e.get("action") == action]
    if actor:     all_events = [e for e in all_events if actor.lower() in (e.get("actor") or "").lower()]
    if user_id:   all_events = [e for e in all_events if e.get("user_id") == user_id]
    if source:    all_events = [e for e in all_events if e.get("source") == source]
    if event_id:  all_events = [e for e in all_events if event_id.lower() in (e.get("id") or "").lower()]
    if from_ts:   all_events = [e for e in all_events if (e.get("timestamp") or "") >= from_ts]
    if to_ts:     all_events = [e for e in all_events if (e.get("timestamp") or "") <= to_ts]

    all_events.sort(key=lambda e: e.get("timestamp") or "", reverse=(sort_order == "desc"))

    user_audit.log(admin["user_id"], "admin_csv_export", admin["email"],
                   screen="audit_log", row_count=len(all_events),
                   filters={"action": action, "actor": actor, "source": source,
                            "from_ts": from_ts, "to_ts": to_ts})

    return all_events


# ---------------------------------------------------------------------------
# Generic activity log (for client-side export events)
# ---------------------------------------------------------------------------

class ActivityEntry(BaseModel):
    screen:    str
    row_count: int
    filters:   dict[str, Any] = {}


@router.post("/log-activity")
async def log_activity(body: ActivityEntry, request: Request):
    """Log an admin action (e.g. CSV export) originating from the browser."""
    admin = _admin(request)
    user_audit.log(admin["user_id"], "admin_csv_export", admin["email"],
                   _client_ip(request),
                   screen=body.screen, row_count=body.row_count, filters=body.filters)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

from scripts import webhooks as wh_store  # noqa: E402


VALID_PAYLOAD_FORMATS = {"generic", "slack", "ms_teams", "grafana_loki"}


VALID_FILTER_CATEGORIES = {"auth", "profile", "applications", "runs", "admin"}


class WebhookCreate(BaseModel):
    name:               str
    url:                str
    events:             list[str] = ["*"]
    headers:            dict[str, str] = {}
    query_params:       dict[str, str] = {}
    secret:             str = ""
    active:             bool = True
    payload_format:     str = "generic"
    filter_actors:      str = ""            # comma-separated emails/user_ids
    filter_source:      str = ""            # "" | "user" | "application"
    filter_categories:  list[str] = []      # subset of VALID_FILTER_CATEGORIES
    filter_app_id:      str = ""


class WebhookUpdate(BaseModel):
    name:               str | None = None
    url:                str | None = None
    events:             list[str] | None = None
    headers:            dict[str, str] | None = None
    query_params:       dict[str, str] | None = None
    secret:             str | None = None
    active:             bool | None = None
    payload_format:     str | None = None
    filter_actors:      str | None = None
    filter_source:      str | None = None
    filter_categories:  list[str] | None = None
    filter_app_id:      str | None = None


@router.get("/webhooks")
async def list_webhooks(request: Request):
    _admin(request)
    return wh_store.list_webhooks()


@router.post("/webhooks", status_code=201)
async def create_webhook(body: WebhookCreate, request: Request):
    admin = _admin(request)
    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must start with http:// or https://")
    if _is_ssrf_url(body.url):
        raise HTTPException(400, "url must not point to a private or internal network address")
    if body.payload_format not in VALID_PAYLOAD_FORMATS:
        raise HTTPException(400, f"payload_format must be one of: {', '.join(sorted(VALID_PAYLOAD_FORMATS))}")
    if body.filter_categories:
        bad = [c for c in body.filter_categories if c not in VALID_FILTER_CATEGORIES]
        if bad:
            raise HTTPException(400, f"Unknown filter_categories: {bad}. Valid: {sorted(VALID_FILTER_CATEGORIES)}")
    webhook = {
        "id":               str(uuid.uuid4()),
        "name":             body.name.strip(),
        "url":              body.url.strip(),
        "events":           body.events,
        "headers":          body.headers,
        "query_params":     body.query_params,
        "secret":             body.secret,
        "active":             body.active,
        "payload_format":     body.payload_format,
        "filter_actors":      body.filter_actors,
        "filter_source":      body.filter_source,
        "filter_categories":  body.filter_categories,
        "filter_app_id":      body.filter_app_id,
        "created_at":       _now(),
        "created_by":       admin["email"],
        "last_triggered_at": None,
        "delivery_stats":   {"total": 0, "success": 0, "failure": 0},
        "recent_deliveries": [],
    }
    wh_store.save_webhook(webhook)
    user_audit.log(admin["user_id"], "webhook_created", admin["email"],
                   webhook_id=webhook["id"], name=webhook["name"])
    return _redact_webhook_secret(webhook)


def _redact_webhook_secret(w: dict) -> dict:
    """Return a copy of the webhook record with the secret redacted for API responses."""
    out = dict(w)
    raw = out.get("secret") or ""
    if len(raw) > 8:
        out["secret"] = raw[:4] + "*" * (len(raw) - 8) + raw[-4:]
    elif raw:
        out["secret"] = "****"
    return out


@router.get("/webhooks/{webhook_id}")
async def get_webhook(webhook_id: str, request: Request):
    _admin(request)
    w = wh_store.get_webhook(webhook_id)
    if not w:
        raise HTTPException(404, "Webhook not found")
    return _redact_webhook_secret(w)


@router.put("/webhooks/{webhook_id}")
async def update_webhook(webhook_id: str, body: WebhookUpdate, request: Request):
    admin = _admin(request)
    w = wh_store.get_webhook(webhook_id)
    if not w:
        raise HTTPException(404, "Webhook not found")
    if body.url is not None and not body.url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must start with http:// or https://")
    if body.url is not None and _is_ssrf_url(body.url):
        raise HTTPException(400, "url must not point to a private or internal network address")
    if body.filter_categories is not None:
        bad = [c for c in body.filter_categories if c not in VALID_FILTER_CATEGORIES]
        if bad:
            raise HTTPException(400, f"Unknown filter_categories: {bad}. Valid: {sorted(VALID_FILTER_CATEGORIES)}")
    for field, val in body.model_dump(exclude_unset=True).items():
        w[field] = val
    wh_store.save_webhook(w)
    user_audit.log(admin["user_id"], "webhook_updated", admin["email"],
                   webhook_id=webhook_id, changes=body.model_dump(exclude_unset=True))
    return _redact_webhook_secret(w)


@router.delete("/webhooks/{webhook_id}", status_code=204)
async def delete_webhook(webhook_id: str, request: Request):
    admin = _admin(request)
    if not wh_store.delete_webhook(webhook_id):
        raise HTTPException(404, "Webhook not found")
    user_audit.log(admin["user_id"], "webhook_deleted", admin["email"],
                   webhook_id=webhook_id)


@router.post("/webhooks/{webhook_id}/test")
async def test_webhook(webhook_id: str, request: Request):
    admin = _admin(request)
    w = wh_store.get_webhook(webhook_id)
    if not w:
        raise HTTPException(404, "Webhook not found")
    test_event = {
        "id":         str(uuid.uuid4()),
        "action":     "webhook_tested",
        "actor":      admin["email"],
        "timestamp":  _now(),
        "ip":         None,
        "details":    {"message": "This is a test delivery from Job Apply admin."},
        "user_id":    admin["user_id"],
        "user_email": admin["email"],
    }
    # Deliver synchronously so we can return the result
    import threading as _th  # noqa: PLC0415
    result: dict = {}
    def _run():
        from scripts.webhooks import _deliver  # noqa: PLC0415
        _deliver(w, test_event)
        fresh = wh_store.get_webhook(webhook_id)
        if fresh and fresh.get("recent_deliveries"):
            result["delivery"] = fresh["recent_deliveries"][0]
    t = _th.Thread(target=_run); t.start(); t.join(timeout=15)
    user_audit.log(admin["user_id"], "webhook_tested", admin["email"],
                   webhook_id=webhook_id, webhook_name=w.get("name"))
    return result.get("delivery", {"success": None, "error": "timeout"})


@router.get("/webhooks/{webhook_id}/deliveries")
async def get_deliveries(webhook_id: str, request: Request):
    _admin(request)
    w = wh_store.get_webhook(webhook_id)
    if not w:
        raise HTTPException(404, "Webhook not found")
    deliveries = w.get("recent_deliveries", [])
    # Sort descending by timestamp and enforce the cap server-side
    deliveries = sorted(deliveries, key=lambda d: d.get("timestamp", ""), reverse=True)
    return deliveries[:wh_store._MAX_DELIVERIES]
