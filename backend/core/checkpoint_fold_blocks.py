"""The message blocks a cold checkpoint fold replays, including its omission envelope."""

from __future__ import annotations

from collections.abc import Sequence

from history.adapters import (
    checkpoint_retention_note,
    checkpoint_source_block,
    mid_turn_resume_text,
)
from history.types import (
    CheckpointFoldUnit,
    PortableHistoryProjection,
    PortableMessage,
)

COLD_FOLD_OLD_TOOL_BUDGET_TOKENS = 1_024
COLD_FOLD_TOOL_REFERENCE_BUDGET_TOKENS = 160


def unit_messages(units: Sequence[CheckpointFoldUnit]) -> tuple[PortableMessage, ...]:
    """Flatten fold units into their messages, in unit order."""
    return tuple(message for unit in units for message in unit.messages)


def omission_block() -> dict[str, str]:
    """State that earlier source was dropped as whole leading Steps."""
    return {
        "role": "user",
        "content": (
            "[CORSIA omitted checkpoint source]\n"
            "Earlier steps were omitted whole to fit this one cold checkpoint "
            "call. A following resume envelope may repeat the open turn's "
            "initiating input and steers sent during omitted steps."
        ),
    }


def resume_anchor_block(presence_line: str | None = None) -> dict[str, str]:
    """The resume anchor as one user block, with the frozen presence line."""
    return {"role": "user", "content": mid_turn_resume_text(presence_line)}


def mid_turn_resume_blocks(
    omitted: Sequence[CheckpointFoldUnit],
    included: Sequence[CheckpointFoldUnit],
    *,
    thread_id: str,
    presence_line: str | None = None,
) -> list[dict[str, str]]:
    """Repeat the user input of a turn the omission cut into.

    That is the turn's initiating input plus any steer that joined an omitted
    Step. Units partition the source, so each repeated row appears once. These
    blocks are untruncated, so a cut that lands inside a turn can be larger
    than keeping that turn whole. Callers that search a range of cut points must
    not assume candidate size falls monotonically across this boundary.
    """
    if not omitted or not included or omitted[-1].turn_id != included[0].turn_id:
        return []
    turn_id = included[0].turn_id
    repeated = []
    seen: set[str] = set()
    for message in unit_messages(omitted):
        if message.turn_id == turn_id and message.role == "user":
            if message.message_id not in seen:
                repeated.append(message)
                seen.add(message.message_id)
    blocks = [resume_anchor_block(presence_line)]
    blocks.extend(
        checkpoint_source_block(message, thread_id=thread_id)
        for message in repeated
    )
    return blocks


def source_blocks(
    source: PortableHistoryProjection,
    units: Sequence[CheckpointFoldUnit],
    *,
    project_tools: bool,
    newest_ordinal: int,
    newest_tool_budget_tokens: int,
    old_tool_budget_tokens: int = COLD_FOLD_OLD_TOOL_BUDGET_TOKENS,
    stub_old_tool_results: bool = False,
    reference_tool_arguments: bool = False,
) -> list[dict[str, str]]:
    """Render units with per-result budgets and optional old-result stubs."""
    blocks: list[dict[str, str]] = []
    included_seqs: set[int] = set()
    latest_tool_id = next(
        (message.message_id for unit in reversed(units)
         for message in reversed(unit.messages) if message.role == "tool"),
        None,
    )
    for unit in units:
        budget = (
            newest_tool_budget_tokens
            if unit.ordinal == newest_ordinal
            else old_tool_budget_tokens
        )
        argument_budget = (
            COLD_FOLD_OLD_TOOL_BUDGET_TOKENS
            if reference_tool_arguments else budget
        )
        for message in unit.messages:
            included_seqs.add(message.seq)
            blocks.append(
                checkpoint_source_block(
                    message,
                    thread_id=source.thread_id,
                    tool_result_budget_tokens=budget if project_tools else None,
                    tool_argument_budget_tokens=argument_budget if project_tools else None,
                    stub_tool_result_body=(
                        stub_old_tool_results and message.role == "tool"
                        and message.message_id != latest_tool_id
                    ),
                )
            )
    # Units are cut in delivery order, not seq order, so an omitted row's seq
    # can fall between included ones; count retained rows by membership.
    retained = len(included_seqs.intersection(source.retention_stub_seqs))
    if retained:
        blocks.append(checkpoint_retention_note(retained))
    return blocks
