"""The supported user actions on durable message rows."""

from __future__ import annotations


def test_delivered_history_has_no_edit_route() -> None:
    from main import app

    assert not [
        getattr(route, "path", "") for route in app.routes
        if getattr(route, "path", "")
        == "/api/threads/{thread_id}/messages/{message_id}"
        and "PATCH" in (getattr(route, "methods", None) or set())
    ]


def test_cancel_advances_the_canonical_message_revision(isolated_db) -> None:
    from infra.database import get_db
    from infra.input_admission import accept_thread_input
    from infra.queued_input_settlement import cancel_queued_input

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('cancel-thread','user-1','native/test','t0','t0')"
        )
        db.commit()
        accepted = accept_thread_input(
            "cancel-thread",
            role="user",
            content="do not send this",
            intent="queue_after_turn",
            message_id="cancel-me",
            db=db,
        )

        row = cancel_queued_input(
            "cancel-thread",
            "cancel-me",
            revision=accepted.revision,
            db=db,
        )

        assert row["delivery_state"] == "cancelled"
        assert row["delivery_revision"] == accepted.revision + 1
        assert row["write_outcome"] == "proved_no_write"
    finally:
        db.close()
