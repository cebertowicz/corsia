
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


# Keep this unit test isolated from api.__init__ and its unrelated provider stack.
_module_path = Path(__file__).parents[1] / "api" / "thread_paging.py"
_spec = importlib.util.spec_from_file_location("_test_thread_paging", _module_path)
assert _spec is not None and _spec.loader is not None
_thread_paging = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_thread_paging)
CursorNotFound = _thread_paging.CursorNotFound
load_tool_result = _thread_paging.load_tool_result
page_messages = _thread_paging.page_messages
page_turn_summaries = _thread_paging._page_turn_summaries
from core.tool_lineage import ToolLineageError


_SID = "sess-pg"
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
        "delivery_state TEXT NOT NULL DEFAULT 'delivered', "
        "delivery_intent TEXT)"
    )
    conn.execute(
        "CREATE INDEX idx_messages_turn_calls ON messages(thread_id, role, "
        "json_extract(metadata, '$.turn_id')) WHERE role='assistant' AND json_valid(metadata)"
    )
    conn.execute(
        "CREATE TABLE active_turns (id TEXT, thread_id TEXT, status TEXT, started_at TEXT, ended_at TEXT, error TEXT)"
    )
    return conn


def _insert(
    conn, mid, role, content, *, ts, metadata=None, thread_id=_SID, seq=None,
    active_turn_id=None,
):
    conn.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,metadata,created_at,seq,active_turn_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (mid, thread_id, role, content, metadata, ts, seq, active_turn_id),
    )


def _seed_plain(conn, n, *, thread_id=_SID):
    ids = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        mid = f"m{i:04d}"
        _insert(
            conn, mid, role, f"msg {i}",
            ts=f"2026-06-12T00:00:{i:04d}", thread_id=thread_id, seq=i + 1,
        )
        ids.append(mid)
    conn.commit()
    return ids



