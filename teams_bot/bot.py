"""
Teams bot activity handler — maps user messages and Adaptive Card submissions
to the same FastAPI backend the Slack bot uses.

Commands (type in chat):
  apply        — generate resume + ATS resume + cover letter
  aq           — answer an application question
  prep         — generate interview prep doc
  thankyou     — generate a post-interview thank-you email
  optimize     — refine an existing run's documents from a prompt
  rescore      — re-score resume/JD match for an application
  tracker      — pipeline summary
  track list   — list applications (optionally filter by status)
  track add    — add a new application
  track view   — view application details
  track update — edit an application's status/fields (two-step: pick app -> edit form)
  track note   — add a comment to an application
  track delete — delete an application (two-step confirm)
  runs         — list recent Drive run folders
  confirm      — link your Teams identity to a Job Apply account
  whoami       — show which account you're linked as
  unlink       — remove your Teams identity's link
  help         — command reference

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

apply/prep/aq only operate on a tracked application — there's no free-text
company/role entry. Each form's "Application" field is an Adaptive Card
dynamic typeahead (dataset "myApplications", handled in _search_my_applications)
searching the caller's own applications. Teams' application/search response
schema only supports {title, value} per result — no icon/image field — so the
company logo can't appear in the dropdown itself; it shows once an item is
picked, on whichever card the selection lands on (see _logo_column).
Submitting that first step (_submit_*_select) looks up a saved
job_description.md in the application's most recent linked Drive folder
(_resolve_app_and_jd); if one exists, the run starts immediately,
otherwise a follow-up card asks the user to paste the JD (_submit_*_final).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from botbuilder.core import ActivityHandler, CardFactory, InvokeResponse, MessageFactory, TurnContext
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


def _logo_url(domain: str, size: int = 18) -> str:
    """Direct Logo.dev CDN URL for a search-result icon — built straight from
    domain rather than a server-computed logo_url field, since neither
    frontend/*.html nor this bot need anything beyond the pk_ public key."""
    if not domain:
        return ""
    return (
        f"https://img.logo.dev/{domain}?token={api_client.Config.LOGODEV_PUBLIC_KEY}"
        f"&size={size}&format=webp&retina=true"
    )


def _logo_column(domain: str, size: int = 32) -> dict | None:
    """An auto-width Adaptive Card Column holding just the company logo, for
    prepending to a ColumnSet row — None (add nothing) if there's no domain."""
    icon = _logo_url(domain, size=size)
    if not icon:
        return None
    return {
        "type": "Column", "width": "auto", "verticalContentAlignment": "Center",
        "items": [{"type": "Image", "url": icon, "width": f"{size}px", "height": f"{size}px"}],
    }


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

    async def on_invoke_activity(self, turn_context: TurnContext):
        # Adaptive Card dynamic typeahead search (track_add_form's "company"
        # field, and the "app_id" field on apply/prep/aq) — not covered by
        # the base SDK's invoke dispatch, so it's intercepted here rather
        # than via an on_teams_* override.
        if turn_context.activity.name == "application/search":
            return await self._handle_dynamic_search(turn_context)
        return await super().on_invoke_activity(turn_context)

    async def _handle_dynamic_search(self, turn_context: TurnContext):
        value = turn_context.activity.value or {}
        dataset = value.get("dataset", "")
        query = (value.get("queryText") or "").strip()

        if dataset == "myApplications":
            results = await self._search_my_applications(turn_context, query)
        else:
            results = await self._search_companies(query)

        # Not using self._create_invoke_response() here — it runs the body through
        # the SDK's msrest serializer, which expects a typed Model, not a plain dict.
        return InvokeResponse(
            status=200,
            body={
                "type": "application/vnd.microsoft.search.searchResponse",
                "value": {"results": results},
            },
        )

    @staticmethod
    async def _search_companies(query: str) -> list[dict]:
        if len(query) < 2:
            return []
        try:
            companies = await asyncio.to_thread(api_client.search_companies, query)
        except Exception:
            return []
        results = []
        for c in companies[:8]:
            name = c.get("name", "?")
            domain = c.get("domain", "")
            title = f"{name} ({domain})" if domain else name
            # No icon here: Teams' application/search response schema only
            # supports {title, value} per result — no image field exists in
            # the documented contract, so a logo can't show in the dropdown
            # itself (it does show once an item is picked — see _logo_column).
            results.append({"title": title[:75], "value": f"{name}|||{domain}"[:250]})
        return results

    async def _search_my_applications(self, turn_context: TurnContext, query: str) -> list[dict]:
        """Search the calling user's own tracked applications by company/role
        substring — backs the app_id typeahead on apply/prep/aq forms."""
        aad_object_id = self._aad_object_id(turn_context)
        if not aad_object_id:
            return []
        try:
            link = await asyncio.to_thread(api_client.teams_link_status, aad_object_id)
        except Exception:
            return []
        if not link.get("linked"):
            return []

        try:
            apps = await asyncio.to_thread(api_client.get_applications, user_email=link["email"])
        except Exception:
            return []

        q = query.lower()
        matches = [
            a for a in apps
            if not q or q in a.get("company", "").lower() or q in a.get("role_title", "").lower()
        ]
        results = []
        for a in matches[:8]:
            title = f"{a.get('company', '?')} | {a.get('role_title', '?')}"
            # See _search_companies — no icon field in the dynamic search
            # response schema, so the logo can't appear in the dropdown itself.
            results.append({"title": title[:75], "value": a["id"]})
        return results

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
            await self._cmd_apply(turn_context, user)
        elif text in ("aq", "/aq"):
            await self._cmd_aq(turn_context, user)
        elif text in ("prep", "/prep"):
            await self._cmd_prep(turn_context, user)
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
        elif text.startswith(("track update", "/track-update", "track-update")):
            await self._cmd_track_update(turn_context, user)
        elif text.startswith(("track note", "/track-note", "track-note")):
            await self._cmd_track_note(turn_context, user)
        elif text.startswith(("track delete", "/track-delete", "track-delete")):
            await self._cmd_track_delete(turn_context, user)
        elif text in ("optimize", "/optimize"):
            await self._cmd_optimize(turn_context, user)
        elif text in ("thankyou", "/thankyou", "thank you", "thank-you"):
            await self._cmd_thankyou(turn_context, user)
        elif text in ("rescore", "/rescore"):
            await self._cmd_rescore(turn_context, user)
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

    async def _require_any_application(self, ctx: TurnContext, user: dict) -> bool:
        """Agent commands only operate on a tracked application — no more
        free-text company/role. Confirms at least one exists first so the
        error is a clear message instead of an empty search box."""
        try:
            apps = await asyncio.to_thread(api_client.get_applications, user_email=user["email"])
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Error loading applications: {exc}"))
            return False
        if not apps:
            await ctx.send_activity(
                MessageFactory.text("❌ No applications on file yet. Add one with **track add** first.")
            )
            return False
        return True

    async def _cmd_apply(self, ctx: TurnContext, user: dict):
        if not await self._require_any_application(ctx, user):
            return
        card = _load_card("apply_form")
        await ctx.send_activity(
            MessageFactory.attachment(_card_attachment(card))
        )

    async def _cmd_aq(self, ctx: TurnContext, user: dict):
        if not await self._require_any_application(ctx, user):
            return
        card = _load_card("aq_form")
        await ctx.send_activity(
            MessageFactory.attachment(_card_attachment(card))
        )

    async def _cmd_prep(self, ctx: TurnContext, user: dict):
        if not await self._require_any_application(ctx, user):
            return
        card = _load_card("prep_form")
        await ctx.send_activity(
            MessageFactory.attachment(_card_attachment(card))
        )

    async def _cmd_track_add(self, ctx: TurnContext):
        card = _load_card("track_add_form")
        await ctx.send_activity(
            MessageFactory.attachment(_card_attachment(card))
        )

    async def _cmd_thankyou(self, ctx: TurnContext, user: dict):
        if not await self._require_any_application(ctx, user):
            return
        card = _load_card("thankyou_form")
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

    async def _cmd_rescore(self, ctx: TurnContext, user: dict):
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

        card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.5",
            "body": [
                {"type": "TextBlock", "text": "Rescore Match", "size": "Large", "weight": "Bolder"},
                {
                    "type": "TextBlock", "wrap": True, "isSubtle": True, "spacing": "None",
                    "text": "Re-score how well your resume matches this application's job posting. "
                            "Requires a linked job description.",
                },
                {
                    "type": "Input.ChoiceSet", "id": "app_id", "label": "Application",
                    "isRequired": True, "errorMessage": "Select an application",
                    "choices": choices,
                },
            ],
            "actions": [
                {"type": "Action.Submit", "title": "Rescore", "data": {"action": "rescore_submit"}},
            ],
        }
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
            columns = []
            logo_col = _logo_column(a.get("domain", ""), size=28)
            if logo_col:
                columns.append(logo_col)
            columns.append({
                "type": "Column", "width": "stretch",
                "items": [
                    {"type": "TextBlock", "text": a.get("company", "?"), "weight": "Bolder", "wrap": True},
                    {
                        "type": "TextBlock", "text": a.get("role_title", "?"),
                        "isSubtle": True, "wrap": True, "spacing": "None", "size": "Small",
                    },
                ],
            })
            columns.append({
                "type": "Column", "width": "auto", "verticalContentAlignment": "Center",
                "items": [
                    {
                        "type": "TextBlock", "text": f"{STATUS_EMOJI.get(status, '')} {status}".strip(),
                        "color": STATUS_COLOR.get(status, "Default"), "wrap": True, "size": "Small",
                    },
                ],
            })
            rows.append({
                "type": "ColumnSet",
                "spacing": "Medium" if i else "Default",
                "separator": i > 0,
                "columns": columns,
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

    async def _cmd_track_update(self, ctx: TurnContext, user: dict):
        try:
            apps = await asyncio.to_thread(api_client.get_applications, user_email=user["email"])
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Error: {exc}"))
            return

        if not apps:
            await ctx.send_activity(
                MessageFactory.text("No applications found. Add one with **track add** first.")
            )
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
                {"type": "TextBlock", "text": "Update Application", "size": "Large", "weight": "Bolder"},
                {
                    "type": "Input.ChoiceSet",
                    "id": "app_id",
                    "label": "Select application to edit",
                    "isRequired": True,
                    "choices": choices,
                },
            ],
            "actions": [
                {"type": "Action.Submit", "title": "Continue", "data": {"action": "track_update_select_submit"}},
            ],
        }
        await ctx.send_activity(MessageFactory.attachment(_card_attachment(card)))

    async def _cmd_track_note(self, ctx: TurnContext, user: dict):
        try:
            apps = await asyncio.to_thread(api_client.get_applications, user_email=user["email"])
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Error: {exc}"))
            return

        if not apps:
            await ctx.send_activity(
                MessageFactory.text("No applications found. Add one with **track add** first.")
            )
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
                {"type": "TextBlock", "text": "Add Note", "size": "Large", "weight": "Bolder"},
                {
                    "type": "Input.ChoiceSet",
                    "id": "app_id",
                    "label": "Application",
                    "isRequired": True,
                    "choices": choices,
                },
                {
                    "type": "Input.Text",
                    "id": "note",
                    "label": "Note",
                    "isMultiline": True,
                    "isRequired": True,
                    "errorMessage": "Note is required",
                    "placeholder": "e.g. Spoke with recruiter — next step is HM interview",
                },
            ],
            "actions": [
                {"type": "Action.Submit", "title": "Add Note", "data": {"action": "track_note_submit"}},
            ],
        }
        await ctx.send_activity(MessageFactory.attachment(_card_attachment(card)))

    async def _cmd_track_delete(self, ctx: TurnContext, user: dict):
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
                {"type": "TextBlock", "text": "Delete Application", "size": "Large", "weight": "Bolder"},
                {
                    "type": "TextBlock", "wrap": True, "color": "Attention",
                    "text": "⚠️ This will permanently delete the record and all its comments.",
                },
                {
                    "type": "Input.ChoiceSet",
                    "id": "app_id",
                    "label": "Application to delete",
                    "isRequired": True,
                    "choices": choices,
                },
            ],
            "actions": [
                {"type": "Action.Submit", "title": "Continue", "data": {"action": "track_delete_select_submit"}},
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

        # Agent run records don't carry domain themselves — one extra call to
        # index the user's applications by id, so each run row can show a logo.
        try:
            apps = await asyncio.to_thread(api_client.get_applications, user_email=user["email"])
        except Exception:
            apps = []
        domain_by_app_id = {a["id"]: a.get("domain", "") for a in apps}

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

            columns = []
            logo_col = _logo_column(domain_by_app_id.get(r.get("app_id", ""), ""), size=28)
            if logo_col:
                columns.append(logo_col)
            columns.append({
                "type": "Column", "width": "stretch",
                "items": [
                    {"type": "TextBlock", "text": f"{status_badge} {type_label}", "weight": "Bolder", "wrap": True},
                    {
                        "type": "TextBlock", "text": label,
                        "isSubtle": True, "wrap": True, "spacing": "None", "size": "Small",
                    },
                ],
            })
            row = {
                "type": "ColumnSet",
                "spacing": "Medium" if i else "Default",
                "separator": i > 0,
                "columns": columns,
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
            "**\U0001f916 Agent Commands** _(pick from your tracked applications — "
            "add one with **track add** first if you don't have any yet)_\n"
            "- **apply** — Generate resume + ATS resume + cover letter\n"
            "- **aq** — Answer an application question\n"
            "- **prep** — Generate interview prep doc\n"
            "- **thankyou** — Generate a post-interview thank-you email\n"
            "- **optimize** — Refine existing run documents\n"
            "- **rescore** — Re-score resume/JD match for an application\n\n"
            "**\U0001f4cb Tracker Commands**\n"
            "- **tracker** — Pipeline summary\n"
            "- **track list** [status] — List applications\n"
            "- **track add** — Add a new application\n"
            "- **track view** — View application details\n"
            "- **track update** — Edit an application's status/fields\n"
            "- **track note** — Add a comment to an application\n"
            "- **track delete** — Delete an application (two-step confirm)\n\n"
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
            # Kept for any apply_form card already open in a chat from before
            # the app-select flow shipped — see _submit_apply_select/_final.
            await self._submit_apply(ctx, data, user)
        elif action == "apply_select_submit":
            await self._submit_apply_select(ctx, data, user)
        elif action == "apply_final_submit":
            await self._submit_apply_final(ctx, data, user)
        elif action == "prep_submit":
            await self._submit_prep(ctx, data, user)
        elif action == "prep_select_submit":
            await self._submit_prep_select(ctx, data, user)
        elif action == "prep_final_submit":
            await self._submit_prep_final(ctx, data, user)
        elif action == "aq_submit":
            await self._submit_aq(ctx, data, user)
        elif action == "aq_select_submit":
            await self._submit_aq_select(ctx, data, user)
        elif action == "aq_final_submit":
            await self._submit_aq_final(ctx, data, user)
        elif action == "track_add_submit":
            await self._submit_track_add(ctx, data, user)
        elif action == "optimize_submit":
            await self._submit_optimize(ctx, data, user)
        elif action == "track_view_submit":
            await self._submit_track_view(ctx, data, user)
        elif action == "thankyou_select_submit":
            await self._submit_thankyou_select(ctx, data, user)
        elif action == "thankyou_final_submit":
            await self._submit_thankyou_final(ctx, data, user)
        elif action == "track_update_select_submit":
            await self._submit_track_update_select(ctx, data, user)
        elif action == "track_update_edit_submit":
            await self._submit_track_update_edit(ctx, data, user)
        elif action == "track_note_submit":
            await self._submit_track_note(ctx, data, user)
        elif action == "track_delete_select_submit":
            await self._submit_track_delete_select(ctx, data, user)
        elif action == "track_delete_confirm_submit":
            await self._submit_track_delete_confirm(ctx, data, user)
        elif action == "track_delete_cancel_submit":
            await ctx.send_activity(MessageFactory.text("Cancelled — nothing was deleted."))
        elif action == "rescore_submit":
            await self._submit_rescore(ctx, data, user)
        else:
            await ctx.send_activity(MessageFactory.text(f"Unknown action: {action}"))

    # ── Application selection + JD lookup (apply/prep/aq/thankyou share this) ─



    async def _resolve_app_and_jd(self, app_id: str, user: dict) -> tuple[dict, str | None]:
        """Return (application record, saved job posting text or None).

        None means no job_description.md was found in the application's
        most recently linked Drive folder (or it has no linked folder at
        all yet) — the caller should ask the user to paste one instead.
        """
        app = await asyncio.to_thread(api_client.get_application, app_id, user_email=user["email"])
        runs = [r for r in (app.get("linked_runs") or []) if r.get("gdrive_folder_id")]
        if not runs:
            return app, None
        runs.sort(key=lambda r: r.get("linked_at", ""), reverse=True)
        folder_id = runs[0]["gdrive_folder_id"]
        try:
            job_posting = await asyncio.to_thread(
                api_client.get_job_posting, folder_id, user_email=user["email"]
            )
        except Exception:
            job_posting = None
        return app, job_posting

    @staticmethod
    def _jd_paste_card(action: str, extra_data: dict) -> dict:
        """Follow-up card asking for the JD text, carrying forward everything
        already collected in the first step via the submit action's data —
        Action.Submit's static data merges with this card's one input."""
        return {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.5",
            "body": [
                {"type": "TextBlock", "text": "Paste the Job Description", "size": "Large", "weight": "Bolder"},
                {
                    "type": "TextBlock", "wrap": True, "isSubtle": True, "spacing": "None",
                    "text": "I couldn't find a saved job description for this application yet — paste it below.",
                },
                {
                    "type": "Input.Text", "id": "job_posting", "label": "Job posting",
                    "isMultiline": True, "isRequired": True, "errorMessage": "Job posting is required",
                },
            ],
            "actions": [
                {"type": "Action.Submit", "title": "Generate", "data": {"action": action, **extra_data}},
            ],
        }

    async def _submit_apply_select(self, ctx: TurnContext, data: dict, user: dict):
        app_id = (data.get("app_id") or "").strip()
        contact = (data.get("contact") or "").strip()
        if not app_id:
            await ctx.send_activity(MessageFactory.text("❌ Please select an application."))
            return

        try:
            app, job_posting = await self._resolve_app_and_jd(app_id, user)
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Could not load application: {exc}"))
            return

        company = app.get("company", "?")
        role = app.get("role_title", "?")
        if job_posting:
            await self._submit_apply(
                ctx, {"company": company, "role": role, "contact": contact, "job_posting": job_posting}, user,
            )
            return

        card = self._jd_paste_card("apply_final_submit", {
            "app_id": app_id, "company": company, "role": role, "contact": contact,
        })
        await ctx.send_activity(MessageFactory.attachment(_card_attachment(card)))

    async def _submit_apply_final(self, ctx: TurnContext, data: dict, user: dict):
        job_posting = (data.get("job_posting") or "").strip()
        if not job_posting:
            await ctx.send_activity(MessageFactory.text("❌ Job posting is required."))
            return
        await self._submit_apply(ctx, {
            "company": data.get("company", ""), "role": data.get("role", ""),
            "contact": data.get("contact", ""), "job_posting": job_posting,
        }, user)

    async def _submit_prep_select(self, ctx: TurnContext, data: dict, user: dict):
        app_id = (data.get("app_id") or "").strip()
        round_type = (data.get("round_type") or "").strip()
        interviewer = (data.get("interviewer") or "").strip()
        focus = (data.get("focus") or "").strip()
        if not app_id or not round_type:
            await ctx.send_activity(MessageFactory.text("❌ Application and interview round are required."))
            return

        try:
            app, job_posting = await self._resolve_app_and_jd(app_id, user)
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Could not load application: {exc}"))
            return

        company = app.get("company", "?")
        role = app.get("role_title", "?")
        if job_posting:
            await self._submit_prep(ctx, {
                "company": company, "role": role, "round_type": round_type,
                "interviewer": interviewer, "focus": focus, "job_posting": job_posting,
            }, user)
            return

        card = self._jd_paste_card("prep_final_submit", {
            "app_id": app_id, "company": company, "role": role,
            "round_type": round_type, "interviewer": interviewer, "focus": focus,
        })
        await ctx.send_activity(MessageFactory.attachment(_card_attachment(card)))

    async def _submit_prep_final(self, ctx: TurnContext, data: dict, user: dict):
        job_posting = (data.get("job_posting") or "").strip()
        if not job_posting:
            await ctx.send_activity(MessageFactory.text("❌ Job posting is required."))
            return
        await self._submit_prep(ctx, {
            "company": data.get("company", ""), "role": data.get("role", ""),
            "round_type": data.get("round_type", ""), "interviewer": data.get("interviewer", ""),
            "focus": data.get("focus", ""), "job_posting": job_posting,
        }, user)

    async def _submit_aq_select(self, ctx: TurnContext, data: dict, user: dict):
        app_id = (data.get("app_id") or "").strip()
        question = (data.get("question") or "").strip()
        tone = (data.get("tone") or "professional").strip()
        char_limit = data.get("char_limit")
        if not app_id or not question:
            await ctx.send_activity(MessageFactory.text("❌ Application and question are required."))
            return

        try:
            app, job_posting = await self._resolve_app_and_jd(app_id, user)
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Could not load application: {exc}"))
            return

        company = app.get("company", "?")
        role = app.get("role_title", "?")
        domain = app.get("domain", "")
        if job_posting:
            await self._submit_aq(ctx, {
                "company": company, "role": role, "domain": domain, "question": question,
                "tone": tone, "char_limit": char_limit, "job_posting": job_posting,
            }, user)
            return

        card = self._jd_paste_card("aq_final_submit", {
            "app_id": app_id, "company": company, "role": role, "domain": domain,
            "question": question, "tone": tone, "char_limit": char_limit,
        })
        await ctx.send_activity(MessageFactory.attachment(_card_attachment(card)))

    async def _submit_aq_final(self, ctx: TurnContext, data: dict, user: dict):
        job_posting = (data.get("job_posting") or "").strip()
        if not job_posting:
            await ctx.send_activity(MessageFactory.text("❌ Job posting is required."))
            return
        await self._submit_aq(ctx, {
            "company": data.get("company", ""), "role": data.get("role", ""),
            "domain": data.get("domain", ""),
            "question": data.get("question", ""), "tone": data.get("tone", "professional"),
            "char_limit": data.get("char_limit"), "job_posting": job_posting,
        }, user)

    async def _submit_thankyou_select(self, ctx: TurnContext, data: dict, user: dict):
        app_id      = (data.get("app_id") or "").strip()
        round_type  = (data.get("round_type") or "").strip()
        tone        = (data.get("tone") or "professional").strip()
        interviewer = (data.get("interviewer") or "").strip()
        topics      = (data.get("topics") or "").strip()
        if not app_id or not round_type:
            await ctx.send_activity(MessageFactory.text("❌ Application and interview round are required."))
            return

        try:
            app, job_posting = await self._resolve_app_and_jd(app_id, user)
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Could not load application: {exc}"))
            return

        company = app.get("company", "?")
        role    = app.get("role_title", "?")
        domain  = app.get("domain", "")
        if job_posting:
            await self._submit_thankyou(ctx, {
                "app_id": app_id, "company": company, "role": role, "domain": domain,
                "round_type": round_type, "tone": tone, "interviewer": interviewer,
                "topics": topics, "job_posting": job_posting,
            }, user)
            return

        card = self._jd_paste_card("thankyou_final_submit", {
            "app_id": app_id, "company": company, "role": role, "domain": domain,
            "round_type": round_type, "tone": tone, "interviewer": interviewer, "topics": topics,
        })
        await ctx.send_activity(MessageFactory.attachment(_card_attachment(card)))

    async def _submit_thankyou_final(self, ctx: TurnContext, data: dict, user: dict):
        job_posting = (data.get("job_posting") or "").strip()
        if not job_posting:
            await ctx.send_activity(MessageFactory.text("❌ Job posting is required."))
            return
        await self._submit_thankyou(ctx, {
            "app_id": data.get("app_id", ""), "company": data.get("company", ""),
            "role": data.get("role", ""), "domain": data.get("domain", ""),
            "round_type": data.get("round_type", ""), "tone": data.get("tone", "professional"),
            "interviewer": data.get("interviewer", ""), "topics": data.get("topics", ""),
            "job_posting": job_posting,
        }, user)

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
        domain = (data.get("domain") or "").strip()
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
                subtitle_text = {
                    "type": "TextBlock", "text": f"{company} — {role}",
                    "isSubtle": True, "wrap": True, "spacing": "None",
                }
                logo_col = _logo_column(domain, size=28)
                subtitle: dict[str, Any] = (
                    {
                        "type": "ColumnSet", "spacing": "None",
                        "columns": [logo_col, {
                            "type": "Column", "width": "stretch",
                            "verticalContentAlignment": "Center", "items": [subtitle_text],
                        }],
                    }
                    if logo_col else subtitle_text
                )
                card = {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.5",
                    "body": [
                        {"type": "TextBlock", "text": "✅ Answer Ready", "size": "Large", "weight": "Bolder"},
                        subtitle,
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

    async def _submit_thankyou(self, ctx: TurnContext, data: dict, user: dict):
        app_id = (data.get("app_id") or "").strip()
        company = (data.get("company") or "").strip()
        role = (data.get("role") or "").strip()
        round_type = (data.get("round_type") or "").strip()
        tone = (data.get("tone") or "professional").strip()
        interviewer = (data.get("interviewer") or "").strip()
        topics = (data.get("topics") or "").strip()
        job_posting = (data.get("job_posting") or "").strip()

        if not company or not role or not round_type or not job_posting:
            await ctx.send_activity(
                MessageFactory.text("❌ Company, role, interview round, and job posting are required.")
            )
            return

        await ctx.send_activity(
            MessageFactory.text(f"⏳ Generating thank-you email for **{role}** at **{company}** ({round_type})…")
        )

        conv_ref = TurnContext.get_conversation_reference(ctx.activity)
        adapter = ctx.adapter
        user_email = user["email"]

        def _run():
            try:
                ty_data = api_client.post_thankyou(
                    job_posting, company, role, round_type, interviewer, topics, tone,
                    app_id=app_id or None, user_email=user_email,
                )
                ty_id = ty_data["ty_id"]
                status = api_client.poll_thankyou(ty_id, user_email=user_email)
            except Exception as exc:
                self._proactive_message(adapter, conv_ref, f"❌ Error: {exc}")
                return

            if status["status"] == "done":
                self._proactive_message(
                    adapter, conv_ref,
                    f"✅ **Thank-you email ready** for {role} @ {company} ({round_type})\n\n"
                    f"Your email and DOCX are in Google Drive.",
                )
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

    async def _submit_rescore(self, ctx: TurnContext, data: dict, user: dict):
        app_id = (data.get("app_id") or "").strip()
        if not app_id:
            await ctx.send_activity(MessageFactory.text("❌ Please select an application."))
            return

        user_email = user["email"]
        try:
            record = await asyncio.to_thread(api_client.get_application, app_id, user_email=user_email)
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Could not load application: {exc}"))
            return

        company = record.get("company", "?")
        role = record.get("role_title", "?")
        domain = record.get("domain", "")

        await ctx.send_activity(MessageFactory.text(f"⏳ Scoring **{role}** @ **{company}**…"))

        conv_ref = TurnContext.get_conversation_reference(ctx.activity)
        adapter = ctx.adapter

        def _run():
            try:
                result = api_client.score_application(app_id, user_email=user_email)
            except Exception as exc:
                detail = str(exc)
                response = getattr(exc, "response", None)
                if response is not None:
                    try:
                        detail = response.json().get("detail", detail)
                    except Exception:
                        pass
                self._proactive_message(adapter, conv_ref, f"❌ Rescore failed: {detail}")
                return

            score = result.get("score", "?")
            category = str(result.get("category", "?"))
            rationale = result.get("rationale", "")
            emoji = "\U0001f7e2" if category == "strong" else ("\U0001f7e1" if category == "good" else "\U0001f534")

            subtitle_text = {
                "type": "TextBlock", "text": f"{company} — {role}",
                "isSubtle": True, "wrap": True, "spacing": "None",
            }
            logo_col = _logo_column(domain, size=28)
            subtitle: dict[str, Any] = (
                {
                    "type": "ColumnSet", "spacing": "None",
                    "columns": [logo_col, {
                        "type": "Column", "width": "stretch",
                        "verticalContentAlignment": "Center", "items": [subtitle_text],
                    }],
                }
                if logo_col else subtitle_text
            )

            body: list[dict[str, Any]] = [
                {"type": "TextBlock", "text": f"{emoji} Match Score", "size": "Large", "weight": "Bolder"},
                subtitle,
                {
                    "type": "FactSet", "spacing": "Medium",
                    "facts": [
                        {"title": "Score", "value": f"{score}/100"},
                        {"title": "Category", "value": category.capitalize()},
                    ],
                },
            ]
            if rationale:
                body.append({"type": "TextBlock", "text": rationale, "wrap": True, "spacing": "Medium", "isSubtle": True})

            card = {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.5",
                "body": body,
            }
            self._proactive_message(adapter, conv_ref, card=card)

        threading.Thread(target=_run, daemon=True).start()

    # ── Instant card submissions ─────────────────────────────────────────

    async def _submit_track_add(self, ctx: TurnContext, data: dict, user: dict):
        # Company comes from the "company" typeahead (see _handle_company_search):
        # its value is "Name|||domain" when a search result was picked, or just
        # freeform text if the user typed something the search never matched.
        company_raw = (data.get("company") or "").strip()
        if "|||" in company_raw:
            company, domain = company_raw.split("|||", 1)
        else:
            company, domain = company_raw, ""

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
        if domain:
            payload["domain"] = domain
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

        header_columns = []
        logo_col = _logo_column(a.get("domain", ""), size=40)
        if logo_col:
            header_columns.append(logo_col)
        header_columns.append({
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
        })

        body: list[dict[str, Any]] = [
            {
                "type": "ColumnSet",
                "columns": [
                    *header_columns,
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

    async def _submit_track_update_select(self, ctx: TurnContext, data: dict, user: dict):
        """Step 1 of track update -> push a full edit form pre-filled with
        the current record's values, mirroring slack_bot.py's modal-push."""
        app_id = (data.get("app_id") or "").strip()
        if not app_id:
            await ctx.send_activity(MessageFactory.text("❌ Please select an application."))
            return

        try:
            a = await asyncio.to_thread(api_client.get_application, app_id, user_email=user["email"])
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Could not load application: {exc}"))
            return

        status_choices = [{"title": s, "value": s} for s in VALID_STATUSES]
        date_field: dict[str, Any] = {
            "type": "Input.Date", "id": "date_applied", "label": "Date Applied (optional)",
        }
        if a.get("date_applied"):
            date_field["value"] = a["date_applied"][:10]

        card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.5",
            "body": [
                {
                    "type": "TextBlock", "wrap": True, "size": "Large", "weight": "Bolder",
                    "text": f"Editing {a.get('company', '?')} — {a.get('role_title', '?')}",
                },
                {
                    "type": "Input.ChoiceSet", "id": "status", "label": "Status", "isRequired": True,
                    "errorMessage": "Status is required",
                    "value": a.get("status", "Researching"), "choices": status_choices,
                },
                date_field,
                {
                    "type": "Input.Text", "id": "job_source", "label": "Job Source (optional)",
                    "value": a.get("job_source", ""), "placeholder": "LinkedIn, Indeed, Referral…",
                },
                {
                    "type": "Input.Text", "id": "location", "label": "Location / Remote (optional)",
                    "value": a.get("location", ""), "placeholder": "Remote, Boston, Hybrid…",
                },
                {
                    "type": "Input.Text", "id": "salary_range", "label": "Salary Range (optional)",
                    "value": a.get("salary_range", ""), "placeholder": "e.g. $130k – $160k",
                },
                {
                    "type": "Input.Text", "id": "url", "label": "Job Posting URL (optional)",
                    "value": a.get("url", ""), "placeholder": "https://…",
                },
                {
                    "type": "Input.Toggle", "id": "dua", "title": "Reported to DUA (unemployment)",
                    "value": "true" if a.get("dua") else "false",
                },
                {
                    "type": "Input.Text", "id": "recruiter_name", "label": "Recruiter Name (optional)",
                    "value": a.get("recruiter_name", ""), "placeholder": "Jane Smith",
                },
                {
                    "type": "Input.Text", "id": "recruiter_email", "label": "Recruiter Email (optional)",
                    "value": a.get("recruiter_email", ""), "placeholder": "jane@company.com",
                },
                {
                    "type": "Input.Text", "id": "note", "label": "Add a note (optional)",
                    "isMultiline": True, "placeholder": "e.g. Got a callback from recruiter",
                },
            ],
            "actions": [
                {
                    "type": "Action.Submit", "title": "Save",
                    "data": {"action": "track_update_edit_submit", "app_id": app_id},
                },
            ],
        }
        await ctx.send_activity(MessageFactory.attachment(_card_attachment(card)))

    async def _submit_track_update_edit(self, ctx: TurnContext, data: dict, user: dict):
        app_id = (data.get("app_id") or "").strip()
        if not app_id:
            await ctx.send_activity(MessageFactory.text("❌ Missing application reference."))
            return

        date_applied = (data.get("date_applied") or "").strip()
        updates: dict[str, Any] = {
            "status": data.get("status"),
            "date_applied": f"{date_applied}T00:00:00Z" if date_applied else None,
            "job_source": (data.get("job_source") or "").strip() or None,
            "location": (data.get("location") or "").strip() or None,
            "salary_range": (data.get("salary_range") or "").strip() or None,
            "url": (data.get("url") or "").strip() or None,
            "dua": data.get("dua") == "true",
            "recruiter_name": (data.get("recruiter_name") or "").strip() or None,
            "recruiter_email": (data.get("recruiter_email") or "").strip() or None,
        }
        # Strip Nones so we don't overwrite fields with null — keep "dua" always
        # since False is a meaningful value, not "field wasn't provided".
        updates = {k: v for k, v in updates.items() if v is not None or k == "dua"}
        note = (data.get("note") or "").strip()

        try:
            record = await asyncio.to_thread(
                api_client.update_application, app_id, updates, user_email=user["email"]
            )
            if note:
                await asyncio.to_thread(api_client.add_comment, app_id, note, user_email=user["email"])
            await ctx.send_activity(MessageFactory.text(
                f"✅ Updated **{record.get('role_title')}** at **{record.get('company')}** "
                f"→ **{updates.get('status', record.get('status'))}**"
                + (f"\n> {note}" if note else "")
            ))
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Failed to update: {exc}"))

    async def _submit_track_note(self, ctx: TurnContext, data: dict, user: dict):
        app_id = (data.get("app_id") or "").strip()
        note = (data.get("note") or "").strip()
        if not app_id or not note:
            await ctx.send_activity(MessageFactory.text("❌ Application and note are required."))
            return

        try:
            record = await asyncio.to_thread(api_client.get_application, app_id, user_email=user["email"])
            await asyncio.to_thread(api_client.add_comment, app_id, note, user_email=user["email"])
            await ctx.send_activity(MessageFactory.text(
                f"✅ Note added to **{record.get('role_title')}** at **{record.get('company')}**\n> {note}"
            ))
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Failed to add note: {exc}"))

    async def _submit_track_delete_select(self, ctx: TurnContext, data: dict, user: dict):
        """Step 1 of track delete -> show a confirmation card before deleting."""
        app_id = (data.get("app_id") or "").strip()
        if not app_id:
            await ctx.send_activity(MessageFactory.text("❌ Please select an application."))
            return

        try:
            a = await asyncio.to_thread(api_client.get_application, app_id, user_email=user["email"])
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Could not load application: {exc}"))
            return

        label = f"{a.get('company', '?')} — {a.get('role_title', '?')}"
        card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.5",
            "body": [
                {
                    "type": "TextBlock", "wrap": True, "color": "Attention",
                    "text": f"⚠️ Are you sure you want to permanently delete **{label}**?\n\nThis cannot be undone.",
                },
            ],
            "actions": [
                {
                    "type": "Action.Submit", "title": "Delete", "style": "destructive",
                    "data": {"action": "track_delete_confirm_submit", "app_id": app_id},
                },
                {"type": "Action.Submit", "title": "Cancel", "data": {"action": "track_delete_cancel_submit"}},
            ],
        }
        await ctx.send_activity(MessageFactory.attachment(_card_attachment(card)))

    async def _submit_track_delete_confirm(self, ctx: TurnContext, data: dict, user: dict):
        app_id = (data.get("app_id") or "").strip()
        if not app_id:
            await ctx.send_activity(MessageFactory.text("❌ Missing application reference."))
            return

        try:
            record = await asyncio.to_thread(api_client.get_application, app_id, user_email=user["email"])
            await asyncio.to_thread(api_client.delete_application, app_id, user_email=user["email"])
            await ctx.send_activity(MessageFactory.text(
                f"🗑️ Deleted **{record.get('role_title')}** at **{record.get('company')}**."
            ))
        except Exception as exc:
            await ctx.send_activity(MessageFactory.text(f"❌ Failed to delete: {exc}"))

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
