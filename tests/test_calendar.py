"""Integration tests for /api/calendar — CRUD plus the ?from=&to= date-range filter."""

import uuid

EVENT_BODY = {
    "title": "HM Interview — Acme",
    "event_type": "interview",
    "datetime": "2026-06-15T14:00:00Z",
    "timezone": "America/New_York",
    "duration_minutes": 60,
}


def _create_event(client, **overrides):
    body = {**EVENT_BODY, **overrides}
    r = client.post("/api/calendar", json=body)
    assert r.status_code == 200, r.text
    return r.json()


class TestListEndpoint:
    def test_requires_auth(self, client):
        r = client.get("/api/calendar")
        assert r.status_code == 401

    def test_no_events_returns_empty_list(self, authed_client):
        r = authed_client.get("/api/calendar")
        assert r.status_code == 200
        assert r.json() == {"events": []}

    def test_lists_created_event(self, authed_client):
        _create_event(authed_client)
        r = authed_client.get("/api/calendar")
        assert r.status_code == 200
        events = r.json()["events"]
        assert len(events) == 1
        assert events[0]["title"] == EVENT_BODY["title"]


class TestDateRangeFilter:
    """Regression coverage for the from_dt/to_dt <-> ?from=&to= alias bug
    (fixed in a1d5e79) — the endpoint's param names didn't match what every
    caller (Slack, Teams, frontend/calendar.html) actually sends, so the
    filter silently never applied and every event came back unfiltered."""

    def test_from_excludes_earlier_events(self, authed_client):
        _create_event(authed_client, title="Early", datetime="2026-06-01T00:00:00Z")
        _create_event(authed_client, title="Late", datetime="2026-06-20T00:00:00Z")
        r = authed_client.get("/api/calendar", params={"from": "2026-06-10T00:00:00Z"})
        titles = [e["title"] for e in r.json()["events"]]
        assert titles == ["Late"]

    def test_to_excludes_later_events(self, authed_client):
        _create_event(authed_client, title="Early", datetime="2026-06-01T00:00:00Z")
        _create_event(authed_client, title="Late", datetime="2026-06-20T00:00:00Z")
        r = authed_client.get("/api/calendar", params={"to": "2026-06-10T00:00:00Z"})
        titles = [e["title"] for e in r.json()["events"]]
        assert titles == ["Early"]

    def test_from_and_to_bound_a_window(self, authed_client):
        _create_event(authed_client, title="Before", datetime="2026-06-01T00:00:00Z")
        _create_event(authed_client, title="Inside", datetime="2026-06-10T00:00:00Z")
        _create_event(authed_client, title="After", datetime="2026-06-20T00:00:00Z")
        r = authed_client.get(
            "/api/calendar",
            params={"from": "2026-06-05T00:00:00Z", "to": "2026-06-15T00:00:00Z"},
        )
        titles = [e["title"] for e in r.json()["events"]]
        assert titles == ["Inside"]

    def test_from_dt_query_name_is_not_the_real_param(self, authed_client):
        """The bug: the endpoint used to accept from_dt/to_dt as the actual
        query names (matching the Python parameter, not the alias). Sending
        the wrong (pre-fix) name must NOT filter anything."""
        _create_event(authed_client, title="Early", datetime="2026-06-01T00:00:00Z")
        _create_event(authed_client, title="Late", datetime="2026-06-20T00:00:00Z")
        r = authed_client.get("/api/calendar", params={"from_dt": "2026-06-10T00:00:00Z"})
        titles = sorted(e["title"] for e in r.json()["events"])
        assert titles == ["Early", "Late"]

    def test_invalid_from_returns_400(self, authed_client):
        r = authed_client.get("/api/calendar", params={"from": "not-a-date"})
        assert r.status_code == 400

    def test_invalid_to_returns_400(self, authed_client):
        r = authed_client.get("/api/calendar", params={"to": "not-a-date"})
        assert r.status_code == 400


class TestUpcomingEndpoint:
    def test_requires_auth(self, client):
        r = client.get("/api/calendar/upcoming")
        assert r.status_code == 401

    def test_excludes_events_outside_next_7_days(self, authed_client):
        _create_event(authed_client, title="Far future", datetime="2030-01-01T00:00:00Z")
        r = authed_client.get("/api/calendar/upcoming")
        assert r.json()["events"] == []


class TestCreateEndpoint:
    def test_requires_auth(self, client):
        r = client.post("/api/calendar", json=EVENT_BODY)
        assert r.status_code == 401

    def test_creates_and_returns_event(self, authed_client):
        ev = _create_event(authed_client)
        assert ev["title"] == EVENT_BODY["title"]
        assert uuid.UUID(ev["id"])

    def test_rejects_empty_title(self, authed_client):
        r = authed_client.post("/api/calendar", json={**EVENT_BODY, "title": "   "})
        assert r.status_code == 422

    def test_rejects_invalid_event_type(self, authed_client):
        r = authed_client.post("/api/calendar", json={**EVENT_BODY, "event_type": "not-a-type"})
        assert r.status_code == 422

    def test_rejects_invalid_datetime(self, authed_client):
        r = authed_client.post("/api/calendar", json={**EVENT_BODY, "datetime": "not-a-date"})
        assert r.status_code == 422

    def test_unknown_app_id_returns_422(self, authed_client):
        r = authed_client.post("/api/calendar", json={**EVENT_BODY, "app_id": str(uuid.uuid4())})
        assert r.status_code == 422


class TestGetEndpoint:
    def test_requires_auth(self, client):
        r = client.get(f"/api/calendar/{uuid.uuid4()}")
        assert r.status_code == 401

    def test_returns_created_event(self, authed_client):
        ev = _create_event(authed_client)
        r = authed_client.get(f"/api/calendar/{ev['id']}")
        assert r.status_code == 200
        assert r.json()["id"] == ev["id"]

    def test_unknown_event_returns_404(self, authed_client):
        r = authed_client.get(f"/api/calendar/{uuid.uuid4()}")
        assert r.status_code == 404

    def test_invalid_id_format_returns_400(self, authed_client):
        r = authed_client.get("/api/calendar/not-a-uuid")
        assert r.status_code == 400


class TestUpdateEndpoint:
    def test_updates_title(self, authed_client):
        ev = _create_event(authed_client)
        r = authed_client.put(f"/api/calendar/{ev['id']}", json={"title": "Rescheduled"})
        assert r.status_code == 200
        assert r.json()["title"] == "Rescheduled"

    def test_unknown_event_returns_404(self, authed_client):
        r = authed_client.put(f"/api/calendar/{uuid.uuid4()}", json={"title": "X"})
        assert r.status_code == 404


class TestDeleteEndpoint:
    def test_deletes_event(self, authed_client):
        ev = _create_event(authed_client)
        r = authed_client.delete(f"/api/calendar/{ev['id']}")
        assert r.status_code == 200
        r2 = authed_client.get(f"/api/calendar/{ev['id']}")
        assert r2.status_code == 404

    def test_unknown_event_returns_404(self, authed_client):
        r = authed_client.delete(f"/api/calendar/{uuid.uuid4()}")
        assert r.status_code == 404
