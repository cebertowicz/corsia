"""One pass over a Turn is enough to project its Assistant Step batches.

A differential test of :func:`history.turns.assistant_step_batches` against the
pre-change implementation, vendored verbatim below, over shapes whose durable
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


# Verbatim copy of `assistant_step_batches` as it was before the single-pass change:
# `git show cdd5514364476442995b2b7e1ececd817c31c17f:backend/history/turns.py`
# (base of this change). Only the function name differs; it calls the unchanged
# `group_assistant_steps` imported above. It is the oracle for the byte-identity claim.
def _pre_3151_assistant_step_batches(
    turn: PortableTurn,
) -> tuple[AssistantStepBatch, ...]:
    """Project complete Assistant Step batches and their legal resume anchors.

    A resume anchor is the final initiating user row immediately before the
    Assistant output. It exists only when durable sequence order cleanly
    partitions checkpoint-covered input from exact assistant/tool replay.
    """
    groups = group_assistant_steps(turn.messages)
    batches: list[AssistantStepBatch] = []
    prior_messages: list[PortableMessage] = []
    for group_index, group in enumerate(groups):
        assistant_index = next(
            (
                index
                for index, message in enumerate(group)
                if message.role == "assistant"
            ),
            None,
        )
        if assistant_index is None:
            prior_messages.extend(group)
            continue
        initiating_inputs = tuple(group[:assistant_index])
        assistant_and_tools = tuple(group[assistant_index:])
        group_end = max(group, key=lambda message: message.seq)
        later_messages = tuple(
            message
            for later_group in groups[group_index + 1 :]
            for message in later_group
        )
        end_is_legal = group_index == len(groups) - 1 or group_end.seq < min(
            message.seq for message in later_messages
        )
        resume_anchor = None
        if initiating_inputs:
            anchor_message = max(
                initiating_inputs,
                key=lambda message: message.seq,
            )
            covered = (*prior_messages, *initiating_inputs)
            exact_and_later = (*assistant_and_tools, *later_messages)
            if anchor_message.seq == max(
                message.seq for message in covered
            ) and anchor_message.seq < min(message.seq for message in exact_and_later):
                resume_anchor = HistoryBoundary(
                    thread_id=turn.boundary.thread_id,
                    history_epoch=turn.boundary.history_epoch,
                    seq=anchor_message.seq,
                    message_id=anchor_message.message_id,
                )

        batches.append(
            AssistantStepBatch(
                initiating_inputs=initiating_inputs,
                assistant_and_tools=assistant_and_tools,
                resume_anchor=resume_anchor,
                end_boundary=(
                    turn.boundary
                    if group_index == len(groups) - 1
                    else (
                        HistoryBoundary(
                            thread_id=turn.boundary.thread_id,
                            history_epoch=turn.boundary.history_epoch,
                            seq=group_end.seq,
                            message_id=group_end.message_id,
                        )
                        if end_is_legal
                        else None
                    )
                ),
            )
        )
        prior_messages.extend(group)
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
        batches = _pre_3151_assistant_step_batches(turn)
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
def test_batches_match_the_pre_change_code_on_shuffled_shapes(seed: int) -> None:
    turn = _shuffled_turn(random.Random(seed))
    assert assistant_step_batches(turn) == _pre_3151_assistant_step_batches(turn)
