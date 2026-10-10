"""The Agents surface's consumed contract, proved against what the route actually serves.

The frontend guard is only as good as its vocabulary. These proofs run the served route and
pin that every part of the answer the surface indexes by name is there with the shape it
assumes, and that an agent with too little history is served a thin rule rather than a
missing one — so a new agent degrades into a dash, never into a false verdict.

Seeding and the route call reuse this suite's own helpers, so the scenario cannot drift from
the schema or from the way the surface asks.
"""

from __future__ import annotations

import pytest

from test_measurement_agents import _agents, _frame, _seed_agent, _seed_project, _seed_period

# Every top-level key the Agents surface consumes, copied from the store's decode contract.
SERVED_KEYS = {
    "caption",
    "definition_version",
    "findings",
    "findings_state",
    "inactive_projects",
    "metric",
    "project_summaries",
    "period",
    "rows",
    "scope",
    "sort",
}

# The four columns the table indexes by id, each read as row.metrics[<id>].
TABLE_COLUMNS = {"outcomes", "fresh_tokens_per_outcome", "first_try_acceptance", "asks_per_outcome"}

# Every per-row key the surface dereferences.
SERVED_ROW_KEYS = {
    "agent",
    "n",
    "difficulty_mix",
    "status",
    "rank",
    "metrics",
    "selected_metric",
    "worsening",
    "flags",
}


@pytest.mark.asyncio
async def test_the_route_serves_every_key_the_surface_consumes(isolated_db):
    """A served key missing or renamed fails here, before the surface could hide it behind a default."""

    def seed(db):
        _seed_project(db, "p1")
        for name in ("alpha", "beta"):
            _seed_agent(db, name, project_id="p1")
            for index, period in enumerate(_frame(9)[:8]):
                _seed_period(db, name, period, 20 + index)

    body = await _agents(seed)
    assert body["rows"], "the scenario served no rows, so nothing below is proved"

    assert set(body) == SERVED_KEYS, (
        f"route serves {sorted(set(body))}, surface consumes {sorted(SERVED_KEYS)}"
    )
    for row in body["rows"]:
        missing = SERVED_ROW_KEYS - set(row)
        assert not missing, f"row {row['agent']['id']} serves no {sorted(missing)}"
        assert row["status"] in {"eligible", "too_few"}, row["status"]
        assert row["rank"] is None or (isinstance(row["rank"], int) and row["rank"] >= 1), row["rank"]
        metrics = row["metrics"]
        assert TABLE_COLUMNS <= set(metrics), f"row serves {sorted(metrics)}"
        for column in TABLE_COLUMNS:
            cell = metrics[column]
            assert {"value", "n", "minimum_n", "periods"} <= set(cell), (column, sorted(cell))
            assert cell["value"] is None or isinstance(cell["value"], (int, float)), (column, cell["value"])
            for point in cell["periods"]:
                assert {"period", "value", "n", "minimum_n"} <= set(point), point
                assert point["value"] is None or isinstance(point["value"], (int, float)), point
        for name in ("worsening", *row["flags"]):
            rule = row["worsening"] if name == "worsening" else row["flags"][name]
            assert {"eligible", "metric", "minimum_n"} <= set(rule), (name, sorted(rule))
            assert isinstance(rule["eligible"], bool), (name, rule["eligible"])
            if rule["eligible"]:
                # An eligible rule claims a comparison happened; the row marker reads `flagged`.
                assert isinstance(rule.get("flagged"), bool), (name, sorted(rule))


@pytest.mark.asyncio
async def test_an_agent_that_was_never_compared_is_served_a_thin_rule_not_a_missing_one(isolated_db):
    """The population case the surface reads `eligible` from.

    An agent with one settled period is on the table and comparable on nothing. Each rule must
    still arrive and say why it judged nothing, so an unread rule cannot become a verdict:
    the shape here is the honest answer, not an absence the reader would have to invent.
    """

    def seed(db):
        _seed_project(db, "p1")
        _seed_agent(db, "newcomer", project_id="p1")
        _seed_period(db, "newcomer", _frame(9)[-1], 4)

    body = await _agents(seed)
    row = next(entry for entry in body["rows"] if entry["agent"]["id"] == "newcomer")

    assert row["status"] == "too_few", row["status"]
    assert row["rank"] is None, f"an ineligible agent was given a rank: {row['rank']}"
    for name in ("worsening", *row["flags"]):
        rule = row["worsening"] if name == "worsening" else row["flags"][name]
        assert rule is not None, f"{name} went absent for a never-compared agent"
        assert {"eligible", "metric", "minimum_n", "reason"} <= set(rule), (name, sorted(rule))
        assert rule["eligible"] is False, (name, rule)
        assert "flagged" not in rule, f"{name} answers a comparison that did not happen: {rule}"


def test_the_surfaces_key_list_is_the_handlers_return_not_a_remembered_one():
    """Both sides name the same contract, so a list cannot drift from the handler in silence.

    Reading the handler's own syntax tree is the only way to compare the two vocabularies
    without importing the frontend: the list above is what the surface indexes, and this says
    whether the route really returns those keys.
    """
    import ast
    import inspect

    from api import measurement

    # The route validates and resolves its grain, then returns this body; Where to look reads the same one.
    handler = ast.parse(inspect.getsource(measurement.agents_payload))
    returned = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
    ]
    assert len(returned) == 1, f"expected one dict return in the handler, found {len(returned)}"
    served = {key.value for key in returned[0].value.keys if isinstance(key, ast.Constant)}
    assert served == SERVED_KEYS, (
        f"handler returns {sorted(served)}, surface consumes {sorted(SERVED_KEYS)}"
    )
