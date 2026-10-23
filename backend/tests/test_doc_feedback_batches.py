"""DCOL-FR4.1/4.2 batching and durable timer behavior."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from automation.plan_liveness_terminal import on_thread_turn_complete
from automation.turn_end_effects import record_turn_end
from doc_feedback_helpers import add_comment, add_person_comment, batches, linked_comment_ids, seed_document, seed_thread
from doceditor import comment_notifications, feedback_batches
from infra.database import get_db, write_transaction


def add_user_edit(db, artifact_id: str, recipient_id: str = "owner") -> None:
    with write_transaction(db, scope="test_add_user_feedback_edit") as connection:
        feedback_batches.record_edit(
            connection,
            artifact_id=artifact_id,
            recipient_id=recipient_id,
            author_kind="user",
            author_id=feedback_batches.USER_AUTHOR_ID,
            actor_display="Steve",
            path=Path(f"/workspace/shared/{artifact_id}.md"),
            before=b"before",
            after=b"after",
        )


def test_each_person_comment_is_delivered_as_its_own_task(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner", title="Documents")
        artifact_id = seed_document(db, "owner", name="DRAFT-PRD.md")
        added = [add_person_comment(db, artifact_id) for _ in range(3)]

        rows = batches(db)
        assert [(row["state"], row["closed_reason"], row["item_count"]) for row in rows] == [("delivered", "send", 1)] * 3
        assert [linked_comment_ids(db, row["id"]) for row in rows] == [[row["id"]] for row, _feedback in added]
        assert len({row["delivered_node_id"] for row in rows}) == 3
    finally:
        db.close()


def test_person_delivery_announces_once_after_all_recipient_batches_close(isolated_db, monkeypatch):
    frames: list[str] = []
    monkeypatch.setattr(comment_notifications, "broadcast_doc_comment_changed", lambda artifact_id, **_kwargs: frames.append(artifact_id))
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        root, _ = add_comment(db, artifact_id)
        frames.clear()
        _reply, feedback = add_comment(
            db, artifact_id, author_kind="user", author_id=None, author="Steve", parent_id=root["id"],
        )

        assert len(feedback["batch_ids"]) == 2
        feedback_batches.deliver_person_comment(db, feedback["batch_ids"], artifact_id)

        assert frames == [artifact_id]
        assert all(row["state"] == "delivered" for row in batches(db) if row["id"] in feedback["batch_ids"])
    finally:
        db.close()


def test_agent_comments_keep_their_quiet_batch_after_an_earlier_delivery(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner", title="Documents")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "owner", name="DRAFT-PRD.md")
        comments = [add_comment(db, artifact_id)[0] for _ in range(3)]

        open_rows = batches(db, state="open")
        assert len(open_rows) == 1
        assert open_rows[0]["item_count"] == 3
        assert linked_comment_ids(db, open_rows[0]["id"]) == sorted(row["id"] for row in comments)
        assert (
            datetime.fromisoformat(open_rows[0]["closes_at"])
            - datetime.fromisoformat(open_rows[0]["last_item_at"])
        ).total_seconds() == feedback_batches.AGENT_QUIET

        assert feedback_batches.close_batch(db, open_rows[0]["id"], "quiet")[0]
        fourth, _feedback = add_comment(db, artifact_id)
        later = batches(db, state="open")
        assert [linked_comment_ids(db, row["id"]) for row in later] == [[fourth["id"]]]
    finally:
        db.close()


def test_user_comment_delivers_immediately_and_client_replay_is_a_noop(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner")

        comment, feedback = add_person_comment(db, artifact_id, client_id="first")
        replay, replay_feedback = add_person_comment(db, artifact_id, client_id="first")

        [batch] = batches(db)
        assert replay["id"] == comment["id"]
        assert (batch["state"], batch["closed_reason"], batch["attempts"]) == ("delivered", "send", 1)
        assert linked_comment_ids(db, batch["id"]) == [comment["id"]]
        assert feedback["woken"] == ["owner"]
        assert replay_feedback["woken"] == []
    finally:
        db.close()


def test_comment_while_an_earlier_task_is_selected_queues_behind_it_without_a_second_wake(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner")
        first, opened = add_person_comment(db, artifact_id)
        assert opened["woken"] == ["owner"]

        second, queued = add_person_comment(db, artifact_id)

        rows = batches(db)
        assert len(rows) == 2
        assert linked_comment_ids(db, rows[0]["id"]) == [first["id"]]
        assert linked_comment_ids(db, rows[1]["id"]) == [second["id"]]
        assert rows[0]["delivered_node_id"] != rows[1]["delivered_node_id"]
        # Admission never preempts the selected task, so the owner is not woken twice.
        assert queued["woken"] == []
    finally:
        db.close()


def test_comment_after_the_earlier_task_settled_selects_a_new_task_and_wakes_the_owner(isolated_db):
    from plans import admission, service

    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner")
        add_person_comment(db, artifact_id)
        [delivered] = batches(db)
        node_id = delivered["delivered_node_id"]
        admission.release(node_id, "test", db=db)
        service.transition_node(node_id, "closed", db=db)
        db.commit()

        _second, feedback = add_person_comment(db, artifact_id)

        rows = batches(db)
        assert len(rows) == 2 and rows[0]["delivered_node_id"] != rows[1]["delivered_node_id"]
        assert feedback["woken"] == ["owner"]
    finally:
        db.close()


def test_submit_and_turn_end_close_agent_batches_with_their_exact_reasons(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        first_doc = seed_document(db, "owner", name="one.md")
        second_doc = seed_document(db, "owner", name="two.md")

        add_comment(db, first_doc, submit=True)
        assert batches(db)[0]["closed_reason"] == "submit"
        add_comment(db, second_doc)
        with write_transaction(db, scope="test_turn_end") as connection:
            feedback_batches.close_for_turn_end(connection, "author")
        assert [row["closed_reason"] for row in batches(db)] == ["submit", "turn_end"]
        assert all(row["state"] == "delivered" for row in batches(db))
    finally:
        db.close()


def test_replayed_comment_with_submit_does_not_submit(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner", name="one.md")
        add_comment(db, artifact_id, client_id="replayed")
        add_comment(db, artifact_id, client_id="replayed", submit=True)
        assert [row["state"] for row in batches(db)] == ["open"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_record_turn_end_closes_agent_author_batch_through_turn_end_effects(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        add_comment(db, artifact_id)

        effects = await record_turn_end(on_thread_turn_complete, "author")

        row = batches(db)[0]
        assert (row["state"], row["closed_reason"]) == ("delivered", "turn_end")
        assert row["delivered_node_id"] is not None
        assert effects.drives == ("owner",)
    finally:
        db.close()


def test_user_edit_delivers_at_its_persisted_idle_deadline(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner", name="idle.md")
        add_user_edit(db, artifact_id)

        [open_batch] = batches(db, state="open")
        assert (
            datetime.fromisoformat(open_batch["closes_at"])
            - datetime.fromisoformat(open_batch["last_item_at"])
        ).total_seconds() == feedback_batches.USER_IDLE

        feedback_batches.close_due(open_batch["closes_at"], db=db)
        [delivered] = batches(db)
        assert delivered["closed_reason"] == "idle"
    finally:
        db.close()


def test_owner_comment_is_a_note_and_reply_batches_to_root_author_and_owner(isolated_db):
    db = get_db()
    try:
        for thread_id in ("owner", "root-author", "replier"):
            seed_thread(db, thread_id)
        artifact_id = seed_document(db, "owner")
        add_comment(db, artifact_id, author_id="owner", author="Owner")
        assert batches(db) == []

        root, _ = add_comment(db, artifact_id, author_id="root-author", author="Root")
        reply, _ = add_comment(
            db, artifact_id, author_id="replier", author="Replier", parent_id=root["id"],
        )
        reply_batches = [row for row in batches(db) if row["author_id"] == "replier"]
        assert {row["recipient_id"] for row in reply_batches} == {"root-author", "owner"}
        assert all(linked_comment_ids(db, row["id"]) == [reply["id"]] for row in reply_batches)
    finally:
        db.close()


def test_user_send_closes_all_recipient_batches_now(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner")
        add_comment(db, artifact_id, author_kind="user", author_id=None, author="Steve")
        feedback_batches.close_for_document(
            db,
            author_kind="user",
            author_id=feedback_batches.USER_AUTHOR_ID,
            artifact_id=artifact_id,
            reason="send",
        )
        row = batches(db)[0]
        assert (row["state"], row["closed_reason"], row["attempts"]) == ("delivered", "send", 1)
    finally:
        db.close()


def test_due_user_feedback_for_sentinel_document_broadcasts_to_all(isolated_db, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(comment_notifications.state, "broadcast_all", calls.append)
    db = get_db()
    try:
        artifact_id = seed_document(db, "ui")
        with write_transaction(db, scope="test_sentinel_due") as connection:
            feedback_batches.record_edit(
                connection,
                artifact_id=artifact_id,
                recipient_id="ui",
                author_kind="user",
                author_id=feedback_batches.USER_AUTHOR_ID,
                actor_display="Steve",
                path=Path("/workspace/shared/sentinel.md"),
                before=b"before",
                after=b"after",
            )
        deadline = batches(db, state="open")[0]["closes_at"]
        feedback_batches.close_due(deadline, db=db)

        assert calls == [{"type": "doc_comment_changed", "artifact_id": artifact_id}]
    finally:
        db.close()


def test_due_user_feedback_for_thread_document_broadcasts_to_owner(isolated_db, monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        comment_notifications.state,
        "broadcast_to_user",
        lambda user_id, message: calls.append((user_id, message)),
    )
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner")
        add_user_edit(db, artifact_id)
        deadline = batches(db, state="open")[0]["closes_at"]
        feedback_batches.close_due(deadline, db=db)

        assert calls == [("owner-token", {"type": "doc_comment_changed", "artifact_id": artifact_id})]
    finally:
        db.close()


def test_user_feedback_projection_has_no_open_comment_after_immediate_delivery(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner", title="Documents & Artifacts")
        artifact_id = seed_document(db, "owner")
        add_person_comment(db, artifact_id)
        add_person_comment(db, artifact_id)

        assert feedback_batches.user_feedback_for_document(db, artifact_id) == {
            "open_count": 0,
            "owner_label": "Documents & Artifacts",
            "undelivered_batches": [],
        }
        assert [row["state"] for row in batches(db)] == ["delivered", "delivered"]
    finally:
        db.close()


def test_due_sweep_without_a_supplied_db_keeps_its_connection_through_publication(monkeypatch):
    events: list[str] = []

    class Result:
        def fetchall(self):
            return [{"id": "batch", "artifact_id": "artifact", "author_kind": "user", "closed_reason": None}]

    class Connection:
        in_transaction = False

        def execute(self, _sql, _params):
            events.append("select")
            return Result()

        def close(self):
            events.append("close")

    def close_batch(received, _batch_id, reason, *, due_by):
        assert received is connection
        assert (reason, due_by) == (None, "2000-01-01T00:00:00+00:00")
        events.append("close_batch")
        return True, ("owner",)

    def collect(_deliveries, received, _row, _closed):
        assert received is connection
        events.append("collect")

    connection = Connection()
    monkeypatch.setattr(feedback_batches, "get_db", lambda: connection)
    monkeypatch.setattr(feedback_batches, "close_batch", close_batch)
    monkeypatch.setattr(comment_notifications, "collect_due_user_delivery", collect)
    monkeypatch.setattr(comment_notifications, "publish_due_user_feedback", lambda _deliveries: events.append("publish"))

    assert feedback_batches.close_due("2000-01-01T00:00:00+00:00") == ["owner"]
    assert events == ["select", "close_batch", "collect", "publish", "close"]
