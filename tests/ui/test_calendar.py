"""UI tests for /calendar.html."""

import pytest
from playwright.sync_api import expect


class TestCalendarPageStructure:
    def test_page_loads(self, auth_page):
        auth_page.goto("/calendar.html")
        auth_page.wait_for_selector("#calGrid, .cal-grid, #calView", timeout=10_000)

    def test_add_event_button_present(self, auth_page):
        auth_page.goto("/calendar.html")
        auth_page.wait_for_load_state("networkidle", timeout=10_000)
        # Look for add/new event button
        add_btn = auth_page.locator(
            "button:has-text('Add'), button:has-text('New'), button:has-text('+')"
        ).first
        expect(add_btn).to_be_visible()

    def test_header_nav_present(self, auth_page):
        auth_page.goto("/calendar.html")
        auth_page.wait_for_load_state("domcontentloaded")
        # Should have nav back to main pages
        expect(auth_page.locator("a[href='/'], a[href='/tracking.html']").first).to_be_visible()

    def test_user_shown_in_header(self, auth_page):
        auth_page.goto("/calendar.html")
        auth_page.wait_for_selector("#headerUser", timeout=8_000)
        expect(auth_page.locator("#headerUser")).not_to_be_empty()
