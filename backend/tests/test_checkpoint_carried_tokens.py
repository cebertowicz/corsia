"""Every Native checkpoint request carries the thread's fixed prompt and tools.

The 32 GB seat: window 48,000, fixed prompt 14,000, tool definitions 4,618. The
in-Turn path caps the body at half the room above that (8,691); the idle and
manual paths must reach the same cap, and an unmeasured thread keeps window // 5.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from core.fork_native import _window_output
from history.types import HistoryBoundary
from llm.resolution import ExecutionTarget

WINDOW, FIXED, TOOLS = 48_000, 14_000, 4_618
ROOM_CAPPED = 8_691  # (36,000 launch - 18,618) // 2
UNMEASURED = WINDOW // 5

TARGET = ExecutionTarget(
    catalog_id="local/m", harness="native", provider="omlx", backend="local",
    transport_model="m", expected_reported_model=None, context_window=WINDOW,
    available=True, availability_reason=None, display_name="m",
    supports_reasoning=True, preserve_thinking_default=True,
)
BOUNDARY = HistoryBoundary("t", 1, 1, "m1")


def _idle_state(fixed: int, tools: int):
    ledger = SimpleNamespace(rendered_input_tokens=30_000)
    messages = [{"role": "user", "content": "go"}]
    return SimpleNamespace(
        fit_ledger=ledger, last_measured_call_usage={"prompt_tokens": 30_000},
        last_measured_rendered_input_tokens=30_000, checkpoint_source_messages=messages,
        messages=messages, final_content="done", tool_definitions=[],
        context_usage={"fit_section_tokens": {"fixed": fixed, "tools": tools}},
    )


async def _idle_allowance(monkeypatch, fixed: int, tools: int) -> int:
    from core import fork_native, fork_native_warm
    from core.checkpoint_native_admission import freeze_idle_receipt
    from thread.idle_checkpoint import _complete_native_idle_checkpoint

    async def fits(*_args, **_kwargs):
        return True

    monkeypatch.setattr(fork_native, "warm_input_fits", fits)
    monkeypatch.setattr(fork_native_warm, "capture_live_warm_source", lambda *_a, **_k: object())
    receipt = freeze_idle_receipt(_idle_state(fixed, tools))
    preparation = SimpleNamespace(
        due=SimpleNamespace(thread_id="t", native_receipt=receipt),
        request=SimpleNamespace(id="c1", thread_id="t"),
        context=SimpleNamespace(
            target=TARGET, boundary=BOUNDARY, working_dir="/w", reasoning_effort=None,
            binary=None,
        ),
    )
    admission = await _complete_native_idle_checkpoint(preparation)
    return _window_output(admission.provider_request).max_output_tokens


@pytest.mark.asyncio
async def test_an_idle_native_checkpoint_gets_the_room_capped_allowance(monkeypatch):
    assert await _idle_allowance(monkeypatch, FIXED, TOOLS) == ROOM_CAPPED


@pytest.mark.asyncio
async def test_an_unmeasured_idle_native_checkpoint_keeps_the_window_allowance(monkeypatch):
    assert await _idle_allowance(monkeypatch, 0, 0) == UNMEASURED


def _thread_db(usage: dict | None, *, epoch: int = 3) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE threads (id TEXT PRIMARY KEY, runtime_context_epoch INTEGER);
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, thread_id TEXT, role TEXT, content TEXT,
            metadata TEXT, created_at TEXT, seq INTEGER
        );
        CREATE TABLE active_turns (
            turn_order INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE,
            thread_id TEXT, status TEXT, started_at TEXT, ended_at TEXT
        );
        """
    )
    db.execute("INSERT INTO threads VALUES ('t', ?)", (epoch,))
    if usage is not None:
        db.execute(
            "INSERT INTO active_turns (id,thread_id,status,started_at) "
            "VALUES ('turn-1','t','completed','s')"
        )
        receipt = {
            "token_usage_available": True, "token_source": "measured",
            "total_used": 30_000, "model_context_window": WINDOW,
            "harness": "native", "catalog_model_id": "local/m",
            "runtime_context_epoch": 3, **usage,
        }
        db.execute(
            "INSERT INTO messages VALUES ('a1','t','assistant','answer',?,'now',1)",
            (json.dumps({"turn_id": "turn-1", "context_usage": receipt}),),
        )
    return db


def _manual_allowance(monkeypatch, db: sqlite3.Connection) -> int:
    from core.checkpoint_agent_admission import _build_manual_cold_admission
    from infra import database

    monkeypatch.setattr(database, "get_db", lambda: SimpleNamespace(
        execute=db.execute, close=lambda: None,
    ))
    context = SimpleNamespace(
        thread_id="t", target=TARGET, boundary=BOUNDARY, previous=None,
        projection=SimpleNamespace(), working_dir="/w", reasoning_effort=None, binary=None,
    )
    monkeypatch.setattr("history.projection.slice_portable_projection", lambda p, through: p)
    admission = _build_manual_cold_admission("c1", context)
    return _window_output(admission.provider_request).max_output_tokens


def test_a_manual_native_checkpoint_gets_the_room_capped_allowance(monkeypatch):
    # The last answer's receipt splits the prompt into system, injected context and tools.
    usage = {"system_tokens": 9_000, "context_source_tokens": 5_000, "tool_def_tokens": TOOLS}

    assert _manual_allowance(monkeypatch, _thread_db(usage)) == ROOM_CAPPED


def test_a_manual_native_checkpoint_with_no_measured_turn_keeps_the_window_allowance(monkeypatch):
    assert _manual_allowance(monkeypatch, _thread_db(None)) == UNMEASURED
