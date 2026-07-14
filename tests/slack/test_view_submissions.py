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


def _prep_select_view(app_id: str = "app-001", round_type: str = "technical",
                       interviewer: str = "", selected_date: str = "", selected_time: str = "",
                       location: str = "", focus: str = "") -> dict:
    return {"state": {"values": {
        "app_block":      {"app_select": {"selected_option": {"value": app_id}}},
        "round_type":     {"value": {"selected_option": {"value": round_type}}},
        "interviewer":    {"value": {"value": interviewer}},
        "interview_date": {"value": {"selected_date": selected_date}},
        "interview_time": {"value": {"selected_time": selected_time}},
        "location":       {"value": {"value": location}},
        "focus":          {"value": {"value": focus}},
    }}}


class TestPrepSelectViewSubmit:
    def test_datepicker_and_timepicker_values_reach_start_prep_run(self):
        client = MagicMock()
        view = _prep_select_view(
            interviewer="Jane Smith - VP Eng\nJohn Doe - Peer",
            selected_date="2026-07-20", selected_time="14:00",
            location="Zoom: https://zoom.us/j/123",
        )
        with patch.object(bot, "_get_app", return_value=SAMPLE_APPS[0]), \
             patch.object(bot, "_get_saved_job_posting", return_value="Saved JD"), \
             patch.object(bot, "_start_prep_run") as mock_start:
            bot.prep_select_view_submit(ack=make_ack(), body={"user": {"id": "U1"}}, client=client, view=view)

        args, kwargs = mock_start.call_args
        # _start_prep_run(channel, client, company, role, round_type, interviewer,
        #                  focus, job_posting, domain, interview_date, interview_time, location)
        assert args[5] == "Jane Smith - VP Eng\nJohn Doe - Peer"
        assert args[9]  == "2026-07-20"
        assert args[10] == "14:00"
        assert args[11] == "Zoom: https://zoom.us/j/123"

    def test_blank_logistics_fields_pass_through_as_empty(self):
        client = MagicMock()
        view = _prep_select_view()
        with patch.object(bot, "_get_app", return_value=SAMPLE_APPS[0]), \
             patch.object(bot, "_get_saved_job_posting", return_value="Saved JD"), \
             patch.object(bot, "_start_prep_run") as mock_start:
            bot.prep_select_view_submit(ack=make_ack(), body={"user": {"id": "U1"}}, client=client, view=view)

        args, kwargs = mock_start.call_args
        assert args[9] == ""
        assert args[10] == ""
        assert args[11] == ""

    def test_no_saved_jd_carries_logistics_into_paste_card_metadata(self):
        import json
        client = MagicMock()
        view = _prep_select_view(
            interviewer="Jane Smith", selected_date="2026-07-20",
            selected_time="14:00", location="123 Main St",
        )
        with patch.object(bot, "_get_app", return_value=SAMPLE_APPS[0]), \
             patch.object(bot, "_get_saved_job_posting", return_value=None):
            ack = make_ack()
            bot.prep_select_view_submit(ack=ack, body={"user": {"id": "U1"}}, client=client, view=view)

        update_view = ack.call_args.kwargs["view"]
        metadata = json.loads(update_view["private_metadata"])
        assert metadata["interview_date"] == "2026-07-20"
        assert metadata["interview_time"] == "14:00"
        assert metadata["location"] == "123 Main St"
