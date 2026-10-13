"""Correct one over-window warm checkpoint fork to its same-model cold path.

Admission picks warm whenever a warm source exists, because only the provider
can report what the live handle actually holds. When the adapter proves at
invoke time that the fork cannot hold its own input, the operation has produced
nothing and paid nothing, so choosing the cold path is the same operation
finding its only usable route -- not a retry of a refused call.

This correction applies to pressure checkpoints only. Manual admission may
start cold when no warm source exists, while idle remains warm-only by contract
and a model switch keeps its own destination-cold ladder.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from core.checkpoint_warm_request import warm_capture_refused

from core.checkpoint_types import (
    ProviderCheckpointRequest,
    WarmForkUnusable,
)
from infra.checkpoints import adopt_cold
from infra.db_async import run_in_db


def cold_correction_available(request: ProviderCheckpointRequest) -> bool:
    """Whether this exact request may correct itself once to cold."""
    return request.warm_source is not None and request.same_model_cold is not None


async def build_cold_request(
    request: ProviderCheckpointRequest,
    reason: str = "warm_fork_over_window",
) -> ProviderCheckpointRequest:
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
    if cold.checkpoint_id != request.checkpoint_id or cold.thread_id != request.thread_id:
        raise ValueError("cold correction must stay on the same checkpoint row")
    if (cold.target.harness, cold.target.catalog_id, cold.target.transport_model) != (
            request.target.harness, request.target.catalog_id, request.target.transport_model):
        raise ValueError('cold correction must keep the exact checkpoint model')
    if cold.warm_source is not None or cold.cold_source is None:
        raise ValueError("cold correction did not produce a cold source")
    await run_in_db(adopt_cold, request.checkpoint_id)
    warm_capture_refused(request.thread_id, request.target.harness, reason)
    # The usage callback is already frozen against this checkpoint id; keep the
    # single-owner binding rather than rebinding a second one.
    return replace(cold, on_usage=request.on_usage, same_model_cold=None,
                   vendor_account_id=request.vendor_account_id,
                   vendor_account_bound=request.vendor_account_bound,
                   vendor_account_receipt=request.vendor_account_receipt,
                   vendor_launch=request.vendor_launch)


async def invoke_with_cold_correction(request, cancel_event, invoke):
    """Run one pressure invocation, correcting an unusable warm fork to cold."""
    try:
        return await invoke(request, cancel_event)
    except WarmForkUnusable as refusal:
        if not cold_correction_available(request):
            raise
        reason = refusal.reason
    if cancel_event.is_set():
        raise asyncio.CancelledError
    cold = await build_cold_request(request, reason)
    return await invoke(cold, cancel_event)
