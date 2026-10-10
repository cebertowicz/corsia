"""Focus pages: visible rows plus one stub per hidden run, and the range read."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from infra.focus_rows import FOCUS_VISIBLE_SQL, focus_kind_sql, is_focus_visible, row_kind
from infra.message_activity import ACTIVITY_WEIGHT_SQL
from api.thread_focus_page import (
    FOCUS_SCAN_CAP,
    InvertedRange,
    _walk as walk_page,
    page_focus,
    page_run_range,
    read_authorized_focus_page,
)
from api.thread_paging import CursorNotFound
import api.thread_paging as paging

_SID = "sess-focus"
_FIXTURE = json.loads(
    (Path(__file__).parents[2] / "testdata" / "focus-visibility-parity.json").read_text()
)
_CALLS = json.dumps([{"id": "c", "function": {"name": "Read", "arguments": "{}"}}])


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE messages ("
        "id TEXT PRIMARY KEY, thread_id TEXT, role TEXT, content TEXT, "
        "metadata TEXT, created_at TEXT, seq INTEGER, active_turn_id TEXT, "
        "delivery_state TEXT NOT NULL DEFAULT 'delivered', delivery_intent TEXT, "
        "input_order INTEGER)"
    )
    conn.execute(
        "CREATE INDEX idx_messages_turn_calls ON messages(thread_id, role, "
        "json_extract(metadata, '$.turn_id')) WHERE role='assistant' AND json_valid(metadata)"
    )
    conn.execute(
        "CREATE TABLE active_turns (id TEXT, thread_id TEXT, status TEXT, "
        "started_at TEXT, ended_at TEXT, error TEXT)"
    )
    return conn


class _Thread:
    """Appends rows in order. `kind` is `u` human, `s` said-to-you, `w` work."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.n = 0
        self.ids: list[str] = []

    def add(self, kind: str, turn: str | None = None, *, seq: bool = True, **extra) -> str:
        self.n += 1
        mid = f"{kind}{self.n:03d}"
        meta: dict = {"turn_id": turn} if turn else {}
        role, content = "assistant", ""
        if kind == "u":
            role, content = "user", f"human {self.n}"
        elif kind == "s":
            meta["for_user"] = True
            content = f"said {self.n}"
        elif kind == "w":
            meta["llm_tool_calls"] = json.loads(_CALLS)
        elif kind == "t":
            role, content = "tool", "result"
        elif kind == "c":
            role, content = "system", "ctx"
            meta["type"] = "context_event"
        meta.update(extra.pop("meta", {}))
        self.conn.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq,"
            "delivery_state) VALUES (?,?,?,?,?,?,?,?)",
            (
                mid, _SID, role, extra.pop("content", content), json.dumps(meta),
                f"2026-06-12T00:{self.n // 60:02d}:{self.n % 60:02d}",
                self.n if seq else None, "delivered" if seq else "queued",
            ),
        )
        self.ids.append(mid)
        return mid


def _live(conn, turn_id: str, status: str = "running") -> None:
    conn.execute(
        "INSERT INTO active_turns (id,thread_id,status) VALUES (?,?,?)",
        (turn_id, _SID, status),
    )


class TestVisibilityParity:
    @pytest.mark.parametrize("case", _FIXTURE["cases"], ids=lambda case: case["name"])
    def test_python_and_sql_rules_agree_with_the_fixture(self, case):
        row = {k: case[k] for k in ("role", "content")} | {"metadata": case["metadata_raw"]}
        row["activity_weight"] = case.get("activity_weight")
        assert is_focus_visible(row) is case["visible"]
        assert row_kind(row) == case["expected_kind"]
        conn = _db()
        conn.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) "
            "VALUES ('m',?,?,?,?,'t')",
            (_SID, case["role"], case["content"], case["metadata_raw"]),
        )
        flag = conn.execute(f"SELECT {FOCUS_VISIBLE_SQL} FROM messages").fetchone()[0]
        assert bool(flag) is case["visible"]
        kind, weight = conn.execute(
            f"SELECT {focus_kind_sql(ACTIVITY_WEIGHT_SQL)}, {ACTIVITY_WEIGHT_SQL} FROM messages"
        ).fetchone()
        assert kind == case["expected_kind"]
        if case["role"] == "assistant":
            assert weight == case["activity_weight"]


