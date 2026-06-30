#!/usr/bin/env python3
"""
scripts/create_admin.py — Create or promote an admin user account.

Usage:
  python3 /app/scripts/create_admin.py [email] [password]

If email/password are omitted, defaults are used.
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts import storage


def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk   = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt:{salt.hex()}:{dk.hex()}"


def main() -> None:
    if not storage.is_configured():
        print("ERROR: Storage not configured")
        sys.exit(1)

    email    = sys.argv[1] if len(sys.argv) > 1 else "admin@job-apply.local"
    password = sys.argv[2] if len(sys.argv) > 2 else None

    if not password:
        # Generate a secure readable password
        import secrets as _s
        import string
        alphabet = string.ascii_letters + string.digits
        password = "Admin!" + "".join(_s.choice(alphabet) for _ in range(10))

    email = email.strip().lower()

    existing = storage.get_user_by_email(email)
    if existing:
        # Promote existing account to admin
        existing["role"] = "admin"
        storage.save_user(existing)
        print(f"Promoted existing account to admin:")
        print(f"  Email:    {email}")
        print(f"  User ID:  {existing['user_id']}")
        print(f"  Password: (unchanged — use existing password)")
        return

    user_id = str(uuid.uuid4())
    user = {
        "user_id":      user_id,
        "email":        email,
        "display_name": "Admin",
        "password_hash": _hash_password(password),
        "role":         "admin",
        "created_at":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    storage.save_user(user)

    print("Admin account created:")
    print(f"  Email:    {email}")
    print(f"  Password: {password}")
    print(f"  User ID:  {user_id}")
    print()
    print("Log in at: https://flowshift.cdlav.us/login.html")


if __name__ == "__main__":
    main()