class TestFirstPage:
    def test_rowid_not_in_response(self):
        conn = _db()
        _seed_plain(conn, 5)
        msgs, _, _ = page_messages(conn, _SID, limit=10, before=None)
        assert msgs, "expected messages"
        for msg in msgs:
            assert "rowid" not in msg, f"rowid leaked into API response for id={msg.get('id')}"

    def test_rowid_not_in_response_cursor_page(self):
        conn = _db()
        _seed_plain(conn, 6)
        _, _, oldest_id = page_messages(conn, _SID, limit=3, before=None)
        assert oldest_id is not None
        msgs2, _, _ = page_messages(conn, _SID, limit=3, before=oldest_id)
        assert msgs2, "expected older messages on cursor page"
        for msg in msgs2:
            assert "rowid" not in msg, f"rowid leaked on cursor page for id={msg.get('id')}"

    def test_a_seed_is_never_sent_and_never_counted(self):
        conn = _db()
        _insert(conn, "seed-old", "system", "starting context", ts="2026-06-12T00:00:0000",
                metadata=json.dumps({"type": "seed"}), seq=1)
        ids = [f"m{i}" for i in range(2, 6)]
        for seq, mid in enumerate(ids, start=2):
            _insert(conn, mid, "user" if seq % 2 else "assistant", f"msg {seq}",
                    ts=f"2026-06-12T00:00:{seq:04d}", seq=seq)
        _insert(conn, "seed-mid", "system", "starting context", ts="2026-06-12T00:00:0006",
                metadata=json.dumps({"cc": True, "type": "seed"}), seq=6)
        _insert(conn, "late", "user", "msg 7", ts="2026-06-12T00:00:0007", seq=7)
        conn.commit()

        # Positioned rows only, then with an unpositioned seed (the wall-clock read).
        for unpositioned in (False, True):
            if unpositioned:
                _insert(conn, "queued-seed", "system", "starting context",
                        ts="2026-06-12T00:00:0008", metadata=json.dumps({"type": "seed"}))
                conn.commit()
            msgs, has_more, oldest_id = page_messages(conn, _SID, limit=5, before=None)
            assert [m["id"] for m in msgs] == [*ids, "late"]
            assert (has_more, oldest_id) == (False, "m2")
            everything, _, _ = page_messages(conn, _SID, limit=None, before=None)
            assert [m["id"] for m in everything] == [*ids, "late"]
            assert "starting context" not in json.dumps([*msgs, *everything])

    def test_returns_last_n_display_messages(self):
        conn = _db()
        _seed_plain(conn, 10)
        msgs, has_more, oldest_id = page_messages(conn, _SID, limit=4, before=None)
        assert [m["id"] for m in msgs] == ["m0006", "m0007", "m0008", "m0009"]
        assert has_more is True
        assert oldest_id == "m0006"

    def test_backdated_accepted_row_survives_latest_page_hard_refresh(self):
        conn = _db()
        _seed_plain(conn, 60)
        _insert(
            conn,
            "backdated-accepted",
            "user",
            "late accepted steer",
            ts="2026-06-11T00:00:0000",
            seq=61,
        )
        conn.commit()

        messages, has_more, _ = page_messages(conn, _SID, limit=50, before=None)

        assert has_more is True
        assert messages[-1]["id"] == "backdated-accepted"
        assert len(messages) == 50

    def test_pending_row_composes_after_every_sequenced_row(self):
        # #1087 reload half: a message queued mid-turn is persisted early
        # (created_at before the reply) and sequenced late. While it is still
        # undelivered (NULL seq) it must render below the whole reply it will
        # follow, never at the spot its timestamp implies.
        conn = _db()
        _insert(conn, "m1", "user", "first", ts="2026-08-15T03:00:00.000000", seq=1)
        _insert(conn, "m3", "assistant", "reply part 1", ts="2026-08-15T03:00:02.000000", seq=2)
        _insert(conn, "m4", "assistant", "reply part 2", ts="2026-08-15T03:00:03.000000", seq=3)
        _insert(
            conn, "m2", "user", "queued mid-turn",
            ts="2026-08-15T03:00:01.000000", seq=None,
        )
        conn.execute(
            "UPDATE messages SET delivery_state='queued' WHERE id='m2'"
        )
        conn.commit()

        unbounded, _, _ = page_messages(conn, _SID, limit=None, before=None)
        bounded, _, _ = page_messages(conn, _SID, limit=4, before=None)

        for page in (unbounded, bounded):
            assert [m["id"] for m in page] == ["m1", "m3", "m4", "m2"]

    def test_has_more_false_when_all_fit(self):
        conn = _db()
        _seed_plain(conn, 4)
        msgs, has_more, oldest_id = page_messages(conn, _SID, limit=10, before=None)
        assert len(msgs) == 4
        assert has_more is False

    def test_exact_fit_has_more_false(self):
        conn = _db()
        _seed_plain(conn, 4)
        msgs, has_more, oldest_id = page_messages(conn, _SID, limit=4, before=None)
        assert len(msgs) == 4
        assert has_more is False

    def test_empty_session(self):
        conn = _db()
        msgs, has_more, oldest_id = page_messages(conn, _SID, limit=4, before=None)
        assert msgs == []
        assert has_more is False
        assert oldest_id is None

    def test_no_limit_returns_full_transcript(self):
        conn = _db()
        _seed_plain(conn, 7)
        msgs, has_more, oldest_id = page_messages(conn, _SID, limit=None, before=None)
        assert len(msgs) == 7
        assert has_more is False

    @pytest.mark.parametrize("limit", [1, None])
    def test_private_corsia_delivery_metadata_never_reaches_rest(self, limit):
        conn = _db()
        _insert(
            conn,
            "private-delivery",
            "user",
            "retained input",
            ts="2026-06-12T00:00:0000",
            metadata=json.dumps({
                "public_key": "visible",
                "_corsia_send_delivery_state": "ambiguous",
                "_corsia_future_private_marker": "hidden",
            }),
        )
        conn.commit()

        messages, _, _ = page_messages(conn, _SID, limit=limit, before=None)

        assert messages[0]["metadata"] == {"public_key": "visible"}

    def test_queue_intent_projects_from_the_canonical_input_row(self):
        conn = _db()
        _insert(
            conn, "queued", "user", "later", ts="2026-06-12T00:00:0000",
            metadata=json.dumps({"delivery_intent": "apply_now"}),
        )
        conn.execute(
            "UPDATE messages SET delivery_intent='queue_after_turn' WHERE id='queued'"
        )
        conn.commit()

        messages, _, _ = page_messages(conn, _SID, limit=10, before=None)

        assert messages[0]["metadata"]["delivery_intent"] == "queue_after_turn"

    def test_bounded_and_unbounded_keep_ordinary_system_rows(self):
        conn = _db()
        _insert(conn, "sys-null", "system", "null metadata",
                ts="2026-06-12T00:00:0000")
        _insert(conn, "sys-empty", "system", "empty metadata",
                ts="2026-06-12T00:00:0001", metadata="{}")
        _insert(conn, "sys-missing-type", "system", "missing type",
                ts="2026-06-12T00:00:0002", metadata=json.dumps({"notice": True}))
        _insert(conn, "sys-notice", "system", "typed notice",
                ts="2026-06-12T00:00:0003", metadata=json.dumps({"type": "notice"}))
        _insert(conn, "sys-result", "system", "result marker",
                ts="2026-06-12T00:00:0004", metadata=json.dumps({"type": "result"}))
        conn.commit()

        bounded, bounded_more, _ = page_messages(conn, _SID, limit=10, before=None)
        unbounded, unbounded_more, _ = page_messages(conn, _SID, limit=None, before=None)

        expected = ["sys-null", "sys-empty", "sys-missing-type", "sys-notice"]
        assert [row["id"] for row in bounded] == expected
        assert [row["id"] for row in unbounded] == expected
        assert bounded_more is False
        assert unbounded_more is False

    def test_before_with_no_limit_raises(self):
        conn = _db()
        ids = _seed_plain(conn, 5)
        with pytest.raises(ValueError, match="spec §1.8"):
            page_messages(conn, _SID, limit=None, before=ids[3])

    def test_before_with_zero_limit_raises(self):
        conn = _db()
        ids = _seed_plain(conn, 5)
        with pytest.raises(ValueError, match="spec §1.8"):
            page_messages(conn, _SID, limit=0, before=ids[3])



