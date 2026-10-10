"""A range after a legal boundary renders exactly as it did from seq 0.

The reference below runs the same projection over every delivered row, which
is what a tail render did before scoped reads. For each history shape, each
legal cut, and both unfinished-Turn modes, the scoped render must produce the
identical projection, or the identical error.
"""

from __future__ import annotations

import json
import re
import sqlite3

import pytest

from core.thread_lifecycle import TOOL_TRIM_MARKER
from history.types import HistoryBoundary

THREAD = "thread-1"
EPOCH = 7


class _History:
    """Build one durable transcript row by row in an in-memory schema."""

    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE threads (id TEXT PRIMARY KEY, history_epoch INTEGER NOT NULL);
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, role TEXT NOT NULL,
                content TEXT NOT NULL, metadata TEXT, created_at TEXT NOT NULL,
                delivery_state TEXT NOT NULL DEFAULT 'delivered', seq INTEGER,
                active_turn_id TEXT, UNIQUE(thread_id, seq)
            );
            CREATE TABLE active_turns (
                turn_order INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                thread_id TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
                ended_at TEXT
            );
            """
        )
        self.db.execute("INSERT INTO threads VALUES (?, ?)", (THREAD, EPOCH))
        self.seq = 0
        self.turns = 0

    def turn(self, status: str = "completed") -> str:
        self.turns += 1
        turn_id = f"t{self.turns}"
        self.db.execute(
            "INSERT INTO active_turns (id,thread_id,status,started_at) VALUES (?,?,?,'s')",
            (turn_id, THREAD, status),
        )
        return turn_id

    def row(self, role, turn_id, content="body", *, meta=None, legacy=None) -> str:
        self.seq += 1
        message_id = f"m{self.seq}"
        metadata = dict(meta or {})
        if legacy is not None:
            metadata["turn_unbound_reason"] = legacy
        elif turn_id is not None:
            metadata["turn_id"] = turn_id
        self.db.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq,"
            "active_turn_id) VALUES (?,?,?,?,?,'c',?,?)",
            (message_id, THREAD, role, content, json.dumps(metadata), self.seq,
             None if legacy is not None else turn_id),
        )
        return message_id

    def step(self, turn_id, *calls, legacy=None) -> str:
        declared = [
            {"id": call, "type": "function", "function": {"name": "Read", "arguments": "{}"}}
            for call in calls
        ]
        return self.row("assistant", turn_id, "step", meta={"llm_tool_calls": declared},
                        legacy=legacy)

    def result(self, turn_id, step_id, call, content="out", **extra) -> str:
        meta = {"tool_call_id": call, "assistant_step_id": step_id, "name": "Read", **extra}
        return self.row("tool", turn_id, content, meta=meta)


def _tool_turns(history: _History, count: int, *, trimmed: frozenset[int] = frozenset()) -> None:
    for index in range(count):
        turn = history.turn()
        history.row("user", turn, "go")
        step = history.step(turn, f"{turn}-c1", f"{turn}-c2")
        body = f"{TOOL_TRIM_MARKER} after 30d retention — 9 chars, 1 lines]" if index in trimmed else "out"
        history.result(turn, step, f"{turn}-c1", body)
        history.result(turn, step, f"{turn}-c2")
        history.row("assistant", turn, "done")


def _shape_tool_turns() -> _History:
    history = _History()
    _tool_turns(history, 3)
    cancelled = history.turn("cancelled")
    history.row("user", cancelled, "stop")
    step = history.step(cancelled, "x1")
    history.result(cancelled, step, "x1", "cancelled", synthetic_tool_result=True,
                   synthetic_reason="user_interruption")
    errored = history.turn("error")
    history.row("user", errored, "lost")
    history.step(errored, "lost-1", "lost-2")
    running = history.turn("running")
    history.row("user", running, "continue")
    step = history.step(running, "r1")
    history.result(running, step, "r1")
    history.step(running, "r2")
    return history


def _shape_inverted_and_quarantined() -> _History:
    history = _History()
    _tool_turns(history, 2)
    turn = history.turn()
    history.row("user", turn, "go")
    first = history.step(turn, "i1", "i2")
    history.result(turn, first, "i1")
    second = history.step(turn, "i3")
    history.result(turn, second, "i3")
    history.result(turn, first, "i2", "late", synthetic_tool_result=True,
                   synthetic_reason="tool_result_missing")
    history.row("user", turn, "more")
    third = history.step(turn, "q1")
    history.result(turn, third, "q1", "partial", tool_result_binding_state="quarantined_unbound")
    history.row("assistant", turn, "done")
    _tool_turns(history, 2)
    return history


def _shape_events_and_legacy() -> _History:
    history = _History()
    history.row("user", None, "imported", legacy="legacy_import")
    history.row("assistant", None, "imported answer", legacy="legacy_import")
    history.row("assistant", None, "more imported", legacy="legacy_import")
    history.row("user", None, "second import", legacy="pre_turn_binding_migration")
    history.row("assistant", None, "answer", legacy="pre_turn_binding_migration")
    _tool_turns(history, 2)
    woken = history.turn()
    history.row("system", woken, "wake", meta={"type": "context_event", "event_disposition": "wake"})
    step = history.step(woken, "w1")
    history.result(woken, step, "w1")
    history.row("system", woken, "turn complete", meta={"type": "result"})
    history.row("assistant", woken, "ack")
    history.row("system", woken, "late audit", meta={"type": "audit"})
    _tool_turns(history, 2)
    return history


def _shape_retention() -> _History:
    history = _History()
    _tool_turns(history, 6, trimmed=frozenset({0, 3}))
    return history


def _shape_running_input_only() -> _History:
    history = _History()
    _tool_turns(history, 3)
    running = history.turn("running")
    history.row("user", running, "next request")
    return history


def _shape_no_steps() -> _History:
    history = _History()
    for status in ("cancelled", "error"):
        turn = history.turn(status)
        history.row("user", turn, "never answered")
    running = history.turn("running")
    history.row("user", running, "still waiting")
    return history


SHAPES = {
    "tool_turns": _shape_tool_turns,
    "inverted_and_quarantined": _shape_inverted_and_quarantined,
    "events_and_legacy": _shape_events_and_legacy,
    "retention": _shape_retention,
    "running_input_only": _shape_running_input_only,
    "no_steps": _shape_no_steps,
}


def _outcome(render):
    try:
        return ("ok", render())
    except Exception as error:  # noqa: BLE001 - equivalence compares the exact failure
        return ("error", type(error).__name__, str(error))


def _compare(monkeypatch, db, start, include_unfinished):
    import history.projection as projection

    def render():
        return projection.render_portable_history(
            THREAD, db=db, start_exclusive=start, include_unfinished_turn=include_unfinished,
        )

    scoped = _outcome(render)
    full_read = projection._read_source
    with monkeypatch.context() as patch:
        patch.setattr(projection, "_read_source", lambda d, t, _s: full_read(d, t, None))
        reference = _outcome(render)
    assert scoped == reference, (start, include_unfinished)
    return scoped


def _legal_boundaries(db) -> list[HistoryBoundary]:
    from history.boundaries import _legal_slice_boundaries
    from history.projection import render_portable_history

    full = render_portable_history(THREAD, db=db, include_unfinished_turn=True)
    return sorted(_legal_slice_boundaries(full), key=lambda boundary: boundary.seq)


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("include_unfinished", (True, False))
def test_scoped_range_matches_full_read_at_every_cut(monkeypatch, shape, include_unfinished):
    db = SHAPES[shape]().db
    outcomes = [
        _compare(monkeypatch, db, boundary, include_unfinished)
        for boundary in _legal_boundaries(db)
    ]
    assert any(outcome[0] == "ok" for outcome in outcomes)


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_scoped_range_rejects_every_non_cut_exactly_like_full_read(monkeypatch, shape):
    db = SHAPES[shape]().db
    legal = set(_legal_boundaries(db))
    for row in db.execute("SELECT id, seq FROM messages ORDER BY seq").fetchall():
        boundary = HistoryBoundary(THREAD, EPOCH, row["seq"], row["id"])
        if boundary not in legal:
            assert _compare(monkeypatch, db, boundary, True)[0] == "error"
    for wrong in (
        HistoryBoundary("other", EPOCH, 1, "m1"),
        HistoryBoundary(THREAD, EPOCH - 1, 1, "m1"),
        HistoryBoundary(THREAD, EPOCH, 1, "not-m1"),
        HistoryBoundary(THREAD, EPOCH, 10_000, "m10000"),
    ):
        assert _compare(monkeypatch, db, wrong, True)[0] == "error"


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_checkpoint_tail_reports_the_exact_replay_seal_without_reading_it(shape):
    from history.projection import render_checkpoint_tail, render_portable_history

    db = SHAPES[shape]().db
    exact = render_portable_history(THREAD, db=db)
    compared = 0
    for boundary in _legal_boundaries(db):
        try:
            expected_tail = render_portable_history(
                THREAD, db=db, start_exclusive=boundary, include_unfinished_turn=True,
            )
        except Exception as error:  # noqa: BLE001 - the pair must fail identically
            with pytest.raises(type(error), match=re.escape(str(error))):
                render_checkpoint_tail(THREAD, db=db, start_exclusive=boundary)
            continue
        tail, sealed = render_checkpoint_tail(THREAD, db=db, start_exclusive=boundary)
        assert tail == expected_tail
        assert sealed == exact.sealed_boundary, boundary
        compared += 1
    assert compared


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_checkpoint_boundary_choice_matches_the_seq0_projection(shape):
    from history.boundaries import boundary_before_latest_assistant_step
    from history.projection import render_portable_history
    from history.scoped_source import assistant_step_through
    from history.types import PortableHistoryError
    from subagents.harness_checkpoint_context import _external_boundary

    db = SHAPES[shape]().db
    full = render_portable_history(THREAD, db=db, include_unfinished_turn=True)
    current_turns = (None, *(turn.turn_id for turn in full.turns), "absent-turn")
    for floor in _legal_boundaries(db):
        try:
            scoped = render_portable_history(
                THREAD, db=db, start_exclusive=floor, include_unfinished_turn=True,
            )
        except PortableHistoryError:
            continue
        precedes = assistant_step_through(db, THREAD, floor.seq)
        assert boundary_before_latest_assistant_step(
            scoped, floor=floor, step_precedes_start=precedes,
        ) == boundary_before_latest_assistant_step(full, floor=floor), floor
        for current in current_turns:
            assert _external_boundary(
                scoped, floor, current_turn_id=current, step_precedes_start=precedes,
            ) == _external_boundary(full, floor, current_turn_id=current), (floor, current)


def test_retention_stubs_stay_thread_wide_before_inside_and_after_the_range(monkeypatch):
    db = _shape_retention().db
    boundaries = _legal_boundaries(db)
    before = _compare(monkeypatch, db, boundaries[-1], True)
    inside = _compare(monkeypatch, db, boundaries[0], True)

    assert before[0] == "ok"
    assert before[1].retention_stub_seqs == (3, 18)
    assert before[1].full_raw_replay_available is False
    assert inside[:2] == ("error", "PortableHistoryUnavailableError")


def test_unsealed_predecessor_still_fails_the_scoped_range(monkeypatch):
    history = _History()
    _tool_turns(history, 1)
    stuck = history.turn("running")
    history.row("user", stuck, "stuck")
    later = history.turn()
    anchor = history.row("user", later, "later")
    step = history.step(later, "l1")
    history.result(later, step, "l1")
    history.row("assistant", later, "done")

    assert anchor == "m7"
    outcome = _compare(
        monkeypatch, history.db, HistoryBoundary(THREAD, EPOCH, 7, anchor), True,
    )
    assert outcome == ("error", "PortableHistoryError", "unsealed Turn precedes later sealed history")


def _interleaved_before_predecessor(*, early: str) -> tuple[_History, HistoryBoundary]:
    """Seed ``early`` Turn history before t1, the Turn preceding t2's owning segment."""
    history = _History()
    first, owning, later = history.turn(), history.turn(), history.turn()
    early_turn = {"owning": owning, "later": later}[early]
    history.step(early_turn)
    history.row("user", first, "first")
    history.row("assistant", first, "first done")
    history.row("user", owning, "owning")
    boundary = history.row("assistant", owning, "owning done")
    history.row("user", later, "later")
    history.row("assistant", later, "later done")
    return history, HistoryBoundary(THREAD, EPOCH, 5, boundary)


