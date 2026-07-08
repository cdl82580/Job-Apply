"""Tests for routers/applications.py — run-linking authorization (IDOR guard)."""

import uuid
from unittest.mock import patch

from scripts.applications import save_application


def _seed_app(user_record) -> str:
    app_id = str(uuid.uuid4())
    save_application(user_record["user_id"], {
        "id":         app_id,
        "company":    "Acme",
        "role_title": "Software Engineer",
        "status":     "Applied",
        "created_at": "2026-01-01T00:00:00Z",
    })
    return app_id


class TestLinkRun:
    def test_requires_auth(self, client):
        r = client.post("/api/applications/some-app/runs", json={"type": "resume"})
        assert r.status_code == 401

    def test_missing_app_404s(self, authed_client):
        r = authed_client.post("/api/applications/does-not-exist/runs",
                               json={"type": "resume", "folder_name": "x"})
        assert r.status_code == 404

    def test_no_folder_id_allowed_without_drive_check(self, authed_client, user_record):
        """A run link with no gdrive_folder_id (e.g. a non-Drive run type) needs no verification."""
        app_id = _seed_app(user_record)
        r = authed_client.post(f"/api/applications/{app_id}/runs", json={
            "type": "resume", "folder_name": "", "folder_url": "", "gdrive_folder_id": "",
        })
        assert r.status_code == 201

    def test_foreign_folder_id_rejected(self, authed_client, user_record):
        """A folder id that isn't in the caller's own Drive listing must be rejected —
        otherwise any user could link (and thereby gain read/write access to) another
        user's Drive folder just by supplying its id."""
        app_id = _seed_app(user_record)
        with patch("apply.list_gdrive_run_folders", return_value=[{"id": "some-other-folder"}]):
            r = authed_client.post(f"/api/applications/{app_id}/runs", json={
                "type": "resume", "folder_name": "x", "folder_url": "",
                "gdrive_folder_id": "not-mine",
            })
        assert r.status_code == 403

    def test_owned_folder_id_accepted(self, authed_client, user_record):
        app_id = _seed_app(user_record)
        with patch("apply.list_gdrive_run_folders", return_value=[{"id": "my-folder-123"}]):
            r = authed_client.post(f"/api/applications/{app_id}/runs", json={
                "type": "resume", "folder_name": "x", "folder_url": "",
                "gdrive_folder_id": "my-folder-123",
            })
        assert r.status_code == 201
        assert r.json()["gdrive_folder_id"] == "my-folder-123"

    def test_drive_lookup_failure_rejects_rather_than_silently_allows(self, authed_client, user_record):
        app_id = _seed_app(user_record)
        with patch("apply.list_gdrive_run_folders", side_effect=RuntimeError("drive down")):
            r = authed_client.post(f"/api/applications/{app_id}/runs", json={
                "type": "resume", "folder_name": "x", "folder_url": "",
                "gdrive_folder_id": "some-folder",
            })
        assert r.status_code == 503
