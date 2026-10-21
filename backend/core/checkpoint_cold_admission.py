"""Checkpoint-only durable admission around a generic provider-fork request."""

from core.checkpoint_fork_kind import CHECKPOINT_KIND
from core.checkpoint_render import render_prior_checkpoint
from core.checkpoint_resume import CheckpointAdmission, installed_replay
from core.checkpoint_types import CheckpointRequest, CheckpointTrigger
from core.fork_admission import build_cold_fork_admission
from core.fork_types import ColdForkSource
from history.projection import slice_portable_projection
from infra.time import utc_now_iso
from prompts.checkpoint import build_checkpoint_system_prompt


def _cold_source(db, *, projection, previous, boundary):
    source = projection
    protected: list[dict[str, str]] = []
    if previous is not None:
        source = slice_portable_projection(installed_replay(previous, db=db).tail, through=boundary)
        protected.append({"role": "user", "content": render_prior_checkpoint(previous)})
    return ColdForkSource(source, tuple(protected))


def build_checkpoint_admission(
    *, checkpoint_id: str, thread_id: str, trigger: CheckpointTrigger,
    source_target, writer_target, boundary, previous, projection, working_dir: str,
    reasoning_effort: str | None, binary: str | None, db,
    source_model: str | None = None, mcp_config: str | None = None,
    originating_turn_id: str | None = None,
) -> CheckpointAdmission:
    """Freeze the exact checkpoint request and its matching provider invocation."""
    source = _cold_source(db, projection=projection, previous=previous, boundary=boundary)
    provider = build_cold_fork_admission(
        fork_id=checkpoint_id, thread_id=thread_id, target=writer_target, kind=CHECKPOINT_KIND,
        instruction=build_checkpoint_system_prompt(), source=source,
        working_dir=working_dir, reasoning_effort=reasoning_effort,
        binary=binary, mcp_config=mcp_config,
    )
    request = CheckpointRequest(
        id=checkpoint_id, thread_id=thread_id, history_epoch=boundary.history_epoch,
        previous_generation_id=previous.id if previous else None,
        through_seq=boundary.seq, through_message_id=boundary.message_id,
        trigger=trigger, mode="cold",
        source_harness=getattr(source_target, "harness", None) or "unknown",
        source_model=source_model or getattr(source_target, "catalog_id", None) or "unknown",
        writer_harness=writer_target.harness, writer_model=writer_target.catalog_id,
        originating_turn_id=originating_turn_id, first_included_seq=None,
        started_at=utc_now_iso(),
    )
    return CheckpointAdmission(request, provider)
