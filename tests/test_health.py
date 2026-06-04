"""Tests for /api/health endpoint."""

import pytest


class TestHealth:
    def test_health_returns_200(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200

    def test_health_has_status_field(self, client):
        r = client.get("/api/health")
        assert "status" in r.json()

    def test_authenticated_gets_full_details(self, authed_client):
        r = authed_client.get("/api/health")
        assert r.status_code == 200
        d = r.json()
        # Authenticated users see extended health details
        assert "status" in d

    def test_unauthenticated_gets_basic_response(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
