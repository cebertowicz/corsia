"""A cold fold drops whole Steps in delivery order, never judged by write seq.

An input that arrives while a Step's tools run is written before that Step's
later results but delivered with the next Step. Judging a cut by seq made every
cut inside such a Turn look non-contiguous, so no Step could ever be dropped.
"""

from __future__ import annotations

import itertools
import json
import re
import sqlite3

import pytest

from infra.schema import ARTIFACT_IDENTITY_SCHEMA_SQL

THREAD = "thread-1"
_MSG_ID = re.compile(r"\[msg ([^\]]+)\]")


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
            seq INTEGER,
            active_turn_id TEXT,
            UNIQUE(thread_id, seq)
        );
        CREATE TABLE active_turns (
            turn_order INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            thread_id TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT
        );
        """
        + ARTIFACT_IDENTITY_SCHEMA_SQL
    )
    db.execute("INSERT INTO threads (id, history_epoch) VALUES (?, 1)", (THREAD,))
    return db


class _Writer:
    """Append durable rows in write order, as the turn runtime does."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.seq = itertools.count(1)

    def turn(self, turn_id: str) -> None:
        self.db.execute(
            "INSERT INTO active_turns (id, thread_id, status, started_at, ended_at) "
            "VALUES (?, ?, 'completed', 'start', 'end')",
            (turn_id, THREAD),
        )

    def row(self, message_id, role, content, *, turn_id, metadata=None) -> None:
        meta = dict(metadata or {})
        if turn_id is not None:
            meta["turn_id"] = turn_id
        seq = next(self.seq)
        self.db.execute(
            "INSERT INTO messages (id, thread_id, role, content, metadata, created_at,"
            " seq, active_turn_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (message_id, THREAD, role, content, json.dumps(meta) if meta else None,
             f"t-{seq:06d}", seq, turn_id),
        )


def _steered_turn(
    writer: _Writer,
    turn_id: str,
    *,
    steps: int,
    calls_per_step: int,
    steers_per_step: int,
    argument_chars: int,
    result_chars: int,
    steer_chars: int,
) -> None:
    """One long Turn whose unbound inputs land among each Step's tool results."""
    writer.turn(turn_id)
    writer.row(f"{turn_id}-ask", "user", f"ORIGINAL ASK {turn_id}", turn_id=turn_id)
    for step in range(steps):
        step_id = f"{turn_id}-a{step}"
        calls = [{
            "id": f"{step_id}-c{call}", "type": "function",
            "function": {"name": "Read", "arguments": json.dumps({"path": "p" * argument_chars})},
        } for call in range(calls_per_step)]
        writer.row(step_id, "assistant", f"STEP {step_id}", turn_id=turn_id,
                   metadata={"step_type": "tool_calls", "llm_tool_calls": calls})
        for index, call in enumerate(calls):
            if index and index <= steers_per_step:
                writer.row(f"{step_id}-steer{index}", "user",
                           f"STEER {step_id}-{index} " + "s" * steer_chars, turn_id=None)
            writer.row(f"{call['id']}-r", "tool", f"RESULT {call['id']} " + "r" * result_chars,
                       turn_id=turn_id, metadata={
                           "tool_call_id": call["id"], "assistant_step_id": step_id,
                           "name": "Read"})
    writer.row(f"{turn_id}-final", "assistant", f"FINAL {turn_id}", turn_id=turn_id)


def _plain_turn(writer: _Writer, turn_id: str) -> None:
    writer.turn(turn_id)
    writer.row(f"{turn_id}-ask", "user", f"ASK {turn_id} " + "q" * 400, turn_id=turn_id)
    writer.row(f"{turn_id}-answer", "assistant", f"ANSWER {turn_id}", turn_id=turn_id)


def _projection(db):
    from history.projection import render_portable_history

    return render_portable_history(THREAD, db=db)


def _rendered_ids(fitted) -> list[str]:
    return [
        match for message in fitted.messages
        for match in _MSG_ID.findall(message["content"])
    ]


def test_steered_long_turn_shape_is_seq_interleaved_across_steps() -> None:
    """Pin the shape: each steer is delivered with the next Step, below its seqs."""
    from history.folds import checkpoint_fold_units

    db = _db()
    _steered_turn(_Writer(db), "turn-1", steps=4, calls_per_step=4, steers_per_step=2,
                  argument_chars=10, result_chars=10, steer_chars=10)
    units = checkpoint_fold_units(_projection(db))

    # Four tool Steps, then the final answer as its own Step.
    assert len(units) == 5
    for previous, unit in zip(units[:3], units[1:4]):
        step_id = next(m.message_id for m in previous.messages if m.role == "assistant")
        steers = [message for message in unit.messages if message.role == "user"]
        assert [m.message_id for m in steers] == [f"{step_id}-steer1", f"{step_id}-steer2"]
        assert unit.messages[: len(steers)] == tuple(steers)
        assert max(message.seq for message in previous.messages) > min(
            message.seq for message in steers
        )


