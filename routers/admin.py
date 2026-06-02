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
    """Return all applications across all users with a 'user_email' field added."""
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
# Active in-memory run listing
# ---------------------------------------------------------------------------

@router.get("/runs")
async def list_active_runs(request: Request):
    """Return all in-memory runs and preps across all users (admin only)."""
    _admin(request)
    from api import _runs, _preps  # noqa: PLC0415

    def _run_summary(run_id: str, r: dict, kind: str) -> dict:
        user = storage.get_user_by_id(r.get("user_id", "")) or {}
        return {
            "id":         run_id,
            "type":       kind,
            "status":     r.get("status", "?"),
            "user_id":    r.get("user_id", ""),
            "user_email": user.get("email", ""),
            "finished_at": r.get("_finished_at"),
        }

    runs = [_run_summary(k, v, "resume") for k, v in _runs.items()]
    runs += [_run_summary(k, v, "interview_prep") for k, v in _preps.items()]
    runs.sort(key=lambda r: r.get("finished_at") or 0, reverse=True)
    return runs
