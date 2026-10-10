"""Sub-Turn checkpoint boundaries: a boundary may land inside one Turn.

A Turn larger than the model window used to make a thread permanently
unrecoverable: the smallest foldable unit was a whole Turn, so the raw-tail
minimum kept the oversized Turn whole and no legal cut point existed anywhere.

These tests pin the two correctness rules that make an interior cut safe — a
tool_use is never separated from its tool_result, and reasoning is never
separated from its signature — plus the replay shape a provider will accept.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from history.types import (
    PORTABLE_HISTORY_FORMAT_VERSION,
    HistoryBoundary,
    PortableHistoryError,
    PortableHistoryProjection,
    PortableMessage,
    PortableTurn,
)
from history.turns import (
    assistant_step_batches,
    group_assistant_steps,
    turn_interior_cut_points,
)
from history.projection import render_portable_history
from history.adapters import to_native_history

EPOCH = 7


# --------------------------------------------------------------------------
# Durable seeding
# --------------------------------------------------------------------------


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            history_epoch INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            created_at TEXT NOT NULL,
            delivery_state TEXT NOT NULL DEFAULT 'delivered',
            active_turn_id TEXT,
            seq INTEGER NOT NULL,
            UNIQUE(thread_id, seq)
        );
        CREATE TABLE active_turns (
            turn_order INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            thread_id TEXT NOT NULL,
            execution_epoch INTEGER NOT NULL,
            model TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            boot_epoch TEXT NOT NULL
        );
        """
    )
    db.execute(
        "INSERT INTO threads (id, history_epoch) VALUES ('thread-1', ?)",
        (EPOCH,),
    )
    return db


def _turn_row(db: sqlite3.Connection, turn_id: str, status: str = "completed") -> None:
    db.execute(
        "INSERT INTO active_turns "
        "(id,thread_id,execution_epoch,model,status,started_at,ended_at,boot_epoch) "
        "VALUES (?, 'thread-1', 0, 'native/test', ?, 'start', ?, 'fixture')",
        (turn_id, status, None if status == "active" else "end"),
    )


def _message(
    db: sqlite3.Connection,
    message_id: str,
    seq: int,
    role: str,
    content: str,
    *,
    turn_id: str,
    metadata: dict | None = None,
) -> None:
    meta = dict(metadata or {})
    meta["turn_id"] = turn_id
    db.execute(
        "INSERT INTO messages "
        "(id, thread_id, role, content, metadata, created_at, seq) "
        "VALUES (?, 'thread-1', ?, ?, ?, ?, ?)",
        (message_id, role, content, json.dumps(meta), f"time-{seq:03d}", seq),
    )


def _anthropic_reasoning(text: str, signature: str) -> dict:
    """One assistant row carrying signed Anthropic thinking, as CC persists it."""
    return {
        "reasoning": text,
        "reasoning_provenance": {
            "source_harness": "claude-code",
            "source_provider": "anthropic",
            "source_effective_model": "claude-opus-5",
            "representation_kind": "anthropic_thinking",
            "native_signatures": [signature],
            "native_blocks": [
                {"type": "thinking", "thinking": text, "signature": signature},
            ],
        },
    }


def _seed_multi_step_turn(db: sqlite3.Connection) -> None:
    """One completed Turn holding three Assistant Steps.

    Groups are ``[u1 a1 r1]``, ``[a2 r2]``, ``[a3]`` — so the legal interior
    cut points are seq 3 and seq 5, and seq 2 / seq 4 would split a tool pair.
    """
    _turn_row(db, "turn-1")
    _message(db, "u1", 1, "user", "investigate", turn_id="turn-1")
    _message(
        db,
        "a1",
        2,
        "assistant",
        "reading the file",
        turn_id="turn-1",
        metadata={
            "llm_tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "Read", "arguments": '{"path":"a"}'},
            }],
            **_anthropic_reasoning("weighing the first step", "sig-a1"),
        },
    )
    _message(
        db,
        "r1",
        3,
        "tool",
        "contents of a",
        turn_id="turn-1",
        metadata={"tool_call_id": "call-1", "assistant_step_id": "a1", "name": "Read"},
    )
    _message(
        db,
        "a2",
        4,
        "assistant",
        "now grepping",
        turn_id="turn-1",
        metadata={
            "llm_tool_calls": [{
                "id": "call-2",
                "type": "function",
                "function": {"name": "Grep", "arguments": '{"q":"x"}'},
            }],
            **_anthropic_reasoning("weighing the second step", "sig-a2"),
        },
    )
    _message(
        db,
        "r2",
        5,
        "tool",
        "three matches",
        turn_id="turn-1",
        metadata={"tool_call_id": "call-2", "assistant_step_id": "a2", "name": "Grep"},
    )
    _message(db, "a3", 6, "assistant", "here is what I found", turn_id="turn-1")
    db.commit()