class TestOlderPages:
    def test_before_cursor_returns_older_window(self):
        conn = _db()
        _seed_plain(conn, 10)
        page1, more1, oldest1 = page_messages(conn, _SID, limit=4, before=None)
        assert more1 is True
        assert oldest1 == "m0006"

        page2, more2, oldest2 = page_messages(conn, _SID, limit=4, before=oldest1)
        assert [m["id"] for m in page2] == ["m0002", "m0003", "m0004", "m0005"]
        assert more2 is True

        page3, more3, _ = page_messages(conn, _SID, limit=4, before=oldest2)
        assert [m["id"] for m in page3] == ["m0000", "m0001"]
        assert more3 is False  # reached the oldest message

    def test_full_walk_covers_every_message_once(self):
        conn = _db()
        _seed_plain(conn, 23)  # not a multiple of the page size
        seen: list[str] = []
        page, more, oldest_id = page_messages(conn, _SID, limit=5, before=None)
        while True:
            seen = [m["id"] for m in page] + seen
            if not more:
                break
            page, more, oldest_id = page_messages(conn, _SID, limit=5, before=oldest_id)
        assert seen == [f"m{i:04d}" for i in range(23)]
        assert len(seen) == len(set(seen))  # no duplicates across pages

    def test_unknown_cursor_raises(self):
        conn = _db()
        _seed_plain(conn, 5)
        with pytest.raises(CursorNotFound):
            page_messages(conn, _SID, limit=4, before="does-not-exist")

    def test_cursor_scoped_to_session(self):
        conn = _db()
        _insert(conn, "foreign", "user", "x",
                ts="2026-06-12T00:00:0000", thread_id="other")
        _seed_plain(conn, 5, thread_id=_SID)
        conn.commit()
        with pytest.raises(CursorNotFound):
            page_messages(conn, _SID, limit=4, before="foreign")

    def test_timestamp_tie_composite_cursor_no_skip(self):
        conn = _db()
        SAME_TS = "2026-06-12T00:00:0000"
        for idx in range(5):
            _insert(conn, f"tied-{idx:02d}", "user", f"msg {idx}", ts=SAME_TS)
        conn.commit()
        page1, more1, oldest1 = page_messages(conn, _SID, limit=2, before=None)
        assert more1 is True
        assert oldest1 is not None
        page2, more2, oldest2 = page_messages(conn, _SID, limit=2, before=oldest1)
        assert more2 is True
        page3, more3, _ = page_messages(conn, _SID, limit=2, before=oldest2)
        assert more3 is False
        all_ids = (
            [m["id"] for m in page1]
            + [m["id"] for m in page2]
            + [m["id"] for m in page3]
        )
        assert len(all_ids) == 5
        assert len(set(all_ids)) == 5



