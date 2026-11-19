"""One pass over a Turn is enough to project its Assistant Step batches.

Two guards on :func:`history.turns.assistant_step_batches`:

* a differential test against the rule as stated, computed the slow obvious
  way, over shapes whose durable ``seq`` order deliberately disagrees with
  group order (a late-written tool result, a trailing initiating user row);
* a pinned digest of everything the checkpoint legality proof reads at a tool
  boundary — the projection, each Turn's batches and interior cut points, the
  latest-step cut, the warm boundary and the foldable tail measure — over every
  legal cut of one long running Turn, so a change in grouping or legality shows
  up as a digest change rather than a silently different cut.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Sequence

import pytest

from history.boundaries import boundary_before_latest_assistant_step
from history.projection import render_portable_history
from history.turns import (
    assistant_step_batches,
    group_assistant_steps,
    turn_interior_cut_points,
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
HEAD_ROWS_PER_TURN = 5
STEPS = 60

# Captured from the pre-change per-group implementation over 76 cut renders of
# one inverted running Turn, and re-verified identical on the single-pass form.
PINNED_BOUNDARY_DIGEST = "fa0e0b57de2088adb928da92f175fe0dfa820f710870953be59eaee354b8768d"


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


def _insert_head_and_running_turn(db) -> None:
    """Three settled Turns, then one running Turn of ``STEPS`` Steps.

    Every fourth Step is preceded by an inbound row whose durable ``seq`` was
    swapped with a tool row of the previous Step, so the Turn is grouped into
    Steps but is not seq-ordered: the same shape a queued inbound input or a
    late-written tool result leaves behind.
    """
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at,history_epoch)"
        " VALUES (?, 'steve', 'native/test', 't', 't', ?)",
        (THREAD, EPOCH),
    )
    order = 0
    seq = 0

    def insert(role: str, turn_id: str, message_id: str, row_seq: int,
               **metadata) -> None:
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,"
            " delivery_state,seq,active_turn_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (message_id, THREAD, role, message_id,
             json.dumps({"turn_id": turn_id, **metadata}), "t", "delivered",
             row_seq, turn_id),
        )

    for head in range(1, 4):
        order += 1
        head_turn = f"{THREAD}-t{order}"
        db.execute(
            "INSERT INTO active_turns (turn_order,id,thread_id,execution_epoch,model,"
            "status,started_at,ended_at,boot_epoch) VALUES (?,?,?,?,?,?,?,?,?)",
            (order, head_turn, THREAD, EPOCH, "native/test", "completed", "t", "t",
             "boot"),
        )
        for step in range(1, 3):
            seq += 1
            insert("user", head_turn, f"{THREAD}-h{head}-{seq}", seq)
            step_id = f"{THREAD}-h{head}-s{step}"
            seq += 1
            insert(
                "assistant", head_turn, step_id, seq,
                llm_tool_calls=[{
                    "id": f"head-call-{head}-{step}", "type": "function",
                    "function": {"name": "Read", "arguments": "{}"},
                }],
                provider_message_id=f"head-pm-{head}-{step}",
            )
            seq += 1
            insert("tool", head_turn, f"{THREAD}-h{head}-r{seq}", seq,
                   assistant_step_id=step_id,
                   tool_call_id=f"head-call-{head}-{step}", name="Read")
        seq += 1
        insert("assistant", head_turn, f"{THREAD}-h{head}-end", seq,
               provider_message_id=f"head-end-{head}")

    db.execute(
        "INSERT INTO active_turns (turn_order,id,thread_id,execution_epoch,model,"
        "status,started_at,boot_epoch) VALUES (?,?,?,?,?,?,?,?)",
        (order + 1, TURN, THREAD, EPOCH, "native/test", "running", "t", "boot"),
    )

    # Group order first, then the durable seq each row actually carries. The
    # running Turn's seqs continue after the settled Turns'.
    plan: list[tuple[str, dict]] = [("user", {})]
    for step in range(1, STEPS + 1):
        if step % 4 == 0:
            plan.append(("user", {"inbound": step}))
        plan.append((
            "assistant",
            {
                "llm_tool_calls": [{
                    "id": f"call-{step}", "type": "function",
                    "function": {"name": "Read", "arguments": "{}"},
                }],
                "provider_message_id": f"pm-{step}",
                "step": step,
            },
        ))
        plan.append(("tool", {"step": step, "slot": 0}))
        plan.append(("tool", {"step": step, "slot": 1}))

    seqs = list(range(seq + 1, seq + 1 + len(plan)))
    for position, (_role, metadata) in enumerate(plan):
        if "inbound" not in metadata:
            continue
        # Swap with the tool row two Steps' worth of rows back: the inbound row
        # lands below a row that precedes it in group order.
        partner = position - 4
        seqs[position], seqs[partner] = seqs[partner], seqs[position]

    current_assistant_id: str | None = None
    for position, (role, metadata) in enumerate(plan):
        step = metadata.get("step", 0)
        fields = dict(metadata)
        message_id = f"{THREAD}-m{seqs[position]}"
        if role == "assistant":
            current_assistant_id = message_id
        elif role == "tool":
            fields["assistant_step_id"] = current_assistant_id
            fields["tool_call_id"] = f"call-{step}"
            fields["name"] = "Read"
            fields.pop("slot", None)
        fields.pop("step", None)
        insert(role, TURN, message_id, seqs[position], **fields)
    db.commit()


def _digest_payload(turn) -> str:
    parts = []
    for batch in assistant_step_batches(turn):
        parts.append(
            "["
            + "|".join(message.message_id for message in batch.initiating_inputs)
            + "]["
            + "|".join(message.message_id for message in batch.assistant_and_tools)
            + f"]|anchor={batch.resume_anchor!r}|end={batch.end_boundary!r}"
        )
    parts.append(
        "CUTS" + "|".join(repr(cut) for cut in turn_interior_cut_points(turn))
    )
    return "\n".join(parts)


def test_every_legal_cut_digests_the_same_legality_reads(isolated_db) -> None:
    from infra.database import get_db

    db = get_db()
    _insert_head_and_running_turn(db)

    rows = db.execute(
        "SELECT seq, id FROM messages WHERE thread_id = ? ORDER BY seq", (THREAD,)
    ).fetchall()
    ordered = [row["seq"] for row in rows]
    assert ordered == sorted(ordered), "rows are selected in durable order"

    ids = {int(row["seq"]): str(row["id"]) for row in rows}
    whole = render_portable_history(THREAD, db=db, include_unfinished_turn=True)
    running = [turn for turn in whole.turns if turn.turn_id == TURN]
    cuts = turn_interior_cut_points(running[0])
    assert 10 < len(cuts) < STEPS, "a cut per Step, minus the inverted ones"

    digest = hashlib.sha256()
    rendered = 0
    from core.fork_fit import foldable_tail_tokens
    from subagents.harness_checkpoint_context import _harness_boundaries

    for cut in [*cuts, whole.turns[0].boundary]:
        floor = HistoryBoundary(THREAD, EPOCH, cut.seq, ids[cut.seq])
        for include_unfinished in (False, True):
            projection = render_portable_history(
                THREAD, db=db, start_exclusive=floor,
                include_unfinished_turn=include_unfinished,
            )
            boundary, warm = _harness_boundaries(
                "codex", projection, floor,
                current_turn_id=TURN, step_precedes_start=True,
            )
            payload = "\n".join([
                repr(projection),
                *(_digest_payload(turn) for turn in projection.turns),
                "cut="
                + repr(boundary_before_latest_assistant_step(
                    projection, floor=floor, step_precedes_start=True,
                )),
                f"boundary={boundary!r}",
                f"warm={warm!r}",
                f"tail={foldable_tail_tokens(projection, boundary)!r}",
            ])
            digest.update(payload.encode())
            rendered += 1

    assert rendered == 2 * (len(cuts) + 1)
    assert digest.hexdigest() == PINNED_BOUNDARY_DIGEST, (
        "the legality reads at a tool boundary changed: computed "
        f"{digest.hexdigest()}; re-pin only after proving each cut is still legal"
    )
