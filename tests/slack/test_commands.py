"""
Unit tests for Slack slash command handlers.

Each handler is called directly with mock ack/respond/client/body arguments.
All outbound HTTP (_api calls) are patched so no network traffic occurs.
"""

import os
os.environ.setdefault("SLACK_BOT_TOKEN",      "xoxb-test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-secret")
os.environ.setdefault("BOT_API_KEY",          "test-key")

import pytest
from unittest.mock import MagicMock, patch, call

from tests.slack.conftest import _PassthroughApp

with patch("slack_bolt.App", _PassthroughApp):
    import slack_bot as bot

from tests.slack.conftest import (
    make_ack, make_respond, make_client, make_body,
    fake_response, SAMPLE_APPS, SAMPLE_ME, SAMPLE_HEALTH,
)


# ── /tracker ─────────────────────────────────────────────────────────────────

class TestTrackerCommand:
    def test_ack_called(self):
        ack = make_ack()
        with patch.object(bot, "_get_apps", return_value=SAMPLE_APPS):
            bot.tracker_command(ack=ack, respond=make_respond())
        ack.assert_called_once()

    def test_respond_called_with_text(self):
        respond = make_respond()
        with patch.object(bot, "_get_apps", return_value=SAMPLE_APPS):
            bot.tracker_command(ack=make_ack(), respond=respond)
        respond.assert_called_once()
        text = respond.call_args[0][0] if respond.call_args[0] else str(respond.call_args)
        assert "Pipeline" in text or "pipeline" in text.lower()

    def test_total_count_in_response(self):
        respond = make_respond()
        with patch.object(bot, "_get_apps", return_value=SAMPLE_APPS):
            bot.tracker_command(ack=make_ack(), respond=respond)
        text = respond.call_args[0][0]
        assert str(len(SAMPLE_APPS)) in text

    def test_api_error_responds_with_error_message(self):
        respond = make_respond()
        with patch.object(bot, "_get_apps", side_effect=Exception("connection refused")):
            bot.tracker_command(ack=make_ack(), respond=respond)
        text = respond.call_args[0][0]
        assert ":x:" in text or "error" in text.lower() or "reach" in text.lower()

    def test_empty_apps_responds_gracefully(self):
        respond = make_respond()
        with patch.object(bot, "_get_apps", return_value=[]):
            bot.tracker_command(ack=make_ack(), respond=respond)
        respond.assert_called_once()

    def test_interviewing_status_shown(self):
        respond = make_respond()
        with patch.object(bot, "_get_apps", return_value=SAMPLE_APPS):
            bot.tracker_command(ack=make_ack(), respond=respond)
        text = respond.call_args[0][0]
        assert "Interviewing" in text


# ── /track-list ───────────────────────────────────────────────────────────────

class TestTrackListCommand:
    def test_ack_called(self):
        ack = make_ack()
        with patch.object(bot, "_get_apps", return_value=SAMPLE_APPS):
            bot.track_list_command(ack=ack, respond=make_respond(), body=make_body())
        ack.assert_called_once()

    def test_returns_blocks(self):
        respond = make_respond()
        with patch.object(bot, "_get_apps", return_value=SAMPLE_APPS):
            bot.track_list_command(ack=make_ack(), respond=respond, body=make_body())
        kwargs = respond.call_args[1] if respond.call_args[1] else {}
        assert "blocks" in kwargs

    def test_status_filter_applied(self):
        respond = make_respond()
        with patch.object(bot, "_get_apps", return_value=[SAMPLE_APPS[1]]) as mock_get:
            bot.track_list_command(
                ack=make_ack(), respond=respond,
                body=make_body(text="applied"),
            )
            mock_get.assert_called_once_with(status="Applied")

    def test_invalid_status_returns_error(self):
        respond = make_respond()
        with patch.object(bot, "_get_apps", return_value=[]):
            bot.track_list_command(
                ack=make_ack(), respond=respond,
                body=make_body(text="unknownstatus"),
            )
        text = respond.call_args[0][0]
        assert ":x:" in text

    def test_empty_results_shows_message(self):
        respond = make_respond()
        with patch.object(bot, "_get_apps", return_value=[]):
            bot.track_list_command(ack=make_ack(), respond=respond, body=make_body())
        respond.assert_called_once()

    def test_api_error_responds_with_error(self):
        respond = make_respond()
        with patch.object(bot, "_get_apps", side_effect=Exception("timeout")):
            bot.track_list_command(ack=make_ack(), respond=respond, body=make_body())
        text = respond.call_args[0][0]
        assert ":x:" in text

    def test_case_insensitive_status_filter(self):
        respond = make_respond()
        with patch.object(bot, "_get_apps", return_value=[SAMPLE_APPS[0]]) as mock_get:
            bot.track_list_command(
                ack=make_ack(), respond=respond,
                body=make_body(text="INTERVIEWING"),
            )
            mock_get.assert_called_with(status="Interviewing")

    def test_truncates_at_15_with_more_notice(self):
        respond = make_respond()
        many_apps = [
            {"id": str(i), "company": f"Co{i}", "role_title": "Eng",
             "status": "Applied", "date_applied": "", "url": ""}
            for i in range(20)
        ]
        with patch.object(bot, "_get_apps", return_value=many_apps):
            bot.track_list_command(ack=make_ack(), respond=respond, body=make_body())
        kwargs = respond.call_args[1]
        blocks = kwargs["blocks"]
        # Last block should mention "more"
        last_text = str(blocks[-1])
        assert "more" in last_text.lower()


