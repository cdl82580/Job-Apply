"""
routers/companies.py — Logo.dev company search proxy.

GET /api/companies/search?q=salesforce
Returns up to 5 matches: [{name, domain, logo_url, description}]
"""

from __future__ import annotations

import os

import requests
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/companies", tags=["companies"])

_LOGODEV_KEY    = os.environ.get("LOGODEV_API_KEY", "")
_LOGODEV_SEARCH = "https://api.logo.dev/search"
_LOGODEV_CDN    = "https://img.logo.dev/{domain}?token={token}"


@router.get("/search")
async def search_companies(q: str = Query(..., min_length=1)):
    if not _LOGODEV_KEY:
        raise HTTPException(503, "Company search not configured (LOGODEV_API_KEY not set)")
    try:
        resp = requests.get(
            _LOGODEV_SEARCH,
            params={"q": q},
            headers={"Authorization": f"Bearer {_LOGODEV_KEY}"},
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
        domain   = item.get("domain", "")
        logo_url = item.get("logo_url") or (
            _LOGODEV_CDN.format(domain=domain, token=_LOGODEV_KEY) if domain else ""
        )
        results.append({
            "name":        item.get("name", ""),
            "domain":      domain,
            "logo_url":    logo_url,
            "description": item.get("description", ""),
        })

    return results


@router.get("/logo")
async def company_logo(domain: str = Query(..., min_length=1)):
    """Redirect to the Logo.dev CDN URL for the given domain."""
    if not _LOGODEV_KEY:
        raise HTTPException(503, "Logo service not configured")
    if "/" in domain or "\\" in domain or domain.startswith("."):
        raise HTTPException(400, "Invalid domain")

    from fastapi.responses import RedirectResponse
    return RedirectResponse(
        url=_LOGODEV_CDN.format(domain=domain, token=_LOGODEV_KEY),
        status_code=302,
    )
