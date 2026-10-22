"""The model-free phase that runs due script schedules.

It is a System phase of the continuation sweep, not a trigger source: a script
run never becomes a ``TriggerRequest`` and never reaches ``deliver_to_thread``,
because admission is shaped around a model Turn and a script run has none.

Each sweep this phase (1) marks any run the row says is in flight but this
process no longer holds as interrupted, and (2) claims due runs up to the
concurrency cap. A claim moves the row to its next slot before the run starts,
so a restart or a slow run never replays and a missed period never piles up.
The runs themselves are tasks, so a 5-minute script never holds the sweep.
Runs ``run_script`` event rules queued share the same cap, after due ones,
take the same per-script claim, so one run of a script is in flight at a time,
and settle the same way (``automation.script_rule_runs``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
import uuid
from functools import partial
from pathlib import Path

from loguru import logger

from automation import script_rule_runs
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
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db_unless_owned, run_off_db
from infra.measure_registry import REGISTRY
from infra.queries import get_thread, thread_is_available

#: Runs in flight at once. Set from the sandbox-startup probe on the owner's Mac.
SCRIPT_RUN_CAP = 2
WORKSPACE_SCOPE = "__workspace__"

_in_flight: dict[str, asyncio.Task] = {}
#: The script schedule each in-flight rule run holds the claim of, by its key.
_holds: dict[str, str] = {}

#: The runner builds a line's event id from the run, so the owner can't resend it: a new run sends every line again.
_RERUN_FIX = "Run the script again to send it; its other event lines are sent again too."


def _forget(key: str, _task: asyncio.Task) -> None:
    _in_flight.pop(key, None)
    _holds.pop(key, None)


def _start(key: str, run, holds: str | None = None) -> None:
    task = asyncio.create_task(run)
    _in_flight[key] = task
    if holds is not None:
        _holds[key] = holds
    task.add_done_callback(partial(_forget, key))


def _live() -> set[str]:
    """The keys of the runs this process holds, and every script schedule whose claim they hold."""
    return {*_in_flight, *(_holds[key] for key in _in_flight if key in _holds)}


async def drive(now: str, *, db=None) -> int:
    """Recover interrupted runs and start due ones, then queued rule runs; return how many started."""
    for key, task in tuple(_in_flight.items()):
        if task.done():
            _forget(key, task)
    capacity = SCRIPT_RUN_CAP - len(_in_flight)
    claims = await run_in_db_unless_owned(
        db, claim_due, now, capacity, _live(), db,
    )
    for schedule in claims:
        _start(schedule.id, run_claimed(schedule, now))
    rule_claims = await run_in_db_unless_owned(
        db, script_rule_runs.claim, now, SCRIPT_RUN_CAP - len(_in_flight), _live(), db,
    )
    for schedule, rule_run in rule_claims:
        _start(rule_run.key, run_claimed(schedule, now, rule_run=rule_run), holds=schedule.id)
    return len(claims) + len(rule_claims)


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


def finished_at() -> str:
    """The time a run is recorded as having finished: when it settled, not when it began."""
    return store.utc_now()


async def run_claimed(
    schedule: Schedule, started_at: str, *, db=None,
    rule_run: script_rule_runs.RuleRun | None = None,
) -> None:
    """Run one claimed schedule, or one rule run of it, to a settled report. Never raises into the loop.

    ``started_at`` is the sweep time that claimed the slot; the row records the
    finish time, so ``last_run_at`` and the hourly note gate are real clocks.
    """
    try:
        report = await _execute(schedule, rule_run)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a run's failure is its report
        logger.exception("script schedule {} failed unexpectedly", schedule.id)
        report = _error_report(schedule, f"the runner failed: {exc}")
    report = await _stamped(schedule, report)
    report = await _admit_events(schedule, report, rule_run.trace if rule_run else ())
    try:
        woken = await run_in_db_unless_owned(
            db, settle, schedule, report, finished_at(), db, rule_run=rule_run,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("script schedule {} could not record its run", schedule.id)
        await run_in_db_unless_owned(
            db, _record_settle_failure, schedule.id, str(exc), finished_at(), db,
            rule_run=rule_run,
        )
        return
    if woken:
        await _drive_owner(schedule.thread_id)


async def _admit_events(schedule: Schedule, report: RunReport, trace: tuple[str, ...] = ()) -> RunReport:
    """Route parsed event lines before synchronous settlement; one refusal wakes the owner once.

    ``trace`` is the rule chain of a rule-started run; each event it sends carries it.
    """
    if not report.events:
        return report
    from automation.script_measures import run_stamps
    from events.intake import admit_event, failure_code, format_event_error

    stamps = run_stamps(schedule, report)
    producer = f"script:{schedule.id}"
    refusals: dict[str, str] = {}  # each distinct code, with the first text it came with
    for index, event in enumerate(report.events):
        try:
            result = await admit_event(
                event_type=event["type"], facts=event["facts"], origin="emitter",
                producer=producer, event_id=f"{producer}:{report.run_id}:{index}", stamps=stamps,
                **({"trace": trace} if trace else {}),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - run_claimed never raises; the run settles as an error
            logger.exception("script schedule {} event line {} failed inside the intake", schedule.id, index)
            code = failure_code(exc)
            refusals.setdefault(code, (
                "the event intake failed unexpectedly" if code == "internal_error"
                else "event intake is unavailable"))
            continue
        if result.refusal is not None:
            refusals.setdefault(result.refusal.code, result.refusal.text)
    if not refusals:
        return report
    shown = [
        format_event_error(code, refusals[code], _RERUN_FIX if code == "intake_unreachable" else None)
        for code in sorted(refusals)[:2]
    ]
    more = f" and {len(refusals) - 2} more" if len(refusals) > 2 else ""
    message = f"event line refused: {' '.join(shown)}{more}"
    return dataclasses.replace(
        report, status="error", error=message,
        signals=[*report.signals, Signal(
            "error", None, f'Script "{(schedule.script_path or "script").rsplit("/", 1)[-1]}": {message}',
            {"message": message},
        )],
    )


async def _stamped(schedule: Schedule, report: RunReport) -> RunReport:
    """Resolve the workflow stamp before settling, off the database pool (it reads Git).

    The owner rule is read live; a failure to resolve records the rows unversioned.
    """
    workflow = (schedule.payload or {}).get("workflow")
    if not workflow or not (report.measures or report.events):
        return report

    def resolve():
        from tools.workflow_owners import workflow_stamp

        db = get_db()
        try:
            return workflow_stamp(schedule.thread_id, str(workflow), db)
        finally:
            db.close()

    try:
        stamp = await run_off_db(resolve)
    except (OSError, RuntimeError, ValueError, sqlite3.Error):  # unresolved: unversioned, never a failed run
        logger.exception("script schedule {} could not resolve workflow {}", schedule.id, workflow)
        stamp = None
    return dataclasses.replace(report, workflow_stamp=stamp)


def _record_settle_failure(
    schedule_id: str, cause: str, now: str, db, *, rule_run: script_rule_runs.RuleRun | None = None,
) -> None:
    """Clear a failed settlement claim without misreporting a restart."""
    from events import rule_runs

    with write_transaction(db, scope="script_runner_settle_failure") as connection:
        store.finish_run(
            schedule_id,
            ran_at=now,
            status="error",
            error=f"could not record script run: {cause}",
            db=connection,
        )
        if rule_run is not None:
            rule_runs.finish(connection, rule_run.decision_id, failed=f"could not record the run: {cause}")


def _error_report(schedule: Schedule, message: str) -> RunReport:
    name = (schedule.script_path or "script").rsplit("/", 1)[-1]
    return RunReport(
        "error", [],
        [Signal("error", None, f'Script "{name}": {message}', {"message": message})],
        error=message,
    )


async def _execute(schedule: Schedule, rule_run: script_rule_runs.RuleRun | None = None) -> RunReport:
    path = Path(schedule.script_path or "")
    try:
        current = await asyncio.to_thread(store.file_sha256, path)
    except OSError:
        return hash_mismatch_report(schedule)
    if current != schedule.script_sha256:
        return hash_mismatch_report(schedule)
    project = None if schedule.scope_id == WORKSPACE_SCOPE else schedule.scope_id
    run_id = uuid.uuid4().hex
    try:
        run = await run_script(
            schedule.thread_id, project, path,
            schedule_id=schedule.id, run_id=run_id, timeout_seconds=int(schedule.timeout_seconds or 0),
            **({"stdin": rule_run.stdin, "trace": rule_run.trace} if rule_run is not None else {}),
        )
    except (ScriptSandboxError, ProcessOwnerError) as exc:
        return _error_report(schedule, f"could not start: {exc}")
    return build_report(schedule, run, run_id=run_id)


def settle(
    schedule: Schedule, report: RunReport, now: str, db, *,
    rule_run: script_rule_runs.RuleRun | None = None,
) -> bool:
    """Record one finished run on its row, and a rule run on its decision; True if it woke the owner."""
    from events import rule_runs

    REGISTRY.ensure_loaded(db if db is not None else get_db())  # the first load reads outside the transaction
    with write_transaction(db, scope="script_runner_settle") as connection:
        if rule_run is not None:
            # A breach is a reading the script ran and reported, not a failed run.
            failed = None if report.status in ("ok", "breach") else (report.error or report.status)
            rule_runs.finish(connection, rule_run.decision_id, failed=failed)
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
        return emit_report(connection, current, report, now=now, rule_run=rule_run is not None)


async def _drive_owner(thread_id: str) -> None:
    from infra.admission_fence import is_fenced
    from infra.thread_execution import drive_thread

    if not is_fenced(thread_id):
        await drive_thread(thread_id)
