"""Public results for the shared thread-input API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.thread_input_types import HeldAdmission


class ProviderDeliveryOutcome(str, Enum):
    """Evidence reported at the provider-write boundary."""

    CONFIRMED = "confirmed"
    AMBIGUOUS = "ambiguous"


class DeliveryFailureSide(str, Enum):
    """Authority for deciding whether an exact provider retry is possible."""

    OURS = "ours"
    PROVIDER = "provider"

    @classmethod
    def declared(cls, value) -> "DeliveryFailureSide":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value))
        except ValueError:
            return cls.OURS


@dataclass(frozen=True, slots=True)
class InputReceipt:
    """Stable identity of one accepted FIFO input."""

    thread_id: str
    message_id: str
    input_order: int
    delivery_revision: int
    created_at: str


@dataclass(frozen=True, slots=True)
class InboxQueuedInput:
    """An accepted input awaiting its safe provider boundary."""

    receipt: InputReceipt
    duplicate: bool = False
    held: HeldAdmission | None = None
    admission_hold: str | None = None

    @property
    def held_line(self) -> str | None:
        """Return the existing held-result field for a limit or admission hold."""
        from infra.thread_input_types import held_admission_line

        return held_admission_line(self.held) or (
            None if self.admission_hold is None else f"Held: {self.admission_hold}."
        )


@dataclass(frozen=True, slots=True)
class SendResult:
    """An input whose Turn was handed to its provider runtime."""

    driver: str
    message_id: str | None = None
    created_at: str | None = None
    seq: int | None = None
    turn_id: str | None = None
    turn_order: int | None = None
    duplicate: bool = False
    turn_task: asyncio.Task | None = None


class SendRejected(Exception):
    """A pre-dispatch failure with a truthful caller-facing shape."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        metadata: dict | None = None,
        side: object | None = None,
        input_undeliverable: bool = False,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.metadata = metadata or {}
        self.side = side
        self.input_undeliverable = input_undeliverable
