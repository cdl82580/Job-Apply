#!/usr/bin/env python3
"""
scripts/fix_created_dates.py — Backfill created_at on all application records.

Rule: created_at = min(date_applied, last_updated) if either exists, else today.

Usage:
  python3 /app/scripts/fix_created_dates.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts import storage
from scripts.applications import _app_key, _index_key, _to_index_entry

TODAY = "2026-06-01T00:00:00Z"
EMAIL = "cdl825@gmail.com"


def main() -> None:
    if not storage.is_configured():
        print("ERROR: storage not configured")
        sys.exit(1)

    user = storage.get_user_by_email(EMAIL)
    if not user:
        print(f"ERROR: user {EMAIL} not found")
        sys.exit(1)

    user_id = user["user_id"]
    print(f"User: {EMAIL}  ({user_id})")

    raw = storage.get_text(_index_key(user_id))
    if not raw:
        print("No index found — nothing to do")
        return

    index = json.loads(raw)
    print(f"Index entries: {len(index)}")

    new_index = []
    updated = 0
    skipped = 0

    for entry in index:
        full_raw = storage.get_text(_app_key(user_id, entry["id"]))
        if not full_raw:
            print(f"  SKIP (record missing): {entry.get('id')}")
            new_index.append(entry)
            skipped += 1
            continue

        rec = json.loads(full_raw)

        # Pick the earlier of the two dates; fall back to today
        candidates = [d for d in [rec.get("date_applied"), rec.get("last_updated")] if d]
        new_created = min(candidates) if candidates else TODAY

        old_created = rec.get("created_at", "")
        rec["created_at"] = new_created

        storage.put_text(_app_key(user_id, rec["id"]), json.dumps(rec))
        new_index.append(_to_index_entry(rec))
        updated += 1

        change = f"{old_created} → {new_created}" if old_created != new_created else "(unchanged)"
        print(f"  ✓ {rec.get('company','?'):35s}  {change}")

    storage.put_text(_index_key(user_id), json.dumps(new_index))
    print(f"\nDone — {updated} updated, {skipped} skipped")


if __name__ == "__main__":
    main()
