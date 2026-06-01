"""
routers/auth_google.py — Google OAuth 2.0 login.

Flow:
  GET /api/auth/google           → redirect to Google consent screen
  GET /api/auth/google/callback  → exchange code, find/create user, set session cookie

Env vars required:
  GOOGLE_CLIENT_ID
  GOOGLE_CLIENT_SECRET
  APP_URL  (e.g. https://job-apply-corey.fly.dev)
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import urllib.parse
import uuid

import requests
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from scripts import storage

router = APIRouter(tags=["auth"])

_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
_APP_URL       = os.environ.get("APP_URL", "https://job-apply-corey.fly.dev")
_REDIRECT_URI  = f"{_APP_URL}/api/auth/google/callback"

_GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_KEYS_URL  = "https://www.googleapis.com/oauth2/v3/tokeninfo"

_SESSION_COOKIE = "session"
_SESSION_DAYS   = 30


def _create_session(user_id: str, email: str, secret: str) -> str:
    """Mirror the token format from api.py — import avoided to prevent circular deps."""
    import base64
    import hmac

    payload = base64.urlsafe_b64encode(json.dumps({
        "user_id": user_id,
        "email":   email,
        "exp":     int(time.time()) + 86400 * _SESSION_DAYS,
    }).encode()).rstrip(b"=").decode()
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


@router.get("/api/auth/google")
async def google_login(request: Request, returnTo: str = "/"):
    if not _CLIENT_ID or not _CLIENT_SECRET:
        return RedirectResponse(
            f"{_APP_URL}/login.html?auth_error=Google+OAuth+not+configured",
            status_code=302,
        )

    if not returnTo.startswith("/"):
        returnTo = "/"

    state = json.dumps({"returnTo": returnTo})
    params = {
        "client_id":     _CLIENT_ID,
        "redirect_uri":  _REDIRECT_URI,
        "response_type": "code",
        "scope":         "openid email profile",
        "access_type":   "offline",
        "prompt":        "select_account",
        "state":         state,
    }
    url = f"{_GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url, status_code=302)


@router.get("/api/auth/google/callback")
async def google_callback(
    request: Request,
    code: str | None = None,
    error: str | None = None,
    state: str | None = None,
):
    fail_base = f"{_APP_URL}/login.html?auth_error="

    return_to = "/"
    if state:
        try:
            return_to = json.loads(state).get("returnTo", "/")
        except Exception:
            pass
    if not return_to.startswith("/"):
        return_to = "/"

    if error or not code:
        msg = urllib.parse.quote(error or "cancelled")
        return RedirectResponse(f"{fail_base}{msg}", status_code=302)

    # Exchange code for tokens
    try:
        token_resp = requests.post(
            _GOOGLE_TOKEN_URL,
            data={
                "code":          code,
                "client_id":     _CLIENT_ID,
                "client_secret": _CLIENT_SECRET,
                "redirect_uri":  _REDIRECT_URI,
                "grant_type":    "authorization_code",
            },
            timeout=10,
        )
        token_resp.raise_for_status()
        tokens = token_resp.json()
    except Exception as exc:
        msg = urllib.parse.quote(f"Token exchange failed: {exc}")
        return RedirectResponse(f"{fail_base}{msg}", status_code=302)

    # Verify the id_token by calling Google's tokeninfo endpoint
    try:
        info_resp = requests.get(
            _GOOGLE_KEYS_URL,
            params={"id_token": tokens["id_token"]},
            timeout=10,
        )
        info_resp.raise_for_status()
        info = info_resp.json()
    except Exception as exc:
        msg = urllib.parse.quote(f"Token verification failed: {exc}")
        return RedirectResponse(f"{fail_base}{msg}", status_code=302)

    if info.get("aud") != _CLIENT_ID:
        return RedirectResponse(
            f"{fail_base}{urllib.parse.quote('Invalid token audience')}",
            status_code=302,
        )

    google_id = info.get("sub", "")
    email     = info.get("email", "").strip().lower()
    name      = info.get("name", email.split("@")[0])

    if not email or not info.get("email_verified"):
        return RedirectResponse(
            f"{fail_base}{urllib.parse.quote('Google account email is not verified')}",
            status_code=302,
        )

    # Find or create user
    user = storage.get_user_by_google_id(google_id)

    if not user:
        # Check for existing account with same email — link it
        user = storage.get_user_by_email(email)
        if user:
            user["google_id"] = google_id
            storage.save_user(user)
        else:
            # Brand-new user via Google
            user_id = str(uuid.uuid4())
            user = {
                "user_id":       user_id,
                "email":         email,
                "display_name":  name,
                "password_hash": f"google:{secrets.token_hex(32)}",  # unusable placeholder
                "google_id":     google_id,
                "created_at":    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            storage.save_user(user)

    # Issue session cookie using the same secret as api.py
    from api import _SESSION_SECRET, FLY_MACHINE_ID  # noqa: PLC0415

    token = _create_session(user["user_id"], email, _SESSION_SECRET)
    response = RedirectResponse(f"{_APP_URL}{return_to}", status_code=302)
    response.set_cookie(
        _SESSION_COOKIE, token,
        max_age=86400 * _SESSION_DAYS,
        httponly=True, samesite="lax", secure=True,
    )
    if FLY_MACHINE_ID:
        response.set_cookie("fly-force-instance-id", FLY_MACHINE_ID, path="/", samesite="lax")
    return response
