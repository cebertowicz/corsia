from __future__ import annotations

import asyncio
import sys
from typing import Any
from unittest.mock import patch

import httpx

from eval.credentials import AUTH_QUERY_KEY as _AUTH_KEY, mint_disposable_token


def _patched_chat_completion_factory(reply_text: str = "pong"):
    from llm.router import LLMResponse

    async def fake_chat_completion(**kwargs: Any) -> LLMResponse:
        return LLMResponse(
            content=reply_text,
            tool_calls=[],
            finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
            reasoning_content=None,
        )

    return fake_chat_completion


def _check_null_probe_cleanup(client: Any, turn_url: str) -> None:
    """An eval tool must never survive in the production tool registry."""
    from infra import state

    agent = state._agent
    assert agent is None or not agent.tools.has("null_probe"), (
        "null_probe leaked into prod tool registry"
    )
    print("  OK: null_probe not leaked into tool registry")

    resp = client.post(
        turn_url,
        json={
            "input": "Reply ok.",
            "metadata": {
                "scenario": "smoke_null_probe_register",
                "mode": "latency_tool_loops",
                "tools_in_scope": ["null_probe"],
                "timeout_seconds": 30.0,
            },
        },
    )
    assert resp.status_code == 200, resp.text
    agent = state._agent
    assert agent is None or not agent.tools.has("null_probe"), (
        "null_probe leaked after eval call with tools_in_scope=['null_probe']"
    )
    print("  OK: null_probe registered transiently then cleaned up")


def _check_context_builder(client: Any, turn_url: str, fake_chat: Any) -> None:
    """AC#6: the prompt reaches the provider through the context builder."""
    captured_messages: list[Any] = []

    async def _spy_chat(**kwargs):
        captured_messages.append(list(kwargs.get("messages") or []))
        return await fake_chat(**kwargs)

    with patch("core.turn_provider_call.chat_completion", _spy_chat):
        resp = client.post(
            turn_url,
            json={
                "input": "MARKER_PROMPT_FOR_AC6",
                "metadata": {
                    "scenario": "smoke_ac6_context_builder",
                    "mode": "smoke",
                    "timeout_seconds": 30.0,
                },
            },
        )

    assert resp.status_code == 200, resp.text
    assert captured_messages, "chat_completion never invoked"
    last_msgs = captured_messages[-1]
    user_msgs = [m for m in last_msgs if m.get("role") == "user"]
    assert any("MARKER_PROMPT_FOR_AC6" in str(m) for m in user_msgs), (
        "user prompt never reached the LLM call — context-builder path bypassed"
    )
    system_msgs = [m for m in last_msgs if m.get("role") == "system"]
    assert system_msgs, "no system message — context-builder skipped"
    print(
        "  OK: AC#6 — context-builder ran (system msg + user marker present "
        f"in {len(last_msgs)} messages)"
    )


async def _run() -> int:
    from fastapi.testclient import TestClient

    fake_chat = _patched_chat_completion_factory(reply_text="pong-eval")

    with patch("core.turn_provider_call.chat_completion", fake_chat):
        from main import app  # noqa: PLC0415

        with TestClient(app) as client:
            # Provision a credential the way an operator would, against this
            # run's throwaway state.
            turn_url = (
                "/api/eval/turn"
                f"?{_AUTH_KEY}={mint_disposable_token('eval', label='integration')}"
            )

            resp = client.post(
                f"/api/eval/turn?{_AUTH_KEY}=bad-credential",
                json={"input": "hi", "metadata": {}},
            )
            assert resp.status_code == 401, (resp.status_code, resp.text)
            print("  OK: bad token → 401")

            resp = client.post(
                turn_url,
                json={
                    "input": "Reply with 'pong'.",
                    "metadata": {
                        "scenario": "smoke_happy_path",
                        "mode": "smoke",
                        "timeout_seconds": 30.0,
                    },
                },
            )
            assert resp.status_code == 200, (resp.status_code, resp.text)
            body = resp.json()
            assert body["content"] == "pong-eval", body
            print(f"  OK: happy path → content={body['content']!r}")

            tid = body.get("trace_id")
            assert isinstance(tid, str) and len(tid) == 32 and tid != "0" * 32, tid
            assert resp.headers.get("x-trace-id") == tid, resp.headers
            print(f"  OK: trace_id={tid[:8]}... carried in body + x-trace-id header")

            summary = body.get("span_summary") or {}
            spans = summary.get("spans") or []
            span_names = {s.get("name") for s in spans}
            for required in ("corsia.turn", "corsia.llm_call", "corsia.eval.request"):
                assert required in span_names, (required, span_names)
            print(f"  OK: span_summary has {len(spans)} spans: {sorted(span_names)}")

            ic = body.get("iteration_count")
            llm_call_count = sum(1 for s in spans if s.get("name") == "corsia.llm_call")
            assert ic == llm_call_count == 1, (ic, llm_call_count)
            print(f"  OK: iteration_count={ic} matches llm_call spans")

            assert body.get("ms_total", 0) > 0, body.get("ms_total")
            print(f"  OK: ms_total={body['ms_total']:.2f}ms > 0")

            _check_null_probe_cleanup(client, turn_url)
            _check_context_builder(client, turn_url, fake_chat)

            print()
            print("Integration smoke: 9/9 passed.")
            return 0

    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(asyncio.run(_run()))
