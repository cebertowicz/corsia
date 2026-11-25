import json
import os
import sqlite3

from infra.schema import SCHEMA_SQL
from doceditor.activity_reply_rows import document_rows, resolve_row_titles


def test_document_unread_and_ownership():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.executemany("INSERT INTO threads(id,user_id,title,created_at,updated_at) VALUES (?,?,?,'0','0')", [('one','u1','One'),('two','u2','Two')])
    for doc, owner in [('d1','one'),('d2','two')]:
        db.execute("INSERT INTO artifact_identities(id,integration,type,external_id,title,created_at,updated_at) VALUES (?,'local','document',?,?,'0','0')", (doc,doc,doc))
        db.execute("INSERT INTO artifact_registrations(id,artifact_id,parent_thread_id,created_at,updated_at) VALUES (?,?,?,'0','0')", ('r'+doc,doc,owner))
        db.execute("INSERT INTO doc_comments(id,artifact_id,author,author_kind,body,created_at,updated_at) VALUES (?,?, 'person','user','question','1','1')", ('root'+doc,doc))
        for suffix in ('2','3'):
            db.execute("INSERT INTO doc_comments(id,artifact_id,parent_id,author,author_kind,author_thread_id,body,created_at,updated_at) VALUES (?,?,?,'agent','agent',?,'answer',?,?)", (doc+suffix,doc,'root'+doc,owner,suffix,suffix))
    # Owning the author thread does not grant access to another user's document.
    db.execute("INSERT INTO doc_comments(id,artifact_id,parent_id,author,author_kind,author_thread_id,body,created_at,updated_at) VALUES ('foreign-reply','d2','rootd2','agent','agent','one','answer','4','4')")
    # A bogus empty registration must never be interpreted as shared ownership.
    db.execute("INSERT INTO artifact_identities(id,integration,type,external_id,title,created_at,updated_at) VALUES ('orphan','local','document','orphan','Orphan','0','0')")
    db.execute("INSERT INTO artifact_registrations(id,artifact_id,parent_thread_id,created_at,updated_at) VALUES ('orphan-reg','orphan','','0','0')")
    db.execute("INSERT INTO doc_comments(id,artifact_id,author,author_kind,body,created_at,updated_at) VALUES ('oroot','orphan','person','user','q','1','1')")
    db.execute("INSERT INTO doc_comments(id,artifact_id,parent_id,author,author_kind,author_thread_id,body,created_at,updated_at) VALUES ('oreply','orphan','oroot','agent','agent','one','a','2','2')")
    rows = document_rows(db, 'u1')
    assert len(rows) == 1
    assert rows[0]['detail'] == {'artifact_id':'d1','unread':2,'latest_reply_id':'d13','agents':['One'],
                                 'threads':[{'root_id':'rootd1','reply_id':'d13'}]}
    assert rows[0]['open'] == {'artifact_id':'d1','comment_id':'rootd1'}
    # One row per document, named by the document; the key never changes as replies arrive.
    assert (rows[0]['kind'], rows[0]['key'], rows[0]['title']) == ('document', 'doc:d1', 'd1')
    db.execute("UPDATE artifact_registrations SET relationship_state='subscriber-recent' WHERE id='rd1'")
    db.execute("INSERT INTO artifact_registration_events(id,artifact_id,event_type,old_owner_thread_id,created_at) VALUES ('ev','d1','ownership_released','one','4')")
    assert document_rows(db, 'u1') == []
    db.execute("UPDATE artifact_registrations SET relationship_state='owner-active' WHERE id='rd1'")
    db.execute("INSERT INTO doc_comment_thread_views(user_id,root_id,seen_at,seen_reply_id) VALUES ('u1','rootd1','3','d13')")
    assert document_rows(db, 'u1') == []


def test_chat_unread_endpoint_and_ownership():
    from infra.chat_comment_index import place_comments
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute("INSERT INTO threads(id,user_id,title,created_at,updated_at) VALUES ('t','u1','Thread','0','0')")
    db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) VALUES ('target','t','assistant','hello','0',1)")
    meta = {'reply_context': {'comments': [{'cid':'c1.1', 'seq':1, 'note':'why?'}]}}
    # Input admission stores the same metadata on the person's message that it places comments from.
    db.execute("INSERT INTO messages(id,thread_id,role,content,metadata,created_at,seq) VALUES ('user','t','user','',?,'1',2)", (json.dumps(meta),))
    place_comments(db, 't', 'user', meta, '1')
    db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) VALUES ('answer','t','assistant','answer','2',3)")
    db.execute("INSERT INTO chat_comment_answers VALUES ('t','user','c1.1','answer','2')")
    _doc(db, 'd1', 't', author='t')
    rows = document_rows(db, 'u1')
    # The document reply remains, while the unread chat reply stays in chat.
    assert [row['key'] for row in rows] == ['doc:d1']
    assert document_rows(db, 'u2') == []
    db.execute("INSERT INTO chat_comment_views(user_id,user_message_id,cid,seen_at) VALUES ('u1','user','c1.1','2')")
    assert [row['key'] for row in document_rows(db, 'u1')] == ['doc:d1']


