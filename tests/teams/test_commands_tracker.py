"""
Tests for Teams application-tracker commands (_cmd_track_add, _cmd_tracker,
_cmd_track_list, _cmd_track_view, _cmd_track_update, _cmd_track_note,
_cmd_track_delete) — the list/pipeline/picker commands that read applications
via api_client.get_applications and render Adaptive Cards.
"""

from unittest.mock import patch

from tests.teams.conftest import make_ctx, sent_texts, sent_cards, SAMPLE_APPS


# ── _cmd_track_add ────────────────────────────────────────────────────────

class TestCmdTrackAdd:
    async def test_sends_track_add_form_card(self, bot):
        ctx = make_ctx()
        await bot._cmd_track_add(ctx)
        cards = sent_cards(ctx)
        assert len(cards) == 1
        assert cards[0]["body"][0]["text"] == "Add Application"


# ── _cmd_tracker ──────────────────────────────────────────────────────────

class TestCmdTracker:
    async def test_get_applications_error_sends_error_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", side_effect=Exception("down")):
            await bot._cmd_tracker(ctx, {"email": "a@b.com"})
        assert "Could not reach the tracker" in sent_texts(ctx)[0]

    async def test_success_renders_factset_with_counts(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=SAMPLE_APPS):
            await bot._cmd_tracker(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        facts = {f["title"]: f["value"] for f in card["body"][2]["facts"]}
        assert facts["\U0001f3af Interviewing"] == "1"
        assert facts["✅ Applied"] == "1"
        assert facts["❌ Rejected"] == "1"
        assert card["body"][1]["text"] == "3 total"

    async def test_zero_count_statuses_excluded_from_factset(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=SAMPLE_APPS):
            await bot._cmd_tracker(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        titles = [f["title"] for f in card["body"][2]["facts"]]
        assert len(titles) == 3
        assert not any("Offer" in t for t in titles)
        assert not any("On Hold" in t for t in titles)

    async def test_no_apps_sends_empty_factset(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=[]):
            await bot._cmd_tracker(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        assert card["body"][2]["facts"] == []
        assert card["body"][1]["text"] == "0 total"


# ── _cmd_track_list ───────────────────────────────────────────────────────

class TestCmdTrackList:
    async def test_unknown_status_filter_lists_valid_statuses(self, bot):
        ctx = make_ctx()
        await bot._cmd_track_list(ctx, "bogus", {"email": "a@b.com"})
        text = sent_texts(ctx)[0]
        assert "Unknown status" in text
        assert "`Interviewing`" in text

    async def test_status_filter_passed_case_insensitively(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=SAMPLE_APPS) as mock_get:
            await bot._cmd_track_list(ctx, "interviewing", {"email": "a@b.com"})
        mock_get.assert_called_once_with(status="Interviewing", user_email="a@b.com")

    async def test_empty_status_filter_uses_all_apps(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=SAMPLE_APPS) as mock_get:
            await bot._cmd_track_list(ctx, "", {"email": "a@b.com"})
        mock_get.assert_called_once_with(status=None, user_email="a@b.com")

    async def test_get_applications_error_sends_error_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", side_effect=Exception("down")):
            await bot._cmd_track_list(ctx, "", {"email": "a@b.com"})
        assert "❌ Error" in sent_texts(ctx)[0]

    async def test_no_apps_sends_active_wording_when_no_filter(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=[]):
            await bot._cmd_track_list(ctx, "", {"email": "a@b.com"})
        assert "No active applications found." in sent_texts(ctx)[0]

    async def test_no_apps_sends_status_wording_when_filtered(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=[]):
            await bot._cmd_track_list(ctx, "Offer", {"email": "a@b.com"})
        assert "No **Offer** applications found." in sent_texts(ctx)[0]

    async def test_apps_sorted_by_status_order_then_company(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=SAMPLE_APPS):
            await bot._cmd_track_list(ctx, "", {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        # body[0]=title, body[1]=subtitle, then one ColumnSet row per app.
        rows = card["body"][2:]
        companies_in_order = []
        for row in rows:
            for col in row["columns"]:
                if col["width"] == "stretch":
                    companies_in_order.append(col["items"][0]["text"])
        # VALID_STATUSES order: Applied, Interviewing, Rejected
        assert companies_in_order == ["Stripe", "Salesforce", "Figma"]

    async def test_title_includes_resolved_status(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=[SAMPLE_APPS[0]]):
            await bot._cmd_track_list(ctx, "interviewing", {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        assert card["body"][0]["text"] == "\U0001f4cb Applications — Interviewing"

    async def test_title_has_no_status_suffix_when_unfiltered(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=SAMPLE_APPS):
            await bot._cmd_track_list(ctx, "", {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        assert card["body"][0]["text"] == "\U0001f4cb Applications"

    async def test_more_than_fifteen_apps_capped_with_overflow_footer(self, bot):
        ctx = make_ctx()
        many_apps = [
            {
                "id": f"app-{i}",
                "company": f"Company{i:02d}",
                "role_title": "Engineer",
                "status": "Applied",
                "domain": "",
            }
            for i in range(20)
        ]
        with patch("api_client.get_applications", return_value=many_apps):
            await bot._cmd_track_list(ctx, "", {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        rows = card["body"][2:-1]
        assert len(rows) == 15
        footer = card["body"][-1]
        assert footer["text"] == "…and 5 more."

    async def test_fifteen_or_fewer_apps_have_no_overflow_footer(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=SAMPLE_APPS):
            await bot._cmd_track_list(ctx, "", {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        assert not any(
            isinstance(block.get("text"), str) and block["text"].startswith("…and")
            for block in card["body"]
        )


# ── _cmd_track_view ───────────────────────────────────────────────────────

class TestCmdTrackView:
    async def test_no_apps_sends_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=[]):
            await bot._cmd_track_view(ctx, {"email": "a@b.com"})
        assert "No applications found." in sent_texts(ctx)[0]

    async def test_get_applications_error_sends_error_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", side_effect=Exception("down")):
            await bot._cmd_track_view(ctx, {"email": "a@b.com"})
        assert "❌ Error" in sent_texts(ctx)[0]

    async def test_apps_found_sends_choiceset_and_submit_action(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=SAMPLE_APPS):
            await bot._cmd_track_view(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        assert card["body"][0]["text"] == "View Application"
        choice_set = card["body"][1]
        assert choice_set["type"] == "Input.ChoiceSet"
        assert choice_set["choices"] == [
            {"title": "Salesforce — Senior Engineer", "value": "app-001"},
            {"title": "Stripe — Staff Engineer", "value": "app-002"},
            {"title": "Figma — Backend Engineer", "value": "app-003"},
        ]
        assert card["actions"] == [
            {"type": "Action.Submit", "title": "View", "data": {"action": "track_view_submit"}},
        ]


# ── _cmd_track_update ─────────────────────────────────────────────────────

class TestCmdTrackUpdate:
    async def test_no_apps_sends_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=[]):
            await bot._cmd_track_update(ctx, {"email": "a@b.com"})
        assert "No applications found. Add one with **track add** first." in sent_texts(ctx)[0]

    async def test_get_applications_error_sends_error_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", side_effect=Exception("down")):
            await bot._cmd_track_update(ctx, {"email": "a@b.com"})
        assert "❌ Error" in sent_texts(ctx)[0]

    async def test_apps_found_sends_choiceset_and_submit_action(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=SAMPLE_APPS):
            await bot._cmd_track_update(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        assert card["body"][0]["text"] == "Update Application"
        choice_set = card["body"][1]
        assert choice_set["label"] == "Select application to edit"
        assert len(choice_set["choices"]) == 3
        assert card["actions"] == [
            {"type": "Action.Submit", "title": "Continue", "data": {"action": "track_update_select_submit"}},
        ]


# ── _cmd_track_note ───────────────────────────────────────────────────────

class TestCmdTrackNote:
    async def test_no_apps_sends_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=[]):
            await bot._cmd_track_note(ctx, {"email": "a@b.com"})
        assert "No applications found. Add one with **track add** first." in sent_texts(ctx)[0]

    async def test_get_applications_error_sends_error_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", side_effect=Exception("down")):
            await bot._cmd_track_note(ctx, {"email": "a@b.com"})
        assert "❌ Error" in sent_texts(ctx)[0]

    async def test_apps_found_sends_choiceset_note_input_and_submit_action(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=SAMPLE_APPS):
            await bot._cmd_track_note(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        assert card["body"][0]["text"] == "Add Note"
        choice_set = card["body"][1]
        assert choice_set["type"] == "Input.ChoiceSet"
        assert len(choice_set["choices"]) == 3
        note_input = card["body"][2]
        assert note_input["type"] == "Input.Text"
        assert note_input["id"] == "note"
        assert note_input["isRequired"] is True
        assert card["actions"] == [
            {"type": "Action.Submit", "title": "Add Note", "data": {"action": "track_note_submit"}},
        ]


# ── _cmd_track_delete ─────────────────────────────────────────────────────

class TestCmdTrackDelete:
    async def test_no_apps_sends_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=[]):
            await bot._cmd_track_delete(ctx, {"email": "a@b.com"})
        assert "No applications found." in sent_texts(ctx)[0]

    async def test_get_applications_error_sends_error_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", side_effect=Exception("down")):
            await bot._cmd_track_delete(ctx, {"email": "a@b.com"})
        assert "❌ Error" in sent_texts(ctx)[0]

    async def test_apps_found_sends_warning_choiceset_and_submit_action(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=SAMPLE_APPS):
            await bot._cmd_track_delete(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        assert card["body"][0]["text"] == "Delete Application"
        warning = card["body"][1]
        assert warning["color"] == "Attention"
        assert "permanently delete" in warning["text"]
        choice_set = card["body"][2]
        assert choice_set["label"] == "Application to delete"
        assert len(choice_set["choices"]) == 3
        assert card["actions"] == [
            {"type": "Action.Submit", "title": "Continue", "data": {"action": "track_delete_select_submit"}},
        ]
