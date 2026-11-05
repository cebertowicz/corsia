"""Correct one over-window warm checkpoint fork to its same-model cold path.

Admission picks warm whenever a warm source exists, because only the provider
can report what the live handle actually holds. When the adapter proves at
invoke time that the fork cannot hold its own input, or its grown input runs
out of room before it wrote any narrative, the warm attempt has nothing to
install. The row then makes one cold attempt on a fitted input. That attempt
starts clean: ``adopt_cold`` discards whatever the warm fork staged, and its
calls are recorded under their own keys beside the warm calls already paid
for. A cold attempt that fails ends the row; nothing chains.

This correction applies to pressure checkpoints only. Manual admission may
start cold when no warm source exists, while idle remains warm-only by contract
and a model switch keeps its own destination-cold ladder.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace


from core.fork_capture import warm_capture_refused

from core.checkpoint_types import CheckpointProviderForkRequest
from core.fork_types import ForkInputExhausted, WarmForkUnusable
from infra.checkpoints import adopt_cold
from infra.db_async import run_in_db


def cold_correction_available(request: CheckpointProviderForkRequest) -> bool:
    """Whether this exact request may correct itself once to cold."""
    return request.warm_source is not None and request.same_model_cold is not None


async def build_cold_request(
    request: CheckpointProviderForkRequest,
    reason: str = "warm_fork_over_window",
) -> CheckpointProviderForkRequest:
    """Build the cold request this caller would have built with no warm source.

    ``reason`` is the refused warm fork's own token, logged as the cause.

    Raises whatever the cold builder raises, including ColdFoldInputTooLarge
    when no cold fold is possible either; that is a terminal failure for the
    operation, not another path to try.
    """
    admission = request.same_model_cold()
    if hasattr(admission, "__await__"):
        admission = await admission
    cold = admission.provider_request
    if cold.fork_id != request.fork_id or cold.thread_id != request.thread_id:
        raise ValueError("cold correction must stay on the same checkpoint row")
    if (cold.target.harness, cold.target.catalog_id, cold.target.transport_model) != (
            request.target.harness, request.target.catalog_id, request.target.transport_model):
        raise ValueError('cold correction must keep the exact checkpoint model')
    if cold.warm_source is not None or cold.cold_source is None:
        raise ValueError("cold correction did not produce a cold source")
    await run_in_db(adopt_cold, request.fork_id)
    warm_capture_refused(request.thread_id, request.target.harness, reason)
    # The usage callback is already frozen against this checkpoint id; keep the
    # single-owner binding, keyed apart so cold call N never meets warm call N.
    return replace(cold, on_usage=_cold_keyed(request.on_usage), same_model_cold=None,
                   vendor_account_id=request.vendor_account_id,
                   vendor_account_bound=request.vendor_account_bound,
                   vendor_account_receipt=request.vendor_account_receipt,
                   vendor_launch=request.vendor_launch)


def _cold_keyed(on_usage):
    if on_usage is None:
        return None

    async def cold_on_usage(call_key, *args):
        return await on_usage(f"cold:{call_key}", *args)

    return cold_on_usage


async def invoke_with_cold_correction(request, cancel_event, invoke):
    """Run one pressure invocation, correcting an unusable warm fork to cold."""
    try:
        return await invoke(request, cancel_event)
    except (WarmForkUnusable, ForkInputExhausted) as refusal:
        if not cold_correction_available(request):
            raise
        reason = refusal.reason
        if isinstance(refusal, ForkInputExhausted):
            # Its text (path, counted, bound) diagnoses the warm count; keep it.
            reason = f"{reason}: {refusal}"
    if cancel_event.is_set():
        raise asyncio.CancelledError
    cold = await build_cold_request(request, reason)
    return await invoke(cold, cancel_event)
