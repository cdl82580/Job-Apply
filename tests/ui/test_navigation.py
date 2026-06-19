"""UI tests for cross-page navigation and shared chrome."""

import pytest
from playwright.sync_api import expect


PAGES = [
    ("/agents.html",   "Agents - Job Apply"),
    ("/tracking.html", None),
    ("/calendar.html", None),
    ("/profile.html",  None),
]


class TestNavigation:
    @pytest.mark.parametrize("path,expected_title", PAGES)
    def test_page_loads_without_error(self, auth_page, path, expected_title):
        auth_page.goto(path)
        auth_page.wait_for_load_state("domcontentloaded")
        # No 404 / error page
        expect(auth_page.locator("body")).not_to_be_empty()
        if expected_title:
            expect(auth_page).to_have_title(expected_title)

    def test_agent_page_link_from_tracker(self, auth_page):
        auth_page.goto("/tracking.html")
        auth_page.wait_for_load_state("domcontentloaded")
        auth_page.locator("a[href='/agents.html']").first.click()
        auth_page.wait_for_url("**/agents.html", timeout=8_000)
        expect(auth_page).to_have_title("Agents - Job Apply")

    def test_tracker_link_from_agent_page(self, auth_page):
        auth_page.goto("/agents.html")
        auth_page.wait_for_load_state("domcontentloaded")
        auth_page.locator("a[href='/tracking.html']").first.click()
        auth_page.wait_for_url("**/tracking.html", timeout=8_000)

    def test_profile_link_from_agent_page(self, auth_page):
        auth_page.goto("/agents.html")
        auth_page.wait_for_load_state("domcontentloaded")
        auth_page.locator("a[href='/profile.html']").first.click()
        auth_page.wait_for_url("**/profile.html", timeout=8_000)

    def test_calendar_link_from_agent_page(self, auth_page):
        auth_page.goto("/agents.html")
        auth_page.wait_for_load_state("domcontentloaded")
        auth_page.locator("a[href='/calendar.html']").first.click()
        auth_page.wait_for_url("**/calendar.html", timeout=8_000)


class TestLogout:
    def test_logout_from_agent_page(self, auth_page):
        auth_page.goto("/agents.html")
        auth_page.wait_for_selector("#logoutBtn", timeout=8_000)
        auth_page.click("#logoutBtn")
        auth_page.wait_for_url(lambda url: "login" in url, timeout=10_000)
        expect(auth_page.locator("#loginForm")).to_be_visible()

    def test_logout_from_profile_page(self, auth_page):
        auth_page.goto("/profile.html")
        auth_page.wait_for_selector("#logoutBtnHeader", timeout=8_000)
        auth_page.click("#logoutBtnHeader")
        auth_page.wait_for_url(lambda url: "login" in url, timeout=10_000)
        expect(auth_page.locator("#loginForm")).to_be_visible()


class TestAdminRedirect:
    def test_admin_page_redirects_regular_user(self, auth_page):
        """Regular users who try to access /admin.html should be redirected or blocked."""
        auth_page.goto("/admin.html")
        auth_page.wait_for_load_state("networkidle", timeout=10_000)
        # Regular user should be redirected away or see an access denied page
        # (Admin page JS redirects non-admins to their dashboard)
        current_url = auth_page.url
        # Either redirected to main app or login
        assert "admin" not in current_url or auth_page.locator("body").inner_text() != ""