class TestCollapseInteraction:
    def _seed_tool_turn(self, conn, base_idx, *, with_text=True):
        a_id = f"a{base_idx:04d}"
        t_id = f"t{base_idx:04d}"
        meta = json.dumps({
            "step_type": "text" if with_text else "tool_calls",
            "tool_calls": [{"id": f"tc{base_idx}", "name": "Bash", "hint": "Bash", "args": "", "result": None}],
        })
        _insert(conn, a_id, "assistant", "running a tool" if with_text else "",
                ts=f"2026-06-12T01:00:{base_idx * 2:04d}", metadata=meta)
        _insert(conn, t_id, "tool", "tool output",
                ts=f"2026-06-12T01:00:{base_idx * 2 + 1:04d}",
                metadata=json.dumps({"tool_call_id": f"tc{base_idx}", "name": "Bash"}))
        return a_id

    def test_tool_rows_collapse_and_count_as_one_display(self):
        conn = _db()
        a_ids = [self._seed_tool_turn(conn, i) for i in range(5)]
        conn.commit()
        msgs, has_more, oldest_id = page_messages(conn, _SID, limit=10, before=None)
        assert [m["id"] for m in msgs] == a_ids
        assert has_more is False
        assert msgs[0]["metadata"]["tool_calls"][0]["result"] == "tool output"

    def test_page_can_defer_tool_results_until_detail_is_requested(self):
        conn = _db()
        assistant_id = self._seed_tool_turn(conn, 0)
        conn.commit()

        messages, _, _ = page_messages(
            conn, _SID, limit=10, before=None, include_tool_results=False,
        )

        call = messages[0]["metadata"]["tool_calls"][0]
        assert "result" not in call
        assert call["result_state"] == "deferred"
        assert load_tool_result(conn, _SID, assistant_id, "tc0") == "tool output"

    def test_page_marks_genuinely_absent_tool_results(self):
        conn = _db()
        _insert(
            conn,
            "absent-result",
            "assistant",
            "",
            ts="2026-06-12T01:00:0000",
            metadata=json.dumps({
                "tool_calls": [{"id": "absent-call", "name": "Bash", "args": ""}],
            }),
        )
        conn.commit()

        messages, _, _ = page_messages(
            conn, _SID, limit=10, before=None, include_tool_results=False,
        )

        call = messages[0]["metadata"]["tool_calls"][0]
        assert "result" not in call
        assert call["result_state"] == "absent"
        assert load_tool_result(conn, _SID, "absent-result", "absent-call") is None

    def test_has_more_correct_despite_collapse(self):
        conn = _db()
        a_ids = [self._seed_tool_turn(conn, i) for i in range(6)]
        conn.commit()
        msgs, has_more, oldest_id = page_messages(conn, _SID, limit=2, before=None)
        assert [m["id"] for m in msgs] == a_ids[-2:]
        assert has_more is True

    def test_paged_walk_with_tools_no_loss(self):
        conn = _db()
        a_ids = [self._seed_tool_turn(conn, i) for i in range(11)]
        conn.commit()
        seen: list[str] = []
        page, more, oldest_id = page_messages(conn, _SID, limit=3, before=None)
        while True:
            ids = [m["id"] for m in page]
            assert all(i.startswith("a") for i in ids)
            seen = ids + seen
            if not more:
                break
            page, more, oldest_id = page_messages(conn, _SID, limit=3, before=oldest_id)
        assert seen == a_ids
        assert len(seen) == len(set(seen))

    def test_rowid_beats_lexicographic_id_for_same_timestamp(self):
        conn = _db()
        SAME_TS = "2026-06-12T00:00:0000"
        meta_asst = json.dumps({
            "step_type": "text",
            "tool_calls": [{"id": "tc1", "name": "Bash", "hint": "Bash", "args": "", "result": None}],
        })
        meta_tool = json.dumps({"tool_call_id": "tc1", "name": "Bash"})
        _insert(conn, "asst-ZZZ", "assistant", "turn with tool", ts=SAME_TS, metadata=meta_asst)
        _insert(conn, "tool-ZZZ", "tool", "tool output", ts=SAME_TS, metadata=meta_tool)
        _insert(conn, "asst-BBB", "assistant", "next turn", ts=SAME_TS)
        conn.commit()

        msgs, has_more, _ = page_messages(conn, _SID, limit=10, before=None)
        assert len(msgs) == 2, "tool-ZZZ must collapse into asst-ZZZ"
        assert msgs[0]["id"] == "asst-ZZZ", "asst-ZZZ (first inserted) must be older"
        assert msgs[1]["id"] == "asst-BBB", "asst-BBB (last inserted) must be newer"
        assert msgs[0]["metadata"]["tool_calls"][0]["result"] == "tool output"

    def test_701_terminal_dangling_tool_calls_limit_1_no_keyerror(self):
        """#701: newest turn ends on dangling tool_calls (no following text
        row) — limit=1 must not KeyError on the synthetic terminal row.
        """
        conn = _db()
        _insert(conn, "u0000", "user", "do a thing", ts="2026-06-12T00:00:0000")
        meta = json.dumps({
            "step_type": "tool_calls",
            "tool_calls": [{"id": "tc1", "name": "Bash", "hint": "Bash", "args": "", "result": None}],
        })
        _insert(conn, "a0001", "assistant", "", ts="2026-06-12T00:00:0001", metadata=meta)
        conn.commit()

        msgs, has_more, oldest_id = page_messages(conn, _SID, limit=1, before=None)

        assert len(msgs) == 1
        assert msgs[0]["id"] == "a0001"
        assert has_more is True
        assert oldest_id == "a0001"

    def test_orphan_tool_rows_never_hide_or_anchor_a_display_page(self):
        conn = _db()
        _insert(conn, "anchor-asst", "assistant", "before tools",
                ts="2026-06-12T00:00:0000")
        for i in range(12):
            _insert(conn, f"tool-{i:02d}", "tool", f"result {i}",
                    ts=f"2026-06-12T00:01:{i:04d}",
                    metadata=json.dumps({"tool_call_id": f"tc{i}", "name": "Bash"}))
        conn.commit()

        msgs, has_more, oldest_id = page_messages(conn, _SID, limit=1, before=None)
        assert [message["id"] for message in msgs] == ["anchor-asst"]
        assert has_more is False
        assert oldest_id == "anchor-asst"

    def test_anchor_page_fetches_every_linked_result_without_fixed_overfetch(self):
        conn = _db()
        call_ids = [f"tc-{index}" for index in range(12)]
        _insert(
            conn,
            "step-many",
            "assistant",
            "many tools",
            ts="2026-06-12T03:00:0000",
            metadata=json.dumps({
                "step_type": "tool_calls",
                "llm_tool_calls": [
                    {
                        "id": tool_id,
                        "type": "function",
                        "function": {"name": "Read", "arguments": "{}"},
                    }
                    for tool_id in call_ids
                ],
            }),
        )
        for index, tool_id in enumerate(call_ids):
            _insert(
                conn,
                f"result-{index}",
                "tool",
                f"output-{index}",
                ts=f"2026-06-12T03:00:{index + 1:04d}",
                metadata=json.dumps({
                    "tool_call_id": tool_id,
                    "assistant_step_id": "step-many",
                    "name": "Read",
                }),
            )
        conn.commit()

        page, has_more, oldest_id = page_messages(conn, _SID, limit=1, before=None)

        assert [message["id"] for message in page] == ["step-many"]
        assert has_more is False
        assert oldest_id == "step-many"
        assert [call["result"] for call in page[0]["metadata"]["tool_calls"]] == [
            f"output-{index}" for index in range(12)
        ]

    def test_tool_child_cursor_returns_complete_parent_step_but_never_anchors_page(self):
        conn = _db()
        _insert(
            conn,
            "step-before-child",
            "assistant",
            "using tool",
            ts="2026-06-12T04:00:0000",
            metadata=json.dumps({
                "step_type": "tool_calls",
                "llm_tool_calls": [{
                    "id": "tc-child",
                    "type": "function",
                    "function": {"name": "Read", "arguments": "{}"},
                }],
            }),
        )
        _insert(
            conn,
            "result-child",
            "tool",
            "complete child",
            ts="2026-06-12T04:00:0001",
            metadata=json.dumps({
                "tool_call_id": "tc-child",
                "assistant_step_id": "step-before-child",
                "name": "Read",
            }),
        )
        _insert(
            conn, "later-user", "user", "later",
            ts="2026-06-12T04:00:0002",
        )
        conn.commit()

        page, has_more, oldest_id = page_messages(
            conn, _SID, limit=1, before="result-child",
        )

        assert [message["id"] for message in page] == ["step-before-child"]
        assert oldest_id == "step-before-child"
        assert has_more is False
        assert page[0]["metadata"]["tool_calls"][0]["result"] == "complete child"

    @pytest.mark.parametrize("corruption", ["missing", "wrong_parent", "cross_thread"])
    def test_selected_call_tolerates_authoritative_lineage_corruption(self, corruption):
        conn = _db()
        call = {
            "id": "reused-call",
            "type": "function",
            "function": {"name": "Read", "arguments": "{}"},
        }
        if corruption == "wrong_parent":
            _insert(
                conn,
                "wrong-parent",
                "assistant",
                "older unrelated step",
                ts="2026-06-12T05:00:0000",
                metadata=json.dumps({
                    "llm_tool_calls": [{
                        "id": "different-call",
                        "type": "function",
                        "function": {"name": "Read", "arguments": "{}"},
                    }],
                }),
            )
            claimed_parent = "wrong-parent"
        elif corruption == "cross_thread":
            _insert(
                conn,
                "foreign-parent",
                "assistant",
                "foreign step",
                ts="2026-06-12T05:00:0000",
                metadata=json.dumps({"llm_tool_calls": [call]}),
                thread_id="foreign-thread",
            )
            claimed_parent = "foreign-parent"
        else:
            claimed_parent = "missing-parent"

        _insert(
            conn,
            "selected-step",
            "assistant",
            "selected",
            ts="2026-06-12T05:00:0010",
            metadata=json.dumps({"llm_tool_calls": [call]}),
        )
        _insert(
            conn,
            "corrupt-result",
            "tool",
            "must not be hidden",
            ts="2026-06-12T05:00:0011",
            metadata=json.dumps({
                "tool_call_id": "reused-call",
                "assistant_step_id": claimed_parent,
                "name": "Read",
            }),
        )
        conn.commit()

        # Display paging must render the page rather than 500 the thread; the
        # corrupt result simply loses its linkage. Model replay retains the
        # loud failure (see test_thread_history_tool_lineage).
        display, has_more, oldest = page_messages(conn, _SID, limit=1, before=None)
        assert display, "page renders despite the corrupt tool row"

    def test_valid_other_step_child_with_reused_call_id_stays_filtered(self):
        conn = _db()
        call = {
            "id": "shared-call",
            "type": "function",
            "function": {"name": "Read", "arguments": "{}"},
        }
        _insert(
            conn,
            "older-step",
            "assistant",
            "older",
            ts="2026-06-12T06:00:0000",
            metadata=json.dumps({"llm_tool_calls": [call]}),
        )
        _insert(
            conn,
            "older-result",
            "tool",
            "belongs only to older",
            ts="2026-06-12T06:00:0001",
            metadata=json.dumps({
                "tool_call_id": "shared-call",
                "assistant_step_id": "older-step",
                "name": "Read",
            }),
        )
        _insert(
            conn,
            "selected-step",
            "assistant",
            "selected",
            ts="2026-06-12T06:00:0010",
            metadata=json.dumps({"llm_tool_calls": [call]}),
        )
        conn.commit()

        page, _, _ = page_messages(conn, _SID, limit=1, before=None)

        assert [message["id"] for message in page] == ["selected-step"]
        assert page[0]["metadata"]["tool_calls"][0]["result"] is None


