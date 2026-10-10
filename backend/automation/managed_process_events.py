"""Persist managed-process truth and deliver the four approved wake outcomes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Any, Sequence
import uuid

from loguru import logger

from infra.database import write_transaction
from infra.managed_process_store import (
    delete_retirement,
    list_retirements,
    record_event_message,
    stage_thread_family_retirements,
    unsettled_family_handles,
    unsettled_handles,
    upsert_transition,
)

if TYPE_CHECKING:
    from host_access.managed_lifecycle import (
        LongRunningReservation,
        ThreadCloseToken,
        Transition,
    )
    from host_access.processes import ProcessRecord

_TERMINAL_EVENTS = {"completed", "failed", "interrupted_by_restart"}
_NEXT = "Read the result with managed_process_logs(handle). It is not running any more."


def epoch_iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    moment = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _seconds_between(start: str | None, end: str | None) -> float:
    if not start or not end:
        return 0.0
    return max(
        0.0,
        (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(),
    )


def duration_text(seconds: float) -> str:
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def row_for(record: ProcessRecord) -> dict[str, Any]:
    from host_access.managed_lifecycle import outcome_for

    outcome = outcome_for(record)
    return {
        "handle": record.handle,
        "thread_id": record.thread_id,
        "turn_id": record.turn_id,
        "lease_id": record.lease_id,
        "description": record.description,
        "long_running": int(record.long_running),
        "pid": record.pid,
        "pgid": record.pgid,
        "creation_fingerprint": record.creation_fingerprint,
        "log_path": str(record.log_path),
        "state": (
            outcome
            if outcome == "interrupted_by_restart"
            else record.terminal_state
        ),
        "outcome": outcome,
        "reason": record.reason,
        "exit_code": record.exit_code,
        "started_at": epoch_iso(record.started_at),
        "last_output_at": epoch_iso(record.last_output_at),
        "stalled_since": epoch_iso(record.stalled_since),
        "stall_after_seconds": record.stall_after_seconds,
        "max_lifetime_seconds": record.max_lifetime_seconds,
        "ended_at": epoch_iso(record.ended_at),
        "log_truncated": int(record.log_truncated),
    }


def _terminal_summary(row: dict[str, Any]) -> str:
    name = f'Your background process "{row["description"]}"'
    ran = duration_text(_seconds_between(row["started_at"], row["ended_at"]))
    if row["outcome"] == "interrupted_by_restart":
        if row["reason"] == "completion_unknown_after_restart":
            return (
                f"{name} was already gone when CORSIA recovered after a restart. "
                "Its exit result and exact end time are unknown. Its log is kept; "
                "review it before deciding whether to relaunch the process."
            )
        return (
            f"{name} was stopped because CORSIA restarted, after {ran}. Its log "
            "is kept; it was not relaunched. Relaunch it if it is still needed "
            "and resumable."
        )
    if row["reason"] == "max_lifetime":
        return f"{name} was stopped at the 24-hour limit after {ran}."
    code = row["exit_code"] if row["exit_code"] is not None else "unknown"
    return f"{name} exited with code {code} after {ran}."


def _message_id(handle: str, key: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"corsia-managed-process:{handle}:{key}",
        )
    )


def event_for(row: dict[str, Any], kind: Transition) -> dict[str, Any] | None:
    """Return only stalled/completed/failed/interrupted wake outcomes."""
    if not row["long_running"]:
        return None
    handle = row["handle"]
    if kind == "stalled":
        quiet = _seconds_between(
            row["last_output_at"] or row["started_at"],
            row["stalled_since"],
        )
        return {
            "internal_event": "managed_process.stalled",
            "summary": (
                f'Your background process "{row["description"]}" has printed '
                f"nothing for {duration_text(quiet)}. It is still running and was "
                "not stopped. Check managed_process_logs, or stop it with "
                "managed_process_stop."
            ),
            "details": {
                "handle": handle,
                "description": row["description"],
                "state": "running",
                "started_at": row["started_at"],
                "last_output_at": row["last_output_at"],
                "stalled_since": row["stalled_since"],
                "log_truncated": bool(row["log_truncated"]),
            },
            "chip": None,
            "message_id": _message_id(handle, f"stalled:{row['stalled_since']}"),
        }
    if kind != "terminal" or row["outcome"] not in _TERMINAL_EVENTS:
        return None
    return {
        "internal_event": f"managed_process.{row['outcome']}",
        "summary": _terminal_summary(row),
        "details": {
            "handle": handle,
            "description": row["description"],
            "outcome": row["outcome"],
            "exit_code": row["exit_code"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "last_output_at": row["last_output_at"],
            "log_truncated": bool(row["log_truncated"]),
            "next": _NEXT,
        },
        "chip": f"Background process finished: {row['description']}",
        "message_id": _message_id(handle, str(row["outcome"])),
        "terminal": True,
    }


def apply_transition(
    row: dict[str, Any],
    kind: Transition,
    *,
    db: sqlite3.Connection | None = None,
) -> str | None:
    """Write row and event together unless a retirement tombstone won."""
    from events.delivery import enqueue_event_wake

    event = event_for(row, kind)
    with write_transaction(db, scope="managed_process_transition") as connection:
        if not upsert_transition(row, db=connection):
            return None
        if event is None:
            return None
        try:
            enqueue_event_wake(
                row["thread_id"],
                event["internal_event"],
                event["summary"],
                event["details"],
                message_id=event["message_id"],
                chip=event["chip"],
                admit_while_fenced=True,
                db=connection,
            )
        except ValueError:
            logger.warning(
                "managed process {} owner {} unavailable; {} not delivered",
                row["handle"],
                row["thread_id"],
                event["internal_event"],
            )
            return None
        if event.get("terminal"):
            record_event_message(row["handle"], event["message_id"], db=connection)
    return str(row["thread_id"])


async def durable_long_running_reservations(
) -> tuple[LongRunningReservation, ...]:
    """Read restart-surviving rows that still consume long-running capacity."""
    from host_access.managed_lifecycle import LongRunningReservation
    from infra.db_async import run_in_db

    rows = await run_in_db(unsettled_handles)
    return tuple(
        LongRunningReservation(
            handle=str(row["handle"]),
            thread_id=str(row["thread_id"]),
            description=str(row["description"] or row["handle"]),
            state=str(row["state"]),
        )
        for row in rows
        if bool(row["long_running"])
    )


async def on_background_transition(record: ProcessRecord, kind: Transition) -> None:
    from infra.db_async import run_in_db

    from infra.admission_fence import is_fenced

    row = row_for(record)
    thread_id = await run_in_db(apply_transition, row, kind)
    if (
        thread_id is not None
        and record.reason != "backend_shutdown"
        and not is_fenced(thread_id)
    ):
        from infra.thread_execution import drive_thread

        await drive_thread(thread_id)


def install() -> None:
    from host_access import processes

    processes.PROCESS_OWNER.configure_transition_callback(on_background_transition)
    processes.PROCESS_OWNER.configure_long_running_capacity_callback(
        durable_long_running_reservations
    )


async def prepare_thread_close(thread_ids: Sequence[str]) -> ThreadCloseToken:
    """Close live admission and refuse unverified durable family ownership."""
    from host_access import processes
    from host_access.managed_lifecycle import ThreadCloseToken
    from infra.db_async import run_in_db

    exact_ids = tuple(dict.fromkeys(str(item) for item in thread_ids))
    try:
        token = await processes.PROCESS_OWNER.prepare_thread_close(exact_ids)
    except BaseException:
        await processes.PROCESS_OWNER.abort_thread_close(
            ThreadCloseToken(thread_ids=exact_ids, handles=())
        )
        raise
    unsettled = await run_in_db(unsettled_family_handles, tuple(thread_ids))
    if unsettled:
        await processes.PROCESS_OWNER.abort_thread_close(token)
        handles = ", ".join(str(row["handle"]) for row in unsettled)
        raise processes.ProcessCleanupError(
            f"managed process death is unverified for: {handles}"
        )
    return token


async def abort_thread_close(token: ThreadCloseToken) -> None:
    from host_access import processes

    await processes.PROCESS_OWNER.abort_thread_close(token)


def stage_thread_close(
    db: sqlite3.Connection, thread_ids: Sequence[str],
) -> tuple[str, ...]:
    from host_access import processes

    return stage_thread_family_retirements(
        db,
        thread_ids,
        process_dir=processes.PROCESS_OWNER.process_dir,
    )


async def finish_thread_close(token: ThreadCloseToken) -> None:
    from host_access import processes
    from infra.db_async import run_in_db

    await processes.PROCESS_OWNER.commit_thread_close(token)
    try:
        await run_in_db(drain_retirements)
    except Exception:
        logger.exception("managed process retirement immediate drain failed")


def drain_retirements() -> tuple[str, ...]:
    """Idempotently unlink staged logs, retiring rows only after both are absent."""
    from host_access import processes

    process_dir = processes.PROCESS_OWNER.process_dir.resolve(strict=False)
    completed: list[str] = []
    for row in list_retirements():
        handle = str(row["handle"])
        expected_head = f"{handle}.log"
        expected_tail = f"{handle}.tail.log"
        head_name = str(row["head_log_name"])
        tail_name = row["tail_log_name"]
        if (
            Path(head_name).name != head_name
            or head_name != expected_head
            or (
                tail_name is not None
                and (
                    Path(str(tail_name)).name != str(tail_name)
                    or str(tail_name) != expected_tail
                )
            )
        ):
            logger.error("managed process {} retirement path is invalid", handle)
            continue
        try:
            (process_dir / head_name).unlink(missing_ok=True)
            if tail_name is not None:
                (process_dir / str(tail_name)).unlink(missing_ok=True)
        except OSError:
            logger.exception("managed process {} retirement unlink failed", handle)
            continue
        delete_retirement(handle)
        completed.append(handle)
    return tuple(completed)
