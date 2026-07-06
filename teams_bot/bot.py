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
  confirm    — link your Teams identity to a Job Apply account
  whoami     — show which account you're linked as
  unlink     — remove your Teams identity's link
  help       — command reference

Auth model: the bot has no notion of "logged in" beyond a per-Teams-identity
link to a Job Apply account (see scripts/teams_links.py). The first time a
linked-or-not-yet-linked user runs any command other than help/confirm/unlink,
_resolve_user() checks the link, and if missing/expired, looks up the caller's
email via the Teams roster API and offers to link it. Links expire after
LINK_DAYS (scripts/teams_links.py) and must be re-confirmed.

If no Job Apply account matches the Teams email, _offer_manual_link() sends a
sign-in card (see scripts/teams_link_tokens.py + frontend/teams-link.html)
so the user can link an existing account under a different email instead —
password or Google, whichever they used to originally register.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from botbuilder.core import ActivityHandler, CardFactory, MessageFactory, TurnContext
from botbuilder.core.teams import TeamsInfo
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

# Adaptive Card TextBlock/FactSet color names (Good/Warning/Attention/Accent/Default).
STATUS_COLOR = {
    "Interviewing":  "Accent",
    "Phone Screen":  "Accent",
    "Applied":       "Good",
    "Offer":         "Good",
    "Researching":   "Default",
    "On Hold":       "Warning",
    "Rejected":      "Attention",
    "Not Applying":  "Attention",
}

