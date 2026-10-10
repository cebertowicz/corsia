"""I9 — every terminal delivery failure keeps its exact durable reason."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.contract


@pytest.mark.parametrize(
    "outcome,state,reason",
    [
        ("ambiguous", "ambiguous", "connection ended after dispatch"),
        ("proved_no_write", "failed", "provider refused before dispatch"),
    ],
)
def test_terminal_delivery_failure_keeps_its_reason(
    isolated_db,
    outcome: str,
    state: str,
    reason: str,
) -> None:
    """A failed input is visible and diagnosable without a side-channel log."""
    from infra.database import get_db
    from infra.input_admission import accept_thread_input
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_inputs import start_idle_turn

    connection = get_db()
    try:
        connection.execute(
            "INSERT INTO threads "
            "(id,user_id,model,execution_epoch,created_at,updated_at) "
            "VALUES ('i9-thread','u','claude-code/sonnet',0,'t0','t0')"
        )
        connection.commit()
        accept_thread_input(
            "i9-thread",
            role="user",
            content="deliver this",
            intent="apply_now",
            message_id="i9-input",
            db=connection,
        )
        batch = start_idle_turn(
            "i9-thread",
            turn_id="i9-turn",
            boot_epoch="i9-boot",
            started_at="t1",
            db=connection,
        )
        assert batch is not None
        begin_batch_write(
            batch,
            attempt_id="i9-attempt",
            started_at="t2",
            db=connection,
        )
        settle_batch_write(
            "i9-attempt",
            outcome=outcome,
            error=reason,
            db=connection,
        )
        row = connection.execute(
            "SELECT delivery_state,delivery_error,write_outcome "
            "FROM messages WHERE id='i9-input'"
        ).fetchone()
    finally:
        connection.close()

    assert tuple(row) == (state, reason, outcome)
