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
import os, json, urllib.request, urllib.error

api_key = os.environ.get("RESEND_API_KEY", "")
print(f"RESEND_API_KEY set: {bool(api_key)} ({api_key[:8]}...)" if api_key else "RESEND_API_KEY not set!")

from api import _send_verification_email, _FROM_ADDRESS
print(f"FROM: {_FROM_ADDRESS}")

token = ev.create_token(user["user_id"], EMAIL)

# Call with verbose error
app_url = os.environ.get("APP_URL", "https://job-apply-corey.fly.dev")
verify_url = f"{app_url}/api/auth/verify-email?token={token}"
name = user.get("display_name", "Corey")

payload = json.dumps({
    "from": _FROM_ADDRESS,
    "to": [EMAIL],
    "subject": "Verify your email — Job Apply",
    "text": f"Hi {name}, verify your email: {verify_url}",
}).encode()
req = urllib.request.Request(
    "https://api.resend.com/emails", data=payload,
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read()
        print(f"Success {r.status}: {body}")
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode()}")
except Exception as e:
    print(f"Error: {e}")
