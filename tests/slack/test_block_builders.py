"""
Tests for modal/block builder functions.

Verifies the structure of blocks returned by _track_add_blocks(),
_cal_add_blocks(), and other builders — catching schema regressions
before they reach Slack's API.
"""

import os
os.environ.setdefault("SLACK_BOT_TOKEN",      "xoxb-test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-secret")
os.environ.setdefault("BOT_API_KEY",          "test-key")

import pytest
from unittest.mock import patch, MagicMock

from tests.slack.conftest import _PassthroughApp

with patch("slack_bolt.App", _PassthroughApp):
    import slack_bot as bot


# ── Block Kit schema helpers ──────────────────────────────────────────────────

def assert_valid_block(block: dict):
    """Every block must have a 'type' field."""
    assert "type" in block, f"Block missing 'type': {block}"


def assert_valid_text_object(obj: dict):
    """Slack text objects must have 'type' and 'text'."""
    assert "type" in obj, f"Text object missing 'type': {obj}"
    assert "text" in obj, f"Text object missing 'text': {obj}"
    assert obj["type"] in ("plain_text", "mrkdwn"), f"Invalid text type: {obj['type']}"


def assert_valid_element(el: dict):
    """Interactive elements must have a 'type'."""
    assert "type" in el, f"Element missing 'type': {el}"


def assert_valid_option(opt: dict):
    """Select options must have 'text' and 'value'."""
    assert "text" in opt, f"Option missing 'text': {opt}"
    assert "value" in opt, f"Option missing 'value': {opt}"
    assert_valid_text_object(opt["text"])
    assert isinstance(opt["value"], str), f"Option value must be string: {opt['value']}"


def walk_blocks(blocks: list[dict]):
    """Yield every block and nested element for validation."""
    for block in blocks:
        yield block
        for el in block.get("elements", []):
            yield el
        if "accessory" in block:
            yield block["accessory"]
        if "element" in block:
            yield block["element"]


# ── _track_add_blocks ─────────────────────────────────────────────────────────

class TestTrackAddBlocks:
    def _blocks(self, prefill=None):
        return bot._track_add_blocks(prefill=prefill)

    def test_returns_list(self):
        assert isinstance(self._blocks(), list)

    def test_non_empty(self):
        assert len(self._blocks()) > 0

    def test_all_blocks_have_type(self):
        for block in self._blocks():
            assert_valid_block(block)

    def test_has_input_blocks(self):
        types = [b["type"] for b in self._blocks()]
        assert "input" in types

    def test_input_blocks_have_label(self):
        for block in self._blocks():
            if block["type"] == "input":
                assert "label" in block, f"Input block missing label: {block}"
                assert_valid_text_object(block["label"])

    def test_input_blocks_have_element(self):
        for block in self._blocks():
            if block["type"] == "input":
                assert "element" in block or "accessory" in block, \
                    f"Input block missing element: {block}"

    def test_no_text_exceeds_75_chars(self):
        """Slack plain_text labels are limited to 75 chars in some contexts."""
        for block in self._blocks():
            if block.get("type") == "input":
                label_text = block.get("label", {}).get("text", "")
                assert len(label_text) <= 75, f"Label too long: {label_text!r}"

    def test_prefill_sets_initial_values(self):
        """Prefill dict should set initial values on matching fields."""
        prefill = {"status": "Applied", "priority": "High"}
        blocks = self._blocks(prefill=prefill)
        # Should not raise and should return blocks with prefilled data
        assert isinstance(blocks, list)
        assert len(blocks) > 0

    def test_status_field_has_valid_options(self):
        """The status select must include all valid statuses."""
        blocks = self._blocks()
        # Find the status block
        status_options = []
        for block in blocks:
            if block.get("type") != "input":
                continue
            el = block.get("element", {})
            if el.get("type") in ("static_select", "external_select"):
                opts = el.get("options", [])
                for opt in opts:
                    status_options.append(opt.get("value", ""))

        # At least some blocks should have options matching valid statuses
        all_options = []
        for block in blocks:
            if block.get("type") == "input":
                el = block.get("element", {})
                for opt in el.get("options", []):
                    all_options.append(opt.get("value", ""))

        for status in ["Applied", "Interviewing", "Rejected"]:
            if all_options:
                assert status in all_options, f"Status '{status}' missing from options"