# ── /whoami ───────────────────────────────────────────────────────────────────

class TestWhoamiCommand:
    def test_ack_called(self):
        ack = make_ack()
        with patch.object(bot, "_api", return_value=fake_response(200, SAMPLE_ME)):
            bot.me_command(ack=ack, respond=make_respond())
        ack.assert_called_once()

    def test_respond_shows_email(self):
        respond = make_respond()
        with patch.object(bot, "_api", return_value=fake_response(200, SAMPLE_ME)):
            bot.me_command(ack=make_ack(), respond=respond)
        text = respond.call_args[0][0]
        assert "test@example.com" in text

    def test_respond_shows_display_name(self):
        respond = make_respond()
        with patch.object(bot, "_api", return_value=fake_response(200, SAMPLE_ME)):
            bot.me_command(ack=make_ack(), respond=respond)
        text = respond.call_args[0][0]
        assert "Test User" in text

    def test_verified_indicator(self):
        respond = make_respond()
        with patch.object(bot, "_api", return_value=fake_response(200, SAMPLE_ME)):
            bot.me_command(ack=make_ack(), respond=respond)
        text = respond.call_args[0][0]
        assert "Verified" in text or "verified" in text.lower()

    def test_api_error_responds_with_error(self):
        respond = make_respond()
        with patch.object(bot, "_api", return_value=fake_response(500)):
            bot.me_command(ack=make_ack(), respond=respond)
        text = respond.call_args[0][0]
        assert ":x:" in text


# ── /runs ─────────────────────────────────────────────────────────────────────

class TestRunsCommand:
    def _runs_response(self):
        return {
            "folders": [
                {"name": "Acme_SeniorEngineer", "web_view_link": "https://drive.google.com/123"},
                {"name": "Stripe_StaffEngineer", "web_view_link": "https://drive.google.com/456"},
            ]
        }

    def test_ack_called(self):
        ack = make_ack()
        with patch.object(bot, "_api", return_value=fake_response(200, self._runs_response())):
            bot.runs_command(ack=ack, respond=make_respond())
        ack.assert_called_once()

    def test_folders_shown_in_response(self):
        respond = make_respond()
        with patch.object(bot, "_api", return_value=fake_response(200, self._runs_response())):
            bot.runs_command(ack=make_ack(), respond=respond)
        respond.assert_called_once()
        text = str(respond.call_args)
        assert "Acme" in text or "runs" in text.lower()

    def test_empty_folders_handled(self):
        respond = make_respond()
        with patch.object(bot, "_api", return_value=fake_response(200, {"folders": []})):
            bot.runs_command(ack=make_ack(), respond=respond)
        respond.assert_called_once()

    def test_api_error_handled(self):
        respond = make_respond()
        with patch.object(bot, "_api", side_effect=Exception("error")):
            bot.runs_command(ack=make_ack(), respond=respond)
        respond.assert_called_once()


# ── /help ─────────────────────────────────────────────────────────────────────

class TestHelpCommand:
    def test_ack_called(self):
        ack = make_ack()
        bot.help_command(ack=ack, respond=make_respond())
        ack.assert_called_once()

    def test_respond_called(self):
        respond = make_respond()
        bot.help_command(ack=make_ack(), respond=respond)
        respond.assert_called_once()

    def test_response_mentions_apply(self):
        respond = make_respond()
        bot.help_command(ack=make_ack(), respond=respond)
        text = str(respond.call_args)
        assert "/apply" in text

    def test_response_mentions_tracker(self):
        respond = make_respond()
        bot.help_command(ack=make_ack(), respond=respond)
        text = str(respond.call_args)
        assert "/tracker" in text or "/track" in text

    def test_response_mentions_prep(self):
        respond = make_respond()
        bot.help_command(ack=make_ack(), respond=respond)
        text = str(respond.call_args)
        assert "/prep" in text


