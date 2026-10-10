"""Crash-stranded provisional tool results must heal, not poison the thread.

Incident shape: a mid-turn failure between canonical tool persistence and
provider echo binding leaves a role='tool' row in binding state
``provisional`` with no ``tool_call_id``. Strict lineage projection (model
replay and portable history) rejected that row forever, failing every later
Turn and wedging every queued head on the thread.
"""

from __future__ import annotations

import json

import pytest

from shared_send_fixtures import _insert_session

SID = "thread-stranded"


def _insert_rows(db, rows: list[dict]) -> None:
    for seq, row in enumerate(rows, start=1):
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,"
            "created_at,seq,delivery_state) VALUES (?,?,?,?,?,?,?,'delivered')",
            (
                row["id"], SID, row["role"], row.get("content", ""),
                row["metadata"], f"2026-08-07T00:00:0{seq}Z", seq,
            ),
        )
    db.commit()


def _seed_stranded_thread(db, *, turn_status: str = "error") -> None:
    """User + assistant declaring one call + stranded provisional result."""
    _insert_session(SID, model="native-test-model", spawned_by=None)
    db.execute(
        "INSERT INTO active_turns "
        "(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES ('turn-crashed',?,0,'native-test-model',?,'t1','test-boot')",
        (SID, turn_status),
    )
    _insert_rows(db, [
        {
            "id": "m-user", "role": "user", "content": "run it",
            "metadata": json.dumps({"turn_id": "turn-crashed"}),
        },
        {
            "id": "step-1", "role": "assistant", "content": "",
            "metadata": json.dumps({
                "turn_id": "turn-crashed",
                "step_type": "tool_calls",
                "llm_tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "Bash", "arguments": "{}"},
                }],
            }),
        },
        {
            "id": "m-stranded", "role": "tool", "content": "partial output",
            "metadata": json.dumps({
                "assistant_step_id": "step-1",
                "name": "Bash",
                "turn_id": "turn-crashed",
                "tool_result_binding_state": "provisional",
            }),
        },
    ])


def test_quarantine_targets_only_terminal_turn_rows(isolated_db):
    from core.tool_result_binding import quarantine_stranded_tool_results
    from infra.database import get_db

    db = get_db()
    try:
        _seed_stranded_thread(db, turn_status="running")
        assert quarantine_stranded_tool_results(thread_id=SID, db=db) == 0

        db.execute(
            "UPDATE active_turns SET status='error' WHERE id='turn-crashed'"
        )
        assert quarantine_stranded_tool_results(thread_id=SID, db=db) == 1
        db.commit()
        state = json.loads(db.execute(
            "SELECT metadata FROM messages WHERE id='m-stranded'"
        ).fetchone()["metadata"])["tool_result_binding_state"]
        assert state == "quarantined_unbound"
        # Idempotent: nothing further to repair.
        assert quarantine_stranded_tool_results(thread_id=SID, db=db) == 0
    finally:
        db.close()


def test_replay_heals_stranded_row_and_stubs_the_call(isolated_db):
    from core.thread_history import rebuild_messages_from_db
    from infra.database import get_db

    db = get_db()
    try:
        _seed_stranded_thread(db)
    finally:
        db.close()

    messages = rebuild_messages_from_db(thread_id=SID, memory_window=None)

    roles = [m["role"] for m in messages]
    assert "assistant" in roles
    tool_messages = [m for m in messages if m["role"] == "tool"]
    # The declared call keeps exactly one result slot — the interruption stub —
    # never the unacknowledged partial content.
    assert len(tool_messages) == 1
    assert "partial output" not in (tool_messages[0].get("content") or "")

    # Second build finds durable repaired state, no repair pass needed.
    assert rebuild_messages_from_db(thread_id=SID, memory_window=None)


def test_portable_history_excludes_quarantined_and_stays_strict(isolated_db):
    from history.types import PortableHistoryError
    from history.projection import render_portable_history
    from infra.database import get_db

    db = get_db()
    try:
        _seed_stranded_thread(db)
        with pytest.raises(PortableHistoryError, match="invalid tool_call_id"):
            render_portable_history(SID, db=db)

        from core.tool_result_binding import quarantine_stranded_tool_results

        assert quarantine_stranded_tool_results(thread_id=SID, db=db) == 1
        db.commit()
        projection = render_portable_history(SID, db=db)
        assert projection is not None
    finally:
        db.close()

