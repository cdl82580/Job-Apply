"""
Entry point — aiohttp web server that receives Teams webhook POSTs
and routes them through the Bot Framework adapter to JobApplyBot.
"""

import sys
import traceback

from aiohttp import web
from aiohttp.web import Request, Response

from botbuilder.core import (
    BotFrameworkAdapterSettings,
    TurnContext,
)
from botbuilder.core.integration import aiohttp_error_middleware
from botbuilder.integration.aiohttp import BotFrameworkHttpAdapter

from config import Config
from bot import JobApplyBot

SETTINGS = BotFrameworkAdapterSettings(Config.APP_ID, Config.APP_PASSWORD)
ADAPTER = BotFrameworkHttpAdapter(SETTINGS)


async def on_error(context: TurnContext, error: Exception):
    print(f"\n[on_turn_error] unhandled error: {error}", file=sys.stderr)
    traceback.print_exc()
    await context.send_activity("Sorry, something went wrong. Please try again.")


ADAPTER.on_turn_error = on_error

BOT = JobApplyBot()


async def messages(req: Request) -> Response:
    if "application/json" not in req.headers.get("Content-Type", ""):
        return Response(status=415)

    body = await req.json()
    activity = Activity().deserialize(body)

    auth_header = req.headers.get("Authorization", "")
    response = await ADAPTER.process_activity(activity, auth_header, BOT.on_turn)
    if response:
        return Response(body=response.body, status=response.status)
    return Response(status=201)


async def health(req: Request) -> Response:
    return Response(text="OK")


# botbuilder ships its own Activity deserialiser
from botbuilder.schema import Activity

APP = web.Application(middlewares=[aiohttp_error_middleware])
APP.router.add_post("/api/messages", messages)
APP.router.add_get("/health", health)

if __name__ == "__main__":
    web.run_app(APP, host="0.0.0.0", port=Config.PORT)
