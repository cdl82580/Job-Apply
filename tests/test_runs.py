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
