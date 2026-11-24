"""Assistant-Step grouping and provider-neutral Turn projection."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from loguru import logger

from history.messages import (
    _missing_tool_result_stub,
    _portable_message,
    _quarantined_interruption_stub,
    _tool_calls,
)
from history.types import (
    AssistantStepBatch,
    HistoryBoundary,
    PortableHistoryError,
    PortableMessage,
    PortableTurn,
    _SourceTurn,
)
from infra.delivery_states import TOOL_RESULT_QUARANTINED_UNBOUND


def _tool_result_step_id(
    source: _SourceTurn,
    row: dict[str, Any],
    *,
    call_id: str,
    assistants: dict[str, dict[str, Any]],
    calls_by_step: dict[str, list[dict[str, Any]]],
) -> str:
    step_id = row["_meta"].get("assistant_step_id")
    if not isinstance(step_id, str) or not step_id:
        if source.turn_id is not None:
            raise PortableHistoryError(
                f"tool result {row['id']!r} has no assistant_step_id"
            )
        candidates = [
            candidate_id
            for candidate_id, calls in calls_by_step.items()
            if assistants[candidate_id]["seq"] < row["seq"]
            and sum(call.get("id") == call_id for call in calls) == 1
        ]
        if len(candidates) != 1:
            raise PortableHistoryError(
                f"legacy tool result {row['id']!r} has ambiguous Assistant Step"
            )
        step_id = candidates[0]
    parent = assistants.get(step_id)
    if parent is None:
        raise PortableHistoryError(
            f"tool result {row['id']!r} names missing Assistant Step {step_id!r}"
        )
    if int(parent["seq"]) >= int(row["seq"]):
        raise PortableHistoryError(
            f"tool result {row['id']!r} precedes Assistant Step {step_id!r}"
        )
    declared = [call for call in calls_by_step[step_id] if call.get("id") == call_id]
    if len(declared) != 1:
        raise PortableHistoryError(
            f"Assistant Step {step_id!r} does not uniquely declare {call_id!r}"
        )
    return step_id


def _prefer_tool_result(
    previous: dict[str, Any] | None,
    candidate: dict[str, Any],
) -> bool:
    if previous is None:
        return True
    previous_synthetic = previous["_meta"].get("synthetic_tool_result") is True
    candidate_synthetic = candidate["_meta"].get("synthetic_tool_result") is True
    return (previous_synthetic and not candidate_synthetic) or (
        previous_synthetic == candidate_synthetic and candidate["seq"] > previous["seq"]
    )


def _collect_tool_results(
    source: _SourceTurn,
    *,
    assistants: dict[str, dict[str, Any]],
    calls_by_step: dict[str, list[dict[str, Any]]],
) -> tuple[
    dict[tuple[str, str], dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    results: dict[tuple[str, str], dict[str, Any]] = {}
    quarantined_by_step: dict[str, dict[str, Any]] = {}
    for row in source.rows:
        if row["role"] != "tool":
            continue
        meta = row["_meta"]
        if meta.get("tool_result_binding_state") == TOOL_RESULT_QUARANTINED_UNBOUND:
            step_id = meta.get("assistant_step_id")
            if isinstance(step_id, str) and step_id:
                quarantined_by_step.setdefault(step_id, row)
            continue
        call_id = meta.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            raise PortableHistoryError(
                f"tool result {row['id']!r} has invalid tool_call_id"
            )
        step_id = _tool_result_step_id(
            source,
            row,
            call_id=call_id,
            assistants=assistants,
            calls_by_step=calls_by_step,
        )
        key = (step_id, call_id)
        if _prefer_tool_result(results.get(key), row):
            results[key] = row
    return results, quarantined_by_step


def _complete_tool_results(
    *,
    assistants: dict[str, dict[str, Any]],
    calls_by_step: dict[str, list[dict[str, Any]]],
    results: dict[tuple[str, str], dict[str, Any]],
    quarantined_by_step: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    lost_calls: list[tuple[str, dict[str, Any]]] = []
    for step_id, calls in calls_by_step.items():
        for call in calls:
            key = (step_id, str(call["id"]))
            if key in results:
                continue
            quarantined = quarantined_by_step.get(step_id)
            if quarantined is not None:
                results[key] = _quarantined_interruption_stub(
                    quarantined,
                    call_id=str(call["id"]),
                )
            else:
                lost_calls.append((step_id, call))
    by_step: dict[str, list[dict[str, Any]]] = {}
    for (step_id, _call_id), result in results.items():
        by_step.setdefault(step_id, []).append(result)
    for step_results in by_step.values():
        step_results.sort(key=lambda item: int(item["seq"]))
    for step_id, call in lost_calls:
        by_step.setdefault(step_id, []).append(
            _missing_tool_result_stub(assistants[step_id], call=call)
        )
        logger.warning(
            "portable history: Assistant Step {} has no durable result for "
            "declared call {}; projecting the canonical missing-result stub",
            step_id,
            call["id"],
        )
    return by_step


def _emit_turn_messages(
    source: _SourceTurn,
    *,
    calls_by_step: dict[str, list[dict[str, Any]]],
    results_by_step: dict[str, list[dict[str, Any]]],
    db=None,
) -> tuple[PortableMessage, ...]:
    emitted: list[PortableMessage] = []
    for row in source.rows:
        if row["role"] == "tool":
            continue
        if row["role"] != "assistant":
            emitted.append(_portable_message(row, source.turn_id, db=db))
            continue
        step_id = str(row["id"])
        emitted.append(
            _portable_message(
                row,
                source.turn_id,
                tool_calls=calls_by_step[step_id],
                db=db,
            )
        )
        emitted.extend(
            _portable_message(result, source.turn_id, db=db)
            for result in results_by_step.get(step_id, ())
        )
    return tuple(emitted)


def _project_turn(
    source: _SourceTurn,
    *,
    starts_turn: bool = True,
    db=None,
) -> tuple[PortableMessage, ...]:
    """Project one Turn or one Assistant-Step-aligned Turn fragment."""
    if (
        starts_turn
        and source.turn_id is not None
        and (not source.rows or source.rows[0]["role"] != "user")
    ):
        raise PortableHistoryError(
            f"Turn {source.turn_id!r} does not begin with user input"
        )
    assistants = {
        str(row["id"]): row for row in source.rows if row["role"] == "assistant"
    }
    calls_by_step = {step_id: _tool_calls(row) for step_id, row in assistants.items()}
    results, quarantined = _collect_tool_results(
        source,
        assistants=assistants,
        calls_by_step=calls_by_step,
    )
    results_by_step = _complete_tool_results(
        assistants=assistants,
        calls_by_step=calls_by_step,
        results=results,
        quarantined_by_step=quarantined,
    )
    return _emit_turn_messages(
        source,
        calls_by_step=calls_by_step,
        results_by_step=results_by_step,
        db=db,
    )


def group_assistant_steps(
    messages: Sequence[PortableMessage],
) -> tuple[tuple[PortableMessage, ...], ...]:
    """Group one projected Turn's messages into Assistant Step units.

    One group is ``[pending user inputs] + one Assistant Step + every tool
    result that Step declared``. This is the sub-Turn atom: it is the same
    pairing rule :func:`_project_turn` enforces, so a boundary between two
    groups can never separate a ``tool_use`` from its ``tool_result``, and it
    never splits a row, so it can never separate reasoning from its signature.

    Defined once here and shared by :func:`checkpoint_fold_units` and
    :func:`turn_interior_cut_points`; duplicating the rule is how a cut and a
    fold drift apart.
    """
    grouped: list[tuple[PortableMessage, ...]] = []
    pending_inputs: list[PortableMessage] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role == "user":
            pending_inputs.append(message)
            index += 1
            continue
        if message.role != "assistant":
            raise PortableHistoryError(
                "canonical fold unit begins with an ownerless tool result"
            )

        step = [*pending_inputs, message]
        pending_inputs.clear()
        index += 1
        while index < len(messages) and messages[index].role == "tool":
            step.append(messages[index])
            index += 1
        grouped.append(tuple(step))

    if pending_inputs:
        grouped.append(tuple(pending_inputs))
    return tuple(grouped)


def _row_boundary(turn: PortableTurn, message: PortableMessage) -> HistoryBoundary:
    """The Turn's boundary at one of its own rows."""
    return HistoryBoundary(
        thread_id=turn.boundary.thread_id,
        history_epoch=turn.boundary.history_epoch,
        seq=message.seq,
        message_id=message.message_id,
    )


