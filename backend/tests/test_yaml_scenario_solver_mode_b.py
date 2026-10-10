
from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from yaml_scenario_solver_fixtures import FIXTURE_TOKEN  # noqa: E402


def _make_sample(
    *,
    sid: str = "test_scenario",
    inp: str = "",
    metadata: dict[str, Any] | None = None,
) -> Any:
    from inspect_ai.dataset import Sample
    return Sample(id=sid, input=inp, metadata=metadata or {})


def _llm_span_with(
    cached_tokens: int = 0,
    prefill_new_tokens: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    ttft_ms: float | None = None,
) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "llm.cached_tokens": cached_tokens,
        "llm.prefill_new_tokens": prefill_new_tokens,
        "llm.prompt_tokens": prompt_tokens,
        "llm.completion_tokens": completion_tokens,
    }
    if ttft_ms is not None:
        attrs["llm.ttft_ms"] = ttft_ms
    return {
        "name": "corsia.llm_call",
        "start_ns": 0,
        "end_ns": 1_000_000,
        "attrs": attrs,
        "status": "OK",
    }


def _ctx_span_with(system_prompt_hash: str, system_prompt_size: int = 1000) -> dict[str, Any]:
    return {
        "name": "corsia.context_build",
        "start_ns": 0,
        "end_ns": 1_000,
        "attrs": {
            "context.system_prompt_hash": system_prompt_hash,
            "context.system_prompt_size": system_prompt_size,
        },
        "status": "OK",
    }


def _response_with(
    *,
    content: str = "ok",
    cached_tokens: int = 0,
    prefill_new_tokens: int = 0,
    prompt_tokens: int = 0,
    ttft_ms: float | None = None,
    system_prompt_hash: str = "abc" * 21 + "a",  # 64-char hex-like
    model: str = "omlx/test-model",
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "content": content,
        "tool_calls": [],
        "iteration_count": 1,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 10,
            "cache_read_tokens": cached_tokens,
        },
        "trace_id": "x" * 32,
        "span_summary": {
            "spans": [
                _ctx_span_with(system_prompt_hash),
                _llm_span_with(
                    cached_tokens=cached_tokens,
                    prefill_new_tokens=prefill_new_tokens,
                    prompt_tokens=prompt_tokens,
                    ttft_ms=ttft_ms,
                ),
            ],
            "trace_id": "x" * 32,
        },
        "model": model,
        "ms_total": 50.0,
        "error": error,
    }


class _TurnHandler:

    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = list(responses)
        self.captured: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.captured.append(body)
        if not self.responses:
            payload = _response_with(error="ran out of mock responses")
        else:
            payload = self.responses.pop(0)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(payload).encode("utf-8"),
        )



class TestModeDetection:

    def test_mode_a_default_for_unmarked_scenario(self):
        from eval.solvers.yaml_scenario import _is_mode_b
        sample = _make_sample(metadata={"mode": "functional_behavior"})
        assert _is_mode_b(sample) is False

    def test_mode_b_when_mode_is_b(self):
        from eval.solvers.yaml_scenario import _is_mode_b
        sample = _make_sample(metadata={"mode": "B"})
        assert _is_mode_b(sample) is True

    def test_mode_b_when_mode_is_real_local(self):
        from eval.solvers.yaml_scenario import _is_mode_b
        sample = _make_sample(metadata={"mode": "real_local"})
        assert _is_mode_b(sample) is True

    def test_mode_b_case_insensitive(self):
        from eval.solvers.yaml_scenario import _is_mode_b
        for v in ("B", "b", "REAL_LOCAL", "Real_Local"):
            sample = _make_sample(metadata={"mode": v})
            assert _is_mode_b(sample) is True, f"failed for {v!r}"

    def test_mode_a_when_mock_llm_replies_fixture_present(self):
        from eval.solvers.yaml_scenario import _is_mode_b
        sample = _make_sample(metadata={
            "pre_conditions": [{"fixture": "mock_llm_replies", "args": {}}],
        })
        assert _is_mode_b(sample) is False

    def test_mode_b_inferred_from_mode_b_scorer(self):
        from eval.solvers.yaml_scenario import _is_mode_b
        sample = _make_sample(metadata={"scorers": ["hit_rate_per_turn"]})
        assert _is_mode_b(sample) is True

    def test_mode_b_inferred_from_top_level_turns(self):
        from eval.solvers.yaml_scenario import _is_mode_b
        sample = _make_sample(metadata={
            "turns": [
                {"role": "user", "content": "hi"},
                {"role": "user", "content": "again"},
            ],
        })
        assert _is_mode_b(sample) is True

    def test_mock_llm_replies_overrides_turns_signal(self):
        from eval.solvers.yaml_scenario import _is_mode_b
        sample = _make_sample(metadata={
            "pre_conditions": [{"fixture": "mock_llm_replies"}],
            "turns": [{"role": "user", "content": "hi"}],
        })
        assert _is_mode_b(sample) is False



