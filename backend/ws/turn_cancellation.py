"""Cancellation settlement for native Turns."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime
from typing import Any, Callable, Coroutine

from core.turn_core import TurnIdentity
from ws.turn_helpers import (
    NativeTurnRequest,
    emit_terminal_product_event,
    reject_future,
    resolve_future,
)
from ws.turn_types import _PreparedTurn


async def handle_cancelled_turn(
    request: NativeTurnRequest,
    error: asyncio.CancelledError,
    prepared: _PreparedTurn | None,
    product_terminal: bool,
    started_at: datetime,
    emit: Callable[..., None],
    settle: Callable[[NativeTurnRequest, BaseException, TurnIdentity], Coroutine[Any, Any, None]],
) -> None:
    """Settle a cancelled native Turn before releasing caller-visible futures."""
    if prepared is None and request.preadmitted_turn is not None:
        settlement: asyncio.Task[None] = asyncio.create_task(
            settle(request, error, request.preadmitted_turn)
        )
        while not settlement.done():
            with suppress(asyncio.CancelledError):
                await asyncio.shield(settlement)
        await settlement
    reject_future(request.start_ack, error)
    resolve_future(request.provider_acceptance, False)
    if request.ingress == "event" and prepared is not None and not product_terminal:
        emit_terminal_product_event(
            request, prepared.identity, prepared.active_model, True, started_at, emit,
        )
