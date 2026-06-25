"""Integration tests for admin-only endpoints."""

import pytest
from tests.conftest import make_user, _store


class TestAdminGate:
    """Non-admin users must be blocked from all /api/admin/* endpoints."""

    @pytest.mark.parametrize("path", [
        "/api/admin/users",
        "/api/admin/applications",
        "/api/admin/runs",
        "/api/admin/audit",
        "/api/admin/webhooks",
    ])
    def test_user_cannot_access_admin(self, authed_client, path):
        r = authed_client.get(path)
        assert r.status_code == 403

    @pytest.mark.parametrize("path", [
        "/api/admin/users",
        "/api/admin/applications",
        "/api/admin/runs",
        "/api/admin/audit",
        "/api/admin/webhooks",
    ])
    def test_unauthenticated_cannot_access_admin(self, client, path):
        r = client.get(path)
        assert r.status_code == 401

    def test_admin_can_list_users(self, admin_client):
        r = admin_client.get("/api/admin/users")
        assert r.status_code == 200

    def test_admin_can_list_webhooks(self, admin_client):
        r = admin_client.get("/api/admin/webhooks")
        assert r.status_code == 200

    def test_admin_can_list_audit(self, admin_client):
        r = admin_client.get("/api/admin/audit")
        assert r.status_code == 200


class TestAdminUserManagement:
    def test_list_users_returns_list(self, admin_client):
        make_user(email="extra@example.com")
        r = admin_client.get("/api/admin/users")
        assert r.status_code == 200
        # Response is either a list or wrapped in a key
        body = r.json()
        users = body if isinstance(body, list) else body.get("users", body.get("items", []))
        assert isinstance(users, list)

    def test_update_user_role(self, admin_client, user_record):
        uid = user_record["user_id"]
        r = admin_client.put(f"/api/admin/users/{uid}", json={"role": "admin"})
        assert r.status_code == 200

    def test_update_nonexistent_user(self, admin_client):
        r = admin_client.put("/api/admin/users/does-not-exist", json={"role": "user"})
        assert r.status_code == 404


class TestWebhookCRUD:
    WEBHOOK_BODY = {
        "name": "Test Webhook",
        "url": "https://hooks.example.com/test",
        "payload_format": "generic",
        "events": ["*"],
        "secret": "my-secret",
        "active": True,
        "filter_actors": "",       # comma-separated string
        "filter_source": "",
        "filter_categories": [],
        "filter_app_id": "",
        "headers": {},
        "query_params": {},
    }

    def test_create_webhook(self, admin_client):
        r = admin_client.post("/api/admin/webhooks", json=self.WEBHOOK_BODY)
        assert r.status_code in (200, 201)
        body = r.json()
        assert "id" in body or "webhook_id" in body

    def test_ssrf_url_rejected(self, admin_client):
        body = {**self.WEBHOOK_BODY, "url": "http://127.0.0.1/steal"}
        r = admin_client.post("/api/admin/webhooks", json=body)
        assert r.status_code == 400

    def test_list_webhooks_after_create(self, admin_client):
        admin_client.post("/api/admin/webhooks", json=self.WEBHOOK_BODY)
        r = admin_client.get("/api/admin/webhooks")
        assert r.status_code == 200
        body = r.json()
        webhooks = body if isinstance(body, list) else body.get("webhooks", [])
        assert len(webhooks) >= 1

    def test_delete_webhook(self, admin_client):
        r = admin_client.post("/api/admin/webhooks", json=self.WEBHOOK_BODY)
        body = r.json()
        wid = body.get("id") or body.get("webhook_id")
        assert wid, f"No webhook id in response: {body}"
        r2 = admin_client.delete(f"/api/admin/webhooks/{wid}")
        assert r2.status_code in (200, 204)

    def test_get_webhook_redacts_secret(self, admin_client):
        r = admin_client.post("/api/admin/webhooks", json=self.WEBHOOK_BODY)
        body = r.json()
        wid = body.get("id") or body.get("webhook_id")
        r2 = admin_client.get(f"/api/admin/webhooks/{wid}")
        assert r2.status_code == 200
        # Secret should not be returned in plaintext
        assert "my-secret" not in r2.text


class TestAdminRuns:
    def test_admin_can_list_runs(self, admin_client):
        r = admin_client.get("/api/admin/runs")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list)

    def test_user_cannot_list_runs(self, authed_client):
        r = authed_client.get("/api/admin/runs")
        assert r.status_code == 403


class TestAdminAuditActionTypes:
    def test_admin_can_get_action_types(self, admin_client):
        r = admin_client.get("/api/admin/audit/action-types")
        assert r.status_code == 200
        types = r.json()
        assert isinstance(types, list)
        assert "aq_started" in types
        assert "aq_completed" in types
        assert "thankyou_started" in types
        assert "optimize_started" in types
        assert "password_reset_requested" in types
        assert "password_reset_completed" in types

    def test_user_cannot_get_action_types(self, authed_client):
        r = authed_client.get("/api/admin/audit/action-types")
        assert r.status_code == 403
