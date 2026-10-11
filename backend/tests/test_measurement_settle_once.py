"""One Measurement bulk job settles the scope's accepted outcomes once, however many readers it feeds."""

from __future__ import annotations

import sys

import pytest
from tests.plan_writer_authorizer import raw_plan_seed
from tests.test_measurement_api import _seed_outcomes, _seed_thread

# One job per panel: the size series, the agents table, cost, and models.
JOBS = {
    "size_series": "/api/measurement/series?ids=estimated_human_hours_per_active_hour&grain=weeks&timezone=UTC&project=all&agent=none",
    "agents": "/api/measurement/agents?project=all&agent=none&grain=weeks&timezone=UTC",
    "cost": "/api/measurement/cost?grain=weeks&timezone=UTC&project=all&agent=none",
    "models": "/api/measurement/models?grain=weeks&timezone=UTC&project=all&agent=none",
}


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api import measurement

    app = FastAPI()
    app.include_router(measurement.router)
    app.dependency_overrides[measurement.require_user] = lambda: {"id": "steve"}
    return TestClient(app)


def _seed() -> None:
    """Two agents' accepted work in the last completed week: one sent back, one sized, one done by the user."""
    from infra.database import get_db

    db = get_db()
    try:
        for thread in ("root", "peer"):
            _seed_thread(db, thread, agent=True)
            _seed_outcomes(db, thread, count=4)
        with raw_plan_seed(db):
            db.execute(
                "INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
                "VALUES ('rejected-1','node-1','test','review_rejected','review_requested','active','2026-09-23T11:00:00Z')"
            )
            db.execute("UPDATE plan_nodes SET size='M' WHERE id IN ('node-0','peer-node-0')")
            db.execute("UPDATE plan_nodes SET assignee='user' WHERE id='peer-node-3'")
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize("job", JOBS)
def test_a_job_settles_the_accepted_outcomes_once(isolated_db, measurement_clock, monkeypatch, job):
    from api import measurement_queries

    _seed()
    real, settlements = measurement_queries.outcome_ctes, []

    def counted(grain=None, **options):
        if options.get("limited_to_range", True):  # the all-history chain read is a different question
            settlements.append(job)
        return real(grain, **options)

    # A reader that imported the builder by name settles through its own reference.
    for module in list(sys.modules.values()):
        if getattr(module, "outcome_ctes", None) is real:
            monkeypatch.setattr(module, "outcome_ctes", counted)

    assert _client().get(JOBS[job]).status_code == 200
    assert len(settlements) == 1


@pytest.mark.parametrize("roots", [False, True])
def test_the_prepared_figures_are_the_ones_a_fresh_settlement_gives(isolated_db, measurement_clock, roots):
    """Everything derived from the one settlement equals what settling each time served before."""
    from api.measurement_queries import outcome_ctes, prepare_outcome_periods
    from api.measurement_size import _PREPARED_ALL_NODES_SQL, _classified, _newest_facts_sql, _nodes_sql, _sized_nodes
    from core.measurement_scope import query_params, resolve_grain, scope_cte
    from datetime import datetime, timezone
    from infra.database import get_db

    _seed()
    grain = resolve_grain("weeks", "UTC", now=datetime(2026, 9, 29, 12, tzinfo=timezone.utc))
    params = {"user_id": "steve", "scope_project": "all", "scope_agent": None, "grain": grain}
    bound = query_params(params)
    db = get_db()
    try:
        prefix = scope_cte() + outcome_ctes(grain)
        reference_periods = [tuple(row) for row in db.execute(
            prefix + "SELECT o.period,o.outcomes,o.first_try,COALESCE(r.rejections,0) "
            "FROM outcome_periods o LEFT JOIN rejection_periods r USING(period) ORDER BY o.period", bound).fetchall()]
        nodes_sql = _nodes_sql("o.id", "outcome_rows", "o.first_try")
        reference_nodes = _classified(db.execute(prefix + nodes_sql, bound).fetchall(),
                                      db.execute(prefix + ", " + _newest_facts_sql(nodes_sql), bound).fetchall())
        reference_roots = [tuple(row) for row in db.execute(
            prefix + "SELECT root_id,period,COUNT(*),SUM(first_try) FROM outcome_rows GROUP BY root_id,period "
            "ORDER BY root_id,period", bound).fetchall()]
        assert reference_periods and reference_periods[0][3] == 1 and len(reference_nodes) == 7

        prepare_outcome_periods(db, params, roots=roots)
        assert [tuple(row) for row in db.execute(
            "SELECT period,outcomes,first_try,rejections FROM measurement_outcome_periods ORDER BY period")] == reference_periods
        assert sorted(_sized_nodes(db, params)) == sorted(
            (node_id, period, int(first_try), status, attributes, size_class)
            for node_id, period, first_try, status, attributes, size_class in reference_nodes)
        assert db.execute(_PREPARED_ALL_NODES_SQL).fetchall()
        if roots:
            assert [tuple(row) for row in db.execute(
                "SELECT root_id,period,outcomes,first_try FROM measurement_root_outcome_periods ORDER BY root_id,period"
            )] == reference_roots
    finally:
        db.close()
