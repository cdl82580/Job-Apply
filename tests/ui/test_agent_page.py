"""
UI tests for the main agent page (index.html).

Covers:
- Page structure and navigation
- Run form fields and validation
- Prep form fields, source mode switching, round type selector
- Past runs section
- Theme toggle
- Header elements
"""

import pytest
from playwright.sync_api import expect


class TestAgentPageStructure:
    def test_page_title(self, auth_page):
        auth_page.goto("/")
        expect(auth_page).to_have_title("Job Apply Agents")

    def test_header_user_name_shown(self, auth_page):
        auth_page.goto("/")
        auth_page.wait_for_selector("#headerUser", timeout=8_000)
        user_el = auth_page.locator("#headerUser")
        expect(user_el).not_to_be_empty()

    def test_nav_links_present(self, auth_page):
        auth_page.goto("/")
        # Tracker icon link
        expect(auth_page.locator("a[href='/tracking.html']")).to_be_visible()
        # Calendar icon link
        expect(auth_page.locator("a[href='/calendar.html']")).to_be_visible()
        # Profile icon link
        expect(auth_page.locator("a[href='/profile.html']")).to_be_visible()

    def test_theme_toggle_present(self, auth_page):
        auth_page.goto("/")
        expect(auth_page.locator("#themeToggle")).to_be_visible()

    def test_theme_toggle_switches_theme(self, auth_page):
        auth_page.goto("/")
        # Get initial theme
        initial = auth_page.evaluate("document.documentElement.getAttribute('data-theme')")
        auth_page.click("#themeToggle")
        after = auth_page.evaluate("document.documentElement.getAttribute('data-theme')")
        assert initial != after

    def test_logout_button_present(self, auth_page):
        auth_page.goto("/")
        expect(auth_page.locator("#logoutBtn")).to_be_visible()


class TestRunForm:
    def test_run_form_visible(self, auth_page):
        auth_page.goto("/")
        expect(auth_page.locator("#runForm")).to_be_visible()

    def test_job_posting_field_present(self, auth_page):
        auth_page.goto("/")
        expect(auth_page.locator("#job_posting")).to_be_visible()

    def test_company_and_role_fields_present(self, auth_page):
        auth_page.goto("/")
        expect(auth_page.locator("#company")).to_be_visible()
        expect(auth_page.locator("#role")).to_be_visible()

    def test_generate_button_present(self, auth_page):
        auth_page.goto("/")
        btn = auth_page.locator("#submitBtn").first
        expect(btn).to_be_visible()
        expect(btn).to_contain_text("Generate")

    def test_tracker_app_picker_present(self, auth_page):
        auth_page.goto("/")
        expect(auth_page.locator("#runAppSearch")).to_be_visible()

    def test_empty_form_shows_validation(self, auth_page):
        auth_page.goto("/")
        # Clear fields and submit
        auth_page.fill("#job_posting", "")
        auth_page.fill("#company", "")
        auth_page.fill("#role", "")
        auth_page.locator("#submitBtn").first.click()
        # Should not show progress card (form not submitted)
        expect(auth_page.locator("#progressCard")).to_be_hidden()

    def test_valid_form_shows_progress_card(self, auth_page):
        auth_page.goto("/")
        auth_page.fill("#job_posting", "Software Engineer at Acme. Requirements: Python, APIs.")
        auth_page.fill("#company", "Acme")
        auth_page.fill("#role",    "Software Engineer")
        auth_page.locator("#submitBtn").first.click()
        # Progress card should appear and start running
        expect(auth_page.locator("#progressCard")).to_be_visible(timeout=10_000)
        expect(auth_page.locator("#statusBadge")).to_be_visible()

    def test_back_button_resets_to_form(self, auth_page):
        auth_page.goto("/")
        auth_page.fill("#job_posting", "Job description text here for testing purposes only.")
        auth_page.fill("#company", "TestCo")
        auth_page.fill("#role",    "Engineer")
        auth_page.locator("#submitBtn").first.click()
        expect(auth_page.locator("#progressCard")).to_be_visible(timeout=10_000)
        # Click back / new run button when it appears
        new_btn = auth_page.locator("#newRunBtn")
        if new_btn.is_visible():
            new_btn.click()
            expect(auth_page.locator("#formCard")).to_be_visible()


