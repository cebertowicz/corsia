"""When a self-restart request counts as in flight.

The one rule, with no imports beyond the standard library: the packaged app's
adoption preflight reads a source install's restart requests read-only from a bare
interpreter, where importing anything that resolves the workspace (``infra.config``)
would bind to the wrong state root. ``automation.self_restart`` re-exports these names.
"""

from __future__ import annotations

from datetime import datetime, timezone

IN_FLIGHT_TTL_SECONDS = 600
HOLD_PHASES = frozenset({
    "holding",
    "steering",
    "draining",
    "ready",
    "restarting",
    "resuming",
})
ACTIVE_PHASES = HOLD_PHASES | {"verifying"}


def parse(ts: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def row_is_active(row, *, now: datetime) -> bool:
    """Operational holds never expire; only legacy pre-bounce rows use the TTL."""
    if str(row["status"]) != "in_flight":
        return False
    phase = row["phase"] if "phase" in row.keys() else None
    if phase in ACTIVE_PHASES:
        return True
    if phase:
        return False
    started = parse(row["requested_at"])
    if started is None:
        return True
    return (now - started).total_seconds() < IN_FLIGHT_TTL_SECONDS
