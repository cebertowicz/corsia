from __future__ import annotations

import pytest
from yaml_scenario_solver_fixtures import (
    FIXTURE_TOKEN,
    _make_sample,
    _mock_transport,
    _ok_response,
)


class TestScorerComposition:
    @pytest.mark.asyncio
    async def test_no_declared_scorers_yields_pass(self):
        from eval.solvers.yaml_scenario import YamlScenarioSolver
        solver = YamlScenarioSolver(
            endpoint="http://test/api/eval/turn",
            token=FIXTURE_TOKEN,
            transport=_mock_transport(_ok_response()),
        )
        sample = _make_sample(sid="s1", metadata={"mode": "functional_behavior"})
        result = await solver.run(sample)
        assert result.status == "pass"
        assert result.scorer_outcomes == {}

    @pytest.mark.asyncio
    async def test_unimplemented_scorer_yields_blocked(self):
        from eval.solvers.yaml_scenario import YamlScenarioSolver
        solver = YamlScenarioSolver(
            endpoint="http://test/api/eval/turn",
            token=FIXTURE_TOKEN,
            transport=_mock_transport(_ok_response()),
        )
        sample = _make_sample(
            sid="s1",
            metadata={
                "mode": "functional_behavior",
                "scorers": [{"name": "check_event_suppressed", "args": {"event": "x"}}],
            },
        )
        result = await solver.run(sample)
        assert result.status == "blocked"
        assert "check_event_suppressed" in result.blocked_scorers

    @pytest.mark.asyncio
    async def test_unknown_scorer_yields_blocked_with_unknown_list(self):
        from eval.solvers.yaml_scenario import YamlScenarioSolver
        solver = YamlScenarioSolver(
            endpoint="http://test/api/eval/turn",
            token=FIXTURE_TOKEN,
            transport=_mock_transport(_ok_response()),
        )
        sample = _make_sample(
            sid="s1",
            metadata={
                "mode": "functional_behavior",
                "scorers": ["nonexistent_scorer_xyz"],
            },
        )
        result = await solver.run(sample)
        assert result.status == "blocked"
        assert "nonexistent_scorer_xyz" in result.unknown_scorers

    @pytest.mark.asyncio
    async def test_passing_scorer_yields_pass(self):
        from eval.solvers.yaml_scenario import YamlScenarioSolver
        from eval.scorers.resolve import SCORER_REGISTRY
        from inspect_ai.scorer import CORRECT, Score

        def _always_pass_factory():
            async def _scorer(state, target):
                return Score(value=CORRECT, explanation="trivially passes")
            return _scorer

        SCORER_REGISTRY["__test_pass"] = _always_pass_factory
        try:
            solver = YamlScenarioSolver(
                endpoint="http://test/api/eval/turn",
                token=FIXTURE_TOKEN,
                transport=_mock_transport(_ok_response()),
            )
            sample = _make_sample(
                sid="s1",
                metadata={"mode": "functional_behavior", "scorers": ["__test_pass"]},
            )
            result = await solver.run(sample)
            assert result.status == "pass"
            assert result.scorer_outcomes["__test_pass"].passed is True
        finally:
            SCORER_REGISTRY.pop("__test_pass", None)

    @pytest.mark.asyncio
    async def test_failing_scorer_yields_fail(self):
        from eval.solvers.yaml_scenario import YamlScenarioSolver
        from eval.scorers.resolve import SCORER_REGISTRY
        from inspect_ai.scorer import INCORRECT, Score

        def _always_fail_factory():
            async def _scorer(state, target):
                return Score(value=INCORRECT, explanation="never passes")
            return _scorer

        SCORER_REGISTRY["__test_fail"] = _always_fail_factory
        try:
            solver = YamlScenarioSolver(
                endpoint="http://test/api/eval/turn",
                token=FIXTURE_TOKEN,
                transport=_mock_transport(_ok_response()),
            )
            sample = _make_sample(
                sid="s1",
                metadata={"mode": "functional_behavior", "scorers": ["__test_fail"]},
            )
            result = await solver.run(sample)
            assert result.status == "fail"
            assert result.scorer_outcomes["__test_fail"].passed is False
            assert "never passes" in result.scorer_outcomes["__test_fail"].explanation
        finally:
            SCORER_REGISTRY.pop("__test_fail", None)

    @pytest.mark.asyncio
    async def test_mixed_pass_fail_yields_fail(self):
        from eval.solvers.yaml_scenario import YamlScenarioSolver
        from eval.scorers.resolve import SCORER_REGISTRY
        from inspect_ai.scorer import CORRECT, INCORRECT, Score

        def _pass_factory():
            async def _s(state, target):
                return Score(value=CORRECT)
            return _s

        def _fail_factory():
            async def _s(state, target):
                return Score(value=INCORRECT)
            return _s

        SCORER_REGISTRY["__test_p"] = _pass_factory
        SCORER_REGISTRY["__test_f"] = _fail_factory
        try:
            solver = YamlScenarioSolver(
                endpoint="http://test/api/eval/turn",
                token=FIXTURE_TOKEN,
                transport=_mock_transport(_ok_response()),
            )
            sample = _make_sample(
                sid="s1",
                metadata={
                    "mode": "functional_behavior",
                    "scorers": ["__test_p", "__test_f"],
                },
            )
            result = await solver.run(sample)
            assert result.status == "fail"
            assert result.scorer_outcomes["__test_p"].passed is True
            assert result.scorer_outcomes["__test_f"].passed is False
        finally:
            SCORER_REGISTRY.pop("__test_p", None)
            SCORER_REGISTRY.pop("__test_f", None)

    @pytest.mark.asyncio
    async def test_blocked_takes_precedence_over_fail(self):
        from eval.solvers.yaml_scenario import YamlScenarioSolver
        from eval.scorers.resolve import SCORER_REGISTRY
        from inspect_ai.scorer import INCORRECT, Score

        def _fail_factory():
            async def _s(state, target):
                return Score(value=INCORRECT)
            return _s

        SCORER_REGISTRY["__test_f2"] = _fail_factory
        try:
            solver = YamlScenarioSolver(
                endpoint="http://test/api/eval/turn",
                token=FIXTURE_TOKEN,
                transport=_mock_transport(_ok_response()),
            )
            sample = _make_sample(
                sid="s1",
                metadata={
                    "mode": "functional_behavior",
                    "scorers": ["__test_f2", "check_event_suppressed"],
                },
            )
            result = await solver.run(sample)
            assert result.status == "blocked"
            assert "check_event_suppressed" in result.blocked_scorers
        finally:
            SCORER_REGISTRY.pop("__test_f2", None)
