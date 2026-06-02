"""
slack_bot.py — Slack bot for the Job Application Agent.

Environment variables required:
  SLACK_BOT_TOKEN       xoxb-... token from the Slack app
  SLACK_SIGNING_SECRET  signing secret from the Slack app Basic Information page
  BOT_API_KEY           must match the BOT_API_KEY set on the Fly.io app
  JOB_APPLY_API_URL     base URL of the deployed app (default: https://job-apply-corey.fly.dev)

Run locally:
  python slack_bot.py

The bot listens on port 3000 (configurable via PORT env var).
In production, run behind a reverse proxy or expose directly via Fly.io.

Slash commands handled:
  /apply         — generate resume + cover letter for a job
  /prep          — generate interview prep doc
  /jobstatus     — check API health

  /tracker       — pipeline summary (counts by status)
  /track-list    — list active applications
  /track-add     — add a new application record (modal)
  /track-update  — update an application's status (modal)
  /track-note    — add a comment to an application (modal)
  /track-delete  — delete an application (modal + confirm)
"""

from __future__ import annotations

import json
import os
import threading
import time

import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SLACK_BOT_TOKEN      = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
SLACK_APP_TOKEN      = os.environ.get("SLACK_APP_TOKEN", "")  # xapp-... for Socket Mode
BOT_API_KEY          = os.environ["BOT_API_KEY"]
API_BASE             = os.environ.get("JOB_APPLY_API_URL", "https://job-apply-corey.fly.dev").rstrip("/")
PORT                 = int(os.environ.get("PORT", "3000"))

ROUND_TYPES = ["recruiter_screen", "hiring_manager", "technical", "panel", "final", "take_home"]

VALID_STATUSES  = ["Not Applying", "Researching", "Applied", "Phone Screen",
                   "Interviewing", "On Hold", "Offer", "Rejected"]
VALID_PRIORITIES = ["High", "Medium", "Low"]

STATUS_EMOJI = {
    "Interviewing":  "🎯",
    "Phone Screen":  "📞",
    "Applied":       "✅",
    "Researching":   "🔬",
    "On Hold":       "⏸️",
    "Offer":         "🎉",
    "Rejected":      "❌",
    "Not Applying":  "🚫",
}

TRACKER_URL = f"{API_BASE}/tracking.html"

app = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)

# ---------------------------------------------------------------------------
# API helpers — agent runs
# ---------------------------------------------------------------------------

def _api(method: str, path: str, **kwargs) -> requests.Response:
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {BOT_API_KEY}"
    return getattr(requests, method)(f"{API_BASE}{path}", headers=headers, timeout=30, **kwargs)


def _post_run(job_posting: str, company: str, role: str, contact: str = "") -> dict:
    r = _api("post", "/api/run", json={
        "job_posting": job_posting,
        "company": company,
        "role": role,
        "contact": contact or None,
    })
    r.raise_for_status()
    return r.json()


