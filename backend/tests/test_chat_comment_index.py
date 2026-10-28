"""Chat comments are located by their target without scanning the transcript."""

import json
import sqlite3

from fastapi.testclient import TestClient
from auth_fixtures import OWNER_TOKEN, OTHER_TOKEN, OWNER_USER
from doc_feedback_helpers import seed_thread
from infra.database import get_db
from infra.chat_comment_index import attach_comments, place_comments
from infra.migration_155_to_156_chat_comments import migrate_155_to_156


def test_chat_migration_follows_thread_confinement():
    from infra.database_migrations import MIGRATIONS
    from infra.migration_154_to_155_thread_confinement import migrate_154_to_155
    from infra.schema import SCHEMA_VERSION

    assert SCHEMA_VERSION == 161
    assert MIGRATIONS[154] is migrate_154_to_155
    assert MIGRATIONS[155] is migrate_155_to_156


def test_migration_accepts_a_partial_database_without_messages():
    with sqlite3.connect(':memory:') as db:
        migrate_155_to_156(db)
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chat_comments'").fetchone()


def test_places_each_comment_on_its_target_and_ignores_missing_targets(isolated_db):
    db = get_db()
    try:
        seed_thread(db, 'thread')
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) VALUES ('target','thread','assistant','text','t',1)")
        metadata = {'reply_context': {'comments': [
            {'cid': 'c1.1', 'seq': 1, 'note': 'a'},
            {'cid': 'c1.2', 'seq': 99, 'note': 'missing'},
        ]}}
        db.execute("INSERT INTO messages(id,thread_id,role,content,metadata,created_at,seq) VALUES ('user','thread','user','',?,'t',2)", (json.dumps(metadata),))
        place_comments(db, 'thread', 'user', metadata, 't')
        place_comments(db, 'thread', 'user', metadata, 't')
        rows = db.execute('SELECT target_message_id,user_message_id,cid FROM chat_comments').fetchall()
        assert [tuple(row) for row in rows] == [('target', 'user', 'c1.1')]
        db.execute('DROP TABLE chat_comments')
        migrate_155_to_156(db)
        rows = db.execute('SELECT target_message_id,user_message_id,cid FROM chat_comments').fetchall()
        assert [tuple(row) for row in rows] == [('target', 'user', 'c1.1')]
    finally:
        db.close()


def test_live_refresh_requires_thread_owner(isolated_db):
    from main import app
    db = get_db()
    try:
        seed_thread(db, 'thread')
        db.execute('UPDATE threads SET user_id=? WHERE id=?', (OWNER_USER, 'thread'))
        db.commit()
    finally:
        db.close()
    client = TestClient(app)
    url = '/api/threads/thread/chat-comment-threads'
    assert client.post(url, json={'message_ids': ['target']}, headers={'Authorization': f'Bearer {OTHER_TOKEN}'}).status_code == 404
    response = client.post(url, json={'message_ids': ['target']}, headers={'Authorization': f'Bearer {OWNER_TOKEN}'})
    assert response.status_code == 200
    assert response.json() == {'messages': [{'id': 'target', 'role': 'assistant'}]}


def test_attaches_answer_and_unread_state_to_loaded_target(isolated_db):
    db = get_db()
    try:
        seed_thread(db, 'thread')
        db.execute("UPDATE threads SET user_id='person' WHERE id='thread'")
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) VALUES ('target','thread','assistant','text','t',1)")
        comment = {'cid': 'c1.1', 'seq': 1, 'from_off': 0, 'to_off': 4, 'quote': 'text', 'note': 'why?'}
        metadata = {'reply_context': {'comments': [comment]}}
        db.execute("INSERT INTO messages(id,thread_id,role,content,metadata,created_at,seq) VALUES ('user','thread','user','',?,'t',2)", (json.dumps(metadata),))
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) VALUES ('answer','thread','assistant','[c1.1] because','u',3)")
        place_comments(db, 'thread', 'user', metadata, 't')
        db.execute("INSERT INTO chat_comment_answers VALUES ('thread','user','c1.1','answer','u')")
        page = [{'id': 'target', 'role': 'assistant'}]
        attach_comments(db, 'thread', 'person', page)
        thread = page[0]['chat_comments'][0]
        assert thread['note'] == 'why?'
        assert thread['answers'][0]['text'] == 'because'
        assert thread['answers'][0]['unread'] is True
        answer_page = [{'id': 'answer', 'role': 'assistant'}]
        attach_comments(db, 'thread', 'person', answer_page)
        assert answer_page[0]['chat_visible_content'] == ''
        assert answer_page[0]['chat_answer_cids'] == ['c1.1']
        from doceditor.reply_attention import chat_reply_items
        db.execute("UPDATE threads SET user_id='person' WHERE id='thread'")
        reply = chat_reply_items(db, 'person', 'thread')[0]['detail']
        assert reply['unread_count'] == 1
        assert reply['destination'] == {'pane': 'chat-replies', 'id': 'target', 'user_message_id': 'user', 'cid': 'c1.1', 'seq': 1}
        db.execute("INSERT INTO chat_comment_views VALUES ('person','user','c1.1','u')")
        assert chat_reply_items(db, 'person', 'thread') == []
        page = [{'id': 'target', 'role': 'assistant'}]
        attach_comments(db, 'thread', 'person', page)
        assert page[0]['chat_comments'][0]['answers'][0]['unread'] is False
    finally:
        db.close()


