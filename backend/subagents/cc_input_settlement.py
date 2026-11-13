"""Settle one Claude Code input write's delivery evidence."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from subagents.cc_runtime_state import _Live


async def settle_input_delivery(
    settle_delivery: Callable[[], Awaitable[None]],
) -> None:
    settlement = asyncio.ensure_future(settle_delivery())
    while not settlement.done():
        try:
            await asyncio.shield(settlement)
        except asyncio.CancelledError:
            continue
    settlement.result()


async def settle_unverified_input_write(
    live: _Live,
    *,
    mark_ambiguous: Callable[[], None] | None,
    settle_delivery: Callable[[], Awaitable[None]] | None,
) -> None:
    """Settle one unproven input write ambiguous; its Turn's terminal stays with its owner."""
    if mark_ambiguous is not None:
        mark_ambiguous()
    live.input_delivery_unproven = True
    if settle_delivery is not None:
        await settle_input_delivery(settle_delivery)