def _post_prep(job_posting: str, company: str, role: str,
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


def _poll_run(run_id: str, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api("get", f"/api/run/{run_id}/status")
        r.raise_for_status()
        data = r.json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(5)
    return {"status": "timeout", "error": "Timed out waiting for run to complete"}


def _poll_prep(prep_id: str, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api("get", f"/api/prep/{prep_id}/status")
        r.raise_for_status()
        data = r.json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(5)
    return {"status": "timeout", "error": "Timed out waiting for prep to complete"}


# ---------------------------------------------------------------------------
# API helpers — application tracker
# ---------------------------------------------------------------------------

def _get_apps(status: str | None = None) -> list[dict]:
    """Fetch all application index entries, optionally filtered by status."""
    params = {}
    if status:
        params["status"] = status
    r = _api("get", "/api/applications", params=params)
    r.raise_for_status()
    data = r.json()
    return data.get("items", data) if isinstance(data, dict) else data


def _get_app(app_id: str) -> dict:
    r = _api("get", f"/api/applications/{app_id}")
    r.raise_for_status()
    return r.json()


def _create_app(payload: dict) -> dict:
    r = _api("post", "/api/applications", json=payload)
    r.raise_for_status()
    return r.json()


def _update_app(app_id: str, payload: dict) -> dict:
    r = _api("put", f"/api/applications/{app_id}", json=payload)
    r.raise_for_status()
    return r.json()


def _delete_app(app_id: str) -> None:
    r = _api("delete", f"/api/applications/{app_id}")
    r.raise_for_status()


def _add_comment(app_id: str, text: str) -> dict:
    r = _api("post", f"/api/applications/{app_id}/comments", json={"text": text})
    r.raise_for_status()
    return r.json()


# Statuses excluded from the default Slack select — typically low-value for
# update/note/delete actions and too numerous to show by default.
_INACTIVE_STATUSES = {"Not Applying", "Rejected"}
# Slack static_select hard cap
_SLACK_MAX_OPTIONS = 100


def _app_options(apps: list[dict] | None = None, active_only: bool = True) -> list[dict]:
    """Return Slack static_select options (max 100).

    active_only=True (default) excludes 'Not Applying' and 'Rejected' to keep
    the list short and relevant. Pass active_only=False for the delete command
    where you may want to clean up any record.
    """
    if apps is None:
        apps = _get_apps()

    if active_only:
        apps = [a for a in apps if a.get("status") not in _INACTIVE_STATUSES]

    order = {s: i for i, s in enumerate(VALID_STATUSES)}
    apps  = sorted(apps, key=lambda a: (order.get(a.get("status", ""), 99), a.get("company", "")))

    options = []
    for a in apps[:_SLACK_MAX_OPTIONS]:
        label = f"{a.get('company', '?')} · {a.get('role_title', '?')} ({a.get('status', '?')})"
        if len(label) > 75:
            label = label[:72] + "…"
        options.append({
            "text":  {"type": "plain_text", "text": label},
            "value": a["id"],
        })
    return options


def _fmt_date(iso: str | None) -> str:
    if not iso:
        return "—"
    parts = iso.split("T")[0].split("-")
    return f"{int(parts[1])}/{int(parts[2])}/{parts[0][2:]}" if len(parts) == 3 else iso


def _app_line(a: dict) -> str:
    emoji  = STATUS_EMOJI.get(a.get("status", ""), "•")
    name   = f"*{a.get('company', '?')}* · {a.get('role_title', '?')}"
    status = a.get("status", "?")
    date   = _fmt_date(a.get("date_applied"))
    url    = a.get("url", "")
    parts  = [f"{emoji} {name}", f"_{status}_"]
    if date != "—":
        parts.append(f"Applied {date}")
    if url:
        parts.append(f"<{url}|Job Post ↗>")
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# /tracker — pipeline summary
# ---------------------------------------------------------------------------

@app.command("/tracker")
def tracker_command(ack, respond):
    ack()
    try:
        apps = _get_apps()
    except Exception as exc:
        respond(f":x: Could not reach the tracker: {exc}")
        return

    counts: dict[str, int] = {s: 0 for s in VALID_STATUSES}
    for a in apps:
        s = a.get("status", "")
        if s in counts:
            counts[s] += 1

    lines = []
    for status in VALID_STATUSES:
        n = counts[status]
        if n:
            lines.append(f"{STATUS_EMOJI[status]} *{status}:* {n}")

    text = (
        f":bar_chart: *Application Pipeline* ({len(apps)} total)\n"
        + "\n".join(lines)
        + f"\n\n<{TRACKER_URL}|Open Tracker →>"
    )
    respond(text)


# ---------------------------------------------------------------------------
# /track-list [status] — list applications
# ---------------------------------------------------------------------------

@app.command("/track-list")
def track_list_command(ack, respond, body):
    ack()
    raw_status = body.get("text", "").strip()

    # Normalise: "interviewing" → "Interviewing", "phone screen" → "Phone Screen"
    status_filter: str | None = None
    if raw_status:
        matches = [s for s in VALID_STATUSES if s.lower() == raw_status.lower()]
        if matches:
            status_filter = matches[0]
        else:
            respond(
                f":x: Unknown status `{raw_status}`. Valid values: "
                + ", ".join(f"`{s}`" for s in VALID_STATUSES)
            )
            return

    try:
        apps = _get_apps(status=status_filter)
    except Exception as exc:
        respond(f":x: Could not reach the tracker: {exc}")
        return

    if not apps:
        label = f"*{status_filter}*" if status_filter else "active"
        respond(f"No {label} applications found.")
        return

    # Sort: most active first
    order = {s: i for i, s in enumerate(VALID_STATUSES)}
    apps = sorted(apps, key=lambda a: (order.get(a.get("status", ""), 99), a.get("company", "")))

    header = f":clipboard: *Applications{' — ' + status_filter if status_filter else ''}* ({len(apps)} total)"
    shown  = apps[:15]

    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": header}}]

    for a in shown:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": _app_line(a)},
        })

    if len(apps) > 15:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn",
                          "text": f"_…and {len(apps) - 15} more. <{TRACKER_URL}|View all in tracker →>_"}],
        })
    else:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"<{TRACKER_URL}|Open Tracker →>"}],
        })

    respond(blocks=blocks, text=header)


