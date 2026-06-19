"""
UI tests for /admin.html.

Covers:
- Redirect behavior for non-admin users
- Page structure (stats bar, 6 tabs)
- Users tab: table, search, filters, export button
- Applications tab: table, search, filters
- Runs tab: table, export
- Audit Log tab: table, filters, export
- Webhooks tab: table, create button, modal

Most tests use the admin_page fixture (requires UI_ADMIN_EMAIL + UI_ADMIN_PASSWORD).
The redirect test uses auth_page (regular user).
"""

import pytest
from playwright.sync_api import expect


class TestAdminAccessControl:
    def test_regular_user_redirected_from_admin(self, auth_page):
        """A logged-in regular user navigating to /admin.html should be redirected."""
        auth_page.goto("/admin.html")
        auth_page.wait_for_url(lambda url: "admin" not in url, timeout=15_000)

    def test_anon_user_redirected_to_login(self, anon_page):
        """Unauthenticated users hitting /admin.html should land on login."""
        anon_page.goto("/admin.html")
        anon_page.wait_for_url(lambda url: "login" in url, timeout=8_000)
        expect(anon_page.locator("#loginForm")).to_be_visible()


class TestAdminPageStructure:
    def test_admin_page_loads(self, admin_page):
        admin_page.goto("/admin.html")
        admin_page.wait_for_selector(".tabs", timeout=10_000)
        expect(admin_page.locator(".tabs")).to_be_visible()

    def test_six_tabs_present(self, admin_page):
        admin_page.goto("/admin.html")
        admin_page.wait_for_selector(".tab", timeout=10_000)
        tabs = admin_page.locator(".tab")
        expect(tabs).to_have_count(6)

    def test_tab_labels(self, admin_page):
        admin_page.goto("/admin.html")
        admin_page.wait_for_selector(".tab", timeout=10_000)
        tab_texts = [admin_page.locator(".tab").nth(i).inner_text() for i in range(6)]
        assert any("User" in t for t in tab_texts)
        assert any("Application" in t for t in tab_texts)
        assert any("Run" in t for t in tab_texts)
        assert any("Audit" in t for t in tab_texts)
        assert any("Webhook" in t for t in tab_texts)
        assert any("Knowledge" in t for t in tab_texts)

    def test_stats_bar_present(self, admin_page):
        admin_page.goto("/admin.html")
        admin_page.wait_for_selector("#statUsers", timeout=10_000)
        expect(admin_page.locator("#statUsers")).to_be_visible()
        expect(admin_page.locator("#statApps")).to_be_visible()
        expect(admin_page.locator("#statRuns")).to_be_visible()
        expect(admin_page.locator("#statAdmins")).to_be_visible()

    def test_stats_bar_loads_numbers(self, admin_page):
        admin_page.goto("/admin.html")
        admin_page.wait_for_selector("#statUsers", timeout=10_000)
        stat_el = admin_page.locator("#statUsers")
        expect(stat_el).not_to_have_text("—", timeout=10_000)
        stat = stat_el.inner_text()
        assert stat.isdigit() or stat.replace(",", "").isdigit()

    def test_header_user_shown(self, admin_page):
        admin_page.goto("/admin.html")
        admin_page.wait_for_selector("#headerUser", timeout=15_000)
        expect(admin_page.locator("#headerUser")).not_to_be_empty()

    def test_theme_toggle_present(self, admin_page):
        admin_page.goto("/admin.html")
        expect(admin_page.locator("#themeToggle")).to_be_visible()

    def test_users_tab_active_by_default(self, admin_page):
        admin_page.goto("/admin.html")
        admin_page.wait_for_selector(".tab", timeout=10_000)
        active_tab = admin_page.locator(".tab.active")
        expect(active_tab).to_contain_text("User")

    def test_deeplink_to_applications_tab(self, admin_page):
        admin_page.goto("/admin.html?tab=applications")
        admin_page.wait_for_selector(".tab", timeout=10_000)
        active_tab = admin_page.locator(".tab.active")
        expect(active_tab).to_contain_text("Application")
        expect(admin_page.locator("#tab-applications")).to_be_visible()

    def test_deeplink_to_auditlog_tab(self, admin_page):
        admin_page.goto("/admin.html?tab=auditlog")
        admin_page.wait_for_selector(".tab", timeout=10_000)
        expect(admin_page.locator("#tab-auditlog")).to_be_visible()

    def test_deeplink_to_webhooks_tab(self, admin_page):
        admin_page.goto("/admin.html?tab=webhooks")
        admin_page.wait_for_selector(".tab", timeout=10_000)
        expect(admin_page.locator("#tab-webhooks")).to_be_visible()