def test_turn_summary_has_whole_display_activity_count_not_page_share():
    conn = _db()
    turn = _FOCUS_SUMMARY_FIXTURE["turn"]
    turn_id = turn["id"]
    conn.execute(
        "INSERT INTO active_turns (id,thread_id,status,started_at,ended_at) VALUES (?,?,?,?,?)",
        (turn_id, turn["thread_id"], turn["status"], turn["started_at"], turn["ended_at"]),
    )
    for message in _FOCUS_SUMMARY_FIXTURE["messages"]:
        _insert(
            conn, message["id"], message["role"], message["content"],
            ts=message["created_at"], seq=message["seq"],
            metadata=json.dumps(message["metadata"]),
        )
    conn.commit()

    newest, _, cursor = page_messages(conn, _SID, limit=1, before=None)
    older, _, _ = page_messages(conn, _SID, limit=2, before=cursor)
    expected = _FOCUS_SUMMARY_FIXTURE["expected"]["activity_count"]

    assert page_turn_summaries(conn, _SID, newest)[turn_id]["activity_count"] == expected
    assert page_turn_summaries(conn, _SID, older)[turn_id]["activity_count"] == expected
    assert page_turn_summaries(conn, _SID, newest)[turn_id] == {
        "started_at": turn["started_at"],
        "ended_at": turn["ended_at"],
        "status": turn["status"],
        "activity_count": expected,
    }


