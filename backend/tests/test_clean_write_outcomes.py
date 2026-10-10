"""Provider-write outcomes for the clean Turn state model."""

from __future__ import annotations

import sqlite3


def _fresh_db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    return db


def _seed_thread(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,model,execution_epoch,created_at,updated_at) "
        "VALUES ('thread-1','user-1','claude-code/opus',7,'t0','t0')"
    )


def _claimed_pair(db: sqlite3.Connection):
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    _seed_thread(db)
    for message_id in ("input-a", "input-b"):
        accept_thread_input(
            "thread-1",
            role="user",
            content=message_id,
            intent="apply_now",
            message_id=message_id,
            db=db,
        )
    db.commit()
    batch = start_idle_turn(
        "thread-1",
        turn_id="turn-1",
        boot_epoch="boot-1",
        started_at="t1",
        db=db,
    )
    assert batch is not None
    return batch


def test_write_acceptance_settles_the_exact_batch_in_transcript_order() -> None:
    """One attempt fences and accepts one whole FIFO batch exactly once."""
    from infra.input_writes import begin_batch_write, settle_batch_write

    db = _fresh_db()
    try:
        db.commit()
        batch = _claimed_pair(db)
        begin_batch_write(batch, attempt_id="attempt-1", started_at="t2", db=db)
        settle_batch_write("attempt-1", outcome="accepted", db=db)
        settle_batch_write("attempt-1", outcome="accepted", db=db)

        rows = db.execute(
            "SELECT id,delivery_state,delivery_attempt_id,write_outcome,seq "
            "FROM messages ORDER BY input_order"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("input-a", "delivered", "attempt-1", "accepted", 1),
            ("input-b", "delivered", "attempt-1", "accepted", 2),
        ]
        assert db.execute(
            "SELECT next_message_seq FROM threads WHERE id='thread-1'"
        ).fetchone()[0] == 3
    finally:
        db.close()


def test_settle_batch_write_work_is_independent_of_unrelated_history() -> None:
    """Exact settlement must seek its batch instead of scanning the transcript."""
    from infra.input_writes import begin_batch_write, settle_batch_write

    db = _fresh_db()
    try:
        db.commit()
        batch = _claimed_pair(db)
        begin_batch_write(batch, attempt_id="attempt-1", started_at="t2", db=db)
        db.execute(
            "WITH RECURSIVE history(value) AS ("
            "SELECT 1 UNION ALL SELECT value + 1 FROM history WHERE value < 10000"
            ") INSERT INTO messages (id,thread_id,role,content,created_at) "
            "SELECT 'history-' || value,'thread-1','assistant','old','t0' "
            "FROM history"
        )
        db.commit()

        progress_calls = 0

        def count_progress() -> int:
            nonlocal progress_calls
            progress_calls += 1
            return 0

        db.set_progress_handler(count_progress, 100)
        settle_batch_write("attempt-1", outcome="accepted", db=db)
        db.set_progress_handler(None, 0)

        rows = db.execute(
            "SELECT id,delivery_state,write_outcome,seq FROM messages "
            "WHERE delivery_attempt_id='attempt-1' ORDER BY input_order"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("input-a", "delivered", "accepted", 1),
            ("input-b", "delivered", "accepted", 2),
        ]
        assert progress_calls < 20
    finally:
        db.set_progress_handler(None, 0)
        db.close()


def test_proved_no_write_and_ambiguous_write_have_distinct_outcomes() -> None:
    """A provider refusal is definitive; an unknown write is never replayed."""
    from infra.input_writes import begin_batch_write, settle_batch_write

    refused_db = _fresh_db()
    ambiguous_db = _fresh_db()
    cancelled_db = _fresh_db()
    try:
        refused_db.commit()
        refused_batch = _claimed_pair(refused_db)
        begin_batch_write(
            refused_batch, attempt_id="refused-attempt", started_at="t2", db=refused_db,
        )
        settle_batch_write(
            "refused-attempt",
            outcome="proved_no_write",
            error="provider refused the request",
            db=refused_db,
        )
        refused = refused_db.execute(
            "SELECT delivery_state,write_outcome,next_attempt_at,seq "
            "FROM messages WHERE id='input-a'"
        ).fetchone()
        assert tuple(refused) == ("failed", "proved_no_write", None, 1)
        assert tuple(refused_db.execute(
            "SELECT status,retry_at FROM active_turns WHERE id='turn-1'"
        ).fetchone()) == ("running", None)

        ambiguous_batch = _claimed_pair(ambiguous_db)
        begin_batch_write(
            ambiguous_batch,
            attempt_id="ambiguous-attempt",
            started_at="t2",
            db=ambiguous_db,
        )
        settle_batch_write(
            "ambiguous-attempt",
            outcome="ambiguous",
            error="connection ended after dispatch",
            db=ambiguous_db,
        )
        ambiguous = ambiguous_db.execute(
            "SELECT delivery_state,write_outcome,next_attempt_at,seq "
            "FROM messages ORDER BY input_order"
        ).fetchall()
        assert [tuple(row) for row in ambiguous] == [
            ("ambiguous", "ambiguous", None, 1),
            ("ambiguous", "ambiguous", None, 2),
        ]

        cancelled_batch = _claimed_pair(cancelled_db)
        begin_batch_write(
            cancelled_batch,
            attempt_id="cancelled-attempt",
            started_at="t2",
            db=cancelled_db,
        )
        settle_batch_write(
            "cancelled-attempt",
            outcome="proved_no_write",
            no_write_state="cancelled",
            db=cancelled_db,
        )
        cancelled = cancelled_db.execute(
            "SELECT delivery_state,write_outcome,seq "
            "FROM messages ORDER BY input_order"
        ).fetchall()
        assert [tuple(row) for row in cancelled] == [
            ("cancelled", "proved_no_write", 1),
            ("cancelled", "proved_no_write", 2),
        ]
    finally:
        refused_db.close()
        ambiguous_db.close()
        cancelled_db.close()


def test_codex_quarantine_reads_the_canonical_ambiguous_outcome(isolated_db) -> None:
    """Codex must not miss a write whose provider acceptance is unknown."""
    from infra.database import get_db
    from subagents.codex_runtime_support import quarantined_unknown_write

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads "
            "(id,user_id,model,execution_epoch,created_at,updated_at) "
            "VALUES ('codex-thread','user-1','codex/luna',0,'t0','t0')"
        )
        db.execute(
            "INSERT INTO messages "
            "(id,thread_id,role,content,created_at,delivery_state,delivery_error,"
            "write_outcome,seq) VALUES "
            "('codex-input','codex-thread','user','hello','t1','ambiguous',"
            "'provider_write_outcome_unknown','ambiguous',1)"
        )
        db.commit()
    finally:
        db.close()

    assert quarantined_unknown_write("codex-thread", "codex-input") is True