def test_seq_interleaved_long_turn_drops_whole_oldest_steps_to_fit() -> None:
    """The wedged shape: one Turn far over budget whose every cut is seq-interleaved."""
    from core.checkpoint_fit import render_cold_fold_input
    from history.folds import checkpoint_fold_units

    db = _db()
    # Sized like a real steered Turn: ~3.5k-byte inputs, and only the last Steps fit.
    _steered_turn(_Writer(db), "turn-1", steps=21, calls_per_step=22, steers_per_step=2,
                  argument_chars=2_000, result_chars=2_400, steer_chars=3_500)
    source = _projection(db)
    units = checkpoint_fold_units(source)
    budget = 229_096
    exact = render_cold_fold_input(source, max_input_tokens=10_000_000)
    assert len(units) == 22 and len(source.messages) > 500
    assert exact.input_tokens > 3 * budget

    fitted = render_cold_fold_input(source, max_input_tokens=budget)
    ids = _rendered_ids(fitted)

    assert fitted.input_tokens <= budget
    assert 0 < fitted.omitted_units < len(units)
    assert len(ids) == len(set(ids))
    kept = units[fitted.omitted_units:]
    assert fitted.first_included_seq == min(
        message.seq for unit in kept for message in unit.messages
    )
    assert "turn-1-final" in ids and "turn-1-ask" in ids
    omitted_steers = [
        message.message_id for unit in units[:fitted.omitted_units]
        for message in unit.messages if message.role == "user"
    ]
    assert omitted_steers and set(omitted_steers) <= set(ids)
    assert "turn-1-a0" not in ids and "turn-1-a0-c0-r" not in ids


def _exactly_once_source():
    db = _db()
    writer = _Writer(db)
    _plain_turn(writer, "turn-1")
    _steered_turn(writer, "turn-2", steps=4, calls_per_step=3, steers_per_step=2,
                  argument_chars=40, result_chars=40, steer_chars=40)
    _plain_turn(writer, "turn-3")
    return _projection(db)


@pytest.mark.parametrize("stub_old_tool_results", (False, True))
def test_every_step_cut_renders_each_source_message_once_or_omits_it(
    stub_old_tool_results,
) -> None:
    from core import checkpoint_fit
    from core.checkpoint_fold_blocks import COLD_FOLD_OLD_TOOL_BUDGET_TOKENS
    from history.folds import checkpoint_fold_units

    source = _exactly_once_source()
    units = checkpoint_fold_units(source)
    every_id = [message.message_id for message in source.messages]
    assert len(every_id) == len(set(every_id))
    assert sorted(m.message_id for u in units for m in u.messages) == sorted(every_id)
    kinds = set()
    for skipped in range(len(units)):
        fitted = checkpoint_fit._candidate(
            source, units, skipped=skipped, project_tools=True,
            newest_tool_budget_tokens=COLD_FOLD_OLD_TOOL_BUDGET_TOKENS,
            stub_old_tool_results=stub_old_tool_results,
            protected_blocks=(), count_tokens=checkpoint_fit._estimate_messages_tokens,
        )
        assert fitted is not None and fitted.omitted_units == skipped
        omitted, included = units[:skipped], units[skipped:]
        cut_turn = included[0].turn_id
        inside = bool(omitted) and omitted[-1].turn_id == cut_turn
        kinds.add("inside" if inside else ("turn start" if omitted else "none"))
        repeated = [
            message.message_id for unit in omitted for message in unit.messages
            if inside and message.turn_id == cut_turn and message.role == "user"
        ]
        shown = [message.message_id for unit in included for message in unit.messages]
        ids = _rendered_ids(fitted)

        assert sorted(ids) == sorted(repeated + shown)
        assert len(ids) == len(set(ids))
        covered = set(every_id) - set(ids)
        assert covered == {
            message.message_id for unit in omitted for message in unit.messages
        } - set(repeated)
        assert fitted.first_included_seq == min(
            message.seq for unit in included for message in unit.messages
        )
        assert fitted.first_included_seq <= source.sealed_boundary.seq
    assert kinds == {"none", "turn start", "inside"}


def test_identical_input_fits_to_the_identical_cut_identity() -> None:
    from core.checkpoint_fit import render_cold_fold_input

    first = render_cold_fold_input(_exactly_once_source(), max_input_tokens=1_200)
    second = render_cold_fold_input(_exactly_once_source(), max_input_tokens=1_200)

    assert first.omitted_units > 0
    assert (first.first_included_seq, first.omitted_units, first.messages) == (
        second.first_included_seq, second.omitted_units, second.messages,
    )


def test_retention_note_counts_only_retained_rows_the_writer_is_shown() -> None:
    """A retained result in an omitted Step can sit inside the shown seq range."""
    from dataclasses import replace

    from core.checkpoint_fold_blocks import source_blocks
    from core.thread_lifecycle import TOOL_TRIM_MARKER
    from history.folds import checkpoint_fold_units

    source = _exactly_once_source()
    units = checkpoint_fold_units(source)
    steered = [unit for unit in units if unit.turn_id == "turn-2"]
    omitted_result = max(
        (m for m in steered[1].messages if m.role == "tool"), key=lambda m: m.seq,
    )
    shown_steer = next(m for m in steered[2].messages if m.role == "user")
    assert omitted_result.seq > shown_steer.seq
    shown_result = next(m for m in steered[2].messages if m.role == "tool")

    def note(included, stub_seqs):
        retained = replace(source, retention_stub_seqs=tuple(sorted(stub_seqs)))
        blocks = source_blocks(
            retained, included, project_tools=True,
            newest_ordinal=units[-1].ordinal, newest_tool_budget_tokens=4_096,
        )
        return [b["content"] for b in blocks if b["content"].startswith("[note]")]

    included = units[units.index(steered[2]):]
    assert note(included, {omitted_result.seq}) == []
    trimmed = [
        replace(unit, messages=tuple(
            replace(m, content=TOOL_TRIM_MARKER + " by retention]")
            if m.message_id == shown_result.message_id else m
            for m in unit.messages
        ))
        for unit in included
    ]
    assert note(trimmed, {omitted_result.seq, shown_result.seq}) == [
        "[note] 1 message(s) in this range were destructively retained; "
        "their content is unavailable."
    ]
