
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from yaml_scenario_solver_fixtures import (
    FIXTURE_TOKEN,
    _make_sample,
    _mock_transport,
    _ok_response,
)



class TestScenarioResultShape:

    def test_pass_status_is_string_literal(self):
        from eval.solvers.yaml_scenario import ScenarioResult, ScenarioStatus
        r = ScenarioResult(
            scenario_id="s",
            status=ScenarioStatus.PASS,
            scorer_outcomes={},
            endpoint_response={},
            errors=[],
            blocked_scorers=[],
            unknown_scorers=[],
        )
        assert r.status == "pass"
        assert r.scenario_id == "s"

    def test_status_enum_values(self):
        from eval.solvers.yaml_scenario import ScenarioStatus
        assert ScenarioStatus.PASS == "pass"
        assert ScenarioStatus.FAIL == "fail"
        assert ScenarioStatus.BLOCKED == "blocked"
        assert ScenarioStatus.ERROR == "error"

    def test_score_result_carries_pass_explanation(self):
        from eval.solvers.yaml_scenario import ScoreResult
        sr = ScoreResult(name="check_x", passed=True, explanation="all good")
        assert sr.passed is True
        assert sr.name == "check_x"
        assert sr.explanation == "all good"



class TestEndpointPlumbing:

    @pytest.mark.asyncio
    async def test_solver_posts_input_and_metadata(self):
        from eval.solvers.yaml_scenario import YamlScenarioSolver

        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(_ok_response()).encode("utf-8"),
            )

        solver = YamlScenarioSolver(
            endpoint="http://test/api/eval/turn",
            token=FIXTURE_TOKEN,
            transport=httpx.MockTransport(handler),
        )
        sample = _make_sample(
            sid="s1",
            inp="Reply with 'pong'.",
            metadata={"mode": "functional_behavior", "model": "x"},
        )

        await solver.run(sample)

        assert captured["method"] == "POST"
        assert "/api/eval/turn" in captured["url"]
        assert "token=t" in captured["url"]
        assert captured["body"]["input"] == "Reply with 'pong'."
        assert captured["body"]["metadata"]["mode"] == "functional_behavior"
        assert captured["body"]["metadata"]["model"] == "x"

    @pytest.mark.asyncio
    async def test_solver_captures_response_payload(self):
        from eval.solvers.yaml_scenario import YamlScenarioSolver
        payload = _ok_response(content="hello world", iteration_count=3)
        solver = YamlScenarioSolver(
            endpoint="http://test/api/eval/turn",
            token=FIXTURE_TOKEN,
            transport=_mock_transport(payload),
        )
        result = await solver.run(_make_sample(sid="s1"))

        assert result.endpoint_response["content"] == "hello world"
        assert result.endpoint_response["iteration_count"] == 3
        assert result.endpoint_response["trace_id"] == "a" * 32

    @pytest.mark.asyncio
    async def test_http_error_yields_error_status(self):
        from eval.solvers.yaml_scenario import YamlScenarioSolver
        solver = YamlScenarioSolver(
            endpoint="http://test/api/eval/turn",
            token=FIXTURE_TOKEN,
            transport=_mock_transport(status_code=500, response_payload={"detail": "boom"}),
        )
        result = await solver.run(_make_sample(sid="s1"))

        assert result.status == "error"
        assert any("500" in e or "HTTP" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_transport_exception_yields_error_status(self):
        from eval.solvers.yaml_scenario import YamlScenarioSolver
        solver = YamlScenarioSolver(
            endpoint="http://test/api/eval/turn",
            token=FIXTURE_TOKEN,
            transport=_mock_transport(raise_exc=httpx.ConnectError("connection refused")),
        )
        result = await solver.run(_make_sample(sid="s1"))

        assert result.status == "error"
        assert result.errors  # at least one error string

    @pytest.mark.asyncio
    async def test_missing_credential_stops_the_run(self, monkeypatch):
        """A setup error must not be recorded as a per-sample endpoint failure."""
        from eval.credentials import TOKEN_ENV
        from eval.solvers.yaml_scenario import YamlScenarioSolver

        monkeypatch.delenv(TOKEN_ENV, raising=False)
        solver = YamlScenarioSolver(
            endpoint="http://test/api/eval/turn",
            transport=_mock_transport(_ok_response()),
        )
        with pytest.raises(RuntimeError, match=TOKEN_ENV):
            await solver.run(_make_sample(sid="s1"))

    @pytest.mark.asyncio
    async def test_eval_endpoint_error_field_yields_error_status(self):
        from eval.solvers.yaml_scenario import YamlScenarioSolver
        payload = _ok_response(error="Eval turn timed out after 120s")
        solver = YamlScenarioSolver(
            endpoint="http://test/api/eval/turn",
            token=FIXTURE_TOKEN,
            transport=_mock_transport(payload),
        )
        result = await solver.run(_make_sample(sid="s1"))

        assert result.status == "error"
        assert any("timed out" in e for e in result.errors)



class TestDeterminism:

    @pytest.mark.asyncio
    async def test_same_scenario_same_result(self):
        from eval.solvers.yaml_scenario import YamlScenarioSolver
        solver = YamlScenarioSolver(
            endpoint="http://test/api/eval/turn",
            token=FIXTURE_TOKEN,
            transport=_mock_transport(_ok_response(content="pong")),
        )
        sample = _make_sample(sid="s1", metadata={"mode": "functional_behavior"})

        r1 = await solver.run(sample)
        r2 = await solver.run(sample)

        assert r1.status == r2.status
        assert r1.endpoint_response["content"] == r2.endpoint_response["content"]
        assert list(r1.scorer_outcomes.keys()) == list(r2.scorer_outcomes.keys())



_SPRINT1_SCENARIOS_ENV = "CORSIA_SPRINT1_SCENARIOS_DIR"
_SPRINT1_SCENARIOS_DIR_VALUE = os.environ.get(_SPRINT1_SCENARIOS_ENV)
_SPRINT1_SCENARIOS_DIR = (
    Path(_SPRINT1_SCENARIOS_DIR_VALUE) if _SPRINT1_SCENARIOS_DIR_VALUE else None
)
_SPRINT1_SCENARIOS_AVAILABLE = (
    _SPRINT1_SCENARIOS_DIR is not None and _SPRINT1_SCENARIOS_DIR.is_dir()
)
_SPRINT1_SCENARIO_FILES = (
    sorted(_SPRINT1_SCENARIOS_DIR.glob("*.yaml"))
    if _SPRINT1_SCENARIOS_AVAILABLE and _SPRINT1_SCENARIOS_DIR is not None
    else []
)


@pytest.mark.skipif(
    not _SPRINT1_SCENARIOS_AVAILABLE,
    reason=f"Set {_SPRINT1_SCENARIOS_ENV} to opt into historical Sprint-1 scenarios",
)
class TestSprint1Scenarios:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("yaml_path", _SPRINT1_SCENARIO_FILES)
    async def test_sprint1_scenario_loads_and_runs(self, yaml_path: Path):
        import yaml as yamllib
        from eval.datasets import from_yaml_scenario
        from eval.solvers.yaml_scenario import YamlScenarioSolver

        with yaml_path.open("r", encoding="utf-8") as f:
            scenario = yamllib.safe_load(f)

        sample = from_yaml_scenario(scenario)

        solver = YamlScenarioSolver(
            endpoint="http://test/api/eval/turn",
            token=FIXTURE_TOKEN,
            transport=_mock_transport(_ok_response()),
        )
        result = await solver.run(sample)

        assert result.status in ("pass", "fail", "blocked", "error"), (
            f"{yaml_path.name}: unexpected status {result.status}"
        )
        assert result.scenario_id == scenario["id"]



class TestPackageReexports:

    def test_corsia_turn_solver_still_importable(self):
        from eval.solvers import corsia_turn_solver
        assert callable(corsia_turn_solver)

    def test_null_tool_solver_still_importable(self):
        from eval.solvers import null_tool_solver
        assert callable(null_tool_solver)

    def test_yaml_scenario_solver_importable_from_package(self):
        from eval.solvers import YamlScenarioSolver
        assert YamlScenarioSolver is not None

    def test_scenario_result_importable_from_package(self):
        from eval.solvers import ScenarioResult
        assert ScenarioResult is not None
