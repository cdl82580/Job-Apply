#!/usr/bin/env python3
"""One-off: send a test verification email to cdl825@gmail.com."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts import storage, email_verification as ev

EMAIL = "cdl825@gmail.com"

user = storage.get_user_by_email(EMAIL)
if not user:
    print(f"User not found: {EMAIL}")
    sys.exit(1)

# Import the helper from api.py
sys.path.insert(0, "/app")
import os, requests as req_lib

api_key = os.environ.get("RESEND_API_KEY", "")
print(f"RESEND_API_KEY set: {bool(api_key)} ({api_key[:8]}...)" if api_key else "RESEND_API_KEY not set!")

from api import _send_verification_email, _FROM_ADDRESS
print(f"FROM: {_FROM_ADDRESS}")

token = ev.create_token(user["user_id"], EMAIL)
app_url = os.environ.get("APP_URL", "https://job-apply-corey.fly.dev")
verify_url = f"{app_url}/api/auth/verify-email?token={token}"
name = user.get("display_name", "Corey")

resp = req_lib.post(
    "https://api.resend.com/emails",
    json={"from": _FROM_ADDRESS, "to": [EMAIL],
          "subject": "Verify your email — Job Apply",
          "text": f"Hi {name}, verify your email: {verify_url}"},
    headers={"Authorization": f"Bearer {api_key}"},
    timeout=10,
)
print(f"Status: {resp.status_code}")
print(f"Body:   {resp.text}")
