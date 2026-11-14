"""The Workflows read's big reads: one SQLite step each, and the same answer as an ordinary fetch.

A read returning thousands of rows through ``fetchall()`` re-takes the GIL once per row, so beside any busy
thread its wall time grows with the rows returned (``infra.packed_rows``). The route reads its rows through
``fetch_all``; these tests hold that to the response the plain fetch gives, for every scope and both grains, and
hold the usage read to the node index that production's missing statistics would not choose by themselves.
"""
from __future__ import annotations

import json
import sys

import pytest

from core.measurement_scope import MeasurementScope, resolve_grain
from tests.test_measurement_workflows import NOW, db, measured_owners, seed  # noqa: F401  (fixtures)

#: Every module that reads its rows through ``fetch_all`` for this route.
_READERS = (
    "api.measurement_workflows", "api.measurement_workflow_threads", "api.measurement_model_cost",
    "core.workflow_builtin_operands", "core.workflow_verdict_read", "plans.run_end",
)
_SCOPES = (
    MeasurementScope("all", None), MeasurementScope("alpha", None),
    MeasurementScope("alpha", "root"), MeasurementScope("alpha", "billing"),
)


def _body(scope: MeasurementScope, grain_name: str) -> str:
    from api.measurement_workflows import workflow_runs
    from infra.database import get_db

    connection = get_db()
    try:
        body = workflow_runs(connection, "steve", scope, resolve_grain(grain_name, "UTC", now=NOW), now=NOW)
    finally:
        connection.close()
    return json.dumps(body, sort_keys=True, default=str)


def _plain(db, sql, params=()):
    """The read as it was before: one step per row."""
    return db.execute(sql, params).fetchall()


@pytest.mark.parametrize("grain_name", ["days", "weeks"])
@pytest.mark.parametrize("scope", _SCOPES, ids=lambda scope: f"{scope.project}-{scope.agent}")
def test_the_route_answers_the_same_through_packed_and_plain_reads(db, monkeypatch, scope, grain_name) -> None:  # noqa: F811
    seed()
    packed = _body(scope, grain_name)
    for name in _READERS:
        monkeypatch.setattr(sys.modules[name], "fetch_all", _plain)
    assert _body(scope, grain_name) == packed
    assert json.loads(packed)["workflows"], "the seed has runs in every scope, so an empty answer proves nothing"


def test_every_listed_module_reads_through_fetch_all(db, monkeypatch) -> None:  # noqa: F811
    """Each module of the route reads through ``fetch_all``: with it stubbed out, the route sees its rows."""
    seed()
    seen: list[str] = []
    for name in _READERS:
        monkeypatch.setattr(sys.modules[name], "fetch_all",
                            lambda db, sql, params=(), _name=name: seen.append(_name) or _plain(db, sql, params))
    _body(MeasurementScope("all", None), "weeks")
    assert set(seen) == set(_READERS)


def test_the_model_read_uses_the_node_index_without_statistics(db) -> None:  # noqa: F811
    """No ANALYZE ever runs, so the planner would otherwise walk each owner's whole usage history."""
    from api.measurement_model_cost import MODEL_GROUPS_SQL

    plan = " ".join(str(row[-1]) for row in db.execute("EXPLAIN QUERY PLAN " + MODEL_GROUPS_SQL, {"ids": "[]"}))
    assert "idx_llm_usage_plan_node" in plan
    assert "idx_llm_usage_session" not in plan
