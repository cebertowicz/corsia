"""Schema v84 gives existing databases the write-hold indexes fresh installs have.

Two claims matter. A database that reaches v84 through migration 83 -> 84
carries exactly the indexes the fresh schema creates, from one shared
definition; and the reads made under the write lock actually search them with
no planner statistics, which is how production databases run. The plans are
read from the statements the real functions execute, not from copied SQL, so
an edited query that stops using its index fails here.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_83_to_84_write_hold_indexes import migrate_83_to_84
from infra.schema import SCHEMA_VERSION
from tests.schema_rewind import rewind_to_v88_policy

#: sqlite records an index's own statement text, minus its ``IF NOT EXISTS``;
#: each value here is one flattened line of that text.
_EXPECTED_INDEXES = {
    "idx_messages_queued_inputs": (
        "CREATE INDEX idx_messages_queued_inputs "
        "ON messages(thread_id, delivery_state, input_order) "
        "WHERE input_order IS NOT NULL AND delivery_state='queued'"
    ),
    "idx_active_turns_thread_status": (
        "CREATE INDEX idx_active_turns_thread_status ON active_turns(thread_id, status)"
    ),
}


def _write_hold_indexes(db: sqlite3.Connection) -> dict[str, str]:
    return {
        str(name): " ".join((sql or "").split())
        for name, sql in db.execute("SELECT name, sql FROM sqlite_master WHERE type='index'")
        if str(name) in _EXPECTED_INDEXES
    }


def _fresh_indexes() -> dict[str, str]:
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        return _write_hold_indexes(db)


def _build_existing_v83_database(path) -> None:
    """Write a v83 database: current tables, live rows, no write-hold indexes."""
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        rewind_to_v88_policy(db)
        db.execute(
            "INSERT INTO threads (id,user_id,title,model,next_message_seq,"
            "created_at,updated_at) VALUES ('t-1','u','T','m',2,'t0','t0')"
        )
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq,input_order,"
            "delivery_state) VALUES ('m-1','t-1','user','body','t0',1,1,'queued')"
        )
        for name in _EXPECTED_INDEXES:
            db.execute(f"DROP INDEX {name}")
        db.execute("PRAGMA user_version=83")
        db.commit()
        # Without this the upgrade below would prove nothing.
        assert _write_hold_indexes(db) == {}


def _connection_with_thread() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='sqlite_stat1'").fetchone()[0] == 0
    db.execute(
        "INSERT INTO threads (id,user_id,title,model,next_message_seq,created_at,updated_at,project_id) "
        "VALUES ('t-1','u','T','m',2,'t0','t0','p')"
    )
    db.commit()
    return db


def _executed(db: sqlite3.Connection, call) -> list[str]:
    """Every SELECT the call ran, as sqlite expanded it."""
    seen: list[str] = []
    db.set_trace_callback(seen.append)
    try:
        call()
    finally:
        db.set_trace_callback(None)
    return [sql for sql in seen if sql.lstrip().upper().startswith("SELECT")]


def _plan(db: sqlite3.Connection, sql: str) -> list[str]:
    return [str(row[3]) for row in db.execute("EXPLAIN QUERY PLAN " + sql)]


def _searches(db: sqlite3.Connection, statements: list[str], alias: str) -> list[str]:
    """Every plan step that reads ``alias`` across the statements."""
    return [step for sql in statements for step in _plan(db, sql)
            if step.startswith((f"SEARCH {alias} ", f"SCAN {alias}"))]


def test_the_step_is_registered_at_83_and_84_is_current() -> None:
    assert SCHEMA_VERSION == 109
    assert MIGRATIONS[83] is migrate_83_to_84


def test_fresh_install_carries_exactly_the_write_hold_indexes() -> None:
    assert _fresh_indexes() == _EXPECTED_INDEXES


def test_account_hold_refresh_reads_only_queued_inputs_without_planner_stats() -> None:
    """The pending, head and stall reads search the queued index, never a thread's whole input history."""
    from infra.queued_input_settlement import refresh_account_input_holds

    with closing(_connection_with_thread()) as db:
        db.execute("BEGIN")
        statements = _executed(db, lambda: refresh_account_input_holds(db, "t-1"))
        pending = [sql for sql in statements if sql.startswith("SELECT id,target_model")]
        assert len(pending) == 1, statements
        assert _plan(db, pending[0]) == [
            "SEARCH messages USING INDEX idx_messages_queued_inputs "
            "(thread_id=? AND delivery_state=? AND input_order>?)"
        ]
        for alias in ("held", "head", "queued"):
            steps = _searches(db, statements, alias)
            assert steps and all("idx_messages_queued_inputs" in step for step in steps), (alias, steps)
        live = _searches(db, statements, "live")
        assert live == ["SEARCH live USING COVERING INDEX idx_active_turns_thread_status (thread_id=? AND status=?)"]
        db.rollback()


