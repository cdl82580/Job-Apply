#!/usr/bin/env python3
"""
scripts/associate_runs.py — Retroactively link Drive run folders to tracker records.

Strategy:
  1. List all Drive run folders (name = CompanyName_RoleTitle format)
  2. Load all application records from Tigris
  3. Normalize both sides (lowercase, alphanumeric only)
  4. Score each folder against each application:
       - company score: substring containment check
       - role score:    character overlap ratio
  5. Link if best match score >= 0.55
  6. Print uncertain matches (0.35–0.55) for review

Usage (run inside the Fly container):
  python3 /app/scripts/associate_runs.py [--dry-run]
"""
from __future__ import annotations

import json
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts import storage
from scripts.applications import _app_key, _index_key, get_application

EMAIL    = "cdl825@gmail.com"
DRY_RUN  = "--dry-run" in sys.argv

AUTO_THRESHOLD    = 0.55   # link automatically
REVIEW_THRESHOLD  = 0.35   # print for manual review


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def norm(s: str) -> str:
    """Lowercase, alphanumeric only."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def company_score(folder_company: str, app_company: str) -> float:
    """1.0 if one contains the other; partial credit for shared prefix."""
    a, b = norm(folder_company), norm(app_company)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    # longest common prefix ratio
    common = 0
    for ca, cb in zip(a, b):
        if ca == cb:
            common += 1
        else:
            break
    return common / max(len(a), len(b))


def role_score(folder_role: str, app_role: str) -> float:
    """Character overlap ratio between normalised role strings."""
    a, b = norm(folder_role), norm(app_role)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    # sliding window: how much of shorter appears in longer?
    best = 0
    for i in range(len(longer) - len(shorter) + 1):
        matches = sum(x == y for x, y in zip(shorter, longer[i:]))
        best = max(best, matches)
    return best / len(shorter)


def combined_score(folder_name: str, app: dict) -> tuple[float, str]:
    """Return (score, debug_string) for a folder vs an application."""
    if "_" not in folder_name:
        fc, fr = folder_name, ""
    else:
        idx = folder_name.index("_")
        fc  = folder_name[:idx]
        fr  = folder_name[idx + 1:]

    cs = company_score(fc, app.get("company", ""))
    rs = role_score(fr, app.get("role_title", ""))
    score = cs * 0.6 + rs * 0.4
    return score, f"co={cs:.2f} role={rs:.2f}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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
    if DRY_RUN:
        print("** DRY RUN — no changes will be written **\n")

    # ── Load applications ───────────────────────────────────────────────
    raw = storage.get_text(_index_key(user_id))
    if not raw:
        print("No applications found")
        return
    index = json.loads(raw)
    print(f"Applications in tracker: {len(index)}")

    # Load full records (need linked_runs to avoid dupes)
    apps = []
    for entry in index:
        rec = get_application(user_id, entry["id"])
        if rec:
            apps.append(rec)
    print(f"Full records loaded:     {len(apps)}\n")

    # Build set of already-linked folder names to avoid dupes
    linked_folders: set[str] = set()
    for app in apps:
        for lr in app.get("linked_runs", []):
            linked_folders.add(lr.get("folder_name", ""))

    # ── List Drive folders ──────────────────────────────────────────────
    # Import apply.py utilities (needs Google credentials in the container)
    try:
        from apply import list_gdrive_run_folders, WorkflowConfig
    except ImportError as e:
        print(f"ERROR importing apply.py: {e}")
        sys.exit(1)

    config = WorkflowConfig(progress=lambda _: None, user_label=EMAIL)
    folders = list_gdrive_run_folders(EMAIL, config)
    print(f"Drive folders found: {len(folders)}\n")

    if not folders:
        print("No Drive folders to process.")
        return

    now_ts  = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    linked  = 0
    skipped = 0
    review  = []

    for folder in folders:
        fname = folder["name"]

        if fname in linked_folders:
            print(f"  SKIP (already linked): {fname}")
            skipped += 1
            continue

        # Score against every application
        scored = [(combined_score(fname, app), app) for app in apps]
        scored.sort(key=lambda x: -x[0][0])
        best_score, best_debug = scored[0][0]
        best_app = scored[0][1]

        # Determine run type from file-naming convention
        # Prep folders contain round type names; resume folders don't
        prep_keywords = {"phonescreen", "hiringmanager", "technical", "executive", "panel", "peer"}
        fname_norm = norm(fname)
        run_type = "interview_prep" if any(kw in fname_norm for kw in prep_keywords) else "resume"

        if best_score >= AUTO_THRESHOLD:
            gdrive_id = folder.get("id", "")
            run_info = {
                "id":               str(uuid.uuid4()),
                "type":             run_type,
                "folder_name":      fname,
                "folder_url":       folder.get("web_view_link", ""),
                "gdrive_folder_id": gdrive_id,
                "linked_at":        now_ts,
                "linked_by":        "associate_runs.py",
            }
            print(
                f"  LINK [{best_score:.2f}] {fname}\n"
                f"       → {best_app['company']} · {best_app['role_title']}  ({best_debug})"
            )
            if not DRY_RUN:
                best_app.setdefault("linked_runs", []).append(run_info)
                best_app.setdefault("audit_log", []).append({
                    "id":        str(uuid.uuid4()),
                    "action":    "run_linked",
                    "actor":     "associate_runs.py",
                    "timestamp": now_ts,
                    "ip":        None,
                    "details": {
                        "run_id":      run_info["id"],
                        "type":        run_type,
                        "folder_name": fname,
                    },
                })
                storage.put_text(_app_key(user_id, best_app["id"]), json.dumps(best_app))
                linked_folders.add(fname)
            linked += 1

        elif best_score >= REVIEW_THRESHOLD:
            review.append((best_score, best_debug, fname, best_app))
        else:
            print(f"  NO MATCH [{best_score:.2f}]: {fname}")

    print(f"\n── Summary ──────────────────────────────────────")
    print(f"  Linked:  {linked}")
    print(f"  Skipped: {skipped}")
    print(f"  Review:  {len(review)}")

    if review:
        print(f"\n── Needs manual review (score {REVIEW_THRESHOLD}–{AUTO_THRESHOLD}) ──")
        for score, debug, fname, app in review:
            print(
                f"  [{score:.2f}] {fname}\n"
                f"         → {app['company']} · {app['role_title']}  ({debug})"
            )


if __name__ == "__main__":
    main()
