"""Activity's document-reply read covers only the documents the person commented in, with dev's rows.

``_unscoped_rows`` is the read as it stood before the scope (one query over every comment), kept
here as the reference: the scoped read must return exactly its rows.
"""
import sqlite3

from doceditor import activity_reply_rows
from doceditor.activity_reply_rows import _document_row, document_rows
from doceditor.feedback_state import _SCOPE_CHUNK, unread_comment_rows
from infra.auth import may_access_artifact
from infra.schema import SCHEMA_SQL
from plans.public import thread_sources


def _unscoped_replies(db, user_id: str):
    unread_sql, unread_params = unread_comment_rows(user_id, None)
    sql = f"""
    WITH owner_candidates AS (
      SELECT r.artifact_id, r.parent_thread_id,
             ROW_NUMBER() OVER (PARTITION BY r.artifact_id ORDER BY
               CASE r.relationship_state WHEN 'owner-pinned' THEN 0 ELSE 1 END,
               r.updated_at DESC, r.id ASC) AS pos
      FROM artifact_registrations r
      WHERE r.relationship_state IN ('owner-pinned','owner-active')
    ), unread AS (
      SELECT root.id AS root_id, item.id AS reply_id, item.artifact_id,
             item.created_at, item.author_thread_id,
             (item.parent_id IS NOT NULL AND item.author_kind='agent'
              AND item.artifact_id=root.artifact_id
              AND EXISTS (SELECT 1 FROM threads author WHERE author.id=item.author_thread_id AND author.user_id=?)
              AND (root.author_kind='user'
                   OR EXISTS (SELECT 1 FROM doc_comments person WHERE person.parent_id=root.id
                              AND person.author_kind='user' AND person.artifact_id=root.artifact_id))
             ) AS qualifies
      {unread_sql}
    ), counted AS (
      SELECT u.*,
             FIRST_VALUE(u.reply_id) OVER (PARTITION BY u.artifact_id
               ORDER BY u.created_at DESC, u.reply_id DESC) AS latest_unread_id
      FROM unread u
    )
    SELECT c.root_id, c.reply_id, c.artifact_id, c.created_at, c.author_thread_id, c.latest_unread_id,
           i.title AS artifact_title, i.path AS artifact_path, own.parent_thread_id AS owner_thread
    FROM counted c
    JOIN artifact_identities i ON i.id=c.artifact_id AND i.state='active'
    JOIN owner_candidates own ON own.artifact_id=i.id AND own.pos=1
    WHERE c.qualifies AND own.parent_thread_id IS NOT NULL AND own.parent_thread_id<>''
    ORDER BY c.artifact_id, c.created_at DESC, c.reply_id DESC
    """
    return db.execute(sql, (user_id, *unread_params)).fetchall()


def _unscoped_rows(db, user_id: str) -> list[dict]:
    replies = [row for row in _unscoped_replies(db, user_id)
               if may_access_artifact(row['owner_thread'], user_id, db=db)]
    sources = thread_sources(db, {str(r['author_thread_id']) for r in replies})
    groups: dict[str, list] = {}
    for row in replies:
        groups.setdefault(row['artifact_id'], []).append(row)
    return [_document_row(artifact, rows, sources) for artifact, rows in groups.items()]


def _db():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.executemany("INSERT INTO threads(id,user_id,title,created_at,updated_at) VALUES (?,?,?,'0','0')",
                   [('one', 'u1', 'One'), ('two', 'u1', 'Two'), ('other', 'u2', 'Other')])
    return db


def _document(db, doc, owner='one', state='owner-active'):
    db.execute("INSERT INTO artifact_identities(id,integration,type,external_id,title,created_at,updated_at) "
               "VALUES (?,'local','document',?,?,'0','0')", (doc, doc, f'Title {doc}'))
    db.execute("INSERT INTO artifact_registrations(id,artifact_id,parent_thread_id,relationship_state,created_at,updated_at) "
               "VALUES (?,?,?,?,'0','0')", ('r' + doc, doc, owner, state))


