"""
routers/companies.py — Logo.dev company search proxy.

GET /api/companies/search?q=salesforce
Returns up to 5 matches: [{name, domain, description, logo_url}]
logo_url is pre-built server-side with the public CDN token (same key
api.py/notif_dispatch.py use for rendered logo <img> tags) so callers —
including the Teams bot's typeahead search — don't need their own copy
of the token.
"""

from __future__ import annotations

import os

import requests
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/companies", tags=["companies"])

_LOGODEV_KEY     = os.environ.get("LOGODEV_API_KEY", "")
_LOGODEV_PUB_KEY = os.environ.get("LOGODEV_PUBLIC_KEY") or _LOGODEV_KEY
_LOGODEV_SEARCH  = "https://api.logo.dev/search"


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
        domain = item.get("domain", "")
        logo_url = (
            f"https://img.logo.dev/{domain}?token={_LOGODEV_PUB_KEY}&size=64"
            if domain and _LOGODEV_PUB_KEY else ""
        )
        results.append({
            "name":        item.get("name", ""),
            "domain":      domain,
            "description": item.get("description", ""),
            "logo_url":    logo_url,
        })

    return results
