"""Startup retirement of the taskless rows earlier completed tasks left behind.

A row registered while its thread had no task selected now leaves when that
thread completes a task it started afterwards, but only from the next
acceptance on. This pass applies the same rule to what has already happened:
for each thread, the start of its most recently started accepted task, through
the shared retirement writer, so every row is audited and comes back on pin or
the thread's next write.

It runs once per instance, and again on the start after a task settlement that
could not classify its rows (``startup_backfill.settlement_retirement``); a
repeat finds only what that settlement left.
"""

from __future__ import annotations

from infra.database import write_transaction
from plans.public import latest_accepted_starts
from registry import node_retirement, startup_backfill

_MARKER = startup_backfill.TASKLESS_MARKER
_TRIGGER = "taskless_backfill"


def retire_taskless_rows(db) -> dict[str, int]:
    """Retire each thread's taskless rows older than its latest accepted task's start."""
    retired: dict[str, int] = {}
    with write_transaction(db, scope="retire_taskless_rows") as connection:
        for thread_id, started_at in sorted(latest_accepted_starts(connection).items()):
            count = node_retirement.retire_taskless_rows(
                connection, thread_id=thread_id, started_at=started_at, trigger=_TRIGGER,
            )
            if count:
                retired[thread_id] = count
    return retired


def run_startup_backfill() -> None:
    """Run the pass unless its marker says it already ran."""
    startup_backfill.run_once(_MARKER, "taskless rows", retire_taskless_rows)
