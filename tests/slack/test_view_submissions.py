"""
Tests for @app.view(...) submission handlers — a gap in the existing Slack
suite, which only covered the slash-command modal openers. Starts with a
regression test for rescore_view_submit reading the wrong score-result field.
"""

import os
os.environ.setdefault("SLACK_BOT_TOKEN",      "xoxb-test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-secret")
os.environ.setdefault("BOT_API_KEY",          "test-key")

from unittest.mock import MagicMock, patch

from tests.slack.conftest import _PassthroughApp

with patch("slack_bolt.App", _PassthroughApp):
    import slack_bot as bot

from tests.slack.conftest import make_ack, fake_response, SAMPLE_APPS


class _SyncThread:
    """threading.Thread replacement that runs its target synchronously on
    .start() — rescore_view_submit (like several Teams bot submit handlers)
    does its work in a background daemon thread."""
    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        if self._target:
            self._target()


def _rescore_view(app_id: str = "app-001") -> dict:
    return {"state": {"values": {"app_block": {"app_select": {"selected_option": {"value": app_id}}}}}}


class TestRescoreViewSubmit:
    def test_reads_rationale_not_summary(self):
        """Regression test for the bug fixed in 8a01544: the handler used to
        read result.get("summary", ""), but POST /api/applications/{id}/score
        returns "rationale" — the score card silently never showed a rationale."""
        client = MagicMock()
        score_result = fake_response(200, {
            "score": 82, "category": "strong", "rationale": "Strong platform engineering overlap.",
        })
        with patch.object(bot, "_get_app", return_value=SAMPLE_APPS[0]), \
             patch.object(bot, "_api", return_value=score_result), \
             patch("slack_bot.threading.Thread", _SyncThread):
            bot.rescore_view_submit(ack=make_ack(), body={"user": {"id": "U1"}}, client=client, view=_rescore_view())

        final_call = client.chat_postMessage.call_args_list[-1]
        blocks = final_call.kwargs["blocks"]
        rationale_block = next(
            b for b in blocks
            if b["type"] == "section" and "Strong platform engineering overlap." in b.get("text", {}).get("text", "")
        )
        assert "Strong platform engineering overlap." in rationale_block["text"]["text"]

    def test_no_rationale_omits_block(self):
        client = MagicMock()
        score_result = fake_response(200, {"score": 40, "category": "weak"})
        with patch.object(bot, "_get_app", return_value=SAMPLE_APPS[0]), \
             patch.object(bot, "_api", return_value=score_result), \
             patch("slack_bot.threading.Thread", _SyncThread):
            bot.rescore_view_submit(ack=make_ack(), body={"user": {"id": "U1"}}, client=client, view=_rescore_view())

        final_call = client.chat_postMessage.call_args_list[-1]
        blocks = final_call.kwargs["blocks"]
        assert not any(
            b["type"] == "section" and b.get("text", {}).get("text", "").startswith("_") for b in blocks
        )

    def test_score_failure_posts_error(self):
        client = MagicMock()
        with patch.object(bot, "_get_app", return_value=SAMPLE_APPS[0]), \
             patch.object(bot, "_api", return_value=fake_response(500)), \
             patch("slack_bot.threading.Thread", _SyncThread):
            bot.rescore_view_submit(ack=make_ack(), body={"user": {"id": "U1"}}, client=client, view=_rescore_view())

        final_call = client.chat_postMessage.call_args_list[-1]
        assert "Rescore failed" in final_call.kwargs["text"]