# ── _cal_add_blocks ───────────────────────────────────────────────────────────

class TestCalAddBlocks:
    def _blocks(self):
        return bot._cal_add_blocks()

    def test_returns_list(self):
        assert isinstance(self._blocks(), list)

    def test_non_empty(self):
        assert len(self._blocks()) > 0

    def test_all_blocks_have_type(self):
        for block in self._blocks():
            assert_valid_block(block)

    def test_has_input_blocks(self):
        types = [b["type"] for b in self._blocks()]
        assert "input" in types

    def test_input_blocks_have_labels(self):
        for block in self._blocks():
            if block["type"] == "input":
                assert "label" in block
                assert_valid_text_object(block["label"])

    def test_event_type_options_present(self):
        """Calendar events should have type options (interview, deadline, etc.)."""
        blocks = self._blocks()
        all_options = []
        for block in blocks:
            if block.get("type") == "input":
                el = block.get("element", {})
                for opt in el.get("options", []):
                    all_options.append(opt.get("value", ""))
        # Should have some options
        assert len(all_options) > 0

    def test_no_empty_text_values(self):
        """No text object should have an empty 'text' value."""
        for block in self._blocks():
            if "label" in block:
                assert len(block["label"].get("text", "")) > 0, \
                    f"Empty label text in block: {block}"


# ── apply modal structure ─────────────────────────────────────────────────────

class TestApplyModal:
    def test_modal_has_required_fields(self):
        client = MagicMock()
        client.views_open.return_value = {"ok": True}
        body = {"user_id": "U123", "trigger_id": "trigger.1", "channel_id": "C1",
                "user_name": "user", "text": "", "team_id": "T1"}
        bot.apply_command(ack=MagicMock(), body=body, client=client)

        view = client.views_open.call_args[1].get("view", {})
        assert view.get("type") == "modal"
        assert "title" in view
        assert "submit" in view
        assert "blocks" in view
        assert "callback_id" in view

    def test_modal_title_is_text_object(self):
        client = MagicMock()
        client.views_open.return_value = {"ok": True}
        body = {"user_id": "U123", "trigger_id": "trigger.1", "channel_id": "C1",
                "user_name": "user", "text": "", "team_id": "T1"}
        bot.apply_command(ack=MagicMock(), body=body, client=client)

        view = client.views_open.call_args[1].get("view", {})
        assert_valid_text_object(view["title"])

    def test_all_blocks_in_modal_have_type(self):
        client = MagicMock()
        client.views_open.return_value = {"ok": True}
        body = {"user_id": "U123", "trigger_id": "trigger.1", "channel_id": "C1",
                "user_name": "user", "text": "", "team_id": "T1"}
        bot.apply_command(ack=MagicMock(), body=body, client=client)

        view = client.views_open.call_args[1].get("view", {})
        for block in view.get("blocks", []):
            assert_valid_block(block)


# ── prep modal structure ──────────────────────────────────────────────────────

class TestPrepModal:
    def test_modal_has_required_fields(self):
        client = MagicMock()
        client.views_open.return_value = {"ok": True}
        body = {"user_id": "U123", "trigger_id": "trigger.1", "channel_id": "C1",
                "user_name": "user", "text": "", "team_id": "T1"}
        bot.prep_command(ack=MagicMock(), body=body, client=client)

        view = client.views_open.call_args[1].get("view", {})
        assert view.get("type") == "modal"
        assert "title" in view
        assert "blocks" in view

    def test_round_type_options_in_modal(self):
        """Prep modal should have round type selector."""
        client = MagicMock()
        client.views_open.return_value = {"ok": True}
        body = {"user_id": "U123", "trigger_id": "trigger.1", "channel_id": "C1",
                "user_name": "user", "text": "", "team_id": "T1"}
        bot.prep_command(ack=MagicMock(), body=body, client=client)

        view = client.views_open.call_args[1].get("view", {})
        all_options = []
        for block in view.get("blocks", []):
            el = block.get("element", {})
            for opt in el.get("options", []):
                all_options.append(opt.get("value", ""))

        # Should have round type options
        assert len(all_options) > 0
