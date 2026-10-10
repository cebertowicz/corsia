"""Display contract of bounded message paging on sequenced threads.

`test_thread_messages_paging` mostly exercises legacy NULL-seq rows. These
cases pin the same contract where every row carries a transcript position, plus
the mixed legacy/sequenced ordering and the query plans that keep a first page
from scanning the whole thread.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


_module_path = Path(__file__).parents[1] / "api" / "thread_paging.py"
_spec = importlib.util.spec_from_file_location("_contract_thread_paging", _module_path)
assert _spec is not None and _spec.loader is not None
_thread_paging = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_thread_paging)
load_tool_result = _thread_paging.load_tool_result
page_messages = _thread_paging.page_messages

_SID = "sess-contract"


class _Thread:
    """Append rows in transcript order; `seq=None` writes a legacy row."""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE messages ("
            "id TEXT PRIMARY KEY, thread_id TEXT, role TEXT, content TEXT, "
            "metadata TEXT, created_at TEXT, seq INTEGER, active_turn_id TEXT, input_order INTEGER, "
            "delivery_state TEXT NOT NULL DEFAULT 'delivered', "
            "delivery_intent TEXT)"
        )
        self.conn.execute(
            "CREATE UNIQUE INDEX idx_messages_session_seq ON messages(thread_id, seq)"
        )
        self.conn.execute(
            "CREATE INDEX idx_messages_active_turn ON messages(active_turn_id, input_order)"
        )
        self.conn.execute(
            "CREATE TABLE active_turns (id TEXT, thread_id TEXT, turn_order INTEGER, status TEXT)"
        )
        self._next = 0

    def add(self, mid, role, content="", *, metadata=None, seq=..., ts=None,
            active_turn_id=None, thread_id=_SID):
        self._next += 1
        self.conn.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq,active_turn_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                mid, thread_id, role, content,
                json.dumps(metadata) if isinstance(metadata, dict) else metadata,
                ts or f"2026-06-12T00:00:{self._next:04d}",
                self._next if seq is ... else seq, active_turn_id,
            ),
        )
        return mid

    def step(self, mid, *call_ids, content=""):
        return self.add(mid, "assistant", content, metadata={
            "step_type": "tool_calls",
            "llm_tool_calls": [
                {"id": call, "type": "function",
                 "function": {"name": "Read", "arguments": "{}"}}
                for call in call_ids
            ],
        })

    def result(self, mid, call_id, content, *, step=..., **extra):
        metadata = {"tool_call_id": call_id, "name": "Read", **extra}
        if step is not ...:
            metadata["assistant_step_id"] = step
        return self.add(mid, "tool", content, metadata=metadata)

    def walk(self, limit, **kwargs):
        """Every page oldest-last, as a list of (ids, has_more, oldest_id)."""
        pages = []
        before = None
        while True:
            page, more, oldest = page_messages(
                self.conn, _SID, limit=limit, before=before, **kwargs,
            )
            pages.append(([row["id"] for row in page], more, oldest, page))
            if not more:
                return pages
            before = oldest


def _results(row: dict) -> list:
    return [call.get("result") for call in row["metadata"].get("tool_calls", [])]


def _flatten(pages) -> dict[str, dict]:
    return {row["id"]: row for *_, page in pages for row in page}


def test_sequenced_walk_orders_by_seq_and_counts_only_anchors():
    thread = _Thread()
    thread.add("u1", "user", "first")
    thread.step("s1", "c1", "c2")
    thread.result("r1", "c1", "one", step="s1")
    thread.result("r2", "c2", "two", step="s1")
    thread.add("sys-result", "system", "hidden", metadata={"type": "result"})
    thread.add("sys-notice", "system", "shown", metadata={"type": "notice"})
    # Backdated wall clock: transcript position, not created_at, orders it.
    thread.add("late-accepted", "user", "steer", ts="2020-01-01T00:00:0000")
    thread.add("a1", "assistant", "done")

    pages = thread.walk(2)

    assert [(ids, more, oldest) for ids, more, oldest, _ in pages] == [
        (["late-accepted", "a1"], True, "late-accepted"),
        (["s1", "sys-notice"], True, "s1"),
        (["u1"], False, "u1"),
    ]
    assert _results(_flatten(pages)["s1"]) == ["one", "two"]


def test_page_does_not_carry_the_deleted_reply_window_mark():
    thread = _Thread()
    thread.conn.execute(
        "INSERT INTO active_turns (id,thread_id,turn_order,status) VALUES (?,?,?,?)",
        ("turn-live", _SID, 7, "running"),
    )
    thread.add("human", "user", "Please report back", active_turn_id="turn-live")
    thread.add("assistant", "assistant", "Still working", metadata={"turn_id": "turn-live"})

    page, _, _ = page_messages(thread.conn, _SID, limit=2, before=None)

    assert page[-1]["id"] == "assistant"
    assert "reply_window_opened" not in page[-1]


def test_child_persisted_after_later_anchors_attaches_across_page_boundary():
    thread = _Thread()
    thread.add("u1", "user")
    thread.step("s1", "slow")
    thread.add("u2", "user", "steer while the tool runs")
    thread.add("a2", "assistant", "answer")
    thread.result("r-slow", "slow", "late result", step="s1")
    thread.add("u3", "user")

    for limit in (1, 2, 3):
        rows = _flatten(thread.walk(limit))
        assert _results(rows["s1"]) == ["late result"], limit
    assert load_tool_result(thread.conn, _SID, "s1", "slow") == "late result"


def test_legacy_reused_call_id_resolves_to_last_preceding_declarer():
    thread = _Thread()
    thread.step("s-old", "dup")
    thread.result("r-old", "dup", "old result")
    thread.add("u", "user")
    thread.step("s-mid", "dup")
    thread.step("s-new", "dup")
    thread.result("r-new", "dup", "new result")

    for limit in (1, 2, 5):
        rows = _flatten(thread.walk(limit))
        assert _results(rows["s-old"]) == ["old result"], limit
        assert _results(rows["s-mid"]) == [None], limit
        assert _results(rows["s-new"]) == ["new result"], limit


def test_other_step_child_with_reused_call_id_never_attaches():
    thread = _Thread()
    thread.step("s-old", "shared")
    thread.result("r-old", "shared", "belongs to old", step="s-old")
    thread.step("s-new", "shared")

    page, _, _ = page_messages(thread.conn, _SID, limit=1, before=None)

    assert [row["id"] for row in page] == ["s-new"]
    assert _results(page[0]) == [None]


def test_null_step_id_child_is_skipped_not_matched_as_legacy():
    thread = _Thread()
    thread.step("s1", "c1")
    thread.result("r1", "c1", "invalid lineage", step=None)

    page, _, _ = page_messages(thread.conn, _SID, limit=1, before=None)

    assert _results(page[0]) == [None]


def test_duplicate_results_prefer_real_over_synthetic_then_durable_order():
    thread = _Thread()
    thread.step("s1", "c1", "c2")
    thread.result("real", "c1", "real output", step="s1")
    thread.result("synthetic", "c1", "interrupted", step="s1",
                  synthetic_tool_result=True)
    thread.result("first", "c2", "first delivery", step="s1")
    thread.result("second", "c2", "second delivery", step="s1")

    page, _, _ = page_messages(thread.conn, _SID, limit=1, before=None)

    assert _results(page[0]) == ["real output", "second delivery"]


def test_legacy_null_seq_rows_page_as_newest_then_sequenced_rows():
    thread = _Thread()
    thread.add("m1", "user")
    thread.add("m2", "assistant")
    thread.add("m3", "user")
    thread.add("n1", "user", seq=None, ts="2026-01-01T00:00:0001")
    thread.add("n2", "assistant", seq=None, ts="2026-01-01T00:00:0002")

    pages = thread.walk(2)

    assert [(ids, more, oldest) for ids, more, oldest, _ in pages] == [
        (["n1", "n2"], True, "n1"),
        (["m2", "m3"], True, "m2"),
        (["m1"], False, "m1"),
    ]


def test_legacy_thread_page_seeks_positions_instead_of_sorting_the_thread():
    thread = _Thread()
    for turn in range(3):
        thread.add(f"m{turn}", "user")
    thread.add("n1", "user", seq=None, ts="2026-01-01T00:00:0001")
    statements: list[str] = []
    thread.conn.set_trace_callback(statements.append)

    page, _, _ = page_messages(thread.conn, _SID, limit=2, before=None)
    thread.conn.set_trace_callback(None)

    assert [row["id"] for row in page] == ["m2", "n1"]
    anchor_plans = [
        [row[3] for row in thread.conn.execute(f"EXPLAIN QUERY PLAN {sql}")]
        for sql in statements if "seq IS NOT NULL" in sql
    ]
    # Positioned rows are read down the position index, not sorted with the
    # whole thread under one ORDER BY.
    assert anchor_plans and all(
        any("idx_messages_session_seq (thread_id=? AND seq>?)" in step for step in plan)
        for plan in anchor_plans
    ), anchor_plans


def test_legacy_null_seq_child_still_attaches_on_a_sequenced_thread():
    thread = _Thread()
    thread.add("u1", "user")
    thread.step("s1", "c1")
    thread.result("r1", "c1", "legacy child", step="s1")
    thread.conn.execute("UPDATE messages SET seq=NULL WHERE id='r1'")

    page, _, _ = page_messages(thread.conn, _SID, limit=1, before=None)

    assert _results(page[0]) == ["legacy child"]


@pytest.mark.parametrize("limit", [1, 3])
def test_delivery_intent_and_deferred_results_on_sequenced_pages(limit):
    thread = _Thread()
    thread.step("s1", "c1", "c2")
    thread.result("r1", "c1", "one", step="s1")
    thread.add("queued", "user", "later", metadata={"delivery_intent": "apply_now"})
    thread.add("applied", "user", "now", metadata={"delivery_intent": "queue_after_turn"})
    thread.conn.execute(
        "UPDATE messages SET delivery_intent='queue_after_turn' WHERE id='queued'"
    )

    rows = _flatten(thread.walk(limit, include_tool_results=False))

    assert rows["queued"]["metadata"] == {"delivery_intent": "queue_after_turn"}
    assert rows["applied"]["metadata"] == {}
    assert [call["result_state"] for call in rows["s1"]["metadata"]["tool_calls"]] == [
        "deferred", "absent",
    ]


def test_other_thread_rows_never_join_a_page():
    thread = _Thread()
    thread.step("s1", "c1")
    thread.add("foreign-step", "assistant", thread_id="other", seq=2, metadata={
        "llm_tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "Read", "arguments": "{}"}}],
    })
    thread.add("foreign-result", "tool", "foreign", thread_id="other", seq=3,
               metadata={"tool_call_id": "c1", "name": "Read"})

    page, more, _ = page_messages(thread.conn, _SID, limit=5, before=None)

    assert [row["id"] for row in page] == ["s1"]
    assert more is False
    assert _results(page[0]) == [None]


def test_bounded_page_queries_seek_indexes_without_sorting_the_thread():
    from infra.schema import SCHEMA_SQL

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    thread = _Thread.__new__(_Thread)
    thread.conn, thread._next = conn, 0
    for turn in range(4):
        thread.add(f"u{turn}", "user")
        thread.step(f"s{turn}", f"c{turn}")
        thread.result(f"r{turn}", f"c{turn}", "out", step=f"s{turn}")
        thread.add(f"a{turn}", "assistant", "done")
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    first, _, oldest = page_messages(conn, _SID, limit=2, before=None)
    older, _, _ = page_messages(conn, _SID, limit=2, before=oldest)
    conn.set_trace_callback(None)

    assert [row["id"] for row in first + older] == ["s3", "a3", "a2", "u3"]
    assert first[0]["metadata"]["tool_calls"][0]["result"] == "out"
    # The trace callback reports SQL with its bound values expanded.
    plans = {
        sql: [row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}")]
        for sql in statements if sql.lstrip().upper().startswith("SELECT")
    }
    assert any("role = 'tool'" in sql for sql in plans)
    for sql, plan in plans.items():
        assert not any("TEMP B-TREE" in step or step.startswith("SCAN") for step in plan), (
            sql, plan,
        )
        if "role = 'tool'" in sql:
            assert any("idx_messages_tool_lineage" in step for step in plan), (sql, plan)
