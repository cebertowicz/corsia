"""Activity's queue read: each rewritten statement returns what its old text returned, and
the read reaches its rows through indexes instead of stepping past the whole history.

The plan checks are the regression guard for cost: production databases never run
ANALYZE, so a read that loses its index scans every plan node or identity, and the
cost grows with all history rather than with what waits on the person.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from api.attention import activity_lists
from doceditor import activity_reply_rows, feedback_state
from doceditor.feedback_state import (USER_AUTHOR_ID, _eligible_top_level, _person_tagged, directed_sql,
                                      past_watermark_sql, unread_comment_rows)
from plans import attention_detail
from plans import public as plans_public
from registry import attention
from tests.activity_queue_scale_seed import USER, connect, seed

NEW_INDEXES = ('idx_plan_nodes_user_open', 'idx_artifact_identities_attention_state')


@pytest.fixture(scope='module')
def db():
    connection = connect()
    seed(connection, 0.05)
    # Leave a third of the history threads unread, so the unread reads return many rows.
    connection.execute("DELETE FROM doc_comment_thread_views WHERE rowid % 3 = 0")
    # Identities sharing an external id across kinds, and an inactive one: the old lookup read
    # by external id alone, the new one by the full key.
    connection.executemany(
        """INSERT INTO artifact_identities (id,integration,type,external_id,title,state,created_at,updated_at)
           VALUES (?,?,?,?,'Twin',?,'t','t')""",
        [('twin-a', 'upload', 'document', '/workspace/notes/doc-000001.md', 'active'),
         ('twin-b', 'workspace', 'image', '/workspace/notes/doc-000001.md', 'active'),
         ('twin-c', 'workspace', 'document', 'gone', 'deleted')])
    connection.commit()
    yield connection
    connection.close()


def _lists(db):
    """Tasks and notifications for Activity, and the dock's list."""
    return [activity_lists(db, USER, view=view)[:2] for view in ('activity', None)]


def _plan(db, sql: str) -> str:
    return '\n'.join(row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + sql))


def _traced(db) -> list[str]:
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        _lists(db)
    finally:
        db.set_trace_callback(None)
    return statements


def _one(statements: list[str], marker: str) -> str:
    found = [sql for sql in statements if marker in sql]
    assert found, marker
    return found[0]


def test_the_queue_is_the_same_with_and_without_the_new_indexes(db):
    with_indexes = _lists(db)
    db.execute('BEGIN')
    for index in NEW_INDEXES:
        db.execute(f'DROP INDEX {index}')
    try:
        without = _lists(db)
        assert db.in_transaction  # the read committed nothing, so the drop rolls back
    finally:
        db.rollback()
    assert with_indexes == without
    assert with_indexes[0][0] and with_indexes[0][1]  # tasks and notifications both present


def test_each_read_reaches_its_rows_through_an_index(db):
    statements = _traced(db)
    candidate = _one(statements, "JOIN plan_nodes n ON n.plan_id=p.id WHERE p.owner_kind='thread'")
    assert 'idx_plan_nodes_user_open' in _plan(db, candidate)
    raised = _one(statements, "json_extract(i.metadata, '$.attention_state')")
    assert 'idx_artifact_identities_attention_state' in _plan(db, raised)
    lookups = [sql for sql in statements if ' key CROSS JOIN artifact_identities i' in sql]
    assert lookups  # the raised batch's identities per list, and the reviews' documents
    for lookup in lookups:
        assert 'SEARCH i USING INDEX sqlite_autoindex_artifact_identities_2' in _plan(db, lookup)
    for chunk in [sql for sql in statements if 'owner_candidates AS' in sql]:
        plan = _plan(db, chunk)
        assert 'SEARCH item USING INDEX idx_doc_comments_artifact' in plan
        assert 'SCAN item' not in plan


