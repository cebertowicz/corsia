"""Native document-feedback send tool behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
from doceditor import feedback_batches
from infra.database import get_db, write_transaction

pytestmark = pytest.mark.asyncio


async def _send(artifact_id: str | None, **kwargs):
    from tools.document_feedback import DocFeedbackSendTool

    return await DocFeedbackSendTool().execute(
        artifact_id=artifact_id,
        _thread_id="author",
        **kwargs,
    )


async def _capture_wakes(monkeypatch):
    calls: list[list[str]] = []

    async def drive_threads(thread_ids):
        calls.append(thread_ids)

    monkeypatch.setattr("plans.public.drive_threads", drive_threads)
    return calls


async def test_send_retries_undelivered_comment_after_owner_is_addressable(isolated_db, monkeypatch):
    calls = await _capture_wakes(monkeypatch)
    db = get_db()
    try:
        seed_thread(db, "owner", model="missing/model")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner", name="feedback.md")
        add_comment(db, artifact_id, submit=True)
        db.execute("UPDATE threads SET model=? WHERE id='owner'", ("openai/test-checkpoint",))
        db.commit()

        result = json.loads(await _send(artifact_id))

        assert result["artifact_id"] == artifact_id
        assert result["woken"] == ["owner"]
        assert result["undelivered_batches"] == []
        assert result["result"] == "Feedback was delivered to the owner."
        assert batches(db)[0]["state"] == "delivered"
        assert calls == [["owner"]]
    finally:
        db.close()


async def test_send_retries_undelivered_edit_after_owner_is_addressable(isolated_db, monkeypatch):
    calls = await _capture_wakes(monkeypatch)
    db = get_db()
    try:
        seed_thread(db, "owner", model="missing/model")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner", name="edited.md")
        with write_transaction(db, scope="test_record_edit") as connection:
            feedback_batches.record_edit(
                connection,
                artifact_id=artifact_id,
                recipient_id="owner",
                author_kind="agent",
                author_id="author",
                actor_display="Author",
                path=Path("/workspace/shared/edited.md"),
                before=b"before\n",
                after=b"after\n",
                at="2026-09-30T08:00:00+00:00",
            )
        db.commit()
        feedback_batches.close_for_document(
            db, author_kind="agent", author_id="author", artifact_id=artifact_id, reason="submit",
        )
        db.execute("UPDATE threads SET model=? WHERE id='owner'", ("openai/test-checkpoint",))
        db.commit()

        result = json.loads(await _send(artifact_id))

        assert result["woken"] == ["owner"]
        assert result["undelivered_batches"] == []
        assert batches(db)[0]["state"] == "delivered"
        assert calls == [["owner"]]
    finally:
        db.close()


async def test_send_closes_open_batch_and_reports_nothing_pending(isolated_db, monkeypatch):
    calls = await _capture_wakes(monkeypatch)
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner", name="open.md")
        add_comment(db, artifact_id)

        delivered = json.loads(await _send(artifact_id))
        empty = json.loads(await _send(artifact_id))

        assert delivered["woken"] == ["owner"]
        assert batches(db)[0]["state"] == "delivered"
        assert empty["result"] == "Nothing was pending for this document."
        assert empty["woken"] == []
        assert calls == [["owner"]]
    finally:
        db.close()


async def test_send_keeps_unaddressable_feedback_visible_without_duplicate_node(isolated_db, monkeypatch):
    calls = await _capture_wakes(monkeypatch)
    db = get_db()
    try:
        seed_thread(db, "owner", model="missing/model")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner", name="unaddressable.md")
        add_comment(db, artifact_id, submit=True)

        result = json.loads(await _send(artifact_id))

        assert result["woken"] == []
        assert result["undelivered_batches"][0]["undelivered_reason"] == "owner_unaddressable"
        assert "owner unaddressable" in result["result"]
        assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0
        assert calls == []
    finally:
        db.close()


async def test_send_requires_one_accessible_registered_target(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")

        assert await _send(None) == "Error: pass exactly one of artifact_id or doc_path"
        assert await _send(artifact_id, doc_path="/workspace/shared/feedback.md") == (
            "Error: pass exactly one of artifact_id or doc_path"
        )
        assert (await _send(None, doc_path="/workspace/shared/missing.md")).startswith("Error:")

        db.execute("UPDATE threads SET user_id=? WHERE id='author'", ("other-user",))
        db.commit()
        assert await _send(artifact_id) == (
            "Error: artifact is inaccessible; use an artifact owned by the current principal"
        )
    finally:
        db.close()