def _boundary(db: sqlite3.Connection, seq: int) -> HistoryBoundary:
    row = db.execute(
        "SELECT id FROM messages WHERE thread_id='thread-1' AND seq=?", (seq,),
    ).fetchone()
    assert row is not None, f"no seeded message at seq {seq}"
    return HistoryBoundary(
        thread_id="thread-1",
        history_epoch=EPOCH,
        seq=seq,
        message_id=str(row["id"]),
    )


# --------------------------------------------------------------------------
# Provider-acceptance shape
# --------------------------------------------------------------------------


def assert_provider_acceptable(messages: list[dict]) -> None:
    """Assert the structural rules a provider rejects a history for.

    Mirrors the invariants this codebase already honours: a user-role anchor
    first (see ``thread_history._trim_to_window``), and no unpaired tool call
    or orphan tool result in either direction.
    """
    assert messages, "provider history cannot be empty"
    assert messages[0]["role"] == "user", (
        f"history must open on a user anchor, got {messages[0]['role']!r}"
    )
    declared: list[str] = []
    answered: list[str] = []
    for message in messages:
        if message["role"] == "assistant":
            declared.extend(call["id"] for call in message.get("tool_calls", ()))
        elif message["role"] == "tool":
            call_id = message["tool_call_id"]
            assert call_id in declared, f"orphan tool result {call_id!r}"
            answered.append(call_id)
    assert sorted(declared) == sorted(answered), (
        f"unpaired tool calls: declared={declared} answered={answered}"
    )


# --------------------------------------------------------------------------
# The cut rule
# --------------------------------------------------------------------------