@pytest.mark.parametrize("early", ("owning", "later"))
def test_scoped_turn_with_history_before_the_predecessor_still_fails(early):
    """A Turn the scope reads is interleaved when it also has rows before the scope."""
    import history.projection as projection
    from history.types import PortableHistoryError

    history, boundary = _interleaved_before_predecessor(early=early)

    with pytest.raises(PortableHistoryError, match="Turn order conflicts"):
        projection.render_portable_history(THREAD, db=history.db)
    for include_unfinished in (True, False):
        with pytest.raises(PortableHistoryError, match="is interleaved"):
            projection.render_portable_history(
                THREAD, db=history.db, start_exclusive=boundary,
                include_unfinished_turn=include_unfinished,
            )
    with pytest.raises(PortableHistoryError, match="is interleaved"):
        projection.render_checkpoint_tail(THREAD, db=history.db, start_exclusive=boundary)


def test_scoped_turn_named_only_by_its_column_or_a_context_event_still_fails():
    """Both Turn provenance sources, and wake rows seq 0 treats as input, are checked."""
    import history.projection as projection
    from history.types import PortableHistoryError

    history, boundary = _interleaved_before_predecessor(early="owning")
    history.db.execute("UPDATE messages SET metadata='{}' WHERE id='m1'")
    history.db.execute("UPDATE messages SET active_turn_id='t2' WHERE id='m1'")
    with pytest.raises(PortableHistoryError, match="'t2' is interleaved"):
        projection.render_portable_history(THREAD, db=history.db, start_exclusive=boundary)

    history.db.execute(
        "UPDATE messages SET role='system', active_turn_id=NULL, metadata=? WHERE id='m1'",
        (json.dumps({"type": "context_event", "event_disposition": "wake", "turn_id": "t2"}),),
    )
    with pytest.raises(PortableHistoryError, match="'t2' is interleaved"):
        projection.render_portable_history(THREAD, db=history.db, start_exclusive=boundary)

    history.db.execute(
        "UPDATE messages SET metadata=? WHERE id='m1'",
        (json.dumps({"type": "audit", "turn_id": "t2"}),),
    )
    assert projection.render_portable_history(
        THREAD, db=history.db, start_exclusive=boundary,
    ).start_exclusive == boundary


