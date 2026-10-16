"""What the first-run classifier download tells the outside: client nudges, the worker wake and the bounded public state."""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

State = dict[str, Any]

_waker: Callable[[], None] | None = None


def set_corrections_waker(waker: Callable[[], None]) -> None:
    """Startup registers the correction worker's scan, so core never imports jobs."""
    global _waker
    _waker = waker


def broadcast_update() -> None:
    """Nudge connected clients to re-read the state; a screen with no socket reads on its own events."""
    from infra.state.ws import broadcast_all, broadcast_from_any_thread

    broadcast_from_any_thread(lambda: broadcast_all({"type": "classifier_updated"}), "classifier_updated")


def wake_corrections() -> None:
    """A newly installed asset: have the correction worker read whatever it left pending."""
    import infra.app_loop as app_loop

    waker = _waker
    if waker is None:
        logger.info("Corrections worker not woken: not registered yet; its startup scan reads the backlog")
        return

    async def _on_loop() -> None:
        waker()

    try:
        app_loop.submit(_on_loop())
    except app_loop.AppLoopUnavailable:
        logger.info("Corrections worker not woken: no live event loop; its next startup reads the backlog")


def public_state(state: State) -> State:
    """The bounded shape setup exposes; technical codes stay out of the reader's way."""
    shape = state.get("state", "idle")
    if shape == "downloading":
        return {
            "state": "downloading",
            "bytes_done": int(state.get("bytes_done", 0)),
            "bytes_total": int(state.get("bytes_total", 0)),
            **({"interrupted": True} if state.get("interrupted") else {}),
        }
    if shape == "failed":
        return {
            "state": "failed",
            "code": state.get("code", "unexpected"),
            "reason": state.get("reason", ""),
            "retryable": bool(state.get("retryable", True)),
            **({"bytes_needed": state["bytes_needed"]} if "bytes_needed" in state else {}),
        }
    return {"state": shape}
