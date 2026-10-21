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
from core.fork_types import FORK_CONTEXT_TRUNCATED
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


def carry_partial_narrative(previous: str, partial: str) -> str:
    """Keep prior narrative when a cut-off update cannot replace it completely."""
    if not previous:
        return partial
    suffix = "\n\n[Incomplete checkpoint update]\n" + partial if partial.strip() else ""
    return previous + suffix


def render_installed_checkpoint(checkpoint: CheckpointRecord) -> str:
    """Render the exact body beside the exact installed continuity snapshot."""
    raw_body = checkpoint.raw_result or ""
    snapshot = checkpoint.continuity_snapshot_json or "[]"
    warning = (
        "[CORSIA warning: Checkpoint truncated at the context limit. The update "
        "is incomplete; completed continuity edits were retained. Do not treat "
        "omitted work as completed.]\n"
        if checkpoint.error == FORK_CONTEXT_TRUNCATED else ""
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
        if checkpoint.error == FORK_CONTEXT_TRUNCATED else ""
    )
    return warning + "[CORSIA prior checkpoint narrative]\n" + (checkpoint.raw_result or "")
