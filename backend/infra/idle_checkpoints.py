"""Atomic integration between producer-owned idle markers and checkpoints."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger

from core.checkpoint_types import (
    CheckpointRequest,
    IdleCheckpointAdmissionResult,
    IdleCheckpointDisposition,
    IdleCheckpointReason,
)
from infra import checkpoints as checkpoint_store
from infra.checkpoint_admission import IDLE_WARM_DELAY_SECONDS
from infra.database import get_db, write_transaction

if TYPE_CHECKING:
    from core.checkpoint_native_admission import NativeIdleReceipt

IDLE_RECOVER_LIMIT = 20

_CONFLICT_REASON: dict[checkpoint_store.CheckpointConflictKind, IdleCheckpointReason] = {
    checkpoint_store.CheckpointConflictKind.STALE_EPOCH: IdleCheckpointReason.STALE,
    checkpoint_store.CheckpointConflictKind.STALE_PREDECESSOR: IdleCheckpointReason.STALE,
    checkpoint_store.CheckpointConflictKind.ACTIVE_CHECKPOINT: (
        IdleCheckpointReason.CONCURRENT_CHECKPOINT
    ),
    checkpoint_store.CheckpointConflictKind.BOUNDARY_NOT_DURABLE: (
        IdleCheckpointReason.BOUNDARY_NOT_DURABLE
    ),
    checkpoint_store.CheckpointConflictKind.BOUNDARY_NOT_ADVANCING: (
        IdleCheckpointReason.BOUNDARY_NOT_ADVANCING
    ),
}


@dataclass(frozen=True, slots=True)
class IdleCheckpointDue:
    """One completed Turn whose producer left an idle checkpoint marker."""

    turn_id: str
    turn_order: int
    thread_id: str
    ended_at: str
    #: A native Turn's final call receipt, held only by the in-process timer.
    #: Boot recovery rebuilds markers without it, so their native fold skips.
    native_receipt: NativeIdleReceipt | None = None

    def fires_at(self) -> datetime:
        """Naive-UTC instant the warm fold must start to beat cache expiry."""
        ended = datetime.fromisoformat(self.ended_at)
        if ended.tzinfo is not None:
            ended = ended.astimezone(UTC).replace(tzinfo=None)
        return ended + timedelta(seconds=IDLE_WARM_DELAY_SECONDS)


def _fold_model_exclusion_reason(
    connection: sqlite3.Connection, thread_id: str, model: str,
) -> IdleCheckpointReason | None:
    """None when eligible; else the sole model-level exclusion for the fold.

    Harness identity excludes no one. The fold-level exclusions are a
    transport that dispatches to the managed oMLX engine (a local fold would
    compete with foreground local turns and no admission path defers it) and a
    cloud model in a project with cloud models off.
    """
    from llm.local_backends import find_backend
    from llm.locality import thread_locality_refusal
    from llm.resolution import ModelUnavailableError, resolve_execution_target

    try:
        target = resolve_execution_target(model, db=connection)
    except (ModelUnavailableError, ValueError) as error:
        # An unresolvable source can never be folded warm; say so instead of
        # failing the Turn terminal that is being committed around this read.
        logger.warning("Idle checkpoint skipped for unresolved {}: {}", model, error)
        return IdleCheckpointReason.UNRESOLVABLE
    if thread_locality_refusal(model, thread_id=thread_id, db=connection):
        return IdleCheckpointReason.PROJECT_CLOUD_OFF
    if target.harness == "native":
        backend = find_backend(target.transport_model)
        if backend is not None and backend.name == "omlx":
            return IdleCheckpointReason.LOCAL_SHARED_MODEL
    return None


def idle_checkpoint_exclusion_reason(
    connection: sqlite3.Connection, thread_id: str, model: str,
) -> IdleCheckpointReason | None:
    """None when eligible; else the sole reason a Turn's marker is not written."""
    thread = connection.execute(
        "SELECT spawned_by,parent_thread_id,lifecycle_state FROM threads WHERE id=?",
        (thread_id,),
    ).fetchone()
    if thread is None:
        return IdleCheckpointReason.MISSING_THREAD
    if thread["spawned_by"] is not None or thread["parent_thread_id"] is not None:
        return IdleCheckpointReason.CHILD
    if (thread["lifecycle_state"] or "active") != "active":
        return IdleCheckpointReason.ARCHIVED
    return _fold_model_exclusion_reason(connection, thread_id, model)