# ---------------------------------------------------------------------------
# /track-add — add a new application
# ---------------------------------------------------------------------------

@app.command("/track-add")
def track_add_command(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "track_add_submit",
            "title": {"type": "plain_text", "text": "Add Application"},
            "submit": {"type": "plain_text", "text": "Add"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "company",
                    "label": {"type": "plain_text", "text": "Company"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "placeholder": {"type": "plain_text", "text": "Acme Corp"}},
                },
                {
                    "type": "input",
                    "block_id": "role_title",
                    "label": {"type": "plain_text", "text": "Role Title"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "placeholder": {"type": "plain_text", "text": "Solutions Engineer"}},
                },
                {
                    "type": "input",
                    "block_id": "status",
                    "label": {"type": "plain_text", "text": "Status"},
                    "element": {
                        "type": "static_select",
                        "action_id": "value",
                        "initial_option": {"text": {"type": "plain_text", "text": "Researching"}, "value": "Researching"},
                        "options": [{"text": {"type": "plain_text", "text": s}, "value": s} for s in VALID_STATUSES],
                    },
                },
                {
                    "type": "input",
                    "block_id": "priority",
                    "label": {"type": "plain_text", "text": "Priority"},
                    "element": {
                        "type": "static_select",
                        "action_id": "value",
                        "initial_option": {"text": {"type": "plain_text", "text": "Medium"}, "value": "Medium"},
                        "options": [{"text": {"type": "plain_text", "text": p}, "value": p} for p in VALID_PRIORITIES],
                    },
                },
                {
                    "type": "input",
                    "block_id": "url",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Job Posting URL (optional)"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "placeholder": {"type": "plain_text", "text": "https://…"}},
                },
                {
                    "type": "input",
                    "block_id": "job_source",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Job Source (optional)"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "placeholder": {"type": "plain_text", "text": "LinkedIn, Indeed, Referral…"}},
                },
                {
                    "type": "input",
                    "block_id": "note",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Initial Note (optional)"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "multiline": True,
                                "placeholder": {"type": "plain_text", "text": "Any notes about this role…"}},
                },
            ],
        },
    )


@app.view("track_add_submit")
def track_add_view_submit(ack, body, client, view):
    ack()
    vals      = view["state"]["values"]
    channel   = body["user"]["id"]

    def _v(block, fallback=""):
        el = vals.get(block, {}).get("value", {})
        if not el:
            return fallback
        return (el.get("value") or el.get("selected_option", {}).get("value") or fallback).strip()

    company    = _v("company")
    role_title = _v("role_title")
    status     = _v("status", "Researching")
    priority   = _v("priority", "Medium")
    url        = _v("url")
    job_source = _v("job_source")
    note       = _v("note")

    try:
        record = _create_app({
            "company":    company,
            "domain":     "",
            "role_title": role_title,
            "status":     status,
            "priority":   priority,
            "url":        url,
            "job_source": job_source,
        })
        if note:
            _add_comment(record["id"], note)
        client.chat_postMessage(
            channel=channel,
            text=(
                f":white_check_mark: Added *{role_title}* at *{company}* "
                f"({status}, {priority} priority)\n"
                f"<{TRACKER_URL}?app={record['id']}|View in Tracker →>"
            ),
        )
    except Exception as exc:
        client.chat_postMessage(channel=channel, text=f":x: Failed to add application: {exc}")


# ---------------------------------------------------------------------------
# /track-update — update an application's status
# ---------------------------------------------------------------------------

