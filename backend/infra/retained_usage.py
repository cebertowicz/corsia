"""Usage a locked database refused, held until its thread's Turn ends.

A measured call is handed to the ledger as it happens (``infra.llm_usage``).
When that write cannot commit -- observed in production as a long
``checkpoint_install`` holding the SQLite write lock for 18-44 s against the
5 s busy timeout -- no durable surface is writable either, so the call is
held here in process memory and written inside the thread's next terminal
transaction (``infra.thread_driver.finish_turn``): the one write every Turn
this process runs must commit, and the same transaction that already carries
the Turn's completed tool results.

The holder is failure-only. With nothing held, settling reads and writes
nothing. An entry leaves the holder only after the transaction that wrote it
has committed; a terminal nested in a caller's transaction writes held calls
but keeps them, and the next settle finds them durable and releases them. A
process exit while a call is held loses it: that gap is accepted in place of a
second durability surface, and restart recovery still settles the Turn.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
import threading
from typing import Any

from loguru import logger

from infra.database import write_transaction
from infra.llm_usage_events import emit_llm_usage_recorded
from infra.turn_usage_receipts import (
    PLAN_FIELDS,
    UsageProjection,
    UsageReceiptConflictError,
    _project_turn_usage_receipt_in_transaction,
    _receipt_id,
    store_turn_usage_receipt,
    write_usage_row,
)

_UNBOUND: Mapping[str, Any] = {field_name: None for field_name in PLAN_FIELDS}


@dataclass(frozen=True)
class HeldUsage:
    """One measured call whose handoff to the ledger could not commit.

    ``binding`` is the call's attribution as its caller passed it: None means
    the Turn's own snapshot, read when the call is written. ``usage_id`` is
    fixed before the first attempt, so a direct write that committed and then
    raised is found rather than written twice.
    """

    thread_id: str
    turn_id: str | None
    usage_key: str | None
    usage_id: str
    model: str
    measured: Mapping[str, Any]
    ttft_ms: int | None
    binding: Mapping[str, Any] | None
    source: str
    now_iso: str
    vendor_account_id: str | None = None
    reasoning_effort: str | None = None

    @property
    def turn_bound(self) -> bool:
        """Whether the call is written as a receipt on its own Turn."""
        return self.turn_id is not None and self.usage_key is not None

    @property
    def identity(self) -> str:
        """The ledger row id a Turn-bound call lands under; stable across replays."""
        if self.turn_bound:
            return _receipt_id(str(self.turn_id), str(self.usage_key))
        return self.usage_id


_held_lock = threading.Lock()
_held: dict[str, dict[str, HeldUsage]] = {}


def hold_usage(entry: HeldUsage) -> None:
    """Hold one unwritten call for its thread's next terminal; a replay is the same entry."""
    with _held_lock:
        _held.setdefault(entry.thread_id, {}).setdefault(entry.identity, entry)


def held_turn_usage(turn_id: str | None = None) -> tuple[HeldUsage, ...]:
    """Every held Turn-bound call, or only ``turn_id``'s."""
    with _held_lock:
        return tuple(
            entry
            for entries in _held.values()
            for entry in entries.values()
            if entry.turn_bound and (turn_id is None or entry.turn_id == turn_id)
        )


def _thread_entries(thread_id: str) -> tuple[HeldUsage, ...]:
    with _held_lock:
        return tuple(_held.get(thread_id, {}).values())


def _release(thread_id: str, identities: list[str]) -> None:
    with _held_lock:
        entries = _held.get(thread_id, {})
        for identity in identities:
            entries.pop(identity, None)
        if not entries:
            _held.pop(thread_id, None)


def _write_direct(tx: Any, entry: HeldUsage) -> UsageProjection | None:
    """Write a call with no Turn row to hold it, unless an earlier write landed."""
    ids = [entry.usage_id]
    if entry.turn_bound:
        ids.append(entry.identity)
    marks = ",".join("?" for _ in ids)
    if tx.execute(f"SELECT 1 FROM llm_usage WHERE id IN ({marks})", ids).fetchone():
        return None
    write_usage_row(
        tx, usage_id=entry.usage_id, thread_id=entry.thread_id, turn_id=entry.turn_id,
        model=entry.model, measured=entry.measured, ttft_ms=entry.ttft_ms,
        binding=_UNBOUND, source=entry.source, now_iso=entry.now_iso,
        vendor_account_id=entry.vendor_account_id, reasoning_effort=entry.reasoning_effort,
    )
    return UsageProjection(
        inserted=True, thread_id=entry.thread_id, model=entry.model,
        measured=entry.measured, ttft_ms=entry.ttft_ms, source=entry.source,
    )