def idle_checkpoint_eligible(
    connection: sqlite3.Connection, thread_id: str, model: str,
) -> bool:
    """The sole idle-fold exclusion rule: child, archived and local-model threads."""
    return idle_checkpoint_exclusion_reason(connection, thread_id, model) is None


def cancel_timer_and_record(
    thread_id: str, *, cleared: bool, reason: IdleCheckpointReason,
) -> None:
    """Drop the thread's timer and count a marker the writer just cleared.

    The durable write belongs to the Turn/input transition layer; this owns
    only the in-memory timer and the reason. A cleared marker is always
    counted, so an armed marker ends as fired, refused, expired or
    invalidated and never simply disappears.
    """
    import infra.idle_checkpoint_timers as idle_checkpoint_timers
    from infra.product_events import emit_product_event

    idle_checkpoint_timers.cancel(thread_id)
    if cleared:
        logger.info(
            "idle_checkpoint invalidated: thread={} reason={}",
            thread_id, reason.value,
        )
        emit_product_event(
            "idle_checkpoint", "invalidated", thread_id=thread_id,
            properties={"reason": reason.value},
        )


def idle_checkpoint_due(
    connection: sqlite3.Connection, turn_id: str,
) -> IdleCheckpointDue | None:
    """Read whether one completed Turn is eligible for an idle marker.

    Read-only: the caller's transition-owner transaction writes the marker
    itself, per the Turn/input one-owner contract. An excluded Turn is
    counted here, at the producer, since nothing downstream will ever see it.
    """
    from infra.product_events import emit_product_event

    row = connection.execute(
        "SELECT id,turn_order,thread_id,model,ended_at FROM active_turns "
        "WHERE id=? AND status='completed'",
        (turn_id,),
    ).fetchone()
    if row is None:
        return None
    thread_id, model = str(row["thread_id"]), str(row["model"])
    reason = idle_checkpoint_exclusion_reason(connection, thread_id, model)
    if reason is not None:
        logger.info(
            "idle_checkpoint excluded: thread={} turn={} model={} reason={}",
            thread_id, turn_id, model, reason.value,
        )
        emit_product_event(
            "idle_checkpoint", "excluded", thread_id=thread_id,
            properties={"reason": reason.value},
        )
        return None
    return IdleCheckpointDue(
        str(row["id"]), int(row["turn_order"]), thread_id, str(row["ended_at"]),
    )


def idle_marker_due(
    connection: sqlite3.Connection,
    turn_id: str,
    *,
    outcome: str,
    successor_claimed: bool,
    switch_pending: bool,
    mark_idle_checkpoint: bool,
    successor_held: bool = False,
) -> IdleCheckpointDue | None:
    """Return the due marker only when nothing else made this Turn's successor runnable.

    A completed Turn that is suppressed here would otherwise leave no trace at
    all, so each suppression names its own cause in the server log; a Turn that
    did not complete is the ordinary case and is not logged.
    """
    if outcome != "completed":
        return None
    suppression = None
    if successor_claimed:
        suppression = "successor_claimed"
    elif successor_held:
        suppression = "successor_held"
    elif switch_pending:
        suppression = "switch_pending"
    elif not mark_idle_checkpoint:
        suppression = "settlement_not_driven"
    if suppression is not None:
        thread_row = connection.execute(
            "SELECT thread_id FROM active_turns WHERE id=?", (turn_id,)
        ).fetchone()
        logger.info(
            "idle_checkpoint not_marked: thread={} turn={} reason={}",
            thread_row["thread_id"] if thread_row is not None else None,
            turn_id, suppression,
        )
        return None
    return idle_checkpoint_due(connection, turn_id)


