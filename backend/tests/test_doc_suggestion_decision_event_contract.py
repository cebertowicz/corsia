"""Fail-first contracts for immediate suggestion-decision events."""

from __future__ import annotations

import pytest

from api.doc_comments import CommentDecision
from doc_feedback_helpers import add_comment, seed_document, seed_thread
from tools.documents import DocCommentUpdateTool


SUGGESTION_REASON_MAX_CHARS = 2_000


def test_schema_persists_one_durable_decision_event_identity(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(doc_comments)").fetchall()
        }
        assert "decision_event_id" in columns
        indexes = {row["name"] for row in db.execute("PRAGMA index_list(doc_comments)").fetchall()}
        assert "idx_doc_comments_decision_event" in indexes
    finally:
        db.close()


def test_decision_reason_is_optional_and_bounded_at_both_agent_boundaries():
    api_reason = CommentDecision.model_json_schema()["properties"]["reason"]
    tool_reason = DocCommentUpdateTool().parameters["properties"]["reason"]

    assert api_reason["maxLength"] == SUGGESTION_REASON_MAX_CHARS
    assert tool_reason["maxLength"] == SUGGESTION_REASON_MAX_CHARS
    assert "verbatim" in tool_reason["description"].lower()


@pytest.mark.asyncio
async def test_oversized_reason_is_refused_before_a_decision_write(isolated_db):
    from doceditor import comment_actions
    from infra.database import get_db

    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        suggestion, _ = add_comment(db, artifact_id, replacement="Replacement")

        async def unused_writer(*_args):
            raise AssertionError("rejection must not write the document")

        with pytest.raises(comment_actions.CommentActionError, match="at most 2000 characters"):
            await comment_actions.decide_suggestion(
                suggestion["id"], "reject", user_id="owner-token", decided_by="Robin",
                actor_thread_id=None, write=unused_writer, reason="x" * 2_001,
            )
        row = db.execute(
            "SELECT decision,decision_event_id FROM doc_comments WHERE id=?", (suggestion["id"],)
        ).fetchone()
        assert tuple(row) == (None, None)
    finally:
        db.close()
