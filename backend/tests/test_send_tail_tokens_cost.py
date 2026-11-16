"""Sizing a warm send's tail costs the same at any thread history size.

Every send to an idle thread with a live Claude Code or Codex session reads the
tool results of the last measured step. Production databases carry no planner
statistics, so the read must reach that step's rows through the tool-lineage
index, never by walking the whole thread in ``seq`` order.
"""

from __future__ import annotations

import json
import sqlite3

THREAD, STEP = "thread-1", "step-1"


def _world(history: int) -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,model,execution_epoch,created_at,updated_at) "
        "VALUES (?,'user-1','claude-code/sonnet',0,'t0','t0')",
        (THREAD,),
    )
    # Earlier history: assistant steps, each with a tool result of its own.
    rows = []
    for n in range(history):
        role, meta = (
            ("assistant", {"turn_id": "t"}) if n % 2 == 0
            else ("tool", {"turn_id": "t", "assistant_step_id": f"old-{n - 1}", "note": "x" * 400})
        )
        rows.append((f"old-{n}", THREAD, role, "earlier", json.dumps(meta), f"t{n:08d}", n + 1))
    rows.append((STEP, THREAD, "assistant", "done", json.dumps({"turn_id": "t"}), "u0", history + 1))
    for k in range(2):
        meta = json.dumps({"turn_id": "t", "assistant_step_id": STEP})
        rows.append((f"result-{k}", THREAD, "tool", "tool result", meta, f"u{k + 1}", history + 2 + k))
    db.executemany(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    db.commit()
    return db


class _Recorder:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db, self.statements = db, []

    def execute(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        return self.db.execute(sql, params)


def _steps(db: sqlite3.Connection) -> tuple[int | None, int, list]:
    """Size the tail and count the SQLite VM steps it takes."""
    from infra.send_replay_reads import _terminal_tail_tokens

    steps = 0

    def tick() -> int:
        nonlocal steps
        steps += 1
        return 0

    recorder = _Recorder(db)
    db.set_progress_handler(tick, 1000)
    try:
        tokens = _terminal_tail_tokens(recorder, THREAD, STEP)
    finally:
        db.set_progress_handler(None, 0)
    return tokens, steps, recorder.statements


def test_tail_read_costs_no_more_with_a_long_thread_and_uses_the_lineage_index() -> None:
    from core.token_estimation import _estimate_tokens

    small, large = _world(10), _world(20_000)
    try:
        expected = _estimate_tokens("done") + 4 + 2 * (_estimate_tokens("tool result") + 4)
        (small_tokens, small_steps, _), (large_tokens, large_steps, statements) = _steps(small), _steps(large)
        assert small_tokens == large_tokens == expected
        # 20,000 earlier rows must not show up as work: the read seeks this step's results.
        assert large_steps <= small_steps + 5, (small_steps, large_steps)

        assert not large.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'").fetchone()
        sql, params = statements[-1]
        assert "assistant_step_id" in sql
        plan = " ".join(str(row[-1]) for row in large.execute("EXPLAIN QUERY PLAN " + sql, params))
        assert "idx_messages_tool_lineage" in plan, plan
        assert "idx_messages_session_seq" not in plan, plan
    finally:
        small.close()
        large.close()