class TestStubsAndCounts:
    def test_hidden_rows_between_visible_ones_become_one_stub_each(self):
        conn = _db()
        t = _Thread(conn)
        first = t.add("u")
        t.add("w", "T1")
        t.add("t")  # a tool child is never an anchor
        t.add("c")
        t.add("w", "T1")
        said = t.add("s", "T1")
        t.add("w", "T2")
        last = t.add("s", "T2")
        conn.commit()

        page = page_focus(conn, _SID, 10, None)

        assert [m["id"] for m in page["messages"]] == [first, said, last]
        assert [run["count"] for run in page["runs"]] == [3, 1]
        older, newer = page["runs"]
        assert (older["first_id"], older["last_id"]) == ("w002", "w005")
        assert (older["first_seq"], older["last_seq"]) == (2, 5)
        assert (newer["first_seq"], newer["last_seq"]) == (7, 7)
        assert older["turn_ids"] == ["T1"] and newer["turn_ids"] == ["T2"]
        assert older["weight"] == 2 and newer["weight"] == 1
        assert page["has_more"] is False
        assert page["oldest_id"] == first
        assert all(m["focus_visible"] for m in page["messages"])

    def test_no_fold_id_is_sent(self):
        conn = _db()
        t = _Thread(conn)
        t.add("w", "T1")
        t.add("s", "T1")
        conn.commit()
        (stub,) = page_focus(conn, _SID, 10, None)["runs"]
        assert "fold_id" not in stub and "fold_key" not in stub
        assert set(stub) == {
            "kind", "first_id", "last_id", "count", "turn_ids", "weight",
            "continues_older", "first_seq", "last_seq", "kinds",
        }

    def test_an_account_move_and_a_finished_subagent_stay_inside_one_run_and_name_their_kinds(self):
        conn = _db()
        t = _Thread(conn)
        t.add("u")
        t.add("c", "T1", meta={"type": "account_switch"})
        t.add("c", "T1", meta={"type": "subagent_done"})
        t.add("c", "T1", meta={"type": "llm_error", "recovered": True})
        t.add("c", "T1", meta={"type": "send_ref"})
        t.add("s", "T1")
        conn.commit()
        page = page_focus(conn, _SID, 10, None)
        (stub,) = page["runs"]
        assert stub["count"] == 4
        assert stub["kinds"] == ["account_move", "error_recovered", "other", "subagent_finished"]
        assert stub["weight"] == 0

    def test_an_unrecovered_error_splits_the_run_and_a_recovered_one_does_not(self):
        conn = _db()
        t = _Thread(conn)
        t.add("w", "T1")
        t.add("c", "T1", meta={"type": "cc_error"}, content="stopped")
        t.add("w", "T2")
        t.add("c", "T2", meta={"type": "tool_bridge_unavailable", "recovered": True}, content="no bridge")
        t.add("w", "T3")
        conn.commit()
        page = page_focus(conn, _SID, 10, None)
        assert [m["id"] for m in page["messages"]] == ["c002"]
        assert [(r["count"], r["kinds"]) for r in page["runs"]] == [(1, ["steps"]), (3, ["error_recovered", "steps"])]

    def test_only_context_rows_report_no_steps_kind(self):
        conn = _db()
        t = _Thread(conn)
        t.add("u")
        t.add("c", "T1")
        t.add("u", seq=True, meta={"sender_kind": "agent"})
        t.add("s", "T1")
        conn.commit()
        (stub,) = page_focus(conn, _SID, 10, None)["runs"]
        assert stub["kinds"] == ["agent_message", "context_update"] and stub["weight"] == 0

    def test_limit_counts_visible_rows_and_stops_after_the_oldest_of_them(self):
        conn = _db()
        t = _Thread(conn)
        for _ in range(6):
            t.add("w", "T1")
            t.add("s", "T1")
        conn.commit()

        page = page_focus(conn, _SID, 2, None)

        assert [m["id"] for m in page["messages"]] == ["s010", "s012"]
        assert [run["count"] for run in page["runs"]] == [1]
        assert page["has_more"] is True
        assert page["oldest_id"] == "s010"
        assert page["runs"][0]["continues_older"] is False

    def test_every_page_in_a_walk_covers_the_thread_without_overlap(self):
        conn = _db()
        t = _Thread(conn)
        for turn in ("T1", "T2", "T3", "T4"):
            t.add("u")
            t.add("w", turn)
            t.add("c")
            t.add("w", turn)
            t.add("s", turn)
        conn.commit()
        seen_rows: list[str] = []
        seen_counts = 0
        cursor = None
        while True:
            page = page_focus(conn, _SID, 3, cursor)
            seen_rows = [m["id"] for m in page["messages"]] + seen_rows
            seen_counts += sum(run["count"] for run in page["runs"])
            if not page["has_more"]:
                break
            cursor = page["oldest_id"]
        assert len(seen_rows) == len(set(seen_rows)) == 8
        assert seen_counts == 12
        assert conn.execute(
            "SELECT COUNT(*) FROM messages WHERE role != 'tool'"
        ).fetchone()[0] == seen_counts + 8


