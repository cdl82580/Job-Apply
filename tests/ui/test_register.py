"""UI tests for /register.html."""

import pytest
from playwright.sync_api import expect


class TestRegisterPage:
    def test_register_page_loads(self, anon_page):
        anon_page.goto("/register.html")
        expect(anon_page.locator("form")).to_be_visible()

    def test_required_fields_present(self, anon_page):
        anon_page.goto("/register.html")
        expect(anon_page.locator("input[name='email'], #email")).to_be_visible()
        expect(anon_page.locator("input[type='password']").first).to_be_visible()

    def test_link_to_login_present(self, anon_page):
        anon_page.goto("/register.html")
        login_link = anon_page.locator("a[href*='login']")
        expect(login_link).to_be_visible()

    def test_sign_in_link_present(self, anon_page):
        anon_page.goto("/register.html")
        # Link back to login should be present
        login_link = anon_page.locator("a[href*='login']")
        expect(login_link).to_be_visible()
