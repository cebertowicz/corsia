"""Routed events are visible messages in the same FIFO."""

from __future__ import annotations

import json


def test_event_wake_has_one_message_and_no_delivery_ledger(isolated_db) -> None:
    from events.delivery import enqueue_event_wake
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('thread-1','user-1','native/test','t0','t0')"
        )
        db.commit()
        receipt = enqueue_event_wake(
            "thread-1",
            "github.pull_request.updated",
            "Pull request changed",
            {"url": "https://example.test/pr/1"},
            message_id="event-1",
            created_at="t1",
            db=db,
        )
        rows = db.execute(
            "SELECT role,content,metadata,input_order,delivery_state "
            "FROM messages WHERE thread_id='thread-1' ORDER BY input_order"
        ).fetchall()
        row = rows[1]
        assert len(rows) == 2
        assert "[PASSIVE CONTEXT DIGEST]" in rows[0]["content"]
        assert json.loads(rows[0]["metadata"])["event_type"] == "context.digest"
        assert receipt.input_order == 2
        assert row["role"] == "system"
        assert row["input_order"] == 2
        assert row["delivery_state"] == "queued"
        assert "[EVENT: github.pull_request.updated]" in row["content"]
        assert '"type": "context_event"' in row["metadata"]
    finally:
        db.close()