def _write_entry(tx: Any, entry: HeldUsage) -> UsageProjection | None:
    """Store the call's receipt on its Turn and project it, as the live path would have."""
    if entry.turn_bound:
        stored = store_turn_usage_receipt(
            tx, thread_id=entry.thread_id, turn_id=str(entry.turn_id),
            usage_key=str(entry.usage_key), model=entry.model, measured=entry.measured,
            ttft_ms=entry.ttft_ms, source=entry.source, now_iso=entry.now_iso,
            binding=entry.binding, reasoning_effort=entry.reasoning_effort,
        )
        if stored is not None:
            identity, already_projected, _binding = stored
            if already_projected:
                return None
            return _project_turn_usage_receipt_in_transaction(tx, str(entry.turn_id), identity)
    return _write_direct(tx, entry)


def _report(entry: HeldUsage, reason: str, error: Exception) -> None:
    """Put a held call its terminal could not store on its task, as the live path does.

    The call's own binding names the task, so this goes to the report writer
    itself; ``report_unrecordable_call`` would name the Turn's snapshot task.
    """
    from plans.public import report_usage_defect

    try:
        report_usage_defect(
            entry.thread_id, entry.binding, entry.turn_id, entry.usage_key,
            reason=reason, detail=f"held usage could not be written at Turn end: {error}",
        )
    except Exception:
        logger.exception(
            "retained_usage: defect for held usage {} could not be reported", entry.identity,
        )


@dataclass
class UsageSettlement:
    """The held calls one terminal transaction wrote, finished once it commits."""

    owns_commit: bool
    thread_id: str | None = None
    written: list[HeldUsage] = field(default_factory=list)
    inserted: list[UsageProjection] = field(default_factory=list)
    conflicts: list[tuple[HeldUsage, Exception]] = field(default_factory=list)
    failures: list[tuple[HeldUsage, Exception]] = field(default_factory=list)

    def settle(self, connection: Any, thread_id: str) -> None:
        """Write every call held for ``thread_id`` inside the caller's open transaction.

        Each call is its own savepoint: one that cannot be written stays held
        and cannot roll back the terminal or the other calls. An error after
        which SQLite itself ended the caller's transaction is re-raised: the
        terminal must fail rather than go on writing outside it.
        """
        self.thread_id = thread_id
        nested = connection.in_transaction
        for entry in _thread_entries(thread_id):
            try:
                with write_transaction(connection, scope="settle_held_usage") as tx:
                    projection = _write_entry(tx, entry)
            except UsageReceiptConflictError as error:
                self.conflicts.append((entry, error))
                continue
            except Exception as error:
                if nested and not connection.in_transaction:
                    raise
                logger.exception(
                    "retained_usage: held usage {} for {} stays held", entry.identity, thread_id,
                )
                self.failures.append((entry, error))
                continue
            self.written.append(entry)
            if projection is not None and projection.inserted:
                self.inserted.append(projection)

    def finish(self) -> None:
        """After the owning commit: release written calls, emit telemetry, report defects.

        A conflicting call is a different measurement already on record under
        its key; it is released as that defect. A call that failed stays held
        for the next terminal and is reported once as ``persist_failed``.
        """
        if not self.owns_commit or self.thread_id is None:
            return
        _release(
            self.thread_id,
            [entry.identity for entry in self.written]
            + [entry.identity for entry, _error in self.conflicts],
        )
        for projection in self.inserted:
            emit_llm_usage_recorded(
                projection.thread_id, projection.model, projection.measured,
                source=projection.source, ttft_ms=projection.ttft_ms,
            )
        for entry, error in self.conflicts:
            _report(entry, "measurement_conflict", error)
        for entry, error in self.failures:
            _report(entry, "persist_failed", error)


@contextmanager
def usage_settlement(db: Any) -> Iterator[UsageSettlement]:
    """Yield the settlement a terminal fills in; finish it only once that terminal committed.

    Entered before the terminal's write transaction and left after it, so a
    terminal that raises leaves every held call held. ``db`` is the terminal's
    own argument: a connection already in a transaction belongs to a caller
    whose commit this frame cannot see.
    """
    settlement = UsageSettlement(owns_commit=db is None or not db.in_transaction)
    yield settlement
    settlement.finish()
