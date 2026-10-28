"""DCOL-FR10.1 reopened comments join the user's owner-feedback batch."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
from doceditor import comment_actions, comments, feedback_batch_edits, feedback_batches
from infra.database import get_db, initialize_schema, write_transaction
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION
from tests.schema_rewind import rewind_to_v88_policy
from plans import service


def _resolved_comment(db, artifact_id: str, *, replacement: str | None = None) -> dict:
    comment, _ = add_comment(db, artifact_id, replacement=replacement)
    return comments.update(comment["id"], {"resolved": True}, db=db)


def _record_reopen(db, comment: dict, *, user_display: str = "Steve") -> None:
    with write_transaction(db, scope="test_record_reopen") as connection:
        feedback_batch_edits.record_reopen(
            connection,
            comment,
            user_id="owner-token",
            user_display=user_display,
            delivery_policy=feedback_batches.DOCUMENT_HELD,
            now="2026-10-02T22:00:00+00:00",
        )


def _resolve_again(db, comment_id: str) -> None:
    with write_transaction(db, scope="test_resolve_again") as connection:
        comments.update(comment_id, {"resolved": True}, db=connection)


def _user_batch(db) -> dict:
    return next(
        batch for batch in batches(db, state="open")
        if batch["author_kind"] == "user"
    )


def test_reopen_adds_an_event_item_to_the_users_owner_batch_and_delivers_node(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner", title="Documents")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "owner", name="DRAFT-PRD.md")
        comment = _resolved_comment(db, artifact_id)

        _record_reopen(db, comment)

        batch = _user_batch(db)
        item = db.execute(
            "SELECT comment_id,event,event_at,event_by FROM doc_feedback_batch_items WHERE batch_id=?",
            (batch["id"],),
        ).fetchone()
        assert dict(item) == {
            "comment_id": comment["id"],
            "event": "reopened",
            "event_at": "2026-10-02T22:00:00+00:00",
            "event_by": "Steve",
        }
        assert (batch["author_id"], batch["recipient_id"], batch["item_count"]) == (
            feedback_batches.USER_AUTHOR_ID,
            "owner",
            1,
        )
        assert feedback_batches.user_feedback_for_document(db, artifact_id)["open_count"] == 1

        assert feedback_batches.close_batch(db, batch["id"], "send")[0]
        delivered = next(row for row in batches(db) if row["id"] == batch["id"])
        node = service.get_node(delivered["delivered_node_id"], db=db)
        assert node.title == "Steve re-opened 1 comment on DRAFT-PRD"
        assert "Comment " + comment["id"] + " (re-opened by Steve after it was resolved)" in node.description
        assert "Feedback: Please check this point." in node.description
    finally:
        db.close()


def test_reopen_reaches_owner_when_owner_wrote_the_agent_comment(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner", title="Documents")
        artifact_id = seed_document(db, "owner", name="owner-comment.md")
        comment, feedback = add_comment(
            db, artifact_id, author_id="owner", author="Documents",
        )
        assert feedback["batch_ids"] == []
        comment = comments.update(comment["id"], {"resolved": True}, db=db)

        _record_reopen(db, comment)

        batch = _user_batch(db)
        assert batch["recipient_id"] == "owner"
        feedback_batches.close_batch(db, batch["id"], "send")
        delivered = next(row for row in batches(db) if row["id"] == batch["id"])
        assert delivered["state"] == "delivered"
    finally:
        db.close()


def test_reopen_of_agent_comment_uses_reopening_user_for_feedback_title(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner", title="Documents")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "owner", name="agent-comment.md")
        comment = _resolved_comment(db, artifact_id)

        _record_reopen(db, comment, user_display="Steve")
        batch = _user_batch(db)
        feedback_batches.close_batch(db, batch["id"], "send")

        delivered = next(row for row in batches(db) if row["id"] == batch["id"])
        node = service.get_node(delivered["delivered_node_id"], db=db)
        assert node.title == "Steve re-opened 1 comment on agent-comment"
        assert "Reviewer" not in node.title
    finally:
        db.close()


def test_reopen_in_an_existing_open_batch_does_not_double_count(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        comment = _resolved_comment(db, artifact_id)

        _record_reopen(db, comment)
        _record_reopen(db, comment)

        batch = _user_batch(db)
        assert batch["item_count"] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM doc_feedback_batch_items WHERE batch_id=?", (batch["id"],)
        ).fetchone()[0] == 1
    finally:
        db.close()


def test_reopen_with_no_owner_stays_visible_as_undelivered_feedback(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        comment = _resolved_comment(db, artifact_id)
        db.execute(
            "UPDATE artifact_registrations SET parent_thread_id='' WHERE artifact_id=?",
            (artifact_id,),
        )
        db.commit()

        _record_reopen(db, comment)

        batch = _user_batch(db)
        assert batch["recipient_id"] == ""
        feedback_batches.close_batch(db, batch["id"], "send")
        delivered = next(row for row in batches(db) if row["id"] == batch["id"])
        assert delivered["state"] == "undelivered"
        assert delivered["undelivered_reason"] == "no_owner"
    finally:
        db.close()


def test_reopen_with_missing_owner_stays_visible_as_undelivered_feedback(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        comment = _resolved_comment(db, artifact_id)
        db.execute(
            "UPDATE artifact_registrations SET parent_thread_id='missing-owner' WHERE artifact_id=?",
            (artifact_id,),
        )
        db.commit()

        _record_reopen(db, comment)

        batch = _user_batch(db)
        assert batch["recipient_id"] == "missing-owner"
        feedback_batches.close_batch(db, batch["id"], "send")
        delivered = next(row for row in batches(db) if row["id"] == batch["id"])
        assert delivered["state"] == "undelivered"
        assert delivered["undelivered_reason"] == "owner_unavailable"
    finally:
        db.close()


def test_reopened_rejected_suggestion_keeps_its_suggested_replacement(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "owner", name="suggestion.md")
        comment = _resolved_comment(db, artifact_id, replacement="Replacement text")
        comments.update(
            comment["id"],
            {"decision": None, "decided_by": None, "decided_by_thread_id": None, "decided_at": None},
            db=db,
        )

        _record_reopen(db, comment)
        batch = _user_batch(db)
        feedback_batches.close_batch(db, batch["id"], "send")

        delivered = next(row for row in batches(db) if row["id"] == batch["id"])
        node = service.get_node(delivered["delivered_node_id"], db=db)
        assert "Suggested replacement: Replacement text" in node.description
    finally:
        db.close()


def test_reresolve_discards_an_unsent_reopen_but_not_a_delivered_one(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        comment = _resolved_comment(db, artifact_id)
        _record_reopen(db, comment)
        _resolve_again(db, comment["id"])
        assert [batch for batch in batches(db, state="open") if batch["author_kind"] == "user"] == []

        comment = _resolved_comment(db, artifact_id)
        _record_reopen(db, comment)
        batch = _user_batch(db)
        feedback_batches.close_batch(db, batch["id"], "send")
        _resolve_again(db, comment["id"])
        assert next(row for row in batches(db) if row["id"] == batch["id"])["state"] == "delivered"
    finally:
        db.close()


def test_deciding_a_reopened_suggestion_drops_its_unsent_reopen(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        suggestion = _resolved_comment(db, artifact_id, replacement="new text")
        _record_reopen(db, suggestion)

        comment_actions.record_decision(
            suggestion["id"], decision="accept", decided_by="Steve",
            decided_by_thread_id=None, db=db,
        )

        assert [batch for batch in batches(db, state="open") if batch["author_kind"] == "user"] == []
        row = db.execute(
            "SELECT decision,decision_event_id FROM doc_comments WHERE id=?", (suggestion["id"],)
        ).fetchone()
        assert row["decision"] == "accepted"
        assert row["decision_event_id"]
    finally:
        db.close()


def test_reresolve_keeps_other_open_reopened_items(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        first = _resolved_comment(db, artifact_id)
        second = _resolved_comment(db, artifact_id)
        _record_reopen(db, first)
        _record_reopen(db, second)
        _resolve_again(db, first["id"])

        batch = _user_batch(db)
        assert batch["item_count"] == 1
        feedback_batches.close_batch(db, batch["id"], "send")
        delivered = next(row for row in batches(db) if row["id"] == batch["id"])
        node = service.get_node(delivered["delivered_node_id"], db=db)
        assert second["id"] in node.description
        assert first["id"] not in node.description
    finally:
        db.close()


def test_feedback_title_matches_delivery_categories_for_mixed_batch():
    from plans.feedback_nodes import _title

    items = [
        {"kind": "edit"},
        {"kind": "comment", "event": None},
        {"kind": "comment", "event": "reopened"},
    ]

    assert _title("A", "d", items) == (
        "A edited d and left 1 comment and 1 re-opened comment"
    )


def test_startup_migrates_existing_feedback_items_to_event_columns(tmp_path):
    path = tmp_path / "feedback-events-v80.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("DROP TABLE doc_feedback_batch_items")
        db.execute(
            """CREATE TABLE doc_feedback_batch_items (
                batch_id TEXT NOT NULL REFERENCES doc_feedback_batches(id) ON DELETE CASCADE,
                comment_id TEXT NOT NULL REFERENCES doc_comments(id) ON DELETE CASCADE,
                PRIMARY KEY (batch_id, comment_id)
            )"""
        )
        rewind_to_v88_policy(db)
        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("PRAGMA user_version=80")
        db.commit()
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        assert SCHEMA_VERSION == 159
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        columns = {row[1] for row in db.execute("PRAGMA table_info(doc_feedback_batch_items)")}
        assert columns == {"batch_id", "comment_id", "event", "event_at", "event_by"}
        assert "'reopened'" in db.execute(
            "SELECT sql FROM sqlite_master WHERE name='doc_feedback_batch_items'"
        ).fetchone()[0]
        assert "'rejected'" not in db.execute(
            "SELECT sql FROM sqlite_master WHERE name='doc_feedback_batch_items'"
        ).fetchone()[0]
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
