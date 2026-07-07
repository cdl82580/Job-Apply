"""
Tests for Teams agent-command launchers (_require_any_application, _cmd_apply,
_cmd_aq, _cmd_prep, _cmd_thankyou, _cmd_optimize, _cmd_rescore) and the
Adaptive Card dynamic-search invoke path (on_invoke_activity,
_handle_dynamic_search, _search_companies, _search_my_applications).
"""

from unittest.mock import AsyncMock, patch

import pytest

from tests.teams.conftest import (
    make_ctx, sent_texts, sent_cards,
    SAMPLE_APPS, SAMPLE_APP,
    SAMPLE_LINK_STATUS_LINKED, SAMPLE_LINK_STATUS_UNLINKED,
)


# ── _require_any_application ─────────────────────────────────────────────

class TestRequireAnyApplication:
    async def test_error_sends_message_and_returns_false(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", side_effect=Exception("down")):
            result = await bot._require_any_application(ctx, {"email": "a@b.com"})
        assert result is False
        assert "Error loading applications" in sent_texts(ctx)[0]

    async def test_no_applications_sends_message_and_returns_false(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=[]):
            result = await bot._require_any_application(ctx, {"email": "a@b.com"})
        assert result is False
        assert "No applications on file yet" in sent_texts(ctx)[0]

    async def test_has_applications_returns_true_with_no_message(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=SAMPLE_APPS):
            result = await bot._require_any_application(ctx, {"email": "a@b.com"})
        assert result is True
        assert sent_texts(ctx) == []


# ── _cmd_apply ────────────────────────────────────────────────────────────

class TestCmdApply:
    async def test_short_circuits_when_no_application(self, bot):
        ctx = make_ctx()
        with patch.object(type(bot), "_require_any_application", new=AsyncMock(return_value=False)):
            await bot._cmd_apply(ctx, {"email": "a@b.com"})
        assert sent_cards(ctx) == []

    async def test_sends_apply_form_card(self, bot):
        ctx = make_ctx()
        with patch.object(type(bot), "_require_any_application", new=AsyncMock(return_value=True)):
            await bot._cmd_apply(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        assert card["actions"][0]["data"]["action"] == "apply_select_submit"


# ── _cmd_aq ───────────────────────────────────────────────────────────────

class TestCmdAq:
    async def test_short_circuits_when_no_application(self, bot):
        ctx = make_ctx()
        with patch.object(type(bot), "_require_any_application", new=AsyncMock(return_value=False)):
            await bot._cmd_aq(ctx, {"email": "a@b.com"})
        assert sent_cards(ctx) == []

    async def test_sends_aq_form_card(self, bot):
        ctx = make_ctx()
        with patch.object(type(bot), "_require_any_application", new=AsyncMock(return_value=True)):
            await bot._cmd_aq(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        assert card["actions"][0]["data"]["action"] == "aq_select_submit"


# ── _cmd_prep ─────────────────────────────────────────────────────────────

class TestCmdPrep:
    async def test_short_circuits_when_no_application(self, bot):
        ctx = make_ctx()
        with patch.object(type(bot), "_require_any_application", new=AsyncMock(return_value=False)):
            await bot._cmd_prep(ctx, {"email": "a@b.com"})
        assert sent_cards(ctx) == []

    async def test_sends_prep_form_card(self, bot):
        ctx = make_ctx()
        with patch.object(type(bot), "_require_any_application", new=AsyncMock(return_value=True)):
            await bot._cmd_prep(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        assert card["actions"][0]["data"]["action"] == "prep_select_submit"


# ── _cmd_thankyou ─────────────────────────────────────────────────────────

class TestCmdThankyou:
    async def test_short_circuits_when_no_application(self, bot):
        ctx = make_ctx()
        with patch.object(type(bot), "_require_any_application", new=AsyncMock(return_value=False)):
            await bot._cmd_thankyou(ctx, {"email": "a@b.com"})
        assert sent_cards(ctx) == []

    async def test_sends_thankyou_form_card(self, bot):
        ctx = make_ctx()
        with patch.object(type(bot), "_require_any_application", new=AsyncMock(return_value=True)):
            await bot._cmd_thankyou(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        assert card["actions"][0]["data"]["action"] == "thankyou_select_submit"


# ── _cmd_optimize ─────────────────────────────────────────────────────────

class TestCmdOptimize:
    async def test_error_loading_applications(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", side_effect=Exception("down")):
            await bot._cmd_optimize(ctx, {"email": "a@b.com"})
        assert "Error loading applications" in sent_texts(ctx)[0]
        assert sent_cards(ctx) == []

    async def test_no_active_applications(self, bot):
        ctx = make_ctx()
        rejected_only = [SAMPLE_APPS[2]]
        with patch("api_client.get_applications", return_value=rejected_only):
            await bot._cmd_optimize(ctx, {"email": "a@b.com"})
        assert "No active applications found" in sent_texts(ctx)[0]
        assert sent_cards(ctx) == []

    async def test_sends_card_with_only_active_apps_as_choices(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=SAMPLE_APPS):
            await bot._cmd_optimize(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        assert card["actions"][0]["data"]["action"] == "optimize_submit"
        choice_set = card["body"][2]
        assert choice_set["type"] == "Input.ChoiceSet"
        assert choice_set["id"] == "app_id"
        values = [c["value"] for c in choice_set["choices"]]
        assert values == ["app-001", "app-002"]
        assert "app-003" not in values


# ── _cmd_rescore ──────────────────────────────────────────────────────────

class TestCmdRescore:
    async def test_error_loading_applications(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", side_effect=Exception("down")):
            await bot._cmd_rescore(ctx, {"email": "a@b.com"})
        assert "Error loading applications" in sent_texts(ctx)[0]
        assert sent_cards(ctx) == []

    async def test_no_active_applications(self, bot):
        ctx = make_ctx()
        rejected_only = [SAMPLE_APPS[2]]
        with patch("api_client.get_applications", return_value=rejected_only):
            await bot._cmd_rescore(ctx, {"email": "a@b.com"})
        assert "No active applications found" in sent_texts(ctx)[0]
        assert sent_cards(ctx) == []

    async def test_sends_card_with_only_active_apps_as_choices(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=SAMPLE_APPS):
            await bot._cmd_rescore(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        assert card["actions"][0]["data"]["action"] == "rescore_submit"
        choice_set = card["body"][2]
        assert choice_set["type"] == "Input.ChoiceSet"
        assert choice_set["id"] == "app_id"
        values = [c["value"] for c in choice_set["choices"]]
        assert values == ["app-001", "app-002"]
        assert "app-003" not in values


# ── on_invoke_activity ────────────────────────────────────────────────────

class TestOnInvokeActivity:
    async def test_dispatches_to_handle_dynamic_search(self, bot):
        ctx = make_ctx(name="application/search", value={"dataset": "companies", "queryText": "sales"})
        with patch.object(type(bot), "_handle_dynamic_search", new=AsyncMock(return_value="sentinel")) as mock_search:
            result = await bot.on_invoke_activity(ctx)
        mock_search.assert_awaited_once_with(ctx)
        assert result == "sentinel"

    async def test_non_search_falls_through_to_base_handler(self, bot):
        ctx = make_ctx(name="some/other-invoke")
        with patch.object(type(bot), "_handle_dynamic_search", new=AsyncMock()) as mock_search, \
             patch("botbuilder.core.ActivityHandler.on_invoke_activity", new=AsyncMock(return_value="base-result")) as mock_base:
            result = await bot.on_invoke_activity(ctx)
        mock_search.assert_not_awaited()
        mock_base.assert_awaited_once_with(ctx)
        assert result == "base-result"


# ── _handle_dynamic_search ────────────────────────────────────────────────

class TestHandleDynamicSearch:
    async def test_my_applications_dataset_dispatches_to_search_my_applications(self, bot):
        ctx = make_ctx(name="application/search", value={"dataset": "myApplications", "queryText": "sales"})
        with patch.object(type(bot), "_search_my_applications", new=AsyncMock(return_value=[{"title": "x", "value": "y"}])) as mock_mine, \
             patch.object(type(bot), "_search_companies", new=AsyncMock(return_value=[])) as mock_companies:
            response = await bot._handle_dynamic_search(ctx)
        mock_mine.assert_awaited_once_with(ctx, "sales")
        mock_companies.assert_not_awaited()
        assert response.status == 200
        assert response.body["value"]["results"] == [{"title": "x", "value": "y"}]
        assert response.body["type"] == "application/vnd.microsoft.search.searchResponse"

    async def test_other_dataset_dispatches_to_search_companies(self, bot):
        ctx = make_ctx(name="application/search", value={"dataset": "companies", "queryText": "sales"})
        with patch.object(type(bot), "_search_my_applications", new=AsyncMock(return_value=[])) as mock_mine, \
             patch.object(type(bot), "_search_companies", new=AsyncMock(return_value=[{"title": "Salesforce", "value": "v"}])) as mock_companies:
            response = await bot._handle_dynamic_search(ctx)
        mock_mine.assert_not_awaited()
        mock_companies.assert_awaited_once_with("sales")
        assert response.body["value"]["results"] == [{"title": "Salesforce", "value": "v"}]

    async def test_missing_value_defaults_to_empty_query_and_companies_dataset(self, bot):
        ctx = make_ctx(name="application/search", value=None)
        with patch.object(type(bot), "_search_companies", new=AsyncMock(return_value=[])) as mock_companies:
            response = await bot._handle_dynamic_search(ctx)
        mock_companies.assert_awaited_once_with("")
        assert response.status == 200


# ── _search_companies ─────────────────────────────────────────────────────

class TestSearchCompanies:
    async def test_short_query_returns_empty_without_calling_api(self, bot_module):
        with patch("api_client.search_companies") as mock_search:
            result = await bot_module.JobApplyBot._search_companies("s")
        assert result == []
        mock_search.assert_not_called()

    async def test_maps_results_with_domain(self, bot_module):
        companies = [{"name": "Salesforce", "domain": "salesforce.com"}]
        with patch("api_client.search_companies", return_value=companies):
            result = await bot_module.JobApplyBot._search_companies("sales")
        assert result == [{"title": "Salesforce (salesforce.com)", "value": "Salesforce|||salesforce.com"}]

    async def test_maps_results_without_domain(self, bot_module):
        companies = [{"name": "Salesforce", "domain": ""}]
        with patch("api_client.search_companies", return_value=companies):
            result = await bot_module.JobApplyBot._search_companies("sales")
        assert result == [{"title": "Salesforce", "value": "Salesforce|||"}]

    async def test_truncates_to_eight_results(self, bot_module):
        companies = [{"name": f"Company{i}", "domain": f"c{i}.com"} for i in range(20)]
        with patch("api_client.search_companies", return_value=companies):
            result = await bot_module.JobApplyBot._search_companies("company")
        assert len(result) == 8

    async def test_exception_returns_empty_silently(self, bot_module):
        with patch("api_client.search_companies", side_effect=Exception("down")):
            result = await bot_module.JobApplyBot._search_companies("sales")
        assert result == []


# ── _search_my_applications ───────────────────────────────────────────────

class TestSearchMyApplications:
    async def test_no_aad_object_id_returns_empty(self, bot):
        ctx = make_ctx(aad_object_id=None)
        result = await bot._search_my_applications(ctx, "sales")
        assert result == []

    async def test_not_linked_returns_empty(self, bot):
        ctx = make_ctx()
        with patch("api_client.teams_link_status", return_value=SAMPLE_LINK_STATUS_UNLINKED):
            result = await bot._search_my_applications(ctx, "sales")
        assert result == []

    async def test_link_status_error_returns_empty(self, bot):
        ctx = make_ctx()
        with patch("api_client.teams_link_status", side_effect=Exception("down")):
            result = await bot._search_my_applications(ctx, "sales")
        assert result == []

    async def test_get_applications_error_returns_empty(self, bot):
        ctx = make_ctx()
        with patch("api_client.teams_link_status", return_value=SAMPLE_LINK_STATUS_LINKED), \
             patch("api_client.get_applications", side_effect=Exception("down")):
            result = await bot._search_my_applications(ctx, "sales")
        assert result == []

    async def test_filters_by_company_substring_case_insensitive(self, bot):
        ctx = make_ctx()
        with patch("api_client.teams_link_status", return_value=SAMPLE_LINK_STATUS_LINKED), \
             patch("api_client.get_applications", return_value=SAMPLE_APPS):
            result = await bot._search_my_applications(ctx, "SALES")
        assert result == [{"title": "Salesforce | Senior Engineer", "value": "app-001"}]

    async def test_filters_by_role_title_substring(self, bot):
        ctx = make_ctx()
        with patch("api_client.teams_link_status", return_value=SAMPLE_LINK_STATUS_LINKED), \
             patch("api_client.get_applications", return_value=SAMPLE_APPS):
            result = await bot._search_my_applications(ctx, "staff")
        assert result == [{"title": "Stripe | Staff Engineer", "value": "app-002"}]

    async def test_empty_query_returns_all_up_to_eight(self, bot):
        ctx = make_ctx()
        with patch("api_client.teams_link_status", return_value=SAMPLE_LINK_STATUS_LINKED), \
             patch("api_client.get_applications", return_value=SAMPLE_APPS):
            result = await bot._search_my_applications(ctx, "")
        assert len(result) == 3

    async def test_truncates_to_eight_results(self, bot):
        ctx = make_ctx()
        many_apps = [
            {"id": f"app-{i}", "company": "Acme", "role_title": "Engineer"}
            for i in range(20)
        ]
        with patch("api_client.teams_link_status", return_value=SAMPLE_LINK_STATUS_LINKED), \
             patch("api_client.get_applications", return_value=many_apps):
            result = await bot._search_my_applications(ctx, "acme")
        assert len(result) == 8

    async def test_calls_get_applications_with_linked_email(self, bot):
        ctx = make_ctx()
        with patch("api_client.teams_link_status", return_value=SAMPLE_LINK_STATUS_LINKED), \
             patch("api_client.get_applications", return_value=SAMPLE_APPS) as mock_get:
            await bot._search_my_applications(ctx, "")
        mock_get.assert_called_once_with(user_email=SAMPLE_LINK_STATUS_LINKED["email"])
