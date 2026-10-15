"""The model-free phase that runs due script schedules.

It is a System phase of the continuation sweep, not a trigger source: a script
run never becomes a ``TriggerRequest`` and never reaches ``deliver_to_thread``,
because admission is shaped around a model Turn and a script run has none.

Each sweep this phase (1) marks any run the row says is in flight but this
process no longer holds as interrupted, and (2) claims due runs up to the
concurrency cap. A claim moves the row to its next slot before the run starts,
so a restart or a slow run never replays and a missed period never piles up.
The runs themselves are tasks, so a 5-minute script never holds the sweep.
"""

from __future__ import annotations

import asyncio
import uuid
from functools import partial
from pathlib import Path

from loguru import logger

from automation import script_schedules as store
from automation.schedules import Schedule, get
from automation.script_readings import (
    RunReport,
    Signal,
    build_report,
    emit_report,
    hash_mismatch_report,
)
from automation.script_sandbox import ScriptSandboxError, run_script
from host_access.processes import ProcessOwnerError
from infra.database import write_transaction
from infra.db_async import run_in_db_unless_owned
from infra.queries import get_thread, thread_is_available

#: Runs in flight at once. Set from the sandbox-startup probe on the owner's Mac.
SCRIPT_RUN_CAP = 2
WORKSPACE_SCOPE = "__workspace__"

_in_flight: dict[str, asyncio.Task] = {}


def _forget(schedule_id: str, _task: asyncio.Task) -> None:
    _in_flight.pop(schedule_id, None)


async def drive(now: str, *, db=None) -> int:
    """Recover interrupted runs and start due ones; return how many started."""
    for schedule_id, task in tuple(_in_flight.items()):
        if task.done():
            _in_flight.pop(schedule_id, None)
    capacity = SCRIPT_RUN_CAP - len(_in_flight)
    claims = await run_in_db_unless_owned(
        db, claim_due, now, capacity, set(_in_flight), db,
    )
    for schedule in claims:
        task = asyncio.create_task(run_claimed(schedule, now, db=db))
        _in_flight[schedule.id] = task
        task.add_done_callback(partial(_forget, schedule.id))
    return len(claims)


def claim_due(now: str, capacity: int, live_ids: set[str], db) -> list[Schedule]:
    """Claim up to ``capacity`` due runs whose owner can take them."""
    from plans.public import thread_paused_since

    claimed: list[Schedule] = []
    with write_transaction(db, scope="script_runner_claim") as connection:
        _recover_interrupted(connection, live_ids, now)
        if capacity <= 0:
            return claimed
        for schedule in store.due_scripts(now, db=connection):
            if len(claimed) >= capacity:
                break
            if not _owner_available(connection, schedule, now):
                continue
            if thread_paused_since(connection, schedule.thread_id) is not None:
                continue  # held: it runs on the first sweep after the pause lifts
            if store.claim(schedule, now, db=connection):
                claimed.append(schedule)
    return claimed


def _recover_interrupted(connection, live_ids: set[str], now: str) -> None:
    for schedule_id in store.in_flight_ids(db=connection):
        if schedule_id not in live_ids:
            store.finish_run(
                schedule_id, ran_at=now, status="interrupted",
                error="the backend restarted during this run; it is not replayed",
                db=connection,
            )


def _owner_available(connection, schedule: Schedule, now: str) -> bool:
    """True if the owner can take a run; else cancel the schedule and report once."""
    row = get_thread(schedule.thread_id, db=connection)
    if thread_is_available(row):
        return True
    store.cancel_for_missing_owner(schedule.id, now=now, db=connection)
    _report_owner_gone(connection, schedule, row)
    return False