class TestMidTurnOpen:
    def test_queued_steer_and_agent_send_ride_the_newest_end_inside_the_limit(self):
        conn = _db()
        t = _Thread(conn)
        t.add("u")
        t.add("w", "T1")
        said = t.add("s", "T1")
        t.add("w", "T2")
        steer = t.add("u", seq=False)
        agent = t.add(
            "u", seq=False, meta={"sender_kind": "agent"}, content="from a peer",
        )
        _live(conn, "T2")
        conn.commit()

        page = page_focus(conn, _SID, 10, None)

        ids = [m["id"] for m in page["messages"]]
        assert ids[-2:] == [steer, agent]
        flags = {m["id"]: m["focus_visible"] for m in page["messages"]}
        assert flags[steer] is True and flags[agent] is False
        live_stub = page["runs"][-1]
        assert live_stub["turn_ids"] == ["T2"]
        # The stub never holds a queued row, which has no position to hold.
        assert live_stub["last_seq"] == 4
        assert (live_stub["first_id"], live_stub["last_id"]) == ("w004", "w004")
        assert said in ids

    def test_every_queued_row_is_returned_fifo_head_first_and_counts_against_the_limit(self):
        conn = _db()
        t = _Thread(conn)
        for _ in range(3):
            t.add("u")
        queued = [t.add("u", seq=False) for _ in range(5)]
        # Delivery order is input_order, not insertion order: the head was created last.
        for order, mid in zip((3, 4, 5, 2, 1), queued):
            conn.execute("UPDATE messages SET input_order=? WHERE id=?", (order, mid))
        conn.commit()

        page = page_focus(conn, _SID, 4, None)

        shown = [m["id"] for m in page["messages"] if m["delivery_state"] == "queued"]
        assert shown == [queued[4], queued[3], queued[0], queued[1], queued[2]]
        # The queue is larger than the limit; one sequenced row still reads so the cursor moves.
        assert len(page["messages"]) == 6
        assert page["oldest_id"] == "u003"

    def test_queued_rows_are_never_returned_on_an_older_page(self):
        conn = _db()
        t = _Thread(conn)
        for _ in range(4):
            t.add("u")
        t.add("u", seq=False)
        conn.commit()
        first = page_focus(conn, _SID, 3, None)
        older = page_focus(conn, _SID, 3, first["oldest_id"])
        assert all(m["delivery_state"] != "queued" for m in older["messages"])

    def test_finished_previous_turn_work_is_split_from_the_live_turn(self):
        conn = _db()
        t = _Thread(conn)
        t.add("u")
        t.add("w", "T1")
        t.add("w", "T1")
        t.add("w", "T2")  # live
        t.add("w", "T2")
        _live(conn, "T2")
        conn.commit()

        page = page_focus(conn, _SID, 10, None)

        assert [(r["turn_ids"], r["count"]) for r in page["runs"]] == [
            (["T1"], 2), (["T2"], 2),
        ]
        assert page["runs"][0]["last_seq"] < page["runs"][1]["first_seq"]
        assert set(page["turn_summaries"]) == {"T2"}  # only the seeded Turn row

    def test_waiting_retry_counts_as_live(self):
        conn = _db()
        t = _Thread(conn)
        t.add("w", "T1")
        t.add("w", "T2")
        _live(conn, "T2", "waiting_retry")
        conn.commit()
        assert [r["turn_ids"] for r in page_focus(conn, _SID, 5, None)["runs"]] == [
            ["T1"], ["T2"],
        ]


