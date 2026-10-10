from __future__ import annotations

import json
from typing import Any

import httpx


# A solver reaches for its credential when it makes a request, so one has to be
# present even behind a mock transport that never checks it. Real values live in
# the account store and are minted per install; this is only a stand-in.
FIXTURE_TOKEN = "t"


def _make_sample(
    *,
    sid: str = "test_scenario",
    inp: str = "Reply with 'pong'.",
    metadata: dict[str, Any] | None = None,
) -> Any:
    from inspect_ai.dataset import Sample
    return Sample(id=sid, input=inp, metadata=metadata or {})


def _ok_response(
    *,
    content: str = "pong",
    tool_calls: list[dict[str, Any]] | None = None,
    iteration_count: int = 1,
    trace_id: str = "a" * 32,
    spans: list[dict[str, Any]] | None = None,
    model: str = "anthropic/claude-haiku-4-5",
    ms_total: float = 12.5,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "content": content,
        "tool_calls": tool_calls or [],
        "iteration_count": iteration_count,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "trace_id": trace_id,
        "span_summary": {"spans": spans or [], "trace_id": trace_id},
        "model": model,
        "ms_total": ms_total,
        "error": error,
    }


def _mock_transport(
    response_payload: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    raise_exc: Exception | None = None,
) -> httpx.MockTransport:
    payload = response_payload if response_payload is not None else _ok_response()

    def handler(request: httpx.Request) -> httpx.Response:
        if raise_exc is not None:
            raise raise_exc
        body = json.dumps(payload).encode("utf-8")
        return httpx.Response(
            status_code,
            headers={
                "content-type": "application/json",
                "x-trace-id": str(payload.get("trace_id", "")),
            },
            content=body,
        )

    return httpx.MockTransport(handler)
