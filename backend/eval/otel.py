from __future__ import annotations

import os
import threading
from collections import OrderedDict
from typing import Any

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.sdk.trace import TracerProvider, ReadableSpan, SpanProcessor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.trace import (
        Tracer, NoOpTracer, NoOpTracerProvider, Span, Status, StatusCode, format_trace_id,
    )
    _OTEL_AVAILABLE = True
except Exception:  # pragma: no cover - defensive
    _OTEL_AVAILABLE = False
    _otel_trace = None  # type: ignore[assignment]
    TracerProvider = ReadableSpan = SpanProcessor = Resource = None  # type: ignore[assignment]
    Tracer = NoOpTracer = NoOpTracerProvider = Span = Status = StatusCode = None  # type: ignore[assignment]

    def format_trace_id(_: int) -> str:  # type: ignore[misc]
        return "0" * 32



SPAN_TURN = "corsia.turn"
SPAN_CONTEXT_BUILD = "corsia.context_build"
SPAN_LLM_CALL = "corsia.llm_call"
SPAN_TOOL_LOOP = "corsia.tool_loop"
SPAN_TOOL_CALL = "corsia.tool_call"



_RecorderBase = SpanProcessor if _OTEL_AVAILABLE else object


class EvalSpanRecorder(_RecorderBase):  # type: ignore[misc]

    def __init__(self, max_traces: int = 256):
        if _OTEL_AVAILABLE:
            super().__init__()
        self._lock = threading.Lock()
        self._traces: "OrderedDict[int, list[dict[str, Any]]]" = OrderedDict()
        self._max_traces = max_traces

    def on_start(self, span: Any, parent_context: Any | None = None) -> None:
        return None

    def on_end(self, span: "ReadableSpan") -> None:
        try:
            ctx = span.get_span_context()
            trace_id = ctx.trace_id
            entry = {
                "name": span.name,
                "start_ns": int(span.start_time or 0),
                "end_ns": int(span.end_time or 0),
                "attrs": {k: _coerce(v) for k, v in (span.attributes or {}).items()},
                "status": str(getattr(span.status, "status_code", "UNSET")),
            }
            with self._lock:
                bucket = self._traces.get(trace_id)
                if bucket is None:
                    if len(self._traces) >= self._max_traces:
                        self._traces.popitem(last=False)
                    bucket = []
                    self._traces[trace_id] = bucket
                else:
                    self._traces.move_to_end(trace_id)
                bucket.append(entry)
        except Exception as e:
            from loguru import logger
            logger.warning(f"EvalSpanRecorder.on_end failed; dropping span: {e}")
            return None

    def shutdown(self) -> None:
        with self._lock:
            self._traces.clear()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def pop(self, trace_id: int) -> list[dict[str, Any]]:
        with self._lock:
            return self._traces.pop(trace_id, [])

    def peek(self, trace_id: int) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._traces.get(trace_id, []))



_TRACER_PROVIDER: Any | None = None
_RECORDER: EvalSpanRecorder | None = None
_SETUP_LOCK = threading.Lock()


def setup_tracing(*, force: bool = False) -> EvalSpanRecorder | None:
    global _TRACER_PROVIDER, _RECORDER
    if not _OTEL_AVAILABLE:
        return None

    with _SETUP_LOCK:
        if _RECORDER is not None and not force:
            return _RECORDER

        provider = TracerProvider(  # type: ignore[misc]
            resource=Resource.create({"service.name": "corsia"}),
        )
        recorder = EvalSpanRecorder()
        provider.add_span_processor(recorder)
        _otel_trace.set_tracer_provider(provider)
        _TRACER_PROVIDER = provider
        _RECORDER = recorder
        return recorder


def get_recorder() -> EvalSpanRecorder | None:
    return _RECORDER


def get_tracer(name: str = "corsia.eval"):
    if not _OTEL_AVAILABLE:
        return _NoOpShim()
    return _otel_trace.get_tracer(name)


def is_tracing_enabled() -> bool:
    if not _OTEL_AVAILABLE:
        return False
    return _RECORDER is not None


def env_says_eval_mode() -> bool:
    return os.environ.get("CORSIA_EVAL_MODE", "").strip().lower() in ("1", "true", "yes")



def current_trace_id_hex() -> str | None:
    if not _OTEL_AVAILABLE:
        return None
    span = _otel_trace.get_current_span()
    if span is None:
        return None
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return None
    return format_trace_id(ctx.trace_id)


def set_attrs(span: Any, attrs: dict[str, Any]) -> None:
    if span is None:
        return
    try:
        for k, v in attrs.items():
            span.set_attribute(k, _coerce(v))
    except Exception as e:
        from loguru import logger
        logger.warning(f"set_attrs failed for span {getattr(span, 'name', '?')}: {e}")
        return None


def _coerce(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, (str, bool, int, float)):
        return v
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return str(v)



class _NoOpSpan:
    def set_attribute(self, *_args, **_kwargs):
        return None

    def add_event(self, *_args, **_kwargs):
        return None

    def set_status(self, *_args, **_kwargs):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _NoOpCtxManager:
    def __enter__(self):
        return _NoOpSpan()

    def __exit__(self, *_args):
        return False


class _NoOpShim:
    def start_as_current_span(self, *_args, **_kwargs):
        return _NoOpCtxManager()

    def start_span(self, *_args, **_kwargs):
        return _NoOpSpan()


__all__ = [
    "EvalSpanRecorder",
    "SPAN_TURN",
    "SPAN_CONTEXT_BUILD",
    "SPAN_LLM_CALL",
    "SPAN_TOOL_LOOP",
    "SPAN_TOOL_CALL",
    "setup_tracing",
    "get_recorder",
    "get_tracer",
    "is_tracing_enabled",
    "current_trace_id_hex",
    "set_attrs",
    "env_says_eval_mode",
]
