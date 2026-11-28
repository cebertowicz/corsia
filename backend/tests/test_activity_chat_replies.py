"""Activity chat replies share chat's own unread projection and read marks."""

import pytest

from doceditor.activity_chat_reply_rows import chat_detail, chat_rows, mark_chat_read
from doceditor.reply_attention import chat_reply_items
from infra.database import get_db
from tests.test_chat_comment_index import _seed_root_and_reply


def test_chat_row_detail_and_read_through_existing_state(isolated_db):
    db = get_db()
    try:
        _seed_root_and_reply(db)
        db.execute("UPDATE threads SET title='' WHERE id='thread'")
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) VALUES ('answer','thread','assistant','[c1.1] because','v',4)")
        db.execute("INSERT INTO chat_comment_answers VALUES ('thread','user','c1.1','answer','v')")
        [row] = chat_rows(db, 'person')
        assert (row['title'], row['detail']['unread'], row['detail']['through']) == ('New Chat', 1, 'v')
        assert chat_rows(db, 'other') == []
        [message] = chat_detail(db, 'person', 'thread')
        [comment] = message['chat_comments']
        answers = [answer['text'] for answer in comment['answers'] if answer['author_kind'] == 'agent']
        assert (message['content'], comment['note'], answers) == ('text', 'why?', ['because'])
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) VALUES ('later','thread','assistant','[c1.1] later','w',5)")
        db.execute("INSERT INTO chat_comment_answers VALUES ('thread','user','c1.1','later','w')")
        mark_chat_read(db, 'person', 'thread', row['detail']['through'])
        assert chat_rows(db, 'person')[0]['detail']['unread'] == 1
        assert chat_reply_items(db, 'person', 'thread')[0]['detail']['unread_count'] == 1
        mark_chat_read(db, 'person', 'thread', 'w')
        assert chat_rows(db, 'person') == []
        assert chat_reply_items(db, 'person', 'thread') == []
    finally:
        db.close()


@pytest.mark.parametrize('state', ['archived_at', 'purged_at', 'lifecycle', 'project'])
def test_inactive_chat_is_not_listed_or_accessible(isolated_db, monkeypatch, state):
    from tests.test_activity_notifications import _client

    db = get_db()
    try:
        _seed_root_and_reply(db)
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) VALUES ('answer','thread','assistant','[c1.1] because','v',4)")
        db.execute("INSERT INTO chat_comment_answers VALUES ('thread','user','c1.1','answer','v')")
        if state == 'project':
            db.execute("INSERT INTO projects(id,name,status,created_at,updated_at) VALUES ('project','Project','archived','t','t')")
            db.execute("UPDATE threads SET project_id='project' WHERE id='thread'")
        elif state == 'lifecycle':
            db.execute("UPDATE threads SET lifecycle_state='archived' WHERE id='thread'")
        else:
            db.execute(f"UPDATE threads SET {state}='t' WHERE id='thread'")
        db.commit()
        assert chat_rows(db, 'person') == []
        client = _client(monkeypatch, 'person')
        notifications = client.get('/api/attention/queue?view=activity').json()['notifications']
        assert notifications['count'] == 0
        assert notifications['items'] == []
        assert client.get('/api/attention/notifications/chats/thread').status_code == 404
        response = client.post('/api/attention/notifications/read', json={
            'documents': [], 'chats': [{'thread_id': 'thread', 'through': 'v'}],
        })
        assert response.status_code == 404
    finally:
        db.close()


def test_activity_api_lists_detail_and_marks_chat_read(isolated_db, monkeypatch):
    from tests.test_activity_notifications import _client

    db = get_db()
    try:
        _seed_root_and_reply(db)
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) VALUES ('answer','thread','assistant','[c1.1] because','2026-11-25T12:00:00Z',4)")
        db.execute("INSERT INTO chat_comment_answers VALUES ('thread','user','c1.1','answer','2026-11-25T12:00:00Z')")
        db.commit()
        client = _client(monkeypatch, 'person')
        rows = client.get('/api/attention/queue?view=activity').json()['notifications']['items']
        [row] = [item for item in rows if item['key'] == 'chat-reply:thread']
        assert row['detail']['unread'] == 1
        response = client.get('/api/attention/notifications/chats/thread')
        assert response.status_code == 200
        answers = response.json()['messages'][0]['chat_comments'][0]['answers']
        assert [answer['text'] for answer in answers if answer['author_kind'] == 'agent'] == ['because']
        response = client.post('/api/attention/notifications/read', json={'documents': [], 'chats': [row['detail']]})
        assert response.status_code == 200
        assert chat_reply_items(db, 'person', 'thread') == []
        assert _client(monkeypatch, 'other').get('/api/attention/notifications/chats/thread').status_code == 404
    finally:
        db.close()
