"""
scripts/cache.py — Simple thread-safe TTL cache for admin hot paths.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
_store: dict[str, tuple[float, Any]] = {}  # key → (expires_at, value)

DEFAULT_TTL = 15  # seconds


def get(key: str) -> Any | None:
    with _lock:
        entry = _store.get(key)
        if entry and entry[0] > time.time():
            return entry[1]
        if entry:
            del _store[key]
    return None


def put(key: str, value: Any, ttl: int = DEFAULT_TTL) -> Any:
    with _lock:
        _store[key] = (time.time() + ttl, value)
    return value


def invalidate(prefix: str = "") -> None:
    with _lock:
        if not prefix:
            _store.clear()
        else:
            for k in [k for k in _store if k.startswith(prefix)]:
                del _store[k]