class TestLegalCutPoints:
    def test_cut_points_land_at_assistant_step_group_ends(self) -> None:
        db = _db()
        _seed_multi_step_turn(db)
        turn = render_portable_history("thread-1", db=db).turns[0]

        cuts = turn_interior_cut_points(turn)

        # Groups are [u1 a1 r1] [a2 r2] [a3]; the Turn's own end is excluded.
        assert [cut.seq for cut in cuts] == [3, 5]
        assert [cut.message_id for cut in cuts] == ["r1", "r2"]

    def test_every_cut_point_ends_a_complete_group(self) -> None:
        db = _db()
        _seed_multi_step_turn(db)
        turn = render_portable_history("thread-1", db=db).turns[0]
        group_end_seqs = {
            max(message.seq for message in group)
            for group in group_assistant_steps(turn.messages)
        }

        for cut in turn_interior_cut_points(turn):
            assert cut.seq in group_end_seqs

    def test_nonmonotone_initiating_user_has_no_resume_anchor(self) -> None:
        boundary = HistoryBoundary("thread-1", EPOCH, 3, "u1")
        turn = PortableTurn(
            turn_id="turn-1",
            status="completed",
            messages=(
                PortableMessage(
                    message_id="u1",
                    seq=3,
                    turn_id="turn-1",
                    role="user",
                    content="ask",
                    created_at="t",
                ),
                PortableMessage(
                    message_id="a1",
                    seq=2,
                    turn_id="turn-1",
                    role="assistant",
                    content="answer",
                    created_at="t",
                ),
            ),
            boundary=boundary,
        )

        batches = assistant_step_batches(turn)

        assert len(batches) == 1
        assert batches[0].resume_anchor is None

    def test_single_step_turn_has_no_interior_cut_point(self) -> None:
        db = _db()
        _turn_row(db, "turn-1")
        _message(db, "u1", 1, "user", "hi", turn_id="turn-1")
        _message(db, "a1", 2, "assistant", "hello", turn_id="turn-1")
        db.commit()
        turn = render_portable_history("thread-1", db=db).turns[0]

        assert turn_interior_cut_points(turn) == ()

    def test_non_monotonic_group_is_skipped_not_guessed(self) -> None:
        """A late-written synthetic result pushes emitted order out of seq order.

        The renderer selects by seq, so that position cannot be cut cleanly.
        It is skipped rather than repaired.
        """
        db = _db()
        _turn_row(db, "turn-1")
        _message(db, "u1", 1, "user", "go", turn_id="turn-1")
        _message(
            db,
            "a1",
            2,
            "assistant",
            "first",
            turn_id="turn-1",
            metadata={"llm_tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "Bash", "arguments": "{}"},
            }]},
        )
        _message(
            db,
            "a2",
            3,
            "assistant",
            "second",
            turn_id="turn-1",
            metadata={"llm_tool_calls": [{
                "id": "call-2",
                "type": "function",
                "function": {"name": "Bash", "arguments": "{}"},
            }]},
        )
        _message(
            db,
            "r2",
            4,
            "tool",
            "second result",
            turn_id="turn-1",
            metadata={
                "tool_call_id": "call-2",
                "assistant_step_id": "a2",
                "name": "Bash",
            },
        )
        # call-1's result is written last, after the next Step already ran.
        _message(
            db,
            "r1",
            5,
            "tool",
            "late first result",
            turn_id="turn-1",
            metadata={
                "tool_call_id": "call-1",
                "assistant_step_id": "a1",
                "name": "Bash",
            },
        )
        db.commit()
        turn = render_portable_history("thread-1", db=db).turns[0]

        # Group 0 is [u1 a1 r1(seq 5)] and group 1 is [a2 r2(seq 4)]: group 0's
        # highest seq outranks group 1's lowest, so there is no clean cut.
        assert turn_interior_cut_points(turn) == ()


# --------------------------------------------------------------------------
# Constraint 1 — never cut between a tool_use and its tool_result
# --------------------------------------------------------------------------


