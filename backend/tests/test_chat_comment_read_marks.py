"""Opening a chat comment thread reads its answers one by one: each read marks only the answer it names, through the row it was sent on."""

import json

from infra.database import get_db
from infra.chat_comment_index import attach_comments, place_comments
from tests.test_chat_comment_index import _seed_root_and_reply


def _unread_count(db):
    from doceditor.reply_attention import chat_reply_items
    items = chat_reply_items(db, 'person', 'thread')
    return items[0]['detail']['unread_count'] if items else 0


def test_viewing_one_threads_answer_leaves_another_threads_answer_from_the_same_message_unread(isolated_db):
    from api.chat_comment_views import _stamp

    db = get_db()
    try:
        _seed_root_and_reply(db)
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) VALUES ('answer','thread','assistant',?,'v',4)",
                   ('[c1.1] first\n\n[c1.2] second',))
        db.execute("INSERT INTO chat_comment_answers VALUES ('thread','user','c1.1','answer','v')")
        db.execute("INSERT INTO chat_comment_answers VALUES ('thread','user','c1.2','answer','v')")
        db.commit()
        assert _unread_count(db) == 2
    finally:
        db.close()
    assert _stamp('person', 'thread', 'user', 'c1.1', 'answer')
    db = get_db()
    try:
        assert _unread_count(db) == 1
        page = [{'id': 'target', 'role': 'assistant'}]
        attach_comments(db, 'thread', 'person', page)
        unread = {thread['cid']: [a['unread'] for a in thread['answers'] if a['author_kind'] == 'agent'] for thread in page[0]['chat_comments']}
        assert unread == {'c1.1': [False], 'c1.2': [True]}
    finally:
        db.close()


def test_viewing_a_shared_answer_leaves_an_earlier_answer_to_the_reply_unread(isolated_db):
    from api.chat_comment_views import _stamp

    db = get_db()
    try:
        _seed_root_and_reply(db)
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) VALUES ('early','thread','assistant','[c3.1] soon','v',4)")
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) VALUES ('shared','thread','assistant','[c1.1][c3.1] both','w',5)")
        db.execute("INSERT INTO chat_comment_answers VALUES ('thread','reply','c3.1','early','v')")
        db.execute("INSERT INTO chat_comment_answers VALUES ('thread','user','c1.1','shared','w')")
        db.execute("INSERT INTO chat_comment_answers VALUES ('thread','reply','c3.1','shared','w')")
        db.commit()
        assert _unread_count(db) == 2
    finally:
        db.close()
    assert _stamp('person', 'thread', 'user', 'c1.1', 'shared')
    db = get_db()
    try:
        assert _unread_count(db) == 1
        page = [{'id': 'target', 'role': 'assistant'}]
        attach_comments(db, 'thread', 'person', page)
        answers = [a for a in page[0]['chat_comments'][0]['answers'] if a['author_kind'] == 'agent']
        assert [(a['assistant_message_id'], a['unread']) for a in answers] == [('early', True), ('shared', False)]
    finally:
        db.close()


def test_opening_a_thread_with_several_turns_reads_every_answer_through_the_row_it_was_sent_on(isolated_db):
    # The person replied twice; the agent's three answers sit on three rows, one of them answering both replies at once.
    # Opening the thread stamps each answer with the row `attach_comments` sent it on, as the client does.
    from api.chat_comment_views import _stamp
    from doceditor.reply_attention import chat_reply_items

    db = get_db()
    try:
        _seed_root_and_reply(db)
        second = {'reply_context': {'comments': [{'cid': 'c5.1', 'seq': 1, 'note': 'and then?', 'root_user_message_id': 'user', 'root_cid': 'c1.1'}]}}
        db.execute("INSERT INTO messages(id,thread_id,role,content,metadata,created_at,seq) VALUES ('reply2','thread','user','',?,'w',6)", (json.dumps(second),))
        place_comments(db, 'thread', 'reply2', second, 'w')
        for msg_id, content, at, seq in (('a1', '[c1.1] first', 'v', 4), ('a2', '[c3.1] second', 'v2', 5), ('a3', '[c3.1][c5.1] third', 'x', 7)):
            db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) VALUES (?,'thread','assistant',?,?,?)", (msg_id, content, at, seq))
        for row in (('user', 'c1.1', 'a1', 'v'), ('reply', 'c3.1', 'a2', 'v2'), ('reply', 'c3.1', 'a3', 'x'), ('reply2', 'c5.1', 'a3', 'x')):
            db.execute("INSERT INTO chat_comment_answers VALUES ('thread',?,?,?,?)", row)
        db.commit()
        page = [{'id': 'target', 'role': 'assistant'}]
        attach_comments(db, 'thread', 'person', page)
        thread = next(t for t in page[0]['chat_comments'] if t['cid'] == 'c1.1')
        agent = [a for a in thread['answers'] if a['author_kind'] == 'agent']
        assert [(a['assistant_message_id'], a['unread']) for a in agent] == [('a1', True), ('a2', True), ('a3', True)]
        assert _unread_count(db) == 3
    finally:
        db.close()
    for answer in agent:
        assert _stamp('person', 'thread', answer['user_message_id'], answer['cid'], answer['assistant_message_id'])
    db = get_db()
    try:
        assert chat_reply_items(db, 'person', 'thread') == []
        page = [{'id': 'target', 'role': 'assistant'}]
        attach_comments(db, 'thread', 'person', page)
        thread = next(t for t in page[0]['chat_comments'] if t['cid'] == 'c1.1')
        assert [a['unread'] for a in thread['answers'] if a['author_kind'] == 'agent'] == [False, False, False]
    finally:
        db.close()
