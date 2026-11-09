"""The two passes a scoped render makes over history its checkpoint already covers.

A render that starts after a boundary still answers two questions about the
rows *before* it: which of them are retention stubs, and whether a Turn it reads
has rows there too. Both answers are consumed — a projection reports the covered
stubs (`full_raw_replay_available`, the retention note, the replay notice) — and
both were answered by stepping over every earlier row of the thread.

The expected values below are literal rows, not a second implementation: they
are what the seeded transcript holds, written out so a change that drops a stub
or stops refusing an interleaved Turn fails here.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from history.scoped_source import ScopedSource, scan_before_scope
from history.source import validate_source_rows
from history.types import PortableHistoryError
from infra.database import initialize_schema
from infra.tool_retention_marker import TOOL_TRIM_MARKER

THREAD = "t-1"

#: seq -> (role, turn, content, active_turn_id). ``STUB`` is a tool payload
#: retention trimmed; ``live`` is a tool payload that still has its bytes.
TRANSCRIPT: tuple[tuple[int, str, str, str, str | None], ...] = (
    (1, "user", "t1", "go-1", "t1"),
    (2, "assistant", "t1", "step", "t1"),
    (3, "tool", "t1", "STUB", "t1"),
    (5, "user", "t2", "go-2", None),
    (6, "assistant", "t2", "step", None),
    (7, "tool", "t2", "STUB", None),
    (8, "tool", "t2", "live", None),
    (9, "assistant", "t2", "done-2", None),
    (10, "user", "t3", "go-3", None),
    (11, "assistant", "t3", "step", None),
    (12, "tool", "t3", "STUB", None),
    (13, "assistant", "t3", "done-3", None),
)

#: seq 4 stays free so an interleaved row can be placed before the scope.
STRAY_SEQ = 4

#: Rows a scope starting at seq 5 must still report from the covered region.
COVERED_STUB_SEQS: tuple[int, ...] = (3,)


def _content(body: str) -> str:
    return (
        f"{TOOL_TRIM_MARKER} after 30d retention — 9 chars, 1 lines]"
        if body == "STUB" else "out"
    )


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    db.execute(
        "INSERT INTO threads (id,user_id,title,next_message_seq,created_at,updated_at) "
        "VALUES (?,?,'T',20,'t0','t0')",
        (THREAD, "u"),
    )
    for turn_order, turn_id in enumerate(("t1", "t2", "t3"), start=1):
        db.execute(
            "INSERT INTO active_turns (turn_order,id,thread_id,execution_epoch,model,"
            "status,started_at,ended_at,boot_epoch) VALUES (?,?,?,0,'m','completed','s','e','b')",
            (turn_order, turn_id, THREAD),
        )
    for seq, role, turn_id, body, column_turn in TRANSCRIPT:
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,"
            "delivery_state,active_turn_id,seq) VALUES (?,?,?,?,?,'t','delivered',?,?)",
            (
                f"m{seq}", THREAD, role, _content(body),
                json.dumps({"turn_id": turn_id}), column_turn, seq,
            ),
        )
    db.commit()
    return db


def _scope_from(db: sqlite3.Connection, first_seq: int) -> ScopedSource:
    """The scope a boundary at ``first_seq`` - 1 reads, built row for row."""
    rows = db.execute(
        "SELECT id,thread_id,role,content,metadata,created_at,seq,active_turn_id "
        "FROM messages WHERE thread_id=? AND delivery_state='delivered' AND seq>=? "
        "ORDER BY seq",
        (THREAD, first_seq),
    ).fetchall()
    return ScopedSource(
        rows=validate_source_rows(rows, THREAD),
        predecessor=None,
        starts_bound_turn=True,
    )


def test_stub_pass_reports_the_covered_region_stubs():
    """The pass keeps returning covered stubs; it only stopped walking for them."""
    with closing(_db()) as db:
        assert scan_before_scope(db, THREAD, _scope_from(db, 5)) == COVERED_STUB_SEQS
        # A scope at the head sees every stub the thread holds, in seq order:
        # seq 12 belongs to the scope itself, so it is not a covered stub.
        assert scan_before_scope(db, THREAD, _scope_from(db, 10)) == (3, 7)


def test_stub_pass_leaves_unpositioned_rows_out():
    """A delivered stub with no position is outside the token."""
    with closing(_db()) as db:
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,"
            "delivery_state,seq) VALUES ('m-null',?,'tool',?,'{}','t','delivered',NULL)",
            (THREAD, _content("STUB")),
        )
        db.commit()
        assert scan_before_scope(db, THREAD, _scope_from(db, 5)) == COVERED_STUB_SEQS


@pytest.mark.parametrize("half", ["column", "metadata"])
def test_turn_with_rows_on_both_sides_still_refuses(half: str):
    """Both provenance halves still find an interleaved Turn and refuse it."""
    with closing(_db()) as db:
        if half == "column":
            db.execute(
                "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,"
                "delivery_state,active_turn_id,seq) "
                "VALUES ('m-stray',?,'assistant','early',?,'t','delivered','t2',?)",
                (THREAD, json.dumps({"turn_id": "t2"}), STRAY_SEQ),
            )
        else:
            db.execute(
                "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,"
                "delivery_state,active_turn_id,seq) "
                "VALUES ('m-stray',?,'assistant','early',?,'t','delivered',NULL,?)",
                (THREAD, json.dumps({"turn_id": "t2"}), STRAY_SEQ),
            )
        db.commit()
        with pytest.raises(PortableHistoryError, match="Turn 't2' is interleaved"):
            scan_before_scope(db, THREAD, _scope_from(db, 5))


SWEEP_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _run_retention_sweep(db: sqlite3.Connection) -> int:
    """Run the shipped tool-payload trim and return how many rows it rewrote."""
    from core import thread_lifecycle_trims
    from core.thread_lifecycle import LifecycleConfig, RetentionReport, _iso

    db.execute(
        "UPDATE messages SET created_at=? WHERE thread_id=?",
        (_iso(SWEEP_NOW - timedelta(days=40)), THREAD),
    )
    db.commit()
    report = RetentionReport()
    thread_lifecycle_trims._trim_tool_payloads(
        db, SWEEP_NOW, LifecycleConfig(tool_trim_after_days=30), report
    )
    return int(report.tool_trimmed)


def _cover_with_checkpoint(db: sqlite3.Connection, through_seq: int) -> None:
    """One installed checkpoint over seq <= ``through_seq``, as retention sees it."""
    from core.thread_lifecycle import _iso

    boundary = db.execute(
        "SELECT id FROM messages WHERE thread_id=? AND seq=?", (THREAD, through_seq)
    ).fetchone()
    epoch = db.execute(
        "SELECT history_epoch FROM threads WHERE id=?", (THREAD,)
    ).fetchone()[0]
    db.execute(
        "INSERT INTO checkpoints "
        "(id,thread_id,history_epoch,through_seq,through_message_id,trigger,mode,"
        "status,source_harness,source_model,writer_harness,writer_model,"
        "continuity_base_revision,continuity_base_snapshot_json,raw_result,"
        "started_at,captured_at,installed_at,ended_at) "
        "VALUES (?,?,?,?,?,'pressure','warm','installed','native','test-model',"
        "'native','test-model',0,'[]','retained checkpoint',?,?,?,?)",
        (f"{THREAD}-cp-{epoch}-{through_seq}", THREAD, epoch, through_seq, boundary["id"],
         *[_iso(SWEEP_NOW)] * 4),
    )
    db.commit()


def _stub_seqs(db: sqlite3.Connection) -> tuple[int, ...]:
    """Every stub position in the thread, by the marker's own SQL text."""
    from infra.tool_retention_marker import TOOL_TRIM_CONTENT_SQL

    return tuple(
        int(row[0])
        for row in db.execute(
            f"SELECT seq FROM messages WHERE thread_id=? AND seq IS NOT NULL "
            f"AND {TOOL_TRIM_CONTENT_SQL} ORDER BY seq",
            (THREAD,),
        )
    )


