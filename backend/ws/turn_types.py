"""Private type definitions shared by native Turn execution phases."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Literal, NamedTuple

from core.native_turn_control import TurnCancelEvent
from core.step_handler import StepHandler
from core.turn_core import TurnIdentity

TurnIngress = Literal["ws", "delegation", "event"]
TurnBroadcast = Callable[[dict], Awaitable[None]]


class SettledSetupError(Exception):
    """Setup failed after its bound runtime and input were settled."""

    def __init__(self, error: BaseException) -> None:
        super().__init__(str(error))
        self.error = error


class _PreparedTurn(NamedTuple):
    """Fully prepared runtime state for an admitted native Turn."""

    agent: Any
    active_model: str
    provider: dict
    cancel_event: TurnCancelEvent
    identity: TurnIdentity
    send_binding: tuple[TurnBroadcast, list[TurnBroadcast | None]]
    handler: StepHandler
    agent_content: str
    media_projection: Any
    unbind: Callable[[TurnIdentity], None]
    reasoning_effort: str | None
