"""DCOL-FR4.5/4.7/4.7a response and passive-surface behavior."""

from __future__ import annotations

import pytest

from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
from doceditor import comments, feedback_batches, feedback_state
from doceditor.comment_action_errors import CommentActionError
from doceditor.comment_actions import reopen_fields
from events.connectors.document import MANIFEST
from infra.database import get_db, write_transaction
from plans import service
from registry import view
from registry.retirement_predicate import RETIREMENT_NOT_EXEMPT_CLAUSE
from tests.plan_review_helpers import submit_review_evidence


def test_resolve_always_sets_acknowledged_in_the_shared_action_path(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        comment, _ = add_comment(db, artifact_id)
        patch = reopen_fields(comment, {"resolved": True})
        updated = comments.update(comment["id"], patch, db=db)
        assert (updated["resolved"], updated["acked"]) == (1, 1)
    finally:
        db.close()


def test_resolving_an_undecided_suggestion_is_refused_in_the_shared_action_path(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        suggestion, _ = add_comment(db, artifact_id, replacement="Replacement")

        with pytest.raises(CommentActionError, match="Accept or reject this suggestion") as raised:
            reopen_fields(suggestion, {"resolved": True})

        assert getattr(raised.value, "kind", None) == "suggestion_decision_required"
        stored = comments.get_comment(suggestion["id"], db=db)
        assert (stored["resolved"], stored["decision"]) == (0, None)
    finally:
        db.close()


def test_reply_waiting_marker_is_passive_and_clears_one_seen_thread(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner", name="replies.md")
        first, _ = add_comment(
            db, artifact_id, author_kind="user", author_id=None, author="Steve",
        )
        first_reply, _ = add_comment(
            db, artifact_id, author_id="owner", author="Owner", parent_id=first["id"],
        )
        second, _ = add_comment(
            db, artifact_id, author_kind="user", author_id=None, author="Steve",
        )
        add_comment(db, artifact_id, author_id="owner", author="Owner", parent_id=second["id"])

        assert feedback_batches.reply_waiting_ids(db, "owner-token") == {artifact_id}
        with write_transaction(db, scope="test_mark_one_thread_viewed") as connection:
            assert feedback_state.mark_thread_viewed(
                connection, "owner-token", artifact_id, first["id"], first_reply["id"],
            ) == []
        assert feedback_batches.reply_waiting_ids(db, "owner-token") == {artifact_id}
        threads = comments.list_for_artifact(artifact_id, db=db)
        feedback_state.annotate_thread_attention(db, "owner-token", artifact_id, threads)
        by_root = {thread["id"]: thread for thread in threads}
        assert {thread["id"] for thread in threads if thread["new_reply_ids"]} == {second["id"]}
        assert by_root[first["id"]]["new_reply_ids"] == []
        assert len(by_root[second["id"]]["new_reply_ids"]) == 1
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    finally:
        db.close()


def test_first_user_comment_makes_workspace_document_retirement_exempt(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner", retention="ephemeral")
        before = db.execute(
            "SELECT retention FROM artifact_identities WHERE id=?", (artifact_id,)
        ).fetchone()
        assert before["retention"] == "ephemeral"

        add_comment(db, artifact_id, author_kind="user", author_id=None, author="Steve")
        identity = db.execute(
            "SELECT retention FROM artifact_identities WHERE id=?", (artifact_id,)
        ).fetchone()
        assert identity["retention"] == "durable"
        not_exempt = db.execute(
            f"""SELECT 1 FROM artifact_registrations AS r
                WHERE r.artifact_id=? AND {RETIREMENT_NOT_EXEMPT_CLAUSE}""",
            (artifact_id,),
        ).fetchone()
        assert not_exempt is None

        batch = batches(db, state="open")[0]
        feedback_batches.close_batch(db, batch["id"], "send")
        delivered = batches(db)[0]
        node_id = delivered["delivered_node_id"]
        service.transition_node(node_id, "review_requested", db=db)
        submit_review_evidence(node_id, db=db)
        service.transition_node(
            node_id, "accepted", verification_result="passed", verified_by="reviewer", db=db,
        )
        registration = db.execute(
            "SELECT retired_at FROM artifact_registrations WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()
        assert registration["retired_at"] is None
    finally:
        db.close()


def test_comment_created_is_emitted_once_as_informational_and_never_wakes(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        comment, _ = add_comment(db, artifact_id, client_id="stable-comment")
        duplicate, _ = add_comment(db, artifact_id, client_id="stable-comment")
        assert duplicate["id"] == comment["id"]

        declaration = MANIFEST.decl("comment.created")
        assert declaration.event_class == "informational"
        events = db.execute(
            """SELECT * FROM context_change_journal
               WHERE object_id=? AND change_kind='comment.created'""",
            (artifact_id,),
        ).fetchall()
        assert len(events) == 1
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    finally:
        db.close()
