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

_BRANDFETCH_KEY = os.environ.get("BRANDFETCH_API_KEY", "1idZFX8Ll28d4x2IVye")
_BRANDFETCH_URL = "https://api.brandfetch.io/v2/search/{query}"


@router.get("/search")
async def search_companies(q: str = Query(..., min_length=1)):
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
        # Prefer the CDN URL pattern (more reliable for display sizes)
        logo_url = f"https://cdn.brandfetch.io/domain/{domain}?c={_BRANDFETCH_KEY}" if domain else icon

        results.append({
            "name":        item.get("name", ""),
            "domain":      domain,
            "logo_url":    logo_url,
            "icon_url":    icon,           # original icon from response
            "description": item.get("claim", ""),
        })

    return results
