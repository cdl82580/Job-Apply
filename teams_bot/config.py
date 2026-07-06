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
