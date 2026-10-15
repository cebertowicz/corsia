"""Startup Turn recovery and ambiguous provider-write quarantine."""

from __future__ import annotations

import sqlite3
from typing import Callable

from infra.database import write_transaction
from infra.input_writes import settle_batch_write
from infra.live_writer_hold import clear_live_writer_holds
from infra.thread_input_types import TurnAdmission
from infra.thread_inputs import start_idle_turn


def ambiguous_stale_writes(db: sqlite3.Connection, turn_id: str, *, error: str) -> None:
    """Record every fenced, unsettled provider write for a Turn as ambiguous."""

    attempts = db.execute(
        "SELECT DISTINCT delivery_attempt_id FROM messages "
        "WHERE active_turn_id=? AND delivery_state='queued' "
        "AND write_started_at IS NOT NULL AND write_outcome IS NULL",
        (turn_id,),
    ).fetchall()
    for row in attempts:
        settle_batch_write(
            str(row["delivery_attempt_id"]),
            outcome="ambiguous",
            error=error,
            db=db,
        )


def recover_threads(
    *,
    boot_epoch: str,
    recovered_at: str,
    successor_id: Callable[[str], str],
    db: sqlite3.Connection | None = None,
):
    """Run the only boot pass; it is not a periodic repair mechanism."""

    from infra.thread_driver import StartupRecovery, TerminalSettlement, finish_turn

    settlements: list[TerminalSettlement] = []
    batches: list[TurnAdmission] = []
    with write_transaction(db, scope="recover_threads") as connection:
        # No dead epoch can own a Codex writer, so nothing would release these holds.
        clear_live_writer_holds(connection)
        rows = connection.execute(
            "SELECT id,thread_id,status,boot_epoch,turn_order,execution_epoch,model "
            "FROM active_turns WHERE status='running' ORDER BY turn_order"
        ).fetchall()
        for row in rows:
            turn_id = str(row["id"])
            if str(row["boot_epoch"]) == boot_epoch:
                continue
            ambiguous_stale_writes(
                connection, turn_id, error="backend restarted after provider dispatch",
            )
            terminal = finish_turn(
                turn_id,
                outcome="error",
                ended_at=recovered_at,
                successor_turn_id=successor_id(str(row["thread_id"])),
                boot_epoch=boot_epoch,
                error="backend restarted while Turn was running",
                db=connection,
                hold_expired_limit_at_startup=True,
            )
            settlements.append(terminal)
        idle_threads = connection.execute(
            "SELECT DISTINCT m.thread_id FROM messages AS m "
            "WHERE m.delivery_state='queued' AND m.input_order IS NOT NULL "
            "AND m.active_turn_id IS NULL ORDER BY m.thread_id"
        ).fetchall()
        for row in idle_threads:
            thread_id = str(row["thread_id"])
            batch = start_idle_turn(
                thread_id,
                turn_id=successor_id(thread_id),
                boot_epoch=boot_epoch,
                started_at=recovered_at,
                db=connection,
                hold_expired_limit_at_startup=True,
            )
            if batch is not None:
                batches.append(batch)
    return StartupRecovery(tuple(settlements), tuple(batches))
