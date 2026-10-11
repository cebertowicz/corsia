"""Models and Agents' dollars per hour read the accepted tasks' chains and their costs once per page load."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from api import measurement, measurement_chain_costs, measurement_model_cost, measurement_models
from api.measurement_join import _flights
from api.measurement_chain_costs import chain_costs, two_windows
from api.measurement_scope import resolve_grain
from core import measurement_scope
from model_identity_fixtures import seed_identity_catalog
from tests.plan_writer_authorizer import raw_plan_seed

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
SCOPE = {"scope_project": "all", "scope_agent": None, "user_id": "steve"}
USER = {"id": "steve"}
OPUS = "claude-code/claude-opus-5-5"


@pytest.fixture
def db(isolated_db, monkeypatch):
    from infra.database import get_db

    monkeypatch.setattr(measurement_scope, "_now", lambda: NOW)
    _flights.clear()
    connection = get_db()
    for thread in ("root", "other"):
        connection.execute("INSERT INTO threads (id,user_id,title,agent_enabled,agent_status,created_at,updated_at) "
                           "VALUES (?,'steve',?,1,'active','t','t')", (thread, thread))
    connection.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) "
                       "VALUES ('plan','thread','root','active',1,'t','t')")
    seed_identity_catalog(connection)
    try:
        yield connection
    finally:
        connection.close()


def _task(db, node: str, size: str | None, accepted: str = "2026-09-30T12:00:00Z") -> None:
    with raw_plan_seed(db):
        db.execute("INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,state,commitment_id,"
                   "requirements_revision,revision,size,created_at,updated_at) "
                   "VALUES (?, 'plan',NULL,1,'task','task','done','accepted',?,1,1,?,'t','t')", (node, node, size))
        db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
                   "VALUES (?,?,'test','accepted','active','accepted',?)", (f"accept-{node}", node, accepted))


def _usage(db, usage_id: str, node: str, model: str = OPUS, *, source: str = "external_harness:claude-code",
           at: str = "2026-09-29T12:00:00Z") -> None:
    db.execute("INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,cache_read_tokens,"
               "total_tokens,source,created_at,plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role) "
               "VALUES (?, 'root',?,1000000,0,0,0,1000000,?,?,'plan',?,'attempt',?,'execution')",
               (usage_id, model, source, at, node, node))


def _seed(db) -> None:
    _task(db, "m", "M")
    _task(db, "l", "L")
    _task(db, "before", "M", accepted="2026-08-19T12:00:00Z")
    _task(db, "unrated", None)
    _task(db, "mixed", "S")
    for node in ("m", "l", "unrated", "mixed"):
        _usage(db, f"u-{node}", node)
    _usage(db, "u-mixed-unproven", "mixed", "codex/gpt-5.6-terra", source="external_harness:codex")
    _usage(db, "u-before", "before", at="2026-08-18T12:00:00Z")
    db.commit()


def _count(monkeypatch) -> dict[str, int]:
    """How many chain walks, cost reads and shared gap reads ran."""
    runs = {"walks": 0, "cost_reads": 0, "gaps": 0}
    chain_ctes, cost_sql = measurement_model_cost.chain_ctes, measurement_model_cost._cost_sql
    gaps = measurement_chain_costs.query_recording_gaps

    def walk(**kwargs):
        runs["walks"] += 1
        return chain_ctes(**kwargs)

    def cost(chain_rows):
        runs["cost_reads"] += 1
        return cost_sql(chain_rows)

    def gap_read(*args, **kwargs):
        runs["gaps"] += 1
        return gaps(*args, **kwargs)

    monkeypatch.setattr(measurement_model_cost, "chain_ctes", walk)
    monkeypatch.setattr(measurement_model_cost, "_cost_sql", cost)
    monkeypatch.setattr(measurement_chain_costs, "query_recording_gaps", gap_read)
    return runs


async def _page(grain):
    """The page's Agents and Models requests in flight together, as one load sends them."""
    return await asyncio.gather(
        measurement.agents_payload("all", "none", grain, "ranked", "fresh_tokens_per_outcome", True, USER),
        measurement_models.models_read("all", "none", grain, USER))


async def test_one_page_load_shares_the_cost_walk_and_gaps(db, monkeypatch):
    _seed(db)
    runs = _count(monkeypatch)
    agents, models = await _page(resolve_grain("weeks", "UTC"))
    assert runs == {"walks": 1, "cost_reads": 1, "gaps": 1}
    assert agents["overall"]["dollars_per_work_hour"]["n"] > 0 and OPUS in {row["model"] for row in models["rows"]}


async def test_agents_alone_and_models_alone_each_read_what_they_need(db, monkeypatch):
    _seed(db)
    grain = resolve_grain("weeks", "UTC")
    runs = _count(monkeypatch)
    await measurement.agents_payload("all", "none", grain, "ranked", "fresh_tokens_per_outcome", True, USER)
    assert runs == {"walks": 1, "cost_reads": 1, "gaps": 1}
    _flights.clear()
    await measurement_models.models_read("all", "none", grain, USER)
    assert runs == {"walks": 2, "cost_reads": 2, "gaps": 2}


async def test_agents_without_dollars_reads_no_chain(db, monkeypatch):
    _seed(db)
    runs = _count(monkeypatch)
    await measurement.agents_payload("all", "none", resolve_grain("weeks", "UTC"), "ranked", "fresh_tokens_per_outcome", False, USER)
    assert runs == {"walks": 0, "cost_reads": 0, "gaps": 0}


async def test_the_served_agents_dollars_equal_the_shared_figures(db):
    _seed(db)
    grain = resolve_grain("weeks", "UTC")
    served = await measurement.agents_payload("all", "none", grain, "ranked", "fresh_tokens_per_outcome", True, USER)
    from api.measurement_agent_cost import dollars_metric, query_task_hour_cost
    from api.measurement_agents import RANKING_MINIMUM_N
    from api.measurement_queries import prepare_outcome_periods

    prepare_outcome_periods(db, {**SCOPE, "grain": grain}, roots=True)
    alone = dollars_metric(query_task_hour_cost(db, {**SCOPE, "grain": two_windows(grain)}, chain_costs(db, SCOPE, grain)["cost_rows"]),
                           None, grain, RANKING_MINIMUM_N)
    assert served["overall"]["dollars_per_work_hour"] == alone
