"""OTel instrumentation helpers for the turn loop.

Extracted from ``core/turns.py`` to keep that file under the per-file LOC
budget. The instrumentation is no-op in production (NoOp tracer) and only
emits spans when the eval harness has called ``eval.otel.setup_tracing``.

Span hierarchy emitted:
    corsia.turn (root)
    ├── corsia.llm_call         (one per agent-loop iteration)
    ├── corsia.tool_loop        (one per "model emits N tools → all run")
    │   └── corsia.tool_call    (one per tool dispatch — wrapped in registry)
    └── ...

Three primitives:
- ``turn_span_scope(...)`` — async context-manager-flavored helper that
  opens the root turn span, emits attributes, and closes it on exit.
- ``llm_span(...)`` — synchronous context manager around a single LLM call.
- ``tool_loop_span(...)`` — synchronous context manager around a tool-loop
  iteration.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from eval.otel import (
    get_tracer, set_attrs,
    SPAN_TURN, SPAN_LLM_CALL, SPAN_TOOL_LOOP, SPAN_CONTEXT_BUILD,
)


_TRACER_NAME = "corsia.turns"


@contextmanager
def turn_span(
    *, thread_id: str, turn_model: str, fresh_context: bool,
) -> Iterator[Any]:
    """Open the root turn span. Yields the span so callers can attach
    end-of-turn attributes (iteration_count, tools_used) before exit.
    """
    tracer = get_tracer(_TRACER_NAME)
    cm = tracer.start_as_current_span(SPAN_TURN)
    span = cm.__enter__()
    set_attrs(span, {
        "turn.session_id": thread_id,
        "turn.model": turn_model,
        "turn.fresh_context": bool(fresh_context),
    })
    try:
        yield span
    finally:
        cm.__exit__(None, None, None)


@contextmanager
def llm_span(
    *, model: str, iteration: int, streaming: bool,
) -> Iterator[Any]:
    """Wrap a single LLM call. Caller can ``set_attrs`` post-response."""
    tracer = get_tracer(_TRACER_NAME)
    with tracer.start_as_current_span(SPAN_LLM_CALL) as span:
        set_attrs(span, {
            "llm.model": model,
            "llm.iteration": iteration,
            "llm.streaming": bool(streaming),
        })
        yield span


@contextmanager
def tool_loop_span(*, tool_count: int, tool_names: list[str]) -> Iterator[Any]:
    """Wrap a tool-loop iteration."""
    tracer = get_tracer(_TRACER_NAME)
    with tracer.start_as_current_span(SPAN_TOOL_LOOP) as span:
        set_attrs(span, {
            "tool_loop.tool_count": tool_count,
            "tool_loop.names": tool_names,
        })
        yield span


@contextmanager
def context_build_span() -> Iterator[Any]:
    """Wrap a single ``ContextBuilder.build_messages`` call.

    Yields the span so the caller can attach post-build attributes
    (prompt_tokens, kb_recent_count, system_prompt_size, messages_count)
    before exit. NoOp in production paths — only emits real spans when
    ``eval.otel.setup_tracing()`` has been called.
    """
    tracer = get_tracer(_TRACER_NAME)
    with tracer.start_as_current_span(SPAN_CONTEXT_BUILD) as span:
        yield span


__all__ = [
    "turn_span", "llm_span", "tool_loop_span", "context_build_span", "set_attrs",
]
