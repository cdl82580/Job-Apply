"""
Playwright UI test configuration.

Tests run against a configurable base URL (defaults to live app).
Set environment variables to control behavior:

    UI_BASE_URL      — target (default: https://apply.cdlav.us)
    UI_TEST_EMAIL    — test account email
    UI_TEST_PASSWORD — test account password

For CI / automated runs against the live app, set these as secrets.
For local dev, they can also be set to point at a locally-running server.

Usage:
    pytest tests/ui/                              # run all UI tests
    pytest tests/ui/ --headed                     # see the browser
    pytest tests/ui/ --slowmo=500                 # slow down for debugging
    UI_BASE_URL=http://localhost:8000 pytest tests/ui/
"""

import os
import json
import pytest
import requests as _req
from playwright.sync_api import Page, BrowserContext, expect

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL       = os.environ.get("UI_BASE_URL",       "https://apply.cdlav.us").rstrip("/")
TEST_EMAIL     = os.environ.get("UI_TEST_EMAIL",     "cdl825+testuser@gmail.com")
TEST_PASSWORD  = os.environ.get("UI_TEST_PASSWORD",  "")
ADMIN_EMAIL    = os.environ.get("UI_ADMIN_EMAIL",    "cdl825+testadmin@gmail.com")
ADMIN_PASSWORD = os.environ.get("UI_ADMIN_PASSWORD", TEST_PASSWORD)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    """Default viewport and timeout for all UI tests."""
    return {
        **browser_context_args,
        "viewport": {"width": 1280, "height": 900},
        "base_url": BASE_URL,
    }


def _apply_timeouts(page):
    page.set_default_navigation_timeout(60_000)
    page.set_default_timeout(15_000)


@pytest.fixture(scope="session")
def authenticated_state(browser, base_url):
    """
    Log in once per session and return the saved storage state (cookies + localStorage).
    All tests that need auth share this session, avoiding repeated logins.
    """
    if not TEST_PASSWORD:
        pytest.skip("UI_TEST_PASSWORD not set — skipping authenticated UI tests")

    context = browser.new_context(base_url=base_url, viewport={"width": 1280, "height": 900})
    page = context.new_page()

    page.goto("/login.html")
    page.fill("#email",    TEST_EMAIL)
    page.fill("#password", TEST_PASSWORD)
    page.click("#submitBtn")

    # Wait for successful redirect away from login
    page.wait_for_url(lambda url: "login" not in url, timeout=15_000)

    state = context.storage_state()
    context.close()
    return state


@pytest.fixture()
def auth_page(browser, base_url, authenticated_state):
    """A Page that already has a valid session cookie."""
    context = browser.new_context(
        base_url=base_url,
        viewport={"width": 1280, "height": 900},
        storage_state=authenticated_state,
    )
    page = context.new_page()
    _apply_timeouts(page)
    yield page
    context.close()


@pytest.fixture()
def anon_page(browser, base_url):
    """A Page with no session (anonymous)."""
    context = browser.new_context(base_url=base_url, viewport={"width": 1280, "height": 900})
    page = context.new_page()
    _apply_timeouts(page)
    yield page
    context.close()


@pytest.fixture(scope="session")
def admin_authenticated_state(browser, base_url):
    """
    Log in as admin once per session.
    Requires UI_ADMIN_EMAIL + UI_ADMIN_PASSWORD (or UI_TEST_PASSWORD) env vars.
    """
    if not ADMIN_PASSWORD:
        pytest.skip("UI_ADMIN_PASSWORD not set — skipping admin UI tests")

    context = browser.new_context(base_url=base_url, viewport={"width": 1280, "height": 900})
    page = context.new_page()

    page.goto("/login.html")
    page.fill("#email",    ADMIN_EMAIL)
    page.fill("#password", ADMIN_PASSWORD)
    page.click("#submitBtn")

    # Admins are redirected to /admin.html
    try:
        page.wait_for_url(lambda url: "admin" in url or "login" not in url, timeout=15_000)
    except Exception:
        pytest.skip("Admin login failed — check UI_ADMIN_EMAIL / UI_ADMIN_PASSWORD")

    if "login" in page.url:
        pytest.skip("Admin login failed — check credentials")

    state = context.storage_state()
    context.close()
    return state


@pytest.fixture()
def admin_page(browser, base_url, admin_authenticated_state):
    """A Page with a valid admin session."""
    context = browser.new_context(
        base_url=base_url,
        viewport={"width": 1280, "height": 900},
        storage_state=admin_authenticated_state,
    )
    page = context.new_page()
    _apply_timeouts(page)
    yield page
    context.close()


@pytest.fixture(scope="session")
def test_application(authenticated_state, base_url):
    """Create a tracked application for the test user, yield it, then delete it."""
    cookies = {c["name"]: c["value"] for c in authenticated_state["cookies"]}
    headers = {"Content-Type": "application/json"}
    url = f"{base_url}/api/applications"

    resp = _req.post(url, cookies=cookies, headers=headers, json={
        "company": "_UITest Corp",
        "role_title": "Test Engineer",
        "status": "Researching",
    }, timeout=15)
    if resp.status_code != 201:
        pytest.skip(f"Could not create test application: {resp.status_code}")

    app_data = resp.json()
    yield app_data

    _req.delete(f"{url}/{app_data['id']}", cookies=cookies, timeout=15)
