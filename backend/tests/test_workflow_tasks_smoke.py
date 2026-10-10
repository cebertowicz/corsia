
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))



def test_workflow_tasks_registry_has_three_tasks():
    from eval.workflow_tasks import WORKFLOW_TASKS
    assert set(WORKFLOW_TASKS) == {
        "corsia_full_session",
        "corsia_multi_turn_with_tools",
        "corsia_error_recovery",
    }
    for name, fn in WORKFLOW_TASKS.items():
        assert callable(fn), f"{name} is not callable"


@pytest.mark.parametrize("name", [
    "full_session", "multi_turn_with_tools", "error_recovery",
])
def test_scenario_yaml_loads(name):
    from eval.workflow_tasks._common import load_scenario
    scenario = load_scenario(name)
    assert isinstance(scenario, dict)
    assert scenario.get("id"), f"{name} missing id"
    assert scenario.get("mode") in ("real_local", "B")
    turns = scenario.get("turns")
    assert isinstance(turns, list) and len(turns) >= 1


@pytest.mark.parametrize("name", [
    "full_session", "multi_turn_with_tools", "error_recovery",
])
def test_rubric_loads_and_is_non_empty(name):
    from eval.workflow_tasks._common import load_rubric
    text = load_rubric(name)
    assert isinstance(text, str)
    assert len(text.strip()) > 100, f"{name} rubric too short"
    assert "score" in text.lower()


def test_missing_scenario_raises_clearly():
    from eval.workflow_tasks._common import load_scenario
    with pytest.raises(FileNotFoundError, match="workflow scenario not found"):
        load_scenario("does_not_exist_anywhere")


def test_missing_rubric_raises_clearly():
    from eval.workflow_tasks._common import load_rubric
    with pytest.raises(FileNotFoundError, match="workflow rubric not found"):
        load_rubric("does_not_exist_anywhere")



def test_full_session_task_constructs():
    from eval.workflow_tasks import corsia_full_session
    t = corsia_full_session(judge_model="oMLX/test-judge", threshold=0.6)
    assert getattr(t, "dataset", None) is not None
    assert getattr(t, "solver", None) is not None
    assert getattr(t, "scorer", None) is not None


def test_multi_turn_with_tools_task_constructs():
    from eval.workflow_tasks import corsia_multi_turn_with_tools
    t = corsia_multi_turn_with_tools()
    assert getattr(t, "dataset", None) is not None


def test_error_recovery_task_constructs():
    from eval.workflow_tasks import corsia_error_recovery
    t = corsia_error_recovery()
    assert getattr(t, "dataset", None) is not None



def test_runner_resolves_workflow_task_names():
    from eval.runner import _resolve_task
    fn = _resolve_task("corsia_full_session")
    assert callable(fn)
    fn2 = _resolve_task("corsia_error_recovery")
    assert callable(fn2)



def test_cli_resolve_workflow_task_names_all():
    from eval.cli import _resolve_workflow_task_names
    names = _resolve_workflow_task_names("all")
    assert set(names) == {
        "corsia_full_session",
        "corsia_multi_turn_with_tools",
        "corsia_error_recovery",
    }


def test_cli_resolve_workflow_task_names_csv():
    from eval.cli import _resolve_workflow_task_names
    names = _resolve_workflow_task_names(
        "corsia_full_session,corsia_error_recovery",
    )
    assert names == ["corsia_full_session", "corsia_error_recovery"]


def test_cli_resolve_workflow_task_names_rejects_unknown():
    from eval.cli import _resolve_workflow_task_names
    with pytest.raises(ValueError, match="unknown workflow task"):
        _resolve_workflow_task_names("not_a_real_task")



class TestCompare:

    def test_no_regression_when_head_equal_or_better(self):
        from eval.workflow_tasks.compare import compare
        regressed, rows = compare(
            base_scores={"a": 0.7, "b": 0.5},
            head_scores={"a": 0.72, "b": 0.5},
            drop_pct=5.0,
        )
        assert regressed is False
        assert all(not r["regression"] for r in rows)

    def test_regression_when_head_drops_more_than_threshold(self):
        from eval.workflow_tasks.compare import compare
        regressed, rows = compare(
            base_scores={"task_a": 0.8},
            head_scores={"task_a": 0.6},
            drop_pct=5.0,
        )
        assert regressed is True
        assert rows[0]["regression"] is True

    def test_small_drop_within_threshold_is_ok(self):
        from eval.workflow_tasks.compare import compare
        regressed, rows = compare(
            base_scores={"task_a": 0.80},
            head_scores={"task_a": 0.77},
            drop_pct=5.0,
        )
        assert regressed is False
        assert rows[0]["regression"] is False

    def test_missing_side_does_not_regress(self):
        from eval.workflow_tasks.compare import compare
        regressed, rows = compare(
            base_scores={"task_a": 0.8},
            head_scores={},
            drop_pct=5.0,
        )
        assert regressed is False
        assert rows[0]["base"] == 0.8
        assert rows[0]["head"] is None
        assert rows[0]["regression"] is False