def test_turn_summary_renders_stored_naive_utc_stamps_as_explicit_utc():
    conn = _db()
    turn_id = "naive-utc-stamp"
    conn.execute(
        "INSERT INTO active_turns (id,thread_id,status,started_at,ended_at) VALUES (?,?,?,?,?)",
        (
            turn_id, _SID, "completed",
            "2026-06-12T00:00:00.123456", "2026-06-12T00:04:00.654321",
        ),
    )
    _insert(
        conn, "naive-step", "assistant", "", ts="2026-06-12T00:00:0000", seq=1,
        metadata=json.dumps({"turn_id": turn_id, "tool_calls": [{"id": "one-step"}]}),
    )
    conn.commit()

    page, _, _ = page_messages(conn, _SID, limit=1, before=None)
    summary = page_turn_summaries(conn, _SID, page)[turn_id]

    assert summary["started_at"] == "2026-06-12T00:00:00.123456Z"
    assert summary["ended_at"] == "2026-06-12T00:04:00.654321Z"


def test_turn_summary_follows_display_call_fallback_when_llm_calls_are_empty():
    conn = _db()
    turn_id = "empty-llm-fallback"
    conn.execute(
        "INSERT INTO active_turns (id,thread_id,status,started_at,ended_at) VALUES (?,?,?,?,?)",
        (turn_id, _SID, "completed", "2026-06-12T00:00:00Z", "2026-06-12T00:01:00Z"),
    )
    _insert(
        conn, "fallback", "assistant", "", ts="2026-06-12T00:00:0000", seq=1,
        metadata=json.dumps({
            "turn_id": turn_id, "llm_tool_calls": [],
            "tool_calls": [{"id": "fallback-call", "name": "Read"}],
        }),
    )
    conn.commit()

    page, _, _ = page_messages(conn, _SID, limit=1, before=None)

    assert [call["id"] for call in page[0]["metadata"]["tool_calls"]] == ["fallback-call"]
    assert page_turn_summaries(conn, _SID, page)[turn_id]["activity_count"] == 1


