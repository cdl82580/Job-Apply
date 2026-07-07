"""
Tests for card-builder helper functions in teams_bot/bot.py — Adaptive Card
equivalent of tests/slack/test_block_builders.py's Block Kit schema checks,
plus tests/slack/test_api_helpers.py's coverage of _local_to_utc_iso.
"""

from datetime import datetime, timezone

import pytest


@pytest.fixture(scope="module")
def bm(bot_module):
    return bot_module


# ── _load_card ────────────────────────────────────────────────────────────

CARD_NAMES = ["apply_form", "aq_form", "optimize_form", "prep_form", "thankyou_form", "track_add_form"]


class TestLoadCard:
    @pytest.mark.parametrize("name", CARD_NAMES)
    def test_loads_valid_adaptive_card(self, bm, name):
        card = bm._load_card(name)
        assert card["type"] == "AdaptiveCard"
        assert "body" in card
        assert isinstance(card["body"], list)

    @pytest.mark.parametrize("name", CARD_NAMES)
    def test_has_submit_action(self, bm, name):
        card = bm._load_card(name)
        submit_actions = [a for a in card.get("actions", []) if a.get("type") == "Action.Submit"]
        assert submit_actions, f"{name} has no Action.Submit"
        assert "action" in submit_actions[0]["data"]

    def test_unknown_card_raises(self, bm):
        with pytest.raises(FileNotFoundError):
            bm._load_card("does_not_exist")


# ── _card_attachment ──────────────────────────────────────────────────────

class TestCardAttachment:
    def test_wraps_dict_as_adaptive_card_attachment(self, bm):
        card = {"type": "AdaptiveCard", "body": []}
        att = bm._card_attachment(card)
        assert att.content_type == "application/vnd.microsoft.card.adaptive"
        assert att.content == card


# ── _logo_url / _logo_column ─────────────────────────────────────────────────

class TestLogoUrl:
    def test_builds_logodev_url_with_domain(self, bm):
        url = bm._logo_url("salesforce.com")
        assert url.startswith("https://img.logo.dev/salesforce.com")
        assert "token=" in url

    def test_empty_domain_returns_empty_string(self, bm):
        assert bm._logo_url("") == ""

    def test_respects_size_param(self, bm):
        url = bm._logo_url("stripe.com", size=64)
        assert "size=64" in url


class TestLogoColumn:
    def test_returns_column_with_image_for_domain(self, bm):
        col = bm._logo_column("salesforce.com")
        assert col["type"] == "Column"
        assert col["items"][0]["type"] == "Image"

    def test_returns_none_for_empty_domain(self, bm):
        assert bm._logo_column("") is None


# ── _fmt_event_dt ─────────────────────────────────────────────────────────

class TestFmtEventDt:
    def test_formats_iso_datetime(self, bm):
        result = bm._fmt_event_dt("2026-07-10T14:00:00Z")
        assert "Jul" in result
        assert "PM" in result
        assert "UTC" in result

    def test_empty_string_returns_dash(self, bm):
        assert bm._fmt_event_dt("") == "—"

    def test_invalid_input_does_not_raise(self, bm):
        result = bm._fmt_event_dt("not-a-date")
        assert isinstance(result, str)


# ── _local_to_utc_iso ─────────────────────────────────────────────────────

class TestLocalToUtcIso:
    def test_utc_timezone_passthrough(self, bm):
        assert bm._local_to_utc_iso("2026-06-15", "14:00", "UTC") == "2026-06-15T14:00:00Z"

    def test_converts_from_named_timezone(self, bm):
        result = bm._local_to_utc_iso("2026-06-15", "14:00", "America/New_York")
        assert result.endswith("Z")
        assert "T" in result

    def test_invalid_timezone_falls_back_to_utc(self, bm):
        result = bm._local_to_utc_iso("2026-06-15", "14:00", "Invalid/Zone")
        assert result == "2026-06-15T14:00:00Z"

    def test_returns_string(self, bm):
        assert isinstance(bm._local_to_utc_iso("2026-01-01", "09:00", "UTC"), str)
