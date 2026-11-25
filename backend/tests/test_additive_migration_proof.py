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


def test_every_additive_step_has_a_registered_migration_and_fixture():
    assert ADDITIVE_STEPS <= MIGRATIONS.keys()
    assert ADDITIVE_STEPS == _FIXTURES.keys()


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
    for version in sorted(ADDITIVE_STEPS):
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
    for version in sorted(ADDITIVE_STEPS):
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
