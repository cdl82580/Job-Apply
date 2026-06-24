#!/usr/bin/env python3
"""
One-time backfill: reconstruct AgentRun records from existing audit events.

Run once after deploying the agent_runs module, then delete this script.

Usage:
    python3 -m scripts.backfill_agent_runs
"""

from __future__ import annotations

import json
import sys
import time

from . import storage, user_audit, agent_runs
from . import applications as app_store


def backfill() -> None:
    users = storage.list_all_users()
    created = 0
    skipped = 0

    # Build app cache for enrichment
    app_cache: dict[tuple[str, str], dict] = {}
    for uid_rec in users:
        uid = uid_rec.get("user_id", "")
        try:
            for app_sum in app_store.list_applications(uid).get("items", []):
                full = app_store.get_application(uid, app_sum["id"])
                if full:
                    app_cache[(uid, full["id"])] = {
                        "company": full.get("company", ""),
                        "role":    full.get("role_title", ""),
                    }
        except Exception:
            pass

    for uid_rec in users:
        uid = uid_rec.get("user_id", "")
        user_email = uid_rec.get("email", "")
        events = user_audit.get_events(uid)

        # Index events by run_id for completion lookups
        events_by_action: dict[str, list[dict]] = {}
        for ev in events:
            events_by_action.setdefault(ev.get("action", ""), []).append(ev)

        def _find_completion(action: str, id_key: str, id_val: str) -> dict | None:
            for ev in events_by_action.get(action, []):
                d = ev.get("details") or {}
                if d.get(id_key) == id_val:
                    return ev
            return None

        # Resume runs
        for ev in events_by_action.get("run_started", []):
            d = ev.get("details") or {}
            run_id = d.get("run_id", "")
            if not run_id:
                continue
            if agent_runs.get(uid, run_id):
                skipped += 1
                continue
            app_id = d.get("app_id", "")
            app_info = app_cache.get((uid, app_id), {})

            rec = agent_runs.create(
                run_id=run_id, run_type="resume", user_id=uid,
                user_email=user_email, company=d.get("company", ""),
                role=d.get("role", ""), app_id=app_id,
                initiated_by=user_email,
            )
            # Backdate started_at
            agent_runs.update(uid, run_id, started_at=ev.get("timestamp", ""))

            comp = _find_completion("run_completed", "run_id", run_id)
            fail = _find_completion("run_failed", "run_id", run_id)
            if comp:
                cd = comp.get("details") or {}
                folder_url = cd.get("folder_url", "")
                folder_id = folder_url.rstrip("/").split("/")[-1] if folder_url else ""
                agent_runs.complete(uid, run_id,
                                    gdrive_folder_id=folder_id,
                                    gdrive_folder_url=folder_url)
                agent_runs.update(uid, run_id, finished_at=comp.get("timestamp", ""))
            elif fail:
                fd = fail.get("details") or {}
                agent_runs.fail(uid, run_id, fd.get("error", "Unknown error"))
                agent_runs.update(uid, run_id, finished_at=fail.get("timestamp", ""))
            else:
                agent_runs.update(uid, run_id, status="completed")
            created += 1

        # Interview prep runs
        for ev in events_by_action.get("prep_started", []):
            d = ev.get("details") or {}
            prep_id = d.get("prep_id", "")
            if not prep_id:
                continue
            if agent_runs.get(uid, prep_id):
                skipped += 1
                continue
            app_id = d.get("app_id", "")
            agent_runs.create(
                run_id=prep_id, run_type="interview_prep", user_id=uid,
                user_email=user_email, company=d.get("company", ""),
                role=d.get("role", ""), app_id=app_id,
                initiated_by=user_email, round_type=d.get("round_type", ""),
            )
            agent_runs.update(uid, prep_id, started_at=ev.get("timestamp", ""))

            comp = _find_completion("prep_completed", "prep_id", prep_id)
            fail = _find_completion("prep_failed", "prep_id", prep_id)
            if comp:
                cd = comp.get("details") or {}
                folder_url = cd.get("folder_url", "")
                folder_id = folder_url.rstrip("/").split("/")[-1] if folder_url else ""
                agent_runs.complete(uid, prep_id,
                                    gdrive_folder_id=folder_id,
                                    gdrive_folder_url=folder_url)
                agent_runs.update(uid, prep_id, finished_at=comp.get("timestamp", ""))
            elif fail:
                fd = fail.get("details") or {}
                agent_runs.fail(uid, prep_id, fd.get("error", "Unknown error"))
                agent_runs.update(uid, prep_id, finished_at=fail.get("timestamp", ""))
            else:
                agent_runs.update(uid, prep_id, status="completed")
            created += 1

        # Optimize runs
        for ev in events_by_action.get("optimize_started", []):
            d = ev.get("details") or {}
            opt_id = d.get("run_id") or d.get("optimize_id", "")
            if not opt_id:
                continue
            if agent_runs.get(uid, opt_id):
                skipped += 1
                continue
            app_id = d.get("app_id", "")
            app_info = app_cache.get((uid, app_id), {})
            agent_runs.create(
                run_id=opt_id, run_type="optimize", user_id=uid,
                user_email=user_email,
                company=app_info.get("company", d.get("company", "")),
                role=app_info.get("role", d.get("role", "")),
                app_id=app_id, initiated_by=user_email,
                gdrive_folder_id=d.get("folder_id", ""),
            )
            agent_runs.update(uid, opt_id, started_at=ev.get("timestamp", ""))

            comp = _find_completion("optimize_completed", "run_id", opt_id)
            fail = _find_completion("optimize_failed", "run_id", opt_id)
            if comp:
                cd = comp.get("details") or {}
                folder_url = cd.get("folder_url", "")
                agent_runs.complete(uid, opt_id, gdrive_folder_url=folder_url)
                agent_runs.update(uid, opt_id, finished_at=comp.get("timestamp", ""))
            elif fail:
                fd = fail.get("details") or {}
                agent_runs.fail(uid, opt_id, fd.get("error", "Unknown error"))
                agent_runs.update(uid, opt_id, finished_at=fail.get("timestamp", ""))
            else:
                agent_runs.update(uid, opt_id, status="completed")
            created += 1

        # AQ runs
        for ev in events_by_action.get("aq_started", []):
            d = ev.get("details") or {}
            aq_id = d.get("aq_id", "")
            if not aq_id:
                continue
            if agent_runs.get(uid, aq_id):
                skipped += 1
                continue
            app_id = d.get("app_id", "")
            app_info = app_cache.get((uid, app_id), {})
            agent_runs.create(
                run_id=aq_id, run_type="aq", user_id=uid,
                user_email=user_email,
                company=app_info.get("company", d.get("company", "")),
                role=app_info.get("role", d.get("role", "")),
                app_id=app_id, initiated_by=user_email,
            )
            agent_runs.update(uid, aq_id, started_at=ev.get("timestamp", ""))

            comp = _find_completion("aq_completed", "aq_id", aq_id)
            fail = _find_completion("aq_failed", "aq_id", aq_id)
            if comp:
                agent_runs.complete(uid, aq_id)
                agent_runs.update(uid, aq_id, finished_at=comp.get("timestamp", ""))
            elif fail:
                fd = fail.get("details") or {}
                agent_runs.fail(uid, aq_id, fd.get("error", "Unknown error"))
                agent_runs.update(uid, aq_id, finished_at=fail.get("timestamp", ""))
            else:
                agent_runs.update(uid, aq_id, status="completed")
            created += 1

        # Thank you runs
        for ev in events_by_action.get("thankyou_started", []):
            d = ev.get("details") or {}
            ty_id = d.get("ty_id", "")
            if not ty_id:
                continue
            if agent_runs.get(uid, ty_id):
                skipped += 1
                continue
            app_id = d.get("app_id", "")
            app_info = app_cache.get((uid, app_id), {})
            agent_runs.create(
                run_id=ty_id, run_type="thank_you", user_id=uid,
                user_email=user_email,
                company=app_info.get("company", d.get("company", "")),
                role=app_info.get("role", d.get("role", "")),
                app_id=app_id, initiated_by=user_email,
            )
            agent_runs.update(uid, ty_id, started_at=ev.get("timestamp", ""))

            comp = _find_completion("thankyou_completed", "ty_id", ty_id)
            fail = _find_completion("thankyou_failed", "ty_id", ty_id)
            if comp:
                cd = comp.get("details") or {}
                folder_url = cd.get("folder_url", "")
                agent_runs.complete(uid, ty_id, gdrive_folder_url=folder_url)
                agent_runs.update(uid, ty_id, finished_at=comp.get("timestamp", ""))
            elif fail:
                fd = fail.get("details") or {}
                agent_runs.fail(uid, ty_id, fd.get("error", "Unknown error"))
                agent_runs.update(uid, ty_id, finished_at=fail.get("timestamp", ""))
            else:
                agent_runs.update(uid, ty_id, status="completed")
            created += 1

        # Match scoring (from audit log — these are fire-and-forget, always complete)
        for ev in events_by_action.get("match_scored", []):
            d = ev.get("details") or {}
            score_id = ev.get("id", "")
            if not score_id:
                continue
            if agent_runs.get(uid, score_id):
                skipped += 1
                continue
            app_id = d.get("app_id", "")
            app_info = app_cache.get((uid, app_id), {})
            agent_runs.create(
                run_id=score_id, run_type="scoring", user_id=uid,
                user_email=user_email,
                company=app_info.get("company", ""),
                role=app_info.get("role", ""),
                app_id=app_id, initiated_by="system",
                score=d.get("score"),
                score_category=d.get("category", ""),
            )
            ts = ev.get("timestamp", "")
            agent_runs.update(uid, score_id, started_at=ts, status="completed", finished_at=ts)
            created += 1

    print(f"Backfill complete: {created} created, {skipped} skipped (already exist)")


if __name__ == "__main__":
    backfill()
