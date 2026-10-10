"""Frozen destination payload and preflight result contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from core.context_replay import ReplaySelection
from core.destination_fit import DestinationFitLedger
from history.types import HistoryBoundary


@dataclass(frozen=True)
class RenderedDestinationPayload:
    """Frozen destination rendering measured by preflight and used by dispatch."""

    payload: Any
    input_tokens: int
    current_user_tokens: int = 0
    source_tokens: int = 0
    source_section_tokens: dict[str, int] | None = None
    accounting_payload: Any | None = None
    fit_section_tokens: dict[str, int] | None = None
    fit_input_identity: str | None = None
    fit_current_input: Any | None = None
    fit_components: dict[str, Any] | None = None
    fit_retained_provider_tokens: int = 0


@dataclass(frozen=True)
class PreparedContextPayload:
    """Truthful admission result carried unchanged to the provider boundary."""

    mode: Literal["provider_resume", "exact_replay", "checkpoint_tail"]
    target_catalog_id: str
    model_context_window: int | None
    history_epoch: int
    sealed_boundary: HistoryBoundary | None
    replay: ReplaySelection | None
    rendered: RenderedDestinationPayload
    live_provider_measured_tokens: int | None = None
    live_provider_tail_tokens: int = 0
    runtime_context_epoch: int = 0
    fit_ledger: DestinationFitLedger | None = None


@dataclass(frozen=True)
class ContextPreflightResult:
    status: str
    continuity_mode: str
    model_context_window: int
    total_tokens: int
    current_user_tokens: int = 0
    history_tokens: int = 0
    source_tokens: int = 0
    source_section_tokens: dict[str, int] | None = None
    block_reason: str | None = None
    trigger_reason: str | None = None
    prepared_context: PreparedContextPayload | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the preflight result for send-service callers."""
        return {
            "status": self.status,
            "continuity_mode": self.continuity_mode,
            "model_context_window": self.model_context_window,
            "total_tokens": self.total_tokens,
            "current_user_tokens": self.current_user_tokens,
            "history_tokens": self.history_tokens,
            "source_tokens": self.source_tokens,
            "source_section_tokens": self.source_section_tokens or {},
            "block_reason": self.block_reason,
            "trigger_reason": self.trigger_reason,
            "destination_fit_ledger": (
                self.prepared_context.fit_ledger.to_dict()
                if self.prepared_context is not None
                and self.prepared_context.fit_ledger is not None
                else None
            ),
            "prepared_context": self.prepared_context,
        }
