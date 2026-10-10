
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _setup_recorder():
    from eval.otel import setup_tracing
    return setup_tracing()


def test_context_build_span_emitted_with_required_attrs(tmp_path):
    from opentelemetry import trace
    from core.context_builder import ContextBuilder

    recorder = _setup_recorder()
    assert recorder is not None, "OTel must be available for this test"

    tracer = trace.get_tracer("corsia.test")
    builder = ContextBuilder(workspace=tmp_path)

    with tracer.start_as_current_span("test_root") as root_span:
        msgs = builder.build_messages(
            history=[
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ],
            current_message="what's the weather?",
        )
        trace_id = root_span.get_span_context().trace_id

    assert len(msgs) == 4  # system + 2 history + 1 current

    spans = recorder.peek(trace_id)
    span_names = [s["name"] for s in spans]
    assert "corsia.context_build" in span_names

    cb_span = next(s for s in spans if s["name"] == "corsia.context_build")
    attrs = cb_span["attrs"]

    assert "context.prompt_tokens" in attrs
    assert "context.kb_recent_count" in attrs
    assert "context.system_prompt_size" in attrs
    assert "context.messages_count" in attrs

    assert attrs["context.messages_count"] == 4

    assert isinstance(attrs["context.system_prompt_size"], int)
    assert attrs["context.system_prompt_size"] > 0

    assert attrs["context.prompt_tokens"] >= 1


def test_context_build_span_reports_no_automatic_kb_entries(tmp_path):
    from opentelemetry import trace
    from core.context_builder import ContextBuilder

    recorder = _setup_recorder()

    tracer = trace.get_tracer("corsia.test")
    builder = ContextBuilder(workspace=tmp_path)

    with tracer.start_as_current_span("test_root") as root_span:
        builder.build_messages(
            history=[],
            current_message="hello",
        )
        trace_id = root_span.get_span_context().trace_id

    cb_span = next(
        s for s in recorder.peek(trace_id) if s["name"] == "corsia.context_build"
    )
    assert cb_span["attrs"]["context.kb_recent_count"] == 0
