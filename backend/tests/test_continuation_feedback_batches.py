"""Document-feedback delivery through the registered continuation sweep."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from automation.continuation import runner
from automation.continuation.sources.feedback_batches import FeedbackBatchSource
from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
from doceditor import feedback_batches
from infra.database import get_db, write_transaction
from plans.feedback_nodes import PROVENANCE


@pytest.mark.asyncio
async def test_registered_feedback_source_closes_due_batch_and_drives_selected_idle_recipient(
    isolated_db,
):
    db = get_db()
    try:
        sources = runner.default_sources()
        assert any(isinstance(source, FeedbackBatchSource) for source in sources)
        seed_thread(db, "idle-owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "idle-owner")
        add_comment(db, artifact_id)
        db.execute("UPDATE doc_feedback_batches SET closes_at='2000-01-01T00:00:00+00:00'")
        db.commit()
        drive = AsyncMock(return_value=None)

        with patch("automation.continuation.runner.public.drive_threads", drive):
            await runner.sweep("2000-01-01T00:00:00+00:00", db=db, sources=sources)

        row = batches(db)[0]
        assert row["state"] == "delivered"
        assert row["delivered_node_id"] is not None
        assert db.execute(
            "SELECT COUNT(*) FROM plan_nodes WHERE id=?", (row["delivered_node_id"],)
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM plan_node_events WHERE provenance=? AND event='node_created'",
            (PROVENANCE,),
        ).fetchone()[0] == 1
        drive.assert_awaited_once_with(["idle-owner"])
    finally:
        db.close()


@pytest.mark.asyncio
async def test_due_user_feedback_notifies_the_open_document_canvas(isolated_db, monkeypatch):
    """The idle delivery must clear an already-visible Send N control."""
    db = get_db()
    try:
        seed_thread(db, "idle-owner")
        artifact_id = seed_document(db, "idle-owner")
        with write_transaction(db, scope="test_user_edit_feedback") as connection:
            feedback_batches.record_edit(
                connection,
                artifact_id=artifact_id,
                recipient_id="idle-owner",
                author_kind="user",
                author_id=feedback_batches.USER_AUTHOR_ID,
                actor_display="Steve",
                path=Path(f"/workspace/shared/{artifact_id}.md"),
                before=b"before",
                after=b"after",
            )
        db.execute("UPDATE doc_feedback_batches SET closes_at='2000-01-01T00:00:00+00:00'")
        db.commit()
        sent: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            "infra.state.broadcast_to_user",
            lambda user_id, frame: sent.append((user_id, frame)),
        )

        await runner.sweep("2000-01-01T00:00:00+00:00", db=db, sources=runner.default_sources())

        assert sent == [(
            "owner-token",
            {"type": "doc_comment_changed", "artifact_id": artifact_id},
        )]
    finally:
        db.close()
