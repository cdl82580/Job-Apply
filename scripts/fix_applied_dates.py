#!/usr/bin/env python3
"""
scripts/fix_applied_dates.py

For every application record where date_applied is empty AND status is not
"Researching" or "Not Applying", set date_applied to the record's created_at.
"""
from __future__ import annotations
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import storage
from scripts.applications import _app_key, _index_key, _to_index_entry

SKIP_STATUSES = {"Researching", "Not Applying"}
EMAIL         = "cdl825@gmail.com"


def main() -> None:
    user = storage.get_user_by_email(EMAIL)
    user_id = user["user_id"]

    raw   = storage.get_text(_index_key(user_id))
    index = json.loads(raw) if raw else []

    updated   = 0
    skipped   = 0
    new_index = []

    for entry in index:
        rec = json.loads(storage.get_text(_app_key(user_id, entry["id"])) or "{}")
        if not rec:
            new_index.append(entry)
            continue

        status       = rec.get("status", "")
        date_applied = rec.get("date_applied")
        created_at   = rec.get("created_at", "")

        if not date_applied and status not in SKIP_STATUSES and created_at:
            rec["date_applied"] = created_at
            storage.put_text(_app_key(user_id, rec["id"]), json.dumps(rec))
            new_index.append(_to_index_entry(rec))
            updated += 1
            print(f"  SET  [{status:15s}] {rec.get('company','?'):35s} → {created_at}")
        else:
            new_index.append(_to_index_entry(rec))
            skipped += 1

    storage.put_text(_index_key(user_id), json.dumps(new_index))
    print(f"\nDone — {updated} updated, {skipped} skipped")


if __name__ == "__main__":
    main()
