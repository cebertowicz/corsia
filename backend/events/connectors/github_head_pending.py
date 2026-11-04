"""Heads waiting in the check aggregator's debounce window, kept across a backend restart.

One settings row maps ``repo@head_sha`` to the branch and PR the head was observed with.
A key is written when a head starts waiting and removed once it settles or is dropped; a
newer observation, a shutdown, or a settled head that could not be admitted keeps it.  A
key older than ``MAX_AGE_SECONDS`` is never re-armed and is pruned, so the row cannot grow
without bound.  A re-arm keeps the first observation's time.  The settled event keeps its
``_event_id``, so a head that already emitted dedupes at intake after a re-arm.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from infra.db_async import run_in_db

SETTING_KEY = "github_head_checks:pending"
# The daily review sweep's period: a backend down for hours (asleep, updating) still re-arms its
# heads, and re-arming an old head costs one read that intake dedupes.
MAX_AGE_SECONDS = 24 * 3600
_lock = asyncio.Lock()


def _entries() -> dict[str, Any]:
    from infra.settings import get_setting
    return get_setting(SETTING_KEY) or {}


def _fresh(entries: dict[str, Any], now: float) -> dict[str, Any]:
    return {k: v for k, v in entries.items() if isinstance(v, dict) and now - float(v.get("at", 0)) <= MAX_AGE_SECONDS}


def _write(repo: str, head_sha: str, entry: dict[str, Any] | None) -> None:
    from infra.settings import set_setting
    entries = _fresh(_entries(), time.time())
    if entry is None:
        entries.pop(f"{repo}@{head_sha}", None)
    else:
        entries[f"{repo}@{head_sha}"] = entry
    set_setting(SETTING_KEY, entries)


async def remember(repo: str, head_sha: str, branch: str | None, pr_number: int | None) -> None:
    async with _lock:
        await run_in_db(_write, repo, head_sha, {"repo": repo, "head_sha": head_sha, "branch": branch,
                                                  "pr_number": pr_number, "at": time.time()})


async def forget(repo: str, head_sha: str) -> None:
    async with _lock:
        await run_in_db(_write, repo, head_sha, None)


async def pending() -> list[dict[str, Any]]:
    """The heads still inside their window; stale keys are pruned from the row."""
    def read() -> list[dict[str, Any]]:
        from infra.settings import set_setting
        stored = _entries()
        entries = _fresh(stored, time.time())
        if entries != stored:
            set_setting(SETTING_KEY, entries)
        return list(entries.values())
    async with _lock:
        return await run_in_db(read)
