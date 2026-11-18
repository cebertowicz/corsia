"""A worker's completion costs its parent's Turn-end the same at any parent history size.

``finish_turn`` checks for an earlier completion inside its write
transaction, so every other writer waits on that check. It must find the
completion by its identity, never by reading the parent's whole history.
"""

from __future__ import annotations

import json
import sqlite3

PARENT, CHILD, TURN = "parent-1", "child-1", "child-turn-1"


def _world(parent_history: int) -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,model,execution_epoch,created_at,updated_at) "
        "VALUES (?,'user-1','claude-code/opus',0,'t0','t0')",
        (PARENT,),
    )
    db.execute(
        "INSERT INTO threads (id,user_id,model,execution_epoch,created_at,updated_at,spawned_by) "
        "VALUES (?,'user-1','claude-code/sonnet',0,'t0','t0',?)",
        (CHILD, PARENT),
    )
    metadata = json.dumps({"turn_id": "parent-turn", "note": "x" * 400})
    db.executemany(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,?,'assistant','earlier work',?,?,?)",
        ((f"p-{n}", PARENT, metadata, f"t{n:08d}", n + 1) for n in range(parent_history)),
    )
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES (?,?,0,'claude-code/sonnet','completed','2099-01-01','boot')",
        (TURN, CHILD),
    )
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES ('c-1',?,'assistant','Done: the answer.',?,'2099-01-02',1)",
        (CHILD, json.dumps({"turn_id": TURN})),
    )
    db.commit()
    return db


def _steps(db: sqlite3.Connection) -> tuple[str | None, int]:
    """Run the completion and count the SQLite VM steps it takes."""
    from infra.thread_driver import _parent_completion

    steps = 0

    def tick() -> int:
        nonlocal steps
        steps += 1
        return 0

    db.set_progress_handler(tick, 1000)
    try:
        return _parent_completion(db, TURN, CHILD), steps
    finally:
        db.set_progress_handler(None, 0)


def test_completion_is_queued_once_and_costs_no_more_with_a_long_parent_history() -> None:
    small, large = _world(10), _world(20_000)
    try:
        first = []
        for db in (small, large):
            first.append(_steps(db))
            assert first[-1][0] == PARENT
            rows = db.execute(
                "SELECT id,content FROM messages WHERE thread_id=? AND id LIKE 'parent-completion:%'",
                (PARENT,),
            ).fetchall()
            assert [tuple(row) for row in rows] == [
                (f"parent-completion:{TURN}", "Subagent finished:\n\nDone: the answer."),
            ]
        # The repeat (a retried Turn end) finds the queued completion and adds nothing.
        small_repeat, large_repeat = _steps(small), _steps(large)
        assert small_repeat[0] is None and large_repeat[0] is None
        assert large.execute(
            "SELECT count(*) FROM messages WHERE id LIKE 'parent-completion:%'"
        ).fetchone()[0] == 1
        # 20,000 parent rows must not show up as work: the check is one keyed read.
        assert first[1][1] <= first[0][1] + 5, first
        assert large_repeat[1] <= small_repeat[1] + 5, (small_repeat, large_repeat)
    finally:
        small.close()
        large.close()
