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


def _app_options(apps: list[dict] | None = None, max_opts: int = 100) -> list[dict]:
    """Return Slack static_select options for an application list.
    Sorted by status priority so most active appear first."""
    if apps is None:
        apps = _get_apps()

    order = {s: i for i, s in enumerate(VALID_STATUSES)}
    apps = sorted(apps, key=lambda a: (order.get(a.get("status", ""), 99), a.get("company", "")))

    options = []
    for a in apps[:max_opts]:
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
        options = _app_options()
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