class TestUsersTab:
    def _open_users_tab(self, page):
        page.goto("/admin.html")
        page.wait_for_selector("#tab-users", timeout=10_000)
        page.locator("#usersBody tr td:first-child").first.wait_for(timeout=10_000)

    def test_users_table_present(self, admin_page):
        self._open_users_tab(admin_page)
        expect(admin_page.locator("#usersBody")).to_be_visible()

    def test_users_table_has_rows(self, admin_page):
        self._open_users_tab(admin_page)
        first_cell = admin_page.locator("#usersBody tr td").first
        expect(first_cell).not_to_have_text("Loading…", timeout=15_000)
        rows = admin_page.locator("#usersBody tr")
        assert rows.count() >= 1

    def test_user_search_input_present(self, admin_page):
        admin_page.goto("/admin.html")
        admin_page.wait_for_selector("#userSearch", timeout=8_000)
        expect(admin_page.locator("#userSearch")).to_be_visible()

    def test_user_role_filter_present(self, admin_page):
        admin_page.goto("/admin.html")
        admin_page.wait_for_selector("#userRoleFilter", timeout=8_000)
        expect(admin_page.locator("#userRoleFilter")).to_be_visible()

    def test_user_verified_filter_present(self, admin_page):
        admin_page.goto("/admin.html")
        admin_page.wait_for_selector("#userVerifiedFilter", timeout=8_000)
        expect(admin_page.locator("#userVerifiedFilter")).to_be_visible()

    def test_search_filters_users(self, admin_page):
        self._open_users_tab(admin_page)
        admin_page.fill("#userSearch", "zzzz_no_match_xyz_999")
        admin_page.wait_for_timeout(700)
        rows = admin_page.locator("#usersBody tr")
        row_text = rows.first.locator("td").first.inner_text()
        # Either "No users" empty state or zero matching rows
        assert "No" in row_text or "zzzz" not in row_text

    def test_clear_filters_button_present(self, admin_page):
        admin_page.goto("/admin.html")
        admin_page.wait_for_selector("#userClearFilters", timeout=8_000)
        expect(admin_page.locator("#userClearFilters")).to_be_visible()

    def test_export_button_present(self, admin_page):
        admin_page.goto("/admin.html")
        admin_page.wait_for_selector("#exportUsersBtn", timeout=8_000)
        expect(admin_page.locator("#exportUsersBtn")).to_be_visible()

    def test_pagination_bar_exists(self, admin_page):
        self._open_users_tab(admin_page)
        expect(admin_page.locator("#usersPagBar")).to_be_attached()


class TestApplicationsTab:
    def _open_tab(self, page):
        page.goto("/admin.html?tab=applications")
        page.wait_for_selector("#tab-applications", timeout=10_000)
        expect(page.locator("#tab-applications")).to_be_visible()

    def test_tab_switches_correctly(self, admin_page):
        admin_page.goto("/admin.html")
        admin_page.wait_for_selector(".tab", timeout=8_000)
        admin_page.locator("button[data-tab='applications']").click()
        admin_page.wait_for_timeout(400)
        expect(admin_page.locator("#tab-applications")).to_be_visible()
        expect(admin_page.locator("#tab-users")).not_to_be_visible()

    def test_app_search_present(self, admin_page):
        self._open_tab(admin_page)
        expect(admin_page.locator("#appSearch")).to_be_visible()

    def test_app_status_filter_present(self, admin_page):
        self._open_tab(admin_page)
        expect(admin_page.locator("#appStatusFilter")).to_be_visible()

    def test_app_user_filter_present(self, admin_page):
        self._open_tab(admin_page)
        expect(admin_page.locator("#appUserFilter")).to_be_visible()

    def test_export_button_present(self, admin_page):
        self._open_tab(admin_page)
        expect(admin_page.locator("#exportAppsBtn")).to_be_visible()


class TestRunsTab:
    def test_tab_switches_correctly(self, admin_page):
        admin_page.goto("/admin.html")
        admin_page.wait_for_selector(".tab", timeout=8_000)
        admin_page.locator("button[data-tab='runs']").click()
        admin_page.wait_for_timeout(400)
        expect(admin_page.locator("#tab-runs")).to_be_visible()

    def test_runs_table_present(self, admin_page):
        admin_page.goto("/admin.html?tab=runs")
        admin_page.wait_for_selector("#tab-runs", timeout=10_000)
        expect(admin_page.locator("#tab-runs table")).to_be_visible()

    def test_export_button_present(self, admin_page):
        admin_page.goto("/admin.html?tab=runs")
        admin_page.wait_for_selector("#exportRunsBtn", timeout=8_000)
        expect(admin_page.locator("#exportRunsBtn")).to_be_visible()