class TestContinuesOlder:
    def test_a_scan_that_stops_inside_a_run_flags_it_and_the_next_page_continues(self):
        conn = _db()
        t = _Thread(conn)
        t.add("s", "T0")
        for _ in range(5):
            t.add("w", "T1")
        t.add("s", "T1")
        conn.commit()

        first = page_focus(conn, _SID, 10, None, scan_cap=3)
        (stub,) = first["runs"]
        assert first["has_more"] is True and first["oldest_id"] == "w005"
        assert stub["continues_older"] is True
        assert (stub["first_id"], stub["last_id"]) == ("w005", "w006")
        assert stub["count"] == 2

        second = page_focus(conn, _SID, 10, first["oldest_id"], scan_cap=10)
        (rest,) = second["runs"]
        assert rest["continues_older"] is False
        assert rest["last_seq"] < stub["first_seq"]  # adjacent: the client merges them
        assert rest["count"] == 3
        assert (first["runs"][0]["count"] + rest["count"]) == 5
        assert [m["id"] for m in second["messages"]] == ["s001"]

    def test_two_runs_of_one_turn_split_by_a_said_row_do_not_merge(self):
        conn = _db()
        t = _Thread(conn)
        t.add("w", "T1")
        t.add("s", "T1")
        t.add("w", "T1")
        t.add("s", "T1")
        conn.commit()

        first = page_focus(conn, _SID, 1, None)
        assert [m["id"] for m in first["messages"]] == ["s004"]
        assert first["runs"] == []  # the run before the oldest shown row is next page's
        second = page_focus(conn, _SID, 1, first["oldest_id"])
        assert [(r["first_id"], r["last_id"]) for r in second["runs"]] == [("w003", "w003")]
        assert second["runs"][0]["continues_older"] is False
        assert [m["id"] for m in second["messages"]] == ["s002"]

    def test_one_run_whose_ends_carry_different_turn_ids_lists_them_oldest_first(self):
        conn = _db()
        t = _Thread(conn)
        t.add("w", "T1")
        t.add("w", None)
        t.add("w", "T2")
        t.add("s", "T2")
        conn.commit()
        (stub,) = page_focus(conn, _SID, 5, None)["runs"]
        assert stub["turn_ids"] == ["T1", "T2"] and stub["count"] == 3
        assert (stub["first_id"], stub["last_id"]) == ("w001", "w003")


