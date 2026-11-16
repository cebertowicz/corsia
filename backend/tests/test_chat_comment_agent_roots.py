"""Comments an agent leaves on its own chat messages: storage, unread, attach, reply admission, stamps, delete.

Nothing writes an agent root yet, so every test seeds the rows directly.
"""

import json
import sqlite3

import pytest

from auth_fixtures import OWNER_USER
from doc_feedback_helpers import seed_thread
from infra.chat_comment_answers import delete_thread_chat_comment_index_rows
from infra.chat_comment_ids import commentable_role_sql, is_commentable_role
from infra.chat_comment_index import attach_comments, place_comments
from infra.database import get_db, initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_196_to_197_agent_comment_roots import migrate_196_to_197
from infra.reply_context import validate_reply_context
from infra.schema import SCHEMA_VERSION

ROOT = 'acr_0001'
T0, T1, T2 = '2026-01-01T00:00:00', '2026-01-02T00:00:00', '2026-01-03T00:00:00'


def _seed(db):
    seed_thread(db, 'thread')
    db.execute('UPDATE threads SET user_id=? WHERE id=?', (OWNER_USER, 'thread'))
    db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) "
               "VALUES ('target','thread','assistant','Ship it on Friday.','2026-01-01',1)")
    db.commit()


def _root(db, root_id=ROOT, cid='a1.1', target='target', updated=T0, note='Is Friday firm?'):
    db.execute("INSERT INTO chat_comment_agent_roots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (root_id, 'thread', cid, target, 'Friday', 11, 17, 'Ship it on ', '.', note, T0, updated))
    db.execute("INSERT INTO chat_comments(thread_id,target_message_id,user_message_id,cid,created_at) "
               "VALUES ('thread',?,?,?,?)", (target, root_id, cid, T0))
    db.commit()


def _replace(db, message_id='target'):
    db.execute("INSERT INTO message_update_state(message_id,thread_id,posted_at,update_kind,presence_at_post,"
               "seen_at,seen_reason,replaced_by,replaced_at,replace_reason) "
               "VALUES (?,?,?,?,?,?,?,?,?,?)",
               (message_id, 'thread', T0, 'post_update', 'here', T0, 'unknown', 'newer', T1, 'updates'))
    db.commit()


def _items(db):
    from doceditor.reply_attention import chat_reply_items
    return chat_reply_items(db, OWNER_USER, 'thread')


def _view(db, seen_at, root_id=ROOT, cid='a1.1'):
    db.execute("INSERT OR REPLACE INTO chat_comment_views VALUES (?,?,?,?)", (OWNER_USER, root_id, cid, seen_at))
    db.commit()


def _page(db):
    page = [{'id': 'target', 'role': 'assistant'}]
    attach_comments(db, 'thread', OWNER_USER, page)
    return page[0]


# --- storage and migration --------------------------------------------------


def test_migration_creates_the_table_and_index_and_is_idempotent():
    db = sqlite3.connect(':memory:')
    initialize_schema(db)
    db.execute('DROP TABLE chat_comment_agent_roots')
    db.execute('PRAGMA user_version=196')
    db.commit()
    assert MIGRATIONS[196] is migrate_196_to_197
    assert migrate_to_current(db, SCHEMA_VERSION).from_version == 196
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE name LIKE '%agent_roots%' AND name NOT LIKE 'sqlite_%'")}
    assert names == {'chat_comment_agent_roots', 'idx_chat_comment_agent_roots_target'}
    migrate_196_to_197(db)
    assert migrate_to_current(db, SCHEMA_VERSION).migrated is False
    db.close()


def test_one_helper_decides_which_messages_take_a_comment():
    assert is_commentable_role('assistant') and not is_commentable_role('user')
    assert commentable_role_sql('t.role') == "t.role IN ('assistant')"


# --- unread -----------------------------------------------------------------


def test_an_unread_root_counts_drops_after_a_view_and_counts_again_after_a_replace(isolated_db):
    db = get_db()
    try:
        _seed(db)
        _root(db)
        items = _items(db)
        assert len(items) == 1
        detail = items[0]['detail']
        assert detail['unread_count'] == 1 and detail['newest_at'] == T0
        assert detail['destination'] == {'pane': 'chat-replies', 'id': 'target', 'user_message_id': ROOT,
                                         'cid': 'a1.1', 'seq': 1}
        _view(db, T0)
        assert _items(db) == []
        db.execute('UPDATE chat_comment_agent_roots SET note=?,updated_at=? WHERE root_id=?', ('Changed', T1, ROOT))
        assert _items(db)[0]['detail']['unread_count'] == 1
    finally:
        db.close()


def test_a_replaced_target_hides_its_root_and_its_answers(isolated_db):
    db = get_db()
    try:
        _seed(db)
        _root(db)
        # A person's comment with an unread agent answer, on the same target.
        metadata = {'reply_context': {'comments': [{'cid': 'c1.1', 'seq': 1, 'from_off': 0, 'to_off': 4,
                                                    'quote': 'Ship', 'note': 'why?'}]}}
        db.execute("INSERT INTO messages(id,thread_id,role,content,metadata,created_at,seq) "
                   "VALUES ('user','thread','user','',?,'t',2)", (json.dumps(metadata),))
        place_comments(db, 'thread', 'user', metadata, 't')
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) "
                   "VALUES ('answer','thread','assistant','[c1.1] because','u',3)")
        db.execute("INSERT INTO chat_comment_answers VALUES ('thread','user','c1.1','answer','u')")
        db.commit()
        assert _items(db)[0]['detail']['unread_count'] == 2
        _replace(db)
        assert _items(db) == []
    finally:
        db.close()


