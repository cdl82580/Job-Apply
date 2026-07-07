"""
Tests for teams_bot/api_client.py — the HTTP client helpers bot.py calls.
Mirrors tests/slack/test_api_helpers.py's coverage of slack_bot.py's
equivalent functions.
"""

from unittest.mock import patch

import pytest

from tests.teams.conftest import fake_response, SAMPLE_APPS, SAMPLE_APP, SAMPLE_EVENT


@pytest.fixture(scope="module")
def api_client(bot_module):
    import api_client as _api_client
    return _api_client


# ── _api ──────────────────────────────────────────────────────────────────

class TestApiHelper:
    def test_sets_authorization_header(self, api_client):
        with patch("requests.get", return_value=fake_response(200, {})) as mock_get:
            api_client._api("get", "/api/health")
            headers = mock_get.call_args[1]["headers"]
            assert headers["Authorization"] == f"Bearer {api_client.Config.BOT_API_KEY}"

    def test_sets_teams_user_email_header_when_provided(self, api_client):
        with patch("requests.get", return_value=fake_response(200, {})) as mock_get:
            api_client._api("get", "/api/health", user_email="a@b.com")
            headers = mock_get.call_args[1]["headers"]
            assert headers["X-Teams-User-Email"] == "a@b.com"

    def test_omits_teams_user_email_header_when_absent(self, api_client):
        with patch("requests.get", return_value=fake_response(200, {})) as mock_get:
            api_client._api("get", "/api/health")
            headers = mock_get.call_args[1]["headers"]
            assert "X-Teams-User-Email" not in headers

    def test_builds_correct_url(self, api_client):
        with patch("requests.post", return_value=fake_response(200, {})) as mock_post:
            api_client._api("post", "/api/run", json={"key": "val"})
            url = mock_post.call_args[0][0]
            assert url == f"{api_client.Config.API_BASE}/api/run"

    def test_timeout_set(self, api_client):
        with patch("requests.get", return_value=fake_response(200, {})) as mock_get:
            api_client._api("get", "/api/test")
            assert mock_get.call_args[1].get("timeout") == 30


# ── Teams identity linking ──────────────────────────────────────────────────

class TestTeamsLinkStatus:
    def test_returns_linked_status(self, api_client):
        resp = fake_response(200, {"linked": True, "email": "a@b.com"})
        with patch.object(api_client, "_api", return_value=resp) as mock_api:
            result = api_client.teams_link_status("aad-1")
        assert result == {"linked": True, "email": "a@b.com"}
        assert mock_api.call_args[0][:2] == ("post", "/api/teams/link-status")
        assert mock_api.call_args[1]["json"] == {"aad_object_id": "aad-1"}


class TestTeamsAccountLookup:
    def test_returns_exists_flag(self, api_client):
        resp = fake_response(200, {"exists": True})
        with patch.object(api_client, "_api", return_value=resp):
            assert api_client.teams_account_lookup("a@b.com") == {"exists": True}


class TestTeamsLinkConfirm:
    def test_returns_linked_result(self, api_client):
        resp = fake_response(200, {"linked": True, "email": "a@b.com"})
        with patch.object(api_client, "_api", return_value=resp):
            result = api_client.teams_link_confirm("aad-1", "a@b.com")
        assert result == {"linked": True, "email": "a@b.com"}

    def test_404_returns_not_linked_without_raising(self, api_client):
        resp = fake_response(404)
        with patch.object(api_client, "_api", return_value=resp):
            result = api_client.teams_link_confirm("aad-1", "a@b.com")
        assert result == {"linked": False}

    def test_raises_on_other_errors(self, api_client):
        import requests
        resp = fake_response(500)
        with patch.object(api_client, "_api", return_value=resp):
            with pytest.raises(requests.HTTPError):
                api_client.teams_link_confirm("aad-1", "a@b.com")


class TestTeamsUnlink:
    def test_posts_aad_object_id(self, api_client):
        resp = fake_response(200, {"ok": True})
        with patch.object(api_client, "_api", return_value=resp) as mock_api:
            api_client.teams_unlink("aad-1")
        assert mock_api.call_args[1]["json"] == {"aad_object_id": "aad-1"}


