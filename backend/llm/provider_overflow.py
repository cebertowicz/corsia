"""Provider context-overflow recovery for native dispatch (I7).

A rejection for size proves our stored window was wrong. The contract
response is re-enter discovery — re-read the live window, re-fit the exact
rendered payload, retry once — and, when it still overflows, return a
machine-classified handled response so the turn owner renders the transition.
The provider's raw sentence never becomes user-facing turn content.

Classification itself lives in ``subagents/provider_context_limits.py`` — the
one marker set shared with the harness paths.
"""

from __future__ import annotations

import enum
import json
from typing import Any

from loguru import logger


class OverflowDisposition(enum.Enum):
    """What one provider error means for the dispatch loop."""

    NOT_OVERFLOW = "not_overflow"
    RETRY = "retry"
    HANDLED = "handled"


def is_context_overflow_error(error: Exception) -> bool:
    """Classify a provider rejection through the one shared overflow classifier."""
    from subagents.provider_context_limits import is_provider_context_limit

    return is_provider_context_limit(str(error))


def context_overflow_response() -> Any:
    """Terminal handled overflow: machine-classified, never a provider sentence.

    Carries no user-facing content; ``core/turns.py`` composes the handled
    discovery-failure transition from ``error_kind``.
    """
    from llm.router import LLMResponse

    return LLMResponse(
        content=None,
        finish_reason="error",
        error_kind="provider_context_overflow",
    )


async def refit_after_overflow_rediscovery(
    model: str,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int | None = None,
) -> bool:
    """Re-enter window discovery and re-fit the rendered payload against it.

    True only when fresh window evidence says this exact payload should fit,
    making one retry worthwhile; False routes to the handled discovery-failure
    response instead of another doomed provider call.
    """
    from core.token_estimation import _estimate_messages_tokens, _text_tokens
    from llm.context_window_discovery import rediscover_context_window

    try:
        window = await rediscover_context_window(model)
    except Exception as exc:
        logger.warning("context-window rediscovery failed for {}: {}", model, exc)
        return False
    if not window:
        return False
    required = _estimate_messages_tokens(list(messages), model=model)
    if tools:
        required += _text_tokens(json.dumps(tools, sort_keys=True, default=str), model) + 4
    required += max(0, int(max_tokens or 0))
    return required <= window


def _learn_memory_fit(
    error: Exception, model: str, messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None, omlx_server_id: str | None,
) -> None:
    """Record what an oMLX prefill-guard refusal teaches about this prompt's size.

    The wire model is ``openai/<model_id>``; the managed server travels beside it.
    """
    from core.token_estimation import _estimate_messages_tokens, _text_tokens
    from llm.memory_fit import record_refusal

    if not omlx_server_id:
        return
    tokens = _estimate_messages_tokens(list(messages), model=model)
    if tools:
        tokens += _text_tokens(json.dumps(tools, sort_keys=True, default=str), model) + 4
    record_refusal(
        omlx_server_id, model.removeprefix("openai/"), str(error), prompt_tokens=tokens,
    )


async def overflow_disposition(
    error: Exception,
    model: str,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int | None = None,
    already_retried: bool = False,
    omlx_server_id: str | None = None,
) -> OverflowDisposition:
    """Decide the dispatch loop's response to one provider error.

    ``NOT_OVERFLOW`` hands the error back to the normal transient logic.
    ``RETRY`` means the window was re-discovered and the payload re-fits, so
    one retry is worthwhile. ``HANDLED`` ends the call with
    :func:`context_overflow_response`.
    """
    if not is_context_overflow_error(error):
        return OverflowDisposition.NOT_OVERFLOW
    _learn_memory_fit(error, model, messages, tools, omlx_server_id)
    if not already_retried and await refit_after_overflow_rediscovery(
        model, messages=messages, tools=tools, max_tokens=max_tokens,
    ):
        logger.warning(
            "provider context overflow for {}; live window re-discovered and "
            "payload re-fits, retrying once",
            model,
        )
        return OverflowDisposition.RETRY
    logger.error(
        "provider context overflow for {} persists after window rediscovery: {}",
        model,
        error,
    )
    return OverflowDisposition.HANDLED
