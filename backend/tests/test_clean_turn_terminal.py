"""Terminal and successor contracts for the clean Turn owner."""

from __future__ import annotations

import json
import sqlite3


def _db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    return db


def _session(
    db: sqlite3.Connection,
    thread_id: str,
    *,
    parent_id: str | None = None,
    is_subagent: bool = True,
) -> None:
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,model,parent_thread_id,spawned_by,execution_epoch,"
        "created_at,updated_at) VALUES (?,?,?,?,?,7,'t0','t0')",
        (
            thread_id,
            "user-1",
            "claude-code/opus",
            parent_id,
            parent_id if parent_id and is_subagent else None,
        ),
    )


def _running_turn(db: sqlite3.Connection, thread_id: str, turn_id: str) -> None:
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    accept_thread_input(
        thread_id,
        role="user",
        content="start",
        intent="apply_now",
        message_id=f"{turn_id}-input",
        db=db,
    )
    db.commit()
    batch = start_idle_turn(
        thread_id,
        turn_id=turn_id,
        boot_epoch="boot-1",
        started_at="t1",
        db=db,
    )
    assert batch is not None
    begin_batch_write(batch, attempt_id=f"{turn_id}-write", started_at="t2", db=db)
    settle_batch_write(f"{turn_id}-write", outcome="accepted", db=db)


def test_first_terminal_wins_and_starts_one_fifo_successor() -> None:
    """Terminal CAS and next-head claim are one transaction."""
    from infra.thread_driver import TerminalResult, finish_turn
    from infra.input_admission import accept_thread_input

    db = _db()
    try:
        _session(db, "thread-1")
        db.commit()
        _running_turn(db, "thread-1", "turn-1")
        accept_thread_input(
            "thread-1",
            role="user",
            content="next",
            intent="queue_after_turn",
            message_id="next-input",
            db=db,
        )

        first = finish_turn(
            "turn-1",
            outcome="completed",
            ended_at="t3",
            successor_turn_id="turn-2",
            boot_epoch="boot-1",
            db=db,
        )
        duplicate = finish_turn(
            "turn-1",
            outcome="completed",
            ended_at="t4",
            successor_turn_id="turn-3",
            boot_epoch="boot-1",
            db=db,
        )
        conflict = finish_turn(
            "turn-1",
            outcome="cancelled",
            ended_at="t4",
            successor_turn_id="turn-3",
            boot_epoch="boot-1",
            db=db,
        )

        assert first.result is TerminalResult.APPLIED
        assert first.successor is not None
        assert first.successor.turn_id == "turn-2"
        assert duplicate.result is TerminalResult.ALREADY_SAME_OUTCOME
        assert duplicate.successor is None
        assert conflict.result is TerminalResult.CONFLICTING_TERMINAL
        assert [tuple(row) for row in db.execute(
            "SELECT id,status FROM active_turns ORDER BY turn_order"
        )] == [("turn-1", "completed"), ("turn-2", "running")]
        assert db.execute(
            "SELECT active_turn_id FROM messages WHERE id='next-input'"
        ).fetchone()[0] == "turn-2"
    finally:
        db.close()


def test_finish_turn_work_is_independent_of_unrelated_history() -> None:
    """Terminal CAS and successor admission must seek the exact Turn inputs."""
    from infra.input_admission import accept_thread_input
    from infra.thread_driver import TerminalResult, finish_turn

    db = _db()
    try:
        _session(db, "thread-1")
        db.commit()
        _running_turn(db, "thread-1", "turn-1")
        accept_thread_input(
            "thread-1",
            role="user",
            content="next",
            intent="queue_after_turn",
            message_id="next-input",
            db=db,
        )
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
        result = finish_turn(
            "turn-1",
            outcome="completed",
            ended_at="t3",
            successor_turn_id="turn-2",
            boot_epoch="boot-1",
            db=db,
        )
        db.set_progress_handler(None, 0)

        assert result.result is TerminalResult.APPLIED
        assert result.successor is not None
        assert result.successor.turn_id == "turn-2"
        assert db.execute(
            "SELECT status FROM active_turns WHERE id='turn-1'"
        ).fetchone()[0] == "completed"
        assert db.execute(
            "SELECT active_turn_id FROM messages WHERE id='next-input'"
        ).fetchone()[0] == "turn-2"
        assert progress_calls < 20
    finally:
        db.set_progress_handler(None, 0)
        db.close()


