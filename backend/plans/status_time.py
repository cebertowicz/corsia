"""Time facts for the compact status: parsing, ages, and the snapshot clock.

Three times stay three things: when a node was created, how long it has been
in its current state, and how long its Turns ran (``node_cost.own_many``).
An unreadable timestamp is None, never zero.
"""

from __future__ import annotations

from datetime import datetime, timezone

from plans.errors import PlanError


def parse(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def clock_at(facts: dict) -> datetime:
    """The snapshot clock as a datetime; it was issued by Plans, so it parses."""
    parsed = parse(facts["clock"])
    if parsed is None:
        raise PlanError(f"status clock is not a timestamp: {facts['clock']!r}")
    return parsed


def ages(node: dict, clock: str) -> dict[str, int | None]:
    now = parse(clock)
    created = parse(node.get("created_at"))
    changed = parse(node.get("last_transition_at"))
    return {
        "created_seconds": (
            max(0, int((now - created).total_seconds())) if now and created else None
        ),
        "in_state_seconds": (
            max(0, int((now - changed).total_seconds())) if now and changed else None
        ),
    }


# --- rows ----------------------------------------------------------------------
