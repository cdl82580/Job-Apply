"""
routers/applications.py — CRUD endpoints for job application tracking.

Endpoints:
  GET    /api/applications                         list (with optional ?status= ?priority= filters)
  POST   /api/applications                         create
  GET    /api/applications/{app_id}                get full record
  PUT    /api/applications/{app_id}                update
  DELETE /api/applications/{app_id}                delete
  GET    /api/applications/{app_id}/audit          audit log for one application
  POST   /api/applications/{app_id}/comments       add comment
  PUT    /api/applications/{app_id}/comments/{cid} edit comment
  DELETE /api/applications/{app_id}/comments/{cid} delete comment
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from scripts import applications as app_store

router = APIRouter(prefix="/api/applications", tags=["applications"])

VALID_STATUSES = {
    "Not Applying", "Researching", "Applied", "Phone Screen",
    "Interviewing", "On Hold", "Offer", "Rejected",
}
VALID_PRIORITIES = {"Low", "Medium", "High"}

# Fields excluded from change-diff (internal / not user-editable)
_AUDIT_SKIP = {"last_updated", "updated_by", "audit_log", "comments"}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class ApplicationCreate(BaseModel):
    company: str
    domain: str = ""
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


class RunLinkCreate(BaseModel):
    type: str             # "resume" | "interview_prep"
    folder_name: str
    folder_url: str = ""
    gdrive_folder_id: str = ""


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------

def _audit_entry(
    action: str,
    actor: str,
    changes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id":        str(uuid.uuid4()),
        "action":    action,
        "actor":     actor,
        "timestamp": _now(),
        "changes":   changes,
    }


def _diff(old: dict, new: dict) -> dict[str, Any]:
    """Return {field: {from: old_val, to: new_val}} for fields that changed."""
    changes = {}
    all_keys = set(old) | set(new)
    for k in all_keys:
        if k in _AUDIT_SKIP:
            continue
        ov, nv = old.get(k), new.get(k)
        if ov != nv:
            changes[k] = {"from": ov, "to": nv}
    return changes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_or_404(user_id: str, app_id: str) -> dict:
    record = app_store.get_application(user_id, app_id)
    if not record:
        raise HTTPException(404, "Application not found")
    return record


def _actor(request: Request) -> str:
    return request.state.user.get("email", request.state.user.get("user_id", "unknown"))


def _trigger_job_description_capture(
    user_id: str, app_id: str, company: str, role_title: str, url: str, actor: str,
) -> None:
    """Best-effort, async: ensure the application's Drive folder exists, link it
    to the application record (so the UI can find the JD before any resume run
    has happened), and auto-capture its job description from `url` via Claude.
    Never raises — failures here must never affect the application-create response."""
    try:
        from apply import auto_capture_job_description, safe_filename, WorkflowConfig

        def _run():
            try:
                config = WorkflowConfig(progress=lambda _: None, user_label=actor)
                folder = auto_capture_job_description(company, role_title, url, config)
                if folder:
                    folder_id, folder_url = folder
                    folder_name = f"{safe_filename(company)}_{safe_filename(role_title)}"
                    record = app_store.link_run(user_id, app_id, {
                        "id":               str(uuid.uuid4()),
                        "type":             "job_description",
                        "folder_name":      folder_name,
                        "folder_url":       folder_url,
                        "gdrive_folder_id": folder_id,
                        "linked_at":        _now(),
                        "linked_by":        "system",
                    })
                    if record:
                        record.setdefault("audit_log", []).append(
                            _audit_entry("run_linked", "system", {
                                "type": "job_description", "folder_name": folder_name,
                            })
                        )
                        app_store.save_application(user_id, record)
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Application CRUD
# ---------------------------------------------------------------------------

@router.get("")
async def list_applications(
    request: Request,
    status:   str | None = None,
    priority: str | None = None,
    page:     int = Query(1, ge=1),
    per_page: int = Query(0, ge=0),
):
    user_id = request.state.user["user_id"]
    return app_store.list_applications(
        user_id, status=status, priority=priority, page=page, per_page=per_page
    )


@router.post("", status_code=201)
async def create_application(body: ApplicationCreate, request: Request):
    user_id = request.state.user["user_id"]
    actor   = _actor(request)

    if body.status not in VALID_STATUSES:
        raise HTTPException(400, f"status must be one of: {', '.join(sorted(VALID_STATUSES))}")
    if body.priority not in VALID_PRIORITIES:
        raise HTTPException(400, f"priority must be one of: {', '.join(sorted(VALID_PRIORITIES))}")
    if body.url and not body.url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must start with http:// or https://")
    if body.company_logo_url and not body.company_logo_url.startswith("https://"):
        raise HTTPException(400, "company_logo_url must start with https://")

    now = _now()
    record = {
        "id":          str(uuid.uuid4()),
        "user_id":     user_id,
        "created_at":  now,
        "created_by":  actor,
        "updated_at":  now,
        "updated_by":  actor,
        "comments":    [],
        "audit_log":   [],
        **body.model_dump(),
    }
    record["audit_log"].append(_audit_entry("created", actor))
    record = app_store.save_application(user_id, record)

    if record.get("url"):
        _trigger_job_description_capture(
            user_id, record["id"], record["company"], record["role_title"], record["url"], actor,
        )

    return record


@router.get("/{app_id}")
async def get_application(app_id: str, request: Request):
    return _get_or_404(request.state.user["user_id"], app_id)


@router.put("/{app_id}")
async def update_application(app_id: str, body: ApplicationUpdate, request: Request):
    user_id = request.state.user["user_id"]
    actor   = _actor(request)
    record  = _get_or_404(user_id, app_id)

    updates = body.model_dump(exclude_unset=True)  # preserves False + explicit nulls

    if "status" in updates and updates["status"] not in VALID_STATUSES:
        raise HTTPException(400, f"status must be one of: {', '.join(sorted(VALID_STATUSES))}")
    if "priority" in updates and updates["priority"] not in VALID_PRIORITIES:
        raise HTTPException(400, f"priority must be one of: {', '.join(sorted(VALID_PRIORITIES))}")
    if updates.get("url") and not updates["url"].startswith(("http://", "https://")):
        raise HTTPException(400, "url must start with http:// or https://")
    if updates.get("company_logo_url") and not updates["company_logo_url"].startswith("https://"):
        raise HTTPException(400, "company_logo_url must start with https://")

    # Compute diff before applying
    changes = _diff(record, {**record, **updates})

    record.update(updates)
    record["updated_at"] = _now()
    record["updated_by"] = actor

    if changes:
        record.setdefault("audit_log", []).append(
            _audit_entry("updated", actor, changes)
        )

    record = app_store.save_application(user_id, record)
    return record


@router.delete("/{app_id}", status_code=204)
async def delete_application(app_id: str, request: Request):
    user_id = request.state.user["user_id"]
    actor   = _actor(request)
    record  = _get_or_404(user_id, app_id)

    # Write one final audit entry into the record before deleting
    record.setdefault("audit_log", []).append(_audit_entry("deleted", actor))
    # Save a tombstone in a separate key so deletion is auditable
    app_store.save_deleted_tombstone(user_id, record)

    app_store.delete_application(user_id, app_id)


@router.get("/{app_id}/audit")
async def get_audit_log(app_id: str, request: Request):
    record = _get_or_404(request.state.user["user_id"], app_id)
    return record.get("audit_log", [])


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

@router.post("/{app_id}/comments", status_code=201)
async def add_comment(app_id: str, body: CommentCreate, request: Request):
    user_id = request.state.user["user_id"]
    actor   = _actor(request)
    record  = _get_or_404(user_id, app_id)

    if not body.text.strip():
        raise HTTPException(400, "Comment text cannot be empty")

    now = _now()
    comment = {
        "id":         str(uuid.uuid4()),
        "text":       body.text.strip(),
        "created_at": now,
        "updated_at": now,
        "author":     actor,
    }
    record.setdefault("comments", []).append(comment)
    record.setdefault("audit_log", []).append(
        _audit_entry("comment_added", actor, {"comment_id": comment["id"], "preview": comment["text"][:60]})
    )
    record["updated_at"] = now
    record["updated_by"] = actor
    app_store.save_application(user_id, record)
    return comment


@router.put("/{app_id}/comments/{comment_id}")
async def update_comment(
    app_id: str, comment_id: str, body: CommentUpdate, request: Request
):
    user_id = request.state.user["user_id"]
    actor   = _actor(request)
    record  = _get_or_404(user_id, app_id)

    if not body.text.strip():
        raise HTTPException(400, "Comment text cannot be empty")

    for c in record.get("comments", []):
        if c["id"] == comment_id:
            old_text = c["text"]
            c["text"]       = body.text.strip()
            c["updated_at"] = _now()
            record.setdefault("audit_log", []).append(
                _audit_entry("comment_edited", actor, {
                    "comment_id": comment_id,
                    "from": old_text[:60],
                    "to":   c["text"][:60],
                })
            )
            record["updated_at"] = _now()
            record["updated_by"] = actor
            app_store.save_application(user_id, record)
            return c  # comment object, not record — no need to reassign

    raise HTTPException(404, "Comment not found")


# ---------------------------------------------------------------------------
# Run links
# ---------------------------------------------------------------------------

@router.get("/{app_id}/runs")
async def get_linked_runs(app_id: str, request: Request):
    record = _get_or_404(request.state.user["user_id"], app_id)
    return record.get("linked_runs", [])


@router.post("/{app_id}/runs", status_code=201)
async def link_run(app_id: str, body: RunLinkCreate, request: Request):
    user_id = request.state.user["user_id"]
    actor   = _actor(request)
    _get_or_404(user_id, app_id)  # 404 guard

    run_info = {
        "id":               str(uuid.uuid4()),
        "type":             body.type,
        "folder_name":      body.folder_name,
        "folder_url":       body.folder_url,
        "gdrive_folder_id": body.gdrive_folder_id,
        "linked_at":        _now(),
        "linked_by":        actor,
    }
    record = app_store.link_run(user_id, app_id, run_info)
    if not record:
        raise HTTPException(404, "Application not found")

    record.setdefault("audit_log", []).append(
        _audit_entry("run_linked", actor, {
            "run_id":      run_info["id"],
            "type":        body.type,
            "folder_name": body.folder_name,
        })
    )
    app_store.save_application(user_id, record)
    return run_info


@router.delete("/{app_id}/runs/{link_id}", status_code=204)
async def unlink_run(app_id: str, link_id: str, request: Request):
    user_id = request.state.user["user_id"]
    actor   = _actor(request)
    record  = _get_or_404(user_id, app_id)

    removed = app_store.unlink_run(user_id, app_id, link_id)
    if not removed:
        raise HTTPException(404, "Run link not found")

    record = app_store.get_application(user_id, app_id)
    if record:
        record.setdefault("audit_log", []).append(
            _audit_entry("run_unlinked", actor, {"link_id": link_id})
        )
        app_store.save_application(user_id, record)


def _resolve_jd_text(record: dict, config) -> str | None:
    """Find job description text for an application: prefer a Drive folder linked
    via any run (job_description capture happens into the same per-company/role
    folder as resume/interview-prep runs), falling back to extracting fresh from
    the application's posting URL."""
    from apply import get_gdrive_job_posting, extract_job_description_from_url

    seen_folders: set[str] = set()
    linked = record.get("linked_runs", [])
    # Prefer an explicit job_description link, then any other linked folder.
    ordered = sorted(linked, key=lambda r: 0 if r.get("type") == "job_description" else 1)
    for run in ordered:
        folder_id = run.get("gdrive_folder_id")
        if not folder_id or folder_id in seen_folders:
            continue
        seen_folders.add(folder_id)
        text = get_gdrive_job_posting(folder_id, config)
        if text:
            return text

    url = record.get("url")
    if url:
        return extract_job_description_from_url(url, config)
    return None


