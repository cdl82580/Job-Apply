"""Integration tests for /api/run and /api/prep endpoints."""

import io
import uuid
import pytest
from unittest.mock import MagicMock, patch
from tests.conftest import _store


def _seed_user_with_resume(user_record):
    """Give the test user a resume and profile so the run/prep endpoints don't 400."""
    fake_docx = b"PK\x03\x04" + b"\x00" * 100
    _store.save_resume(user_record["user_id"], fake_docx)
    _store.save_profile(user_record["user_id"], "# Profile\n\nVoice guide text.")


JD = "Software Engineer at Acme. Must know Python, APIs, and CI/CD. 5+ years experience."
RUN_BODY = {
    "job_posting": JD,
    "company": "Acme",
    "role": "Software Engineer",
}
PREP_BODY = {
    "job_posting": JD,
    "company": "Acme",
    "role": "Software Engineer",
    "round_type": "Hiring Manager",
}


class TestRunEndpoint:
    def test_requires_auth(self, client):
        r = client.post("/api/run", json=RUN_BODY)
        assert r.status_code == 401

    def test_requires_resume(self, authed_client):
        r = authed_client.post("/api/run", json=RUN_BODY)
        assert r.status_code == 400
        assert "resume" in r.json()["detail"].lower()

    def test_requires_company_and_role(self, authed_client, user_record):
        _seed_user_with_resume(user_record)
        # Missing required fields should fail pydantic validation
        body = {"job_posting": JD}  # no company or role
        r = authed_client.post("/api/run", json=body)
        assert r.status_code == 422

    def test_returns_run_id_and_machine_id(self, authed_client, user_record):
        _seed_user_with_resume(user_record)
        # Mock the background thread so it doesn't actually run apply.py
        with patch("api.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            r = authed_client.post("/api/run", json=RUN_BODY)
        assert r.status_code == 200
        d = r.json()
        assert "run_id" in d
        assert "machine_id" in d
        assert uuid.UUID(d["run_id"])  # valid UUID

    def test_run_id_in_store(self, authed_client, user_record):
        _seed_user_with_resume(user_record)
        with patch("api.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            r = authed_client.post("/api/run", json=RUN_BODY)
        run_id = r.json()["run_id"]
        # Status endpoint should find the run
        r2 = authed_client.get(f"/api/run/{run_id}/status")
        assert r2.status_code == 200
        assert r2.json()["run_id"] == run_id

    def test_status_unknown_run(self, authed_client):
        r = authed_client.get(f"/api/run/{uuid.uuid4()}/status")
        assert r.status_code == 404

    def test_admin_blocked_from_running(self, admin_client, admin_record):
        _seed_user_with_resume(admin_record)
        r = admin_client.post("/api/run", json=RUN_BODY)
        assert r.status_code == 403


class TestPrepEndpoint:
    def test_requires_auth(self, client):
        r = client.post("/api/prep", json=PREP_BODY)
        assert r.status_code == 401

    def test_requires_resume(self, authed_client):
        r = authed_client.post("/api/prep", json=PREP_BODY)
        assert r.status_code == 400

    def test_requires_profile(self, authed_client, user_record):
        _store.save_resume(user_record["user_id"], b"PK\x03\x04" + b"\x00" * 100)
        # No profile saved
        r = authed_client.post("/api/prep", json=PREP_BODY)
        assert r.status_code == 400
        assert "profile" in r.json()["detail"].lower()

    def test_invalid_round_type(self, authed_client, user_record):
        _seed_user_with_resume(user_record)
        body = {**PREP_BODY, "round_type": "InvalidRound"}
        r = authed_client.post("/api/prep", json=body)
        assert r.status_code == 400

    def test_returns_prep_id_and_machine_id(self, authed_client, user_record):
        _seed_user_with_resume(user_record)
        with patch("api.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            r = authed_client.post("/api/prep", json=PREP_BODY)
        assert r.status_code == 200
        d = r.json()
        assert "prep_id" in d
        assert "machine_id" in d

    def test_prep_status_found(self, authed_client, user_record):
        _seed_user_with_resume(user_record)
        with patch("api.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            r = authed_client.post("/api/prep", json=PREP_BODY)
        prep_id = r.json()["prep_id"]
        r2 = authed_client.get(f"/api/prep/{prep_id}/status")
        assert r2.status_code == 200

    def test_admin_blocked(self, admin_client, admin_record):
        _seed_user_with_resume(admin_record)
        r = admin_client.post("/api/prep", json=PREP_BODY)
        assert r.status_code == 403

    @pytest.mark.parametrize("round_type", [
        "Phone Screen", "Hiring Manager", "Peer",
        "Technical", "Executive", "Panel",
    ])
    def test_all_valid_round_types_accepted(self, authed_client, user_record, round_type):
        _seed_user_with_resume(user_record)
        with patch("api.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            body = {**PREP_BODY, "round_type": round_type}
            r = authed_client.post("/api/prep", json=body)
        assert r.status_code == 200

    def test_interviewer_and_logistics_fields_reach_prep_config(self, authed_client, user_record):
        """interviewer/interview_date/interview_time/location should be threaded
        through to the InterviewPrepConfig used to build the doc."""
        _seed_user_with_resume(user_record)
        body = {
            **PREP_BODY,
            "interviewer": "Jane Smith - VP Eng\nJohn Doe - Peer",
            "interview_date": "2026-07-20",
            "interview_time": "14:00",
            "location": "Zoom: https://zoom.us/j/123",
        }
        with patch("api.threading.Thread") as mock_thread, \
             patch("api.generate_interview_prep") as mock_gen:
            mock_thread.return_value = MagicMock()
            r = authed_client.post("/api/prep", json=body)
            assert r.status_code == 200

            # Grab the _prep_fn closure passed to the background worker and
            # invoke it directly, with generate_interview_prep mocked out.
            _worker_args = mock_thread.call_args[1]["args"]
            prep_fn = _worker_args[5]
            mock_gen.return_value = MagicMock(run_dir="/tmp/x", folder_url=None,
                                               prep_path=MagicMock(name="prep.docx"))
            prep_fn(resume_path="/tmp/resume.docx", progress=lambda *_: None)

        _, kwargs = mock_gen.call_args
        config = kwargs["config"]
        assert config.interviewer == "Jane Smith - VP Eng\nJohn Doe - Peer"
        assert config.interview_date == "2026-07-20"
        assert config.interview_time == "14:00"
        assert config.location == "Zoom: https://zoom.us/j/123"

    def test_logistics_fields_default_to_empty_string(self, authed_client, user_record):
        _seed_user_with_resume(user_record)
        with patch("api.threading.Thread") as mock_thread, \
             patch("api.generate_interview_prep") as mock_gen:
            mock_thread.return_value = MagicMock()
            r = authed_client.post("/api/prep", json=PREP_BODY)
            assert r.status_code == 200

            _worker_args = mock_thread.call_args[1]["args"]
            prep_fn = _worker_args[5]
            mock_gen.return_value = MagicMock(run_dir="/tmp/x", folder_url=None,
                                               prep_path=MagicMock(name="prep.docx"))
            prep_fn(resume_path="/tmp/resume.docx", progress=lambda *_: None)

        config = mock_gen.call_args.kwargs["config"]
        assert config.interviewer == ""
        assert config.interview_date == ""
        assert config.interview_time == ""
        assert config.location == ""

    def test_app_id_domain_reaches_prep_config(self, authed_client, user_record):
        """Regression test: brand color/logo lookups used to always do a fuzzy
        company-name search, which can resolve to the wrong company for an
        ambiguous name. When the request has an app_id, the domain already
        stored on that tracked application record should be used instead."""
        from scripts.applications import save_application
        _seed_user_with_resume(user_record)
        app_id = str(uuid.uuid4())
        save_application(user_record["user_id"], {
            "id": app_id, "company": "Melior", "role_title": "Engineer",
            "status": "Applied", "domain": "getmelior.com",
        })
        with patch("api.threading.Thread") as mock_thread, \
             patch("api.generate_interview_prep") as mock_gen:
            mock_thread.return_value = MagicMock()
            body = {**PREP_BODY, "company": "Melior", "app_id": app_id}
            r = authed_client.post("/api/prep", json=body)
            assert r.status_code == 200

            _worker_args = mock_thread.call_args[1]["args"]
            prep_fn = _worker_args[5]
            mock_gen.return_value = MagicMock(run_dir="/tmp/x", folder_url=None,
                                               prep_path=MagicMock(name="prep.docx"))
            prep_fn(resume_path="/tmp/resume.docx", progress=lambda *_: None)

        config = mock_gen.call_args.kwargs["config"]
        assert config.domain == "getmelior.com"

    def test_explicit_domain_takes_priority_over_app_record(self, authed_client, user_record):
        from scripts.applications import save_application
        _seed_user_with_resume(user_record)
        app_id = str(uuid.uuid4())
        save_application(user_record["user_id"], {
            "id": app_id, "company": "Melior", "role_title": "Engineer",
            "status": "Applied", "domain": "wrong-domain.com",
        })
        with patch("api.threading.Thread") as mock_thread, \
             patch("api.generate_interview_prep") as mock_gen:
            mock_thread.return_value = MagicMock()
            body = {**PREP_BODY, "company": "Melior", "app_id": app_id, "domain": "getmelior.com"}
            r = authed_client.post("/api/prep", json=body)
            assert r.status_code == 200

            _worker_args = mock_thread.call_args[1]["args"]
            prep_fn = _worker_args[5]
            mock_gen.return_value = MagicMock(run_dir="/tmp/x", folder_url=None,
                                               prep_path=MagicMock(name="prep.docx"))
            prep_fn(resume_path="/tmp/resume.docx", progress=lambda *_: None)

        config = mock_gen.call_args.kwargs["config"]
        assert config.domain == "getmelior.com"


THANKYOU_BODY = {
    "job_posting": JD,
    "company": "Acme",
    "role": "Software Engineer",
    "round_type": "Phone Screen",
    "tone": "professional",
}


class TestThankYouEndpoint:
    def test_requires_auth(self, client):
        r = client.post("/api/thankyou", json=THANKYOU_BODY)
        assert r.status_code == 401

    def test_requires_resume(self, authed_client):
        r = authed_client.post("/api/thankyou", json=THANKYOU_BODY)
        assert r.status_code == 400
        assert "resume" in r.json()["detail"].lower()

    def test_requires_profile(self, authed_client, user_record):
        _store.save_resume(user_record["user_id"], b"PK\x03\x04" + b"\x00" * 100)
        r = authed_client.post("/api/thankyou", json=THANKYOU_BODY)
        assert r.status_code == 400
        assert "profile" in r.json()["detail"].lower()

    def test_invalid_tone_rejected(self, authed_client, user_record):
        _seed_user_with_resume(user_record)
        body = {**THANKYOU_BODY, "tone": "aggressive"}
        r = authed_client.post("/api/thankyou", json=body)
        assert r.status_code == 400
        assert "tone" in r.json()["detail"].lower()

    @pytest.mark.parametrize("tone", ["professional", "conversational", "concise"])
    def test_valid_tones_accepted(self, authed_client, user_record, tone):
        _seed_user_with_resume(user_record)
        with patch("api.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            body = {**THANKYOU_BODY, "tone": tone}
            r = authed_client.post("/api/thankyou", json=body)
        assert r.status_code == 200

    def test_returns_ty_id_and_machine_id(self, authed_client, user_record):
        _seed_user_with_resume(user_record)
        with patch("api.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            r = authed_client.post("/api/thankyou", json=THANKYOU_BODY)
        assert r.status_code == 200
        d = r.json()
        assert "ty_id" in d
        assert "machine_id" in d
        assert uuid.UUID(d["ty_id"])

    def test_admin_blocked(self, admin_client, admin_record):
        _seed_user_with_resume(admin_record)
        r = admin_client.post("/api/thankyou", json=THANKYOU_BODY)
        assert r.status_code == 403

    def test_corrupt_resume_rejected(self, authed_client, user_record):
        _store.save_resume(user_record["user_id"], b"NOT_A_ZIP" + b"\x00" * 100)
        _store.save_profile(user_record["user_id"], "# Profile\n\nVoice guide text.")
        r = authed_client.post("/api/thankyou", json=THANKYOU_BODY)
        assert r.status_code == 400
        assert "corrupt" in r.json()["detail"].lower()

    def test_status_unknown_ty(self, authed_client):
        r = authed_client.get(f"/api/thankyou/{uuid.uuid4()}/status")
        assert r.status_code == 404


AQ_BODY = {
    "question": "Why do you want to work here?",
    "job_posting": JD,
    "company": "Acme",
    "role": "Software Engineer",
    "tone": "professional",
}


class TestAQEndpoint:
    def test_requires_auth(self, client):
        r = client.post("/api/aq", json=AQ_BODY)
        assert r.status_code == 401

    def test_requires_resume(self, authed_client):
        r = authed_client.post("/api/aq", json=AQ_BODY)
        assert r.status_code == 400
        assert "resume" in r.json()["detail"].lower()

    def test_requires_profile(self, authed_client, user_record):
        _store.save_resume(user_record["user_id"], b"PK\x03\x04" + b"\x00" * 100)
        r = authed_client.post("/api/aq", json=AQ_BODY)
        assert r.status_code == 400
        assert "profile" in r.json()["detail"].lower()

    def test_invalid_tone_rejected(self, authed_client, user_record):
        _seed_user_with_resume(user_record)
        body = {**AQ_BODY, "tone": "aggressive"}
        r = authed_client.post("/api/aq", json=body)
        assert r.status_code == 400
        assert "tone" in r.json()["detail"].lower()

    @pytest.mark.parametrize("tone", ["professional", "conversational", "technical", "concise"])
    def test_valid_tones_accepted(self, authed_client, user_record, tone):
        _seed_user_with_resume(user_record)
        with patch("api.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            body = {**AQ_BODY, "tone": tone}
            r = authed_client.post("/api/aq", json=body)
        assert r.status_code == 200

    def test_returns_aq_id_and_machine_id(self, authed_client, user_record):
        _seed_user_with_resume(user_record)
        with patch("api.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            r = authed_client.post("/api/aq", json=AQ_BODY)
        assert r.status_code == 200
        d = r.json()
        assert "aq_id" in d
        assert "machine_id" in d
        assert uuid.UUID(d["aq_id"])

    def test_admin_blocked(self, admin_client, admin_record):
        _seed_user_with_resume(admin_record)
        r = admin_client.post("/api/aq", json=AQ_BODY)
        assert r.status_code == 403

    def test_corrupt_resume_rejected(self, authed_client, user_record):
        _store.save_resume(user_record["user_id"], b"NOT_A_ZIP" + b"\x00" * 100)
        _store.save_profile(user_record["user_id"], "# Profile\n\nVoice guide text.")
        r = authed_client.post("/api/aq", json=AQ_BODY)
        assert r.status_code == 400
        assert "corrupt" in r.json()["detail"].lower()

    def test_status_unknown_aq(self, authed_client):
        r = authed_client.get(f"/api/aq/{uuid.uuid4()}/status")
        assert r.status_code == 404

    def test_requires_question_field(self, authed_client, user_record):
        _seed_user_with_resume(user_record)
        body = {k: v for k, v in AQ_BODY.items() if k != "question"}
        r = authed_client.post("/api/aq", json=body)
        assert r.status_code == 422