def _group_partition_facts(
    groups: Sequence[Sequence[PortableMessage]],
) -> list[tuple[int | None, bool, bool]]:
    """Per group: its assistant row index, and whether its anchor and end rows may become boundaries.

    Both tests are about rows in *other* groups, so both come from aggregates over the
    group sequence -- a suffix minimum and a prefix maximum of ``seq`` -- the way
    :func:`turn_interior_cut_points` already does them. Reading the rows of every later
    group for every group cost ``O(rows²)`` per call, and this runs several times at every
    tool boundary of a Turn, so a long Turn re-read tens of thousands of rows per step.
    """
    facts: list[tuple[int | None, bool, bool]] = []
    group_min_seqs = [min(message.seq for message in group) for group in groups]
    # suffix_min[index] is the lowest seq anywhere in groups[index:].
    suffix_min = list(group_min_seqs)
    for index in range(len(suffix_min) - 2, -1, -1):
        suffix_min[index] = min(suffix_min[index], suffix_min[index + 1])
    # head_max is the highest seq anywhere in groups[:index]; None when there is none.
    head_max: int | None = None
    for index, group in enumerate(groups):
        assistant_index = next(
            (position for position, message in enumerate(group) if message.role == "assistant"),
            None,
        )
        group_max = max(message.seq for message in group)
        # The Turn's last group ends at the Turn itself, so nothing later can interleave.
        end_ok = index == len(groups) - 1 or group_max < suffix_min[index + 1]
        # An anchor exists only when it is the highest covered seq and every exact row
        # (this group's assistant/tool rows and any later group) starts after it.
        anchor_ok = assistant_index is not None and assistant_index > 0
        if anchor_ok:
            anchor_seq = max(message.seq for message in group[:assistant_index])
            exact_min = min(message.seq for message in group[assistant_index:])
            if index + 1 < len(groups):
                exact_min = min(exact_min, suffix_min[index + 1])
            if head_max is not None and head_max > anchor_seq:
                anchor_ok = False
            elif anchor_seq >= exact_min:
                anchor_ok = False
        facts.append((assistant_index, anchor_ok, end_ok))
        head_max = group_max if head_max is None else max(head_max, group_max)
    return facts


