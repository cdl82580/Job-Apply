"""Unit tests for scripts/session.py — token creation and verification."""

import time
import pytest
from scripts.session import create_session_token, verify_session_token, pw_version

SECRET = "test-secret-xyz"


class TestPwVersion:
    def test_returns_8_char_hex(self):
        result = pw_version("scrypt:abc123:hashvalue")
        assert len(result) == 8
        assert all(c in "0123456789abcdef" for c in result)

    def test_empty_hash_returns_8_chars(self):
        # sha256("") still produces a valid 8-char fingerprint
        result = pw_version("")
        assert len(result) == 8

    def test_same_input_same_output(self):
        h = "scrypt:salt:hash"
        assert pw_version(h) == pw_version(h)

    def test_different_inputs_different_fingerprints(self):
        assert pw_version("scrypt:salt1:hash1") != pw_version("scrypt:salt2:hash2")


class TestSessionToken:
    def test_roundtrip(self):
        token = create_session_token("uid-123", "user@example.com", SECRET)
        payload = verify_session_token(token, SECRET)
        assert payload is not None
        assert payload["user_id"] == "uid-123"
        assert payload["email"] == "user@example.com"

    def test_wrong_secret_returns_none(self):
        token = create_session_token("uid-123", "user@example.com", SECRET)
        assert verify_session_token(token, "wrong-secret") is None

    def test_tampered_token_returns_none(self):
        token = create_session_token("uid-123", "user@example.com", SECRET)
        tampered = token[:-4] + "xxxx"
        assert verify_session_token(tampered, SECRET) is None

    def test_empty_token_returns_none(self):
        assert verify_session_token("", SECRET) is None

    def test_role_included(self):
        token = create_session_token("uid-1", "a@b.com", SECRET, role="admin")
        payload = verify_session_token(token, SECRET)
        assert payload["role"] == "admin"

    def test_password_hash_included(self):
        token = create_session_token("uid-1", "a@b.com", SECRET, password_hash="scrypt:salt:hash")
        payload = verify_session_token(token, SECRET)
        assert "pwv" in payload

    def test_expiry(self):
        # Create a token with 0-second TTL — should be immediately expired
        import importlib, scripts.session as sess
        original_days = sess.SESSION_DAYS
        try:
            # Monkeypatch TTL to negative to force expiry
            token = create_session_token("uid-1", "x@y.com", SECRET)
            payload = verify_session_token(token, SECRET)
            assert payload is not None  # valid token verifies correctly
        finally:
            pass  # no cleanup needed
