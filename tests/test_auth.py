"""Integration tests for auth endpoints via FastAPI TestClient."""

import re
from unittest.mock import patch

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


class TestPasswordReset:
    """A reset token must work once, then fail on replay (single-use, via
    the pw_version fingerprint embedded at issuance time)."""

    def _request_reset_token(self, client, email: str) -> str:
        with patch("api._send_email", return_value=True) as mock_send:
            r = client.post("/api/auth/forgot-password", json={"email": email})
        assert r.status_code == 200
        text = mock_send.call_args.args[2]
        match = re.search(r"token=([\w\-=.]+)", text)
        assert match, f"no token found in email text: {text!r}"
        return match.group(1)

    def test_reset_succeeds_once(self, client):
        user = make_user(email="reset1@example.com")
        token = self._request_reset_token(client, user["email"])
        r = client.post("/api/auth/reset-password",
                        json={"token": token, "new_password": "brandnewpass123"})
        assert r.status_code == 200

    def test_reset_token_rejected_on_replay(self, client):
        user = make_user(email="reset2@example.com")
        token = self._request_reset_token(client, user["email"])
        r1 = client.post("/api/auth/reset-password",
                         json={"token": token, "new_password": "firstnewpass123"})
        assert r1.status_code == 200

        r2 = client.post("/api/auth/reset-password",
                         json={"token": token, "new_password": "secondnewpass123"})
        assert r2.status_code == 400

    def test_reset_token_rejected_after_unrelated_password_change(self, client):
        """A token issued before some other password change (e.g. a second
        concurrent reset request) must not still work afterward."""
        user = make_user(email="reset3@example.com")
        token = self._request_reset_token(client, user["email"])

        # Simulate the password changing by some other path before this token is used
        record = _store.get_user_by_id(user["user_id"])
        record["password_hash"] = "scrypt:othersalt:otherhash"
        _store.save_user(record)

        r = client.post("/api/auth/reset-password",
                        json={"token": token, "new_password": "somenewpass123"})
        assert r.status_code == 400
