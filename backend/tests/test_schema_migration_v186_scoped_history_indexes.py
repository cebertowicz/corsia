"""Schema 186 gives existing databases the scoped-history indexes.

Four claims, and the first two are the whole reason the indexes exist.

* The stub pass's own statement — captured from what ``scan_before_scope``
  executes, not a copied string — searches ``idx_messages_retention_stub``, so
  the pass is bounded by the stubs a thread holds instead of by its whole
  history. The marker has to reach SQLite inside the statement text for that to
  happen at all, which is why ``infra/tool_retention_marker.py`` builds the
  predicate as text: the reader's ``WHERE`` and the index predicate are both
  built from that one string.
* The Turn-provenance halves each seek their own index, because the OR across
  them gets no MULTI-INDEX OR at a real Turn count.
* A fresh schema and a migrated one carry identical indexes.
* The model-switch token's stub count excludes an unpositioned row.

Like the v84 and v96 indexes, plans are read with no planner statistics,
because production databases never run ANALYZE.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from history.scoped_source import ScopedSource, scan_before_scope
from history.source import validate_source_rows
from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS
from infra.migration_185_to_186_scoped_history_indexes import migrate_185_to_186
from infra.schema import SCHEMA_VERSION
from infra.scoped_history_indexes import SCOPED_HISTORY_INDEXES
from infra.tool_retention_marker import TOOL_TRIM_MARKER
from thread.model_row import _model_switch_transcript_version

#: sqlite records an index's own statement text, minus its ``IF NOT EXISTS``;
#: each value here is one flattened line of that text.
_EXPECTED_INDEXES = {
    "idx_messages_retention_stub": (
        "CREATE INDEX idx_messages_retention_stub ON messages(thread_id, seq) "
        f"WHERE role = 'tool' AND substr(content, 1, {len(TOOL_TRIM_MARKER)}) = "
        f"'{TOOL_TRIM_MARKER}'"
    ),
    "idx_messages_meta_turn_seq": (
        "CREATE INDEX idx_messages_meta_turn_seq ON messages("
        "thread_id, json_extract(metadata, '$.turn_id'), seq) "
        "WHERE json_valid(metadata)"
    ),
    "idx_messages_column_turn_seq": (
        "CREATE INDEX idx_messages_column_turn_seq ON messages("
        "thread_id, active_turn_id, seq) WHERE active_turn_id IS NOT NULL"
    ),
}

_STUB_INDEX = "idx_messages_retention_stub"
_THREAD = "t-1"
_MARKER_SQL = f"substr(content, 1, {len(TOOL_TRIM_MARKER)}) = '{TOOL_TRIM_MARKER}'"

#: seq, Turn, whether the tool payload was trimmed, and whether the row is
#: claimed in the Turn column or only in its metadata.
ROWS: tuple[tuple[int | None, str, bool, bool], ...] = (
    (1, "t1", False, True),
    (2, "t1", True, True),
    (3, "t1", False, False),
    (4, "t2", True, False),
    (5, "t2", False, False),
    (None, "t2", True, False),   # an unpositioned stub: outside every token
)

#: Positioned stubs are seq 2 and 4; a scope starting at seq 4 leaves seq 2 as
#: the covered history it must still report.
COVERED_STUB_SEQS: tuple[int, ...] = (2,)


def _index_rows(db: sqlite3.Connection) -> dict[str, str]:
    return {
        str(name): " ".join((sql or "").split())
        for name, sql in db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            "AND name IN ('idx_messages_retention_stub','idx_messages_meta_turn_seq',"
            "'idx_messages_column_turn_seq')"
        )
    }


def _schema(with_indexes: bool) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    assert db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='sqlite_stat1'"
    ).fetchone()[0] == 0
    if not with_indexes:
        for name in _EXPECTED_INDEXES:
            db.execute(f"DROP INDEX {name}")
        db.commit()
        assert _index_rows(db) == {}
    return db


def _seed(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,title,next_message_seq,created_at,updated_at) "
        "VALUES (?,?,'T',9,'t0','t0')",
        (_THREAD, "u"),
    )
    for turn_order, turn_id in enumerate(("t1", "t2"), start=1):
        db.execute(
            "INSERT INTO active_turns (turn_order,id,thread_id,execution_epoch,model,"
            "status,started_at,ended_at,boot_epoch) "
            "VALUES (?,?,?,0,'m','completed','s','e','b')",
            (turn_order, turn_id, _THREAD),
        )
    for index, (seq, turn_id, trimmed, in_column) in enumerate(ROWS, start=1):
        content = (
            f"{TOOL_TRIM_MARKER} after 30d retention — 9 chars, 1 lines]"
            if trimmed else "out"
        )
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,"
            "delivery_state,active_turn_id,seq) VALUES (?,?,?,?,?,'t','delivered',?,?)",
            (
                f"m{index}", _THREAD, "tool" if trimmed else "assistant", content,
                json.dumps({"turn_id": turn_id}), turn_id if in_column else None, seq,
            ),
        )
    db.commit()


def _scope_from(db: sqlite3.Connection, first_seq: int) -> ScopedSource:
    rows = db.execute(
        "SELECT id,thread_id,role,content,metadata,created_at,seq,active_turn_id "
        "FROM messages WHERE thread_id=? AND delivery_state='delivered' AND seq>=? "
        "ORDER BY seq",
        (_THREAD, first_seq),
    ).fetchall()
    return ScopedSource(
        rows=validate_source_rows(rows, _THREAD), predecessor=None, starts_bound_turn=True,
    )


def _executed(db: sqlite3.Connection, call) -> list[str]:
    """Every SELECT the call ran, as SQLite expanded it."""
    seen: list[str] = []
    db.set_trace_callback(seen.append)
    try:
        call()
    finally:
        db.set_trace_callback(None)
    return [sql for sql in seen if sql.lstrip().upper().startswith("SELECT")]


def _plan(db: sqlite3.Connection, sql: str, *bindings) -> list[str]:
    return [str(row[3]) for row in db.execute("EXPLAIN QUERY PLAN " + sql, bindings)]


def test_schema_186_registers_the_migration_step():
    assert SCHEMA_VERSION == 188
    assert MIGRATIONS[185] is migrate_185_to_186


def test_expected_index_text_is_the_one_that_ships():
    """The pinned text matches what a fresh schema records, marker length included."""
    with closing(_schema(with_indexes=True)) as db:
        assert _index_rows(db) == _EXPECTED_INDEXES
        assert len(SCOPED_HISTORY_INDEXES) == len(_EXPECTED_INDEXES)


def test_migration_lands_the_same_indexes_a_fresh_schema_has():
    migrated = _schema(with_indexes=False)
    migrate_185_to_186(migrated)
    migrated.commit()
    with closing(_schema(with_indexes=True)) as fresh:
        assert _index_rows(migrated) == _index_rows(fresh)
    migrated.close()


def test_stub_pass_searches_the_partial_index():
    with closing(_schema(with_indexes=True)) as db:
        _seed(db)
        executed = _executed(db, lambda: scan_before_scope(db, _THREAD, _scope_from(db, 4)))
        stub_selects = [sql for sql in executed if _MARKER_SQL in sql]
        assert stub_selects, f"the stub pass did not run: {executed}"
        # A traced statement is already expanded, so the plan is read as text.
        plan = _plan(db, stub_selects[0])
        assert any(_STUB_INDEX in step for step in plan), plan
        assert not any("idx_messages_session_seq" in step for step in plan), plan


def test_provenance_halves_seek_their_own_indexes():
    with closing(_schema(with_indexes=True)) as db:
        _seed(db)
        column_half = (
            "SELECT id FROM messages WHERE thread_id = ? AND delivery_state='delivered' "
            "AND seq < ? AND active_turn_id IN (?)"
        )
        meta_half = (
            "SELECT id FROM messages WHERE thread_id = ? AND delivery_state='delivered' "
            "AND seq < ? AND active_turn_id IS NULL AND json_valid(metadata) "
            "AND json_extract(metadata, '$.turn_id') IN (?)"
        )
        column_plan = _plan(db, column_half, _THREAD, 3, "t2")
        meta_plan = _plan(db, meta_half, _THREAD, 3, "t2")
        assert any("idx_messages_column_turn_seq" in step for step in column_plan), column_plan
        assert any("idx_messages_meta_turn_seq" in step for step in meta_plan), meta_plan


def test_stub_pass_still_reports_covered_stubs():
    with closing(_schema(with_indexes=True)) as db:
        _seed(db)
        assert scan_before_scope(db, _THREAD, _scope_from(db, 4)) == COVERED_STUB_SEQS


def test_model_switch_token_keeps_unpositioned_rows_out():
    """(next_message_seq, message_count, max_seq, retention_stub_count), literal.

    Six seeded rows, one of them unpositioned; five of them positioned, of
    which two are stubs — and the unpositioned stub stays out of the count,
    because the token reads positioned rows only.
    """
    with closing(_schema(with_indexes=True)) as db:
        _seed(db)
        assert _model_switch_transcript_version(db, _THREAD) == (9, 5, 5, 2)
