"""
routers/companies.py — BrandFetch company search proxy.

GET /api/companies/search?q=salesforce
Returns up to 5 matches: [{name, domain, logo_url, description}]
"""

from __future__ import annotations

import os

import requests
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/companies", tags=["companies"])

_BRANDFETCH_KEY = os.environ.get("BRANDFETCH_API_KEY", "")
_BRANDFETCH_URL     = "https://api.brandfetch.io/v2/search/{query}"


@router.get("/search")
async def search_companies(q: str = Query(..., min_length=1)):
    if not _BRANDFETCH_KEY:
        raise HTTPException(503, "Company search not configured (BRANDFETCH_API_KEY not set)")
    try:
        resp = requests.get(
            _BRANDFETCH_URL.format(query=q),
            params={"c": _BRANDFETCH_KEY},
            timeout=8,
        )
        resp.raise_for_status()
        raw = resp.json()
    except requests.Timeout:
        raise HTTPException(504, "Company search timed out")
    except Exception as exc:
        raise HTTPException(502, f"Company search failed: {exc}")

    results = []
    for item in raw[:5]:
        domain = item.get("domain", "")
        icon   = item.get("icon", "")
        if not isinstance(icon, str):
            icon = ""
        # Use the icon URL from the response directly — it's a signed CDN URL
        # that works immediately in <img> tags. The domain CDN URL is used
        # on the list view (after the record is saved) via the domain field.
        results.append({
            "name":        item.get("name", ""),
            "domain":      domain,
            "logo_url":    icon,   # ready-to-use signed URL for dropdown display
            "description": item.get("claim", ""),
        })

    return results


@router.get("/logo")
async def company_logo(domain: str = Query(..., min_length=1)):
    """Fetch a fresh logo URL for the domain via BrandFetch search, then proxy the image."""
    if not _BRANDFETCH_KEY:
        raise HTTPException(503, "Logo service not configured")
    if "/" in domain or "\\" in domain or domain.startswith("."):
        raise HTTPException(400, "Invalid domain")
    try:
        search = requests.get(
            _BRANDFETCH_URL.format(query=domain),
            params={"c": _BRANDFETCH_KEY},
            timeout=8,
        )
        search.raise_for_status()
        results = search.json()
    except Exception as exc:
        raise HTTPException(502, f"Logo search failed: {exc}")

    icon_url = None
    for item in results:
        if (item.get("domain") or "").lower() == domain.lower():
            icon_url = item.get("icon") or ""
            break
    if not icon_url and results:
        icon_url = results[0].get("icon") or ""
    if not icon_url:
        raise HTTPException(404, "Logo not found")

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=icon_url, status_code=302)
