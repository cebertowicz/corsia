"""Deterministic startup spread for the continuation runner."""

from __future__ import annotations

import hashlib
import os

RESTART_JITTER_SECONDS = int(os.getenv("CORSIA_RESTART_JITTER_SECONDS", "30"))


def restart_jitter_seconds(thread_id: str, *, maximum: int) -> int:
    """Spread restart wakes deterministically over a bounded window."""

    if maximum <= 0:
        return 0
    digest = hashlib.sha256(thread_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % (maximum + 1)
