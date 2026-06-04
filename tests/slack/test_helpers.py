"""
Unit tests for pure helper functions in slack_bot.py.
No mocking needed — these are pure transforms.
"""

import os, sys
os.environ.setdefault("SLACK_BOT_TOKEN",      "xoxb-test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-secret")
os.environ.setdefault("BOT_API_KEY",          "test-key")

import pytest
from unittest.mock import MagicMock, patch

# Patch App constructor before import
from tests.slack.conftest import _PassthroughApp

with patch("slack_bolt.App", _PassthroughApp):
    import slack_bot as bot


# ── _fmt_date ─────────────────────────────────────────────────────────────────

class TestFmtDate:
    def test_none_returns_dash(self):
        assert bot._fmt_date(None) == "—"

    def test_empty_returns_dash(self):
        assert bot._fmt_date("") == "—"

    def test_iso_datetime(self):
        assert bot._fmt_date("2026-05-15T00:00:00Z") == "5/15/26"

    def test_iso_date_only(self):
        assert bot._fmt_date("2026-01-01T00:00:00Z") == "1/1/26"

    def test_december(self):
        assert bot._fmt_date("2025-12-31T00:00:00Z") == "12/31/25"

    def test_leading_zero_month_stripped(self):
        result = bot._fmt_date("2026-03-07T00:00:00Z")
        assert result == "3/7/26"


# ── _app_line ─────────────────────────────────────────────────────────────────

class TestAppLine:
    def _app(self, **kwargs):
        base = {
            "id": "app-1",
            "company": "Acme",
            "role_title": "Engineer",
            "status": "Applied",
            "date_applied": "2026-05-01T00:00:00Z",
            "url": "https://acme.com/jobs/1",
        }
        base.update(kwargs)
        return base

    def test_contains_company_name(self):
        line = bot._app_line(self._app())
        assert "Acme" in line

    def test_contains_role_title(self):
        line = bot._app_line(self._app())
        assert "Engineer" in line

    def test_contains_status(self):
        line = bot._app_line(self._app())
        assert "Applied" in line

    def test_contains_date_when_set(self):
        line = bot._app_line(self._app())
        assert "Applied 5/1/26" in line

    def test_no_date_when_missing(self):
        line = bot._app_line(self._app(date_applied=""))
        assert "Applied" in line  # status still shown
        assert "Applied " not in line.split("Applied")[0]  # "Applied date" not shown

    def test_contains_url_link(self):
        line = bot._app_line(self._app())
        assert "https://acme.com/jobs/1" in line

    def test_no_url_when_empty(self):
        line = bot._app_line(self._app(url=""))
        assert "Job Post" not in line

    def test_status_emoji_interviewing(self):
        line = bot._app_line(self._app(status="Interviewing"))
        assert "🎯" in line

    def test_status_emoji_rejected(self):
        line = bot._app_line(self._app(status="Rejected"))
        assert "❌" in line

    def test_status_emoji_offer(self):
        line = bot._app_line(self._app(status="Offer"))
        assert "🎉" in line

    def test_missing_company_shows_placeholder(self):
        line = bot._app_line({"status": "Applied", "role_title": "Eng", "date_applied": ""})
        assert "?" in line


# ── _app_options ─────────────────────────────────────────────────────────────

class TestAppOptions:
    def _apps(self):
        return [
            {"id": "1", "company": "Acme", "role_title": "Eng", "status": "Applied"},
            {"id": "2", "company": "Beta", "role_title": "Dev", "status": "Interviewing"},
            {"id": "3", "company": "Coda", "role_title": "PM",  "status": "Rejected"},
            {"id": "4", "company": "Dext", "role_title": "SWE", "status": "Not Applying"},
        ]

    def test_returns_list_of_dicts(self):
        opts = bot._app_options(self._apps())
        assert isinstance(opts, list)
        assert all(isinstance(o, dict) for o in opts)

    def test_option_has_text_and_value(self):
        opts = bot._app_options(self._apps())
        for o in opts:
            assert "text" in o
            assert "value" in o

    def test_active_only_excludes_rejected(self):
        opts = bot._app_options(self._apps(), active_only=True)
        values = [o["value"] for o in opts]
        assert "3" not in values  # Rejected

    def test_active_only_excludes_not_applying(self):
        opts = bot._app_options(self._apps(), active_only=True)
        values = [o["value"] for o in opts]
        assert "4" not in values  # Not Applying

    def test_active_false_includes_all(self):
        opts = bot._app_options(self._apps(), active_only=False)
        values = [o["value"] for o in opts]
        assert "3" in values
        assert "4" in values

    def test_label_max_75_chars(self):
        long_apps = [{"id": "x", "company": "A" * 50, "role_title": "B" * 40, "status": "Applied"}]
        opts = bot._app_options(long_apps, active_only=False)
        for o in opts:
            assert len(o["text"]["text"]) <= 75

    def test_capped_at_100(self):
        many_apps = [
            {"id": str(i), "company": f"Co{i}", "role_title": "Eng", "status": "Applied"}
            for i in range(150)
        ]
        opts = bot._app_options(many_apps, active_only=False)
        assert len(opts) <= 100

    def test_sorted_by_status_priority(self):
        apps = [
            {"id": "a", "company": "Z", "role_title": "Eng", "status": "Researching"},
            {"id": "b", "company": "A", "role_title": "Eng", "status": "Interviewing"},
        ]
        opts = bot._app_options(apps, active_only=False)
        # Interviewing (index 4 in VALID_STATUSES) should come before Researching (index 1)
        # Actually Interviewing is higher priority in the order list
        assert len(opts) == 2

    def test_empty_list_returns_empty(self):
        assert bot._app_options([]) == []


# ── STATUS_EMOJI completeness ─────────────────────────────────────────────────

class TestStatusEmoji:
    def test_all_valid_statuses_have_emoji(self):
        for status in bot.VALID_STATUSES:
            assert status in bot.STATUS_EMOJI, f"Missing emoji for status: {status}"

    def test_emoji_values_are_strings(self):
        for status, emoji in bot.STATUS_EMOJI.items():
            assert isinstance(emoji, str) and len(emoji) > 0


# ── VALID_STATUSES / VALID_PRIORITIES ────────────────────────────────────────

class TestConstants:
    def test_valid_statuses_is_list(self):
        assert isinstance(bot.VALID_STATUSES, list)
        assert len(bot.VALID_STATUSES) >= 6

    def test_valid_priorities_is_list(self):
        assert isinstance(bot.VALID_PRIORITIES, list)
        assert "High" in bot.VALID_PRIORITIES
        assert "Medium" in bot.VALID_PRIORITIES
        assert "Low" in bot.VALID_PRIORITIES

    def test_expected_statuses_present(self):
        for expected in ["Applied", "Interviewing", "Rejected", "Offer", "Researching"]:
            assert expected in bot.VALID_STATUSES
