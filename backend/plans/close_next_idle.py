"""Which open tasks an idle executor's ``next`` block lists, and whether to say pick one up (W9).

An executor is idle when nothing holds its slot and none of its open tasks is
ready: everything it owes is waiting, paused or blocked. Rather than list
those in plan order, the block lists waits with an end first, soonest first,
then waits with no end, then paused and blocked work, each in plan order. It
tells the agent to start a waiting task that can move now, except in a paused
plan or when a wait ends within ``IMMINENT_SECONDS``: that wait is about to
report, and the agent should let it.

An executor with something ready lists ready work first, then held work, each
in plan order (``ready_first``), so a ready task ranked below waits is never
cut from the block (corsia#403).

This module only orders and decides. When a wait ends comes from the
admission projection's watch causes (``ends_at``); ``plans.close_next`` turns
the order into rows and ``prompts.plan_tools`` owns every word.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

#: Held conditions: the task is open but cannot move until its cause clears.
HELD_CONDITIONS = frozenset({"paused", "blocked", "waiting"})

#: The open tasks an idle executor's block lists.
IDLE_TASK_LIMIT = 5

#: A wait ending this soon is about to report: the idle block still lists it,
#: but does not tell the agent to pick other work up.
IMMINENT_SECONDS = 15 * 60


@dataclass(frozen=True)
class IdleListing:
    """The tasks to list, in order, with their projections and seconds left."""

    shown: list
    projections: dict[str, dict]
    seconds: dict[str, float | None]
    total: int
    pick_up: bool


def idle_listing(
    plan, tasks: list, projections: dict[str, dict], now: datetime,
) -> IdleListing | None:
    """Return what an idle executor lists, or None when a task is ready or none is open.

    ``tasks`` is every open task of the executor in plan order, and
    ``projections`` their admission projections. A task a park just set aside
    is one of them, so it is listed rather than left out.
    """
    if not tasks:
        return None
    if any(projections[str(row["id"])]["condition"] not in HELD_CONDITIONS for row in tasks):
        return None
    seconds = {str(row["id"]): _seconds_left(projections[str(row["id"])], now) for row in tasks}
    waits = [
        seconds[str(row["id"])] for row in tasks
        if projections[str(row["id"])]["condition"] == "waiting"
    ]
    order = sorted(
        range(len(tasks)),
        key=lambda index: _key(
            projections[str(tasks[index]["id"])], seconds[str(tasks[index]["id"])], index,
        ),
    )
    imminent = any(left is not None and left <= IMMINENT_SECONDS for left in waits)
    return IdleListing(
        shown=[tasks[index] for index in order[:IDLE_TASK_LIMIT]],
        projections=projections,
        seconds=seconds,
        total=len(tasks),
        pick_up=plan["state"] == "active" and bool(waits) and not imminent,
    )


def ready_first(tasks: list, projections: dict[str, dict]) -> list:
    """Return ``tasks`` with ready ones first, then held ones, each kept in plan order."""
    def held(row) -> bool:
        return projections[str(row["id"])]["condition"] in HELD_CONDITIONS

    return sorted(tasks, key=held)


def release_wait(projection: dict, row) -> dict:
    """Return the optional row field that releases its primary self-scheduled wait."""
    causes = projection["causes"]
    if not causes or causes[0].get("scheduled_reason") is None:
        return {}
    return {
        "release_wait": {
            "operation": "resolve_watch",
            "params": {
                "node_id": str(row["id"]),
                "watch_id": causes[0]["watch_id"],
                "expected_revision": int(row["revision"]),
            },
        },
    }


def _key(projection: dict, seconds: float | None, index: int) -> tuple:
    """Order idle rows: timed waits soonest first, untimed waits, then other held work."""
    if projection["condition"] != "waiting":
        return (2, 0.0, index)
    if seconds is None:
        return (1, 0.0, index)
    return (0, seconds, index)


def _seconds_left(projection: dict, now: datetime) -> float | None:
    """Return the seconds until a task's earliest wait ends, or None when none ends by itself."""
    ends = [
        moment for cause in projection["causes"]
        if (moment := _aware(cause.get("ends_at"))) is not None
    ]
    return min(ends).timestamp() - now.timestamp() if ends else None


def _aware(value: str | None) -> datetime | None:
    """Parse a stored deadline; a legacy value without a zone has no knowable end."""
    try:
        moment = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    return moment if moment is not None and moment.tzinfo is not None else None