# ── /company ─────────────────────────────────────────────────────────────────

class TestCompanyCommand:
    def test_ack_called(self):
        ack = make_ack()
        company_data = [{"name": "Stripe", "domain": "stripe.com", "description": "Payments"}]
        with patch.object(bot, "_api", return_value=fake_response(200, company_data)):
            bot.company_command(ack=ack, respond=make_respond(), body=make_body(text="Stripe"))
        ack.assert_called_once()

    def test_no_query_shows_usage(self):
        respond = make_respond()
        bot.company_command(ack=make_ack(), respond=respond, body=make_body(text=""))
        text = respond.call_args[0][0]
        assert "Usage" in text or "usage" in text.lower() or "/company" in text

    def test_results_shown(self):
        respond = make_respond()
        company_data = [{"name": "Stripe", "domain": "stripe.com", "description": "Payments platform"}]
        with patch.object(bot, "_api", return_value=fake_response(200, company_data)):
            bot.company_command(ack=make_ack(), respond=respond, body=make_body(text="Stripe"))
        text = str(respond.call_args)
        assert "Stripe" in text

    def test_no_results_handled(self):
        respond = make_respond()
        with patch.object(bot, "_api", return_value=fake_response(200, [])):
            bot.company_command(ack=make_ack(), respond=respond, body=make_body(text="XYZNoMatch"))
        respond.assert_called_once()

    def test_api_error_handled(self):
        respond = make_respond()
        with patch.object(bot, "_api", side_effect=Exception("timeout")):
            bot.company_command(ack=make_ack(), respond=respond, body=make_body(text="Stripe"))
        text = respond.call_args[0][0]
        assert ":x:" in text


# ── /profile-resume ───────────────────────────────────────────────────────────

class TestProfileResumeCommand:
    def test_ack_called(self):
        ack = make_ack()
        bot.profile_resume_command(ack=ack, respond=make_respond())
        ack.assert_called_once()

    def test_respond_called_with_instructions(self):
        respond = make_respond()
        bot.profile_resume_command(ack=make_ack(), respond=respond)
        respond.assert_called_once()
        text = str(respond.call_args)
        assert ".docx" in text or "resume" in text.lower()


# ── /track-add (modal open) ───────────────────────────────────────────────────

class TestTrackAddCommand:
    def test_ack_called(self):
        ack = make_ack()
        client = make_client()
        bot.track_add_command(ack=ack, body=make_body(), client=client)
        ack.assert_called_once()

    def test_opens_modal(self):
        client = make_client()
        bot.track_add_command(ack=make_ack(), body=make_body(), client=client)
        client.views_open.assert_called_once()

    def test_modal_has_trigger_id(self):
        client = make_client()
        body = make_body(trigger_id="trigger.xyz")
        bot.track_add_command(ack=make_ack(), body=body, client=client)
        call_kwargs = client.views_open.call_args[1]
        assert call_kwargs.get("trigger_id") == "trigger.xyz"


# ── /track-delete (modal open) ────────────────────────────────────────────────

class TestTrackDeleteCommand:
    def test_ack_called(self):
        ack = make_ack()
        respond = make_respond()
        with patch.object(bot, "_get_apps", return_value=SAMPLE_APPS):
            bot.track_delete_command(ack=ack, body=make_body(), client=make_client(), respond=respond)
        ack.assert_called_once()

    def test_no_apps_responds_with_message(self):
        respond = make_respond()
        with patch.object(bot, "_get_apps", return_value=[]):
            bot.track_delete_command(
                ack=make_ack(), body=make_body(),
                client=make_client(), respond=respond,
            )
        respond.assert_called()

    def test_opens_modal_when_apps_exist(self):
        client = make_client()
        with patch.object(bot, "_get_apps", return_value=SAMPLE_APPS):
            bot.track_delete_command(
                ack=make_ack(), body=make_body(),
                client=client, respond=make_respond(),
            )
        client.views_open.assert_called_once()


# ── /cal-today ────────────────────────────────────────────────────────────────