@app.command("/track-update")
def track_update_command(ack, body, client, respond):
    ack()
    try:
        options = _app_options()
    except Exception as exc:
        respond(f":x: Could not load applications: {exc}")
        return

    if not options:
        respond("No applications found. Use `/track-add` to create one.")
        return

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "track_update_submit",
            "title": {"type": "plain_text", "text": "Update Application"},
            "submit": {"type": "plain_text", "text": "Update"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "app_id",
                    "label": {"type": "plain_text", "text": "Application"},
                    "element": {
                        "type": "static_select",
                        "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "Select an application…"},
                        "options": options,
                    },
                },
                {
                    "type": "input",
                    "block_id": "status",
                    "label": {"type": "plain_text", "text": "New Status"},
                    "element": {
                        "type": "static_select",
                        "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "Select new status…"},
                        "options": [{"text": {"type": "plain_text", "text": s}, "value": s} for s in VALID_STATUSES],
                    },
                },
                {
                    "type": "input",
                    "block_id": "note",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Add a Note (optional)"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "multiline": True,
                                "placeholder": {"type": "plain_text", "text": "e.g. Got a callback from recruiter"}},
                },
            ],
        },
    )


@app.view("track_update_submit")
def track_update_view_submit(ack, body, client, view):
    ack()
    vals    = view["state"]["values"]
    channel = body["user"]["id"]

    app_id = vals["app_id"]["value"]["selected_option"]["value"]
    status = vals["status"]["value"]["selected_option"]["value"]
    note   = (vals.get("note", {}).get("value", {}) or {}).get("value", "").strip()

    try:
        record = _update_app(app_id, {"status": status})
        if note:
            _add_comment(app_id, note)
        client.chat_postMessage(
            channel=channel,
            text=(
                f":pencil2: Updated *{record.get('role_title')}* at *{record.get('company')}* "
                f"→ *{status}*"
                + (f"\n> {note}" if note else "")
                + f"\n<{TRACKER_URL}?app={app_id}|View in Tracker →>"
            ),
        )
    except Exception as exc:
        client.chat_postMessage(channel=channel, text=f":x: Failed to update: {exc}")


# ---------------------------------------------------------------------------
# /track-note — add a comment to an application
# ---------------------------------------------------------------------------

@app.command("/track-note")
def track_note_command(ack, body, client, respond):
    ack()
    try:
        options = _app_options()
    except Exception as exc:
        respond(f":x: Could not load applications: {exc}")
        return

    if not options:
        respond("No applications found. Use `/track-add` to create one.")
        return

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "track_note_submit",
            "title": {"type": "plain_text", "text": "Add Note"},
            "submit": {"type": "plain_text", "text": "Add Note"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "app_id",
                    "label": {"type": "plain_text", "text": "Application"},
                    "element": {
                        "type": "static_select",
                        "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "Select an application…"},
                        "options": options,
                    },
                },
                {
                    "type": "input",
                    "block_id": "note",
                    "label": {"type": "plain_text", "text": "Note"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "multiline": True,
                                "placeholder": {"type": "plain_text",
                                                "text": "e.g. Spoke with recruiter — next step is HM interview"}},
                },
            ],
        },
    )


@app.view("track_note_submit")
def track_note_view_submit(ack, body, client, view):
    ack()
    vals    = view["state"]["values"]
    channel = body["user"]["id"]

    app_id = vals["app_id"]["value"]["selected_option"]["value"]
    note   = vals["note"]["value"]["value"].strip()

    try:
        rec = _get_app(app_id)
        _add_comment(app_id, note)
        client.chat_postMessage(
            channel=channel,
            text=(
                f":speech_balloon: Note added to *{rec.get('role_title')}* at *{rec.get('company')}*\n"
                f"> {note}\n"
                f"<{TRACKER_URL}?app={app_id}|View in Tracker →>"
            ),
        )
    except Exception as exc:
        client.chat_postMessage(channel=channel, text=f":x: Failed to add note: {exc}")


# ---------------------------------------------------------------------------
# /track-delete — delete an application (with confirmation step)
# ---------------------------------------------------------------------------

@app.command("/track-delete")
def track_delete_command(ack, body, client, respond):
    ack()
    try:
        options = _app_options(active_only=False)  # allow deleting any record
    except Exception as exc:
        respond(f":x: Could not load applications: {exc}")
        return

    if not options:
        respond("No applications found.")
        return

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "track_delete_select",
            "title": {"type": "plain_text", "text": "Delete Application"},
            "submit": {"type": "plain_text", "text": "Continue →"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": ":warning: *This will permanently delete the record and all its comments.*"},
                },
                {
                    "type": "input",
                    "block_id": "app_id",
                    "label": {"type": "plain_text", "text": "Application to delete"},
                    "element": {
                        "type": "static_select",
                        "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "Select an application…"},
                        "options": options,
                    },
                },
            ],
        },
    )


