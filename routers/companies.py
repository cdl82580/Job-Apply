"""
routers/companies.py — Logo.dev company search proxy.

GET /api/companies/search?q=salesforce
Returns up to 5 matches: [{name, domain, description}]
Logo URLs are built by each caller directly from domain + the public
pk_ CDN token (frontend/*.html hardcode it client-side; the Teams bot
builds it in teams_bot/bot.py:_logo_url) rather than from a field here.
"""

from __future__ import annotations

import os

import requests
from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api/companies", tags=["companies"])

_LOGODEV_KEY    = os.environ.get("LOGODEV_API_KEY", "")
_LOGODEV_SEARCH = "https://api.logo.dev/search"


@router.get("/search")
async def search_companies(request: Request, q: str = Query(..., min_length=1)):
    from api import _check_rate_limit  # deferred: api.py imports this router at module load time
    _check_rate_limit(request, "company_search", max_hits=30, window_secs=60)

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
        results.append({
            "name":        item.get("name", ""),
            "domain":      item.get("domain", ""),
            "description": item.get("description", ""),
        })

    return results
