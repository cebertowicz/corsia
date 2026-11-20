"""Capture and install one provider checkpoint through the existing store."""

from __future__ import annotations

import json
from dataclasses import replace

from core.checkpoint_render import (
    carry_partial_narrative,
    carry_provider_error_partial,
    complete_lines,
    complete_narrative,
    usable_result,
)
from core.fork_types import (
    FORK_CONTEXT_TRUNCATED,
    ProviderForkRejected,
    ProviderForkResult,
    is_partial_provider_error,
)
from core.checkpoint_types import (

    CheckpointRecord,
)
from infra import checkpoint_queries
from infra import checkpoints as checkpoint_store
from infra.checkpoints import CheckpointTransitionError
from infra.db_async import run_in_db
from infra.provider_usage import checkpoint_call_receipts
from infra.time import utc_now_iso


class CheckpointNotFound(RuntimeError):
    """The requested retained checkpoint does not exist."""


async def read_checkpoint(checkpoint_id: str) -> CheckpointRecord:
    record = await run_in_db(checkpoint_queries.read, checkpoint_id)
    if record is None:
        raise CheckpointNotFound(f"checkpoint {checkpoint_id!r} does not exist")
    return record


async def terminal_failure(checkpoint_id: str, error: BaseException) -> CheckpointRecord:
    detail = str(error).strip() or type(error).__name__
    try:
        return await run_in_db(
            checkpoint_store.fail, checkpoint_id,
            error=detail, ended_at=utc_now_iso(),
        )
    except CheckpointTransitionError:
        return await read_checkpoint(checkpoint_id)


async def capture_and_install(checkpoint_id: str, result: ProviderForkResult) -> CheckpointRecord:
    # Gate on the provider's own output, before any carry-forward: a call
    # that produced no narrative text is a failed attempt, whether or not a
    # previous generation or staged edits exist. Staged edits alone no longer
    # earn an install, and a call that made no progress must not silently
    # re-stamp the previous narrative under a new generation (#1767). Whenever
    # this passes, carry_partial_narrative below can only extend a nonblank
    # body, so the body it captures is never blank either.
    if not usable_result(result.raw_body):
        from loguru import logger

        logger.warning(
            "checkpoint.no_content checkpoint={} diagnostic={}",
            checkpoint_id, result.diagnostic,
        )
        return await terminal_failure(
            checkpoint_id, ProviderForkRejected("checkpoint provider produced no content"),
        )
    truncated = result.diagnostic == FORK_CONTEXT_TRUNCATED
    provider_error = is_partial_provider_error(result.diagnostic)
    if truncated or provider_error:
        current = await read_checkpoint(checkpoint_id)
        previous = (
            await read_checkpoint(current.previous_generation_id)
            if current.previous_generation_id else None
        )
        if provider_error:
            # Never replaces the last complete narrative: only whole lines of the
            # assistant's output before the error are carried after it.
            partial = complete_lines(result.raw_body)
            if not partial:
                return await terminal_failure(checkpoint_id, ProviderForkRejected(
                    "checkpoint provider error left no complete line"))
            body = carry_provider_error_partial(complete_narrative(previous), partial)
            result = replace(result, raw_body=body)
        elif previous is not None:
            body = carry_partial_narrative(complete_narrative(previous), result.raw_body)
            result = replace(result, raw_body=body)
    usage = (
        json.dumps(checkpoint_call_receipts(result.usage), sort_keys=True, separators=(",", ":"))
        if result.usage is not None else None
    )
    try:
        await run_in_db(
            checkpoint_store.capture, checkpoint_id,
            raw_result=result.raw_body, usage_json=usage,
            captured_at=utc_now_iso(), diagnostic=result.diagnostic,
        )
    except CheckpointTransitionError:
        current = await read_checkpoint(checkpoint_id)
        if current.status == "cancelled":
            return current
        raise
    return await run_in_db(checkpoint_store.install, checkpoint_id, installed_at=utc_now_iso())