def _comment(db, cid, doc, at, *, parent=None, kind='agent', thread='one', tagged=None):
    db.execute("INSERT INTO doc_comments(id,artifact_id,parent_id,author,author_kind,author_thread_id,body,"
               "tagged_user_id,created_at,updated_at) VALUES (?,?,?,'x',?,?,'b',?,?,?)",
               (cid, doc, parent, kind, None if kind == 'user' else thread, tagged, at, at))


def _directed(db, cid, doc):
    db.execute("INSERT INTO doc_feedback_batches(id,artifact_id,author_kind,author_id,recipient_id,state,opened_at,"
               "last_item_at,closes_at,created_at,updated_at) VALUES (?,?,'agent','one','ui','delivered','0','0','0','0','0')",
               ('b' + cid, doc))
    db.execute("INSERT INTO doc_feedback_batch_items(batch_id,comment_id) VALUES (?,?)", ('b' + cid, cid))


def _cases(db):
    # No comment by the person: agent replies, a tagged item and a directed item stay out.
    _document(db, 'agents-only')
    _comment(db, 'a-root', 'agents-only', '01')
    _comment(db, 'a-reply', 'agents-only', '02', parent='a-root', thread='two')
    _comment(db, 'a-tagged', 'agents-only', '03', tagged='u1')
    _comment(db, 'a-directed', 'agents-only', '04')
    _directed(db, 'a-directed', 'agents-only')
    # The person commented elsewhere in the document; a tagged-only reply in an agent thread stays out.
    _document(db, 'tagged-only')
    _comment(db, 't-person', 'tagged-only', '01', kind='user')
    _comment(db, 't-root', 'tagged-only', '02')
    _comment(db, 't-reply', 'tagged-only', '03', parent='t-root', tagged='u1')
    # A released owner: the person's thread is answered, but the document has no current owner.
    _document(db, 'released', state='subscriber-recent')
    _comment(db, 'r-root', 'released', '01', kind='user')
    _comment(db, 'r-reply', 'released', '02', parent='r-root')
    # Qualifying replies (one under the person's root, one in an agent thread the person replied in)
    # beside newer unrelated unread items: the count is the qualifying two, latest is the newest of any kind.
    _document(db, 'mixed')
    _comment(db, 'm-root', 'mixed', '01', kind='user')
    _comment(db, 'm-reply', 'mixed', '02', parent='m-root')
    _comment(db, 'm-agent-root', 'mixed', '03', thread='two')
    _comment(db, 'm-person-reply', 'mixed', '04', parent='m-agent-root', kind='user')
    _comment(db, 'm-agent-reply', 'mixed', '05', parent='m-agent-root', thread='two')
    _comment(db, 'm-unrelated', 'mixed', '06', tagged='u1')
    _comment(db, 'm-directed', 'mixed', '07')
    _directed(db, 'm-directed', 'mixed')
    # More documents than one chunk, each with one answered comment by the person.
    for n in range(_SCOPE_CHUNK + 15):
        doc = f'many-{n:04}'
        _document(db, doc, owner='one' if n % 3 else 'two')
        _comment(db, f'{doc}-root', doc, '01', kind='user')
        _comment(db, f'{doc}-reply', doc, f'{2 + n % 5:02}', parent=f'{doc}-root')
    # Another account's agent answers in the person's thread: not this person's reply.
    _comment(db, 'm-other', 'mixed', '08', parent='m-root', thread='other')


