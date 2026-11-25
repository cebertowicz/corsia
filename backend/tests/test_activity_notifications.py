"""Activity's Notifications list: one row per document, read up to what the person was shown."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from doc_feedback_helpers import add_comment, seed_document, seed_thread
from infra.database import get_db, write_transaction
from plans import person_comment_task


def _client(monkeypatch, user_id='owner-token') -> TestClient:
    from api import attention as api_attention
    from tests.test_attention_queue_api import _immediate

    monkeypatch.setattr(api_attention, 'run_in_db', _immediate)
    app = FastAPI()
    app.include_router(api_attention.router)
    app.dependency_overrides[api_attention.require_user] = lambda: {'id': user_id}
    app.dependency_overrides[api_attention.require_user_header_only] = lambda: {'id': user_id}
    return TestClient(app)


def _answered(db, owner='owner', replies=1):
    doc = seed_document(db, owner)
    root, _ = add_comment(db, doc, author_kind='user', author_id=None)
    out = [add_comment(db, doc, author_id=owner, parent_id=root['id'])[0] for _ in range(replies)]
    return doc, root, out


def test_queue_answers_both_lists_and_the_ranker_sees_tasks_only(isolated_db, monkeypatch):
    from api.attention import activity_queue
    db = get_db()
    try:
        seed_thread(db, 'owner')
        doc, _, replies = _answered(db, replies=2)
        body = _client(monkeypatch).get('/api/attention/queue?view=activity').json()
        assert body['notifications']['count'] == 1
        [row] = body['notifications']['items']
        assert (row['key'], row['detail']['unread'], row['detail']['latest_reply_id']) == (f'doc:{doc}', 2, replies[-1]['id'])
        assert all(item['kind'] != 'document' for item in body['items'])
        rows, _, _ = activity_queue(db, 'owner-token', view='activity')
        assert all(r['kind'] != 'document' for r in rows)
        # The default view carries no notifications block.
        assert 'notifications' not in _client(monkeypatch).get('/api/attention/queue').json()
    finally:
        db.close()


def test_notifications_are_newest_reply_first(isolated_db, monkeypatch):
    db = get_db()
    try:
        seed_thread(db, 'owner')
        first, _, _ = _answered(db)
        second, _, _ = _answered(db)
        _, _, _ = _answered(db, replies=0)
        add_comment(db, first, author_id='owner', parent_id=db.execute(
            "SELECT id FROM doc_comments WHERE artifact_id=? AND parent_id IS NULL", (first,)).fetchone()[0])
        items = _client(monkeypatch).get('/api/attention/queue?view=activity').json()['notifications']['items']
        assert [i['detail']['artifact_id'] for i in items] == [first, second]
    finally:
        db.close()


def test_read_stops_at_the_reply_shown_and_a_later_one_stays_unread(isolated_db, monkeypatch):
    db = get_db()
    try:
        seed_thread(db, 'owner')
        doc, root, [shown] = _answered(db)
        other, _ = add_comment(db, doc, author_kind='user', author_id=None)
        add_comment(db, doc, author_id='owner', parent_id=other['id'])
        through = db.execute("SELECT id FROM doc_comments WHERE parent_id=?", (other['id'],)).fetchone()[0]
        later, _ = add_comment(db, doc, author_id='owner', parent_id=root['id'])
        response = _client(monkeypatch).post('/api/attention/notifications/read',
                                             json={'documents': [{'artifact_id': doc, 'through_reply_id': through}]})
        assert response.status_code == 200
        [row] = _client(monkeypatch).get('/api/attention/queue?view=activity').json()['notifications']['items']
        assert row['detail']['unread'] == 1 and row['detail']['latest_reply_id'] == later['id']
        _client(monkeypatch).post('/api/attention/notifications/read',
                                  json={'documents': [{'artifact_id': doc, 'through_reply_id': later['id']}]})
        assert _client(monkeypatch).get('/api/attention/queue?view=activity').json()['notifications']['count'] == 0
    finally:
        db.close()


def test_mark_all_read_closes_open_comment_tasks_and_decides_nothing(isolated_db, monkeypatch):
    db = get_db()
    try:
        seed_thread(db, 'owner')
        docs = [_answered(db) for _ in range(3)]
        with write_transaction(db, scope='test_tasks') as connection:
            tasks = [person_comment_task.reconcile(connection, doc) for doc, _, _ in docs]
        assert all(tasks)
        body = {'documents': [{'artifact_id': doc, 'through_reply_id': replies[-1]['id']} for doc, _, replies in docs]}
        assert _client(monkeypatch).post('/api/attention/notifications/read', json=body).json() == {'read': 3}
        states = {r[0] for r in db.execute(
            "SELECT state FROM plan_nodes WHERE id IN (SELECT value FROM json_each(?))", (__import__('json').dumps(tasks),))}
        assert states == {'accepted'}
    finally:
        db.close()


def test_a_document_the_caller_cannot_reach_refuses_the_whole_call(isolated_db, monkeypatch):
    db = get_db()
    try:
        seed_thread(db, 'owner')
        mine, _, [reply] = _answered(db)
        db.execute("INSERT INTO threads(id,user_id,title,created_at,updated_at) VALUES ('stranger','other','S','t','t')")
        theirs, _, [their_reply] = _answered(db, owner='stranger')
        db.commit()
        body = {'documents': [{'artifact_id': mine, 'through_reply_id': reply['id']},
                              {'artifact_id': theirs, 'through_reply_id': their_reply['id']}]}
        response = _client(monkeypatch).post('/api/attention/notifications/read', json=body)
        assert response.status_code in (403, 404)
        assert _client(monkeypatch).get('/api/attention/queue?view=activity').json()['notifications']['count'] == 1
        # A reply from another document is refused too.
        body = {'documents': [{'artifact_id': mine, 'through_reply_id': their_reply['id']}]}
        assert _client(monkeypatch).post('/api/attention/notifications/read', json=body).status_code == 404
    finally:
        db.close()
