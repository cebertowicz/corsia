"""Tool-boundary maintenance and send preflight read only the history they need.

Two threads share one tail shape after an installed checkpoint and differ only
in how much covered history precedes it. Every read below must materialize the
same number of message rows for both threads: covered history is not reloaded
on each settled tool step or send.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from core.checkpoint_resume import CheckpointWaitScope
from core.turn_core import TurnIdentity
from llm.resolution import ExecutionTarget

SMALL, LARGE = "slice-small", "slice-large"
_PREFIX_TURNS = {SMALL: 3, LARGE: 60}
_TURN_ORDER_BASE = {SMALL: 0, LARGE: 1000}


def _target(harness: str) -> ExecutionTarget:
    return ExecutionTarget(
        catalog_id=f"{harness}/slice-reads",
        harness=harness,
        provider={"claude-code": "anthropic", "codex": "openai"}.get(harness, "omlx"),
        backend="cli" if harness in {"claude-code", "codex"} else "network",
        transport_model="slice-reads",
        expected_reported_model=None,
        context_window=200_000,
        available=True,
        availability_reason=None,
        display_name="slice-reads",
        supports_reasoning=True,
        preserve_thinking_default=True,
    )


class _CountingCursor:
    """Count rows a messages query hands back to Python."""

    def __init__(self, cursor, counter):
        self._cursor, self._counter = cursor, counter

    def fetchone(self):
        row = self._cursor.fetchone()
        self._counter.add(0 if row is None else 1)
        return row

    def fetchall(self):
        rows = self._cursor.fetchall()
        self._counter.add(len(rows))
        return rows

    def fetchmany(self, size=None):
        rows = self._cursor.fetchmany() if size is None else self._cursor.fetchmany(size)
        self._counter.add(len(rows))
        return rows

    def __iter__(self):
        for row in self._cursor:
            self._counter.add(1)
            yield row

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _CountingConnection:
    """Proxy one pooled connection and count message rows materialized."""

    def __init__(self, connection, counter):
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_counter", counter)

    def execute(self, sql, *args):
        cursor = self._connection.execute(sql, *args)
        if "messages" in " ".join(str(sql).lower().split()).split(" from ", 1)[-1]:
            return _CountingCursor(cursor, self._counter)
        return cursor

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __setattr__(self, name, value):
        setattr(self._connection, name, value)


class _RowCounter:
    def __init__(self):
        self._lock = threading.Lock()
        self.rows = 0

    def add(self, count: int) -> None:
        with self._lock:
            self.rows += count

    def reset(self) -> int:
        with self._lock:
            rows, self.rows = self.rows, 0
        return rows


@pytest.fixture
def row_counter(isolated_db, monkeypatch):
    import infra.database as database

    counter = _RowCounter()
    real_get_db = database.get_db
    monkeypatch.setattr(
        database, "get_db", lambda: _CountingConnection(real_get_db(), counter),
    )
    return counter


def _insert(db, message_id, thread_id, seq, role, content, turn_id, meta=None):
    metadata = {"turn_id": turn_id, **(meta or {})}
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq,"
        "active_turn_id) VALUES (?,?,?,?,?,'t',?,?)",
        (message_id, thread_id, role, content, json.dumps(metadata), seq, turn_id),
    )


def _tool_step(db, thread_id, turn_id, seq, step_id, call_count):
    calls = [
        {"id": f"{step_id}-call-{index}", "type": "function",
         "function": {"name": "read_file", "arguments": "{}"}}
        for index in range(call_count)
    ]
    _insert(db, step_id, thread_id, seq, "assistant", "checking", turn_id,
            {"llm_tool_calls": calls})
    for index in range(call_count):
        seq += 1
        _insert(db, f"{step_id}-result-{index}", thread_id, seq, "tool", "x" * 2000,
                turn_id, {"tool_call_id": f"{step_id}-call-{index}",
                          "assistant_step_id": step_id, "name": "read_file"})
    return seq


def _register_adopted_handle(thread_id: str, harness: str) -> None:
    """Register the live provider handle as already adopted onto the floor."""
    from infra.database import get_db
    from registry.service import register_cc_session, register_codex_thread

    register = {"claude-code": register_cc_session, "codex": register_codex_thread}.get(harness)
    if register is None:
        return
    db = get_db()
    register(thread_id, f"{thread_id}-provider", checkpoint_generation_id=f"{thread_id}-floor", db=db)
    db.commit()
    db.close()


def _seed(thread_id: str, model: str, harness: str) -> None:
    """Seed covered history, an installed checkpoint, and one live tool step."""
    from infra.database import get_db

    db = get_db()
    base, seq = _TURN_ORDER_BASE[thread_id], 0
    turns: list[tuple[object, ...]] = []
    db.execute(
        "INSERT INTO threads (id,user_id,model,history_epoch,next_message_seq,"
        "created_at,updated_at) VALUES (?, 'user-1', ?, 0, 1, 't', 't')",
        (thread_id, model),
    )
    for index in range(_PREFIX_TURNS[thread_id]):
        turn_id = f"{thread_id}-covered-{index}"
        turns.append((turn_id, thread_id, 0, base + index + 1, model, "completed", "t", "t", "boot"))
        seq += 1
        _insert(db, f"{turn_id}-user", thread_id, seq, "user", "go", turn_id)
        seq = _tool_step(db, thread_id, turn_id, seq + 1, f"{turn_id}-step", 2)
        seq += 1
        _insert(db, f"{turn_id}-final", thread_id, seq, "assistant", "done", turn_id)
    floor_seq, floor_id = seq, f"{thread_id}-covered-{_PREFIX_TURNS[thread_id] - 1}-final"
    live_turn = f"{thread_id}-live"
    turns.append((live_turn, thread_id, 0, base + _PREFIX_TURNS[thread_id] + 1, model,
                  "running", "t", None, "boot"))
    seq += 1
    _insert(db, f"{live_turn}-user", thread_id, seq, "user", "continue", live_turn)
    seq = _tool_step(db, thread_id, live_turn, seq + 1, f"{thread_id}-live-step-1", 1)
    db.executemany(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,turn_order,model,status,"
        "started_at,ended_at,boot_epoch) VALUES (?,?,?,?,?,?,?,?,?)", turns,
    )
    db.execute(
        "INSERT INTO checkpoints (id,thread_id,history_epoch,through_seq,through_message_id,"
        "trigger,mode,status,source_harness,source_model,writer_harness,writer_model,"
        "continuity_base_revision,continuity_base_snapshot_json,raw_result,"
        "continuity_snapshot_json,started_at,captured_at,installed_at,ended_at) "
        "VALUES (?,?,0,?,?,'pressure','warm','installed',?,?,?,?,0,'[]',"
        "'prior','[]','t','t','t','t')",
        (f"{thread_id}-floor", thread_id, floor_seq, floor_id, harness, model, harness, model),
    )
    db.execute("UPDATE threads SET next_message_seq=? WHERE id=?", (seq + 1, thread_id))
    db.commit()
    db.close()
    _register_adopted_handle(thread_id, harness)


def _settle_next_step(thread_id: str, step: int) -> None:
    from infra.database import get_db

    db = get_db()
    seq = db.execute("SELECT max(seq) FROM messages WHERE thread_id=?", (thread_id,)).fetchone()[0]
    _tool_step(db, thread_id, f"{thread_id}-live", seq + 1, f"{thread_id}-live-step-{step}", 1)
    db.commit()
    db.close()


def _seed_both(monkeypatch, harness: str) -> ExecutionTarget:
    target = _target(harness)
    for thread_id in (SMALL, LARGE):
        _seed(thread_id, target.catalog_id, harness)
    monkeypatch.setattr("llm.resolution.resolve_execution_target", lambda *_a, **_k: target)
    monkeypatch.setattr(
        "subagents.codex_runtime_support.resolve_working_dir", lambda *_a, **_k: "/tmp",
    )
    monkeypatch.setattr("core.checkpoint_render.checkpoint_binary", lambda *_a, **_k: None)
    return target


def _rows_for_both(counter: _RowCounter, read) -> dict[str, int]:
    counts = {}
    for thread_id in (SMALL, LARGE):
        counter.reset()
        read(thread_id)
        counts[thread_id] = counter.reset()
    return counts


def test_checkpoint_tail_replay_does_not_reload_covered_history(row_counter, monkeypatch):
    from core.checkpoint_resume import installed_replay
    from infra.checkpoint_queries import current_installed

    _seed_both(monkeypatch, "codex")

    counts = _rows_for_both(
        row_counter, lambda thread_id: installed_replay(current_installed(thread_id)),
    )

    assert counts[SMALL] == counts[LARGE]


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ("claude-code", "codex"))
async def test_repeated_external_tool_steps_do_not_reload_covered_history(
    row_counter, monkeypatch, harness,
):
    from subagents import harness_checkpoint_resume as bridge

    target = _seed_both(monkeypatch, harness)
    monkeypatch.setattr(bridge, "external_measurement", lambda *_a: (1_000, 200_000))
    per_step: dict[str, list[int]] = {SMALL: [], LARGE: []}
    for step in (2, 3):
        for thread_id in (SMALL, LARGE):
            live = SimpleNamespace(
                corsia_turn=TurnIdentity(f"{thread_id}-live", 1),
                checkpoint_wait_scope=CheckpointWaitScope(),
                catalog_model_id=target.catalog_id,
                session=SimpleNamespace(),
            )
            row_counter.reset()
            assert await bridge.maintain_external_tool_boundary(live, thread_id, harness) is False
            per_step[thread_id].append(row_counter.reset())
            _settle_next_step(thread_id, step)

    assert per_step[SMALL] == per_step[LARGE]


def test_native_checkpoint_context_does_not_reload_covered_history(row_counter, monkeypatch):
    from core.turn_iteration_context import _read_native_checkpoint_context

    target = _seed_both(monkeypatch, "native")

    counts = _rows_for_both(
        row_counter,
        lambda thread_id: _read_native_checkpoint_context(thread_id, target.catalog_id),
    )

    assert counts[SMALL] == counts[LARGE]


def test_send_preflight_candidates_do_not_reload_covered_history(row_counter, monkeypatch):
    from infra.send_preflight import _read_candidates

    _seed_both(monkeypatch, "codex")

    counts = _rows_for_both(row_counter, _read_candidates)

    assert counts[SMALL] == counts[LARGE]


@pytest.mark.parametrize("owner", ["manual", "idle", "switch"])
def test_checkpoint_preparation_does_not_reload_covered_history(row_counter, monkeypatch, owner):
    from core.checkpoint_agent_admission import CheckpointNotReady, read_checkpoint_preparation
    from infra.database import get_db
    from thread import model_checkpoint
    from infra.idle_checkpoints import IdleCheckpointDue
    from thread.idle_checkpoint import read_idle_context

    target = _seed_both(monkeypatch, "native")
    monkeypatch.setattr(model_checkpoint, "_switch_targets", lambda *_: (target, target))
    monkeypatch.setattr(model_checkpoint, "_switch_thread_row", lambda db, request:
                        db.execute("SELECT * FROM threads WHERE id=?", (request.thread_id,)).fetchone())

    def read(thread_id):
        if owner == "manual":
            with pytest.raises(CheckpointNotReady, match="already covers"):
                read_checkpoint_preparation(thread_id, "user-1", trigger="manual")
        elif owner == "switch":
            model_checkpoint.read_switch_checkpoint_context(SimpleNamespace(thread_id=thread_id))
        else:
            db = get_db()
            turn = db.execute(
                "SELECT id,turn_order FROM active_turns WHERE thread_id=? AND status='completed' "
                "ORDER BY turn_order DESC LIMIT 1", (thread_id,),
            ).fetchone()
            db.execute("UPDATE active_turns SET idle_checkpoint_pending=1 WHERE id=?", (turn["id"],))
            db.commit()
            db.close()
            read_idle_context(IdleCheckpointDue(turn["id"], turn["turn_order"], thread_id, "t"))

    counts = _rows_for_both(row_counter, read)
    assert counts[SMALL] == counts[LARGE]
