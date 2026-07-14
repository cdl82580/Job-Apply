#!/usr/bin/env python3
"""
Fetch brand colors and logos from Brandfetch API.

API key is read from the BRANDFETCH_API_KEY environment variable or .env file
in the project root. Never hardcode the key in source.

Usage:
    from scripts.brand_color import get_brand_color, get_brand_logo
    colors = get_brand_color("Brightflag")
    # {"primary": "1A3C5E", "secondary": "00695C", "border": "2B6CB0", "fill": "EEF4FB"}
    logo = get_brand_logo("Brightflag")
    # {"bytes": b"...", "format": "png", "width": 512, "height": 512} or None
"""

import os
import re
from pathlib import Path
from urllib.parse import quote

try:
    import requests
except ImportError:
    requests = None  # type: ignore

# Fallback palette — Corey's default navy/teal scheme
DEFAULT_PALETTE = {
    "primary":   "1A3C5E",
    "secondary": "00695C",
    "border":    "2B6CB0",
    "fill":      "EEF4FB",
}

# Brandfetch search client token (autocomplete endpoint). Set BRANDFETCH_SEARCH_CLIENT in env.
_SEARCH_CLIENT = os.environ.get("BRANDFETCH_SEARCH_CLIENT", "")

# Brand colors are spliced into generated JS/DOCX-XML downstream — only accept
# well-formed 6-digit hex so a malformed API response can't break out of those contexts.
_HEX_RE = re.compile(r"^[0-9A-F]{6}$")

# Raster formats safe to embed via docx.js ImageRun.
_LOGO_FORMATS = ("png", "jpeg", "jpg")

_COLOR_PRIORITY = ("accent", "dark", "light")


def _load_api_key() -> str:
    key = os.environ.get("BRANDFETCH_API_KEY", "")
    if key:
        return key
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("BRANDFETCH_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(
        "BRANDFETCH_API_KEY not found. Set it in the environment or in .env"
    )


def _lighten(hex6: str, factor: float) -> str:
    """Blend hex6 color with white. factor=0 → original, factor=1 → white."""
    r = int(hex6[0:2], 16)
    g = int(hex6[2:4], 16)
    b = int(hex6[4:6], 16)
    r = round(r + (255 - r) * factor)
    g = round(g + (255 - g) * factor)
    b = round(b + (255 - b) * factor)
    return f"{r:02X}{g:02X}{b:02X}"


def _darken(hex6: str, factor: float) -> str:
    """Blend hex6 color with black. factor=0 → original, factor=1 → black."""
    r = int(hex6[0:2], 16)
    g = int(hex6[2:4], 16)
    b = int(hex6[4:6], 16)
    r = round(r * (1 - factor))
    g = round(g * (1 - factor))
    b = round(b * (1 - factor))
    return f"{r:02X}{g:02X}{b:02X}"


def _resolve_domain(company_name: str) -> str | None:
    """Look up company_name via Brandfetch's search endpoint. Returns the domain or None."""
    search_url = f"https://api.brandfetch.io/v2/search/{quote(company_name)}"
    search_resp = requests.get(search_url, params={"c": _SEARCH_CLIENT}, timeout=10)
    search_data = search_resp.json()

    if not isinstance(search_data, list) or not search_data:
        print(f"  ⚠ Brandfetch: no results for '{company_name}'")
        return None

    domain = search_data[0].get("domain", "")
    if not domain:
        print("  ⚠ Brandfetch: no domain in search result")
        return None

    print(f"  ✓ Brandfetch: resolved '{company_name}' → {domain}")
    return domain


def _fetch_brand_data(domain: str, api_key: str) -> dict:
    """Fetch the full Brandfetch brand record for a resolved domain."""
    brand_url = f"https://api.brandfetch.io/v2/brands/domain/{quote(domain, safe='.')}"
    brand_resp = requests.get(
        brand_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )
    return brand_resp.json()


def _palette_from_hex(hex6: str, secondary_hex: str | None) -> dict:
    """Derive the four-color palette from a primary brand hex value.

    secondary_hex, if given, is a second distinct brand color pulled straight
    from Brandfetch. Otherwise a darkened shade of primary is used so it
    reads as a genuinely different accent, not just a lighter tint.
    """
    return {
        "primary":   hex6,
        "secondary": secondary_hex or _darken(hex6, 0.35),
        "border":    _lighten(hex6, 0.25),   # 25% lighter — section borders
        "fill":      _lighten(hex6, 0.85),    # 85% lighter — competency table bg
    }


