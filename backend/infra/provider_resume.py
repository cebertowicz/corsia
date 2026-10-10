"""One provider-resume decision for send preflight and context preview."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from infra.send_replay_reads import ProviderCapacity, read_provider_capacity
from llm.resolution import ExecutionTarget


@dataclass(frozen=True, slots=True)
class ProviderResumeDecision:
    """Whether a selected provider handle may resume and its trusted capacity."""

    available: bool
    capacity: ProviderCapacity | None

    @property
    def retained_tokens(self) -> int | None:
        if self.capacity is None:
            return None
        return self.capacity.measured_tokens + self.capacity.tail_tokens


def _same_selected_model(thread_row: Any, target: ExecutionTarget) -> bool:
    return bool(
        hasattr(thread_row, "keys")
        and "model" in thread_row.keys()
        and str(thread_row["model"] or "") == target.catalog_id
    )


def provider_resume_available(
    provider: str,
    *,
    thread_row: Any,
    target: ExecutionTarget,
    runtime_truth: dict,
) -> bool:
    """Return whether the selected provider handle may resume unchanged."""
    if not _same_selected_model(thread_row, target):
        return False
    if provider == "cc":
        return bool(
            runtime_truth.get("cc_has_session")
            or (runtime_truth.get("cc_live") and runtime_truth.get("busy"))
        )
    if provider == "codex":
        return bool(
            runtime_truth.get("codex_has_thread")
            or (runtime_truth.get("codex_live") and runtime_truth.get("busy"))
        )
    return False


def provider_resume_decision(
    thread_id: str,
    *,
    provider: str,
    thread_row: Any,
    target: ExecutionTarget,
    runtime_truth: dict,
    db=None,
) -> ProviderResumeDecision:
    """Return the one live-handle decision and matching measured capacity.

    ``db`` makes the receipt read share the caller's explicit state target.
    A stale/mismatched receipt is deliberately represented as no capacity; send
    may then render a cold candidate, while the preview defers to the last
    measured reading rather than presenting that cold candidate as occupancy.
    """
    if not provider_resume_available(
        provider, thread_row=thread_row, target=target, runtime_truth=runtime_truth,
    ):
        return ProviderResumeDecision(False, None)

    capacity = read_provider_capacity(thread_id, db=db)
    harness = {"cc": "claude-code", "codex": "codex"}.get(provider)
    matches = bool(
        capacity is not None
        and capacity.harness == harness
        and capacity.catalog_model_id == target.catalog_id
    )
    return ProviderResumeDecision(True, capacity if matches else None)