@app.view("track_delete_select")
def track_delete_select_submit(ack, body, client, view):
    """First step: show confirmation modal."""
    app_id = view["state"]["values"]["app_id"]["value"]["selected_option"]["value"]
    label  = view["state"]["values"]["app_id"]["value"]["selected_option"]["text"]["text"]

    ack({
        "response_action": "push",
        "view": {
            "type": "modal",
            "callback_id": "track_delete_confirm",
            "title": {"type": "plain_text", "text": "Confirm Delete"},
            "submit": {"type": "plain_text", "text": "Delete"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "private_metadata": app_id,
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":warning: Are you sure you want to permanently delete:\n\n*{label}*\n\nThis cannot be undone.",
                    },
                },
            ],
        },
    })


@app.view("track_delete_confirm")
def track_delete_confirm_submit(ack, body, client, view):
    ack()
    app_id  = view["private_metadata"]
    channel = body["user"]["id"]

    try:
        rec = _get_app(app_id)
        _delete_app(app_id)
        client.chat_postMessage(
            channel=channel,
            text=(
                f":wastebasket: Deleted *{rec.get('role_title')}* at *{rec.get('company')}*."
            ),
        )
    except Exception as exc:
        client.chat_postMessage(channel=channel, text=f":x: Failed to delete: {exc}")


# ---------------------------------------------------------------------------
# /apply — generate resume + cover letter
# ---------------------------------------------------------------------------

@app.command("/apply")
def apply_command(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "apply_submit",
            "title": {"type": "plain_text", "text": "Generate Application"},
            "submit": {"type": "plain_text", "text": "Generate"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "company",
                    "label": {"type": "plain_text", "text": "Company name"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "placeholder": {"type": "plain_text", "text": "Acme Corp"}},
                },
                {
                    "type": "input",
                    "block_id": "role",
                    "label": {"type": "plain_text", "text": "Role title"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "placeholder": {"type": "plain_text", "text": "Solutions Engineer"}},
                },
                {
                    "type": "input",
                    "block_id": "contact",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Hiring manager name (optional)"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "placeholder": {"type": "plain_text", "text": "Jane Smith"}},
                },
                {
                    "type": "input",
                    "block_id": "job_posting",
                    "label": {"type": "plain_text", "text": "Job posting (paste full text)"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "multiline": True,
                                "placeholder": {"type": "plain_text", "text": "Paste the full job description here…"}},
                },
            ],
        },
    )


@app.view("apply_submit")
def apply_view_submit(ack, body, client, view):
    ack()
    vals        = view["state"]["values"]
    company     = vals["company"]["value"]["value"].strip()
    role        = vals["role"]["value"]["value"].strip()
    contact     = (vals["contact"]["value"]["value"] or "").strip()
    job_posting = vals["job_posting"]["value"]["value"].strip()
    channel     = body["user"]["id"]

    def _run():
        client.chat_postMessage(
            channel=channel,
            text=f":hourglass_flowing_sand: Starting application for *{role}* at *{company}*…",
        )
        try:
            run_data = _post_run(job_posting, company, role, contact)
            run_id   = run_data["run_id"]
            status   = _poll_run(run_id)
        except Exception as exc:
            client.chat_postMessage(channel=channel, text=f":x: Error starting run: {exc}")
            return

        if status["status"] == "done":
            client.chat_postMessage(
                channel=channel,
                text=(
                    f":white_check_mark: *{role} @ {company}* — done!\n"
                    f"Resume, ATS resume, and cover letter are in your Google Drive.\n"
                    f"<{API_BASE}|Open the app> to download the files."
                ),
            )
        elif status["status"] == "timeout":
            client.chat_postMessage(
                channel=channel,
                text=f":warning: Run is taking longer than expected. Check <{API_BASE}|the app> for status.",
            )
        else:
            client.chat_postMessage(
                channel=channel,
                text=f":x: Run failed: {status.get('error', 'Unknown error')}",
            )

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# /prep — generate interview prep doc
# ---------------------------------------------------------------------------

