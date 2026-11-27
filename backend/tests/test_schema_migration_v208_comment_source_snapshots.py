"""Schema 207 -> 208: the snapshot store an anchor is measured against.

The step is purely additive, so these tests are the whole step: it registers at 207,
gives an existing database the same shapes a fresh install declares, runs twice, and
backfills nothing, because the text a past version held is not recoverable from the
current file and a guessed snapshot would place anchors worse than NULL does.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS
from infra.migration_207_to_208_comment_source_snapshots import migrate_207_to_208
from infra.schema import SCHEMA_VERSION

_COMMENT = ("INSERT INTO doc_comments (id,artifact_id,author,author_kind,body,anchor_text,"
            "from_off,to_off,review,resolved,created_at,updated_at)"
            " VALUES ('c1','a1','me','user','b','target',7,13,0,0,'t','t')")


def _fresh() -> sqlite3.Connection:
    db = sqlite3.connect(':memory:')
    initialize_schema(db)
    return db


def _installed_at_207() -> sqlite3.Connection:
    """A database as it stands at schema 207: this build's schema with what 208 adds taken back out.

    SQLite will not drop the column on its own here (it re-parses the table's own DDL
    afterwards and refuses), so the comments table is rebuilt from the DDL it was created
    with, minus its last definition, and its other indexes are re-created from the
    definitions they were created with. No part of the 207 shape is hand-written.
    """
    db = _fresh()
    table_sql = db.execute("SELECT sql FROM sqlite_master WHERE name='doc_comments'").fetchone()[0]
    index_sql = [row[0] for row in db.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='doc_comments' AND type='index' AND sql IS NOT NULL"
        " AND name NOT IN ('idx_doc_comments_snapshot','snapshots_digest')"
    )]
    without_column = _ddl_without_last_definition(table_sql, 'snapshot_id')
    columns = [row[1] for row in db.execute('PRAGMA table_info(doc_comments)') if row[1] != 'snapshot_id']
    with closing(db.execute('SELECT * FROM doc_comments')) as cursor:
        names = [description[0] for description in cursor.description]
        rows = [tuple(value for name, value in zip(names, row) if name != 'snapshot_id')
                for row in cursor.fetchall()]
    assert names[-1] == 'snapshot_id'

    db.execute('DROP INDEX idx_doc_comments_snapshot')
    db.execute('DROP INDEX snapshots_digest')
    db.execute('DROP TABLE doc_comment_snapshots')
    db.execute('DROP TABLE doc_comments')
    db.execute(without_column)
    for statement in index_sql:
        db.execute(statement)
    if rows:
        db.executemany(
            f"INSERT INTO doc_comments ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", rows
        )
    db.execute('PRAGMA user_version=207')
    return db


def _ddl_without_last_definition(ddl: str, column: str) -> str:
    """The table's own DDL with its last column dropped: the comma that separated it goes too.

    The comma search skips lines that are SQL comments, since a comment can hold a comma.
    """
    at = ddl.index(column)
    before = len(ddl)
    while True:
        before = ddl.rindex(',', 0, before)
        line = ddl[ddl.rfind('\n', 0, before) + 1:before]
        if not line.strip().startswith('--'):
            break
    assert ddl.count(column) == 1 and column in ddl[before:]
    return ddl[:before].rstrip() + '\n)'


def _columns(db: sqlite3.Connection) -> dict[str, tuple]:
    return {row[1]: (row[2], row[3], row[4], row[5]) for row in db.execute('PRAGMA table_info(doc_comments)')}


def _snapshot_objects(db: sqlite3.Connection) -> list[tuple[str, str]]:
    return sorted(
        (row[0], ' '.join((row[1] or '').split()))
        for row in db.execute(
            "SELECT type, sql FROM sqlite_master"
            " WHERE name IN ('doc_comment_snapshots','snapshots_digest','idx_doc_comments_snapshot')"
        )
    )


def test_the_step_is_registered_at_207() -> None:
    assert SCHEMA_VERSION == 211
    assert MIGRATIONS[207] is migrate_207_to_208


def test_a_fresh_install_declares_the_store_and_the_reference() -> None:
    with closing(_fresh()) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='doc_suggestion_history'").fetchone()
        assert _snapshot_objects(db) == [
            ('index', 'CREATE INDEX idx_doc_comments_snapshot ON doc_comments(snapshot_id) WHERE snapshot_id IS NOT NULL'),
            ('index', 'CREATE INDEX snapshots_digest ON doc_comment_snapshots(digest)'),
            ('table', 'CREATE TABLE doc_comment_snapshots ( id INTEGER PRIMARY KEY, digest BLOB NOT NULL,'
                      ' text BLOB NOT NULL )'),
        ]
        # The digest only narrows the lookup, so it must not be unique: different bytes
        # sharing a digest keep separate rows (comment_snapshots.intern).
        assert 'UNIQUE' not in _snapshot_objects(db)[2][1]
        assert _columns(db)['snapshot_id'] == ('INTEGER', 0, None, 0)


def test_upgrade_from_207_keeps_suggestion_history_and_adds_snapshots() -> None:
    from infra.database_migrations import migrate_to_current

    with closing(_installed_at_207()) as db:
        db.execute("INSERT INTO doc_suggestion_history VALUES (?, ?, ?, ?, ?)",
                   ('event', 'artifact', '{}', 'before', 'after'))
        db.commit()
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.from_version, result.to_version) == (207, 211)
        assert db.execute('SELECT event_id FROM doc_suggestion_history').fetchone() == ('event',)
        assert _snapshot_objects(db)


def test_the_step_gives_an_older_database_the_fresh_shapes_and_runs_twice() -> None:
    with closing(_fresh()) as fresh, closing(_installed_at_207()) as db:
        assert _snapshot_objects(db) == []
        assert 'snapshot_id' not in _columns(db)

        migrate_207_to_208(db)
        migrate_207_to_208(db)

        # The step appends the column, which is where a fresh schema declares it, so the two
        # agree column by column in order as well as by name and definition.
        assert _snapshot_objects(db) == _snapshot_objects(fresh)
        assert _columns(db) == _columns(fresh)
        assert [row[1] for row in db.execute('PRAGMA table_info(doc_comments)')] == \
               [row[1] for row in fresh.execute('PRAGMA table_info(doc_comments)')]


def test_an_existing_comment_starts_with_no_snapshot_and_keeps_its_anchor() -> None:
    """Nothing is backfilled: the old text is not recoverable, and NULL places as before."""
    with closing(_installed_at_207()) as db:
        db.execute(_COMMENT)
        assert db.execute('SELECT id,anchor_text,from_off,to_off FROM doc_comments'
                          " WHERE id='c1'").fetchone() == ('c1', 'target', 7, 13)

        migrate_207_to_208(db)

        assert db.execute('SELECT id,anchor_text,from_off,to_off,snapshot_id'
                          " FROM doc_comments WHERE id='c1'").fetchone() == ('c1', 'target', 7, 13, None)
        assert db.execute('SELECT COUNT(*) FROM doc_comment_snapshots').fetchone()[0] == 0


def test_the_step_skips_a_database_without_document_comments() -> None:
    """An old database that never held a comment gains nothing, as the other comment steps do."""
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY)')
    db.execute('PRAGMA user_version=207')

    migrate_207_to_208(db)

    assert _snapshot_objects(db) == []
    assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='doc_comments'").fetchone()[0] == 0


def test_the_step_writes_nothing_besides_the_new_shapes() -> None:
    """Row-for-row the same comment data, only with the new column widened onto it."""
    carried = 'id,artifact_id,author,author_kind,body,anchor_text,from_off,to_off,review,resolved'
    with closing(_installed_at_207()) as db:
        db.execute(_COMMENT)
        rows_before = db.execute(f'SELECT {carried} FROM doc_comments').fetchall()

        migrate_207_to_208(db)

        assert db.execute(f'SELECT {carried} FROM doc_comments').fetchall() == rows_before
