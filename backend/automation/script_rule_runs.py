"""Claim the script runs ``run_script`` rules queued, for the script runner's sweep.

A queued run is checked when it would start, the time of its effect: a paused
owner's run is skipped, and a run whose rule was turned off, whose script
schedule is no longer active or whose owner is gone fails with the reason. None
of these wakes anyone; ``explain`` and ``list`` show them. The queue itself is
``events.rule_runs``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from automation.schedules import Schedule, get
from events import rule_runs
from infra.database import write_transaction
from infra.queries import get_thread, thread_is_available

#: The key a rule run holds in the runner's in-flight map, beside the schedule ids.
KEY_PREFIX = "rule:"


@dataclass(frozen=True)
class RuleRun:
    """One claimed rule run: its decision row, the event for stdin, and the rule chain."""

    decision_id: int
    rule_id: str
    stdin: bytes
    trace: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{KEY_PREFIX}{self.decision_id}"


def live_decisions(keys: set[str]) -> set[int]:
    """The decision ids of the rule runs this process holds."""
    return {int(key[len(KEY_PREFIX):]) for key in keys if key.startswith(KEY_PREFIX)}


def _refusal(connection, row) -> tuple[Schedule | None, str | None]:
    """The run's schedule, or why it can't start now."""
    if not row["enabled"]:
        return None, rule_runs.TURNED_OFF
    try:
        schedule = get(row["script_id"], db=connection)
    except KeyError:
        return None, "its script schedule no longer exists"
    if schedule.state != "active":
        return None, f"its script schedule is {schedule.state}"
    if not thread_is_available(get_thread(row["thread_id"], db=connection)):
        return None, "its owner is no longer available"
    return schedule, None


def claim(now: str, capacity: int, live_keys: set[str], db) -> list[tuple[Schedule, RuleRun]]:
    """End interrupted rule runs, then claim up to ``capacity`` queued ones, oldest first."""
    from plans.public import thread_paused_since

    claimed: list[tuple[Schedule, RuleRun]] = []
    with write_transaction(db, scope="script_rule_runs_claim") as connection:
        rule_runs.interrupt(connection, live_decisions(live_keys))
        if capacity <= 0:
            return claimed
        for row in rule_runs.queued(connection):
            if len(claimed) >= capacity:
                break
            schedule, failed = _refusal(connection, row)
            if failed is not None:
                rule_runs.finish(connection, row["id"], failed=failed)
                continue
            if thread_paused_since(connection, row["thread_id"]) is not None:
                rule_runs.finish(connection, row["id"], skipped=rule_runs.OWNER_PAUSED)
                continue
            run_input = json.loads(row["run_input"] or "{}")
            if rule_runs.start(connection, row["id"]):
                claimed.append((schedule, RuleRun(
                    row["id"], row["rule_id"],
                    json.dumps(run_input.get("event", {}), ensure_ascii=False).encode(),
                    tuple(run_input.get("trace", ())),
                )))
    return claimed
