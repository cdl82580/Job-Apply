#!/usr/bin/env python3
"""One-off: send a test verification email to cdl825@gmail.com."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts import storage, email_verification as ev
import os

EMAIL = "cdl825@gmail.com"

api_key = os.environ.get("RESEND_API_KEY", "")
print(f"RESEND_API_KEY set: {bool(api_key)} ({api_key[:8]}...)" if api_key else "RESEND_API_KEY not set!")

from api import _send_verification_email, _FROM_ADDRESS
print(f"FROM: {_FROM_ADDRESS}")

user = storage.get_user_by_email(EMAIL)
if not user:
    print(f"User not found: {EMAIL}")
    sys.exit(1)

token = ev.create_token(user["user_id"], EMAIL)
name = user.get("display_name", "Corey")

ok = _send_verification_email(EMAIL, name, token)
print(f"Sent: {ok}")