def _doc_db(owner_thread_title='One'):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.executemany("INSERT INTO threads(id,user_id,title,created_at,updated_at) VALUES (?,?,?,'0','0')", [('one','u1',owner_thread_title),('two','u2','Two')])
    return db


def _doc(db, doc, owner, root_body='question', replies=('2',), author='one'):
    db.execute("INSERT INTO artifact_identities(id,integration,type,external_id,title,created_at,updated_at) VALUES (?,'local','document',?,?,'0','0')", (doc,doc,doc))
    db.execute("INSERT INTO artifact_registrations(id,artifact_id,parent_thread_id,created_at,updated_at) VALUES (?,?,?,'0','0')", ('r'+doc,doc,owner))
    db.execute("INSERT INTO doc_comments(id,artifact_id,author,author_kind,body,created_at,updated_at) VALUES (?,?,'person','user',?,'1','1')", ('root'+doc,doc,root_body))
    for suffix in replies:
        db.execute("INSERT INTO doc_comments(id,artifact_id,parent_id,author,author_kind,author_thread_id,body,created_at,updated_at) VALUES (?,?,?,'agent','agent',?,'answer',?,?)", (doc+suffix,doc,'root'+doc,author,suffix,suffix))


def test_one_row_per_document_with_the_newest_reply():
    db = _doc_db()
    _doc(db, 'd1', 'one', replies=('2','4','3'))
    # A second thread on the same document, answered by another of the person's agents.
    db.execute("INSERT INTO threads(id,user_id,title,created_at,updated_at) VALUES ('three','u1','Three','0','0')")
    db.execute("INSERT INTO doc_comments(id,artifact_id,author,author_kind,body,created_at,updated_at) VALUES ('second','d1','person','user','q2','1','1')")
    db.execute("INSERT INTO doc_comments(id,artifact_id,parent_id,author,author_kind,author_thread_id,body,created_at,updated_at) VALUES ('d1-5','d1','second','agent','agent','three','a','5','5')")
    [row] = document_rows(db, 'u1')
    assert row['raised_at'] == '5' and row['detail']['latest_reply_id'] == 'd1-5'
    assert row['open'] == {'artifact_id': 'd1', 'comment_id': 'second'}
    assert row['detail']['unread'] == 4 and row['detail']['agents'] == ['Three', 'One']
    assert row['detail']['threads'] == [{'root_id': 'second', 'reply_id': 'd1-5'}, {'root_id': 'rootd1', 'reply_id': 'd14'}]


def test_count_is_the_replies_to_the_person_and_a_document_without_a_reply_has_no_row():
    db = _doc_db()
    _doc(db, 'd1', 'one')
    # An agent's top-level comment directed at the person is unread in the document, but it is not a
    # reply to the person's comment: the row counts the one reply only (the approved count).
    db.execute("INSERT INTO doc_comments(id,artifact_id,author,author_kind,author_thread_id,body,tagged_user_id,created_at,updated_at) VALUES ('top','d1','agent','agent','one','look','u1','3','3')")
    _doc(db, 'd2', 'one', replies=())
    db.execute("INSERT INTO doc_comments(id,artifact_id,author,author_kind,author_thread_id,body,tagged_user_id,created_at,updated_at) VALUES ('top2','d2','agent','agent','one','look','u1','3','3')")
    from doceditor.feedback_state import unread_item_ids
    assert unread_item_ids(db, 'u1', ['d1', 'd2']) == {'d1', 'd2'}  # the document surface still counts every unread item
    assert [(r['detail']['artifact_id'], r['detail']['unread']) for r in document_rows(db, 'u1')] == [('d1', 1)]


def test_released_owner_has_no_reply_row():
    db = _doc_db()
    _doc(db, 'd1', 'one')
    db.execute("UPDATE artifact_registrations SET relationship_state='subscriber-recent' WHERE id='rd1'")
    assert document_rows(db, 'u1') == []


