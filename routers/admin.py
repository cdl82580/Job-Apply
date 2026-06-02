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

import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from scripts import storage
from scripts import applications as app_store
from scripts import user_audit

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
        # Get app count from index
        try:
            apps = app_store.list_applications(uid)
            app_count = apps["total"]
        except Exception:
            app_count = 0

        result.append({
            "user_id":      uid,
            "email":        u.get("email", ""),
            "display_name": u.get("display_name", ""),
            "role":         u.get("role", "user"),
            "created_at":   u.get("created_at", ""),
            "app_count":    app_count,
            "has_resume":   storage.has_resume(uid),
        })

    result.sort(key=lambda u: u.get("created_at", ""))
    return result


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

    user_audit.log(
        user_id, "role_changed", admin["email"],
        old_role=old_role, new_role=body.role,
        changed_by=admin["email"],
    )

    return {"ok": True, "user_id": user_id, "role": body.role}


# ---------------------------------------------------------------------------
# Cross-user application access
# ---------------------------------------------------------------------------

@router.get("/applications")
async def list_all_applications(request: Request):
    """Return all applications across all users.
    Fetches full records to include _gdrive_url (first linked run's folder URL)."""
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
                # Fetch first linked run's Drive URL from the full record
                try:
                    full = app_store.get_application(uid, item["id"])
                    runs = (full or {}).get("linked_runs", [])
                    item["_gdrive_url"] = runs[0]["folder_url"] if runs else ""
                except Exception:
                    item["_gdrive_url"] = ""
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


# ---------------------------------------------------------------------------
# Active in-memory run listing
# ---------------------------------------------------------------------------

@router.get("/runs")
async def list_all_runs(request: Request):
    """Return all agent runs across all users from Google Drive (persistent history)
    plus any still-active in-memory runs not yet in Drive."""
    _admin(request)
    import re as _re  # noqa: PLC0415
    from apply import list_gdrive_run_folders, WorkflowConfig  # noqa: PLC0415

    PREP_KEYWORDS = {"phonescreen", "hiringmanager", "technical", "executive", "panel", "peer"}

    def _infer_type(folder_name: str) -> str:
        norm = _re.sub(r"[^a-z0-9]", "", folder_name.lower())
        return "interview_prep" if any(kw in norm for kw in PREP_KEYWORDS) else "resume"

    users    = storage.list_all_users()
    seen_ids: set[str] = set()
    all_runs: list[dict] = []

    config = WorkflowConfig(progress=lambda _: None, user_label="admin")
    for u in users:
        email = u.get("email", "")
        if not email:
            continue
        try:
            folders = list_gdrive_run_folders(email, config)
            for f in folders:
                if f["id"] in seen_ids:
                    continue
                seen_ids.add(f["id"])
                all_runs.append({
                    "id":            f["id"],
                    "name":          f["name"],
                    "type":          _infer_type(f["name"]),
                    "web_view_link": f.get("web_view_link", ""),
                    "source":        f.get("source", ""),
                    "user_email":    email,
                    "status":        "complete",   # Drive entry = completed run
                })
        except Exception:
            pass

    # Also surface any still-active in-memory runs not yet uploaded to Drive
    from api import _runs, _preps  # noqa: PLC0415
    for run_id, r in list(_runs.items()):
        if r.get("status") in ("queued", "running"):
            user = storage.get_user_by_id(r.get("user_id", "")) or {}
            all_runs.append({
                "id":            run_id,
                "name":          "",
                "type":          "resume",
                "web_view_link": "",
                "source":        "in_progress",
                "user_email":    user.get("email", ""),
                "status":        r.get("status", "running"),
            })
    for prep_id, p in list(_preps.items()):
        if p.get("status") in ("queued", "running"):
            user = storage.get_user_by_id(p.get("user_id", "")) or {}
            all_runs.append({
                "id":            prep_id,
                "name":          "",
                "type":          "interview_prep",
                "web_view_link": "",
                "source":        "in_progress",
                "user_email":    user.get("email", ""),
                "status":        p.get("status", "running"),
            })

    return all_runs
