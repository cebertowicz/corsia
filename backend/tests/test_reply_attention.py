"""Unread reply chips project the canonical comment and viewed state."""

import asyncio

from doc_feedback_helpers import add_comment, seed_document, seed_thread
from doceditor.feedback_state import mark_thread_viewed
from doceditor.reply_attention import unread_reply_items
from infra.database import get_db, write_transaction


def test_unread_replies_group_by_thread_and_document_and_clear_on_read(isolated_db, tmp_path, monkeypatch):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "other")
        artifact_id = seed_document(db, "owner", name="brief.md")
        path = tmp_path / "brief.md"
        path.write_text("A document", encoding="utf-8")
        db.execute("UPDATE artifact_identities SET path=? WHERE id=?", (str(path), artifact_id))
        db.commit()
        first, _ = add_comment(db, artifact_id, author_kind="user", author_id=None)
        second, _ = add_comment(db, artifact_id, author_kind="user", author_id=None)
        one, _ = add_comment(db, artifact_id, author_id="owner", parent_id=first["id"])
        add_comment(db, artifact_id, author_id="owner", parent_id=second["id"])
        rows = unread_reply_items(db, "owner-token")
        assert len(rows) == 1
        assert rows[0]["detail"]["unread_count"] == 2
        assert rows[0]["detail"]["destination"]["path"] == str(path)
        assert unread_reply_items(db, "foreign-user") == []
        from api import attention

        async def immediate(fn):
            return fn()

        decision = {"external_id": "decision", "renderer": "decision", "detail": {}}
        monkeypatch.setattr(attention, "run_in_db", immediate)
        monkeypatch.setattr(attention, "_queue_for", lambda _: [decision])
        monkeypatch.setattr(attention, "_reply_items_for", lambda user: unread_reply_items(db, user))
        response = asyncio.run(attention.get_queue({"id": "owner-token"}))
        assert response["current"] == decision
        assert response["count"] == 1
        assert response["items"] == [decision]
        assert response["reply_items"] == rows
        with write_transaction(db, scope="test_reply_attention_view") as connection:
            mark_thread_viewed(connection, "owner-token", artifact_id, first["id"], one["id"])
        assert unread_reply_items(db, "owner-token")[0]["detail"]["unread_count"] == 1
        path.unlink()
        assert unread_reply_items(db, "owner-token") == []
    finally:
        db.close()