def test_roots_and_answers_are_one_union_across_the_thread(isolated_db):
    db = get_db()
    try:
        _seed(db)
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) "
                   "VALUES ('target2','thread','assistant','Second.','2026-01-01',2)")
        _root(db)
        _root(db, root_id='acr_0002', cid='a2.1', target='target2', updated=T2)
        detail = _items(db)[0]['detail']
        assert detail['unread_count'] == 2 and detail['newest_at'] == T2
        assert detail['destination']['user_message_id'] == ROOT  # the oldest unread comes first
    finally:
        db.close()


def test_another_persons_thread_shows_no_unread_root(isolated_db):
    from doceditor.reply_attention import chat_reply_items
    db = get_db()
    try:
        _seed(db)
        _root(db)
        assert chat_reply_items(db, 'someone-else', 'thread') == []
    finally:
        db.close()


# --- attach -----------------------------------------------------------------


def test_attach_returns_agent_roots_with_no_offsets_and_replies_under_them(isolated_db):
    db = get_db()
    try:
        _seed(db)
        _root(db)
        reply = {'reply_context': {'comments': [{
            'cid': 'c2.1', 'seq': 1, 'from_off': 11, 'to_off': 17, 'quote': 'Friday', 'anchor_prefix': '',
            'anchor_suffix': '', 'note': 'Yes, firm.', 'root_user_message_id': ROOT, 'root_cid': 'a1.1'}]}}
        db.execute("INSERT INTO messages(id,thread_id,role,content,metadata,created_at,seq) "
                   "VALUES ('reply','thread','user','',?,'t',2)", (json.dumps(reply),))
        place_comments(db, 'thread', 'reply', reply, T1)
        message = _page(db)
    finally:
        db.close()
    [thread] = message['chat_comments']
    assert thread == {
        'author_kind': 'agent', 'user_message_id': ROOT, 'cid': 'a1.1', 'quote': 'Friday', 'note': 'Is Friday firm?',
        'created_at': T0, 'updated_at': T0, 'unread': True, 'delivery_state': 'delivered',
        'answers': thread['answers'],
    }
    assert not {'from_off', 'to_off', 'anchor_prefix', 'anchor_suffix'} & set(thread)
    assert [(a['author_kind'], a['text']) for a in thread['answers']] == [('user', 'Yes, firm.')]


