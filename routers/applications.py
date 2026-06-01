"""
routers/applications.py — CRUD endpoints for job application tracking.

Endpoints:
  GET    /api/applications                         list (with optional ?status= ?priority= filters)
  POST   /api/applications                         create
  GET    /api/applications/{app_id}                get full record
  PUT    /api/applications/{app_id}                update
  DELETE /api/applications/{app_id}                delete
  POST   /api/applications/{app_id}/comments       add comment
  PUT    /api/applications/{app_id}/comments/{cid} edit comment
  DELETE /api/applications/{app_id}/comments/{cid} delete comment
"""

from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from scripts import applications as app_store

router = APIRouter(prefix="/api/applications", tags=["applications"])

VALID_STATUSES = {
    "Not Applying", "Researching", "Applied", "Phone Screen",
    "Interviewing", "On Hold", "Offer", "Rejected",
}
VALID_PRIORITIES = {"Low", "Medium", "High"}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class ApplicationCreate(BaseModel):
    company: str
    domain: str
    company_logo_url: str = ""
    role_title: str
    status: str = "Researching"
    date_applied: str | None = None
    dua: bool = False
    job_source: str = ""
    location: str = ""
    salary_range: str = ""
    priority: str = "Medium"
    recruiter_name: str = ""
    recruiter_email: str = ""
    url: str = ""


class ApplicationUpdate(BaseModel):
    company: str | None = None
    domain: str | None = None
    company_logo_url: str | None = None
    role_title: str | None = None
    status: str | None = None
    date_applied: str | None = None
    dua: bool | None = None
    job_source: str | None = None
    location: str | None = None
    salary_range: str | None = None
    priority: str | None = None
    recruiter_name: str | None = None
    recruiter_email: str | None = None
    url: str | None = None


class CommentCreate(BaseModel):
    text: str


class CommentUpdate(BaseModel):
    text: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_or_404(user_id: str, app_id: str) -> dict:
    record = app_store.get_application(user_id, app_id)
    if not record:
        raise HTTPException(404, "Application not found")
    return record


# ---------------------------------------------------------------------------
# Application CRUD
# ---------------------------------------------------------------------------

@router.get("")
async def list_applications(
    request: Request,
    status: str | None = None,
    priority: str | None = None,
):
    user_id = request.state.user["user_id"]
    return app_store.list_applications(user_id, status=status, priority=priority)


@router.post("", status_code=201)
async def create_application(body: ApplicationCreate, request: Request):
    user_id = request.state.user["user_id"]

    if body.status not in VALID_STATUSES:
        raise HTTPException(400, f"status must be one of: {', '.join(sorted(VALID_STATUSES))}")
    if body.priority not in VALID_PRIORITIES:
        raise HTTPException(400, f"priority must be one of: {', '.join(sorted(VALID_PRIORITIES))}")

    record = {
        "id":               str(uuid.uuid4()),
        "user_id":          user_id,
        "created_at":       _now(),
        "comments":         [],
        **body.model_dump(),
    }
    app_store.save_application(user_id, record)
    return record


@router.get("/{app_id}")
async def get_application(app_id: str, request: Request):
    return _get_or_404(request.state.user["user_id"], app_id)


@router.put("/{app_id}")
async def update_application(app_id: str, body: ApplicationUpdate, request: Request):
    user_id = request.state.user["user_id"]
    record = _get_or_404(user_id, app_id)

    updates = body.model_dump(exclude_unset=True)  # preserves False + explicit nulls

    if "status" in updates and updates["status"] not in VALID_STATUSES:
        raise HTTPException(400, f"status must be one of: {', '.join(sorted(VALID_STATUSES))}")
    if "priority" in updates and updates["priority"] not in VALID_PRIORITIES:
        raise HTTPException(400, f"priority must be one of: {', '.join(sorted(VALID_PRIORITIES))}")

    record.update(updates)
    app_store.save_application(user_id, record)
    return record


@router.delete("/{app_id}", status_code=204)
async def delete_application(app_id: str, request: Request):
    user_id = request.state.user["user_id"]
    _get_or_404(user_id, app_id)  # 404 if not found
    app_store.delete_application(user_id, app_id)


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

@router.post("/{app_id}/comments", status_code=201)
async def add_comment(app_id: str, body: CommentCreate, request: Request):
    user_id = request.state.user["user_id"]
    record = _get_or_404(user_id, app_id)

    if not body.text.strip():
        raise HTTPException(400, "Comment text cannot be empty")

    comment = {
        "id":         str(uuid.uuid4()),
        "text":       body.text.strip(),
        "created_at": _now(),
        "updated_at": _now(),
    }
    record.setdefault("comments", []).append(comment)
    app_store.save_application(user_id, record)
    return comment


@router.put("/{app_id}/comments/{comment_id}")
async def update_comment(
    app_id: str, comment_id: str, body: CommentUpdate, request: Request
):
    user_id = request.state.user["user_id"]
    record = _get_or_404(user_id, app_id)

    if not body.text.strip():
        raise HTTPException(400, "Comment text cannot be empty")

    for c in record.get("comments", []):
        if c["id"] == comment_id:
            c["text"] = body.text.strip()
            c["updated_at"] = _now()
            app_store.save_application(user_id, record)
            return c

    raise HTTPException(404, "Comment not found")


@router.delete("/{app_id}/comments/{comment_id}", status_code=204)
async def delete_comment(app_id: str, comment_id: str, request: Request):
    user_id = request.state.user["user_id"]
    record = _get_or_404(user_id, app_id)

    original_len = len(record.get("comments", []))
    record["comments"] = [c for c in record.get("comments", []) if c["id"] != comment_id]

    if len(record["comments"]) == original_len:
        raise HTTPException(404, "Comment not found")

    app_store.save_application(user_id, record)