class TestPastRunsSection:
    def test_past_runs_card_present(self, auth_page):
        auth_page.goto("/")
        expect(auth_page.locator("#pastRunsCard")).to_be_visible()

    def test_past_runs_toggle_works(self, auth_page):
        auth_page.goto("/")
        toggle = auth_page.locator("#pastRunsToggle")
        expect(toggle).to_be_visible()
        body = auth_page.locator("#pastRunsBody")
        # Click to expand/collapse
        toggle.click()
        auth_page.wait_for_timeout(300)
        # Body visibility should have changed
        is_visible_after = body.is_visible()
        toggle.click()
        auth_page.wait_for_timeout(300)
        is_visible_after_2 = body.is_visible()
        assert is_visible_after != is_visible_after_2


class TestPrepForm:
    def test_prep_card_present(self, auth_page):
        auth_page.goto("/")
        expect(auth_page.locator("#prepCard")).to_be_visible()

    def test_prep_toggle_expands_form(self, auth_page):
        auth_page.goto("/")
        prep_toggle = auth_page.locator("#prepToggle")
        expect(prep_toggle).to_be_visible()
        # Click to expand
        prep_toggle.click()
        auth_page.wait_for_timeout(300)
        expect(auth_page.locator("#prepForm, #prepSubmitBtn")).to_be_visible()

    def test_prep_source_mode_buttons(self, auth_page):
        auth_page.goto("/")
        auth_page.locator("#prepToggle").click()
        auth_page.wait_for_timeout(300)
        expect(auth_page.locator("#srcTrackerBtn")).to_be_visible()
        expect(auth_page.locator("#srcDropBtn")).to_be_visible()
        expect(auth_page.locator("#srcPasteBtn")).to_be_visible()

    def test_paste_mode_shows_jd_textarea(self, auth_page):
        auth_page.goto("/")
        auth_page.locator("#prepToggle").click()
        auth_page.wait_for_timeout(300)
        auth_page.locator("#srcPasteBtn").click()
        auth_page.wait_for_timeout(200)
        expect(auth_page.locator("#prepJdText")).to_be_visible()

    def test_tracker_mode_shows_app_picker(self, auth_page):
        auth_page.goto("/")
        auth_page.locator("#prepToggle").click()
        auth_page.wait_for_timeout(300)
        auth_page.locator("#srcTrackerBtn").click()
        auth_page.wait_for_timeout(200)
        expect(auth_page.locator("#prepAppPickerWrap")).to_be_visible()

    def test_round_type_selector_has_options(self, auth_page):
        auth_page.goto("/")
        auth_page.locator("#prepToggle").click()
        auth_page.wait_for_timeout(300)
        options = auth_page.locator("#prepRound option")
        expect(options).to_have_count(6)  # Phone Screen, HM, Peer, Technical, Executive, Panel

    def test_prep_company_role_fields_present(self, auth_page):
        auth_page.goto("/")
        auth_page.locator("#prepToggle").click()
        auth_page.wait_for_timeout(300)
        expect(auth_page.locator("#prepCompany")).to_be_visible()
        expect(auth_page.locator("#prepRole")).to_be_visible()

    def test_focus_slant_field_present(self, auth_page):
        auth_page.goto("/")
        auth_page.locator("#prepToggle").click()
        auth_page.wait_for_timeout(300)
        expect(auth_page.locator("#prepFocus")).to_be_visible()

    def test_generate_prep_button_present(self, auth_page):
        auth_page.goto("/")
        auth_page.locator("#prepToggle").click()
        auth_page.wait_for_timeout(300)
        expect(auth_page.locator("#prepSubmitBtn")).to_be_visible()
        expect(auth_page.locator("#prepSubmitBtn")).to_contain_text("Generate Prep Doc")

    def test_prep_empty_form_shows_error(self, auth_page):
        auth_page.goto("/")
        auth_page.locator("#prepToggle").click()
        auth_page.wait_for_timeout(300)
        # Switch to paste mode and try to submit with empty fields
        auth_page.locator("#srcPasteBtn").click()
        auth_page.locator("#prepSubmitBtn").click()
        auth_page.wait_for_timeout(500)
        err = auth_page.locator("#prepFormErr")
        expect(err).to_be_visible()
        expect(err).not_to_be_empty()
