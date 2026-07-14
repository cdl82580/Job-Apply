"""Unit tests for scripts/brand_color.py — brand color + logo lookup."""

from unittest.mock import MagicMock, patch

import pytest

from scripts import brand_color


def _resp(json_data=None, status_code=200, content=b""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.content = content
    return r


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("BRANDFETCH_API_KEY", "test-key")


class TestGetBrandColor:
    def test_no_search_results_returns_default(self):
        with patch.object(brand_color.requests, "get", return_value=_resp([])):
            result = brand_color.get_brand_color("NobodyHeardOfThisCo")
        assert result == brand_color.DEFAULT_PALETTE

    def test_no_domain_returns_default(self):
        with patch.object(brand_color.requests, "get", return_value=_resp([{"domain": ""}])):
            result = brand_color.get_brand_color("Acme")
        assert result == brand_color.DEFAULT_PALETTE

    def test_no_usable_color_returns_default(self):
        search_resp = _resp([{"domain": "acme.com"}])
        brand_resp = _resp({"colors": []})
        with patch.object(brand_color.requests, "get", side_effect=[search_resp, brand_resp]):
            result = brand_color.get_brand_color("Acme")
        assert result == brand_color.DEFAULT_PALETTE

    def test_single_color_derives_distinct_secondary(self):
        """With only one brand color, secondary should be a darkened shade —
        not just a fixed fallback and not identical to primary or border."""
        search_resp = _resp([{"domain": "acme.com"}])
        brand_resp = _resp({"colors": [{"type": "accent", "hex": "#3366CC"}]})
        with patch.object(brand_color.requests, "get", side_effect=[search_resp, brand_resp]):
            result = brand_color.get_brand_color("Acme")
        assert result["primary"] == "3366CC"
        assert result["secondary"] not in (result["primary"], result["border"])
        assert result["secondary"] == brand_color._darken("3366CC", 0.35)

    def test_two_distinct_colors_uses_real_secondary(self):
        """When Brandfetch returns two distinct colors, secondary should be
        the real second brand color, not a derived tint."""
        search_resp = _resp([{"domain": "acme.com"}])
        brand_resp = _resp({"colors": [
            {"type": "accent", "hex": "#3366CC"},
            {"type": "dark", "hex": "#112233"},
        ]})
        with patch.object(brand_color.requests, "get", side_effect=[search_resp, brand_resp]):
            result = brand_color.get_brand_color("Acme")
        assert result["primary"] == "3366CC"
        assert result["secondary"] == "112233"

    def test_malformed_hex_is_skipped(self):
        search_resp = _resp([{"domain": "acme.com"}])
        brand_resp = _resp({"colors": [{"type": "accent", "hex": "not-a-color"}]})
        with patch.object(brand_color.requests, "get", side_effect=[search_resp, brand_resp]):
            result = brand_color.get_brand_color("Acme")
        assert result == brand_color.DEFAULT_PALETTE

    def test_exception_returns_default(self):
        with patch.object(brand_color.requests, "get", side_effect=Exception("network down")):
            result = brand_color.get_brand_color("Acme")
        assert result == brand_color.DEFAULT_PALETTE

    def test_requests_unavailable_returns_default(self):
        with patch.object(brand_color, "requests", None):
            result = brand_color.get_brand_color("Acme")
        assert result == brand_color.DEFAULT_PALETTE


class TestGetBrandLogo:
    def test_no_domain_returns_none(self):
        with patch.object(brand_color.requests, "get", return_value=_resp([])):
            assert brand_color.get_brand_logo("Acme") is None

    def test_no_logos_returns_none(self):
        search_resp = _resp([{"domain": "acme.com"}])
        brand_resp = _resp({"logos": []})
        with patch.object(brand_color.requests, "get", side_effect=[search_resp, brand_resp]):
            assert brand_color.get_brand_logo("Acme") is None

    def test_no_raster_format_returns_none(self):
        search_resp = _resp([{"domain": "acme.com"}])
        brand_resp = _resp({"logos": [
            {"type": "logo", "formats": [{"format": "svg", "src": "https://x/logo.svg"}]},
        ]})
        with patch.object(brand_color.requests, "get", side_effect=[search_resp, brand_resp]):
            assert brand_color.get_brand_logo("Acme") is None

    def test_downloads_and_returns_logo_bytes(self):
        search_resp = _resp([{"domain": "acme.com"}])
        brand_resp = _resp({"logos": [
            {"type": "logo", "formats": [
                {"format": "png", "src": "https://x/logo.png", "width": 200, "height": 60},
            ]},
        ]})
        img_resp = _resp(content=b"\x89PNG-fake-bytes", status_code=200)
        with patch.object(brand_color.requests, "get", side_effect=[search_resp, brand_resp, img_resp]):
            result = brand_color.get_brand_logo("Acme")
        assert result == {"bytes": b"\x89PNG-fake-bytes", "format": "png", "width": 200, "height": 60}

    def test_prefers_logo_type_over_icon(self):
        search_resp = _resp([{"domain": "acme.com"}])
        brand_resp = _resp({"logos": [
            {"type": "icon", "formats": [{"format": "png", "src": "https://x/icon.png"}]},
            {"type": "logo", "formats": [{"format": "png", "src": "https://x/logo.png"}]},
        ]})
        img_resp = _resp(content=b"logo-bytes")
        with patch.object(brand_color.requests, "get", side_effect=[search_resp, brand_resp, img_resp]) as mock_get:
            result = brand_color.get_brand_logo("Acme")
        assert result["bytes"] == b"logo-bytes"
        assert mock_get.call_args_list[-1][0][0] == "https://x/logo.png"

    def test_image_download_failure_returns_none(self):
        search_resp = _resp([{"domain": "acme.com"}])
        brand_resp = _resp({"logos": [
            {"type": "logo", "formats": [{"format": "png", "src": "https://x/logo.png"}]},
        ]})
        img_resp = _resp(status_code=404, content=b"")
        with patch.object(brand_color.requests, "get", side_effect=[search_resp, brand_resp, img_resp]):
            assert brand_color.get_brand_logo("Acme") is None

    def test_exception_returns_none(self):
        with patch.object(brand_color.requests, "get", side_effect=Exception("network down")):
            assert brand_color.get_brand_logo("Acme") is None

    def test_requests_unavailable_returns_none(self):
        with patch.object(brand_color, "requests", None):
            assert brand_color.get_brand_logo("Acme") is None


class TestDarkenLighten:
    def test_darken_moves_toward_black(self):
        assert brand_color._darken("FFFFFF", 0.5) == "808080"
        assert brand_color._darken("336699", 0.0) == "336699"

    def test_lighten_moves_toward_white(self):
        assert brand_color._lighten("000000", 0.5) == "808080"
        assert brand_color._lighten("336699", 0.0) == "336699"
