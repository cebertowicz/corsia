"""Canonical post-persistence telemetry for LLM usage rows."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from loguru import logger

from infra.product_events import emit_product_event


def cache_read_fraction(
    *,
    source: str,
    prompt_tokens: int | None,
    cache_read_tokens: int | None,
    cache_creation_tokens: int | None,
) -> float | None:
    """Compute live telemetry from canonical disjoint ledger counts."""
    if (
        not isinstance(source, str)
        or isinstance(prompt_tokens, bool)
        or not isinstance(prompt_tokens, int)
        or prompt_tokens < 0
        or isinstance(cache_read_tokens, bool)
        or not isinstance(cache_read_tokens, int)
        or cache_read_tokens < 0
        or isinstance(cache_creation_tokens, bool)
        or not isinstance(cache_creation_tokens, int)
        or cache_creation_tokens < 0
    ):
        return None
    denominator = prompt_tokens + cache_read_tokens + cache_creation_tokens
    return cache_read_tokens / denominator if denominator > 0 else None


def emit_llm_usage_recorded(
    thread_id: str,
    model: str,
    usage: Mapping[str, Any],
    *,
    source: str,
    ttft_ms: int | None = None,
    _emit_product_event: Callable[..., None] | None = None,
) -> None:
    """Emit canonical telemetry after one usage row is durably committed."""
    emit = _emit_product_event or emit_product_event
    cost = usage.get("cost_usd")
    observed_cache_fraction = cache_read_fraction(
        source=source,
        prompt_tokens=usage.get("prompt_tokens"),
        cache_read_tokens=usage.get("cache_read_tokens"),
        cache_creation_tokens=usage.get("cache_creation_tokens"),
    )
    try:
        if cost is not None and cost > 0:
            logger.info(
                "llm_usage: {} — {} tokens, ${:.6f} [{}]",
                model,
                usage.get("total_tokens"),
                cost,
                source,
            )
        emit(
            "cost",
            "recorded",
            thread_id=thread_id,
            properties={
                "model": model,
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
                "cache_creation_tokens": usage.get("cache_creation_tokens"),
                "cache_read_tokens": usage.get("cache_read_tokens"),
                "cost_usd": cost,
                "ttft_ms": ttft_ms,
                "source": source,
                "cache_read_fraction": observed_cache_fraction,
            },
        )
    except Exception as exc:
        logger.warning(
            "llm_usage: failed to emit usage event for {}: {}",
            thread_id,
            type(exc).__name__,
        )
