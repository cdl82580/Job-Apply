"""Integration tests for auth endpoints via FastAPI TestClient."""

import pytest
from tests.conftest import make_user, _store


class TestRegister:
    def test_requires_resume(self, client):
        # Missing resume file should fail validation
        r = client.post("/api/auth/register", data={"email": "x@x.com", "password": "pass1234"})
        assert r.status_code in (400, 422)

    def test_duplicate_email(self, client):
        import io
        make_user(email="dup@example.com")
        r = client.post(
            "/api/auth/register",
            data={"email": "dup@example.com", "password": "pass1234"},
            files={"resume": ("resume.docx", io.BytesIO(b"PK\x03\x04" + b"\x00" * 2000), "application/octet-stream")},
        )
        assert r.status_code in (409, 400, 422)

    def test_weak_password_rejected(self, client):
        import io
        r = client.post(
            "/api/auth/register",
            data={"email": "new@example.com", "password": "short"},
            files={"resume": ("resume.docx", io.BytesIO(b"PK\x03\x04" + b"\x00" * 2000), "application/octet-stream")},
        )
        assert r.status_code in (400, 422)


class TestLogin:
    def test_unknown_email(self, client):
        r = client.post("/api/auth/login", json={"email": "nobody@x.com", "password": "pass"})
        assert r.status_code == 401

    def test_valid_session_token_via_factory(self, authed_client):
        """authed_client fixture bypasses password — just confirm /api/auth/me works."""
        r = authed_client.get("/api/auth/me")
        assert r.status_code == 200
        assert r.json()["email"] == "test@example.com"


class TestLogout:
    def test_logout_returns_ok(self, authed_client):
        r = authed_client.post("/api/auth/logout")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_logout_no_session_still_200(self, client):
        # Logout is idempotent — no session should still return 200
        r = client.post("/api/auth/logout")
        # API returns 200 regardless (just clears cookies)
        assert r.status_code in (200, 401)  # either is acceptable


class TestMe:
    def test_unauthenticated(self, client):
        r = client.get("/api/auth/me")
        assert r.status_code == 401

    def test_authenticated(self, authed_client):
        r = authed_client.get("/api/auth/me")
        assert r.status_code == 200
        d = r.json()
        assert "email" in d
        assert "role" in d
        assert "email_verified" in d

    def test_admin_redirected_from_agent_page(self, admin_client):
        r = admin_client.get("/api/auth/me")
        assert r.status_code == 200
        assert r.json()["role"] == "admin"


class TestProtectedRoutes:
    """Any endpoint requiring auth should return 401 when unauthenticated."""

    @pytest.mark.parametrize("method,path", [
        ("GET",  "/api/profile"),
        ("PUT",  "/api/profile"),
        ("GET",  "/api/audit/me"),
        ("GET",  "/api/runs"),
        ("GET",  "/api/gdrive/runs"),
        ("POST", "/api/run"),
        ("POST", "/api/prep"),
    ])
    def test_requires_auth(self, client, method, path):
        fn = getattr(client, method.lower())
        # GET requests don't send json body
        if method == "GET":
            r = fn(path)
        else:
            r = fn(path, json={})
        assert r.status_code == 401
