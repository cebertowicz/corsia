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
        assert set(rows[0]['detail']) == {'id', 'title', 'unread_count', 'newest_at', 'destination', 'source'}
        assert set(rows[0]['detail']['source']) == {'thread_id', 'thread_label'}
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


def test_grouping_matches_original_unread_predicate(isolated_db, tmp_path):
    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact_id = seed_document(db, 'owner')
        path = tmp_path / 'brief.md'
        path.write_text('A document', encoding='utf-8')
        db.execute('UPDATE artifact_identities SET path=? WHERE id=?', (str(path), artifact_id))
        db.commit()
        user_root, _ = add_comment(db, artifact_id, author_kind='user', author_id=None)
        agent_root, _ = add_comment(db, artifact_id, author_id='owner')
        untouched, _ = add_comment(db, artifact_id, author_id='owner')
        for _ in range(2):
            add_comment(db, artifact_id, author_kind='user', author_id=None, parent_id=agent_root['id'])
        for root in (user_root, agent_root, untouched):
            add_comment(db, artifact_id, author_id='owner', parent_id=root['id'])
        original = db.execute('''SELECT root.artifact_id, reply.author_thread_id, COUNT(*) AS n
            FROM doc_comments reply JOIN threads thread ON thread.id=reply.author_thread_id
            JOIN doc_comments root ON root.id=reply.parent_id
            LEFT JOIN doc_comment_thread_views view ON view.user_id=? AND view.root_id=root.id
            WHERE reply.author_kind='agent' AND thread.user_id=? AND root.resolved=0
              AND (root.author_kind='user' OR EXISTS (SELECT 1 FROM doc_comments touched
                   WHERE touched.parent_id=root.id AND touched.author_kind='user'))
              AND (reply.created_at>COALESCE(view.seen_at, '') OR
                   (view.seen_at IS NOT NULL AND reply.created_at=view.seen_at AND reply.id>view.seen_reply_id))
            GROUP BY root.artifact_id, reply.author_thread_id''', ('owner-token', 'owner-token')).fetchall()
        rows = unread_reply_items(db, 'owner-token')
        assert [(row['detail']['destination']['id'], row['detail']['source']['thread_id'], row['detail']['unread_count'])
                for row in rows] == [tuple(row) for row in original] == [(artifact_id, 'owner', 2)]
    finally:
        db.close()


def test_reply_attention_frames_cover_resolution_and_read(isolated_db, tmp_path, monkeypatch):
    from doceditor import comment_notifications, comment_thread_views, comments

    frames = []
    monkeypatch.setattr(comment_notifications, 'broadcast_doc_comment_changed',
                        lambda artifact_id, **kwargs: frames.append(kwargs.get('reply_attention_changed', False)))
    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact_id = seed_document(db, 'owner')
        path = tmp_path / 'brief.md'
        path.write_text('A document', encoding='utf-8')
        db.execute('UPDATE artifact_identities SET path=? WHERE id=?', (str(path), artifact_id))
        db.commit()
        root, _ = add_comment(db, artifact_id, author_kind='user', author_id=None)
        add_comment(db, artifact_id, author_id='owner')
        reply, _ = add_comment(db, artifact_id, author_id='owner', parent_id=root['id'])
        assert frames == [False, True]
        assert unread_reply_items(db, 'owner-token')[0]['detail']['unread_count'] == 1
        comments.update(root['id'], {'resolved': True}, db=db)
        assert frames[-1] is True
        assert unread_reply_items(db, 'owner-token') == []
        comments.update(root['id'], {'resolved': False}, db=db)
        assert frames[-1] is True
        assert unread_reply_items(db, 'owner-token')[0]['detail']['unread_count'] == 1
        comments.update(reply['id'], {'resolved': True}, db=db)
        assert frames[-1] is False
        assert comment_thread_views.mark_viewed('owner-token', artifact_id, root['id'], reply['id']) == []
        assert frames[-1] is True
        assert unread_reply_items(db, 'owner-token') == []
    finally:
        db.close()