@app.command("/prep")
def prep_command(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "prep_submit",
            "title": {"type": "plain_text", "text": "Interview Prep"},
            "submit": {"type": "plain_text", "text": "Generate"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "company",
                    "label": {"type": "plain_text", "text": "Company name"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "placeholder": {"type": "plain_text", "text": "Acme Corp"}},
                },
                {
                    "type": "input",
                    "block_id": "role",
                    "label": {"type": "plain_text", "text": "Role title"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "placeholder": {"type": "plain_text", "text": "Solutions Engineer"}},
                },
                {
                    "type": "input",
                    "block_id": "round_type",
                    "label": {"type": "plain_text", "text": "Round type"},
                    "element": {
                        "type": "static_select",
                        "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "Select round type"},
                        "options": [
                            {"text": {"type": "plain_text", "text": rt.replace("_", " ").title()},
                             "value": rt}
                            for rt in ROUND_TYPES
                        ],
                    },
                },
                {
                    "type": "input",
                    "block_id": "interviewer",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Interviewer name (optional)"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "placeholder": {"type": "plain_text", "text": "Jane Smith, VP Engineering"}},
                },
                {
                    "type": "input",
                    "block_id": "focus",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Focus areas (optional)"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "placeholder": {"type": "plain_text", "text": "System design, API architecture"}},
                },
                {
                    "type": "input",
                    "block_id": "job_posting",
                    "label": {"type": "plain_text", "text": "Job posting (paste full text)"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "multiline": True,
                                "placeholder": {"type": "plain_text", "text": "Paste the full job description here…"}},
                },
            ],
        },
    )


@app.view("prep_submit")
def prep_view_submit(ack, body, client, view):
    ack()
    vals        = view["state"]["values"]
    company     = vals["company"]["value"]["value"].strip()
    role        = vals["role"]["value"]["value"].strip()
    round_type  = vals["round_type"]["value"]["selected_option"]["value"]
    interviewer = (vals["interviewer"]["value"]["value"] or "").strip()
    focus       = (vals["focus"]["value"]["value"] or "").strip()
    job_posting = vals["job_posting"]["value"]["value"].strip()
    channel     = body["user"]["id"]

    def _run():
        client.chat_postMessage(
            channel=channel,
            text=f":hourglass_flowing_sand: Generating *{round_type.replace('_', ' ').title()}* prep for *{role}* at *{company}*…",
        )
        try:
            prep_data = _post_prep(job_posting, company, role, round_type, focus, interviewer)
            prep_id   = prep_data["prep_id"]
            status    = _poll_prep(prep_id)
        except Exception as exc:
            client.chat_postMessage(channel=channel, text=f":x: Error starting prep: {exc}")
            return

        if status["status"] == "done":
            client.chat_postMessage(
                channel=channel,
                text=(
                    f":white_check_mark: *{round_type.replace('_', ' ').title()} prep* for *{role} @ {company}* — done!\n"
                    f"Your prep card is in Google Drive.\n"
                    f"<{API_BASE}|Open the app> to download it."
                ),
            )
        elif status["status"] == "timeout":
            client.chat_postMessage(
                channel=channel,
                text=f":warning: Prep is taking longer than expected. Check <{API_BASE}|the app> for status.",
            )
        else:
            client.chat_postMessage(
                channel=channel,
                text=f":x: Prep failed: {status.get('error', 'Unknown error')}",
            )

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# /jobstatus — check API health
# ---------------------------------------------------------------------------

@app.command("/jobstatus")
def jobstatus_command(ack, respond):
    ack()
    try:
        r = requests.get(f"{API_BASE}/api/health", timeout=10)
        r.raise_for_status()
        data = r.json()
        respond(f":white_check_mark: API is up. Storage configured: `{data.get('storage', '?')}`")
    except Exception as exc:
        respond(f":x: API health check failed: {exc}")


# ---------------------------------------------------------------------------
# /me — account info
# ---------------------------------------------------------------------------

