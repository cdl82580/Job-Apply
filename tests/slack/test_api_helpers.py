"""
Tests for the API helper functions (_api, _get_apps, _post_run, etc.)
and the polling/retry logic.
"""

import os, time
os.environ.setdefault("SLACK_BOT_TOKEN",      "xoxb-test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-secret")
os.environ.setdefault("BOT_API_KEY",          "test-key")

import pytest
from unittest.mock import MagicMock, patch, call

from tests.slack.conftest import _PassthroughApp

with patch("slack_bolt.App", _PassthroughApp):
    import slack_bot as bot

from tests.slack.conftest import fake_response, SAMPLE_APPS


# ── _api ──────────────────────────────────────────────────────────────────────

class TestApiHelper:
    def test_sets_authorization_header(self):
        with patch("requests.get", return_value=fake_response(200, {})) as mock_get:
            bot._api("get", "/api/health")
            headers = mock_get.call_args[1]["headers"]
            assert "Authorization" in headers
            assert headers["Authorization"] == f"Bearer {bot.BOT_API_KEY}"

    def test_builds_correct_url(self):
        with patch("requests.post", return_value=fake_response(200, {})) as mock_post:
            bot._api("post", "/api/run", json={"key": "val"})
            url = mock_post.call_args[0][0]
            assert url == f"{bot.API_BASE}/api/run"

    def test_passes_kwargs_to_requests(self):
        with patch("requests.get", return_value=fake_response(200, {})) as mock_get:
            bot._api("get", "/api/test", params={"foo": "bar"})
            assert mock_get.call_args[1].get("params") == {"foo": "bar"}

    def test_timeout_set(self):
        with patch("requests.get", return_value=fake_response(200, {})) as mock_get:
            bot._api("get", "/api/test")
            assert mock_get.call_args[1].get("timeout") == 30


# ── _get_apps ─────────────────────────────────────────────────────────────────

class TestGetApps:
    def test_returns_list_from_items_key(self):
        resp = fake_response(200, {"items": SAMPLE_APPS, "total": 4})
        with patch.object(bot, "_api", return_value=resp):
            result = bot._get_apps()
        assert result == SAMPLE_APPS

    def test_returns_list_when_flat_array(self):
        resp = fake_response(200, SAMPLE_APPS)
        with patch.object(bot, "_api", return_value=resp):
            result = bot._get_apps()
        assert result == SAMPLE_APPS

    def test_passes_status_filter(self):
        resp = fake_response(200, [SAMPLE_APPS[0]])
        with patch.object(bot, "_api", return_value=resp) as mock_api:
            bot._get_apps(status="Interviewing")
            call_kwargs = mock_api.call_args[1]
            assert call_kwargs.get("params", {}).get("status") == "Interviewing"

    def test_no_status_filter_passes_no_params(self):
        resp = fake_response(200, SAMPLE_APPS)
        with patch.object(bot, "_api", return_value=resp) as mock_api:
            bot._get_apps()
            call_kwargs = mock_api.call_args[1]
            assert not call_kwargs.get("params")

    def test_raises_on_http_error(self):
        import requests
        resp = fake_response(500)
        with patch.object(bot, "_api", return_value=resp):
            with pytest.raises(requests.HTTPError):
                bot._get_apps()


# ── _post_run ─────────────────────────────────────────────────────────────────

class TestPostRun:
    def test_posts_to_correct_path(self):
        resp = fake_response(200, {"run_id": "run-123", "machine_id": None})
        with patch.object(bot, "_api", return_value=resp) as mock_api:
            bot._post_run("JD text", "Acme", "Engineer")
            path = mock_api.call_args[0][1]
            assert path == "/api/run"

    def test_sends_correct_payload(self):
        resp = fake_response(200, {"run_id": "run-123", "machine_id": None})
        with patch.object(bot, "_api", return_value=resp) as mock_api:
            bot._post_run("Job description", "Stripe", "Staff Engineer", contact="Jane")
            payload = mock_api.call_args[1]["json"]
            assert payload["job_posting"] == "Job description"
            assert payload["company"] == "Stripe"
            assert payload["role"] == "Staff Engineer"
            assert payload["contact"] == "Jane"

    def test_returns_json_response(self):
        resp = fake_response(200, {"run_id": "run-abc", "machine_id": None})
        with patch.object(bot, "_api", return_value=resp):
            result = bot._post_run("JD", "Co", "Role")
        assert result["run_id"] == "run-abc"

    def test_raises_on_error(self):
        import requests
        with patch.object(bot, "_api", return_value=fake_response(400)):
            with pytest.raises(requests.HTTPError):
                bot._post_run("JD", "Co", "Role")


