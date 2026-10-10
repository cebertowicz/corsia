"""Turn LLM usage logging."""

from __future__ import annotations

from typing import Any

from loguru import logger

from infra.llm_usage import (
    PlanUsagePersistenceError,
    UsageReceiptConflictError,
)
from infra.product_events import emit_product_event
from llm.resolution import Harness


def _log_llm_usage(
    thread_id: str,
    model: str,
    usage: dict | None,
    source: str = "conversation",
    ttft_ms: int | None = None,
    *,
    turn_id: str | None = None,
    usage_key: str | None = None,
    binding: Any = None,
    harness: Harness | None = None,
    reasoning_effort: str | None = None,
) -> None:
    """Persist one provider call without inventing unknown measurements.

    ``binding`` is the task captured when the call began
    (``plans.usage_calls.binding_at_call_start``); the call is billed to it
    even if the Turn has since moved on.

    Runs after the provider response already exists, so a ledger failure must
    never take the response down with it: this is a terminal path guarded only
    by try/finally, and re-raising here is how a lost ledger row becomes a turn
    the user never receives.

    The catch below is therefore deliberate, and it is not a swallow. A failed
    projection leaves a durable receipt on the Turn for startup reconciliation.
    While it is pending, `plans.commitment_cost` counts that receipt under
    `unknown_rows` -- per pending row, not per Turn, since a Turn is many
    calls and its other calls' rows must not make it look measured -- so no
    total is presented as complete but short. That end-to-end guarantee is
    pinned by `test_a_lost_plan_bound_row_is_declared_unknown_not_silently_short`;
    if the binding or that per-row count goes away, the failure becomes silent
    and the catch stops being defensible.
    """

    measured = usage or {}
    normalized = {
        "prompt_tokens": measured.get("prompt_tokens"),
        "completion_tokens": measured.get("completion_tokens"),
        "cache_creation_tokens": measured.get("cache_creation_tokens"),
        "cache_read_tokens": measured.get("cache_read_tokens"),
        "total_tokens": measured.get("total_tokens"),
        "cost_usd": measured.get("cost_usd"),
    }
    try:
        from plans.public import record_call_usage

        record_call_usage(
            thread_id,
            model,
            normalized,
            source=source,
            ttft_ms=ttft_ms,
            turn_id=turn_id,
            usage_key=usage_key,
            binding=binding, harness=harness, reasoning_effort=reasoning_effort,
            _emit_product_event=emit_product_event,
        )
    except (PlanUsagePersistenceError, UsageReceiptConflictError):
        logger.error(
            "turns_util: plan-bound usage handoff incomplete for {} turn {}; "
            "turn continues",
            thread_id,
            turn_id,
        )


def usage_call_fields(
    response, iteration: int, binding, reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """The keyword facts one native provider call is recorded with.

    ``binding`` is the task captured before the provider was asked
    (``plans.usage_calls.binding_at_call_start``): the Turn may move on to
    other work before the measurement arrives, and the call stays billed to
    the task it was made for. ``reasoning_effort`` is the effort the call was
    sent with.
    """
    from infra import state as runtime_state

    ttft_ms = getattr(response, "ttft_ms", None)
    turn_identity = runtime_state._current_turn_identity.get(None)
    return {
        "ttft_ms": int(ttft_ms) if isinstance(ttft_ms, (int, float)) else None,
        "turn_id": getattr(turn_identity, "turn_id", None),
        "usage_key": f"provider-call:{iteration}",
        "binding": binding,
        "reasoning_effort": reasoning_effort,
    }