@router.post("/{app_id}/score")
async def score_application(app_id: str, request: Request):
    """(Re)score how well the user's resume/profile matches this application's
    job posting. Synchronous — same pattern as /api/jd/format."""
    user_id = request.state.user["user_id"]
    actor   = _actor(request)
    record  = _get_or_404(user_id, app_id)

    from apply import score_application_match, extract_resume_text, WorkflowConfig
    from scripts import storage
    import tempfile
    from pathlib import Path

    config = WorkflowConfig(progress=lambda _: None, user_label=actor)

    jd_text = _resolve_jd_text(record, config)
    if not jd_text:
        raise HTTPException(
            422,
            "No job description available to score against — add a posting URL "
            "or link a job description to this application first.",
        )

    resume_bytes = storage.get_resume(user_id)
    if not resume_bytes:
        raise HTTPException(400, "No master resume uploaded. Add one in your profile.")
    profile_text = storage.get_profile(user_id)
    if not profile_text:
        raise HTTPException(400, "No profile guide saved. Add one in your profile.")

    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False, dir="/tmp")
    try:
        tmp.write(resume_bytes)
        tmp.close()
        resume_config = WorkflowConfig(progress=lambda _: None, user_label=actor,
                                       master_resume=Path(tmp.name))
        resume_text = extract_resume_text(resume_config)
    finally:
        try:
            Path(tmp.name).unlink()
        except OSError:
            pass

    try:
        match_score = score_application_match(jd_text, resume_text, profile_text, config)
    except Exception as e:
        raise HTTPException(500, f"Scoring failed: {e}")

    match_score["scored_at"] = _now()
    match_score["scored_by"] = actor

    record = app_store.save_match_score(user_id, app_id, match_score)
    if not record:
        raise HTTPException(404, "Application not found")

    record.setdefault("audit_log", []).append(
        _audit_entry("match_scored", actor, {
            "score": match_score["score"], "category": match_score["category"],
        })
    )
    app_store.save_application(user_id, record)

    return match_score


@router.delete("/{app_id}/comments/{comment_id}", status_code=204)
async def delete_comment(app_id: str, comment_id: str, request: Request):
    user_id = request.state.user["user_id"]
    actor   = _actor(request)
    record  = _get_or_404(user_id, app_id)

    original_len = len(record.get("comments", []))
    deleted = [c for c in record.get("comments", []) if c["id"] == comment_id]
    record["comments"] = [c for c in record.get("comments", []) if c["id"] != comment_id]

    if len(record["comments"]) == original_len:
        raise HTTPException(404, "Comment not found")

    if deleted:
        record.setdefault("audit_log", []).append(
            _audit_entry("comment_deleted", actor, {"comment_id": comment_id})
        )
    record["updated_at"] = _now()
    record["updated_by"] = actor
    app_store.save_application(user_id, record)
