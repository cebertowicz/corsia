"""Shared, status-free repository read accounting and gate."""
from __future__ import annotations

import asyncio
import functools
from datetime import datetime, timedelta
from typing import Any

from events.connectors import github_notifications_feed as feed

MAX_HOURLY_READS = 300


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def prune_reads(state: dict[str, Any], now: datetime) -> list[str]:
    floor = _stamp(now - timedelta(hours=1))
    reads = [stamp for stamp in state.get("reads", []) if isinstance(stamp, str) and stamp >= floor]
    state["reads"] = reads
    return reads


def counted(response: feed.GhResponse, state: dict[str, Any], now: datetime) -> None:
    reads = prune_reads(state, now)
    if response.status != 304:
        reads.append(_stamp(now))
    state["reads"] = reads


async def get(runner: feed.GhRunner, path: str, state: dict[str, Any], now: datetime, *,
              send_etag: bool = True, keep_etag: bool = True,
              propagate_refusal: bool = False) -> tuple[feed.GhResponse | None, str | None]:
    if len(prune_reads(state, now)) >= MAX_HOURLY_READS:
        return None, "budget_exhausted"
    etag = state.get("etags", {}).get(path) if send_etag else None
    try:
        response = await asyncio.to_thread(functools.partial(
            feed.gh_get, runner, path, {"If-None-Match": etag} if etag else None, low_priority=True))
    except feed.FeedError as exc:
        if propagate_refusal and exc.error_class == "read_refused":
            raise
        return None, exc.error_class
    if keep_etag and response.headers.get("etag"):
        state.setdefault("etags", {})[path] = response.headers["etag"]
    counted(response, state, now)
    return response, None
