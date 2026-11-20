"""Accept and render checkpoint output without judging its shape."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.checkpoint_snapshot_render import (
    render_artifact_snapshot,
    render_checkpoint_dates,
    render_plan_status_snapshot,
)
from core.checkpoint_types import CheckpointRecord
from core.fork_types import FORK_CONTEXT_TRUNCATED, is_partial_provider_error
from core.continuity_render import CONTINUITY_ENTRIES_HEADER, render_continuity_entries


@dataclass(frozen=True, slots=True)
class CapturedCheckpoint:
    """The only provider output facts that checkpoint acceptance owns."""

    raw_body: str
    has_staged_edits: bool
    usage: dict[str, Any] | None

    def usable_result(self) -> bool:
        """Whether this call's narrative has text to install as covered history."""
        return usable_result(self.raw_body)


def usable_result(raw_body: str) -> bool:
    """Whether a narrative is nonblank enough to become the install source."""
    return bool(raw_body.strip())


INCOMPLETE_UPDATE = "[Incomplete checkpoint update]\n"
PARTIAL_UPDATE_END = (
    "[End of incomplete update. It stopped before it was finished: work done after "
    "this point is not recorded here. Before saying something is complete, approved "
    "or sent, check with the person or the work itself.]"
)
PROVIDER_ERROR_WARNING = (
    "[CORSIA warning: The latest checkpoint update stopped on a provider error. The "
    "narrative below is the last complete checkpoint; any text after "
    "\"[Incomplete checkpoint update]\" is partial and may leave out later work. Do "
    "not treat omitted work as completed.]\n"
)
PRIOR_PROVIDER_ERROR_WARNING = (
    "[CORSIA warning: Prior checkpoint update stopped on a provider error; text after "
    "\"[Incomplete checkpoint update]\" is partial.]\n"
)


def carry_partial_narrative(previous: str, partial: str) -> str:
    """Keep prior narrative when a cut-off update cannot replace it completely."""
    if not previous:
        return partial
    suffix = "\n\n" + INCOMPLETE_UPDATE + partial if partial.strip() else ""
    return previous + suffix


def complete_narrative(previous: CheckpointRecord | None) -> str:
    """The previous narrative without a partial it carried from a provider error.

    A carried partial is replaced by the next one, never stacked under it.
    """
    if previous is None:
        return ""
    body = previous.raw_result or ""
    if is_partial_provider_error(previous.error) and (start := body.rfind(INCOMPLETE_UPDATE)) >= 0:
        return body[:start].rstrip("\n")
    return body


def complete_lines(partial: str) -> str:
    """Cut a partial back to its last complete line: a cut word reads as another word."""
    return partial[:partial.rfind("\n") + 1].strip("\n")


def carry_provider_error_partial(previous: str, partial: str) -> str:
    """Append assistant output cut short by a provider error after the last complete narrative."""
    head = previous + "\n\n" if previous else ""
    return head + INCOMPLETE_UPDATE + partial + "\n" + PARTIAL_UPDATE_END


def render_installed_checkpoint(checkpoint: CheckpointRecord) -> str:
    """Render the exact body beside the exact installed continuity snapshot."""
    raw_body = checkpoint.raw_result or ""
    snapshot = checkpoint.continuity_snapshot_json or "[]"
    warning = (
        "[CORSIA warning: Checkpoint truncated at the context limit. The update "
        "is incomplete; completed continuity edits were retained. Do not treat "
        "omitted work as completed.]\n"
        if checkpoint.error == FORK_CONTEXT_TRUNCATED
        else PROVIDER_ERROR_WARNING if is_partial_provider_error(checkpoint.error) else ""
    )
    rendered = (
        warning
        + render_checkpoint_dates(checkpoint.through_at, checkpoint.captured_at)
        + "[CORSIA checkpoint narrative]\n"
        + raw_body
        + "\n" + CONTINUITY_ENTRIES_HEADER
        + render_continuity_entries(snapshot)
    )
    for section in (
        render_artifact_snapshot(checkpoint.artifact_snapshot_json),
        render_plan_status_snapshot(checkpoint.plan_status_snapshot_json),
    ):
        if section:
            rendered += "\n" + section
    return rendered


def render_prior_checkpoint(checkpoint: CheckpointRecord) -> str:
    """Carry prior prose and truncation state without copying continuity bodies."""
    warning = (
        "[CORSIA warning: Prior checkpoint was truncated at the context limit.]\n"
        if checkpoint.error == FORK_CONTEXT_TRUNCATED
        else PRIOR_PROVIDER_ERROR_WARNING if is_partial_provider_error(checkpoint.error) else ""
    )
    return warning + "[CORSIA prior checkpoint narrative]\n" + (checkpoint.raw_result or "")
