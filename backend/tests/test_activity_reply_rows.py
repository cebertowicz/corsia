import json
import sqlite3

from infra.schema import SCHEMA_SQL
from doceditor.activity_reply_rows import reply_rows


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
    rows = reply_rows(db, 'u1', reviewed_artifact_ids=set())
    assert len(rows) == 1
    assert rows[0]['detail'] == {'artifact_id':'d1','root_id':'rootd1','reply_id':'d13'}
    assert rows[0]['open'] == {'artifact_id':'d1','comment_id':'rootd1'}
    # The row names who answered which of the person's comments, never the file name.
    assert rows[0]['title'] == 'One replied to your comment “question”'
    db.execute("UPDATE artifact_registrations SET relationship_state='subscriber-recent' WHERE id='rd1'")
    db.execute("INSERT INTO artifact_registration_events(id,artifact_id,event_type,old_owner_thread_id,created_at) VALUES ('ev','d1','ownership_released','one','4')")
    assert reply_rows(db,'u1', reviewed_artifact_ids=set()) == []
    db.execute("UPDATE artifact_registrations SET relationship_state='owner-active' WHERE id='rd1'")
    db.execute("INSERT INTO doc_comment_thread_views(user_id,root_id,seen_at,seen_reply_id) VALUES ('u1','rootd1','3','d13')")
    assert reply_rows(db,'u1', reviewed_artifact_ids=set()) == []


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
    rows = reply_rows(db,'u1', reviewed_artifact_ids=set())
    assert len(rows) == 1
    assert rows[0]['open'] == {'thread_id':'t','message_id':'target'}
    assert rows[0]['title'] == 'Thread replied to your comment “why?”'
    assert {k: rows[0]['detail'][k] for k in ('thread_id','message_id','cid','assistant_message_id')} == {'thread_id':'t','message_id':'user','cid':'c1.1','assistant_message_id':'answer'}
    assert reply_rows(db,'u2', reviewed_artifact_ids=set()) == []
    db.execute("INSERT INTO chat_comment_views(user_id,user_message_id,cid,seen_at) VALUES ('u1','user','c1.1','2')")
    assert reply_rows(db,'u1', reviewed_artifact_ids=set()) == []


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


def test_one_row_per_root_with_the_newest_reply():
    db = _doc_db()
    _doc(db, 'd1', 'one', replies=('2','4','3'))
    rows = reply_rows(db, 'u1', reviewed_artifact_ids=set())
    assert [r['detail']['reply_id'] for r in rows] == ['d14']


def test_reviewed_document_has_no_reply_row():
    db = _doc_db()
    _doc(db, 'd1', 'one')
    _doc(db, 'd2', 'one')
    rows = reply_rows(db, 'u1', reviewed_artifact_ids={'d1'})
    assert [r['detail']['artifact_id'] for r in rows] == ['d2']


def test_released_owner_has_no_reply_row():
    db = _doc_db()
    _doc(db, 'd1', 'one')
    db.execute("UPDATE artifact_registrations SET relationship_state='subscriber-recent' WHERE id='rd1'")
    assert reply_rows(db, 'u1', reviewed_artifact_ids=set()) == []


def test_shared_owned_documents_show_to_any_account_whose_agent_replied():
    # Pins today's behaviour (#3225): person comment authors are stored as a shared 'ui'
    # author, so a 'ui'- or 'project:'-owned document's replies show to every account
    # whose own agent answered there.
    db = _doc_db()
    _doc(db, 'ui-doc', 'ui', author='two')
    _doc(db, 'project-doc', 'project:p1', author='two')
    assert {r['detail']['artifact_id'] for r in reply_rows(db, 'u2', reviewed_artifact_ids=set())} == {'ui-doc', 'project-doc'}
    assert reply_rows(db, 'u1', reviewed_artifact_ids=set()) == []


def test_long_comment_keeps_its_closing_quote_and_untitled_agent_reads_an_agent():
    db = _doc_db(owner_thread_title='')
    _doc(db, 'd1', 'one', root_body='word ' * 80)
    title = reply_rows(db, 'u1', reviewed_artifact_ids=set())[0]['title']
    assert title.startswith('An agent replied to your comment “word')
    assert title.endswith('…”')
    assert len(title) <= 160