def test_corruption_only_in_covered_history_no_longer_blocks_the_range(monkeypatch):
    """Approved in #1588 D1: covered Turns before B's owning segment are not revalidated."""
    import history.projection as projection

    history = _History()
    _tool_turns(history, 2)
    history.db.execute(
        "UPDATE messages SET metadata='not json' WHERE id='m3'"
    )
    boundary = HistoryBoundary(THREAD, EPOCH, 10, "m10")

    scoped = projection.render_portable_history(THREAD, db=history.db, start_exclusive=boundary)
    assert scoped.turns == ()
    with pytest.raises(Exception, match="malformed metadata"):
        projection.render_portable_history(THREAD, db=history.db)


class _RowsToPython:
    """Count the rows a connection hands to Python, however they are fetched."""

    def __init__(self, db) -> None:
        self._db = db
        self.rows = 0

    def execute(self, sql, params=()):
        counter = self
        cursor = self._db.execute(sql, params)

        class _Counted:
            def fetchone(self):
                row = cursor.fetchone()
                counter.rows += row is not None
                return row

            def fetchall(self):
                rows = cursor.fetchall()
                counter.rows += len(rows)
                return rows

            def __iter__(self):
                for row in cursor:
                    counter.rows += 1
                    yield row

            def close(self):
                cursor.close()

        return _Counted()


def test_scan_before_scope_names_every_prior_stub_in_one_row_to_python():
    from history.scoped_source import scan_before_scope, scoped_source

    history = _History()
    _tool_turns(history, 8, trimmed=frozenset({0, 3, 4}))
    boundary = _legal_boundaries(history.db)[-1]
    scoped = scoped_source(history.db, THREAD, boundary)
    counted = _RowsToPython(history.db)

    assert scan_before_scope(counted, THREAD, scoped) == (3, 18, 23)
    assert counted.rows == 1


def test_scan_before_scope_without_a_prior_stub_is_empty():
    from history.scoped_source import scan_before_scope, scoped_source

    history = _History()
    _tool_turns(history, 4)
    boundary = _legal_boundaries(history.db)[-1]
    scoped = scoped_source(history.db, THREAD, boundary)

    assert scan_before_scope(history.db, THREAD, scoped) == ()