def get_brand_color(company_name: str, domain: str | None = None) -> dict:
    """
    Look up the brand accent/dark/light colors for company_name via Brandfetch.

    If domain is given (e.g. the domain already resolved and stored on the
    application tracker record), it's used directly — company-name search is
    fuzzy and can match the wrong company entirely for a common/ambiguous
    name (a "Melior" search might not resolve to getmelior.com). Only falls
    back to a name search when no domain is known yet.

    Returns a palette dict: {primary, secondary, border, fill} as 6-char
    uppercase hex strings. Falls back to DEFAULT_PALETTE on any error.
    """
    if requests is None:
        print("  ⚠ brand_color: 'requests' not installed — using default colors")
        return DEFAULT_PALETTE

    try:
        api_key = _load_api_key()

        domain = domain.strip() if domain else _resolve_domain(company_name)
        if not domain:
            print("  ⚠ Brandfetch: using default colors")
            return DEFAULT_PALETTE

        brand_data = _fetch_brand_data(domain, api_key)

        # ── Pick primary color by priority, then look for a second, distinct
        #    color among the remaining priorities to use as secondary ────────
        colors = brand_data.get("colors", [])
        hex_val = None
        chosen_type = None
        for priority in _COLOR_PRIORITY:
            match = next((c for c in colors if c.get("type") == priority), None)
            if match and match.get("hex"):
                candidate = match["hex"].lstrip("#").upper()
                if _HEX_RE.match(candidate):
                    hex_val = candidate
                    chosen_type = priority
                    break
                print(f"  ⚠ Brandfetch: malformed hex '{match['hex']}' for type '{priority}' — skipping")

        if not hex_val:
            print("  ⚠ Brandfetch: no usable color in brand data — using default colors")
            return DEFAULT_PALETTE

        secondary_hex = None
        for priority in _COLOR_PRIORITY:
            if priority == chosen_type:
                continue
            match = next((c for c in colors if c.get("type") == priority), None)
            if match and match.get("hex"):
                candidate = match["hex"].lstrip("#").upper()
                if _HEX_RE.match(candidate) and candidate != hex_val:
                    secondary_hex = candidate
                    break

        palette = _palette_from_hex(hex_val, secondary_hex)
        print(f"  ✓ Brandfetch: {chosen_type} color #{hex_val} → "
              f"secondary #{palette['secondary']}, border #{palette['border']}, fill #{palette['fill']}")
        return palette

    except Exception as exc:
        print(f"  ⚠ Brandfetch lookup failed ({exc}) — using default colors")
        return DEFAULT_PALETTE


def get_brand_logo(company_name: str, domain: str | None = None) -> dict | None:
    """
    Look up and download company_name's logo via Brandfetch.

    See get_brand_color() for why a pre-resolved domain (when available)
    should always be passed instead of relying on a name search.

    Returns {"bytes": raw image bytes, "format": "png"/"jpeg", "width": int|None,
    "height": int|None}, or None if no logo is available or anything fails.
    """
    if requests is None:
        return None

    try:
        api_key = _load_api_key()

        domain = domain.strip() if domain else _resolve_domain(company_name)
        if not domain:
            return None

        brand_data = _fetch_brand_data(domain, api_key)

        logos = brand_data.get("logos", [])
        # Prefer the "icon" mark (square, reads well small in a doc header)
        # over the full "logo" wordmark/lockup, which is often wide and thin.
        ordered_logos = (
            [l for l in logos if l.get("type") == "icon"]
            + [l for l in logos if l.get("type") != "icon"]
        )

        for logo in ordered_logos:
            formats = logo.get("formats", [])
            fmt_match = next(
                (f for f in formats if str(f.get("format", "")).lower() in _LOGO_FORMATS),
                None,
            )
            if not fmt_match or not fmt_match.get("src"):
                continue

            img_resp = requests.get(fmt_match["src"], timeout=10)
            if img_resp.status_code != 200 or not img_resp.content:
                continue

            print(f"  ✓ Brandfetch: fetched logo for '{company_name}' "
                  f"({fmt_match.get('format')}, {fmt_match.get('width')}x{fmt_match.get('height')})")
            return {
                "bytes":  img_resp.content,
                "format": str(fmt_match["format"]).lower(),
                "width":  fmt_match.get("width"),
                "height": fmt_match.get("height"),
            }

        print(f"  ⚠ Brandfetch: no usable logo image for '{company_name}'")
        return None

    except Exception as exc:
        print(f"  ⚠ Brandfetch logo lookup failed ({exc}) — skipping logo")
        return None
