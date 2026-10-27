"""Data-only contracts returned by the canonical thread-input FIFO owner."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal

from connections.account_receipts import ReceiptFence


@dataclass(frozen=True, slots=True)
class ClaimedInput:
    """One input claimed by an exact active Turn."""

    message_id: str
    input_order: int
    revision: int
    role: str
    content: str
    intent: str
    metadata: dict
    delivery_options: dict
    created_at: str


@dataclass(frozen=True, slots=True)
class TurnIdentity:
    """Stable identity of one logical assistant response."""

    turn_id: str
    turn_order: int

    def frame_fields(self) -> dict[str, str | int]:
        """Return this identity in the public Turn-frame shape."""
        return {"turn_id": self.turn_id, "turn_order": self.turn_order}


@dataclass(frozen=True, slots=True)
class Admission:
    """One verdict on a target. ``load_for`` names the person inputs that authorize a load."""

    kind: Literal["run", "load", "wait", "fail"]
    cause: str | None = None
    load_for: tuple[str, ...] = ()

    def error(self, catalog_id: str) -> Exception:
        """The one refusal error: a person's load refusal, otherwise the unavailable model."""
        from llm.local_model_load import LOAD_REFUSALS, ModelLoadFailed
        from llm.resolution import ModelUnavailableError

        if self.load_for and self.cause in LOAD_REFUSALS:
            return ModelLoadFailed(self.cause)  # type: ignore[arg-type]
        return ModelUnavailableError(catalog_id, self.cause or "model_unavailable")


@dataclass(frozen=True, slots=True)
class ClaimedBatch:
    """One contiguous FIFO prefix claimed by a Turn."""

    thread_id: str
    turn_id: str
    turn_order: int
    target_execution_epoch: int
    target_model: str
    items: tuple[ClaimedInput, ...]
    vendor_receipt: ReceiptFence | None = field(default=None, repr=False)
    #: Error rows this claim stamped `recovered`; published with the batch.
    recovered_message_ids: tuple[str, ...] = ()
    admission: Admission = Admission("run")

    @property
    def identity(self) -> TurnIdentity:
        """Return the stable identity carried by this claimed batch."""
        return TurnIdentity(self.turn_id, self.turn_order)

    @property
    def anchor_message_id(self) -> str:
        """The batch's final row: it takes the max seq on settle, so it is the
        resume anchor a warm boundary names. Providers record it as the input's
        own id (Claude Code frame ``uuid``, Codex ``clientUserMessageId``)."""
        return self.items[-1].message_id


async def bind_exact_turn(batch: ClaimedBatch, turn_id: str, _previous) -> None:
    if turn_id != batch.turn_id:
        raise RuntimeError("provider tried to replace the admitted Turn identity")


@dataclass(frozen=True, slots=True)
class FailedInputBatch:
    """The exact failed FIFO prefix and its committed caller-visible reason."""

    thread_id: str
    message_ids: tuple[str, ...]
    error_text: str


@dataclass(frozen=True, slots=True)
class HeldAdmission:
    """A FIFO prefix retained because its bound connection is account-limited."""

    connection: str
    reason: str | None
    resets_at: str | None
    thread_id: str
    model: str


def held_admission_line(held: HeldAdmission | None) -> str | None:
    """Render the one sender-facing account-limit admission message."""
    if held is None:
        return None
    if held.resets_at is None:
        return f"Held: account limit on {held.connection}; reset time unknown."
    try:
        reset = datetime.fromisoformat(held.resets_at.replace("Z", "+00:00"))
    except ValueError:
        reset = None
    local_time = held.resets_at if reset is None else reset.astimezone().strftime("%b %-d, %Y at %-I:%M %p %Z")
    return f"Held: account limit on {held.connection}, resets {local_time}."


TurnAdmission = ClaimedBatch | FailedInputBatch | HeldAdmission


def batch_option(batch: ClaimedBatch, name: str, default=None):
    """Read the newest member's request-local presentation option."""
    for item in reversed(batch.items):
        if name in item.delivery_options:
            return item.delivery_options[name]
    return default


def safe_input_prefixes(batch: ClaimedBatch):
    """Try longest Send-admitted prefixes first; never split off queued-only input."""
    for length in range(len(batch.items), 0, -1):
        if batch.items[length - 1].intent == "apply_now":
            yield replace(batch, items=batch.items[:length])
