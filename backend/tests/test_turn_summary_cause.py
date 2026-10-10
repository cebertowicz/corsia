"""A plan-limit Stop names its cause in the Turn summary; every other summary is unchanged."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from core.stop_causes import PLAN_LIMIT_REASON, PlanLimitStop

_module_path = Path(__file__).parents[1] / "api" / "thread_paging.py"
_spec = importlib.util.spec_from_file_location("_test_turn_summary_paging", _module_path)
assert _spec is not None and _spec.loader is not None
_thread_paging = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_thread_paging)

_SID = "thread"
_START, _END = "2026-09-30T20:00:00Z", "2026-09-30T20:01:00Z"


def _db(turns: list[tuple[str, str, str | None]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, thread_id TEXT, role TEXT, content TEXT, "
        "metadata TEXT, created_at TEXT, seq INTEGER, active_turn_id TEXT, "
        "delivery_state TEXT NOT NULL DEFAULT 'delivered', delivery_intent TEXT)"
    )
    conn.execute(
        "CREATE INDEX idx_messages_turn_calls ON messages(thread_id, role, "
        "json_extract(metadata, '$.turn_id')) WHERE role='assistant' AND json_valid(metadata)"
    )
    conn.execute(
        "CREATE TABLE active_turns (id TEXT, thread_id TEXT, status TEXT, started_at TEXT, "
        "ended_at TEXT, error TEXT)"
    )
    for seq, (turn_id, status, error) in enumerate(turns, start=1):
        conn.execute(
            "INSERT INTO active_turns VALUES (?,?,?,?,?,?)", (turn_id, _SID, status, _START, _END, error),
        )
        conn.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) VALUES (?,?,?,?,?,?,?)",
            (f"m-{turn_id}", _SID, "assistant", "working", json.dumps({"turn_id": turn_id}),
             f"2026-09-30T20:00:{seq:02d}Z", seq),
        )
    conn.commit()
    return conn


def _reload(conn: sqlite3.Connection) -> dict[str, dict]:
    """What a page load returns: the page, then its Turn summaries."""
    page, _, _ = _thread_paging.page_messages(conn, _SID, 50, None, include_tool_results=False)
    return _thread_paging._page_turn_summaries(conn, _SID, page)


def test_a_reloaded_page_names_the_plan_limit_cause() -> None:
    # The one terminal writer stores str(PlanLimitStop()) on the cancelled Turn.
    conn = _db([("paused", "cancelled", str(PlanLimitStop()))])

    assert _reload(conn)["paused"]["cause"] == PLAN_LIMIT_REASON


def test_a_turn_stopped_before_its_first_output_is_named_through_the_input_it_took() -> None:
    conn = _db([])
    conn.execute("INSERT INTO active_turns VALUES (?,?,?,?,?,?)", ("paused", _SID, "cancelled", _START, _END, str(PlanLimitStop())))
    conn.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq,active_turn_id) VALUES (?,?,?,?,?,?,?,?)",
        ("ask", _SID, "user", "summarize", "{}", _START, 1, "paused"),
    )
    conn.commit()

    summary = _reload(conn)["paused"]
    assert (summary["cause"], summary["activity_count"]) == (PLAN_LIMIT_REASON, 0)


@pytest.mark.parametrize(
    ("status", "error"),
    [
        ("cancelled", None),  # user Stop
        ("cancelled", "superseded_by_input"),
        ("completed", None),
        ("error", PLAN_LIMIT_REASON),  # only a cancel carries the cause
        ("running", None),
    ],
)
def test_every_other_summary_is_byte_identical(status: str, error: str | None) -> None:
    conn = _db([("turn", status, error)])

    expected = {"started_at": _START, "ended_at": _END, "status": status, "activity_count": 1}
    assert json.dumps(_reload(conn)["turn"]) == json.dumps(expected)