@app.command("/me")
def me_command(ack, respond):
    ack()
    try:
        r = _api("get", "/api/auth/me")
        r.raise_for_status()
        u = r.json()
    except Exception as exc:
        respond(f":x: Could not load account: {exc}")
        return

    verified = ":white_check_mark: Verified" if u.get("email_verified", True) else ":x: Not verified"
    respond(
        f":bust_in_silhouette: *{u.get('display_name') or u.get('email')}*\n"
        f"• Email: `{u.get('email')}`  ·  {verified}\n"
        f"• Role: `{u.get('role', 'user')}`\n"
        f"• Resume on file: {'✓' if u.get('has_resume') else '—'}  ·  "
        f"Profile guide: {'✓' if u.get('has_profile') else '—'}"
    )


# ---------------------------------------------------------------------------
# /runs — recent agent run folders from Drive
# ---------------------------------------------------------------------------

@app.command("/runs")
def runs_command(ack, respond):
    ack()
    try:
        r = _api("get", "/api/gdrive/runs")
        r.raise_for_status()
        runs = r.json().get("runs", [])
    except Exception as exc:
        respond(f":x: Could not load runs: {exc}")
        return

    if not runs:
        respond("No agent runs found in Drive yet.")
        return

    shown = runs[:10]
    lines = []
    for run in shown:
        name  = run.get("name", "")
        idx   = name.find("_")
        label = f"{name[:idx]} · {name[idx+1:].replace('_',' ')}" if idx > 0 else name
        link  = run.get("web_view_link", "")
        badge = " ↩" if run.get("source") == "legacy" else ""
        lines.append(f"• {'<' + link + '|' + label + '>' if link else label}{badge}")

    header = f":file_folder: *Recent Agent Runs* ({len(runs)} total)"
    if len(runs) > 10:
        lines.append(f"_…and {len(runs) - 10} more — <{API_BASE}|open the app> to see all._")
    respond(header + "\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# /activity — recent personal audit events
# ---------------------------------------------------------------------------

@app.command("/activity")
def activity_command(ack, respond):
    ack()
    try:
        r = _api("get", "/api/audit/me")
        r.raise_for_status()
        events = r.json()[:10]
    except Exception as exc:
        respond(f":x: Could not load activity: {exc}")
        return

    if not events:
        respond("No recent activity found.")
        return

    lines = []
    for e in events:
        action = e.get("action", "?")
        ts     = (e.get("timestamp") or "")[:16].replace("T", " ")
        det    = e.get("details") or {}
        det_str = ""
        if det:
            first = next(((k, v) for k, v in det.items() if v), None)
            if first:
                det_str = f" · `{first[0]}`: {str(first[1])[:50]}"
        lines.append(f"• `{action}` _{ts}_{det_str}")

    respond(f":scroll: *Your Recent Activity*\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# /company — BrandFetch company lookup
# ---------------------------------------------------------------------------

@app.command("/company")
def company_command(ack, respond, body):
    ack()
    query = body.get("text", "").strip()
    if not query:
        respond("Usage: `/company [company name]`\nExample: `/company Salesforce`")
        return
    try:
        r = _api("get", "/api/companies/search", params={"q": query})
        r.raise_for_status()
        results = r.json()
    except Exception as exc:
        respond(f":x: Search failed: {exc}")
        return

    if not results:
        respond(f":mag: No results found for *{query}*.")
        return

    lines = []
    for c in results[:5]:
        name   = c.get("name", "?")
        domain = c.get("domain", "")
        desc   = c.get("description", "")
        line   = f"• *{name}*"
        if domain:
            line += f"  `{domain}`"
        if desc:
            line += f"\n  _{desc}_"
        lines.append(line)

    respond(f":mag: *Company search: {query}*\n\n" + "\n\n".join(lines))


# ---------------------------------------------------------------------------
# /resend-verify — resend email verification
# ---------------------------------------------------------------------------

@app.command("/resend-verify")
def resend_verify_command(ack, respond):
    ack()
    try:
        r = _api("post", "/api/auth/resend-verification")
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        respond(f":x: Failed: {exc}")
        return

    if data.get("already_verified"):
        respond(":white_check_mark: Your email is already verified — nothing to do.")
    elif data.get("sent"):
        respond(":email: Verification email sent! Check your inbox.")
    else:
        respond(":warning: Could not send — check that RESEND_API_KEY is configured.")


# ---------------------------------------------------------------------------
# /track-view — view full application details
# ---------------------------------------------------------------------------

@app.command("/track-view")
def track_view_command(ack, body, client, respond):
    ack()
    try:
        options = _app_options()
    except Exception as exc:
        respond(f":x: Could not load applications: {exc}")
        return

    if not options:
        respond("No applications found. Use `/track-add` to create one.")
        return

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "track_view_submit",
            "title": {"type": "plain_text", "text": "View Application"},
            "submit": {"type": "plain_text", "text": "View"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "app_id",
                    "label": {"type": "plain_text", "text": "Application"},
                    "element": {
                        "type": "static_select",
                        "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "Select an application…"},
                        "options": options,
                    },
                },
            ],
        },
    )


