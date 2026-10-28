"""Claim the script runs ``run_script`` rules queued, for the script runner's sweep.

A queued run is checked when it would start, the time of its effect: a run whose
owner is paused, whose rule was turned off, or whose script schedule or owner is
gone is skipped with the reason, since it never started. None of these wakes
anyone; ``explain`` and ``list`` show them. A rule run takes its script's own
in-flight claim, the one a scheduled run takes, so at most one run of a script
is in flight; a run whose script is busy stays queued for a later sweep. The
queue itself is ``events.rule_runs``. The repository a run may read GitHub for is
taken from that stored input's event here, before the script sees any of it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from automation import script_schedules as store
from automation.script_github_channel import bound_repository
from automation.schedules import Schedule, get
from events import rule_runs
from infra.database import write_transaction
from infra.queries import get_thread, thread_is_available

#: The key a rule run holds in the runner's in-flight map, beside the schedule ids.
KEY_PREFIX = "rule:"


@dataclass(frozen=True)
class RuleRun:
    """One claimed rule run: its decision row, the event for stdin, the rule chain, and
    the repository its stored connector or platform event names, the only one its GitHub
    channel reads."""

    decision_id: int
    rule_id: str
    stdin: bytes
    trace: tuple[str, ...]
    github_repo: str | None = None

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
        return None, rule_runs.OWNER_GONE
    return schedule, None


def claim(now: str, capacity: int, live_keys: set[str], db) -> list[tuple[Schedule, RuleRun]]:
    """End interrupted rule runs, then claim up to ``capacity`` queued ones, oldest first."""
    from plans.public import thread_paused_since

    claimed: list[tuple[Schedule, RuleRun]] = []
    with write_transaction(db, scope="script_rule_runs_claim") as connection:
        rule_runs.interrupt(connection, live_decisions(live_keys))
        for row in rule_runs.queued(connection, capacity):
            schedule, refused = _refusal(connection, row)
            if refused is None and thread_paused_since(connection, row["thread_id"]) is not None:
                refused = rule_runs.OWNER_PAUSED
            if schedule is None or refused is not None:
                rule_runs.finish(connection, row["id"], skipped=refused)
                continue
            if not store.claim_for_rule(schedule.id, now, db=connection):
                continue  # an earlier row of this sweep took the script: this one waits its turn
            # Still queued under this write lock; were it not, the next sweep frees the claim.
            run_input = rule_runs.start(connection, row["id"])
            if run_input is not None:
                event = run_input.get("event", {})
                claimed.append((schedule, RuleRun(
                    row["id"], row["rule_id"],
                    json.dumps(event, ensure_ascii=False).encode(),
                    tuple(run_input.get("trace", ())),
                    bound_repository(event),
                )))
    return claimed