def test_vendor_policy_thread_walk_probes_queued_inputs_by_index() -> None:
    from connections.account_holds import policy_thread_ids

    with closing(_connection_with_thread()) as db:
        statements = _executed(db, lambda: policy_thread_ids("p", "codex", db=db))
        assert _searches(db, statements, "m") == [
            "SEARCH m USING INDEX idx_messages_queued_inputs (thread_id=? AND delivery_state=? AND input_order>?)"
        ]


def test_running_turn_probe_searches_thread_and_status() -> None:
    with closing(_connection_with_thread()) as db:
        assert _plan(db, "SELECT 1 FROM active_turns WHERE thread_id='t-1' AND status='running'") == [
            "SEARCH active_turns USING COVERING INDEX idx_active_turns_thread_status (thread_id=? AND status=?)"
        ]


def test_queued_index_wins_whichever_index_was_created_first() -> None:
    """With no stats, a tie between indexes goes by creation order; this one must not need the tie."""
    from infra.queued_input_settlement import refresh_account_input_holds

    with closing(_connection_with_thread()) as db:
        (input_order_sql,) = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_messages_input_order'"
        ).fetchone()
        db.execute("DROP INDEX idx_messages_input_order")
        db.execute(input_order_sql)
        db.execute("BEGIN")
        statements = _executed(db, lambda: refresh_account_input_holds(db, "t-1"))
        pending = next(sql for sql in statements if sql.startswith("SELECT id,target_model"))
        assert "idx_messages_queued_inputs" in _plan(db, pending)[0]
        db.rollback()


def test_v83_upgrade_lands_the_same_indexes_a_fresh_install_has(tmp_path) -> None:
    path = tmp_path / "write-hold-indexes-v83.db"
    _build_existing_v83_database(path)

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 83, SCHEMA_VERSION)
        assert _write_hold_indexes(db) == _fresh_indexes()
        assert db.execute("SELECT content, delivery_state FROM messages WHERE id='m-1'").fetchone() == (
            "body", "queued",
        )
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_the_step_skips_tables_a_database_lacks() -> None:
    """A partial database indexes what it has instead of failing the whole ladder."""
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE active_turns (id TEXT, thread_id TEXT, status TEXT)")
        migrate_83_to_84(db)

        assert _write_hold_indexes(db) == {
            "idx_active_turns_thread_status": _EXPECTED_INDEXES["idx_active_turns_thread_status"],
        }


def test_the_step_repeats_cleanly_and_never_claims_the_version(tmp_path) -> None:
    """A half-applied 83 -> 84 must be safe to run again on the next startup."""
    path = tmp_path / "write-hold-indexes-retry.db"
    _build_existing_v83_database(path)

    with closing(sqlite3.connect(path)) as db:
        migrate_83_to_84(db)
        migrate_83_to_84(db)
        db.commit()

        assert _write_hold_indexes(db) == _EXPECTED_INDEXES
        assert db.execute("PRAGMA user_version").fetchone()[0] == 83
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 83, SCHEMA_VERSION)
        assert _write_hold_indexes(db) == _fresh_indexes()
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
