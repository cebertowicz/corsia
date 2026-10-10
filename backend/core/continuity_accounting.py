"""Account for the continuity block a harness delivered to the provider.

Each harness decides the outcome once, at its own delivery step, and carries
the resulting ``ContinuityAccounting`` unchanged to every receipt of that turn.
An outcome is either a measured block (bytes and tokens) or one explicit
reason; a receipt never carries bytes for a block that was not sent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.continuity_render import CONTINUITY_ENTRIES_HEADER
from core.token_estimation import _text_tokens

_RECEIPT_KEYS = (
    "continuity_block_bytes", "continuity_block_tokens", "continuity_block_reason",
)

NO_CHECKPOINT_INSTALLED = "no_checkpoint_installed"
PROVIDER_RESUME_UNOBSERVABLE = "provider_resume_unobservable"
UNACCOUNTED_CONTEXT_PATH = "unaccounted_context_path"


@dataclass(frozen=True)
class ContinuityAccounting:
    """Measured delivered block, or the single reason none was observed."""

    block_bytes: int | None = None
    block_tokens: int | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        measured = self.block_bytes is not None and self.block_tokens is not None
        if measured == (self.reason is not None):
            raise ValueError("continuity accounting is measured xor carries a reason")

    @classmethod
    def measured(cls, block: str, model: str | None) -> ContinuityAccounting:
        return cls(
            block_bytes=len(block.encode("utf-8")),
            block_tokens=_text_tokens(block, model),
        )

    @classmethod
    def not_delivered(cls) -> ContinuityAccounting:
        return cls(reason=NO_CHECKPOINT_INSTALLED)

    @classmethod
    def provider_resume(cls) -> ContinuityAccounting:
        return cls(reason=PROVIDER_RESUME_UNOBSERVABLE)

    @classmethod
    def unaccounted(cls) -> ContinuityAccounting:
        return cls(reason=UNACCOUNTED_CONTEXT_PATH)

    def receipt_fields(self) -> dict[str, Any]:
        if self.reason is not None:
            return {"continuity_block_reason": self.reason}
        return {
            "continuity_block_bytes": self.block_bytes,
            "continuity_block_tokens": self.block_tokens,
        }


def measure_delivered_block(
    delivered_text: str | None, model: str | None,
) -> ContinuityAccounting:
    """Measure the continuity block inside text the destination actually gets."""
    if not delivered_text:
        return ContinuityAccounting.not_delivered()
    start = delivered_text.find(CONTINUITY_ENTRIES_HEADER)
    if start < 0:
        return ContinuityAccounting.not_delivered()
    return ContinuityAccounting.measured(delivered_text[start:], model)


def with_continuity(
    usage: dict[str, Any] | None, accounting: ContinuityAccounting | None,
) -> dict[str, Any] | None:
    """Return ``usage`` carrying the turn's accounting, replacing any earlier one.

    A path that never decided accounting reports that explicitly, so it cannot
    be mistaken for a receipt written before this feature.
    """
    if usage is None:
        return usage
    accounting = accounting or ContinuityAccounting.unaccounted()
    kept = {k: v for k, v in usage.items() if k not in _RECEIPT_KEYS}
    return {**kept, **accounting.receipt_fields()}
