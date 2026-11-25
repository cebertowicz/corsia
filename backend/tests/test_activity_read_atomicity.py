"""A rejected batch must not mark an earlier document read."""

from doc_feedback_helpers import add_comment, seed_document, seed_thread
from infra.database import get_db
from tests.test_activity_notifications import _client


def test_invalid_reply_in_later_chunk_leaves_earlier_document_unread(isolated_db, monkeypatch):
    from doceditor import comment_thread_views

    monkeypatch.setattr(comment_thread_views, '_READ_CHUNK', 1)
    db = get_db()
    try:
        seed_thread(db, 'owner')
        docs = []
        for _ in range(2):
            doc = seed_document(db, 'owner')
            root, _ = add_comment(db, doc, author_kind='user', author_id=None)
            reply, _ = add_comment(db, doc, author_id='owner', parent_id=root['id'])
            docs.append((doc, reply['id']))
        client = _client(monkeypatch)
        before = client.get('/api/attention/queue?view=activity').json()['notifications']['count']
        assert before == 2
        response = client.post('/api/attention/notifications/read', json={'documents': [
            {'artifact_id': docs[0][0], 'through_reply_id': docs[0][1]},
            {'artifact_id': docs[1][0], 'through_reply_id': docs[0][1]},
        ]})
        assert response.status_code == 404
        assert client.get('/api/attention/queue?view=activity').json()['notifications']['count'] == before
    finally:
        db.close()