class TestTurnInputs:

    def test_turns_field_at_top_level(self):
        from eval.solvers.yaml_scenario import _extract_turn_inputs
        sample = _make_sample(metadata={
            "turns": [
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "ignored"},
                {"role": "user", "content": "two"},
            ],
        })
        assert _extract_turn_inputs(sample) == ["one", "two"]

    def test_steps_user_send_actions(self):
        from eval.solvers.yaml_scenario import _extract_turn_inputs
        sample = _make_sample(metadata={
            "steps": [
                {"action": "user_send", "args": {"content": "alpha"}},
                {"action": "wait_for_event", "args": {"event": "turn_completed"}},
                {"action": "user_send", "args": {"content": "beta"}},
            ],
        })
        assert _extract_turn_inputs(sample) == ["alpha", "beta"]

    def test_no_turns_returns_empty(self):
        from eval.solvers.yaml_scenario import _extract_turn_inputs
        sample = _make_sample(metadata={})
        assert _extract_turn_inputs(sample) == []



class TestExtractSpanAttrs:

    def test_pulls_cached_tokens_from_llm_span(self):
        from eval.solvers.yaml_scenario import _extract_span_attrs
        resp = _response_with(cached_tokens=500, prefill_new_tokens=50)
        attrs = _extract_span_attrs(resp)
        assert attrs["cached_tokens"] == 500
        assert attrs["prefill_new_tokens"] == 50

    def test_pulls_system_prompt_hash_from_ctx_span(self):
        from eval.solvers.yaml_scenario import _extract_span_attrs
        resp = _response_with(system_prompt_hash="deadbeef" * 8)
        attrs = _extract_span_attrs(resp)
        assert attrs["system_prompt_hash"] == "deadbeef" * 8

    def test_falls_back_to_usage_block_when_no_spans(self):
        from eval.solvers.yaml_scenario import _extract_span_attrs
        resp = {
            "usage": {"prompt_tokens": 1000, "cache_read_tokens": 800, "completion_tokens": 10},
            "span_summary": {"spans": []},
            "trace_id": "z" * 32,
        }
        attrs = _extract_span_attrs(resp)
        assert attrs["cached_tokens_usage"] == 800
        assert attrs["prompt_tokens"] == 1000

    def test_handles_empty_response(self):
        from eval.solvers.yaml_scenario import _extract_span_attrs
        attrs = _extract_span_attrs({})
        assert isinstance(attrs, dict)



