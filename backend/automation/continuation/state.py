"""Timestamp and startup-spread helpers for the continuation runner."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from automation.continuation import restart_jitter


def parse_iso(value: str) -> datetime:
    """Parse one runner timestamp as an aware datetime."""

    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def with_startup_jitter(request, now: str, maximum: int):
    """Apply deterministic per-thread startup spread to an unbounded request."""

    if maximum <= 0 or request.not_before is not None:
        return request
    seconds = restart_jitter.restart_jitter_seconds(
        request.thread_id,
        maximum=maximum,
    )
    due = parse_iso(now) + timedelta(seconds=seconds)
    return replace(request, not_before=due.isoformat())