def test_attach_marks_a_viewed_root_read_and_a_replaced_one_unread_again(isolated_db):
    db = get_db()
    try:
        _seed(db)
        _root(db)
        _view(db, T0)
        assert _page(db)['chat_comments'][0]['unread'] is False
        db.execute('UPDATE chat_comment_agent_roots SET updated_at=? WHERE root_id=?', (T1, ROOT))
        assert _page(db)['chat_comments'][0]['unread'] is True
    finally:
        db.close()


def test_attach_keeps_an_agent_root_after_the_anchored_comments(isolated_db):
    db = get_db()
    try:
        _seed(db)
        _root(db)
        metadata = {'reply_context': {'comments': [{'cid': 'c1.1', 'seq': 1, 'from_off': 0, 'to_off': 4,
                                                    'quote': 'Ship', 'note': 'why?'}]}}
        db.execute("INSERT INTO messages(id,thread_id,role,content,metadata,created_at,seq) "
                   "VALUES ('user','thread','user','',?,'t',2)", (json.dumps(metadata),))
        place_comments(db, 'thread', 'user', metadata, 't')
        assert [c['cid'] for c in _page(db)['chat_comments']] == ['c1.1', 'a1.1']
    finally:
        db.close()


# --- reply admission --------------------------------------------------------


def _reply(**changes):
    return {'id': 'key', 'note': 'Yes, firm.', 'root_user_message_id': ROOT, 'root_cid': 'a1.1', **changes}


def _admit(db, item):
    return validate_reply_context(db, 'thread', {'comments': [item]})


def test_a_reply_naming_an_agent_root_by_id_and_cid_takes_its_quote_and_anchors(isolated_db):
    db = get_db()
    try:
        _seed(db)
        _root(db)
        stored = _admit(db, _reply())
    finally:
        db.close()
    [comment] = stored['comments']
    assert {k: comment[k] for k in ('seq', 'from_off', 'to_off', 'quote', 'anchor_prefix', 'anchor_suffix', 'note',
                                    'root_user_message_id', 'root_cid')} == {
        'seq': 1, 'from_off': 11, 'to_off': 17, 'quote': 'Friday', 'anchor_prefix': 'Ship it on ',
        'anchor_suffix': '.', 'note': 'Yes, firm.', 'root_user_message_id': ROOT, 'root_cid': 'a1.1'}


def test_a_reply_cannot_override_the_root_row_it_names(isolated_db):
    db = get_db()
    try:
        _seed(db)
        _root(db)
        [comment] = _admit(db, _reply(quote='forged', seq=9, from_off=0, to_off=1))['comments']
    finally:
        db.close()
    assert (comment['quote'], comment['seq'], comment['from_off']) == ('Friday', 1, 11)


@pytest.mark.parametrize('changes', [
    {'root_user_message_id': 'acr_missing'}, {'root_cid': 'a9.9'}, {'root_cid': None}, {'root_cid': ''},
])
def test_a_reply_to_an_unknown_agent_root_is_refused(isolated_db, changes):
    db = get_db()
    try:
        _seed(db)
        _root(db)
        assert _admit(db, _reply(**changes)) is None
    finally:
        db.close()


def test_a_reply_to_a_root_in_another_thread_is_refused(isolated_db):
    db = get_db()
    try:
        _seed(db)
        _root(db)
        seed_thread(db, 'other')
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) "
                   "VALUES ('o1','other','assistant','Ship it on Friday.','t',1)")
        assert validate_reply_context(db, 'other', {'comments': [_reply()]}) is None
    finally:
        db.close()


def test_a_reply_to_a_root_whose_target_was_replaced_is_refused(isolated_db):
    db = get_db()
    try:
        _seed(db)
        _root(db)
        _replace(db)
        assert _admit(db, _reply()) is None
    finally:
        db.close()


