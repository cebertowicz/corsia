"""Every idle-checkpoint decision names itself in the server log.

The reason vocabulary already reaches the analytics store, but the server holds
that store's single connection, so a running instance cannot be asked why a
fold did not happen. These tests bind each decision point to the loguru sink
the server writes, which is the destination an operator can actually read.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator

import pytest
from loguru import logger

CC_MODEL = "claude-code/test"


@pytest.fixture()
def server_log() -> Iterator[list[str]]:
    """Capture what the server's own logger would write."""
    lines: list[str] = []
    sink_id = logger.add(lines.append, format="{message}", level="INFO")
    try:
        yield lines
    finally:
        logger.remove(sink_id)


def _db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,"
        "display_name,created_at,updated_at) VALUES (?,'claude-code','anthropic',"
        "?,?,?,'t0','t0')",
        (CC_MODEL,) * 4,
    )
    db.execute(
        "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,"
        "display_name,created_at,updated_at) VALUES "
        "('omlx@test','native','omlx','omlx@test','omlx@test','omlx@test','t0','t0')"
    )
    return db


def _child_thread(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('parent','user',?,'t0','t0')",
        (CC_MODEL,),
    )
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at,parent_thread_id) "
        "VALUES ('thread-1','user',?,'t0','t0','parent')",
        (CC_MODEL,),
    )
    db.execute(
        "INSERT INTO active_turns "
        "(id,thread_id,execution_epoch,model,status,started_at,ended_at,boot_epoch) "
        "VALUES ('turn-1','thread-1',0,?,'completed','t0','2026-01-01T00:00:00','boot')",
        (CC_MODEL,),
    )


def _due():
    from infra.idle_checkpoints import IdleCheckpointDue

    return IdleCheckpointDue("turn-1", 1, "thread-1", "2026-01-01T00:00:00")


def test_a_producer_exclusion_names_itself_in_the_server_log(
    server_log: list[str],
) -> None:
    from infra.idle_checkpoints import idle_checkpoint_due

    db = _db()
    try:
        _child_thread(db)
        assert idle_checkpoint_due(db, "turn-1") is None
    finally:
        db.close()
    assert any(
        "idle_checkpoint excluded" in line and "reason=child" in line
        for line in server_log
    ), server_log


def test_a_local_model_exclusion_names_its_own_reason(
    server_log: list[str],
) -> None:
    """The kept-back local fold gets a distinct, non-native reason (#2283)."""
    from infra.idle_checkpoints import idle_checkpoint_due

    db = _db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('thread-1','user','omlx@test','t0','t0')"
        )
        db.execute(
            "INSERT INTO active_turns "
            "(id,thread_id,execution_epoch,model,status,started_at,ended_at,boot_epoch) "
            "VALUES ('turn-1','thread-1',0,?,'completed','t0','2026-01-01T00:00:00','boot')",
            ("omlx@test",),
        )
        assert idle_checkpoint_due(db, "turn-1") is None
    finally:
        db.close()
    assert any(
        "idle_checkpoint excluded" in line
        and "reason=local_shared_model" in line
        for line in server_log
    ), server_log


def test_a_suppressed_completed_turn_names_its_cause(server_log: list[str]) -> None:
    from infra.idle_checkpoints import idle_marker_due

    db = _db()
    try:
        _child_thread(db)
        assert idle_marker_due(
            db, "turn-1", outcome="completed", successor_claimed=True,
            switch_pending=False, mark_idle_checkpoint=True,
        ) is None
    finally:
        db.close()
    assert any(
        "idle_checkpoint not_marked" in line and "reason=successor_claimed" in line
        for line in server_log
    ), server_log


def test_an_uncompleted_turn_is_not_logged_as_a_suppression(
    server_log: list[str],
) -> None:
    from infra.idle_checkpoints import idle_marker_due

    db = _db()
    try:
        _child_thread(db)
        assert idle_marker_due(
            db, "turn-1", outcome="cancelled", successor_claimed=False,
            switch_pending=False, mark_idle_checkpoint=True,
        ) is None
    finally:
        db.close()
    assert not any("idle_checkpoint not_marked" in line for line in server_log)


def test_arming_and_cancelling_a_timer_are_both_logged(
    server_log: list[str],
) -> None:
    import infra.idle_checkpoint_timers as idle_checkpoint_timers

    async def scenario() -> None:
        async def never(_due) -> None:  # pragma: no cover - the timer is cancelled
            await asyncio.sleep(3600)

        idle_checkpoint_timers.arm(_due(), 3600, never)
        idle_checkpoint_timers.cancel("thread-1")

    asyncio.run(scenario())
    assert any(
        "idle_checkpoint armed" in line and "thread=thread-1" in line
        for line in server_log
    ), server_log
    assert any(
        "idle_checkpoint timer_cancelled" in line for line in server_log
    ), server_log


def test_a_refusal_logs_its_reason_and_measured_tokens(
    server_log: list[str],
) -> None:
    from core.checkpoint_types import (
        IdleCheckpointAdmissionResult,
        IdleCheckpointDisposition,
        IdleCheckpointReason,
        IdleCheckpointSkip,
    )
    from thread.idle_checkpoint import _emit_result

    _emit_result(
        _due(),
        IdleCheckpointAdmissionResult(
            IdleCheckpointDisposition.REFUSED,
            reason=IdleCheckpointReason.BELOW_MINIMUM,
        ),
        IdleCheckpointSkip(
            IdleCheckpointReason.BELOW_MINIMUM,
            foldable_tokens=1_000,
            threshold_tokens=32_768,
        ),
    )
    assert any(
        "idle_checkpoint not_admitted" in line
        and "reason=below_minimum" in line
        and "foldable_tokens=1000" in line
        and "threshold_tokens=32768" in line
        for line in server_log
    ), server_log
