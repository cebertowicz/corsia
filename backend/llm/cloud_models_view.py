"""A cloud provider's model list for Settings: the last discovery, refreshed behind the read.

Opening a provider card used to run a full discovery (the vendor's own model API, a database
write and a catalog-changed broadcast to every client) on each read. The read now answers from
the last discovery; a stale one is refreshed in the background, and clients are told only when
the refresh found a different list.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from infra.db_async import run_in_db
from infra.state.ws import notify_models_changed

log = logging.getLogger(__name__)

REFRESH_AFTER_SECONDS = 600

# provider -> (fingerprint of the key it was discovered with, monotonic time, models)
_last: dict[str, tuple[str, float, list[dict]]] = {}
_refreshing: dict[str, asyncio.Task] = {}


def _fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def remember(provider: str, api_key: str, models: list[dict]) -> None:
    """Record a successful discovery as what the next read answers with."""
    _last[provider] = (_fingerprint(api_key), time.monotonic(), models)


def _same_list(before: list[dict], after: list[dict]) -> bool:
    """Whether two discoveries list the same models, leaving out the user's own on/off choice."""
    def rows(models: list[dict]) -> list[dict]:
        return sorted(({k: v for k, v in m.items() if k != "enabled"} for m in models), key=lambda m: m["id"])

    return rows(before) == rows(after)


def _enabled_by_id(provider: str) -> dict[str, int]:
    from infra.database import get_db

    db = get_db()
    try:
        rows = db.execute("SELECT id, enabled FROM models WHERE provider = ?", (provider,)).fetchall()
    finally:
        db.close()
    return {row["id"]: row["enabled"] for row in rows}


async def _refresh(provider: str, api_key: str, before: list[dict]) -> None:
    from llm.catalog import discover_cloud_models

    try:
        result = await discover_cloud_models(provider, api_key, notify=False)
    except Exception as exc:
        log.warning("cloud model refresh failed for %s: %s", provider, type(exc).__name__)
        result = {"ok": False}
    if not result.get("ok"):
        # Keep answering with the last list; the next try waits a full interval, not the next read.
        held = _last.get(provider)
        if held is not None:
            _last[provider] = (held[0], time.monotonic(), held[2])
        return
    if not _same_list(before, result["models"]):
        notify_models_changed()


def _refresh_behind(provider: str, api_key: str, before: list[dict]) -> None:
    running = _refreshing.get(provider)
    if running is not None and not running.done():
        return
    task = asyncio.create_task(_refresh(provider, api_key, before))
    _refreshing[provider] = task
    task.add_done_callback(lambda done: _refreshing.pop(provider, None) if _refreshing.get(provider) is done else None)


async def cloud_models_for_settings(provider: str, api_key: str) -> dict:
    """The models a provider card lists: the last discovery with the user's current on/off choices."""
    from llm.catalog import discover_cloud_models

    held = _last.get(provider)
    if held is None or held[0] != _fingerprint(api_key):
        return await discover_cloud_models(provider, api_key)
    _, discovered_at, models = held
    if time.monotonic() - discovered_at > REFRESH_AFTER_SECONDS:
        _refresh_behind(provider, api_key, models)
    enabled = await run_in_db(_enabled_by_id, provider)
    current = [{**m, "enabled": enabled.get(m["db_id"], m["enabled"])} for m in models]
    current.sort(key=lambda m: (-m.get("enabled", 0), m["display_name"]))
    return {"ok": True, "models": current}
