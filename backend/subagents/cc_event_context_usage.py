"""Claude Code terminal context-occupancy accounting."""

from __future__ import annotations

import logging
import math

from core.continuity_accounting import with_continuity
from infra import provider_usage
from infra.context_receipts import (
    attach_context_receipt,
    live_context_usage_payload,
    positive_window,
    unavailable_context_usage,
)
from infra.context_usage_attribution import merge_context_usage_with_snapshot
from infra.db_async import run_in_db
from subagents.cc_handle import context_receipt_identity
from subagents.cc_runtime_state import _Live


log = logging.getLogger("subagents.cc_runtime_events")


def context_usage_from_final_call(
    final_call_usage: dict | None,
    *,
    selected_context_window: int | None = None,
    discovered_context_window: int | None = None,
) -> dict | None:
    """Build one measured usage row from Claude Code's final-call counters.

    ``selected_context_window`` is what CORSIA counts against and may be an
    operator cap. Claude Code owns its own conversation, so ``total_used`` is
    bounded by the provider's window, NOT by that cap: between crossing the cap
    and the fold installing, the measured total legitimately exceeds it. The
    discovered window travels alongside so the surface can say which limit the
    user is looking at instead of labelling a budget a window (#1753).
    """
    if not isinstance(final_call_usage, dict):
        return None

    def token_count(key: str, *, required: bool = False) -> int | None:
        if key not in final_call_usage:
            return None if required else 0
        value = final_call_usage[key]
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        if (
            isinstance(value, float)
            and math.isfinite(value)
            and value.is_integer()
            and value >= 0
        ):
            return int(value)
        return None

    input_tokens = token_count("input_tokens", required=True)
    cache_read_tokens = token_count("cache_read_input_tokens")
    cache_creation_tokens = token_count("cache_creation_input_tokens")
    if None in (input_tokens, cache_read_tokens, cache_creation_tokens):
        return None
    total_used = provider_usage.TokenUsage(
        input_uncached=input_tokens,
        cache_read=cache_read_tokens,
        cache_write=cache_creation_tokens,
    ).input_total
    window = int(selected_context_window or 0)
    utilization = total_used / window if window else None
    return {
        "model_context_window": window,
        "discovered_context_window": positive_window(
            discovered_context_window, absent=None,
        ),
        "system_tokens": 0,
        "tool_def_tokens": 0,
        "history_tokens": total_used,
        "user_tokens": 0,
        "total_used": total_used,
        "utilization": utilization,
        "provider_reported_input_tokens": input_tokens,
        "provider_reported_cache_read_input_tokens": cache_read_tokens,
        "provider_reported_cache_creation_input_tokens": cache_creation_tokens,
        "provider_reported_total_used": total_used,
        "provider_reported_utilization": utilization,
        "token_usage_available": True,
        "was_trimmed": False,
        "history_messages": 0,
    }


def context_usage_with_snapshot(
    context_usage: dict | None,
    live: _Live,
) -> dict | None:
    return with_continuity(merge_context_usage_with_snapshot(
        context_usage,
        snapshot=live.context_snapshot,
        snapshot_id=live.context_snapshot_id,
        source_artifacts=live.source_artifacts,
        continuity_mode="reconstructed",
        known_tool_definitions=live.tool_definitions,
    ), live.continuity_accounting)


def live_measured_context_usage(
    child_id: str,
    live: _Live,
    final_call_usage: dict | None,
) -> dict | None:
    """Return current measured usage, merged with its snapshot and continuity."""
    measured = context_usage_from_final_call(
        final_call_usage,
        selected_context_window=live.context_window,
        discovered_context_window=live.discovered_context_window,
    )
    identity = context_receipt_identity(child_id, live)
    if measured is None or identity is None:
        return None
    return context_usage_with_snapshot(measured, live)


async def consume_and_attach_terminal_context_usage(
    *,
    live: _Live,
    child_id: str,
    payload: dict,
    message_id: str | None,
    db=None,
) -> dict | None:
    """Consume one Turn's final-call measurement and attach its compact receipt."""
    final_call_usage = live.last_call_usage
    live.last_call_usage = None
    measured = context_usage_from_final_call(
        final_call_usage,
        selected_context_window=live.context_window,
        discovered_context_window=live.discovered_context_window,
    )
    if measured is not None:
        live.context_usage_stale_reason = None
    else:
        measured = unavailable_context_usage(
            model_context_window=live.context_window,
            discovered_context_window=live.discovered_context_window,
            stale_reason=(
                live.context_usage_stale_reason or "terminal_without_measurement"
            ),
        )
    context_usage = context_usage_with_snapshot(measured, live)
    if not context_usage:
        payload.pop("context_message_id", None)
        return None
    receipt = None
    identity = context_receipt_identity(child_id, live)
    if message_id is not None and identity is not None:
        kwargs = {
            "context_usage": context_usage,
            "identity": identity,
        }
        try:
            if db is None:
                receipt = await run_in_db(
                    attach_context_receipt,
                    child_id,
                    message_id,
                    **kwargs,
                )
            else:
                receipt = attach_context_receipt(
                    child_id,
                    message_id,
                    db=db,
                    **kwargs,
                )
        except Exception as exc:
            log.error(
                "CC terminal context receipt failed for %s: %s",
                child_id,
                exc,
            )
    if receipt is None:
        payload.pop("context_message_id", None)
    payload["context_usage"] = live_context_usage_payload(context_usage)
    return context_usage