class TestScanCap:
    def test_the_walk_reads_at_most_cap_rows_and_returns_a_cursor(self):
        conn = _db()
        t = _Thread(conn)
        t.add("s", "T0")
        for _ in range(50):
            t.add("w", "T1")
        conn.commit()

        page = page_focus(conn, _SID, 10, None, scan_cap=7)

        assert page["messages"] == []
        (stub,) = page["runs"]
        assert stub["count"] == 7 and stub["continues_older"] is True
        assert page["has_more"] is True
        assert page["oldest_id"] == "w045"
        deeper = page_focus(conn, _SID, 10, page["oldest_id"], scan_cap=500)
        assert deeper["has_more"] is False
        assert [m["id"] for m in deeper["messages"]] == ["s001"]

    def test_a_cap_that_equals_the_thread_does_not_claim_more(self):
        conn = _db()
        t = _Thread(conn)
        for _ in range(4):
            t.add("w", "T1")
        conn.commit()
        page = page_focus(conn, _SID, 10, None, scan_cap=4)
        assert page["has_more"] is False
        assert page["runs"][0]["continues_older"] is False

    def test_the_walk_stays_in_one_read_snapshot(self, monkeypatch):
        conn = _db()
        _Thread(conn).add("s", "T1")
        conn.commit()
        seen: list[bool] = []

        def spy(thread_id, user_id, *, db=None):
            seen.append(bool(db.in_transaction))
            return {"id": thread_id}

        monkeypatch.setattr("api.thread_focus_page.get_thread_for_user", spy)
        page = read_authorized_focus_page(conn, _SID, 5, None, user_id="u1")
        assert page is not None and seen == [True]

    def test_a_thread_the_user_does_not_own_returns_nothing(self, monkeypatch):
        conn = _db()
        monkeypatch.setattr("api.thread_focus_page.get_thread_for_user", lambda *a, **k: None)
        assert read_authorized_focus_page(conn, _SID, 5, None, user_id="u2") is None


class TestRangeRead:
    def _run(self):
        conn = _db()
        t = _Thread(conn)
        t.add("u")
        for _ in range(5):
            t.add("w", "T1")
        t.add("s", "T1")
        conn.commit()
        return conn

    def test_both_bounds_are_inclusive(self):
        conn = self._run()
        page = page_run_range(conn, _SID, 10, "w002", "w006", None)
        assert [m["id"] for m in page["messages"]] == [
            "w002", "w003", "w004", "w005", "w006",
        ]
        assert page["has_more"] is False
        assert page["oldest_id"] == "w002"

    def test_a_large_run_pages_newest_first_within_its_range(self):
        conn = self._run()
        first = page_run_range(conn, _SID, 2, "w002", "w006", None)
        assert [m["id"] for m in first["messages"]] == ["w005", "w006"]
        assert first["has_more"] is True
        second = page_run_range(conn, _SID, 2, "w002", "w006", first["oldest_id"])
        assert [m["id"] for m in second["messages"]] == ["w003", "w004"]
        third = page_run_range(conn, _SID, 2, "w002", "w006", second["oldest_id"])
        assert [m["id"] for m in third["messages"]] == ["w002"]
        assert third["has_more"] is False

    def test_the_range_never_reads_outside_it(self):
        conn = self._run()
        page = page_run_range(conn, _SID, 50, "w003", "w004", None)
        assert [m["id"] for m in page["messages"]] == ["w003", "w004"]

    def test_an_unknown_bound_is_an_empty_page_like_an_unknown_cursor(self):
        conn = self._run()
        with pytest.raises(CursorNotFound):
            page_run_range(conn, _SID, 5, "nope", "w006", None)
        with pytest.raises(CursorNotFound):
            page_run_range(conn, _SID, 5, "w002", "nope", None)

    def test_a_queued_or_tool_row_is_not_a_bound(self):
        conn = self._run()
        conn.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,delivery_state) "
            "VALUES ('queued-row',?,'user','q','{}','t','queued')", (_SID,),
        )
        conn.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
            "VALUES ('tool-row',?,'tool','x','{}','t',99)", (_SID,),
        )
        with pytest.raises(CursorNotFound):
            page_run_range(conn, _SID, 5, "tool-row", "w006", None)
        with pytest.raises(CursorNotFound):
            page_run_range(conn, _SID, 5, "w002", "queued-row", None)

    def test_an_inverted_bound_is_refused(self):
        conn = self._run()
        with pytest.raises(InvertedRange):
            page_run_range(conn, _SID, 5, "w006", "w002", None)

    def test_a_range_page_carries_lineage_children(self):
        conn = _db()
        conn.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
            "VALUES ('a1',?,'assistant','',?,'2026-06-12T00:00:01',1)",
            (_SID, json.dumps({"llm_tool_calls": json.loads(_CALLS), "turn_id": "T1"})),
        )
        conn.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
            "VALUES ('r1',?,'tool','output',?,'2026-06-12T00:00:02',2)",
            (_SID, json.dumps({"tool_call_id": "c", "assistant_step_id": "a1"})),
        )
        conn.commit()
        (row,) = page_run_range(conn, _SID, 5, "a1", "a1", None)["messages"]
        (call,) = row["metadata"]["tool_calls"]
        assert call["id"] == "c" and call["result_state"] == "deferred"

    def test_a_focus_page_summarises_the_turns_its_stubs_name(self):
        conn = _db()
        t = _Thread(conn)
        t.add("w", "T1")
        t.add("s", "T2")
        conn.execute(
            "INSERT INTO active_turns (id,thread_id,status,started_at,ended_at) "
            "VALUES ('T1',?,'completed','2026-06-12 00:00:00','2026-06-12 00:01:00')",
            (_SID,),
        )
        conn.commit()
        assert set(page_focus(conn, _SID, 5, None)["turn_summaries"]) == {"T1"}