def test_identity_lookup_by_full_key_matches_the_old_external_id_read(db):
    keys = [(r['integration'], r['type'], r['external_id']) for r in db.execute(
        'SELECT integration,type,external_id FROM artifact_identities ORDER BY id')]
    keys += [('workspace', 'document', 'missing'), ('workspace', 'document', 'gone')]
    columns = 'id AS artifact_id, external_id, integration, type, title, metadata, created_at'
    wanted = set(keys)
    old = db.execute(
        f"SELECT {columns} FROM artifact_identities WHERE state='active'"
        " AND external_id IN (SELECT value FROM json_each(?))", (json.dumps(sorted({k[2] for k in keys})),))
    old_rows = sorted(tuple(r) for r in old if (r['integration'], r['type'], r['external_id']) in wanted)
    new = attention_detail.active_identities(db, ', '.join(f'i.{c}' for c in columns.split(', ')), keys)
    assert sorted(tuple(r) for r in new) == old_rows
    assert len(old_rows) > 1000


def test_raised_identities_read_matches_the_old_text(db):
    linked = plans_public.ask_linked_sql('i')
    old = ("SELECT * FROM artifact_identities i WHERE i.state='active'"
           f" AND json_extract(i.metadata, '$.attention_state')=? AND NOT {linked}")
    new = ("SELECT * FROM artifact_identities i WHERE i.state='active' AND json_valid(i.metadata)"
           f" AND json_extract(i.metadata, '$.attention_state')=? AND NOT {linked}")
    rows = sorted(tuple(r) for r in db.execute(old, (attention.ATTENTION_RAISED,)))
    assert sorted(tuple(r) for r in db.execute(new, (attention.ATTENTION_RAISED,))) == rows
    assert rows


def _old_unread_comment_rows(user_id, artifact_id):
    """The read as it was before the thread view moved ahead of the person's comment joins."""
    scope = (artifact_id,) if isinstance(artifact_id, str) else (artifact_id or ())
    scoped = artifact_id is not None
    within = f"IN ({','.join('?' * len(scope))})"
    sql = f"""FROM doc_comments item
           JOIN doc_comments root ON root.id=COALESCE(item.parent_id, item.id)
           LEFT JOIN (
               SELECT DISTINCT parent_id FROM doc_comments
               WHERE author_kind='user' AND parent_id IS NOT NULL
                 {f"AND artifact_id {within}" if scoped else ""}
           ) touched ON touched.parent_id=root.id
           LEFT JOIN (
               SELECT artifact_id, MIN(created_at) AS first_at FROM doc_comments
               WHERE author_kind='user' {f"AND artifact_id {within}" if scoped else ""}
               GROUP BY artifact_id
           ) first_person ON first_person.artifact_id=item.artifact_id
           LEFT JOIN doc_comment_thread_views thread_view
             ON thread_view.user_id=? AND thread_view.root_id=root.id
           WHERE item.author_kind<>'user'
             {f"AND item.artifact_id {within} AND root.artifact_id {within}" if scoped else ""}
             AND ((item.parent_id IS NOT NULL
                   AND (root.author_kind='user' OR touched.parent_id IS NOT NULL OR {_person_tagged('item')}))
                  OR ({_eligible_top_level('item')})
                  OR {directed_sql('item')})
             AND {past_watermark_sql('item', 'thread_view')}"""
    directed = (USER_AUTHOR_ID, user_id)
    return sql, ((*scope, *scope, user_id, *scope, *scope, user_id, user_id, *directed) if scoped
                 else (user_id, user_id, user_id, *directed))


@pytest.mark.parametrize('scope', ['chunk', 'one', 'all'])
def test_unread_rows_match_the_old_join_order(db, scope):
    documents = activity_reply_rows._person_documents(db)
    artifact_id = {'chunk': tuple(documents[:feedback_state._SCOPE_CHUNK]), 'one': documents[0], 'all': None}[scope]
    select = 'SELECT root.id, item.id '

    def read(build):
        sql, params = build(USER, artifact_id)
        return sorted(tuple(r) for r in db.execute(select + sql, params))

    rows = read(_old_unread_comment_rows)
    assert read(unread_comment_rows) == rows
    assert rows


def test_an_identity_with_malformed_metadata_is_skipped_not_an_error():
    from infra.database import initialize_schema

    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    db.execute("""INSERT INTO artifact_identities (id,integration,type,external_id,metadata,created_at,updated_at)
                  VALUES ('bad','workspace','document','/w/bad.md','not json','t','t')""")
    db.commit()
    assert attention.list_raised(db=db) == []