def assistant_step_batches(turn: PortableTurn) -> tuple[AssistantStepBatch, ...]:
    """Project complete Assistant Step batches and their legal resume anchors.

    A resume anchor is the final initiating user row immediately before the
    Assistant output. It exists only when durable sequence order cleanly
    partitions checkpoint-covered input from exact assistant/tool replay.
    """
    groups = group_assistant_steps(turn.messages)
    facts = _group_partition_facts(groups)
    batches: list[AssistantStepBatch] = []
    for group_index, group in enumerate(groups):
        assistant_index, anchor_ok, end_ok = facts[group_index]
        if assistant_index is None:
            continue
        initiating_inputs = tuple(group[:assistant_index])
        assistant_and_tools = tuple(group[assistant_index:])
        resume_anchor = None
        if anchor_ok:
            anchor = max(initiating_inputs, key=lambda message: message.seq)
            resume_anchor = _row_boundary(turn, anchor)
        end_boundary = None
        if group_index == len(groups) - 1:
            end_boundary = turn.boundary
        elif end_ok:
            group_end = max(group, key=lambda message: message.seq)
            end_boundary = _row_boundary(turn, group_end)
        batches.append(
            AssistantStepBatch(
                initiating_inputs=initiating_inputs,
                assistant_and_tools=assistant_and_tools,
                resume_anchor=resume_anchor,
                end_boundary=end_boundary,
            )
        )
    return tuple(batches)


def turn_interior_cut_points(turn: PortableTurn) -> tuple[HistoryBoundary, ...]:
    """Return every legal checkpoint boundary strictly inside one Turn.

    A cut is legal only at the end of a complete Assistant Step group that is
    not the Turn's last group — see :func:`group_assistant_steps` for why that
    keeps tool pairs and reasoning/signature pairs intact.

    A boundary is denominated in ``seq`` and the renderer selects by ``seq``,
    but :func:`_project_turn` emits in *group* order, which a late-written
    synthetic tool result can push out of ``seq`` order. So a candidate is
    admitted only when it cleanly partitions the Turn: every message at or
    before the cut has a lower ``seq`` than every message after it. A candidate
    that fails that test is skipped, never repaired — guessing where a
    non-monotonic Turn splits is exactly the kind of lie this module refuses.

    The Turn's own final boundary is deliberately excluded; it is already a
    complete Turn boundary.
    """
    groups = group_assistant_steps(turn.messages)
    if len(groups) < 2:
        return ()
    group_min_seqs = [min(message.seq for message in group) for group in groups]
    # suffix_min[index] is the lowest seq anywhere in groups[index:].
    suffix_min = list(group_min_seqs)
    for index in range(len(suffix_min) - 2, -1, -1):
        suffix_min[index] = min(suffix_min[index], suffix_min[index + 1])

    cuts: list[HistoryBoundary] = []
    head_max: PortableMessage | None = None
    for index in range(len(groups) - 1):
        for message in groups[index]:
            if head_max is None or message.seq > head_max.seq:
                head_max = message
        if head_max is None or head_max.seq >= suffix_min[index + 1]:
            continue
        cuts.append(_row_boundary(turn, head_max))
    return tuple(cuts)