_EXPECTED = Path(__file__).parents[2] / "testdata" / "focus-walk-expected.json"


def rich_thread(conn: sqlite3.Connection) -> _Thread:
    """One thread with every row kind a Focus page distinguishes (synthetic content).

    In order: human turns; marked (`for_user`) updates; work rows, with tool children
    and a row with no Turn; hidden activity, a visible system notice and a result row
    that is never an anchor; a `for_user` row with no content, one whose flag is only
    truthy, one with unparseable metadata and an agent-sent `user` row (all hidden);
    finished Turns T1 and T2 and a live Turn T3 directly after T2's work; and two
    queued inputs (a human steer and an agent send) with no position.
    """
    t = _Thread(conn)
    t.add("u")
    t.add("s", "T1")
    t.add("w", "T1")
    t.add("t")
    t.add("w", "T1")
    t.add("c")
    t.add("c", meta={"type": "error"}, content="notice")
    t.add("s", "T1", content="")
    t.add("w", "T1")
    t.add("u", meta={"sender_kind": "agent"}, content="from a peer")
    t.add("w")
    t.add("c", meta={"type": "result"}, content="result")
    t.add("s", "T2")
    t.add("w", "T2")
    t.add("s", "T2", meta={"for_user": 1})
    t.add("w", "T2")
    t.n += 1
    conn.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq,delivery_state) "
        "VALUES (?,?,'assistant','x','{not json','2026-06-12T00:00:30',?,'delivered')",
        (f"b{t.n:03d}", _SID, t.n),
    )
    t.ids.append(f"b{t.n:03d}")
    t.add("w", "T2")
    t.add("u")
    t.add("w", "T2")
    t.add("w", "T3")
    t.add("t")
    t.add("w", "T3")
    t.add("w", "T3")
    steer = t.add("u", seq=False)
    agent = t.add("u", seq=False, meta={"sender_kind": "agent"}, content="from a peer")
    for order, mid in ((2, steer), (1, agent)):
        conn.execute("UPDATE messages SET input_order=? WHERE id=?", (order, mid))
    _live(conn, "T3")
    conn.commit()
    return t


