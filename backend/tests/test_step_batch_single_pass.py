"""One pass over a Turn is enough to project its Assistant Step batches.

A differential test of :func:`history.turns.assistant_step_batches` against the
rule as stated, computed the slow obvious way, over shapes whose durable
``seq`` order deliberately disagrees with group order (a late-written tool
result, a trailing initiating user row).
"""

from __future__ import annotations

import random

import pytest

from history.turns import (
    assistant_step_batches,
    group_assistant_steps,
)
from history.types import (
    AssistantStepBatch,
    HistoryBoundary,
    PortableMessage,
    PortableTurn,
)

THREAD = "step-batch-thread"
TURN = "step-batch-turn"
EPOCH = 0


def _reference_batches(turn: PortableTurn) -> tuple[AssistantStepBatch, ...]:
    """The documented rule, computed the slow obvious way.

    Deliberately a transcription rather than a reuse: it re-reads every later
    row for every group, which is exactly the shape the shipped function must
    not need. It is the oracle for what a batch *is*, not for how fast one is.
    """
    groups = group_assistant_steps(turn.messages)
    batches: list[AssistantStepBatch] = []
    prior: list[PortableMessage] = []
    for index, group in enumerate(groups):
        assistant_index = next(
            (
                position
                for position, message in enumerate(group)
                if message.role == "assistant"
            ),
            None,
        )
        if assistant_index is None:
            prior.extend(group)
            continue
        initiating = tuple(group[:assistant_index])
        exact = tuple(group[assistant_index:])
        group_end = max(group, key=lambda message: message.seq)
        later = [
            message for rest in groups[index + 1 :] for message in rest
        ]
        end_is_legal = (
            index == len(groups) - 1
            or group_end.seq < min(message.seq for message in later)
        )
        anchor = None
        if initiating:
            anchor_message = max(initiating, key=lambda message: message.seq)
            covered = [*prior, *initiating]
            exact_and_later = [*exact, *later]
            if (
                anchor_message.seq == max(message.seq for message in covered)
                and anchor_message.seq
                < min(message.seq for message in exact_and_later)
            ):
                anchor = HistoryBoundary(
                    turn.boundary.thread_id,
                    turn.boundary.history_epoch,
                    anchor_message.seq,
                    anchor_message.message_id,
                )
        batches.append(
            AssistantStepBatch(
                initiating_inputs=initiating,
                assistant_and_tools=exact,
                resume_anchor=anchor,
                end_boundary=(
                    turn.boundary
                    if index == len(groups) - 1
                    else (
                        HistoryBoundary(
                            turn.boundary.thread_id,
                            turn.boundary.history_epoch,
                            group_end.seq,
                            group_end.message_id,
                        )
                        if end_is_legal
                        else None
                    )
                ),
            )
        )
        prior.extend(group)
    return tuple(batches)


def _message(message_id: str, seq: int, role: str) -> PortableMessage:
    return PortableMessage(
        message_id=message_id,
        seq=seq,
        turn_id=TURN,
        role=role,
        content=message_id,
        created_at="2026-01-01T00:00:00+00:00",
        tool_calls=(
            (
                {
                    "id": f"call-{message_id}",
                    "type": "function",
                    "function": {"name": "Read", "arguments": "{}"},
                },
            )
            if role == "assistant"
            else ()
        ),
        tool_call_id=None if role != "tool" else f"call-{message_id}",
    )


def _shuffled_turn(rng: random.Random) -> PortableTurn:
    """One Turn whose durable seq order is shuffled away from group order.

    Group order is fixed by the projection: a Step's tool rows directly follow
    it. Only ``seq`` moves, which is what a late-written tool result or a queued
    inbound row does to a Turn in production.
    """
    plan: list[tuple[str, str]] = []
    for _ in range(rng.randint(1, 6)):
        for _ in range(rng.randint(0, 2)):
            plan.append(("input", "user"))
        plan.append(("step", "assistant"))
        for _ in range(rng.randint(0, 3)):
            plan.append(("step", "tool"))
    if rng.random() < 0.25:
        plan.append(("input", "user"))

    seqs = list(range(100, 100 + len(plan)))
    # The first row and each Step row keep their slot so the Turn still groups
    # into Steps; everything else is free to land out of order.
    movable = [
        index
        for index, (kind, role) in enumerate(plan)
        if index and not (kind == "step" and role == "assistant")
    ]
    movable_seqs = [seqs[index] for index in movable]
    rng.shuffle(movable_seqs)
    for index, seq in zip(movable, movable_seqs):
        seqs[index] = seq

    message_ids = [f"m{index}" for index in range(len(plan))]
    messages = tuple(
        _message(message_ids[index], seqs[index], plan[index][1])
        for index in range(len(plan))
    )
    last = message_ids[seqs.index(max(seqs))]
    return PortableTurn(
        turn_id=TURN,
        status="running",
        messages=messages,
        boundary=HistoryBoundary(THREAD, EPOCH, max(seqs), last),
    )


def test_shuffled_shapes_are_not_all_trivial() -> None:
    """The corpus actually contains the cases that make the rule bite."""
    anchors = illegal_ends = trailing_inputs = out_of_order = 0
    for seed in range(60):
        turn = _shuffled_turn(random.Random(seed))
        if [message.seq for message in turn.messages] != sorted(
            message.seq for message in turn.messages
        ):
            out_of_order += 1
        batches = assistant_step_batches(turn)
        anchors += sum(1 for batch in batches if batch.resume_anchor is not None)
        illegal_ends += sum(
            1 for batch in batches[:-1] if batch.end_boundary is None
        )
        trailing_inputs += sum(
            1
            for group in group_assistant_steps(turn.messages)
            if all(message.role == "user" for message in group)
        )
    assert out_of_order > 0
    assert anchors > 0
    assert illegal_ends > 0
    assert trailing_inputs > 0


@pytest.mark.parametrize("seed", range(200))
def test_batches_match_the_stated_rule_on_shuffled_shapes(seed: int) -> None:
    turn = _shuffled_turn(random.Random(seed))
    assert assistant_step_batches(turn) == _reference_batches(turn)
