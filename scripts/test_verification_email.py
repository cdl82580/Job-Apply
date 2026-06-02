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
from api import _send_verification_email

token = ev.create_token(user["user_id"], EMAIL)
sent  = _send_verification_email(EMAIL, user.get("display_name", "Corey"), token)
print(f"Sent: {sent}")
