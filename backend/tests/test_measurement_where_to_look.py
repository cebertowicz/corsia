"""Biggest changes: what moved most in the latest window against the one before, worse and improved.

The rules are fixed so every install reads the same: both windows need 20 outcomes, a
move counts from 10 percent, the worse list leads with the largest, each measure has
one fixed action line, and an agent view compares that agent alone.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from api.measurement_where_to_look import ACTIONS_BY_MEASURE, MEASURES, SERIES_IDS, build_where_to_look
from core.measurement_scope import resolve_grain
from test_measurement_agents import (
    POST_BREAK, _freeze, _seed_agent, _seed_project, _seed_period, _seeded,
)

# Weeks in UTC on Monday 2026-11-16: the latest window is Oct 19 to Nov 16, the previous Sep 21 to Oct 19.
GRAIN = resolve_grain("weeks", "UTC", now=POST_BREAK)
LATEST, BEFORE = "2026-11-09", "2026-10-05"
SCOPE = {"id": "p1", "name": "Project One", "kind": "scope"}


def _agent(agent_id: str, name: str, **measures: tuple[float | None, int, float | None, int]) -> dict:
    """series -> (previous value, previous n, latest value, latest n)."""
    return {"agent": {"id": agent_id, "name": name}, "metrics": {
        series: {"previous_value": before, "previous_n": before_n, "value": after, "n": after_n}
        for series, (before, before_n, after, after_n) in measures.items()}}


def _windows(series: str, before: float | None, after: float | None, n: int = 30) -> dict:
    """One ``/series`` windows entry, keyed by its series id."""
    return {series: {"previous": {"value": before, "n": n}, "latest": {"value": after, "n": n}}}


def _build(*rows: dict, windows: dict | None = None, comparable=lambda series, period: True,
           overall: dict | None = None, models: dict | None = None, cost: dict | None = None) -> dict:
    return build_where_to_look(SCOPE, windows or {}, {"rows": list(rows), "overall": overall or {}}, comparable, GRAIN, models, cost)


def _cost(before: float | None, after: float | None, n: int = 30, n_before: int | None = None) -> dict:
    """The ``/cost`` payload's pooled windows: ``per_hour`` is its headline, ``sized`` the outcomes behind it."""
    return {"previous": {"per_hour": before, "sized": n if n_before is None else n_before},
            "latest": {"per_hour": after, "sized": n}}


def _cell(size: str, tasks: int, before: float | None, after: float) -> dict:
    """One Models size cell as the Models table serves it; ``before`` None is a class new this window."""
    return {"size": size, "tasks": tasks, "cost_per_task": after, "previous_cost_per_task": before}


def _model(label: str, *cells: dict, other: bool = False) -> dict:
    return {"model": "" if other else label.lower(), "label": label, "sizes": list(cells), **({"other": True} if other else {})}


def _ids(rows: list[dict]) -> list[tuple[str, str]]:
    return [(row["subject"]["name"], row["measure"]) for row in rows]


def test_the_largest_worse_move_leads_and_improvements_are_listed_apart():
    result = _build(
        _agent("a", "Alpha", first_try_acceptance=(0.90, 30, 0.60, 30)),
        _agent("b", "Beta", first_try_acceptance=(0.50, 30, 0.80, 30), active_hours_per_outcome=(0.10, 25, 0.30, 25)),
        windows=_windows("first_try_acceptance", 0.6, 0.75),
    )
    assert _ids(result["worse"]) == [("Beta", "minutes_per_outcome"), ("Alpha", "first_try_acceptance")]
    # Improved is the scope's own page measures: Beta's better first try is an agent row, which only the worse list carries.
    assert _ids(result["improved"]) == [("Project One", "first_try_acceptance")]
    first = result["worse"][1]
    assert (first["previous"], first["latest"], first["n"], first["overall"]) == (0.90, 0.60, 30, 0.75)
    assert round(first["change"], 4) == -0.3333
    assert first["action"] == ACTIONS_BY_MEASURE["first_try_acceptance"]
    assert (result["period"], result["minimum_n"]) == (GRAIN.describe(), 20)


def test_the_scope_itself_is_a_subject_and_minutes_are_converted_from_hours():
    result = _build(windows={
        **_windows("active_hours_per_outcome", 0.10, 0.03),
        **_windows("estimated_human_hours_per_active_hour", 30.0, 92.0),
        **_windows("asks_per_outcome", 0.1, 0.5),
    })
    assert _ids(result["improved"]) == [("Project One", "hours_per_hour"), ("Project One", "minutes_per_outcome")]
    minutes = result["improved"][1]
    assert (round(minutes["previous"], 6), round(minutes["latest"], 6)) == (6.0, 1.8)
    assert _ids(result["worse"]) == [("Project One", "asks_per_outcome")]
    assert result["worse"][0]["overall"] is None