class TestToolPairsAreNeverSplit:
    @pytest.mark.parametrize("seq", [2, 4])
    def test_boundary_on_an_assistant_step_with_pending_results_is_rejected(
        self,
        seq: int,
    ) -> None:
        db = _db()
        _seed_multi_step_turn(db)

        with pytest.raises(PortableHistoryError, match="interior cut point"):
            render_portable_history("thread-1", db=db, through=_boundary(db, seq))

    @pytest.mark.parametrize("seq", [2, 4])
    def test_illegal_boundary_is_rejected_as_a_tail_start_too(
        self,
        seq: int,
    ) -> None:
        db = _db()
        _seed_multi_step_turn(db)

        with pytest.raises(PortableHistoryError, match="interior cut point"):
            render_portable_history(
                "thread-1", db=db, start_exclusive=_boundary(db, seq),
            )

    def test_missing_tool_result_projects_stub_from_resume_anchor(self) -> None:
        """A resume-anchor slice over a lost result renders the missing stub."""
        db = _db()
        _turn_row(db, "turn-1")
        _message(db, "u1", 1, "user", "go", turn_id="turn-1")
        _message(
            db,
            "a1",
            2,
            "assistant",
            "calling",
            turn_id="turn-1",
            metadata={
                "llm_tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "Read", "arguments": "{}"},
                }]
            },
        )
        db.commit()

        projection = render_portable_history(
            "thread-1",
            db=db,
            start_exclusive=_boundary(db, 1),
        )

        stub = projection.messages[-1]
        assert stub.role == "tool"
        assert stub.tool_call_id == "call-1"
        assert stub.synthetic_tool_result is True
        assert stub.synthetic_reason == "tool_result_missing"

    def test_initiating_user_resume_anchor_is_covered_not_replayed(self) -> None:
        db = _db()
        _seed_multi_step_turn(db)
        anchor = _boundary(db, 1)

        checkpoint_source = render_portable_history(
            "thread-1",
            db=db,
            through=anchor,
        )
        tail = render_portable_history(
            "thread-1",
            db=db,
            start_exclusive=anchor,
        )

        assert [message.message_id for message in checkpoint_source.messages] == [
            "u1"
        ]
        assert [message.message_id for message in tail.messages] == [
            "a1",
            "r1",
            "a2",
            "r2",
            "a3",
        ]
        assert tail.messages[0].role == "assistant"
        assert "u1" not in {message.message_id for message in tail.messages}
        assert_provider_acceptable([
            {"role": "user", "content": "checkpoint summary"},
            *to_native_history(tail),
        ])

    def test_legal_interior_cut_keeps_both_sides_tool_complete(self) -> None:
        db = _db()
        _seed_multi_step_turn(db)
        cut = _boundary(db, 3)

        head = render_portable_history("thread-1", db=db, through=cut)
        tail = render_portable_history("thread-1", db=db, start_exclusive=cut)

        assert [message.message_id for message in head.messages] == ["u1", "a1", "r1"]
        assert [message.message_id for message in tail.messages] == [
            "a2", "r2", "a3",
        ]
        # call-1 and its result ride together; call-2 and its result ride together.
        assert_provider_acceptable(to_native_history(head))
        assert_provider_acceptable(
            to_native_history(
                tail,
                destination_provider="anthropic",
                destination_effective_model="claude-opus-5",
            )
        )


# --------------------------------------------------------------------------
# Constraint 2 — never split reasoning from its signature
# --------------------------------------------------------------------------


class TestReasoningAndSignatureAreNeverSplit:
    def test_reasoning_and_signature_stay_on_their_own_row(self) -> None:
        """They live on one row, so a row-granular cut cannot separate them."""
        db = _db()
        _seed_multi_step_turn(db)
        cut = _boundary(db, 3)

        head = render_portable_history("thread-1", db=db, through=cut)
        tail = render_portable_history("thread-1", db=db, start_exclusive=cut)

        head_a1 = next(m for m in head.messages if m.message_id == "a1")
        tail_a2 = next(m for m in tail.messages if m.message_id == "a2")
        for message, signature in ((head_a1, "sig-a1"), (tail_a2, "sig-a2")):
            blocks = message.reasoning_provenance["native_blocks"]
            assert [block["signature"] for block in blocks] == [signature]
            assert message.reasoning_text is not None

    def test_replayed_thinking_blocks_keep_their_signature_across_a_cut(self) -> None:
        db = _db()
        _seed_multi_step_turn(db)
        cut = _boundary(db, 3)

        tail = render_portable_history("thread-1", db=db, start_exclusive=cut)
        native = to_native_history(
            tail,
            destination_provider="anthropic",
            destination_effective_model="claude-opus-5",
        )

        thinking = [
            block
            for message in native
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if block.get("type") == "thinking"
        ]
        assert thinking == [
            {
                "type": "thinking",
                "thinking": "weighing the second step",
                "signature": "sig-a2",
            }
        ]

    def test_no_cut_point_ever_lands_on_an_assistant_row(self) -> None:
        """A cut between an Assistant Step and its own reasoning is unnameable."""
        db = _db()
        _seed_multi_step_turn(db)
        turn = render_portable_history("thread-1", db=db).turns[0]
        assistant_seqs = {
            message.seq for message in turn.messages if message.role == "assistant"
        }

        for cut in turn_interior_cut_points(turn):
            assert cut.seq not in assistant_seqs


# --------------------------------------------------------------------------
# Replay shape
# --------------------------------------------------------------------------