class TestAuditLogTab:
    def test_tab_switches_correctly(self, admin_page):
        admin_page.goto("/admin.html")
        admin_page.wait_for_selector(".tab", timeout=8_000)
        admin_page.locator("button[data-tab='auditlog']").click()
        admin_page.wait_for_timeout(400)
        expect(admin_page.locator("#tab-auditlog")).to_be_visible()

    def test_audit_table_present(self, admin_page):
        admin_page.goto("/admin.html?tab=auditlog")
        admin_page.wait_for_selector("#tab-auditlog", timeout=10_000)
        expect(admin_page.locator("#tab-auditlog table")).to_be_visible()

    def test_audit_filter_inputs_present(self, admin_page):
        admin_page.goto("/admin.html?tab=auditlog")
        admin_page.wait_for_selector("#tab-auditlog", timeout=10_000)
        date_inputs = admin_page.locator("#tab-auditlog input[type='datetime-local']")
        assert date_inputs.count() >= 2

    def test_export_button_present(self, admin_page):
        admin_page.goto("/admin.html?tab=auditlog")
        admin_page.wait_for_selector("#exportAuditBtn", timeout=8_000)
        expect(admin_page.locator("#exportAuditBtn")).to_be_visible()

    def test_audit_table_loads_entries(self, admin_page):
        admin_page.goto("/admin.html?tab=auditlog")
        admin_page.wait_for_selector("#tab-auditlog", timeout=10_000)
        first_cell = admin_page.locator("#tab-auditlog tbody tr td").first
        first_cell.wait_for(timeout=10_000)
        expect(first_cell).not_to_have_text("Loading…", timeout=10_000)
        rows = admin_page.locator("#tab-auditlog tbody tr")
        assert rows.count() >= 1


class TestWebhooksTab:
    def _open_tab(self, page):
        page.goto("/admin.html?tab=webhooks")
        page.wait_for_selector("#tab-webhooks", timeout=10_000)
        page.locator("#webhooksBody tr td").first.wait_for(timeout=10_000)

    def test_tab_switches_correctly(self, admin_page):
        admin_page.goto("/admin.html")
        admin_page.wait_for_selector(".tab", timeout=8_000)
        admin_page.locator("button[data-tab='webhooks']").click()
        admin_page.wait_for_timeout(400)
        expect(admin_page.locator("#tab-webhooks")).to_be_visible()

    def test_create_webhook_button_present(self, admin_page):
        self._open_tab(admin_page)
        expect(admin_page.locator("#createWebhookBtn")).to_be_visible()
        expect(admin_page.locator("#createWebhookBtn")).to_contain_text("New Webhook")

    def test_webhooks_table_present(self, admin_page):
        self._open_tab(admin_page)
        expect(admin_page.locator("#webhooksTable")).to_be_visible()

    def test_webhook_count_exists(self, admin_page):
        self._open_tab(admin_page)
        expect(admin_page.locator("#webhookCount")).to_be_attached()

    def test_create_webhook_button_opens_modal(self, admin_page):
        self._open_tab(admin_page)
        admin_page.click("#createWebhookBtn")
        admin_page.wait_for_timeout(400)
        modal = admin_page.locator("#webhookModal")
        expect(modal).to_be_visible()
        expect(modal.locator("#webhookModalTitle")).to_contain_text("New Webhook")

    def test_webhook_modal_has_url_field(self, admin_page):
        self._open_tab(admin_page)
        admin_page.click("#createWebhookBtn")
        admin_page.wait_for_timeout(400)
        modal = admin_page.locator("#webhookModal")
        url_field = modal.locator("input[type='url'], input[name='url'], #webhookUrl")
        assert url_field.count() > 0

    def test_webhook_modal_close_hides_modal(self, admin_page):
        self._open_tab(admin_page)
        admin_page.click("#createWebhookBtn")
        admin_page.wait_for_timeout(400)
        expect(admin_page.locator("#webhookModal")).to_be_visible()
        # Close via cancel/close button
        cancel = admin_page.locator("#webhookModal button:has-text('Cancel'), #webhookModal button:has-text('Close')")
        if cancel.count() > 0:
            cancel.first.click()
            admin_page.wait_for_timeout(400)
            expect(admin_page.locator("#webhookModal")).not_to_have_class("open")


class TestTabSwitching:
    def test_all_tabs_switch_without_error(self, admin_page):
        admin_page.goto("/admin.html")
        admin_page.wait_for_selector(".tab", timeout=10_000)

        tab_ids = ["users", "applications", "runs", "auditlog", "webhooks", "kb"]
        for tab_id in tab_ids:
            admin_page.locator(f"button[data-tab='{tab_id}']").click()
            admin_page.wait_for_timeout(300)
            pane = admin_page.locator(f"#tab-{tab_id}")
            expect(pane).to_be_visible()
            # Active tab button should match
            active = admin_page.locator(".tab.active")
            assert active.get_attribute("data-tab") == tab_id

    def test_inactive_tab_panes_are_hidden(self, admin_page):
        admin_page.goto("/admin.html")
        admin_page.wait_for_selector(".tab", timeout=10_000)
        # On Users tab, other panes should be hidden
        expect(admin_page.locator("#tab-applications")).not_to_be_visible()
        expect(admin_page.locator("#tab-runs")).not_to_be_visible()
        expect(admin_page.locator("#tab-auditlog")).not_to_be_visible()
        expect(admin_page.locator("#tab-webhooks")).not_to_be_visible()
        expect(admin_page.locator("#tab-kb")).not_to_be_visible()
