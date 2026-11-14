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
from core.cost_identity import IDENTITY_GROUP, identity_columns, identity_join
from core.measurement_pricing import grouped_usage_per_row_sql, grouped_usage_sql
from tests.test_measurement_workflows import NOW, db, measured_owners, seed, usage  # noqa: F401  (fixtures)

#: Every module that reads its rows through ``fetch_all`` for this route.
_READERS = (
    "api.measurement_workflows", "api.measurement_workflow_threads", "api.measurement_model_cost",
    "core.workflow_builtin_operands", "core.workflow_verdict_read", "plans.run_end",
)
_SCOPES = (
    MeasurementScope("all", None), MeasurementScope("alpha", None),
    MeasurementScope("alpha", "root"), MeasurementScope("alpha", "billing"),
)


def _body(scope: MeasurementScope, grain_name: str, *, coverage_only: bool = False) -> str:
    from api.measurement_workflows import workflow_runs
    from infra.database import get_db

    connection = get_db()
    try:
        body = workflow_runs(connection, "steve", scope, resolve_grain(grain_name, "UTC", now=NOW), now=NOW,
                             coverage_only=coverage_only)
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


# The two usage reads as one grouping over every call, as they were before each call set was grouped on its own.
_ONE_GROUPING_MODELS = f"""
    SELECT node_id,{IDENTITY_GROUP},SUM(fresh) AS fresh FROM (
      SELECT u.plan_node_id AS node_id,{identity_columns("u")},
        COALESCE(u.prompt_tokens,0)+COALESCE(u.completion_tokens,0)+COALESCE(u.cache_creation_tokens,0) AS fresh
      FROM llm_usage u INDEXED BY idx_llm_usage_plan_node {identity_join("u")}
      JOIN plans p ON p.id=u.plan_id AND p.owner_kind='thread' AND p.owner_id=u.thread_id
      WHERE u.plan_node_id IN (SELECT value FROM json_each(:ids))
    ) GROUP BY node_id,{IDENTITY_GROUP}
"""


def _one_grouping_cost(chain_rows: str) -> str:
    return grouped_usage_sql(
        {"period": "x.period", "task_id": "x.task_id"},
        f"{chain_rows} x JOIN llm_usage u ON u.plan_node_id=x.node_id AND u.created_at<=x.settled_at",
    )


def _odd_usage() -> None:
    """Calls the plain seed lacks: a second manifest and none, a second model, a call after the run settled,
    and calls admission refuses (a fractional count, a missing bucket, a contract the source does not prove)."""
    from infra.database import get_db

    connection = get_db()
    try:
        usage(connection, "u-a-gather-2", "a-gather", "2026-10-07T09:21:00Z", prompt=7, manifest="m-other")
        usage(connection, "u-a-gather-3", "a-gather", "2026-10-07T09:22:00Z", prompt=11, manifest=None)
        usage(connection, "u-a-gather-late", "a-gather", "2026-10-09T09:00:00Z", prompt=13)
        usage(connection, "u-c-gather-2", "c-gather", "2026-10-15T09:16:00Z", prompt=17,
              model="codex/gpt-5.6-terra")
        usage(connection, "u-c-gather-3", "c-gather", "2026-10-15T09:17:00Z", prompt=19)
        usage(connection, "u-b-gather-real", "b-gather", "2026-10-14T09:21:00Z", prompt=23)
        connection.execute("UPDATE llm_usage SET prompt_tokens=0.1, total_tokens=0.1 WHERE id='u-b-gather-real'")
        usage(connection, "u-b-gather-null", "b-gather", "2026-10-14T09:22:00Z", prompt=29)
        connection.execute("UPDATE llm_usage SET completion_tokens=NULL WHERE id='u-b-gather-null'")
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize("read", ["days", "weeks", "coverage-days", "coverage-weeks"])
@pytest.mark.parametrize("scope", _SCOPES, ids=lambda scope: f"{scope.project}-{scope.agent}")
def test_the_route_answers_the_same_grouping_each_call_set_alone(db, monkeypatch, scope, read) -> None:  # noqa: F811
    """Grouping each task's calls on their own, then their few groups, answers what one grouping of every call did."""
    import api.measurement_model_cost as model_cost
    import core.workflow_verdict_read as verdict_read

    seed()
    _odd_usage()
    grain_name, coverage_only = read.removeprefix("coverage-"), read.startswith("coverage-")
    per_set = _body(scope, grain_name, coverage_only=coverage_only)
    monkeypatch.setattr(model_cost, "_cost_sql", _one_grouping_cost)
    monkeypatch.setattr(verdict_read, "MODEL_GROUPS_SQL", _ONE_GROUPING_MODELS)
    assert _body(scope, grain_name, coverage_only=coverage_only) == per_set
    assert json.loads(per_set)["coverage"]


def test_grouping_each_row_alone_returns_the_rows_and_order_of_one_grouping(db) -> None:  # noqa: F811
    """Row for row and in order, including a row read twice, a row with no calls and refused admissions."""
    seed()
    _odd_usage()
    rows = ("(SELECT 'k1' AS key,'a-gather' AS node_id,'2026-10-08T00:00:00Z' AS settled_at"
            " UNION ALL SELECT 'k1','a-gather','2026-10-08T00:00:00Z'"
            " UNION ALL SELECT 'k1','c-gather','2026-10-16T00:00:00Z'"
            " UNION ALL SELECT 'k2','b-gather','2026-10-15T00:00:00Z'"
            " UNION ALL SELECT 'k2','d-report','2026-10-17T00:00:00Z'"
            " UNION ALL SELECT 'k3','plain-1','2026-10-17T00:00:00Z')")
    calls = "u.plan_node_id=x.node_id AND u.created_at<=x.settled_at"
    keys = {"key": "x.key"}
    expected = [tuple(row) for row in db.execute(grouped_usage_sql(keys, f"{rows} x JOIN llm_usage u ON {calls}"))]
    actual = [tuple(row) for row in db.execute(grouped_usage_per_row_sql(keys, f"{rows} x", calls))]
    assert actual == expected
    assert {row[0] for row in expected} == {"k1", "k2"}
    assert {row[5] for row in expected} >= {"ok", "tokens_unreported"}, "admitted and refused calls both"
