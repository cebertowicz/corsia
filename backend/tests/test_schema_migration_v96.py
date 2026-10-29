"""Schema v96 gives existing databases the installed-boundary index the reader needs.

Three claims matter, and the first two are the whole reason the index exists.

* The boundary reader's own statement — captured from what ``read_boundaries``
  executes, not a copied string — searches the partial index, so the read is
  bounded by the boundaries a thread has instead of by its whole message
  history. A guard passed as a bound parameter does not qualify: SQLite matches
  a partial index against the statement's expression tree, and a parameter is a
  variable there, so the planner falls back to a full per-thread scan. The last
  two tests pin that fallback so a future "tidy" cannot silently rebind it.
* The guard is only a narrowing. The ``json_type`` check still decides, and the
  rows are identical whether or not the index is present.

Like the v84 indexes, the plans are read with no planner statistics, because
production databases never run ANALYZE.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from core.lesson_report_boundaries import read_boundaries
from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_95_to_96_learning_boundary_index import migrate_95_to_96
from infra.schema import SCHEMA_VERSION

#: sqlite records an index's own statement text, minus its ``IF NOT EXISTS``;
#: this value is one flattened line of that text.
_EXPECTED_INDEX = (
    "CREATE INDEX idx_messages_installed_boundaries "
    "ON messages(thread_id, seq, id) "
    "WHERE instr(metadata,'_corsia_installed_context_sources')>0"
)
_INDEX = "idx_messages_installed_boundaries"
_KEY = "_corsia_installed_context_sources"

#: The thread's whole message history, so a plan that ignores the index is a
#: plan that reads it. The boundary rows keep the same ``seq`` order they had in
#: the thread; a NULL sequence and a foreign-user thread travel with them.
_BOUNDARY = (
    '{"responsibility_map_snapshot":{"thread_id":"t-1"},'
    f'"{_KEY}":[{{"source_id":"dynamic:skills-index","inclusion":"included",'
    '"metadata":{"lessons":[{"id":"lesson-a","behavioral_sha256":"v1"}]}}]}'
)


def _index_rows(db: sqlite3.Connection) -> dict[str, str]:
    return {
        str(name): " ".join((sql or "").split())
        for name, sql in db.execute("SELECT name, sql FROM sqlite_master WHERE type='index'")
        if str(name) == _INDEX
    }


def _fresh_indexes() -> dict[str, str]:
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        return _index_rows(db)


def _thread(db, thread: str, user: str) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,title,model,next_message_seq,created_at,updated_at) "
        "VALUES (?,?,'T','m',100,'t0','t0')",
        (thread, user),
    )


def _message(db, msg_id: str, thread: str, seq, metadata, content: str = "body") -> None:
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,seq,created_at) "
        "VALUES (?,?,'assistant',?,?,?,'t')",
        (msg_id, thread, content, metadata, seq),
    )


def _seed(db: sqlite3.Connection) -> None:
    """One thread with two boundaries and every decoy that must stay out.

    The decoys all carry the key text so the guard matches them, and every one
    of them is metadata the ``json_type`` check rejects: nested inside another
    value, held as an object rather than an array, or not JSON at all. The
    foreign thread proves scope still applies.
    """
    _thread(db, "t-1", "u")
    _thread(db, "t-2", "other")
    _message(db, "plain-1", "t-1", 1, '{"name":"other"}')
    _message(db, "boundary-null-seq", "t-1", None, _BOUNDARY)
    _message(db, "boundary-first", "t-1", 2, _BOUNDARY)
    _message(db, "plain-2", "t-1", 3, '{"name":"other"}')
    _message(db, "decoy-nested", "t-1", 4, f'{{"note":"reads {_KEY} from disk"}}')
    _message(db, "decoy-object", "t-1", 5, f'{{"{_KEY}":{{"sources":[]}}}}')
    _message(db, "decoy-string", "t-1", 6, f'{{"{_KEY}":"dynamic:skills-index"}}')
    _message(db, "decoy-in-content", "t-1", 7, '{"name":"other"}', content=f"mentions {_KEY}")
    _message(db, "broken-metadata", "t-1", 8, "not json at all")
    _message(db, "plain-3", "t-1", 9, None)
    _message(db, "foreign-boundary", "t-2", 1, _BOUNDARY)
    db.commit()


def _connection(with_index: bool) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='sqlite_stat1'").fetchone()[0] == 0
    _seed(db)
    if not with_index:
        db.execute(f"DROP INDEX {_INDEX}")
        db.commit()
        assert _index_rows(db) == {}
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


def _plan(db: sqlite3.Connection, sql: str, *bindings) -> list[str]:
    return [str(row[3]) for row in db.execute("EXPLAIN QUERY PLAN " + sql, bindings)]


def _selected(db: sqlite3.Connection) -> list[tuple]:
    return [tuple(row) for row in db.execute(
        "SELECT m.id,m.metadata FROM threads t JOIN messages m ON m.thread_id=t.id "
        "WHERE t.id=? AND t.user_id=? "
        "AND CASE WHEN json_valid(m.metadata) THEN json_type(m.metadata,?) END='array' "
        "ORDER BY m.seq,m.id",
        ("t-1", "u", f"$.{_KEY}"),
    )]


def test_the_step_is_registered_at_95_and_96_is_current() -> None:
    assert SCHEMA_VERSION == 164
    assert MIGRATIONS[95] is migrate_95_to_96


def test_fresh_install_carries_exactly_the_installed_boundary_index() -> None:
    assert _fresh_indexes() == {_INDEX: _EXPECTED_INDEX}


def test_the_reader_searches_the_partial_index_and_sorts_in_it() -> None:
    """The statement ``read_boundaries`` actually runs searches the index, with no planner stats."""
    with closing(_connection(with_index=True)) as db:
        statements = _executed(db, lambda: read_boundaries(db, user_id="u", thread_ids=("t-1",)))
        assert len(statements) == 1, statements
        assert _KEY in statements[0], statements
        assert _plan(db, statements[0]) == [
            "SEARCH t USING INDEX sqlite_autoindex_threads_1 (id=?)",
            f"SEARCH m USING INDEX {_INDEX} (thread_id=?)",
        ]


def test_the_guarded_statement_keeps_every_row_the_unguarded_one_returns() -> None:
    """Identical rows and order with and without the index: the guard narrows reads, not results."""
    with closing(_connection(with_index=True)) as indexed, closing(_connection(with_index=False)) as bare:
        assert _selected(indexed) == _selected(bare)
        assert [row[0] for row in _selected(bare)] == ["boundary-null-seq", "boundary-first"]

        facts = read_boundaries(indexed, user_id="u", thread_ids=("t-1",))
        assert facts == read_boundaries(bare, user_id="u", thread_ids=("t-1",))
        assert [(fact["thread_id"], fact["id"]) for fact in facts] == [
            ("t-1", "lesson-a"), ("t-1", "lesson-a"),
        ]


def test_the_key_reaches_sqlite_as_text_not_as_a_bound_parameter() -> None:
    """The one spelling that can use the index: a bound key silently loses it."""
    guarded = (
        "SELECT m.id,m.metadata FROM threads t JOIN messages m ON m.thread_id=t.id "
        "WHERE t.id=? AND t.user_id=? "
        f"AND instr(m.metadata,'{_KEY}')>0 "
        "AND CASE WHEN json_valid(m.metadata) THEN json_type(m.metadata,?) END='array' "
        "ORDER BY m.seq,m.id"
    )
    bound = guarded.replace(f"'{_KEY}'", "?")
    with closing(_connection(with_index=True)) as db:
        statements = _executed(db, lambda: read_boundaries(db, user_id="u", thread_ids=("t-1",)))
        assert statements and _KEY in statements[0], statements
        guarded_plan = _plan(db, guarded, "t-1", "u", f"$.{_KEY}")
        bound_plan = _plan(db, bound, "t-1", "u", _KEY, f"$.{_KEY}")
        assert [step for step in guarded_plan if step.startswith("SEARCH m")] == [
            f"SEARCH m USING INDEX {_INDEX} (thread_id=?)"
        ]
        assert [step for step in bound_plan if step.startswith("SEARCH m")] == [
            "SEARCH m USING INDEX idx_messages_session_seq (thread_id=?)"
        ]
        assert any("TEMP B-TREE" in step for step in bound_plan), bound_plan
        assert not any("TEMP B-TREE" in step for step in guarded_plan), guarded_plan


def test_the_reader_still_selects_the_same_rows_with_the_index_dropped() -> None:
    """Dropping the index changes the plan only: the reader is correct either way."""
    with closing(_connection(with_index=True)) as db:
        statements = _executed(db, lambda: read_boundaries(db, user_id="u", thread_ids=("t-1", "t-2")))
        before = read_boundaries(db, user_id="u", thread_ids=("t-1", "t-2"))
        db.execute(f"DROP INDEX {_INDEX}")
        db.commit()
        after = read_boundaries(db, user_id="u", thread_ids=("t-1", "t-2"))

        assert before and before == after
        assert statements and _KEY in statements[0]


def test_v95_upgrade_lands_the_same_index_a_fresh_install_has(tmp_path) -> None:
    path = tmp_path / "learning-boundary-index-v95.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        _seed(db)
        db.execute(f"DROP INDEX {_INDEX}")
        db.execute("PRAGMA user_version=95")
        db.commit()
        assert _index_rows(db) == {}

    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 95, SCHEMA_VERSION)
        assert _index_rows(db) == _fresh_indexes()
        assert [row[0] for row in _selected(db)] == ["boundary-null-seq", "boundary-first"]
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_the_step_skips_a_database_without_the_message_table() -> None:
    """A partial database indexes what it has instead of failing the whole ladder."""
    with closing(sqlite3.connect(":memory:")) as db:
        migrate_95_to_96(db)
        assert _index_rows(db) == {}


def test_the_step_repeats_cleanly_and_never_claims_the_version(tmp_path) -> None:
    """A half-applied 95 -> 96 must be safe to run again on the next startup."""
    path = tmp_path / "learning-boundary-index-retry.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        _seed(db)
        db.execute(f"DROP INDEX {_INDEX}")
        db.execute("PRAGMA user_version=95")
        db.commit()

        migrate_95_to_96(db)
        migrate_95_to_96(db)
        db.commit()

        assert _index_rows(db) == {_INDEX: _EXPECTED_INDEX}
        assert db.execute("PRAGMA user_version").fetchone()[0] == 95
        assert migrate_to_current(db, SCHEMA_VERSION).from_version == 95
