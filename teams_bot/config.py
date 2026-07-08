"""Configuration from environment variables."""

import os


class Config:
    PORT = int(os.environ.get("PORT", "3978"))
    APP_ID = os.environ.get("MICROSOFT_APP_ID", "")
    APP_PASSWORD = os.environ.get("MICROSOFT_APP_PASSWORD", "")
    APP_TENANT_ID = os.environ.get("MICROSOFT_APP_TENANT_ID", "")
    BOT_API_KEY = os.environ.get("BOT_API_KEY", "")
    API_BASE = os.environ.get("JOB_APPLY_API_URL", "https://apply.cdlav.us").rstrip("/")
    # Publishable Logo.dev CDN key — same one frontend/*.html hardcode client-side
    # for rendered <img> logos, safe to embed since it's a pk_ (public) key.
    LOGODEV_PUBLIC_KEY = os.environ.get("LOGODEV_PUBLIC_KEY", "pk_U3oIYbhyTvinmftvOvCTJg")


# botbuilder's BotFrameworkAdapter treats an empty APP_ID/APP_PASSWORD pair as
# "auth disabled" (its local-emulator mode — see teams_bot/README.md's Local
# Development section). That's intentional off of Fly.io, but on Fly.io it
# would silently accept forged, unauthenticated Bot Framework activities.
# FLY_APP_NAME is only ever set by the Fly.io runtime, so it's a reliable
# "are we actually deployed" signal.
if os.environ.get("FLY_APP_NAME") and not (Config.APP_ID and Config.APP_PASSWORD):
    raise RuntimeError(
        "MICROSOFT_APP_ID / MICROSOFT_APP_PASSWORD must both be set when running "
        "on Fly.io — an empty pair puts the Bot Framework adapter in unauthenticated "
        "mode, which would accept forged Teams activities. Set them as Fly secrets."
    )