# Commands that must work even without a linked account.
_NO_AUTH_COMMANDS = ("help", "/help", "confirm", "/confirm", "unlink", "/unlink")


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
        text = (turn_context.activity.text or "").strip().lower()

        # Strip bot mention in group chats
        if turn_context.activity.entities:
            for entity in turn_context.activity.entities:
                if entity.type == "mention":
                    mention_text = entity.additional_properties.get("text", "")
                    text = text.replace(mention_text.lower(), "").strip()

        if text in ("help", "/help"):
            await self._cmd_help(turn_context)
            return
        if text in ("confirm", "/confirm"):
            await self._cmd_confirm(turn_context)
            return
        if text in ("unlink", "/unlink"):
            await self._cmd_unlink(turn_context)
            return

        user = await self._resolve_user(turn_context)
        if user is None:
            return  # _resolve_user already told them what's wrong / what to do

        # Adaptive Card submissions come as message activities with a value payload
        if turn_context.activity.value:
            await self._handle_card_submit(turn_context, user)
            return

        if text in ("whoami", "/whoami"):
            await self._cmd_whoami(turn_context, user)
        elif text in ("apply", "/apply"):
            await self._cmd_apply(turn_context)
        elif text in ("aq", "/aq"):
            await self._cmd_aq(turn_context)
        elif text in ("prep", "/prep"):
            await self._cmd_prep(turn_context)
        elif text in ("tracker", "/tracker"):
            await self._cmd_tracker(turn_context, user)
        elif text.startswith(("track list", "/track-list", "track-list")):
            status_filter = text.split(maxsplit=2)[-1] if len(text.split()) > 2 else ""
            if status_filter in ("list", "track-list", "/track-list", "track"):
                status_filter = ""
            await self._cmd_track_list(turn_context, status_filter, user)
        elif text in ("track add", "/track-add", "track-add"):
            await self._cmd_track_add(turn_context)
        elif text.startswith(("track view", "/track-view", "track-view")):
            await self._cmd_track_view(turn_context, user)
        elif text in ("optimize", "/optimize"):
            await self._cmd_optimize(turn_context, user)
        elif text in ("runs", "/runs"):
            await self._cmd_runs(turn_context, user)
        else:
            await turn_context.send_activity(
                MessageFactory.text(
                    "I didn't recognise that command. Type **help** to see what I can do."
                )
            )

    # ── Identity resolution ──────────────────────────────────────────────

    @staticmethod
    def _aad_object_id(turn_context: TurnContext) -> str | None:
        from_prop = turn_context.activity.from_property
        return getattr(from_prop, "aad_object_id", None) if from_prop else None

    async def _teams_email(self, turn_context: TurnContext) -> str | None:
        """Look up the caller's email via the Teams roster API."""
        member_id = turn_context.activity.from_property.id
        member = await TeamsInfo.get_member(turn_context, member_id)
        return (member.email or member.user_principal_name or "").strip() or None

    async def _resolve_user(self, turn_context: TurnContext) -> dict | None:
        """Return {"email": ...} for a linked caller, or None after telling
        them why they can't proceed (no account, or needs to confirm)."""
        aad_object_id = self._aad_object_id(turn_context)
        if not aad_object_id:
            await turn_context.send_activity(MessageFactory.text(
                "❌ I can't verify your identity here — no Azure AD object id on this message."
            ))
            return None

        try:
            status = await asyncio.to_thread(api_client.teams_link_status, aad_object_id)
        except Exception as exc:
            await turn_context.send_activity(
                MessageFactory.text(f"❌ Could not check your account link: {exc}")
            )
            return None

        if status.get("linked"):
            return {"email": status["email"]}

        try:
            email = await self._teams_email(turn_context)
        except Exception as exc:
            await turn_context.send_activity(
                MessageFactory.text(f"❌ Could not look up your Teams profile: {exc}")
            )
            return None

        if not email:
            await turn_context.send_activity(MessageFactory.text(
                "❌ I couldn't find an email address on your Teams profile — "
                "I can't verify your account."
            ))
            return None

        try:
            lookup = await asyncio.to_thread(api_client.teams_account_lookup, email)
        except Exception as exc:
            await turn_context.send_activity(
                MessageFactory.text(f"❌ Error checking your account: {exc}")
            )
            return None

        if not lookup.get("exists"):
            await self._offer_manual_link(turn_context, aad_object_id, email)
            return None

        await turn_context.send_activity(MessageFactory.text(
            f"I found a Job Apply account for **{email}**. "
            f"Reply **confirm** to let me act on your behalf."
        ))
        return None

    async def _offer_manual_link(self, turn_context: TurnContext, aad_object_id: str, email: str):
        """Teams email has no matching account — offer a sign-in link so the
        user can associate an existing Job Apply account under a different
        email (password or Google), instead of dead-ending here."""
        try:
            token = await asyncio.to_thread(api_client.teams_link_token, aad_object_id, email)
        except Exception as exc:
            await turn_context.send_activity(MessageFactory.text(
                f"❌ I don't have a Job Apply account for {email}, "
                f"and couldn't generate a sign-in link ({exc})."
            ))
            return

        link_url = f"{api_client.Config.API_BASE}/teams-link.html?token={token}"
        card = HeroCard(
            text=(
                f"I don't have a Job Apply account for {email}. If you already have an "
                "account under a different email, sign in below to link it "
                "(this link expires in 15 minutes)."
            ),
            buttons=[CardAction(type="openUrl", title="Sign in to link account", value=link_url)],
        )
        await turn_context.send_activity(MessageFactory.attachment(CardFactory.hero_card(card)))

    async def _cmd_confirm(self, turn_context: TurnContext):
        aad_object_id = self._aad_object_id(turn_context)
        if not aad_object_id:
            await turn_context.send_activity(
                MessageFactory.text("❌ No Azure AD identity on this message.")
            )
            return

        try:
            email = await self._teams_email(turn_context)
        except Exception as exc:
            await turn_context.send_activity(
                MessageFactory.text(f"❌ Could not look up your Teams profile: {exc}")
            )
            return

        if not email:
            await turn_context.send_activity(
                MessageFactory.text("❌ No email address found on your Teams profile.")
            )
            return

        try:
            result = await asyncio.to_thread(api_client.teams_link_confirm, aad_object_id, email)
        except Exception as exc:
            await turn_context.send_activity(MessageFactory.text(f"❌ Error linking your account: {exc}"))
            return

        if not result.get("linked"):
            await self._offer_manual_link(turn_context, aad_object_id, email)
            return

        await turn_context.send_activity(MessageFactory.text(
            f"✅ Linked as **{result['email']}**. Send your command again."
        ))

    async def _cmd_whoami(self, turn_context: TurnContext, user: dict):
        try:
            profile = await asyncio.to_thread(api_client.get_profile, user_email=user["email"])
        except Exception as exc:
            await turn_context.send_activity(MessageFactory.text(
                f"You're linked as **{user['email']}**, but couldn't load full profile details: {exc}"
            ))
            return

        link = None
        aad_object_id = self._aad_object_id(turn_context)
        if aad_object_id:
            try:
                link = await asyncio.to_thread(api_client.teams_link_status, aad_object_id)
            except Exception:
                link = None

        email = profile.get("email", user["email"])
        display_name = profile.get("display_name") or email.split("@")[0]
        role = profile.get("role", "user")
        verified = profile.get("email_verified", True)
        has_resume = profile.get("has_resume", False)
        resume_filename = profile.get("resume_filename")
        has_profile_guide = bool((profile.get("profile_text") or "").strip())

        resume_value = "❌ Not uploaded"
        if has_resume:
            resume_value = f"✅ {resume_filename}" if resume_filename else "✅ Uploaded"

        facts = [
            {"title": "Name", "value": display_name},
            {"title": "Email", "value": email},
            {"title": "Role", "value": role.capitalize()},
            {"title": "Email Verified", "value": "✅ Yes" if verified else "❌ No"},
            {"title": "Master Resume", "value": resume_value},
            {"title": "Profile Guide", "value": "✅ Yes" if has_profile_guide else "❌ Not set"},
        ]
        if link and link.get("expires_at"):
            expires_str = datetime.fromtimestamp(link["expires_at"]).strftime("%Y-%m-%d")
            facts.append({"title": "Teams Link Expires", "value": expires_str})

        card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.5",
            "body": [
                {
                    "type": "TextBlock", "text": f"\U0001f464 {display_name}",
                    "size": "Large", "weight": "Bolder", "wrap": True,
                },
                {"type": "TextBlock", "text": "Linked Job Apply account", "isSubtle": True, "spacing": "None"},
                {"type": "FactSet", "facts": facts, "spacing": "Medium"},
            ],
        }
        await turn_context.send_activity(MessageFactory.attachment(_card_attachment(card)))

    async def _cmd_unlink(self, turn_context: TurnContext):
        aad_object_id = self._aad_object_id(turn_context)
        if not aad_object_id:
            await turn_context.send_activity(
                MessageFactory.text("❌ No Azure AD identity on this message.")
            )
            return
        try:
            await asyncio.to_thread(api_client.teams_unlink, aad_object_id)
        except Exception as exc:
            await turn_context.send_activity(MessageFactory.text(f"❌ Error unlinking: {exc}"))
            return
        await turn_context.send_activity(
            MessageFactory.text("✅ Unlinked. Message me again to re-link.")
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

    async def _cmd_optimize(self, ctx: TurnContext, user: dict):
        try:
            apps = await asyncio.to_thread(api_client.get_applications, user_email=user["email"])
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
            {"title": f"{a.get('company', '?')} — {a.get('role_title', '?')}", "value": a["id"]}
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

    async def _cmd_tracker(self, ctx: TurnContext, user: dict):
        try:
            apps = await asyncio.to_thread(api_client.get_applications, user_email=user["email"])
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Could not reach the tracker: {exc}"))
            return

        counts: dict[str, int] = {s: 0 for s in VALID_STATUSES}
        for a in apps:
            s = a.get("status", "")
            if s in counts:
                counts[s] += 1

        facts = [
            {"title": f"{STATUS_EMOJI[status]} {status}", "value": str(n)}
            for status in VALID_STATUSES
            if (n := counts[status])
        ]

        card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.5",
            "body": [
                {
                    "type": "TextBlock", "text": "\U0001f4ca Application Pipeline",
                    "size": "Large", "weight": "Bolder",
                },
                {
                    "type": "TextBlock", "text": f"{len(apps)} total",
                    "isSubtle": True, "spacing": "None",
                },
                {"type": "FactSet", "facts": facts, "spacing": "Medium"},
            ],
        }
        await ctx.send_activity(MessageFactory.attachment(_card_attachment(card)))

    async def _cmd_track_list(self, ctx: TurnContext, status_filter: str, user: dict):
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
            apps = await asyncio.to_thread(
                api_client.get_applications, status=resolved, user_email=user["email"]
            )
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

        rows = []
        for i, a in enumerate(shown):
            status = a.get("status", "?")
            rows.append({
                "type": "ColumnSet",
                "spacing": "Medium" if i else "Default",
                "separator": i > 0,
                "columns": [
                    {
                        "type": "Column", "width": "stretch",
                        "items": [
                            {"type": "TextBlock", "text": a.get("company", "?"), "weight": "Bolder", "wrap": True},
                            {
                                "type": "TextBlock", "text": a.get("role_title", "?"),
                                "isSubtle": True, "wrap": True, "spacing": "None", "size": "Small",
                            },
                        ],
                    },
                    {
                        "type": "Column", "width": "auto", "verticalContentAlignment": "Center",
                        "items": [
                            {
                                "type": "TextBlock", "text": f"{STATUS_EMOJI.get(status, '')} {status}".strip(),
                                "color": STATUS_COLOR.get(status, "Default"), "wrap": True, "size": "Small",
                            },
                        ],
                    },
                ],
            })

        title = f"\U0001f4cb Applications{' — ' + resolved if resolved else ''}"
        body: list[dict[str, Any]] = [
            {"type": "TextBlock", "text": title, "size": "Large", "weight": "Bolder"},
            {"type": "TextBlock", "text": f"{len(apps)} total", "isSubtle": True, "spacing": "None"},
            *rows,
        ]
        if len(apps) > 15:
            body.append({
                "type": "TextBlock", "text": f"…and {len(apps) - 15} more.",
                "isSubtle": True, "spacing": "Medium",
            })

        card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.5",
            "body": body,
        }
        await ctx.send_activity(MessageFactory.attachment(_card_attachment(card)))

    async def _cmd_track_view(self, ctx: TurnContext, user: dict):
        try:
            apps = await asyncio.to_thread(api_client.get_applications, user_email=user["email"])
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Error: {exc}"))
            return

        if not apps:
            await ctx.send_activity(MessageFactory.text("No applications found."))
            return

        choices = [
            {"title": f"{a.get('company', '?')} — {a.get('role_title', '?')}", "value": a["id"]}
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

    async def _cmd_runs(self, ctx: TurnContext, user: dict):
        try:
            runs = await asyncio.to_thread(api_client.get_agent_runs, user_email=user["email"])
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
        rows = []
        for i, r in enumerate(shown):
            type_label = TYPE_LABELS.get(r.get("type", ""), r.get("type", "?"))
            status_badge = STATUS_BADGES.get(r.get("status", ""), "")
            company = r.get("company", "")
            role = r.get("role", "")
            label = f"{company} — {role}" if company else r.get("id", "?")[:8]
            drive_url = r.get("gdrive_folder_url", "")

            row = {
                "type": "Container",
                "spacing": "Medium" if i else "Default",
                "separator": i > 0,
                "items": [
                    {"type": "TextBlock", "text": f"{status_badge} {type_label}", "weight": "Bolder", "wrap": True},
                    {
                        "type": "TextBlock", "text": label,
                        "isSubtle": True, "wrap": True, "spacing": "None", "size": "Small",
                    },
                ],
            }
            if drive_url:
                row["selectAction"] = {"type": "Action.OpenUrl", "url": drive_url}
            rows.append(row)

        body: list[dict[str, Any]] = [
            {"type": "TextBlock", "text": "\U0001f4c2 Recent Agent Runs", "size": "Large", "weight": "Bolder"},
            {"type": "TextBlock", "text": f"{len(runs)} total", "isSubtle": True, "spacing": "None"},
            *rows,
        ]
        if len(runs) > 15:
            body.append({
                "type": "TextBlock", "text": f"…and {len(runs) - 15} more.",
                "isSubtle": True, "spacing": "Medium",
            })

        card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.5",
            "body": body,
        }
        await ctx.send_activity(MessageFactory.attachment(_card_attachment(card)))

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
            "**\U0001f511 Account**\n"
            "- **confirm** — Link your Teams identity to a Job Apply account "
            "(offers a sign-in link if none matches your Teams email)\n"
            "- **whoami** — Show which account you're linked as\n"
            "- **unlink** — Remove your link\n\n"
            "**\U0001f527 Other**\n"
            "- **runs** — List recent Drive run folders\n"
            "- **help** — This message"
        )
        await ctx.send_activity(MessageFactory.text(text))

    # ── Card submission handler ──────────────────────────────────────────

    async def _handle_card_submit(self, ctx: TurnContext, user: dict):
        data = ctx.activity.value or {}
        action = data.get("action", "")

        if action == "apply_submit":
            await self._submit_apply(ctx, data, user)
        elif action == "prep_submit":
            await self._submit_prep(ctx, data, user)
        elif action == "aq_submit":
            await self._submit_aq(ctx, data, user)
        elif action == "track_add_submit":
            await self._submit_track_add(ctx, data, user)
        elif action == "optimize_submit":
            await self._submit_optimize(ctx, data, user)
        elif action == "track_view_submit":
            await self._submit_track_view(ctx, data, user)
        else:
            await ctx.send_activity(MessageFactory.text(f"Unknown action: {action}"))

    # ── Long-running agent submissions (threaded) ────────────────────────

    async def _submit_apply(self, ctx: TurnContext, data: dict, user: dict):
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
        user_email = user["email"]

        def _run():
            try:
                run_data = api_client.post_run(job_posting, company, role, contact, user_email=user_email)
                run_id = run_data["run_id"]
                status = api_client.poll_run(run_id, user_email=user_email)
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

    async def _submit_prep(self, ctx: TurnContext, data: dict, user: dict):
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
        user_email = user["email"]

        def _run():
            try:
                prep_data = api_client.post_prep(
                    job_posting, company, role, round_type, focus, interviewer, user_email=user_email
                )
                prep_id = prep_data["prep_id"]
                status = api_client.poll_prep(prep_id, user_email=user_email)
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

    async def _submit_aq(self, ctx: TurnContext, data: dict, user: dict):
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
        user_email = user["email"]

        def _run():
            try:
                aq_data = api_client.post_aq(
                    question, job_posting, company, role, tone, char_limit, user_email=user_email
                )
                aq_id = aq_data["aq_id"]
                status = api_client.poll_aq(aq_id, user_email=user_email)
            except Exception as exc:
                self._proactive_message(adapter, conv_ref, f"❌ Error: {exc}")
                return

            if status["status"] == "done":
                answer = status.get("answer", "(no answer returned)")
                card = {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.5",
                    "body": [
                        {"type": "TextBlock", "text": "✅ Answer Ready", "size": "Large", "weight": "Bolder"},
                        {
                            "type": "TextBlock", "text": f"{company} — {role}",
                            "isSubtle": True, "wrap": True, "spacing": "None",
                        },
                        {
                            "type": "Container", "spacing": "Medium", "separator": True,
                            "items": [
                                {"type": "TextBlock", "text": "Question", "weight": "Bolder", "size": "Small"},
                                {"type": "TextBlock", "text": question, "wrap": True},
                            ],
                        },
                        {
                            "type": "Container", "spacing": "Medium", "separator": True,
                            "items": [
                                {"type": "TextBlock", "text": "Answer", "weight": "Bolder", "size": "Small"},
                                {"type": "TextBlock", "text": answer, "wrap": True},
                            ],
                        },
                    ],
                }
                self._proactive_message(adapter, conv_ref, card=card)
            elif status["status"] == "timeout":
                self._proactive_message(adapter, conv_ref, "⚠️ Taking longer than expected.")
            else:
                self._proactive_message(
                    adapter, conv_ref, f"❌ Failed: {status.get('error', 'Unknown error')}"
                )

        threading.Thread(target=_run, daemon=True).start()

    async def _submit_optimize(self, ctx: TurnContext, data: dict, user: dict):
        app_id = (data.get("app_id") or "").strip()
        instruction = (data.get("instruction") or "").strip()
        optimize_resume = data.get("optimize_resume", "true") == "true"
        optimize_cover_letter = data.get("optimize_cover_letter", "true") == "true"

        if not app_id or not instruction:
            await ctx.send_activity(
                MessageFactory.text("❌ Application and optimization prompt are required.")
            )
            return

        user_email = user["email"]

        try:
            record = await asyncio.to_thread(api_client.get_application, app_id, user_email=user_email)
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
                    optimize_resume, optimize_cover_letter, user_email=user_email,
                )
                optimize_id = result["optimize_id"]
                status = api_client.poll_optimize(optimize_id, user_email=user_email)
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

    async def _submit_track_add(self, ctx: TurnContext, data: dict, user: dict):
        company = (data.get("company") or "").strip()
        role = (data.get("role") or "").strip()
        status_val = (data.get("status") or "Researching").strip()

        if not company or not role:
            await ctx.send_activity(MessageFactory.text("❌ Company and role are required."))
            return

        payload: dict[str, Any] = {
            "company": company,
            "role_title": role,
            "status": status_val,
        }
        for field in ("url", "location", "salary_range", "note"):
            val = (data.get(field) or "").strip()
            if val:
                payload[field] = val

        try:
            result = await asyncio.to_thread(
                api_client.create_application, payload, user_email=user["email"]
            )
            await ctx.send_activity(
                MessageFactory.text(
                    f"✅ Added **{company}** — {role} ({status_val})"
                )
            )
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Error: {exc}"))

    async def _submit_track_view(self, ctx: TurnContext, data: dict, user: dict):
        app_id = (data.get("app_id") or "").strip()
        if not app_id:
            await ctx.send_activity(MessageFactory.text("❌ No application selected."))
            return

        try:
            a = await asyncio.to_thread(api_client.get_application, app_id, user_email=user["email"])
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Error: {exc}"))
            return

        status = a.get("status", "?")
        emoji = STATUS_EMOJI.get(status, "")
        color = STATUS_COLOR.get(status, "Default")

        facts = []
        for field, label in [
            ("location", "Location"),
            ("salary_range", "Salary"),
            ("date_applied", "Applied"),
            ("source", "Source"),
            ("recruiter_name", "Recruiter"),
        ]:
            val = a.get(field)
            if val:
                facts.append({"title": label, "value": str(val)})

        body: list[dict[str, Any]] = [
            {
                "type": "ColumnSet",
                "columns": [
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {
                                "type": "TextBlock", "text": a.get("company", "?"),
                                "size": "Large", "weight": "Bolder", "wrap": True,
                            },
                            {
                                "type": "TextBlock", "text": a.get("role_title", "?"),
                                "size": "Medium", "isSubtle": True, "wrap": True, "spacing": "None",
                            },
                        ],
                    },
                    {
                        "type": "Column",
                        "width": "auto",
                        "verticalContentAlignment": "Center",
                        "items": [
                            {
                                "type": "TextBlock", "text": f"{emoji} {status}".strip(),
                                "weight": "Bolder", "color": color, "wrap": True,
                            },
                        ],
                    },
                ],
            },
        ]
        if facts:
            body.append({"type": "FactSet", "facts": facts, "spacing": "Medium"})

        comments = a.get("comments", [])
        if comments:
            body.append({
                "type": "Container",
                "spacing": "Medium",
                "separator": True,
                "items": [
                    {"type": "TextBlock", "text": "Notes", "weight": "Bolder"},
                    *[
                        {"type": "TextBlock", "text": f"• {c.get('text', '')}", "wrap": True}
                        for c in comments[-5:]
                    ],
                ],
            })

        card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.5",
            "body": body,
        }
        url = a.get("url")
        if url:
            card["actions"] = [{"type": "Action.OpenUrl", "title": "Open Job Posting", "url": url}]

        await ctx.send_activity(MessageFactory.attachment(_card_attachment(card)))

    # ── Proactive messaging helper ───────────────────────────────────────

    @staticmethod
    def _proactive_message(adapter, conv_ref: ConversationReference, text: str = "", card: dict | None = None):
        import asyncio

        async def _send(tc: TurnContext):
            if card:
                await tc.send_activity(MessageFactory.attachment(_card_attachment(card)))
            else:
                await tc.send_activity(MessageFactory.text(text))

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(adapter.continue_conversation(conv_ref, _send, None))
        finally:
            loop.close()