def test_an_agent_row_carries_minutes_converted_from_hours_with_the_scope_figure_as_overall():
    result = _build(
        _agent("a", "Alpha", active_hours_per_outcome=(0.05, 30, 0.10, 30)),
        windows=_windows("active_hours_per_outcome", 0.04, 0.04),
    )
    (worse,) = result["worse"]
    assert (worse["measure"], round(worse["previous"], 6), round(worse["latest"], 6), round(worse["overall"], 6)) == (
        "minutes_per_outcome", 3.0, 6.0, 2.4)


def test_a_move_under_ten_percent_or_under_the_minimum_n_in_either_window_is_not_a_row():
    result = _build(
        _agent("a", "Small", first_try_acceptance=(0.80, 30, 0.75, 30)),
        _agent("b", "ThinLatest", first_try_acceptance=(0.90, 30, 0.40, 19)),
        _agent("c", "ThinBefore", first_try_acceptance=(0.90, 19, 0.40, 30)),
        _agent("d", "Unmeasured", active_hours_per_outcome=(None, 0, 0.5, 30)),
        _agent("e", "FromZero", active_hours_per_outcome=(0.0, 30, 0.5, 30)),
        windows=_windows("first_try_acceptance", 0.9, 0.4, n=19),
    )
    assert result["worse"] == [] and result["improved"] == []


def test_a_measure_across_a_definition_break_is_never_compared():
    result = _build(
        _agent("a", "Alpha", active_hours_per_outcome=(0.1, 30, 0.5, 30)),
        windows=_windows("asks_per_outcome", 0.1, 0.5),
        comparable=lambda series, first: first != GRAIN.previous.first,
    )
    assert result["worse"] == []


def test_an_agent_dollars_per_hour_row_reads_against_the_scope_pooled_figure():
    result = _build(
        _agent("a", "Alpha", dollars_per_work_hour=(1.0, 30, 1.5, 30)),
        _agent("b", "Beta", dollars_per_work_hour=(2.0, 30, 1.0, 30)),
        _agent("c", "Thin", dollars_per_work_hour=(1.0, 30, 3.0, 19)),
        overall={"dollars_per_work_hour": {"value": 1.2, "n": 80}},
    )
    (worse,) = result["worse"]
    assert (worse["subject"]["name"], worse["measure"], worse["label"], worse["format"]) == (
        "Alpha", "dollars_per_hour", "$ per person-hour of work delivered", "dollars")
    assert (worse["previous"], worse["latest"], worse["n"], worse["overall"]) == (1.0, 1.5, 30, 1.2)
    assert result["improved"] == []
    # Neither dollars measure is a /series id, so the series read is never asked for one.
    assert not {"dollars_per_work_hour", "cost_per_task"} & set(SERIES_IDS)


def test_a_model_row_is_its_biggest_size_class_priced_in_both_windows_against_that_class_pooled():
    result = _build(models={
        "size_overall": [{"size": "S", "cost_per_task": 2.0}, {"size": "M", "cost_per_task": 8.0}],
        "rows": [
            # M has more tasks than S, so M is the class read; L is new this window and is skipped.
            _model("Opus", _cell("S", 20, 2.0, 3.0), _cell("M", 40, 8.0, 12.0), _cell("L", 90, None, 30.0)),
            _model("Mid", _cell("S", 20, 4.0, 2.0)),
            _model("Flat", _cell("S", 20, 2.0, 2.1)),
            _model("New", _cell("S", 20, None, 2.0)),
            _model("Other (3 more models)", _cell("S", 50, 1.0, 9.0), other=True),
        ],
    })
    (worse,) = result["worse"]
    assert (worse["subject"], worse["measure"], worse["label"], worse["format"]) == (
        {"id": "opus", "name": "Opus", "kind": "model"}, "cost_per_task", "M $ per task", "dollars")
    assert (worse["previous"], worse["latest"], worse["n"], worse["overall"]) == (8.0, 12.0, 40, 8.0)
    assert worse["action"] == ACTIONS_BY_MEASURE["cost_per_task"]
    # Mid got cheaper, and a model's better move is not listed: Improved is the page's own measures.
    assert result["improved"] == []
    # A model's failed turns are not a Biggest changes measure.
    assert not any("failed" in row["measure"] for row in (*result["worse"], *result["improved"]))


def test_a_model_cost_row_is_not_compared_across_a_definition_break():
    result = _build(
        models={"size_overall": [{"size": "S", "cost_per_task": 2.0}], "rows": [_model("Opus", _cell("S", 20, 2.0, 3.0))]},
        comparable=lambda series, first: first != GRAIN.previous.first)
    assert result["worse"] == []