def test_terminal_settles_an_unsettled_plan_dispatch_without_a_hold() -> None:
    """A Turn that dies mid-call closes its dispatch and keeps valid work.

    The call's effect is unknown, so the dispatch settles as ``unknown``. Plans
    does not turn a missing tool result into a generic ``uncertain_effect``
    hold: the attempt is still the executor's valid selection, so it stays
    open, the task keeps running, and a duplicate terminal changes nothing.
    """
    from infra.thread_driver import TerminalResult, finish_turn
    from plans import admission, service

    db = _db()
    try:
        _session(db, "thread-1")
        plan = service.ensure_plan("thread", "thread-1", db=db)
        node = service.add_node(
            plan.id,
            kind="task",
            title="External effect",
            acceptance_criteria="The external effect is reconciled",
            db=db,
        )
        selected = admission.activate(
            node.id,
            expected_revision=node.revision,
            actor_id="thread-1",
            provenance="test",
            db=db,
        )
        attempt_id = selected["admission"]["selection"]["attempt_id"]
        db.commit()
        _running_turn(db, "thread-1", "turn-1")
        db.execute(
            """INSERT INTO plan_tool_dispatches
               (id,attempt_id,turn_id,tool_name,started_at)
               VALUES ('dispatch-1',?,'turn-1','bash','t3')""",
            (attempt_id,),
        )

        first = finish_turn(
            "turn-1",
            outcome="error",
            ended_at="t4",
            successor_turn_id="turn-2",
            boot_epoch="boot-1",
            error="backend restarted",
            db=db,
        )
        settled = tuple(db.execute(
            "SELECT outcome,settled_at FROM plan_tool_dispatches"
        ).fetchone())
        duplicate = finish_turn(
            "turn-1",
            outcome="error",
            ended_at="t5",
            successor_turn_id="turn-3",
            boot_epoch="boot-1",
            error="backend restarted",
            db=db,
        )

        assert first.result is TerminalResult.APPLIED
        assert duplicate.result is TerminalResult.ALREADY_SAME_OUTCOME
        assert settled[0] == "unknown"
        assert settled[1] is not None
        assert db.execute("SELECT COUNT(*) FROM plan_node_holds").fetchone()[0] == 0
        assert [tuple(row) for row in db.execute(
            "SELECT id,released_at,release_reason FROM plan_node_attempts"
        )] == [(attempt_id, None, None)]
        projected = admission.project(node.id, db=db)
        assert projected["condition"] == "running"
        assert projected["selection"]["attempt_id"] == attempt_id
        assert projected["selection"]["in_flight_dispatch_count"] == 0
        assert tuple(db.execute(
            "SELECT outcome,settled_at FROM plan_tool_dispatches"
        ).fetchone()) == settled
    finally:
        db.close()


