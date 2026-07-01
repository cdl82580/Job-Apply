"""HTTP client for the Job Apply FastAPI backend — mirrors slack_bot.py helpers."""

from __future__ import annotations

import time
from typing import Any

import requests

from config import Config


def _api(method: str, path: str, **kwargs) -> requests.Response:
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {Config.BOT_API_KEY}"
    return getattr(requests, method)(
        f"{Config.API_BASE}{path}", headers=headers, timeout=30, **kwargs,
    )


# ── Agent runs ───────────────────────────────────────────────────────────

def post_run(job_posting: str, company: str, role: str, contact: str = "") -> dict:
    r = _api("post", "/api/run", json={
        "job_posting": job_posting,
        "company": company,
        "role": role,
        "contact": contact or None,
    })
    r.raise_for_status()
    return r.json()


def poll_run(run_id: str, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api("get", f"/api/run/{run_id}/status")
        r.raise_for_status()
        data = r.json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(5)
    return {"status": "timeout", "error": "Timed out waiting for run to complete"}


def post_prep(job_posting: str, company: str, role: str,
              round_type: str, focus: str = "", interviewer: str = "") -> dict:
    r = _api("post", "/api/prep", json={
        "job_posting": job_posting,
        "company": company,
        "role": role,
        "round_type": round_type,
        "focus": focus or None,
        "interviewer": interviewer or None,
    })
    r.raise_for_status()
    return r.json()


def poll_prep(prep_id: str, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api("get", f"/api/prep/{prep_id}/status")
        r.raise_for_status()
        data = r.json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(5)
    return {"status": "timeout", "error": "Timed out waiting for prep to complete"}


def post_aq(question: str, job_posting: str, company: str, role: str,
            tone: str = "professional", char_limit: int | None = None) -> dict:
    payload: dict[str, Any] = {
        "question": question,
        "job_posting": job_posting,
        "company": company,
        "role": role,
        "tone": tone,
    }
    if char_limit:
        payload["char_limit"] = char_limit
    r = _api("post", "/api/aq", json=payload)
    r.raise_for_status()
    return r.json()


def poll_aq(aq_id: str, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api("get", f"/api/aq/{aq_id}/status")
        r.raise_for_status()
        data = r.json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(5)
    return {"status": "timeout", "error": "Timed out waiting for answer to complete"}


# ── Tracker ──────────────────────────────────────────────────────────────

def get_applications(status: str | None = None) -> list[dict]:
    params = {}
    if status:
        params["status"] = status
    r = _api("get", "/api/applications", params=params)
    r.raise_for_status()
    return r.json().get("items", [])


def get_application(app_id: str) -> dict:
    r = _api("get", f"/api/applications/{app_id}")
    r.raise_for_status()
    return r.json()


def create_application(data: dict) -> dict:
    r = _api("post", "/api/applications", json=data)
    r.raise_for_status()
    return r.json()


# ── Profile ──────────────────────────────────────────────────────────────

def get_profile() -> dict:
    r = _api("get", "/api/profile")
    r.raise_for_status()
    return r.json()


# ── Optimize ─────────────────────────────────────────────────────────────

def post_optimize(app_id: str, folder_id: str, instruction: str,
                  company: str, role: str,
                  optimize_resume: bool = True,
                  optimize_cover_letter: bool = True) -> dict:
    r = _api("post", "/api/optimize", json={
        "app_id": app_id,
        "folder_id": folder_id,
        "instruction": instruction,
        "company": company,
        "role": role,
        "optimize_resume": optimize_resume,
        "optimize_cover_letter": optimize_cover_letter,
    })
    r.raise_for_status()
    return r.json()


def poll_optimize(optimize_id: str, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api("get", f"/api/optimize/{optimize_id}/status")
        r.raise_for_status()
        data = r.json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(5)
    return {"status": "timeout", "error": "Timed out waiting for optimize to complete"}


# ── Agent runs ───────────────────────────────────────────────────────────

def get_agent_runs() -> list[dict]:
    r = _api("get", "/api/agent-runs")
    r.raise_for_status()
    return r.json().get("runs", [])


# ── Runs (legacy) ────────────────────────────────────────────────────────

def get_drive_runs() -> list[dict]:
    r = _api("get", "/api/gdrive/runs")
    r.raise_for_status()
    return r.json()
