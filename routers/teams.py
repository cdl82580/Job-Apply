"""
routers/teams.py — Microsoft Teams Bot Framework webhook.

Mounts the bot built in teams_bot/ directly onto the main FastAPI app so it
shares the web process's public domain/TLS cert instead of needing its own
Fly machine and port. teams_bot/ has no __init__.py and uses flat
`from config import Config` / `import api_client` imports (mirroring how it's
run standalone via `python app.py`), so we add it to sys.path once at import
time rather than rewriting it as a package.

  POST /api/messages — Bot Framework webhook (Azure Bot -> here)
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings, TurnContext
from botbuilder.schema import Activity
from fastapi import APIRouter, HTTPException, Request, Response

_TEAMS_BOT_DIR = str(Path(__file__).resolve().parent.parent / "teams_bot")
if _TEAMS_BOT_DIR not in sys.path:
    sys.path.insert(0, _TEAMS_BOT_DIR)

from config import Config as TeamsConfig  # noqa: E402
from bot import JobApplyBot  # noqa: E402

router = APIRouter(tags=["teams"])

_SETTINGS = BotFrameworkAdapterSettings(
    TeamsConfig.APP_ID,
    TeamsConfig.APP_PASSWORD,
    channel_auth_tenant=TeamsConfig.APP_TENANT_ID or None,
)
_ADAPTER = BotFrameworkAdapter(_SETTINGS)
_BOT = JobApplyBot()


async def _on_error(context: TurnContext, error: Exception):
    print(f"\n[teams on_turn_error] unhandled error: {error}", file=sys.stderr)
    traceback.print_exc()
    await context.send_activity("Sorry, something went wrong. Please try again.")


_ADAPTER.on_turn_error = _on_error


@router.post("/api/messages")
async def teams_messages(request: Request):
    if "application/json" not in request.headers.get("Content-Type", ""):
        raise HTTPException(status_code=415, detail="Unsupported Media Type")

    body = await request.json()
    activity = Activity().deserialize(body)
    auth_header = request.headers.get("Authorization", "")

    invoke_response = await _ADAPTER.process_activity(activity, auth_header, _BOT.on_turn)
    if invoke_response:
        return Response(
            content=json.dumps(invoke_response.body) if invoke_response.body else None,
            status_code=invoke_response.status,
            media_type="application/json",
        )
    return Response(status_code=201)