def test_stop_cancels_an_unwritten_claim_without_erasing_it() -> None:
    """Stopping before dispatch leaves one visible cancelled input, not a wedge."""
    from infra.thread_driver import finish_turn
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    db = _db()
    try:
        _session(db, "thread-1")
        accept_thread_input(
            "thread-1", role="user", content="stop me", intent="apply_now",
            message_id="input-1", db=db,
        )
        db.commit()
        assert start_idle_turn(
            "thread-1", turn_id="turn-1", boot_epoch="boot-1",
            started_at="t1", db=db,
        ) is not None

        finish_turn(
            "turn-1", outcome="cancelled", ended_at="t2",
            successor_turn_id="turn-2", boot_epoch="boot-1", db=db,
        )
        row = db.execute(
            "SELECT delivery_state,seq,active_turn_id FROM messages WHERE id='input-1'"
        ).fetchone()
        assert tuple(row) == ("cancelled", 1, "turn-1")
        assert db.execute(
            "SELECT COUNT(*) FROM active_turns WHERE status='running'"
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_child_completion_is_one_ordinary_parent_fifo_input() -> None:
    """Successful child terminal and parent admission cannot diverge."""
    from infra.thread_driver import finish_turn

    db = _db()
    try:
        _session(db, "parent")
        _session(db, "child", parent_id="parent")
        db.commit()
        _running_turn(db, "child", "child-turn")
        db.execute(
            "INSERT INTO messages "
            "(id,thread_id,role,content,metadata,created_at,delivery_state,seq) "
            "VALUES ('answer','child','assistant','done',?,'t3','delivered',2)",
            (json.dumps({"turn_id": "child-turn"}),),
        )
        db.execute("UPDATE threads SET next_message_seq=3 WHERE id='child'")

        result = finish_turn(
            "child-turn", outcome="completed", ended_at="t4",
            successor_turn_id="child-next", boot_epoch="boot-1", db=db,
        )
        rows = db.execute(
            "SELECT role,content,metadata,input_order,delivery_state "
            "FROM messages WHERE thread_id='parent' ORDER BY input_order"
        ).fetchall()
        assert "[PASSIVE CONTEXT DIGEST]" in rows[0]["content"]
        parent = next(
            row for row in rows
            if json.loads(row["metadata"] or "{}").get("child_turn_id")
            == "child-turn"
        )
        assert parent is not None
        assert parent["role"] == "user"
        assert parent["content"] == "Subagent finished:\n\ndone"
        assert json.loads(parent["metadata"])["child_turn_id"] == "child-turn"
        assert (parent["input_order"], parent["delivery_state"]) == (2, "queued")
        assert result.wake_thread_ids == ("parent",)

        duplicate = finish_turn(
            "child-turn", outcome="completed", ended_at="t5",
            successor_turn_id="unused", boot_epoch="boot-1", db=db,
        )
        assert duplicate.wake_thread_ids == ()
        assert db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id='parent' "
            "AND json_extract(metadata,'$.child_turn_id')='child-turn'"
        ).fetchone()[0] == 1
    finally:
        db.close()


def test_attached_main_completion_does_not_notify_linked_parent() -> None:
    """A visible attached thread is not a subagent completion source."""
    from infra.thread_driver import finish_turn

    db = _db()
    try:
        _session(db, "parent")
        _session(db, "attached", parent_id="parent", is_subagent=False)
        db.commit()
        _running_turn(db, "attached", "attached-turn")

        result = finish_turn(
            "attached-turn", outcome="completed", ended_at="t4",
            successor_turn_id="attached-next", boot_epoch="boot-1", db=db,
        )

        assert db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id='parent'"
        ).fetchone()[0] == 0
        assert result.wake_thread_ids == ()
    finally:
        db.close()


def test_blank_child_turn_is_silent() -> None:
    """A child Turn that produced nothing tells the parent nothing.

    The wrapper carries the child's result. Without one there is no result to
    deliver, and a bare "Subagent finished." would wake the parent for a
    no-op: a model call per blank Turn, and a legitimately unchanged wait
    that is never quiet.
    """
    from infra.thread_driver import finish_turn

    db = _db()
    try:
        _session(db, "parent")
        _session(db, "child", parent_id="parent")
        db.commit()
        _running_turn(db, "child", "child-turn")

        result = finish_turn(
            "child-turn", outcome="completed", ended_at="t4",
            successor_turn_id="child-next", boot_epoch="boot-1", db=db,
        )

        assert db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id='parent'"
        ).fetchone()[0] == 0
        assert result.wake_thread_ids == ()
    finally:
        db.close()
