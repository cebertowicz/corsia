"""Build the cold admission an external-harness boundary falls back to.

Split out of `harness_checkpoint_resume` so that module can stay small: this
is the one path both its warm and boundaryless cases use to freeze the cold
admission a boundary would have built with no warm source.
"""

from __future__ import annotations

from typing import Any

from subagents.harness_checkpoint_context import ExternalCheckpointContext


def build_external_cold_admission(
    checkpoint_id: str,
    *,
    context: ExternalCheckpointContext,
    live: Any,
    identity: Any,
):
    """Freeze the cold admission this boundary uses when no warm fork serves."""
    from core.checkpoint_render import build_cold_checkpoint_admission
    from core.checkpoint_types import CheckpointNotAdmitted
    from history.projection import slice_portable_projection
    from infra.database import get_db

    # Late settlement of an older Step can invalidate an already installed cut.
    # Apply the warm capture prerequisite before freezing the cold fallback too.
    if getattr(live, "tool_step_ids", None):
        raise CheckpointNotAdmitted("external checkpoint tool calls are outstanding")
    boundary = context.boundary
    db = get_db()
    try:
        session = getattr(live, "session", None)
        mcp_config = (
            getattr(session, "mcp_config", None)
            if context.target.harness == "claude-code"
            else None
        )
        return build_cold_checkpoint_admission(
            checkpoint_id=checkpoint_id,
            thread_id=boundary.thread_id,
            trigger="pressure",
            source_target=context.target,
            writer_target=context.target,
            boundary=boundary,
            previous=context.previous,
            projection=slice_portable_projection(context.history, through=boundary),
            working_dir=context.working_dir,
            reasoning_effort=context.reasoning_effort,
            binary=context.binary,
            mcp_config=mcp_config,
            originating_turn_id=identity.turn_id,
            db=db,
        )
    finally:
        db.close()
