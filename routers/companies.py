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

_BRANDFETCH_KEY     = os.environ.get("BRANDFETCH_API_KEY", "")
_BRANDFETCH_CDN_KEY = os.environ.get("BRANDFETCH_CDN_KEY", "")
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
    """Proxy BrandFetch CDN logo requests so the CDN key stays server-side."""
    if not _BRANDFETCH_CDN_KEY:
        raise HTTPException(503, "Logo service not configured")
    # Validate domain is a simple hostname with no path traversal
    if "/" in domain or "\\" in domain or domain.startswith("."):
        raise HTTPException(400, "Invalid domain")
    import requests as _req
    try:
        resp = _req.get(
            f"https://cdn.brandfetch.io/domain/{domain}",
            params={"c": _BRANDFETCH_CDN_KEY},
            timeout=8,
            stream=True,
        )
        if resp.status_code == 404:
            raise HTTPException(404, "Logo not found")
        resp.raise_for_status()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "Logo fetch failed")
    from fastapi.responses import Response
    ct = resp.headers.get("Content-Type", "image/png")
    return Response(content=resp.content, media_type=ct)