def test_the_scoped_read_returns_the_unscoped_reads_rows():
    db = _db()
    db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)  # SQLite's oldest default, as the chunk is sized for
    _cases(db)
    rows = document_rows(db, 'u1')
    assert rows == _unscoped_rows(db, 'u1')
    by_doc = {row['detail']['artifact_id']: row for row in rows}
    assert not {'agents-only', 'tagged-only', 'released'} & set(by_doc)
    assert len(by_doc) == 1 + _SCOPE_CHUNK + 15
    mixed = by_doc['mixed']
    assert mixed['title'] == 'Title mixed'
    assert mixed['detail']['unread'] == 2
    assert mixed['detail']['latest_reply_id'] == 'm-other'  # newest unread item of any kind (#3358)
    assert mixed['detail']['threads'] == [{'root_id': 'm-agent-root', 'reply_id': 'm-agent-reply'},
                                          {'root_id': 'm-root', 'reply_id': 'm-reply'}]
    assert mixed['detail']['agents'] == ['Two', 'One']
    assert by_doc['many-0001']['detail']['agents'] == ['One']
    db.execute("INSERT INTO doc_comment_thread_views(user_id,root_id,seen_at,seen_reply_id) VALUES ('u1','m-root','08','m-other')")
    assert document_rows(db, 'u1') == _unscoped_rows(db, 'u1')
    assert document_rows(db, 'u2') == _unscoped_rows(db, 'u2') == []


def test_no_statement_the_read_runs_scans_every_comment_or_every_batch():
    db = _db()
    _cases(db)
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    document_rows(db, 'u1')
    db.set_trace_callback(None)
    plans = [' | '.join(row['detail'] for row in db.execute(f'EXPLAIN QUERY PLAN {sql}'))
             for sql in statements if 'doc_comments' in sql]
    assert plans
    for plan in plans:
        assert 'SCAN item' not in plan and 'SCAN doc_comments USING INDEX idx_doc_comments_artifact' not in plan, plan
        assert 'idx_doc_feedback_batches_recipient_open' not in plan, plan
    assert any('SEARCH batch_item USING INDEX idx_doc_feedback_batch_items_comment (comment_id=?)' in p for p in plans)


def test_reply_qualification_does_not_rescan_person_replies_per_unread_row():
    db = _db()
    _cases(db)
    statements = []
    db.set_trace_callback(statements.append)
    activity_reply_rows._replies(db, 'u1', ('mixed',))
    db.set_trace_callback(None)
    sql = next(s for s in statements if 'WITH owner_candidates' in s)
    plan = [row['detail'] for row in db.execute(f'EXPLAIN QUERY PLAN {sql}')]
    assert not any('SEARCH person ' in detail for detail in plan), plan
    assert any('MATERIALIZE person_threads' in detail for detail in plan), plan


def test_multiple_person_replies_do_not_duplicate_qualified_replies():
    db = _db()
    _cases(db)
    _comment(db, 'm-person-reply-2', 'mixed', '04', parent='m-agent-root', kind='user')
    rows = activity_reply_rows._replies(db, 'u1', ('mixed',))
    assert [row['reply_id'] for row in rows] == ['m-agent-reply', 'm-reply']
    assert {row['latest_unread_id'] for row in rows} == {'m-other'}
    assert [tuple(row) for row in rows] == [tuple(row) for row in _unscoped_replies(db, 'u1')
                                           if row['artifact_id'] == 'mixed']
    assert document_rows(db, 'u1') == _unscoped_rows(db, 'u1')


def test_cross_document_person_reply_does_not_qualify_a_thread():
    db = _db()
    _document(db, 'one-doc')
    _document(db, 'other-doc')
    _comment(db, 'root', 'one-doc', '01')
    _comment(db, 'answer', 'one-doc', '03', parent='root')
    _comment(db, 'person-elsewhere', 'other-doc', '02', parent='root', kind='user')
    _comment(db, 'scope', 'one-doc', '01', kind='user')
    rows = activity_reply_rows._replies(db, 'u1', ('one-doc', 'other-doc'))
    assert [tuple(row) for row in rows] == [tuple(row) for row in _unscoped_replies(db, 'u1')] == []


def test_the_person_documents_read_only_the_persons_comments():
    db = _db()
    _cases(db)
    assert activity_reply_rows._person_documents(db)[:3] == ['many-0000', 'many-0001', 'many-0002']
    assert set(activity_reply_rows._person_documents(db)) == {
        'tagged-only', 'released', 'mixed', *(f'many-{n:04}' for n in range(_SCOPE_CHUNK + 15))}