def _report_owner_gone(connection, schedule: Schedule, row) -> None:
    """Tell the nearest live parent; with none, park it for the workspace health sink."""
    from events import dead_letter
    from events.delivery import enqueue_event_wake
    from events.health_sink import is_health_sink, resolve_live_health_owner

    chain = [row["parent_thread_id"], row["spawned_by"]] if row is not None else []
    sink = resolve_live_health_owner(chain, db=connection)
    summary = (
        f"Script schedule {schedule.id} was cancelled because its owner "
        f"{schedule.thread_id} is no longer available."
    )
    details = {"schedule_id": schedule.id, "owner": schedule.thread_id}
    if not is_health_sink(sink):
        try:
            enqueue_event_wake(
                sink, "script.owner_gone", summary, details,
                message_id=str(uuid.uuid5(
                    uuid.NAMESPACE_URL, f"corsia-script-owner-gone:{schedule.id}",
                )),
                db=connection,
            )
            return
        except ValueError:
            pass
    dead_letter.park(
        "script.owner_gone", {**details, "summary": summary},
        "owner_inactive", schedule.id, db=connection,
    )


def finished_at(_started_at: str) -> str:
    """The time a run is recorded as having finished: when it settled, not when it began."""
    return store.utc_now()


async def run_claimed(schedule: Schedule, started_at: str, *, db=None) -> None:
    """Run one claimed schedule to a settled report. Never raises into the loop.

    ``started_at`` is the sweep time that claimed the slot; the row records the
    finish time, so ``last_run_at`` and the hourly note gate are real clocks.
    """
    try:
        report = await _execute(schedule)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a run's failure is its report
        logger.exception("script schedule {} failed unexpectedly", schedule.id)
        report = _error_report(schedule, f"the runner failed: {exc}")
    try:
        woken = await run_in_db_unless_owned(
            db, settle, schedule, report, finished_at(started_at), db,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("script schedule {} could not record its run", schedule.id)
        await run_in_db_unless_owned(
            db, _record_settle_failure, schedule.id, str(exc), finished_at(started_at), db,
        )
        return
    if woken:
        await _drive_owner(schedule.thread_id)


def _record_settle_failure(schedule_id: str, cause: str, now: str, db) -> None:
    """Clear a failed settlement claim without misreporting a restart."""
    with write_transaction(db, scope="script_runner_settle_failure") as connection:
        store.finish_run(
            schedule_id,
            ran_at=now,
            status="error",
            error=f"could not record script run: {cause}",
            db=connection,
        )


def _error_report(schedule: Schedule, message: str) -> RunReport:
    name = (schedule.script_path or "script").rsplit("/", 1)[-1]
    return RunReport(
        "error", [],
        [Signal("error", None, f'Script "{name}": {message}', {"message": message})],
        error=message,
    )


async def _execute(schedule: Schedule) -> RunReport:
    path = Path(schedule.script_path or "")
    try:
        current = await asyncio.to_thread(store.file_sha256, path)
    except OSError:
        return hash_mismatch_report(schedule)
    if current != schedule.script_sha256:
        return hash_mismatch_report(schedule)
    project = None if schedule.scope_id == WORKSPACE_SCOPE else schedule.scope_id
    try:
        run = await run_script(
            schedule.thread_id, project, path,
            run_id=uuid.uuid4().hex, timeout_seconds=int(schedule.timeout_seconds or 0),
        )
    except (ScriptSandboxError, ProcessOwnerError) as exc:
        return _error_report(schedule, f"could not start: {exc}")
    return build_report(schedule, run)


def settle(schedule: Schedule, report: RunReport, now: str, db) -> bool:
    """Record one finished run on its row; True if it woke the owner."""
    with write_transaction(db, scope="script_runner_settle") as connection:
        try:
            current = get(schedule.id, db=connection)
        except KeyError:
            return False  # the owner was deleted while the run was in flight
        if current.state == "cancelled":
            store.finish_run(schedule.id, ran_at=now, status="cancelled", db=connection)
            return False
        if report.status == "hash_mismatch":
            store.stop_runs(
                schedule.id, status="hash_mismatch",
                error=report.error or "changed on disk", now=now, db=connection,
            )
        return emit_report(connection, current, report, now=now)


async def _drive_owner(thread_id: str) -> None:
    from infra.admission_fence import is_fenced
    from infra.thread_execution import drive_thread

    if not is_fenced(thread_id):
        await drive_thread(thread_id)