@app.view("track_view_submit")
def track_view_view_submit(ack, body, client, view):
    ack()
    app_id  = view["state"]["values"]["app_id"]["value"]["selected_option"]["value"]
    channel = body["user"]["id"]
    try:
        a = _get_app(app_id)
        lines = [
            f":briefcase: *{a.get('company')} — {a.get('role_title')}*",
            f"• Status: `{a.get('status')}` | Priority: `{a.get('priority')}`",
        ]
        if a.get("date_applied"):
            lines.append(f"• Applied: {a['date_applied'][:10]}")
        if a.get("location"):
            lines.append(f"• Location: {a['location']}")
        if a.get("salary_range"):
            lines.append(f"• Salary: {a['salary_range']}")
        if a.get("recruiter_name"):
            rec = a["recruiter_name"]
            if a.get("recruiter_email"):
                rec += f" <{a['recruiter_email']}>"
            lines.append(f"• Recruiter: {rec}")
        if a.get("url"):
            lines.append(f"• <{a['url']}|Job Posting ↗>")
        comments = a.get("comments", [])
        if comments:
            lines.append(f"\n:speech_balloon: *Notes ({len(comments)}):*")
            for c in comments[-3:]:
                lines.append(f"  › {c['text'][:120]}")
        client.chat_postMessage(channel=channel, text="\n".join(lines))
    except Exception as exc:
        client.chat_postMessage(channel=channel, text=f":x: Could not load application: {exc}")


# ---------------------------------------------------------------------------
# /help — command reference
# ---------------------------------------------------------------------------

@app.command("/help")
def help_command(ack, respond):
    ack()
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "📖  Job Apply — Command Reference"},
        },

        # Agent runs
        {"type": "section", "text": {"type": "mrkdwn", "text": "*🤖  Agent Runs*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            "*/apply* — Generate resume, ATS resume & cover letter for a job\n"
            "*/prep* — Generate an interview prep document\n"
            "*/runs* — List your recent agent run folders from Drive"
        )}},
        {"type": "divider"},

        # Tracker
        {"type": "section", "text": {"type": "mrkdwn", "text": "*📋  Application Tracker*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            "*/tracker* — Pipeline summary grouped by status\n"
            "*/track-list `[status]`* — List applications (optional: filter by status)\n"
            "*/track-view* — View full details of an application\n"
            "*/track-add* — Add a new application record\n"
            "*/track-update* — Update an application's status (+ optional note)\n"
            "*/track-note* — Add a comment/note to an application\n"
            "*/track-delete* — Delete an application (two-step confirm)"
        )}},
        {"type": "divider"},

        # Lookup
        {"type": "section", "text": {"type": "mrkdwn", "text": "*🔍  Lookup & Info*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            "*/company `[name]`* — Search company info — logo, domain, description\n"
            "*/me* — Show your account details and verification status\n"
            "*/activity* — Show your 10 most recent audit events"
        )}},
        {"type": "divider"},

        # System
        {"type": "section", "text": {"type": "mrkdwn", "text": "*🛠️  System*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            "*/jobstatus* — Check API health and storage status\n"
            "*/resend-verify* — Resend your email verification\n"
            "*/help* — Show this command reference"
        )}},
        {"type": "divider"},

        # Footer
        {
            "type": "context",
            "elements": [{"type": "mrkdwn",
                          "text": f"Job Apply Agent · <{API_BASE}|Open App> · <{API_BASE}/admin.html|Admin Dashboard>"}],
        },
    ]
    respond(blocks=blocks, text="Job Apply — Command Reference")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if SLACK_APP_TOKEN:
        handler = SocketModeHandler(app, SLACK_APP_TOKEN)
        print("Starting in Socket Mode")
        handler.start()
    else:
        print(f"Starting HTTP server on port {PORT}")
        app.start(port=PORT)