def test_the_real_sweep_stubs_exactly_the_rows_the_stub_pass_finds():
    """Sweep, reader and index are one contract, so all three run in this test.

    The sweep, the reader and ``idx_messages_retention_stub`` all test the
    marker through ``TOOL_TRIM_CONTENT_SQL``, the sweep negated because it is
    looking for payloads that are not stubs yet, so a marker change cannot serve
    one of them and not the others. What still needs proving is that the negation
    and the index behave as written, so this runs the shipped trim, then asks the
    shipped pass what it sees and how SQLite served it.
    """
    with closing(_db()) as db:
        _cover_with_checkpoint(db, through_seq=9)
        # m3, m7 and m12 arrived already stubbed; m8 is the covered tool payload
        # still holding its bytes, and it is the only row the sweep rewrites.
        assert _run_retention_sweep(db) == 1
        assert _stub_seqs(db) == (3, 7, 8, 12)
        # The pass reports the swept row as covered history, in seq order, and
        # nothing from the scope itself (seq 12 sits above this scope).
        assert scan_before_scope(db, THREAD, _scope_from(db, 10)) == (3, 7, 8)

        seen: list[str] = []
        db.set_trace_callback(seen.append)
        try:
            scan_before_scope(db, THREAD, _scope_from(db, 10))
        finally:
            db.set_trace_callback(None)
        stub_selects = [sql for sql in seen if TOOL_TRIM_MARKER in sql]
        assert stub_selects, f"the stub pass did not run: {seen}"
        plan = [str(row[3]) for row in db.execute("EXPLAIN QUERY PLAN " + stub_selects[0])]
        assert any("idx_messages_retention_stub" in step for step in plan), plan
