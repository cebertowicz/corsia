"""DCOL-FR4.1/4.2 batching and durable timer behavior."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from automation.plan_liveness_terminal import on_thread_turn_complete
from automation.turn_end_effects import record_turn_end
from doc_feedback_helpers import add_comment, batches, linked_comment_ids, seed_document, seed_thread
from doceditor import comment_notifications, feedback_batches
from infra.database import get_db, write_transaction


def test_agent_comments_coalesce_then_a_later_comment_opens_a_new_batch(isolated_db):
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
        fourth, _ = add_comment(db, artifact_id)
        all_rows = batches(db)
        assert [row["state"] for row in all_rows] == ["delivered", "open"]
        assert all_rows[0]["closed_reason"] == "quiet"
        assert linked_comment_ids(db, all_rows[1]["id"]) == [fourth["id"]]
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


def test_user_leave_return_restores_idle_and_due_rows_record_leave_or_idle(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        first_doc = seed_document(db, "owner", name="leave.md")
        second_doc = seed_document(db, "owner", name="idle.md")
        add_comment(db, first_doc, author_kind="user", author_id=None, author="Steve")

        with write_transaction(db, scope="test_leave") as connection:
            assert feedback_batches.mark_user_left(connection, first_doc) == 1
        leaving = batches(db, state="open")[0]
        assert leaving["closed_reason"] == "leave"
        with write_transaction(db, scope="test_return") as connection:
            assert feedback_batches.mark_user_returned(connection, first_doc) == 1
        returned = batches(db, state="open")[0]
        assert returned["closed_reason"] is None
        assert (
            datetime.fromisoformat(returned["closes_at"])
            - datetime.fromisoformat(returned["last_item_at"])
        ).total_seconds() == feedback_batches.USER_IDLE

        with write_transaction(db, scope="test_leave_again") as connection:
            feedback_batches.mark_user_left(connection, first_doc)
        leaving = batches(db, state="open")[0]
        feedback_batches.close_due(leaving["closes_at"], db=db)
        assert batches(db)[0]["closed_reason"] == "leave"

        add_comment(db, second_doc, author_kind="user", author_id=None, author="Steve")
        idle = batches(db, state="open")[0]
        feedback_batches.close_due(idle["closes_at"], db=db)
        assert batches(db)[1]["closed_reason"] == "idle"
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
        with write_transaction(db, scope="test_sentinel_leave") as connection:
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
            feedback_batches.mark_user_left(connection, artifact_id)
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
        add_comment(db, artifact_id, author_kind="user", author_id=None, author="Steve")
        del calls[:]  # the comment's own frame; this test is about the due batch
        with write_transaction(db, scope="test_thread_leave") as connection:
            feedback_batches.mark_user_left(connection, artifact_id)
        deadline = batches(db, state="open")[0]["closes_at"]
        feedback_batches.close_due(deadline, db=db)

        assert calls == [("owner-token", {"type": "doc_comment_changed", "artifact_id": artifact_id})]
    finally:
        db.close()


def test_user_feedback_projection_tracks_open_count_owner_and_undelivered(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner", title="Documents & Artifacts")
        artifact_id = seed_document(db, "owner")
        add_comment(db, artifact_id, author_kind="user", author_id=None, author="Steve")
        add_comment(db, artifact_id, author_kind="user", author_id=None, author="Steve")

        open_feedback = feedback_batches.user_feedback_for_document(db, artifact_id)
        assert open_feedback == {
            "open_count": 2,
            "owner_label": "Documents & Artifacts",
            "grace_closes_at": None,
            "undelivered_batches": [],
        }
        with write_transaction(db, scope="test_projection_leave") as connection:
            feedback_batches.mark_user_left(connection, artifact_id)
        grace_deadline = batches(db, state="open")[0]["closes_at"]
        assert feedback_batches.user_feedback_for_document(db, artifact_id)["grace_closes_at"] == grace_deadline
        with write_transaction(db, scope="test_projection_return") as connection:
            feedback_batches.mark_user_returned(connection, artifact_id)
        assert feedback_batches.user_feedback_for_document(db, artifact_id)["grace_closes_at"] is None

        feedback_batches.close_for_document(
            db,
            author_kind="user",
            author_id=feedback_batches.USER_AUTHOR_ID,
            artifact_id=artifact_id,
            reason="send",
        )
        assert feedback_batches.user_feedback_for_document(db, artifact_id)["open_count"] == 0

        add_comment(db, artifact_id, author_kind="user", author_id=None, author="Steve")
        batch = batches(db, state="open")[0]
        db.execute("UPDATE doc_feedback_batches SET recipient_id='' WHERE id=?", (batch["id"],))
        db.commit()
        feedback_batches.close_batch(db, batch["id"], "send")
        undelivered = feedback_batches.user_feedback_for_document(db, artifact_id)["undelivered_batches"]
        assert [(row["item_count"], row["undelivered_reason"]) for row in undelivered] == [(1, "no_owner")]
    finally:
        db.close()