class TestCalTodayCommand:
    def _events(self):
        return [
            {
                "id": "ev-1",
                "title": "Phone Screen — Acme",
                "event_type": "phone_screen",
                "datetime": "2026-06-04T14:00:00Z",
                "timezone": "America/New_York",
                "duration_minutes": 30,
            }
        ]

    def test_ack_called(self):
        ack = make_ack()
        with patch.object(bot, "_get_events", return_value=self._events()):
            bot.cal_today_command(ack=ack, respond=make_respond())
        ack.assert_called_once()

    def test_shows_events(self):
        respond = make_respond()
        with patch.object(bot, "_get_events", return_value=self._events()):
            bot.cal_today_command(ack=make_ack(), respond=respond)
        text = str(respond.call_args)
        assert "Acme" in text or "Phone Screen" in text

    def test_no_events_message(self):
        respond = make_respond()
        with patch.object(bot, "_get_events", return_value=[]):
            bot.cal_today_command(ack=make_ack(), respond=respond)
        text = str(respond.call_args)
        assert "no" in text.lower() or "empty" in text.lower() or "nothing" in text.lower()

    def test_api_error_handled(self):
        respond = make_respond()
        with patch.object(bot, "_get_events", side_effect=Exception("error")):
            bot.cal_today_command(ack=make_ack(), respond=respond)
        respond.assert_called_once()


# ── /cal-week ─────────────────────────────────────────────────────────────────

class TestCalWeekCommand:
    def test_ack_called(self):
        ack = make_ack()
        with patch.object(bot, "_get_events", return_value=[]):
            bot.cal_week_command(ack=ack, respond=make_respond())
        ack.assert_called_once()

    def test_respond_called(self):
        respond = make_respond()
        with patch.object(bot, "_get_events", return_value=[]):
            bot.cal_week_command(ack=make_ack(), respond=respond)
        respond.assert_called_once()


# ── /apply (modal open) ───────────────────────────────────────────────────────

class TestApplyCommand:
    def test_ack_called(self):
        ack = make_ack()
        bot.apply_command(ack=ack, body=make_body(), client=make_client())
        ack.assert_called_once()

    def test_opens_modal(self):
        client = make_client()
        bot.apply_command(ack=make_ack(), body=make_body(), client=client)
        client.views_open.assert_called_once()

    def test_modal_callback_id(self):
        client = make_client()
        bot.apply_command(ack=make_ack(), body=make_body(), client=client)
        view_arg = client.views_open.call_args[1].get("view", {})
        assert view_arg.get("callback_id") == "apply_submit"


# ── /prep (modal open) ────────────────────────────────────────────────────────

class TestPrepCommand:
    def test_ack_called(self):
        ack = make_ack()
        bot.prep_command(ack=ack, body=make_body(), client=make_client())
        ack.assert_called_once()

    def test_opens_modal(self):
        client = make_client()
        bot.prep_command(ack=make_ack(), body=make_body(), client=client)
        client.views_open.assert_called_once()

    def test_modal_callback_id(self):
        client = make_client()
        bot.prep_command(ack=make_ack(), body=make_body(), client=client)
        view_arg = client.views_open.call_args[1].get("view", {})
        assert view_arg.get("callback_id") == "prep_submit"


# ── /thankyou (modal open) ───────────────────────────────────────────────────

class TestThankYouCommand:
    def test_ack_called(self):
        ack = make_ack()
        bot.thankyou_command(ack=ack, body=make_body(), client=make_client())
        ack.assert_called_once()

    def test_opens_modal(self):
        client = make_client()
        bot.thankyou_command(ack=make_ack(), body=make_body(), client=client)
        client.views_open.assert_called_once()

    def test_modal_callback_id(self):
        client = make_client()
        bot.thankyou_command(ack=make_ack(), body=make_body(), client=client)
        view_arg = client.views_open.call_args[1].get("view", {})
        assert view_arg.get("callback_id") == "thankyou_submit"


# ── /help includes /thankyou ─────────────────────────────────────────────────

# ── /aq (modal open) ─────────────────────────────────────────────────────────

class TestAQCommand:
    def test_ack_called(self):
        ack = make_ack()
        bot.aq_command(ack=ack, body=make_body(), client=make_client())
        ack.assert_called_once()

    def test_opens_modal(self):
        client = make_client()
        bot.aq_command(ack=make_ack(), body=make_body(), client=client)
        client.views_open.assert_called_once()

    def test_modal_callback_id(self):
        client = make_client()
        bot.aq_command(ack=make_ack(), body=make_body(), client=client)
        view_arg = client.views_open.call_args[1].get("view", {})
        assert view_arg.get("callback_id") == "aq_submit"


# ── /help includes /thankyou and /aq ────────────────────────────────────────

class TestHelpIncludesCommands:
    def test_help_mentions_thankyou(self):
        respond = make_respond()
        bot.help_command(ack=make_ack(), respond=respond)
        text = str(respond.call_args)
        assert "/thankyou" in text

    def test_help_mentions_aq(self):
        respond = make_respond()
        bot.help_command(ack=make_ack(), respond=respond)
        text = str(respond.call_args)
        assert "/aq" in text