@pytest.mark.asyncio
class TestModeBSolver:

    async def test_three_turn_scenario_captures_per_turn_attrs(self):
        from eval.solvers.yaml_scenario import YamlScenarioSolver

        responses = [
            _response_with(cached_tokens=0, prefill_new_tokens=1000, prompt_tokens=1000),
            _response_with(cached_tokens=950, prefill_new_tokens=50, prompt_tokens=1000),
            _response_with(cached_tokens=970, prefill_new_tokens=30, prompt_tokens=1000),
        ]
        handler = _TurnHandler(responses)

        solver = YamlScenarioSolver(
            endpoint="http://test/api/eval/turn",
            token=FIXTURE_TOKEN,
            transport=httpx.MockTransport(handler),
        )
        sample = _make_sample(
            sid="cache-warm-baseline",
            metadata={
                "mode": "real_local",
                "turns": [
                    {"role": "user", "content": "one"},
                    {"role": "user", "content": "two"},
                    {"role": "user", "content": "three"},
                ],
            },
        )
        result = await solver.run(sample)

        assert result.mode == "B"
        assert len(result.turn_attrs) == 3
        assert result.turn_attrs[0]["cached_tokens"] == 0
        assert result.turn_attrs[1]["cached_tokens"] == 950
        assert result.turn_attrs[2]["cached_tokens"] == 970
        assert len(handler.captured) == 3
        assert handler.captured[0]["input"] == "one"
        assert handler.captured[1]["input"] == "two"
        assert handler.captured[2]["input"] == "three"
        assert handler.captured[0]["metadata"]["turn_index"] == 0
        assert handler.captured[2]["metadata"]["turn_index"] == 2

    async def test_metadata_model_propagated_from_setup(self):
        from eval.solvers.yaml_scenario import YamlScenarioSolver
        handler = _TurnHandler([_response_with()])
        solver = YamlScenarioSolver(
            endpoint="http://test/api/eval/turn",
            token=FIXTURE_TOKEN,
            transport=httpx.MockTransport(handler),
        )
        sample = _make_sample(
            sid="x",
            metadata={
                "mode": "B",
                "setup": {"model": "omlx/Qwen3.6-test"},
                "turns": [{"role": "user", "content": "hi"}],
            },
        )
        await solver.run(sample)
        assert handler.captured[0]["metadata"]["model"] == "omlx/Qwen3.6-test"

    async def test_transport_error_mid_conversation_yields_error_status(self):
        from eval.solvers.yaml_scenario import YamlScenarioSolver

        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    content=json.dumps(_response_with(cached_tokens=0)).encode("utf-8"),
                )
            raise httpx.ConnectError("connection refused")

        solver = YamlScenarioSolver(
            endpoint="http://test/api/eval/turn",
            token=FIXTURE_TOKEN,
            transport=httpx.MockTransport(handler),
        )
        sample = _make_sample(
            metadata={
                "mode": "B",
                "turns": [
                    {"role": "user", "content": "a"},
                    {"role": "user", "content": "b"},
                ],
            },
        )
        result = await solver.run(sample)
        assert result.status == "error"
        assert result.mode == "B"
        assert len(result.turn_responses) >= 1

    async def test_mode_b_scorer_receives_turn_attrs(self):
        from eval.solvers.yaml_scenario import YamlScenarioSolver
        from eval.scorers.resolve import SCORER_REGISTRY
        from inspect_ai.scorer import CORRECT, Score

        captured: dict[str, Any] = {}

        def _capturing_factory():
            async def _scorer(state, target):
                captured["turn_attrs"] = state.store.get("turn_attrs")
                return Score(value=CORRECT, explanation="ok")
            return _scorer

        SCORER_REGISTRY["__test_capture"] = _capturing_factory
        try:
            responses = [
                _response_with(cached_tokens=0, prefill_new_tokens=500),
                _response_with(cached_tokens=480, prefill_new_tokens=20),
            ]
            handler = _TurnHandler(responses)
            solver = YamlScenarioSolver(
                endpoint="http://test/api/eval/turn",
                token=FIXTURE_TOKEN,
                transport=httpx.MockTransport(handler),
            )
            sample = _make_sample(
                sid="x",
                metadata={
                    "mode": "B",
                    "turns": [
                        {"role": "user", "content": "a"},
                        {"role": "user", "content": "b"},
                    ],
                    "scorers": ["__test_capture"],
                },
            )
            result = await solver.run(sample)
            assert result.status == "pass"
            assert len(captured["turn_attrs"]) == 2
            assert captured["turn_attrs"][1]["cached_tokens"] == 480
        finally:
            SCORER_REGISTRY.pop("__test_capture", None)



@pytest.mark.asyncio
class TestModeABackwardCompat:

    async def test_mode_a_records_mode_a_field(self):
        from eval.solvers.yaml_scenario import YamlScenarioSolver

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(_response_with()).encode("utf-8"),
            )

        solver = YamlScenarioSolver(
            endpoint="http://test/api/eval/turn",
            token=FIXTURE_TOKEN,
            transport=httpx.MockTransport(handler),
        )
        sample = _make_sample(metadata={"mode": "functional_behavior"})
        result = await solver.run(sample)
        assert result.mode == "A"
        assert result.turn_attrs == []