# ── _post_prep ────────────────────────────────────────────────────────────────

class TestPostPrep:
    def test_posts_to_correct_path(self):
        resp = fake_response(200, {"prep_id": "prep-123", "machine_id": None})
        with patch.object(bot, "_api", return_value=resp) as mock_api:
            bot._post_prep("JD", "Acme", "Engineer", "Phone Screen")
            path = mock_api.call_args[0][1]
            assert path == "/api/prep"

    def test_sends_correct_payload(self):
        resp = fake_response(200, {"prep_id": "prep-123", "machine_id": None})
        with patch.object(bot, "_api", return_value=resp) as mock_api:
            bot._post_prep("JD text", "Stripe", "Staff Eng", "Hiring Manager",
                           focus="AI angle", interviewer="Bill")
            payload = mock_api.call_args[1]["json"]
            assert payload["round_type"] == "Hiring Manager"
            assert payload["focus"] == "AI angle"
            assert payload["interviewer"] == "Bill"

    def test_empty_focus_sends_none(self):
        resp = fake_response(200, {"prep_id": "p1", "machine_id": None})
        with patch.object(bot, "_api", return_value=resp) as mock_api:
            bot._post_prep("JD", "Co", "Role", "Phone Screen", focus="")
            payload = mock_api.call_args[1]["json"]
            assert payload.get("focus") is None


# ── _poll_run ─────────────────────────────────────────────────────────────────

class TestPollRun:
    def test_returns_immediately_on_done(self):
        resp = fake_response(200, {"status": "done", "error": None})
        with patch.object(bot, "_api", return_value=resp):
            with patch("time.sleep") as mock_sleep:
                result = bot._poll_run("run-123", timeout=60)
        assert result["status"] == "done"
        mock_sleep.assert_not_called()

    def test_returns_immediately_on_error(self):
        resp = fake_response(200, {"status": "error", "error": "Claude failed"})
        with patch.object(bot, "_api", return_value=resp):
            result = bot._poll_run("run-123", timeout=60)
        assert result["status"] == "error"

    def test_polls_until_done(self):
        responses = [
            fake_response(200, {"status": "running"}),
            fake_response(200, {"status": "running"}),
            fake_response(200, {"status": "done"}),
        ]
        with patch.object(bot, "_api", side_effect=responses):
            with patch("time.sleep"):
                result = bot._poll_run("run-123", timeout=60)
        assert result["status"] == "done"

    def test_returns_timeout_when_exceeded(self):
        resp = fake_response(200, {"status": "running"})
        with patch.object(bot, "_api", return_value=resp):
            with patch("time.sleep"):
                with patch("time.time", side_effect=[0, 0, 999]):
                    result = bot._poll_run("run-123", timeout=1)
        assert result["status"] == "timeout"


# ── _poll_prep ────────────────────────────────────────────────────────────────

class TestPollPrep:
    def test_returns_on_done(self):
        resp = fake_response(200, {"status": "done", "error": None})
        with patch.object(bot, "_api", return_value=resp):
            result = bot._poll_prep("prep-123", timeout=60)
        assert result["status"] == "done"

    def test_returns_timeout(self):
        resp = fake_response(200, {"status": "running"})
        with patch.object(bot, "_api", return_value=resp):
            with patch("time.sleep"):
                with patch("time.time", side_effect=[0, 0, 999]):
                    result = bot._poll_prep("prep-123", timeout=1)
        assert result["status"] == "timeout"


# ── _local_to_utc_iso ────────────────────────────────────────────────────────

class TestLocalToUtcIso:
    def test_basic_conversion(self):
        result = bot._local_to_utc_iso("2026-06-15", "14:00", "America/New_York")
        assert "T" in result
        assert result.endswith("Z")

    def test_utc_timezone(self):
        result = bot._local_to_utc_iso("2026-06-15", "14:00", "UTC")
        assert "2026-06-15T14:00:00Z" == result

    def test_invalid_timezone_falls_back(self):
        # Should not raise — should return something usable
        result = bot._local_to_utc_iso("2026-06-15", "14:00", "Invalid/Zone")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_returns_string(self):
        result = bot._local_to_utc_iso("2026-01-01", "09:00", "UTC")
        assert isinstance(result, str)
