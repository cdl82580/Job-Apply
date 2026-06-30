"""
Teams bot activity handler — maps user messages and Adaptive Card submissions
to the same FastAPI backend the Slack bot uses.

Commands (type in chat):
  apply      — generate resume + ATS resume + cover letter
  aq         — answer an application question
  prep       — generate interview prep doc
  tracker    — pipeline summary
  track list — list applications (optionally filter by status)
  track add  — add a new application
  track view — view application details
  runs       — list recent Drive run folders
  help       — command reference
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any

from botbuilder.core import ActivityHandler, CardFactory, MessageFactory, TurnContext
from botbuilder.schema import (
    Activity,
    ActivityTypes,
    Attachment,
    ChannelAccount,
    ConversationReference,
    HeroCard,
    CardAction,
)

import api_client

CARDS_DIR = Path(__file__).parent / "cards"

VALID_STATUSES = [
    "Not Applying", "Researching", "Applied", "Phone Screen",
    "Interviewing", "On Hold", "Offer", "Rejected",
]

STATUS_EMOJI = {
    "Interviewing":  "\U0001f3af",
    "Phone Screen":  "\U0001f4de",
    "Applied":       "✅",
    "Researching":   "\U0001f52c",
    "On Hold":       "⏸️",
    "Offer":         "\U0001f389",
    "Rejected":      "❌",
    "Not Applying":  "\U0001f6ab",
}


def _load_card(name: str) -> dict:
    with open(CARDS_DIR / f"{name}.json") as f:
        return json.load(f)


def _card_attachment(card_json: dict) -> Attachment:
    return CardFactory.adaptive_card(card_json)


class JobApplyBot(ActivityHandler):
    """Microsoft Teams bot for the Job Apply agent platform."""

    async def on_members_added_activity(
        self, members_added: list[ChannelAccount], turn_context: TurnContext,
    ):
        for member in members_added:
            if member.id != turn_context.activity.recipient.id:
                welcome = (
                    "**Welcome to Job Apply!** \U0001f4bc\n\n"
                    "I help you generate tailored resumes, cover letters, and "
                    "interview prep materials.\n\n"
                    "Type **help** to see available commands."
                )
                await turn_context.send_activity(MessageFactory.text(welcome))

    async def on_message_activity(self, turn_context: TurnContext):
        # Adaptive Card submissions come as message activities with a value payload
        if turn_context.activity.value:
            await self._handle_card_submit(turn_context)
            return

        text = (turn_context.activity.text or "").strip().lower()

        # Strip bot mention in group chats
        if turn_context.activity.entities:
            for entity in turn_context.activity.entities:
                if entity.type == "mention":
                    mention_text = entity.additional_properties.get("text", "")
                    text = text.replace(mention_text.lower(), "").strip()

        if text in ("apply", "/apply"):
            await self._cmd_apply(turn_context)
        elif text in ("aq", "/aq"):
            await self._cmd_aq(turn_context)
        elif text in ("prep", "/prep"):
            await self._cmd_prep(turn_context)
        elif text in ("tracker", "/tracker"):
            await self._cmd_tracker(turn_context)
        elif text.startswith(("track list", "/track-list", "track-list")):
            status_filter = text.split(maxsplit=2)[-1] if len(text.split()) > 2 else ""
            if status_filter in ("list", "track-list", "/track-list", "track"):
                status_filter = ""
            await self._cmd_track_list(turn_context, status_filter)
        elif text in ("track add", "/track-add", "track-add"):
            await self._cmd_track_add(turn_context)
        elif text.startswith(("track view", "/track-view", "track-view")):
            await self._cmd_track_view(turn_context)
        elif text in ("optimize", "/optimize"):
            await self._cmd_optimize(turn_context)
        elif text in ("runs", "/runs"):
            await self._cmd_runs(turn_context)
        elif text in ("help", "/help"):
            await self._cmd_help(turn_context)
        else:
            await turn_context.send_activity(
                MessageFactory.text(
                    "I didn't recognise that command. Type **help** to see what I can do."
                )
            )

    # ── Card form launchers ──────────────────────────────────────────────

    async def _cmd_apply(self, ctx: TurnContext):
        card = _load_card("apply_form")
        await ctx.send_activity(
            MessageFactory.attachment(_card_attachment(card))
        )

    async def _cmd_aq(self, ctx: TurnContext):
        card = _load_card("aq_form")
        await ctx.send_activity(
            MessageFactory.attachment(_card_attachment(card))
        )

    async def _cmd_prep(self, ctx: TurnContext):
        card = _load_card("prep_form")
        await ctx.send_activity(
            MessageFactory.attachment(_card_attachment(card))
        )

    async def _cmd_track_add(self, ctx: TurnContext):
        card = _load_card("track_add_form")
        await ctx.send_activity(
            MessageFactory.attachment(_card_attachment(card))
        )

    async def _cmd_optimize(self, ctx: TurnContext):
        try:
            apps = await asyncio.to_thread(api_client.get_applications)
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Error loading applications: {exc}"))
            return

        active = [a for a in apps if a.get("status") not in ("Rejected", "Not Applying")]
        if not active:
            await ctx.send_activity(
                MessageFactory.text("❌ No active applications found. Add one with **track add** first.")
            )
            return

        choices = [
            {"title": f"{a.get('company', '?')} — {a.get('role', '?')}", "value": a["id"]}
            for a in active[:20]
        ]

        card = _load_card("optimize_form")
        app_selector = {
            "type": "Input.ChoiceSet",
            "id": "app_id",
            "label": "Application",
            "isRequired": True,
            "errorMessage": "Select an application",
            "choices": choices,
        }
        card["body"].insert(2, app_selector)
        await ctx.send_activity(MessageFactory.attachment(_card_attachment(card)))

    # ── Instant commands ─────────────────────────────────────────────────
    # api_client calls below run via asyncio.to_thread: in production this
    # bot is mounted on the same FastAPI process it calls over HTTP, so a
    # direct blocking `requests` call here would stall the only event loop
    # — including the inbound self-request it's waiting on.

    async def _cmd_tracker(self, ctx: TurnContext):
        try:
            apps = await asyncio.to_thread(api_client.get_applications)
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Could not reach the tracker: {exc}"))
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
                lines.append(f"{STATUS_EMOJI[status]} **{status}:** {n}")

        text = (
            f"\U0001f4ca **Application Pipeline** ({len(apps)} total)\n\n"
            + "\n".join(lines)
        )
        await ctx.send_activity(MessageFactory.text(text))

    async def _cmd_track_list(self, ctx: TurnContext, status_filter: str):
        resolved: str | None = None
        if status_filter:
            matches = [s for s in VALID_STATUSES if s.lower() == status_filter.lower()]
            if matches:
                resolved = matches[0]
            else:
                await ctx.send_activity(
                    MessageFactory.text(
                        f"❌ Unknown status `{status_filter}`. "
                        f"Valid: {', '.join(f'`{s}`' for s in VALID_STATUSES)}"
                    )
                )
                return

        try:
            apps = await asyncio.to_thread(api_client.get_applications, status=resolved)
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Error: {exc}"))
            return

        if not apps:
            label = f"**{resolved}**" if resolved else "active"
            await ctx.send_activity(MessageFactory.text(f"No {label} applications found."))
            return

        order = {s: i for i, s in enumerate(VALID_STATUSES)}
        apps = sorted(apps, key=lambda a: (order.get(a.get("status", ""), 99), a.get("company", "")))
        shown = apps[:15]

        lines = [f"\U0001f4cb **Applications{' — ' + resolved if resolved else ''}** ({len(apps)} total)\n"]
        for a in shown:
            emoji = STATUS_EMOJI.get(a.get("status", ""), "")
            company = a.get("company", "?")
            role = a.get("role", "?")
            status = a.get("status", "?")
            lines.append(f"{emoji} **{company}** — {role} ({status})")

        if len(apps) > 15:
            lines.append(f"\n_…and {len(apps) - 15} more._")

        await ctx.send_activity(MessageFactory.text("\n".join(lines)))

    async def _cmd_track_view(self, ctx: TurnContext):
        try:
            apps = await asyncio.to_thread(api_client.get_applications)
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Error: {exc}"))
            return

        if not apps:
            await ctx.send_activity(MessageFactory.text("No applications found."))
            return

        choices = [
            {"title": f"{a.get('company', '?')} — {a.get('role', '?')}", "value": a["id"]}
            for a in apps[:20]
        ]

        card = {
            "type": "AdaptiveCard",
            "version": "1.5",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "body": [
                {"type": "TextBlock", "text": "View Application", "size": "Large", "weight": "Bolder"},
                {
                    "type": "Input.ChoiceSet",
                    "id": "app_id",
                    "label": "Select application",
                    "isRequired": True,
                    "choices": choices,
                },
            ],
            "actions": [
                {"type": "Action.Submit", "title": "View", "data": {"action": "track_view_submit"}},
            ],
        }
        await ctx.send_activity(MessageFactory.attachment(_card_attachment(card)))

    async def _cmd_runs(self, ctx: TurnContext):
        try:
            runs = await asyncio.to_thread(api_client.get_agent_runs)
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Error: {exc}"))
            return

        if not runs:
            await ctx.send_activity(MessageFactory.text("No agent runs found."))
            return

        TYPE_LABELS = {
            "resume": "\U0001f4c4 Resume",
            "interview_prep": "\U0001f393 Prep",
            "aq": "\U00002753 AQ",
            "optimize": "\U0001f504 Optimize",
            "thank_you": "\U0001f64f Thank You",
            "scoring": "\U0001f3af Score",
        }
        STATUS_BADGES = {
            "completed": "✅",
            "failed": "❌",
            "running": "⏳",
            "queued": "\U0001f551",
        }

        shown = runs[:15]
        lines = [f"\U0001f4c2 **Recent Agent Runs** ({len(runs)} total)\n"]
        for r in shown:
            type_label = TYPE_LABELS.get(r.get("type", ""), r.get("type", "?"))
            status_badge = STATUS_BADGES.get(r.get("status", ""), "")
            company = r.get("company", "")
            role = r.get("role", "")
            label = f"{company} — {role}" if company else r.get("id", "?")[:8]
            drive_url = r.get("gdrive_folder_url", "")
            if drive_url:
                lines.append(f"- {status_badge} {type_label}: [{label}]({drive_url})")
            else:
                lines.append(f"- {status_badge} {type_label}: {label}")

        if len(runs) > 15:
            lines.append(f"\n_…and {len(runs) - 15} more._")

        await ctx.send_activity(MessageFactory.text("\n".join(lines)))

    async def _cmd_help(self, ctx: TurnContext):
        text = (
            "**Job Apply — Teams Bot Commands**\n\n"
            "**\U0001f916 Agent Commands**\n"
            "- **apply** — Generate resume + ATS resume + cover letter\n"
            "- **aq** — Answer an application question\n"
            "- **prep** — Generate interview prep doc\n"
            "- **optimize** — Refine existing run documents\n\n"
            "**\U0001f4cb Tracker Commands**\n"
            "- **tracker** — Pipeline summary\n"
            "- **track list** [status] — List applications\n"
            "- **track add** — Add a new application\n"
            "- **track view** — View application details\n\n"
            "**\U0001f527 Other**\n"
            "- **runs** — List recent Drive run folders\n"
            "- **help** — This message"
        )
        await ctx.send_activity(MessageFactory.text(text))

    # ── Card submission handler ──────────────────────────────────────────

    async def _handle_card_submit(self, ctx: TurnContext):
        data = ctx.activity.value or {}
        action = data.get("action", "")

        if action == "apply_submit":
            await self._submit_apply(ctx, data)
        elif action == "prep_submit":
            await self._submit_prep(ctx, data)
        elif action == "aq_submit":
            await self._submit_aq(ctx, data)
        elif action == "track_add_submit":
            await self._submit_track_add(ctx, data)
        elif action == "optimize_submit":
            await self._submit_optimize(ctx, data)
        elif action == "track_view_submit":
            await self._submit_track_view(ctx, data)
        else:
            await ctx.send_activity(MessageFactory.text(f"Unknown action: {action}"))

    # ── Long-running agent submissions (threaded) ────────────────────────

    async def _submit_apply(self, ctx: TurnContext, data: dict):
        company = (data.get("company") or "").strip()
        role = (data.get("role") or "").strip()
        contact = (data.get("contact") or "").strip()
        job_posting = (data.get("job_posting") or "").strip()

        if not company or not role or not job_posting:
            await ctx.send_activity(MessageFactory.text("❌ Company, role, and job posting are required."))
            return

        await ctx.send_activity(
            MessageFactory.text(f"⏳ Starting application for **{role}** at **{company}**…")
        )

        conv_ref = TurnContext.get_conversation_reference(ctx.activity)
        adapter = ctx.adapter

        def _run():
            try:
                run_data = api_client.post_run(job_posting, company, role, contact)
                run_id = run_data["run_id"]
                status = api_client.poll_run(run_id)
            except Exception as exc:
                self._proactive_message(adapter, conv_ref, f"❌ Error starting run: {exc}")
                return

            if status["status"] == "done":
                self._proactive_message(
                    adapter, conv_ref,
                    f"✅ **{role} @ {company}** — done!\n\n"
                    f"Resume, ATS resume, and cover letter are in your Google Drive.",
                )
            elif status["status"] == "timeout":
                self._proactive_message(
                    adapter, conv_ref,
                    f"⚠️ Run is taking longer than expected. Check the app for status.",
                )
            else:
                self._proactive_message(
                    adapter, conv_ref,
                    f"❌ Run failed: {status.get('error', 'Unknown error')}",
                )

        threading.Thread(target=_run, daemon=True).start()

    async def _submit_prep(self, ctx: TurnContext, data: dict):
        company = (data.get("company") or "").strip()
        role = (data.get("role") or "").strip()
        round_type = (data.get("round_type") or "").strip()
        interviewer = (data.get("interviewer") or "").strip()
        focus = (data.get("focus") or "").strip()
        job_posting = (data.get("job_posting") or "").strip()

        if not company or not role or not round_type or not job_posting:
            await ctx.send_activity(
                MessageFactory.text("❌ Company, role, round type, and job posting are required.")
            )
            return

        await ctx.send_activity(
            MessageFactory.text(f"⏳ Generating prep for **{role}** at **{company}** ({round_type})…")
        )

        conv_ref = TurnContext.get_conversation_reference(ctx.activity)
        adapter = ctx.adapter

        def _run():
            try:
                prep_data = api_client.post_prep(job_posting, company, role, round_type, focus, interviewer)
                prep_id = prep_data["prep_id"]
                status = api_client.poll_prep(prep_id)
            except Exception as exc:
                self._proactive_message(adapter, conv_ref, f"❌ Error: {exc}")
                return

            if status["status"] == "done":
                self._proactive_message(
                    adapter, conv_ref,
                    f"✅ **Interview prep for {role} @ {company}** — done!\n\n"
                    f"Your prep doc is in Google Drive.",
                )
            elif status["status"] == "timeout":
                self._proactive_message(adapter, conv_ref, "⚠️ Prep is taking longer than expected.")
            else:
                self._proactive_message(
                    adapter, conv_ref, f"❌ Prep failed: {status.get('error', 'Unknown error')}"
                )

        threading.Thread(target=_run, daemon=True).start()

    async def _submit_aq(self, ctx: TurnContext, data: dict):
        company = (data.get("company") or "").strip()
        role = (data.get("role") or "").strip()
        question = (data.get("question") or "").strip()
        tone = (data.get("tone") or "professional").strip()
        char_limit_raw = data.get("char_limit")
        job_posting = (data.get("job_posting") or "").strip()

        char_limit = int(char_limit_raw) if char_limit_raw else None

        if not company or not role or not question or not job_posting:
            await ctx.send_activity(
                MessageFactory.text("❌ Company, role, question, and job posting are required.")
            )
            return

        await ctx.send_activity(
            MessageFactory.text(f"⏳ Generating answer for **{company}** — **{role}**…")
        )

        conv_ref = TurnContext.get_conversation_reference(ctx.activity)
        adapter = ctx.adapter

        def _run():
            try:
                aq_data = api_client.post_aq(question, job_posting, company, role, tone, char_limit)
                aq_id = aq_data["aq_id"]
                status = api_client.poll_aq(aq_id)
            except Exception as exc:
                self._proactive_message(adapter, conv_ref, f"❌ Error: {exc}")
                return

            if status["status"] == "done":
                answer = status.get("answer", "(no answer returned)")
                self._proactive_message(
                    adapter, conv_ref,
                    f"✅ **Answer ready** ({company} — {role})\n\n"
                    f"**Q:** {question}\n\n"
                    f"**A:** {answer}",
                )
            elif status["status"] == "timeout":
                self._proactive_message(adapter, conv_ref, "⚠️ Taking longer than expected.")
            else:
                self._proactive_message(
                    adapter, conv_ref, f"❌ Failed: {status.get('error', 'Unknown error')}"
                )

        threading.Thread(target=_run, daemon=True).start()

    async def _submit_optimize(self, ctx: TurnContext, data: dict):
        app_id = (data.get("app_id") or "").strip()
        instruction = (data.get("instruction") or "").strip()
        optimize_resume = data.get("optimize_resume", "true") == "true"
        optimize_cover_letter = data.get("optimize_cover_letter", "true") == "true"

        if not app_id or not instruction:
            await ctx.send_activity(
                MessageFactory.text("❌ Application and optimization prompt are required.")
            )
            return

        try:
            record = await asyncio.to_thread(api_client.get_application, app_id)
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Could not load application: {exc}"))
            return

        company = record.get("company", "?")
        role = record.get("role_title", record.get("role", "?"))

        runs = [r for r in (record.get("linked_runs") or []) if r.get("gdrive_folder_id")]
        if not runs:
            await ctx.send_activity(
                MessageFactory.text(
                    f"❌ **{role} @ {company}** has no linked Drive run folder. "
                    f"Run **apply** for this application first."
                )
            )
            return

        runs.sort(key=lambda r: r.get("linked_at", ""), reverse=True)
        preferred = next((r for r in runs if r.get("type") in ("resume", "optimize")), None)
        folder_id = (preferred or runs[0])["gdrive_folder_id"]

        await ctx.send_activity(
            MessageFactory.text(f"⏳ Optimizing **{role}** @ **{company}**…")
        )

        conv_ref = TurnContext.get_conversation_reference(ctx.activity)
        adapter = ctx.adapter

        def _run():
            try:
                result = api_client.post_optimize(
                    app_id, folder_id, instruction, company, role,
                    optimize_resume, optimize_cover_letter,
                )
                optimize_id = result["optimize_id"]
                status = api_client.poll_optimize(optimize_id)
            except Exception as exc:
                self._proactive_message(adapter, conv_ref, f"❌ Error: {exc}")
                return

            if status["status"] == "done":
                self._proactive_message(
                    adapter, conv_ref,
                    f"✅ **{role} @ {company}** — optimization complete!\n\n"
                    f"Updated documents are in your Google Drive run folder.",
                )
            elif status["status"] == "timeout":
                self._proactive_message(
                    adapter, conv_ref,
                    "⚠️ Optimization is taking longer than expected.",
                )
            else:
                self._proactive_message(
                    adapter, conv_ref,
                    f"❌ Optimization failed: {status.get('error', 'Unknown error')}",
                )

        threading.Thread(target=_run, daemon=True).start()

    # ── Instant card submissions ─────────────────────────────────────────

    async def _submit_track_add(self, ctx: TurnContext, data: dict):
        company = (data.get("company") or "").strip()
        role = (data.get("role") or "").strip()
        status_val = (data.get("status") or "Researching").strip()

        if not company or not role:
            await ctx.send_activity(MessageFactory.text("❌ Company and role are required."))
            return

        payload: dict[str, Any] = {
            "company": company,
            "role": role,
            "status": status_val,
        }
        for field in ("url", "location", "salary_range", "note"):
            val = (data.get(field) or "").strip()
            if val:
                payload[field] = val

        try:
            result = await asyncio.to_thread(api_client.create_application, payload)
            await ctx.send_activity(
                MessageFactory.text(
                    f"✅ Added **{company}** — {role} ({status_val})"
                )
            )
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Error: {exc}"))

    async def _submit_track_view(self, ctx: TurnContext, data: dict):
        app_id = (data.get("app_id") or "").strip()
        if not app_id:
            await ctx.send_activity(MessageFactory.text("❌ No application selected."))
            return

        try:
            a = await asyncio.to_thread(api_client.get_application, app_id)
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Error: {exc}"))
            return

        emoji = STATUS_EMOJI.get(a.get("status", ""), "")
        lines = [
            f"**{a.get('company', '?')}** — {a.get('role', '?')}",
            f"Status: {emoji} {a.get('status', '?')}",
        ]
        for field, label in [
            ("location", "Location"),
            ("salary_range", "Salary"),
            ("url", "URL"),
            ("date_applied", "Applied"),
            ("source", "Source"),
            ("recruiter_name", "Recruiter"),
        ]:
            val = a.get(field)
            if val:
                lines.append(f"{label}: {val}")

        comments = a.get("comments", [])
        if comments:
            lines.append("\n**Notes:**")
            for c in comments[-5:]:
                lines.append(f"- {c.get('text', '')}")

        await ctx.send_activity(MessageFactory.text("\n".join(lines)))

    # ── Proactive messaging helper ───────────────────────────────────────

    @staticmethod
    def _proactive_message(adapter, conv_ref: ConversationReference, text: str):
        import asyncio

        async def _send(tc: TurnContext):
            await tc.send_activity(MessageFactory.text(text))

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(adapter.continue_conversation(conv_ref, _send, None))
        finally:
            loop.close()