def test_a_subject_has_one_row_its_largest_worse_move():
    result = _build(
        _agent("a", "Alpha", active_hours_per_outcome=(0.10, 30, 0.12, 30), first_try_acceptance=(0.90, 30, 0.60, 30),
               dollars_per_work_hour=(1.0, 30, 1.2, 30)),
        _agent("b", "Beta", active_hours_per_outcome=(0.10, 30, 0.15, 30), dollars_per_work_hour=(1.0, 30, 1.3, 30)),
        models={"size_overall": [{"size": "S", "cost_per_task": 2.0}],
                "rows": [_model("Alpha", _cell("S", 20, 2.0, 3.0))]},
    )
    # Alpha's first try (-33%) beats its minutes (+20%) and dollars (+20%); Beta's minutes (+50%) beat its dollars (+30%);
    # a model that shares the name is its own subject.
    assert [(row["subject"]["kind"], row["subject"]["name"], row["measure"]) for row in result["worse"]] == [
        ("model", "Alpha", "cost_per_task"), ("agent", "Beta", "minutes_per_outcome"), ("agent", "Alpha", "first_try_acceptance")]


def test_the_scope_rows_are_not_one_per_subject_and_the_other_subjects_rows_leave_them_alone():
    result = _build(
        _agent("a", "Alpha", first_try_acceptance=(0.9, 30, 0.5, 30)),
        windows={**_windows("asks_per_outcome", 0.1, 0.5), **_windows("active_hours_per_outcome", 0.02, 0.04)},
    )
    assert sorted(_ids(result["worse"])) == [
        ("Alpha", "first_try_acceptance"), ("Project One", "asks_per_outcome"), ("Project One", "minutes_per_outcome")]


def test_agents_do_not_rank_asks_per_outcome_but_the_scope_keeps_it_as_a_page_measure():
    result = _build(
        _agent("a", "Alpha", asks_per_outcome=(0.1, 30, 0.5, 30), first_try_acceptance=(0.9, 30, 0.5, 30)),
        windows=_windows("asks_per_outcome", 0.1, 0.5),
    )
    assert sorted(_ids(result["worse"])) == [("Alpha", "first_try_acceptance"), ("Project One", "asks_per_outcome")]


def test_all_work_cost_per_hour_is_a_page_measure_worse_when_it_rises():
    result = _build(cost=_cost(1.0, 1.5))
    (worse,) = result["worse"]
    assert _ids([worse]) == [("Project One", "cost_per_hour")]
    assert (worse["previous"], worse["latest"], worse["n"]) == (1.0, 1.5, 30)
    assert worse["action"] == ACTIONS_BY_MEASURE["cost_per_hour"]
    assert _build(cost=_cost(1.5, 1.0))["improved"][0]["measure"] == "cost_per_hour"
    # It reads the served Cost value, so it is not a /series id and no agent or model is ranked on it.
    assert "cost_per_hour" not in SERIES_IDS and MEASURES["cost_per_hour"].per_agent is False


def test_cost_per_hour_needs_a_served_figure_twenty_outcomes_in_both_windows_a_ten_percent_move_and_a_comparable_period():
    assert _build(cost=_cost(None, 1.5))["worse"] == []
    assert _build(cost=_cost(1.0, None))["worse"] == []
    assert _build(cost=_cost(1.0, 1.5, n=19))["worse"] == []
    assert _build(cost=_cost(1.0, 1.5, n=30, n_before=19))["worse"] == []
    assert _build(cost=_cost(1.0, 1.05))["worse"] == []
    assert _build(cost=_cost(0.0, 1.5))["worse"] == []
    assert _build(cost=_cost(1.0, 1.5), comparable=lambda series, first: first != GRAIN.previous.first)["worse"] == []
    assert _build()["worse"] == []


def test_fresh_tokens_per_outcome_is_not_a_biggest_changes_measure():
    assert "fresh_tokens_per_outcome" not in MEASURES and "fresh_tokens_per_outcome" not in SERIES_IDS
    result = _build(
        _agent("c", "Gamma", fresh_tokens_per_outcome=(100.0, 40, 130.0, 40)),
        windows=_windows("fresh_tokens_per_outcome", 100.0, 60.0),
    )
    assert result["worse"] == [] and result["improved"] == []


def test_improved_lists_only_the_page_measures_of_the_scope():
    result = _build(
        _agent("a", "Alpha", active_hours_per_outcome=(0.10, 30, 0.05, 30), dollars_per_work_hour=(2.0, 30, 1.0, 30)),
        windows={**_windows("estimated_human_hours_per_active_hour", 30.0, 92.0), **_windows("asks_per_outcome", 0.5, 0.1)},
        models={"size_overall": [{"size": "S", "cost_per_task": 2.0}], "rows": [_model("Mid", _cell("S", 20, 4.0, 2.0))]},
    )
    assert result["worse"] == []
    assert sorted(_ids(result["improved"])) == [("Project One", "asks_per_outcome"), ("Project One", "hours_per_hour")]


