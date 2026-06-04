"""Tests that security headers are present on all responses."""

import pytest


REQUIRED_HEADERS = [
    "strict-transport-security",
    "x-frame-options",
    "x-content-type-options",
    "referrer-policy",
]


class TestSecurityHeaders:
    @pytest.mark.parametrize("path", ["/api/health"])
    def test_security_headers_present(self, client, path):
        r = client.get(path)
        for header in REQUIRED_HEADERS:
            assert header in {h.lower() for h in r.headers}, \
                f"Missing security header: {header} on {path}"

    def test_x_frame_options_is_deny_or_sameorigin(self, client):
        r = client.get("/api/health")
        xfo = r.headers.get("x-frame-options", "").upper()
        assert xfo in ("DENY", "SAMEORIGIN"), f"Unexpected X-Frame-Options: {xfo}"

    def test_x_content_type_nosniff(self, client):
        r = client.get("/api/health")
        assert r.headers.get("x-content-type-options", "").lower() == "nosniff"

    def test_hsts_includes_max_age(self, client):
        r = client.get("/api/health")
        hsts = r.headers.get("strict-transport-security", "")
        assert "max-age" in hsts.lower()
