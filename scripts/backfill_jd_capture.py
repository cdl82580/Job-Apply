#!/usr/bin/env python3
"""
scripts/backfill_jd_capture.py

One-off backfill: for every application in "Researching" status that has a
posting URL but no linked job_description Drive folder yet, run the same
auto-capture pipeline that now fires automatically on application creation
(ensure Drive folder -> extract JD via Claude -> save job_description.md ->
link folder to the application record).

Run on the Fly machine (has AWS/Drive/Anthropic creds):
  fly ssh console -C "python3 scripts/backfill_jd_capture.py"
"""
from __future__ import annotations
import json, sys, time, uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts import storage, applications as app_store
from scripts.applications import _app_key, _index_key

EMAIL = "cdl825@gmail.com"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _audit_entry(action: str, actor: str, changes: dict | None = None) -> dict:
    return {
        "id":        str(uuid.uuid4()),
        "action":    action,
        "actor":     actor,
        "timestamp": _now(),
        "changes":   changes,
    }


def main() -> None:
    from apply import auto_capture_job_description, safe_filename, WorkflowConfig

    user = storage.get_user_by_email(EMAIL)
    user_id = user["user_id"]
    actor = EMAIL

    raw = storage.get_text(_index_key(user_id))
    index = json.loads(raw) if raw else []

    targets = []
    for entry in index:
        rec = json.loads(storage.get_text(_app_key(user_id, entry["id"])) or "{}")
        if not rec or rec.get("status") != "Researching" or not rec.get("url"):
            continue
        if any(l.get("type") == "job_description" for l in rec.get("linked_runs", [])):
            continue
        targets.append(rec)

    print(f"Found {len(targets)} Researching applications to process\n")

    for rec in targets:
        company, role, url, app_id = rec["company"], rec["role_title"], rec["url"], rec["id"]
        print(f"--- {company} / {role} ---")

        config = WorkflowConfig(progress=lambda m: print(f"  {m}"), user_label=actor)
        folder = auto_capture_job_description(company, role, url, config)
        if not folder:
            print("  ✗ Could not resolve Drive folder — skipping link step\n")
            continue

        folder_id, folder_url = folder
        folder_name = f"{safe_filename(company)}_{safe_filename(role)}"
        record = app_store.link_run(user_id, app_id, {
            "id":               str(uuid.uuid4()),
            "type":             "job_description",
            "folder_name":      folder_name,
            "folder_url":       folder_url,
            "gdrive_folder_id": folder_id,
            "linked_at":        _now(),
            "linked_by":        "system",
        })
        if record:
            record.setdefault("audit_log", []).append(
                _audit_entry("run_linked", "system", {
                    "type": "job_description", "folder_name": folder_name,
                })
            )
            app_store.save_application(user_id, record)
            print(f"  ✓ Linked Drive folder to application record\n")
        else:
            print("  ✗ Application record not found when linking\n")

        time.sleep(1)

    print("Done.")


if __name__ == "__main__":
    main()