def test_every_measure_has_one_fixed_action_and_no_internal_reference():
    assert set(ACTIONS_BY_MEASURE) == set(MEASURES)
    for action in ACTIONS_BY_MEASURE.values():
        assert action.endswith(".") and "#" not in action and not any(ch.isdigit() for ch in action)


def test_an_agent_view_ranks_only_that_agent_and_lists_no_agents():
    result = build_where_to_look(
        {"id": "z", "name": "Zed", "kind": "agent"}, _windows("asks_per_outcome", 0.1, 0.5), None,
        lambda series, period: True, GRAIN)
    assert _ids(result["worse"]) == [("Zed", "asks_per_outcome")] and result["improved"] == []


@pytest.mark.asyncio
async def test_the_served_route_ranks_a_seeded_scope(isolated_db, monkeypatch):
    from api.measurement_where_to_look import measurement_where_to_look

    _freeze(monkeypatch, POST_BREAK)

    def seed(db):
        _seed_project(db, "p1")
        for agent in ("worse", "better", "thin"):
            _seed_agent(db, agent, project_id="p1")
        _seed_period(db, "worse", BEFORE, 30, not_first_try=3)
        _seed_period(db, "worse", LATEST, 30, not_first_try=12)
        _seed_period(db, "better", BEFORE, 30, not_first_try=15)
        _seed_period(db, "better", LATEST, 30, not_first_try=3)
        _seed_period(db, "thin", BEFORE, 10, not_first_try=1)
        _seed_period(db, "thin", LATEST, 10, not_first_try=9)

    _seeded(seed)
    result = await measurement_where_to_look(
        project="p1", agent="none", grain="weeks", timezone_name="UTC", user={"id": "steve"})
    agent_firsts = lambda rows: [row["subject"]["id"] for row in rows
                                 if row["measure"] == "first_try_acceptance" and row["subject"]["kind"] == "agent"]
    assert agent_firsts(result["worse"]) == ["worse"] and agent_firsts(result["improved"]) == []
    assert result["period"]["latest"] == {"from": "2026-10-19", "to": "2026-11-15"}
    scoped = await measurement_where_to_look(
        project="p1", agent="worse", grain="weeks", timezone_name="UTC", user={"id": "steve"})
    assert all(row["subject"]["id"] == "worse" for row in (*scoped["worse"], *scoped["improved"]))


@pytest.mark.asyncio
async def test_the_daily_all_work_read_an_agent_consumes_keeps_its_fields(isolated_db, monkeypatch):
    """The ``attention-watch`` skill's own query: every project's agents, the Days grain, these fields."""
    from api.measurement_where_to_look import measurement_where_to_look

    _freeze(monkeypatch, POST_BREAK)

    def seed(db):
        # Monday Nov 16 in UTC: the latest 7 completed days are Nov 9-15, the previous Nov 2-8.
        for project, agent in (("p1", "one"), ("p2", "two")):
            _seed_project(db, project)
            _seed_agent(db, agent, project_id=project)
            _seed_period(db, agent, "2026-11-03", 30, not_first_try=3)
            _seed_period(db, agent, "2026-11-12", 30, not_first_try=12)

    _seeded(seed)
    result = await measurement_where_to_look(
        project="all", agent="none", grain="days", timezone_name="UTC", user={"id": "steve"})
    agents = [row for row in result["worse"] if row["subject"]["kind"] == "agent"]
    assert sorted(row["subject"]["id"] for row in agents) == ["one", "two"]
    for row in result["worse"]:
        assert {"subject", "label", "format", "previous", "latest", "n", "action", "worse"} <= set(row)
        assert set(row["subject"]) == {"kind", "id", "name"} and row["subject"]["kind"] in {"agent", "model", "scope"}
        assert row["worse"] is True and row["action"] in ACTIONS_BY_MEASURE.values()
        assert row["format"] in {"hours", "minutes", "ratio", "percent", "dollars"}
    first = next(row for row in agents if row["subject"]["id"] == "one")
    # ``percent`` is a fraction: 27 of 30 then 18 of 30 first try.
    assert (first["format"], first["previous"], first["latest"], first["n"]) == ("percent", 0.9, 0.6, 30)
    assert result["period"]["latest"] == {"from": "2026-11-09", "to": "2026-11-15"}
    assert result["period"]["previous"] == {"from": "2026-11-02", "to": "2026-11-08"}
