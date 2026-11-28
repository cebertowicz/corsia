"""Prove each declared additive migration cannot write existing rows in its fixture."""

import ast
import inspect
import sqlite3
import textwrap
from contextlib import closing

import pytest

from infra.database import initialize_schema
from infra.database_migrations import ADDITIVE_STEPS, MIGRATIONS, migrate_to_current
from tests.plan_writer_authorizer import _original_connect


def _version_208() -> sqlite3.Connection:
    # Use the real 207 -> 208 ladder on an unguarded, disposable connection.
    # Reconstruct the immediately preceding schema from the current empty schema.
    db = _original_connect(":memory:")
    initialize_schema(db)
    db.execute("DROP TABLE saved_login_fill_audit")
    ddl = db.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name='doc_comments'").fetchone()[0]
    indexes = [row[0] for row in db.execute(
        "SELECT sql FROM sqlite_schema WHERE type='index' AND tbl_name='doc_comments' "
        "AND name != 'idx_doc_comments_snapshot' AND sql IS NOT NULL"
    )]
    # SQLite cannot DROP this last column: the trailing line comments in the
    # existing DDL leave an incomplete statement. Recreate the empty table.
    previous_ddl = ddl.split("-- The document text these offsets count from", 1)[0].rstrip().removesuffix(",") + "\n)"
    db.execute("DROP TABLE doc_comments")
    db.execute(previous_ddl)
    for statement in indexes:
        db.execute(statement)
    db.execute("DROP TABLE doc_comment_snapshots")
    db.execute("PRAGMA user_version=207")
    db.commit()
    migrate_to_current(db, 208)
    return db


_FIXTURES = {208: _version_208}
# The guard and body-pin proof share this mapping so neither can drift.
_PINNED_BODIES = {
    209: '''
    def step(conn):
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "plans" not in tables:
            return
        columns = {row[1] for row in conn.execute("PRAGMA table_info(plans)")}
        if "later_reviewed_at" not in columns:
            conn.execute("ALTER TABLE plans ADD COLUMN later_reviewed_at TEXT")
    ''',
    210: '''
    def step(conn):
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, sql in (("plan_nodes", USER_OPEN_TASK_INDEX_SQL), ("artifact_identities", ATTENTION_STATE_INDEX_SQL)):
            if table in tables:
                conn.execute(sql.strip().rstrip(";"))
    ''',
    211: '''
    def step(conn):
        conn.execute(ACTIVITY_DRAFT_ACK_SCHEMA_SQL)
    ''',
}
#: The first from-version the additive mechanism can reach: the runner skips the
#: whole-database check only when *every* step in a migration's range is
#: additive, and 208 is the lowest step ever listed. Steps below it take the full
#: check whatever their author wrote. The whole ladder is squashed into one clean
#: schema before release, so this floor moves with the squash -- when leading
#: steps leave MIGRATIONS, raise it to the lowest one that survives.
_CLASSIFICATION_FLOOR = 208

#: Steps at or above the floor whose author chose the whole-database
#: foreign-key validation path. Every registered step at or above the floor
#: belongs in exactly one of the two sets: ``ADDITIVE_STEPS`` when this file can
#: prove it writes nothing existing, here when it cannot. Nothing reads this set
#: at runtime -- the runner still reads only ``ADDITIVE_STEPS`` -- so an
#: unclassified step is caught by the guard test below rather than by behaviour.
FULL_CHECK_STEPS: frozenset[int] = frozenset()

_NAMED_READ_PRAGMAS = {"table_info", "table_xinfo", "index_list", "index_info", "index_xinfo", "foreign_key_list"}
_READ_PRAGMAS = _NAMED_READ_PRAGMAS | {"database_list", "table_list", "compile_options", "data_version", "page_count", "page_size", "freelist_count", "schema_version", "user_version"}
_ALLOWED = {
    sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION,
    # REINDEX rebuilds index b-trees from the table's rows; it changes neither
    # rows nor declared parent keys. The runner's changed-table integrity check
    # has the same coverage as the full-scan path.
    sqlite3.SQLITE_REINDEX,
}


def _allowed(action, first, second, _database, _source):
    if action in _ALLOWED:
        return True
    if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE}:
        return first in {"sqlite_master", "sqlite_schema"}
    if action == sqlite3.SQLITE_PRAGMA:
        return first in _READ_PRAGMAS and (second is None or first in _NAMED_READ_PRAGMAS)
    return False


def test_every_additive_step_has_a_registered_migration_and_proof():
    assert ADDITIVE_STEPS <= MIGRATIONS.keys()
    assert ADDITIVE_STEPS == _FIXTURES.keys() | _PINNED_BODIES.keys()


def test_every_step_at_or_above_the_floor_is_classified():
    """A new step's author has to say which validation path it takes.

    ``ADDITIVE_STEPS`` is opt-in, so a step nobody classified silently costs
    every install a whole-database ``foreign_key_check`` plus a backup read on
    the update that crosses it -- 298 s holding the write lock on a large dev
    database for step 211 (#3489, after the same thing at 209 on Nov 26).
    """
    unclassified = sorted(
        version
        for version in MIGRATIONS
        if version >= _CLASSIFICATION_FLOOR
        and version not in ADDITIVE_STEPS
        and version not in FULL_CHECK_STEPS
    )
    assert not unclassified, (
        f"step(s) {unclassified} are in neither ADDITIVE_STEPS nor "
        "FULL_CHECK_STEPS: a new step belongs in exactly one of them, and one "
        "declared additive needs its proof in this file"
    )


