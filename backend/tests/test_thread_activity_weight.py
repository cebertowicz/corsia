"""Per-row activity weight: the Turn summary's own counting rule, one row at a time."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3

from subagents.assistant_step_display import load_assistant_step_display


# Keep this unit test isolated from api.__init__ and its unrelated provider stack.
_module_path = Path(__file__).parents[1] / "api" / "thread_paging.py"
_spec = importlib.util.spec_from_file_location("_test_thread_paging_weight", _module_path)
assert _spec is not None and _spec.loader is not None
_thread_paging = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_thread_paging)
page_messages = _thread_paging.page_messages
page_turn_summaries = _thread_paging._page_turn_summaries

_SID = "sess-weight"
_FOCUS_SUMMARY_FIXTURE = json.loads(
    (Path(__file__).parents[2] / "testdata" / "focus-turn-summary-parity.json").read_text()
)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE messages ("
        "id TEXT PRIMARY KEY, thread_id TEXT, role TEXT, content TEXT, "
        "metadata TEXT, created_at TEXT, seq INTEGER, active_turn_id TEXT, "
        "write_started_at TEXT, delivery_state TEXT NOT NULL DEFAULT 'delivered', "
        "delivery_intent TEXT, input_order INTEGER)"
    )
    conn.execute(
        "CREATE INDEX idx_messages_turn_calls ON messages(thread_id, role, "
        "json_extract(metadata, '$.turn_id')) WHERE role='assistant' AND json_valid(metadata)"
    )
    conn.execute(
        "CREATE TABLE active_turns (id TEXT, thread_id TEXT, status TEXT, started_at TEXT, ended_at TEXT, error TEXT)"
    )
    return conn


def _turn(conn, turn_id, status="completed"):
    conn.execute(
        "INSERT INTO active_turns (id,thread_id,status,started_at,ended_at) VALUES (?,?,?,?,?)",
        (turn_id, _SID, status, "2026-06-12T00:00:00Z", "2026-06-12T00:04:00Z"),
    )


def _row(conn, mid, role, content, seq, metadata=None, **columns):
    raw = metadata if isinstance(metadata, str) or metadata is None else json.dumps(metadata)
    conn.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,metadata,created_at,seq,active_turn_id,write_started_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            mid, _SID, role, content, raw, f"2026-06-12T00:00:{seq:02d}", seq,
            columns.get("active_turn_id"), columns.get("write_started_at"),
        ),
    )


def _seed_mixed_turn(conn, turn_id="turn-mixed"):
    """One Turn holding every row kind the transcript shows, and what each weighs."""
    _turn(conn, turn_id)
    call = {"id": "c1", "function": {"name": "Read", "arguments": "{}"}}
    _row(conn, "ask", "user", "please", 1, {}, active_turn_id=turn_id, write_started_at="2026-06-12T00:00:01")
    _row(conn, "context", "system", "context loaded", 2, {"type": "context_event"})
    _row(conn, "calls", "assistant", "", 3, {"turn_id": turn_id, "reasoning": "look", "llm_tool_calls": [call, {**call, "id": "c2"}]})
    _row(conn, "result", "tool", "ok", 4, {"tool_call_id": "c1", "assistant_step_id": "calls", "turn_id": turn_id})
    _row(conn, "hints", "assistant", "", 5, {"turn_id": turn_id, "tool_hints": ["Read a", "Grep b"]})
    _row(conn, "text", "assistant", "thinking aloud", 6, {"turn_id": turn_id})
    _row(conn, "broken", "assistant", "kept", 7, "{not json")
    _row(conn, "notice", "system", "stopped", 8, {"type": "cc_error"})
    _row(conn, "reply", "assistant", "Done", 9, {"turn_id": turn_id, "for_user": True})
    conn.commit()
    return {"ask": 0, "context": 0, "calls": 3, "hints": 2, "text": 1, "broken": 0, "notice": 0, "reply": 0}


def _weights(page):
    return {row["id"]: row["activity_weight"] for row in page}


def test_page_rows_carry_a_weight_that_sums_to_the_turn_activity_count():
    conn = _db()
    expected = _seed_mixed_turn(conn)

    page, _, _ = page_messages(conn, _SID, limit=20, before=None)

    assert _weights(page) == expected
    summary = page_turn_summaries(conn, _SID, page)["turn-mixed"]
    assert sum(_weights(page).values()) == summary["activity_count"] == 6


def test_weight_is_a_known_zero_for_rows_the_summary_never_counts():
    conn = _db()
    _seed_mixed_turn(conn)

    page, _, _ = page_messages(conn, _SID, limit=20, before=None)

    for row_id in ("ask", "context", "broken", "notice"):
        row = next(row for row in page if row["id"] == row_id)
        assert row["activity_weight"] == 0 and row["activity_weight"] is not None


def test_first_page_older_page_whole_history_and_step_loader_agree_on_each_weight():
    conn = _db()
    expected = _seed_mixed_turn(conn)

    newest, _, cursor = page_messages(conn, _SID, limit=3, before=None)
    older, _, _ = page_messages(conn, _SID, limit=20, before=cursor)
    whole, _, _ = page_messages(conn, _SID, limit=None, before=None)

    assert {**_weights(older), **_weights(newest)} == expected
    assert _weights(whole) == expected
    for step_id in ("calls", "hints", "text", "reply"):
        step = load_assistant_step_display(_SID, step_id, db=conn)
        assert step["activity_weight"] == expected[step_id]


def test_fixture_turn_weights_sum_to_the_public_count():
    conn = _db()
    turn = _FOCUS_SUMMARY_FIXTURE["turn"]
    conn.execute(
        "INSERT INTO active_turns (id,thread_id,status,started_at,ended_at) VALUES (?,?,?,?,?)",
        (turn["id"], _SID, turn["status"], turn["started_at"], turn["ended_at"]),
    )
    for message in _FOCUS_SUMMARY_FIXTURE["messages"]:
        _row(conn, message["id"], message["role"], message["content"], message["seq"], message["metadata"])
    conn.commit()

    page, _, _ = page_messages(conn, _SID, limit=20, before=None)

    expected = _FOCUS_SUMMARY_FIXTURE["expected"]["activity_count"]
    assert sum(_weights(page).values()) == expected
    assert page_turn_summaries(conn, _SID, page)[turn["id"]]["activity_count"] == expected


def test_empty_tool_calls_with_several_hints_counts_the_hints_it_displays():
    conn = _db()
    turn_id = "empty-calls-hints"
    _turn(conn, turn_id)
    hints = ["Read a", "Grep b", "Edit c"]
    _row(conn, "legacy", "assistant", "", 1, {"turn_id": turn_id, "tool_calls": [], "tool_hints": hints})
    conn.commit()

    page, _, _ = page_messages(conn, _SID, limit=1, before=None)

    # The display falls through the empty list to the hints, one step each.
    assert "tool_calls" not in page[0]["metadata"]
    assert page[0]["metadata"]["tool_hints"] == hints
    assert page_turn_summaries(conn, _SID, page)[turn_id]["activity_count"] == len(hints)
    assert page[0]["activity_weight"] == len(hints)


def test_page_rows_keep_the_input_timing_fields_the_client_bounds_time_with():
    conn = _db()
    _seed_mixed_turn(conn)

    page, _, _ = page_messages(conn, _SID, limit=20, before=None)

    ask = next(row for row in page if row["id"] == "ask")
    assert ask["active_turn_id"] == "turn-mixed"
    assert ask["write_started_at"] == "2026-06-12T00:00:01"
    assert next(row for row in page if row["id"] == "reply")["write_started_at"] is None


def test_a_turn_stopped_before_output_keeps_its_provenance_and_weighs_nothing():
    conn = _db()
    conn.execute(
        "INSERT INTO active_turns (id,thread_id,status,started_at,ended_at,error) VALUES (?,?,?,?,?,?)",
        ("paused", _SID, "cancelled", "2026-06-12T00:00:00Z", "2026-06-12T00:00:01Z", "plan_limit_overage_off"),
    )
    _row(conn, "wake", "user", "wake", 1, {"sender_kind": "agent"}, active_turn_id="paused")
    conn.commit()

    page, _, _ = page_messages(conn, _SID, limit=20, before=None)
    summary = page_turn_summaries(conn, _SID, page)["paused"]

    assert (summary["cause"], summary["activity_count"]) == ("plan_limit_overage_off", 0)
    assert _weights(page) == {"wake": 0}


def test_summary_and_weight_share_one_expression_whose_row_rule_is_the_index_predicate():
    import re

    from api import thread_turn_summaries as summaries
    from infra import message_activity as activity

    # The summary sums the helper's own objects; it defines no rule of its own.
    assert summaries.ACTIVITY_SQL is activity.ACTIVITY_SQL
    assert summaries.ACTIVITY_ROW_SQL is activity.ACTIVITY_ROW_SQL
    assert not hasattr(summaries, "_DISPLAY_CALL_COUNT_SQL")
    assert activity.ACTIVITY_WEIGHT_SQL == (
        f"CASE WHEN {activity.ACTIVITY_ROW_SQL} THEN {activity.ACTIVITY_SQL} ELSE 0 END"
    )
    assert activity.DISPLAY_ROW_SELECT.count(activity.ACTIVITY_SQL) == 1
    # The row rule is the shipped schema's own partial-index predicate.
    schema = (Path(__file__).parents[1] / "infra" / "schema.py").read_text()
    predicate = re.search(r"idx_messages_turn_calls\b.*?\) WHERE (.*?);", schema, re.S)
    assert predicate is not None and predicate.group(1) == activity.ACTIVITY_ROW_SQL
    conn = _db()
    plan = " ".join(row[3] for row in conn.execute(
        "EXPLAIN QUERY PLAN SELECT json_extract(metadata, '$.turn_id'), "
        f"SUM({activity.ACTIVITY_SQL}) FROM messages INDEXED BY idx_messages_turn_calls "
        f"WHERE thread_id=? AND {activity.ACTIVITY_ROW_SQL} "
        "AND json_extract(metadata, '$.turn_id') IN (?) "
        "GROUP BY json_extract(metadata, '$.turn_id')",
        (_SID, "turn"),
    ))
    assert "SEARCH messages USING INDEX idx_messages_turn_calls" in plan