class TestMidTurnTailReplays:
    def test_mid_turn_tail_opens_on_a_user_anchor(self) -> None:
        db = _db()
        _seed_multi_step_turn(db)
        tail = render_portable_history(
            "thread-1", db=db, start_exclusive=_boundary(db, 3),
        )

        native = to_native_history(tail)

        assert tail.turns[0].includes_turn_start is False
        assert native[0]["role"] == "user"
        assert "resumes mid-turn" in native[0]["content"]
        assert native[1]["role"] == "assistant"

    def test_turn_granular_tail_gets_no_anchor(self) -> None:
        """The bridge exists only for a mid-Turn cut; it must not leak."""
        db = _db()
        _seed_multi_step_turn(db)
        _turn_row(db, "turn-2")
        _message(db, "u2", 7, "user", "next", turn_id="turn-2")
        _message(db, "a4", 8, "assistant", "ok", turn_id="turn-2")
        db.commit()
        tail = render_portable_history(
            "thread-1", db=db, start_exclusive=_boundary(db, 6),
        )

        native = to_native_history(tail)

        assert tail.turns[0].includes_turn_start is True
        assert native[0]["content"] == "next"

    def test_fragments_keep_one_turn_identity(self) -> None:
        """The Turn record is never split: both fragments name the same Turn."""
        db = _db()
        _seed_multi_step_turn(db)
        cut = _boundary(db, 3)

        head = render_portable_history("thread-1", db=db, through=cut)
        tail = render_portable_history("thread-1", db=db, start_exclusive=cut)

        assert head.turns[0].turn_id == tail.turns[0].turn_id == "turn-1"
        assert (head.turns[0].includes_turn_start, head.turns[0].includes_turn_end) == (
            True,
            False,
        )
        assert (tail.turns[0].includes_turn_start, tail.turns[0].includes_turn_end) == (
            False,
            True,
        )

    def test_fold_units_commit_at_the_interior_boundary(self) -> None:
        """The fold can actually stop there: the last unit carries a boundary.

        ``checkpoint_generation`` may only commit a checkpoint at a fold unit
        whose ``boundary`` is set, so an interior cut is only useful if the
        head fragment's final unit carries one.
        """
        from history.folds import checkpoint_fold_units

        db = _db()
        _seed_multi_step_turn(db)
        cut = _boundary(db, 3)
        head = render_portable_history("thread-1", db=db, through=cut)

        units = checkpoint_fold_units(head)

        assert units[-1].completes_turn is True
        assert units[-1].boundary == cut
        assert units[-1].turn_id == "turn-1"

    def test_whole_turn_rendering_is_unchanged(self) -> None:
        db = _db()
        _seed_multi_step_turn(db)

        full = render_portable_history("thread-1", db=db)

        assert full.turns[0].includes_turn_start is True
        assert full.turns[0].includes_turn_end is True
        assert to_native_history(full)[0]["content"] == "investigate"
        assert_provider_acceptable(to_native_history(full))


@pytest.mark.parametrize(
    ("provider_ids", "expected_seq"),
    [
        (("cc-call", "cc-call"), 1),
        ((None, None), 2),
        (("codex-item-1", "codex-item-2"), 2),
    ],
    ids=("claude-message-group", "native-unchanged", "codex-items-unchanged"),
)
def test_latest_step_cut_starts_before_its_provider_message_group(
    provider_ids, expected_seq,
) -> None:
    """Only a Claude call split into several events moves the legal cut."""
    from history.boundaries import boundary_before_latest_assistant_step

    db = _db()
    _turn_row(db, "turn-1", status="running")
    _message(db, "u1", 1, "user", "go", turn_id="turn-1")
    _message(
        db, "a1", 2, "assistant", "first", turn_id="turn-1",
        metadata={"provider_message_id": provider_ids[0]},
    )
    _message(
        db, "a2", 3, "assistant", "second", turn_id="turn-1",
        metadata={"provider_message_id": provider_ids[1]},
    )
    db.commit()

    projection = render_portable_history("thread-1", db=db, include_unfinished_turn=True)

    assert boundary_before_latest_assistant_step(projection).seq == expected_seq