def _payload(page) -> dict:
    if isinstance(page, tuple):
        messages, has_more, oldest_id, summaries = page
        page = {
            "messages": messages, "has_more": has_more, "oldest_id": oldest_id,
            "turn_summaries": summaries,
        }
    return json.loads(json.dumps(page, default=str))


def rich_cases(conn: sqlite3.Connection, focus_page) -> dict[str, dict]:
    """Every first page and its older pages, for both views, keyed by case name.

    `focus_page(conn, limit, before, scan_cap)` is the Focus read under test; the
    plain view reads through `thread_paging`. Older pages follow the cursor the page
    before them returned.
    """
    out: dict[str, dict] = {}

    def chain(name: str, read, limit: int, pages: int) -> None:
        before = None
        for number in range(1, pages + 1):
            page = _payload(read(limit, before))
            out[f"{name}-page{number}"] = page
            before = page["oldest_id"]

    chain("focus-limit3", lambda n, b: focus_page(conn, n, b, FOCUS_SCAN_CAP), 3, 4)
    chain("focus-limit50", lambda n, b: focus_page(conn, n, b, FOCUS_SCAN_CAP), 50, 1)
    chain("focus-cap5", lambda n, b: focus_page(conn, n, b, 5), 50, 4)
    chain(
        "plain-limit4",
        lambda n, b: paging.read_authorized_page_with_summaries(conn, _SID, n, b), 4, 3,
    )
    return out


class TestExpectedPayloads:
    """Literal pages, written out by the walk this one replaced and compared whole."""

    def test_every_page_of_the_rich_thread_matches_its_literal_payload(self):
        conn = _db()
        rich_thread(conn)
        expected = json.loads(_EXPECTED.read_text())
        actual = rich_cases(
            conn,
            lambda c, limit, before, cap: page_focus(c, _SID, limit, before, scan_cap=cap),
        )
        assert actual.keys() == expected.keys()
        for name in expected:
            assert actual[name] == expected[name], name

    def test_the_fixture_reaches_every_case_it_exists_for(self):
        expected = json.loads(_EXPECTED.read_text())
        pages = [page for name, page in expected.items() if name.startswith("focus-")]
        stubs = [run for page in pages for run in page["runs"]]
        shown = [m for page in pages for m in page["messages"]]
        # Marked updates and the human turn show on their own; hidden rows collapse.
        assert any(m["role"] == "assistant" and m["focus_visible"] for m in shown)
        assert any(not m["focus_visible"] for m in shown)  # the queued agent send
        # The live Turn's rows never share a stub with the finished Turn before it.
        assert any(run["turn_ids"] == ["T3"] for run in stubs)
        assert not any({"T2", "T3"} <= set(run["turn_ids"]) for run in stubs)
        # The cap stops a walk inside a run, and the next page continues it.
        capped = [expected[f"focus-cap5-page{n}"] for n in (1, 2, 3, 4)]
        assert any(run["continues_older"] for page in capped for run in page["runs"])
        assert all(page["has_more"] for page in capped[:-1])
        # Queued inputs ride the first page only, head of the queue first.
        for name, page in expected.items():
            ids = [m["id"] for m in page["messages"] if m["delivery_state"] == "queued"]
            if name.startswith("focus-") and name.endswith("page1"):
                assert ids == ["u026", "u025"], name
            elif name.startswith("focus-"):
                assert ids == [], name

    def test_the_walk_reports_how_many_anchors_it_inspected(self):
        conn = _db()
        t = _Thread(conn)
        for _ in range(3):
            t.add("w", "T1")
        t.add("s", "T1")
        for _ in range(4):
            t.add("w", "T1")
        conn.commit()
        assert walk_page(conn, _SID, None, 5, 4).scanned == 4  # the cap
        assert walk_page(conn, _SID, None, 1, 50).scanned == 5  # the budget fills at seq 4
        assert walk_page(conn, _SID, None, 5, 50).scanned == 8  # the whole thread
