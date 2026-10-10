"""Biggest changes: what moved most in the latest window against the one before, worse and improved.

The rules are fixed so every install reads the same: both windows need 20 outcomes, a
move counts from 10 percent, the worse list leads with the largest, each measure has
one fixed action line, and an agent view compares that agent alone.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from api.measurement_where_to_look import ACTIONS_BY_MEASURE, MEASURES, build_where_to_look
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


def _build(*rows: dict, windows: dict | None = None, comparable=lambda series, period: True) -> dict:
    return build_where_to_look(SCOPE, windows or {}, {"rows": list(rows)}, comparable, GRAIN)


def _ids(rows: list[dict]) -> list[tuple[str, str]]:
    return [(row["subject"]["name"], row["measure"]) for row in rows]


def test_the_largest_worse_move_leads_and_improvements_are_listed_apart():
    result = _build(
        _agent("a", "Alpha", first_try_acceptance=(0.90, 30, 0.60, 30)),
        _agent("b", "Beta", first_try_acceptance=(0.50, 30, 0.80, 30), asks_per_outcome=(0.10, 25, 0.30, 25)),
        _agent("c", "Gamma", fresh_tokens_per_outcome=(100.0, 40, 130.0, 40)),
        windows=_windows("first_try_acceptance", 0.7, 0.75),
    )
    assert _ids(result["worse"]) == [
        ("Beta", "asks_per_outcome"), ("Alpha", "first_try_acceptance"), ("Gamma", "fresh_tokens_per_outcome")]
    assert _ids(result["improved"]) == [("Beta", "first_try_acceptance")]
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
        _agent("a", "Alpha", active_hours_per_outcome=(0.10, 30, 0.05, 30)),
        windows=_windows("active_hours_per_outcome", 0.04, 0.04),
    )
    (better,) = result["improved"]
    assert (better["measure"], round(better["previous"], 6), round(better["latest"], 6), round(better["overall"], 6)) == (
        "minutes_per_outcome", 6.0, 3.0, 2.4)


def test_a_move_under_ten_percent_or_under_the_minimum_n_in_either_window_is_not_a_row():
    result = _build(
        _agent("a", "Small", first_try_acceptance=(0.80, 30, 0.75, 30)),
        _agent("b", "ThinLatest", first_try_acceptance=(0.90, 30, 0.40, 19)),
        _agent("c", "ThinBefore", first_try_acceptance=(0.90, 19, 0.40, 30)),
        _agent("d", "Unmeasured", asks_per_outcome=(None, 0, 0.5, 30)),
        _agent("e", "FromZero", asks_per_outcome=(0.0, 30, 0.5, 30)),
        windows=_windows("first_try_acceptance", 0.9, 0.4, n=19),
    )
    assert result["worse"] == [] and result["improved"] == []


def test_a_measure_across_a_definition_break_is_never_compared():
    result = _build(
        _agent("a", "Alpha", asks_per_outcome=(0.1, 30, 0.5, 30)),
        windows=_windows("asks_per_outcome", 0.1, 0.5),
        comparable=lambda series, first: first != GRAIN.previous.first,
    )
    assert result["worse"] == []


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
    assert agent_firsts(result["worse"]) == ["worse"] and agent_firsts(result["improved"]) == ["better"]
    assert result["period"]["latest"] == {"from": "2026-10-19", "to": "2026-11-15"}
    scoped = await measurement_where_to_look(
        project="p1", agent="worse", grain="weeks", timezone_name="UTC", user={"id": "steve"})
    assert all(row["subject"]["id"] == "worse" for row in (*scoped["worse"], *scoped["improved"]))