def test_a_person_root_still_needs_its_own_five_fields(isolated_db):
    db = get_db()
    try:
        _seed(db)
        item = {'id': 'key', 'seq': 1, 'from_off': 0, 'to_off': 4, 'quote': 'Ship', 'anchor_prefix': '',
                'anchor_suffix': '', 'note': 'n', 'root_user_message_id': 'user', 'root_cid': 'c1.1'}
        assert _admit(db, item) is None
    finally:
        db.close()


# --- stamp ------------------------------------------------------------------


def _stamp(root_id=ROOT, cid='a1.1', shown=T0, assistant=None, user=OWNER_USER):
    from api.chat_comment_views import _stamp as stamp
    return stamp(user, 'thread', root_id, cid, assistant, shown)


def _seen(db):
    row = db.execute("SELECT seen_at FROM chat_comment_views WHERE user_message_id=?", (ROOT,)).fetchone()
    return row[0] if row else None


def test_stamping_a_root_needs_no_assistant_message_and_clears_it(isolated_db):
    db = get_db()
    try:
        _seed(db)
        _root(db)
        assert _stamp(shown=T0, assistant=None)
        assert _seen(db) == T0
        assert _items(db) == []
    finally:
        db.close()


def test_a_root_replaced_after_it_was_shown_stays_unread(isolated_db):
    db = get_db()
    try:
        _seed(db)
        _root(db)
        db.execute('UPDATE chat_comment_agent_roots SET note=?,updated_at=? WHERE root_id=?', ('Changed', T2, ROOT))
        db.commit()
        assert _stamp(shown=T0)
        assert _seen(db) == T0
        assert _items(db)[0]['detail']['unread_count'] == 1
        assert _page(db)['chat_comments'][0]['unread'] is True
    finally:
        db.close()


def test_a_stamp_never_runs_past_the_root_or_back_over_a_later_view(isolated_db):
    db = get_db()
    try:
        _seed(db)
        _root(db, updated=T1)
        assert _stamp(shown='2030-01-01T00:00:00')  # a time after the root: stamped no further than the root
        assert _seen(db) == T1
        _view(db, T2)
        assert _stamp(shown=T0)  # an older stamp never moves a view back
        assert _seen(db) == T2
    finally:
        db.close()


def test_a_stamp_is_refused_for_an_unknown_root_a_missing_time_or_another_person(isolated_db):
    db = get_db()
    try:
        _seed(db)
        _root(db)
        assert not _stamp(root_id='acr_missing')
        assert not _stamp(cid='a9.9')
        assert not _stamp(shown=None)
        assert not _stamp(user='someone-else')
        assert _seen(db) is None
    finally:
        db.close()


def test_a_person_comment_stamp_still_needs_its_answer(isolated_db):
    db = get_db()
    try:
        _seed(db)
        assert not _stamp(root_id='user', cid='c1.1', assistant=None)
    finally:
        db.close()


# --- delete -----------------------------------------------------------------


def test_thread_delete_removes_roots_their_views_and_comment_rows(isolated_db):
    db = get_db()
    try:
        _seed(db)
        _root(db)
        _view(db, T0)
        seed_thread(db, 'keep')
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) "
                   "VALUES ('k1','keep','assistant','x','t',1)")
        db.execute("INSERT INTO chat_comment_agent_roots VALUES ('acr_keep','keep','a1.1','k1','x',0,1,'','','n',?,?)",
                   (T0, T0))
        db.execute("INSERT INTO chat_comment_views VALUES (?,?,?,?)", (OWNER_USER, 'acr_keep', 'a1.1', T0))
        delete_thread_chat_comment_index_rows(db, 'thread')
        db.commit()
        for table, column in (('chat_comment_agent_roots', 'root_id'), ('chat_comment_views', 'user_message_id'),
                              ('chat_comments', 'user_message_id')):
            assert [r[0] for r in db.execute(f'SELECT {column} FROM {table}')] == (
                ['acr_keep'] if table != 'chat_comments' else []), table
    finally:
        db.close()