def test_turn_summary_query_seeks_the_functional_index_at_64k_rows():
    conn = _db()
    turn_id = "turn-at-the-end"
    conn.execute(
        "INSERT INTO active_turns (id,thread_id,status,started_at,ended_at) VALUES (?,?,?,?,?)",
        (turn_id, _SID, "completed", "2026-06-12T00:00:00Z", "2026-06-12T00:01:00Z"),
    )
    conn.executemany(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            (
                f"history-{index}", _SID, "assistant", "", json.dumps({"turn_id": f"old-{index}"}),
                f"2026-06-12T01:{index:05d}", index + 1,
            )
            for index in range(64_000)
        ],
    )
    _insert(
        conn, "target", "assistant", "", ts="2026-06-12T02:00:00Z", seq=64_001,
        metadata=json.dumps({"turn_id": turn_id, "reasoning": "done"}),
    )
    conn.commit()

    plan = " ".join(row[3] for row in conn.execute(
        "EXPLAIN QUERY PLAN SELECT json_extract(metadata, '$.turn_id') FROM messages "
        "INDEXED BY idx_messages_turn_calls "
        "WHERE thread_id=? AND role='assistant' AND json_valid(metadata) "
        "AND json_extract(metadata, '$.turn_id') IN (?)",
        (_SID, turn_id),
    ))

    assert "SEARCH messages USING INDEX idx_messages_turn_calls" in plan
    assert page_turn_summaries(conn, _SID, [{
        "id": "target", "role": "assistant", "metadata": {"turn_id": turn_id},
    }])[turn_id]["activity_count"] == 1