class TestTeamsLinkToken:
    def test_returns_token_string(self, api_client):
        resp = fake_response(200, {"token": "tok-abc"})
        with patch.object(api_client, "_api", return_value=resp):
            assert api_client.teams_link_token("aad-1", "a@b.com") == "tok-abc"


# ── Agent runs (apply/prep/aq) ───────────────────────────────────────────────

class TestPostRun:
    def test_sends_correct_payload(self, api_client):
        resp = fake_response(200, {"run_id": "run-123"})
        with patch.object(api_client, "_api", return_value=resp) as mock_api:
            api_client.post_run("JD text", "Acme", "Engineer", contact="Jane", user_email="a@b.com")
        assert mock_api.call_args[1]["json"]["company"] == "Acme"
        assert mock_api.call_args[1]["json"]["contact"] == "Jane"
        assert mock_api.call_args[1]["user_email"] == "a@b.com"

    def test_empty_contact_sends_none(self, api_client):
        resp = fake_response(200, {"run_id": "run-123"})
        with patch.object(api_client, "_api", return_value=resp) as mock_api:
            api_client.post_run("JD", "Acme", "Engineer")
        assert mock_api.call_args[1]["json"]["contact"] is None

    def test_raises_on_error(self, api_client):
        import requests
        with patch.object(api_client, "_api", return_value=fake_response(400)):
            with pytest.raises(requests.HTTPError):
                api_client.post_run("JD", "Co", "Role")


class TestPollRun:
    def test_returns_immediately_on_done(self, api_client):
        resp = fake_response(200, {"status": "done"})
        with patch.object(api_client, "_api", return_value=resp):
            with patch("time.sleep") as mock_sleep:
                result = api_client.poll_run("run-123", timeout=60)
        assert result["status"] == "done"
        mock_sleep.assert_not_called()

    def test_polls_until_done(self, api_client):
        responses = [
            fake_response(200, {"status": "running"}),
            fake_response(200, {"status": "running"}),
            fake_response(200, {"status": "done"}),
        ]
        with patch.object(api_client, "_api", side_effect=responses):
            with patch("time.sleep"):
                result = api_client.poll_run("run-123", timeout=60)
        assert result["status"] == "done"

    def test_returns_timeout_when_exceeded(self, api_client):
        resp = fake_response(200, {"status": "running"})
        with patch.object(api_client, "_api", return_value=resp):
            with patch("time.sleep"):
                with patch("time.time", side_effect=[0, 0, 999]):
                    result = api_client.poll_run("run-123", timeout=1)
        assert result["status"] == "timeout"


class TestPostAq:
    def test_omits_char_limit_when_not_set(self, api_client):
        resp = fake_response(200, {"aq_id": "aq-1"})
        with patch.object(api_client, "_api", return_value=resp) as mock_api:
            api_client.post_aq("Why us?", "JD", "Co", "Role")
        assert "char_limit" not in mock_api.call_args[1]["json"]

    def test_includes_char_limit_when_set(self, api_client):
        resp = fake_response(200, {"aq_id": "aq-1"})
        with patch.object(api_client, "_api", return_value=resp) as mock_api:
            api_client.post_aq("Why us?", "JD", "Co", "Role", char_limit=500)
        assert mock_api.call_args[1]["json"]["char_limit"] == 500


# ── Company search ────────────────────────────────────────────────────────────

class TestSearchCompanies:
    def test_returns_results(self, api_client):
        resp = fake_response(200, [{"name": "Salesforce", "domain": "salesforce.com"}])
        with patch.object(api_client, "_api", return_value=resp) as mock_api:
            result = api_client.search_companies("sales")
        assert result[0]["name"] == "Salesforce"
        assert mock_api.call_args[1]["params"] == {"q": "sales"}


# ── Tracker (applications) ───────────────────────────────────────────────────