def recover_idle_markers(
    *, now: datetime, db: sqlite3.Connection | None = None,
) -> tuple[IdleCheckpointDue, ...]:
    """At boot, keep the oldest still-warm markers and clear the rest unfolded.

    A marker whose warm window has already passed needs no model resolution
    to know it can only be cleared, so that half of the backlog is cleared
    in one unbounded bulk statement -- boot work here does not grow with a
    stale backlog's size. Only markers that could still be warm are read
    individually and resolved, bounded to IDLE_RECOVER_LIMIT rows oldest
    first, so a fresh marker is never starved behind an arbitrarily large
    pile of already-expired ones; the bound protects the resolution cost,
    not the cheap expiry check. Anything beyond that bound stays pending for
    a later boot's pass. Candidate rows are read, and each row's model
    resolved, before any write transaction opens, so this bounded page of
    per-row catalog/connection lookups never holds the database write lock.
    """
    from infra.product_events import emit_product_event
    from infra.thread_driver import consume_idle_checkpoint_marker

    connection = db if db is not None else get_db()
    warm_cutoff = (now - timedelta(seconds=IDLE_WARM_DELAY_SECONDS)).isoformat()
    stale_rows = connection.execute(
        "SELECT id,turn_order,thread_id,ended_at FROM active_turns "
        "INDEXED BY idx_active_turns_idle_checkpoint_due "
        "WHERE idle_checkpoint_pending=1 AND ended_at<=?",
        (warm_cutoff,),
    ).fetchall()
    candidate_rows = connection.execute(
        "SELECT id,turn_order,thread_id,model,ended_at FROM active_turns "
        "INDEXED BY idx_active_turns_idle_checkpoint_due "
        "WHERE idle_checkpoint_pending=1 AND ended_at>? "
        "ORDER BY ended_at,turn_order LIMIT ?",
        (warm_cutoff, IDLE_RECOVER_LIMIT),
    ).fetchall()

    def _due(row) -> IdleCheckpointDue:
        return IdleCheckpointDue(
            str(row["id"]), int(row["turn_order"]), str(row["thread_id"]),
            str(row["ended_at"]),
        )

    live: list[IdleCheckpointDue] = []
    expired: list[tuple[IdleCheckpointDue, IdleCheckpointReason]] = [
        (_due(row), IdleCheckpointReason.WARM_WINDOW_PASSED) for row in stale_rows
    ]
    for row in candidate_rows:
        due = _due(row)
        if idle_checkpoint_eligible(connection, due.thread_id, str(row["model"])):
            live.append(due)
        else:
            expired.append((due, IdleCheckpointReason.INELIGIBLE))

    logger.info(
        "idle_checkpoint boot_recovery: live={} expired={}",
        len(live), len(expired),
    )
    if expired:
        with write_transaction(db, scope="idle_checkpoint_recover") as writer:
            for due, reason in expired:
                consume_idle_checkpoint_marker(writer, due.turn_id)
                logger.info(
                    "idle_checkpoint expired: thread={} turn={} reason={}",
                    due.thread_id, due.turn_id, reason.value,
                )
                emit_product_event(
                    "idle_checkpoint", "expired", thread_id=due.thread_id,
                    properties={"reason": reason.value},
                )
    return tuple(live)


