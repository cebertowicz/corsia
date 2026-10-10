"""Retire a Codex handle after provider compaction and adopt a CORSIA checkpoint."""

from __future__ import annotations

import asyncio

from subagents.codex_runtime_support import async_method

# A liveness backstop, not a latency target: the adoption waits for a required
# checkpoint to be written. Stop releases it sooner; this only guarantees a
# stuck wait ends in a visible, settled Turn.
ADOPTION_TIMEOUT_SECONDS = 600.0


async def retire_compacting_handle(thread_id: str, live, session) -> None:
    """Terminate the compacted handle and adopt a checkpoint, or settle the Turn.

    The dead-generation owner is the only settler of this exit. It is held off
    from the moment the process is terminated until this retirement either
    resumes the Turn or settles it with the reason adoption failed.
    """
    from subagents.codex_runtime import settle_dead_live_generation_if_observed
    from subagents.harness_checkpoint_resume import maintain_external_tool_boundary

    live.exit_owned_by_compaction_retire = True
    bound = asyncio.timeout(ADOPTION_TIMEOUT_SECONDS)
    try:
        terminate = async_method(session, "terminate")
        if terminate is None:
            raise RuntimeError("Codex compaction handle cannot be terminated")
        await terminate()
        async with bound:
            resumed = await maintain_external_tool_boundary(
                live, thread_id, "codex", must_adopt=True,
            )
        if not resumed:
            raise RuntimeError("Codex compaction boundary produced no continuation")
    except BaseException as error:
        live.provider_context_untrusted = True
        await settle_dead_live_generation_if_observed(
            thread_id, retiring=live, error=_settle_reason(error, timed_out=bound.expired()),
        )
        raise
    finally:
        live.exit_owned_by_compaction_retire = False


def _settle_reason(error: BaseException, *, timed_out: bool) -> str:
    if timed_out:
        return (
            "Codex compaction checkpoint adoption timed out after "
            f"{ADOPTION_TIMEOUT_SECONDS:g}s"
        )
    if isinstance(error, asyncio.CancelledError):
        return "Codex compaction checkpoint adoption was cancelled"
    return str(error) or type(error).__name__