def test_page_and_scoped_attention_use_answer_indexes(isolated_db):
    from doceditor.reply_attention import chat_reply_items

    db = get_db()
    class Capture:
        def __init__(self, connection):
            self.connection = connection
            self.statements = []
        def execute(self, sql, params=()):
            self.statements.append((sql, params))
            return self.connection.execute(sql, params)
    try:
        captured = Capture(db)
        attach_comments(captured, 'thread', 'person', [{'id': 'target', 'role': 'assistant'}])
        sql, params = captured.statements[0]
        page_plan = [row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + sql, params)]
        assert any('idx_chat_comment_answers_comment' in step for step in page_plan), page_plan
        assert not any('SCAN answer' in step or 'SCAN a' in step for step in page_plan), page_plan
        captured.statements.clear()
        chat_reply_items(captured, 'person', 'thread')
        sql, params = captured.statements[0]
        attention_plan = [row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + sql, params)]
        assert any('idx_chat_comment_answers_thread_time' in step and 'thread_id=?' in step for step in attention_plan), attention_plan
    finally:
        db.close()


def test_unread_projections_agree_for_missing_rows_and_new_answers(isolated_db):
    from doceditor.reply_attention import chat_reply_items

    db = get_db()
    try:
        seed_thread(db, 'thread')
        db.execute("UPDATE threads SET user_id='person' WHERE id='thread'")
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) VALUES ('target','thread','assistant','text','t',1)")
        comment = {'cid': 'c1.1', 'seq': 1, 'from_off': 0, 'to_off': 4, 'quote': 'text', 'note': 'why?'}
        metadata = {'reply_context': {'comments': [comment]}}
        db.execute("INSERT INTO messages(id,thread_id,role,content,metadata,created_at,seq) VALUES ('user','thread','user','',?,'t',2)", (json.dumps(metadata),))
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) VALUES ('a1','thread','assistant','[c1.1] first','u',3)")
        db.execute("INSERT INTO chat_comment_answers VALUES ('thread','user','c1.1','a1','u')")
        assert chat_reply_items(db, 'person', 'thread') == []
        place_comments(db, 'thread', 'user', metadata, 't')
        db.execute("INSERT INTO chat_comment_views VALUES ('person','user','c1.1','u')")
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) VALUES ('a2','thread','assistant','[c1.1] again','v',4)")
        db.execute("INSERT INTO chat_comment_answers VALUES ('thread','user','c1.1','a2','v')")
        assert chat_reply_items(db, 'person', 'thread')[0]['detail']['unread_count'] == 1
        page = [{'id': 'target', 'role': 'assistant'}]
        attach_comments(db, 'thread', 'person', page)
        assert [answer['unread'] for answer in page[0]['chat_comments'][0]['answers']] == [False, True]
        db.execute("DELETE FROM messages WHERE id='target'")
        assert chat_reply_items(db, 'person', 'thread') == []
        page = [{'id': 'target', 'role': 'assistant'}]
        attach_comments(db, 'thread', 'person', page)
        assert not any(answer['unread'] for answer in page[0].get('chat_comments', [])[0]['answers'])
    finally:
        db.close()