def _consume_marker(
    connection: sqlite3.Connection,
    turn_id: str,
    request: CheckpointRequest | None,
) -> IdleCheckpointAdmissionResult:
    from infra.thread_driver import consume_idle_checkpoint_marker

    marker = connection.execute(
        "SELECT thread_id FROM active_turns WHERE id=? AND status='completed' "
        "AND idle_checkpoint_pending=1", (turn_id,),
    ).fetchone()
    if marker is None:
        return IdleCheckpointAdmissionResult(
            IdleCheckpointDisposition.STALE, reason=IdleCheckpointReason.STALE,
        )
    thread_id = str(marker["thread_id"])
    # Idle folds are always warm: a fold that cannot fork warm skips instead.
    valid_request = request is None or (
        request.thread_id == thread_id and request.trigger == "idle"
        and request.mode == "warm"
        and request.originating_turn_id == turn_id
    )
    if not valid_request:
        raise ValueError("idle checkpoint request does not match its Turn marker")
    if not consume_idle_checkpoint_marker(connection, turn_id):
        return IdleCheckpointAdmissionResult(
            IdleCheckpointDisposition.STALE, reason=IdleCheckpointReason.STALE,
        )
    if request is None:
        return IdleCheckpointAdmissionResult(
            IdleCheckpointDisposition.REFUSED, reason=IdleCheckpointReason.NOT_REQUESTED,
        )
    turn_running, already_covered = connection.execute(
        "SELECT EXISTS(SELECT 1 FROM active_turns WHERE thread_id=? "
        "AND status='running'), EXISTS("
        "SELECT 1 FROM checkpoints WHERE thread_id=? AND history_epoch=? "
        "AND through_message_id=? AND trigger='idle')",
        (thread_id, request.thread_id, request.history_epoch, request.through_message_id),
    ).fetchone()
    if turn_running:
        return IdleCheckpointAdmissionResult(
            IdleCheckpointDisposition.REFUSED, reason=IdleCheckpointReason.TURN_RUNNING,
        )
    if already_covered:
        return IdleCheckpointAdmissionResult(
            IdleCheckpointDisposition.REFUSED, reason=IdleCheckpointReason.ALREADY_COVERED,
        )
    try:
        record = checkpoint_store.create_running(request, db=connection)
    except checkpoint_store.CheckpointConflict as conflict:
        return IdleCheckpointAdmissionResult(
            IdleCheckpointDisposition.REFUSED, reason=_CONFLICT_REASON[conflict.kind],
        )
    if record.id == request.id:
        return IdleCheckpointAdmissionResult(
            IdleCheckpointDisposition.ADMITTED, record, reason=IdleCheckpointReason.FIRED,
        )
    return IdleCheckpointAdmissionResult(
        IdleCheckpointDisposition.COALESCED, record, reason=IdleCheckpointReason.COALESCED,
    )


def consume_idle_marker(
    turn_id: str,
    request: CheckpointRequest | None,
    *,
    db: sqlite3.Connection | None = None,
) -> IdleCheckpointAdmissionResult:
    """Consume one completed-Turn marker and optionally create its checkpoint."""
    with write_transaction(db, scope="idle_checkpoint_consume") as connection:
        return _consume_marker(connection, turn_id, request)


def consume_idle_marker_failure(
    turn_id: str,
    request: CheckpointRequest | None,
    *,
    error: str,
    ended_at: str,
    db: sqlite3.Connection | None = None,
) -> IdleCheckpointAdmissionResult:
    """Consume one marker and return its typed failure atomically."""
    with write_transaction(db, scope="idle_checkpoint_failure") as connection:
        result = _consume_marker(connection, turn_id, request)
        if request is None:
            if result.disposition is IdleCheckpointDisposition.REFUSED:
                return IdleCheckpointAdmissionResult(
                    IdleCheckpointDisposition.FAILED, reason=IdleCheckpointReason.FAILED,
                )
            return result
        if (
            result.disposition is not IdleCheckpointDisposition.ADMITTED
            or result.record is None
        ):
            return result
        record = checkpoint_store.fail(
            request.id, error=error, ended_at=ended_at, db=connection
        )
        return IdleCheckpointAdmissionResult(
            IdleCheckpointDisposition.FAILED, record, reason=IdleCheckpointReason.FAILED,
        )
