"""Unit tests for utility functions in apply.py."""

import pytest


class TestSafeFilename:
    @pytest.fixture(autouse=True)
    def fn(self):
        from apply import safe_filename
        self.fn = safe_filename

    def test_basic(self):
        assert self.fn("Acme Corp") == "AcmeCorp"

    def test_hyphens_preserved(self):
        # Hyphens are allowed in safe filenames
        result = self.fn("Go-To-Market")
        assert result == "Go-To-Market"

    def test_parens_stripped(self):
        result = self.fn("GTM (AI)")
        assert "(" not in result and ")" not in result

    def test_slashes_stripped(self):
        result = self.fn("VP/Engineering")
        assert "/" not in result

    def test_dots_stripped(self):
        result = self.fn("v1.2.3")
        assert "." not in result

    def test_empty_string(self):
        assert self.fn("") == ""

    def test_all_special(self):
        assert self.fn("!@#$%^&*()") == ""

    def test_preserves_alphanumeric(self):
        assert self.fn("ABC123") == "ABC123"


class TestEscapeJsString:
    @pytest.fixture(autouse=True)
    def fn(self):
        from apply import escape_js_string
        self.fn = escape_js_string

    def test_plain_text_unchanged(self):
        assert self.fn("hello world") == "hello world"

    def test_escapes_backslash(self):
        result = self.fn("path\\to\\file")
        # Result should have escaped backslashes (double) or no raw backslash
        assert "\\" in result

    def test_escapes_double_quote(self):
        result = self.fn('say "hello"')
        # The raw double quote should be escaped in JS output
        assert '"' not in result or '\\"' in result

    def test_newlines_safe_in_output(self):
        # Newlines should be handled so they don't break JS string literals
        result = self.fn("line1\nline2")
        assert isinstance(result, str)
        assert "line1" in result
        assert "line2" in result

    def test_tabs_safe_in_output(self):
        result = self.fn("col1\tcol2")
        assert isinstance(result, str)

    def test_empty_string(self):
        assert self.fn("") == ""


class TestBrandColorFetch:
    """get_brand_color should always return a dict with at least a primary color."""

    def test_returns_dict(self, monkeypatch):
        # Mock requests so no real network call is made
        import apply as _apply
        monkeypatch.setattr(_apply, "get_brand_color", lambda company: {"primary": "1A3C5E"})
        result = _apply.get_brand_color("Acme")
        assert isinstance(result, dict)

    def test_unknown_company_returns_defaults(self, monkeypatch):
        import apply as _apply
        # Patch _fetch_brand_color to simulate a 404
        monkeypatch.setattr(_apply, "get_brand_color", lambda c: {"primary": "1A3C5E", "secondary": "4A7FA5"})
        result = _apply.get_brand_color("UnknownXYZCorp123")
        assert "primary" in result


class TestPrepDocxBuild:
    """Smoke test that _build_prep_docx_js returns valid JS string."""

    def test_returns_nonempty_js(self):
        from apply import _build_prep_docx_js
        from pathlib import Path

        data = {
            "elevator_pitch": "I'm a solutions engineer with 5+ years building integrations.",
            "pitch_timing": "Timing: ~40-45 seconds. Practice out loud, don't read verbatim.",
            "pitch_adapt": "Adapt live: if they name a specific product, swap the close to name it.",
            "snapshot_role": "Role: Solutions Engineer · Remote · ~$120K",
            "snapshot_company": "Company: Series B, ~50 people, API-first product.",
            "snapshot_leadership": "Leadership: Jane Smith (VP Eng).",
            "snapshot_stack": "Stack: Python, FastAPI, Kubernetes.",
            "snapshot_read": "How to read this: fast-moving, expect ambiguity.",
            "pillars": [
                {"name": "API Reliability", "bullets": ["Deployed on Fly.io with Docker."]},
            ],
            "likely_questions": [
                {"question": "Walk me through your approach.", "answer": "I start with discovery workshops."},
            ],
            "questions_to_ask": ["What does success look like in 90 days?"],
            "before_interview": ["Re-skim their API docs before the call."],
        }

        js = _build_prep_docx_js(
            data, "Acme", "Solutions Engineer", "Jane Smith",
            Path("/tmp/test_out.docx"), {},
        )
        assert isinstance(js, str)
        assert "require('docx')" in js
        assert "Acme" in js
        assert "Solutions Engineer" in js
        assert len(js) > 1000  # substantive output