class TestGetApplications:
    def test_returns_items_list(self, api_client):
        resp = fake_response(200, {"items": SAMPLE_APPS})
        with patch.object(api_client, "_api", return_value=resp):
            result = api_client.get_applications()
        assert result == SAMPLE_APPS

    def test_missing_items_key_returns_empty_list(self, api_client):
        resp = fake_response(200, {})
        with patch.object(api_client, "_api", return_value=resp):
            assert api_client.get_applications() == []

    def test_passes_status_filter(self, api_client):
        resp = fake_response(200, {"items": [SAMPLE_APP]})
        with patch.object(api_client, "_api", return_value=resp) as mock_api:
            api_client.get_applications(status="Interviewing")
        assert mock_api.call_args[1]["params"] == {"status": "Interviewing"}

    def test_passes_user_email(self, api_client):
        resp = fake_response(200, {"items": []})
        with patch.object(api_client, "_api", return_value=resp) as mock_api:
            api_client.get_applications(user_email="a@b.com")
        assert mock_api.call_args[1]["user_email"] == "a@b.com"


class TestGetApplication:
    def test_returns_record(self, api_client):
        resp = fake_response(200, SAMPLE_APP)
        with patch.object(api_client, "_api", return_value=resp):
            assert api_client.get_application("app-001") == SAMPLE_APP


class TestScoreApplication:
    def test_posts_to_score_endpoint(self, api_client):
        resp = fake_response(200, {"score": 82, "category": "strong"})
        with patch.object(api_client, "_api", return_value=resp) as mock_api:
            result = api_client.score_application("app-001", user_email="a@b.com")
        assert result["score"] == 82
        assert mock_api.call_args[0] == ("post", "/api/applications/app-001/score")


# ── Calendar ─────────────────────────────────────────────────────────────────

class TestGetCalendarEvents:
    def test_passes_from_and_to_query_params(self, api_client):
        resp = fake_response(200, {"events": [SAMPLE_EVENT]})
        with patch.object(api_client, "_api", return_value=resp) as mock_api:
            api_client.get_calendar_events(from_dt="2026-07-01T00:00:00Z", to_dt="2026-07-08T00:00:00Z")
        params = mock_api.call_args[1]["params"]
        assert params["from"] == "2026-07-01T00:00:00Z"
        assert params["to"] == "2026-07-08T00:00:00Z"

    def test_omits_params_when_not_given(self, api_client):
        resp = fake_response(200, {"events": []})
        with patch.object(api_client, "_api", return_value=resp) as mock_api:
            api_client.get_calendar_events()
        assert mock_api.call_args[1]["params"] == {}

    def test_returns_events_list(self, api_client):
        resp = fake_response(200, {"events": [SAMPLE_EVENT]})
        with patch.object(api_client, "_api", return_value=resp):
            assert api_client.get_calendar_events() == [SAMPLE_EVENT]


class TestGetUpcomingEvents:
    def test_hits_upcoming_endpoint(self, api_client):
        resp = fake_response(200, {"events": [SAMPLE_EVENT]})
        with patch.object(api_client, "_api", return_value=resp) as mock_api:
            api_client.get_upcoming_events()
        assert mock_api.call_args[0] == ("get", "/api/calendar/upcoming")


# ── Profile ──────────────────────────────────────────────────────────────────

class TestUploadResume:
    def test_sends_multipart_file(self, api_client):
        resp = fake_response(200, {"ok": True})
        with patch.object(api_client, "_api", return_value=resp) as mock_api:
            api_client.upload_resume("resume.docx", b"PK\x03\x04...", user_email="a@b.com")
        files = mock_api.call_args[1]["files"]
        assert files["resume"][0] == "resume.docx"
        assert files["resume"][1] == b"PK\x03\x04..."


# ── Runs / job posting lookup ─────────────────────────────────────────────────

class TestGetJobPosting:
    def test_returns_text_when_present(self, api_client):
        resp = fake_response(200, {"job_posting": "Full JD text"})
        with patch.object(api_client, "_api", return_value=resp):
            assert api_client.get_job_posting("folder-1") == "Full JD text"

    def test_returns_none_on_404(self, api_client):
        resp = fake_response(404)
        with patch.object(api_client, "_api", return_value=resp):
            assert api_client.get_job_posting("folder-1") is None