def test_the_two_step_classifications_do_not_overlap():
    assert not ADDITIVE_STEPS & FULL_CHECK_STEPS


def _assert_literal_execute_only(function):
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    assert len(tree.body) == 1 and isinstance(tree.body[0], ast.FunctionDef)
    definition = tree.body[0]
    positional = definition.args.posonlyargs + definition.args.args
    assert positional
    connection = positional[0].arg
    body = definition.body
    if ast.get_docstring(definition) is not None:
        body = body[1:]
    for statement in body:
        assert isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
        call = statement.value
        assert isinstance(call.func, ast.Attribute) and call.func.attr == "execute"
        assert isinstance(call.func.value, ast.Name) and call.func.value.id == connection
        assert len(call.args) == 1 and not call.keywords
        assert isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)


def test_declared_additive_steps_are_literal_execute_statements():
    for version in sorted(_FIXTURES):
        _assert_literal_execute_only(MIGRATIONS[version])


def test_literal_execute_proof_rejects_data_dependent_branch():
    def branching_step(conn):
        if conn.execute("SELECT 1").fetchone():
            conn.execute("UPDATE sample SET value = 1")

    with pytest.raises(AssertionError):
        _assert_literal_execute_only(branching_step)


def test_proof_rejects_executescript_implicit_commit():
    with closing(_original_connect(":memory:")) as db:
        db.execute("BEGIN IMMEDIATE")
        events = []
        db.set_authorizer(lambda *event: (events.append(event), sqlite3.SQLITE_OK)[1])
        db.executescript("CREATE TABLE sample (id INTEGER);")
        db.set_authorizer(None)
        assert any(event[0] == sqlite3.SQLITE_TRANSACTION for event in events)
        assert not all(_allowed(*event) for event in events)


def test_declared_additive_steps_only_issue_safe_actions():
    for version in sorted(_FIXTURES):
        with closing(_FIXTURES[version]()) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == version
            events = []
            db.execute("BEGIN IMMEDIATE")
            db.set_authorizer(lambda *event: (events.append(event), sqlite3.SQLITE_OK)[1])
            try:
                MIGRATIONS[version](db)
            finally:
                db.set_authorizer(None)
                db.rollback()
            assert events, f"step {version} produced no authorizer events"
            assert all(_allowed(*event) for event in events), [
                event for event in events if not _allowed(*event)
            ]


def _body_ast(source):
    definition = ast.parse(textwrap.dedent(source)).body[0]
    body = definition.body
    if ast.get_docstring(definition) is not None:
        body = body[1:]
    return ast.dump(ast.Module(body=body, type_ignores=[]), include_attributes=False)


def test_metadata_guarded_steps_have_only_the_reviewed_operations():
    # An authorizer sees only the executed branch and cannot inspect ADD COLUMN's
    # definition. Pin the whole executable body and the SQL constants it names.
    for version, expected in _PINNED_BODIES.items():
        assert _body_ast(inspect.getsource(MIGRATIONS[version])) == _body_ast(expected)
    from infra.activity_draft_ack_schema import ACTIVITY_DRAFT_ACK_SCHEMA_SQL
    from infra.migration_210_to_211_queue_read_indexes import (
        ATTENTION_STATE_INDEX_SQL, USER_OPEN_TASK_INDEX_SQL,
    )
    assert ' '.join(USER_OPEN_TASK_INDEX_SQL.split()) == (
        "CREATE INDEX IF NOT EXISTS idx_plan_nodes_user_open ON plan_nodes(plan_id) "
        "WHERE assignee='user' AND kind='task' AND state IN ('pending','active') "
        "AND archived_at IS NULL;"
    )
    assert ' '.join(ATTENTION_STATE_INDEX_SQL.split()) == (
        "CREATE INDEX IF NOT EXISTS idx_artifact_identities_attention_state "
        "ON artifact_identities(json_extract(metadata, '$.attention_state')) "
        "WHERE state='active' AND json_valid(metadata);"
    )
    assert ' '.join(ACTIVITY_DRAFT_ACK_SCHEMA_SQL.split()) == (
        "CREATE TABLE IF NOT EXISTS activity_draft_acknowledgements ( "
        "user_id TEXT NOT NULL, "
        "draft_id TEXT NOT NULL REFERENCES action_drafts(id) ON DELETE CASCADE, "
        "PRIMARY KEY (user_id, draft_id) );"
    )


def test_metadata_guarded_steps_only_issue_safe_actions():
    with closing(_original_connect(":memory:")) as db:
        db.executescript("""
            CREATE TABLE plans (id TEXT);
            CREATE TABLE plan_nodes (plan_id TEXT, assignee TEXT, kind TEXT, state TEXT, archived_at TEXT);
            CREATE TABLE artifact_identities (metadata TEXT, state TEXT);
            CREATE TABLE action_drafts (id TEXT PRIMARY KEY);
        """)
        for version in _PINNED_BODIES:
            events = []
            db.set_authorizer(lambda *event: (events.append(event), sqlite3.SQLITE_OK)[1])
            try:
                MIGRATIONS[version](db)
            finally:
                db.set_authorizer(None)
            assert events
            assert all(_allowed(*event) or (
                version == 209 and event[:3] == (sqlite3.SQLITE_ALTER_TABLE, 'main', 'plans')
            ) for event in events), events
