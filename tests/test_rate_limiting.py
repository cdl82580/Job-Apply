"""Tests for rate limiting on sensitive endpoints."""

import pytest
from unittest.mock import patch


class TestLoginRateLimit:
    def test_repeated_login_attempts_eventually_rate_limited(self, client, monkeypatch):
        """After enough failed logins, /api/auth/login should return 429."""
        # Make get_user_by_email always return None (unknown user → 401 each time)
        import scripts.storage as st
        monkeypatch.setattr(st, "get_user_by_email", lambda *a, **kw: None)

        limited = False
        for _ in range(15):  # limit is 10/min
            r = client.post("/api/auth/login", json={"email": "x@x.com", "password": "bad"})
            if r.status_code == 429:
                limited = True
                break
        assert limited, "Expected 429 after repeated login attempts"


class TestPasswordChangeRateLimit:
    def test_password_change_rate_limited(self, authed_client, monkeypatch):
        """After enough attempts, /api/profile/password should return 429."""
        limited = False
        for _ in range(10):
            r = authed_client.post(
                "/api/profile/password",
                json={"current_password": "wrong", "new_password": "newpass123"},
            )
            if r.status_code == 429:
                limited = True
                break
        assert limited, "Expected 429 after repeated password change attempts"