def test_shared_owned_documents_show_to_any_account_whose_agent_replied():
    # Pins today's behaviour (#3225): person comment authors are stored as a shared 'ui'
    # author, so a 'ui'- or 'project:'-owned document's replies show to every account
    # whose own agent answered there.
    db = _doc_db()
    _doc(db, 'ui-doc', 'ui', author='two')
    _doc(db, 'project-doc', 'project:p1', author='two')
    assert {r['detail']['artifact_id'] for r in document_rows(db, 'u2')} == {'ui-doc', 'project-doc'}
    assert document_rows(db, 'u1') == []


def test_long_document_title_is_clipped_and_untitled_agent_reads_an_agent():
    db = _doc_db(owner_thread_title='')
    _doc(db, 'd1', 'one')
    db.execute("UPDATE artifact_identities SET title=? WHERE id='d1'", ('word ' * 80,))
    [row] = document_rows(db, 'u1')
    assert row['title'].startswith('word') and len(row['title']) <= 160
    assert row['detail']['agents'] == ['An agent']


def _file_doc(db, tmp_path, *, title: str, file_name: str, body: str) -> None:
    """One registered document, its registry title and path as a plain file registration stores them."""
    document = tmp_path / file_name
    document.write_text(body)
    db.execute("INSERT INTO artifact_identities(id,integration,type,external_id,title,path,created_at,updated_at) VALUES ('d1','local','document',?,?,?,'0','0')", (file_name, title, str(document)))
    db.execute("INSERT INTO artifact_registrations(id,artifact_id,parent_thread_id,created_at,updated_at) VALUES ('rd1','d1','one','0','0')")
    db.execute("INSERT INTO doc_comments(id,artifact_id,author,author_kind,body,created_at,updated_at) VALUES ('rootd1','d1','person','user','question','1','1')")
    db.execute("INSERT INTO doc_comments(id,artifact_id,parent_id,author,author_kind,author_thread_id,body,created_at,updated_at) VALUES ('d12','d1','rootd1','agent','agent','one','answer','2','2')")


def test_file_named_row_uses_the_documents_first_heading(tmp_path):
    db = _doc_db()
    _file_doc(db, tmp_path, title='Onboarding notes.md', file_name='Onboarding notes.md',
              body='# Onboarding: who owns which file\n\nWho owns what.\n')
    rows = document_rows(db, 'u1')
    resolve_row_titles(rows)  # what the queue does to the page it returns
    [row] = rows
    assert row['title'] == 'Onboarding: who owns which file'


def test_a_registry_title_that_is_not_the_file_name_beats_the_heading(tmp_path):
    db = _doc_db()
    _file_doc(db, tmp_path, title='Onboarding guide', file_name='onboarding-notes.md',
              body='# Written title\n\nText.\n')
    rows = document_rows(db, 'u1')
    resolve_row_titles(rows)
    [row] = rows
    assert row['title'] == 'Onboarding guide'


def test_the_file_name_is_kept_when_the_document_has_no_title(tmp_path):
    db = _doc_db()
    _file_doc(db, tmp_path, title='Onboarding notes.md', file_name='Onboarding notes.md',
              body='Just text, no heading.\n')
    rows = document_rows(db, 'u1')
    resolve_row_titles(rows)
    [row] = rows
    assert row['title'] == 'Onboarding notes.md'
    # A file that went away keeps its registry name too; nothing is invented.
    db.execute("UPDATE artifact_identities SET title='Gone.md', path=? WHERE id='d1'", (str(tmp_path / 'Gone.md'),))
    rows = document_rows(db, 'u1')
    resolve_row_titles(rows)
    [row] = rows
    assert row['title'] == 'Gone.md'


def test_the_title_read_happens_only_for_the_returned_page(tmp_path):
    """Building rows reads no file; only the page the queue resolves pays the head read."""
    db = _doc_db()
    _file_doc(db, tmp_path, title='Onboarding notes.md', file_name='Onboarding notes.md',
              body='# Onboarding: who owns which file\n\nWho owns what.\n')
    [row] = document_rows(db, 'u1')
    assert row['title'] == 'Onboarding notes.md'  # built, unread
    assert row['detail']['heading_path']
    resolve_row_titles([row])  # the page cut kept this row
    assert row['title'] == 'Onboarding: who owns which file'
    assert 'heading_path' not in row['detail']  # internal mark consumed, never sent on


def test_a_fifo_never_blocks_the_title_read(tmp_path):
    from doceditor.activity_reply_rows import _first_heading
    fifo = tmp_path / 'pipe.md'
    os.mkfifo(fifo)
    assert _first_heading(str(fifo)) == ''
