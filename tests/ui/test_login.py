"""UI tests for /login.html — auth flow, validation, redirects."""

import pytest
from playwright.sync_api import expect
from tests.ui.conftest import BASE_URL, TEST_EMAIL, TEST_PASSWORD


class TestLoginPage:
    def test_login_page_loads(self, anon_page):
        anon_page.goto("/login.html")
        # Title should be non-empty
        title = anon_page.title()
        assert len(title) > 0
        expect(anon_page.locator("#loginForm")).to_be_visible()

    def test_email_and_password_fields_present(self, anon_page):
        anon_page.goto("/login.html")
        expect(anon_page.locator("#email")).to_be_visible()
        expect(anon_page.locator("#password")).to_be_visible()
        expect(anon_page.locator("#submitBtn")).to_be_visible()

    def test_google_sign_in_button_present(self, anon_page):
        anon_page.goto("/login.html")
        expect(anon_page.locator("#googleBtn")).to_be_visible()

    def test_error_element_exists(self, anon_page):
        anon_page.goto("/login.html")
        expect(anon_page.locator("#err")).to_be_attached()

    def test_empty_form_does_not_submit(self, anon_page):
        anon_page.goto("/login.html")
        # Native HTML5 validation prevents submit — form stays on page
        anon_page.click("#submitBtn")
        # Still on login page
        assert "login" in anon_page.url

    def test_link_to_register_present(self, anon_page):
        anon_page.goto("/login.html")
        reg_link = anon_page.locator("a[href*='register']")
        expect(reg_link).to_be_visible()

    def test_profile_page_redirects_to_login(self, anon_page):
        anon_page.goto("/profile.html")
        anon_page.wait_for_url(lambda url: "login" in url, timeout=8_000)

    def test_tracking_page_redirects_to_login(self, anon_page):
        anon_page.goto("/tracking.html")
        anon_page.wait_for_url(lambda url: "login" in url, timeout=8_000)

    @pytest.mark.skipif(not TEST_PASSWORD, reason="UI_TEST_PASSWORD not set")
    def test_valid_login_redirects_away(self, anon_page):
        anon_page.goto("/login.html")
        anon_page.fill("#email",    TEST_EMAIL)
        anon_page.fill("#password", TEST_PASSWORD)
        anon_page.click("#submitBtn")
        anon_page.wait_for_url(lambda url: "login" not in url, timeout=30_000)
        # Should land on the main app
        assert "login" not in anon_page.url
