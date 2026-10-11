"""Per-agent dollars per hour of work, over the grain's latest and previous windows."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from api.cost_identity import IdentityResolver
from api.measurement_agent_cost import dollars_metric, query_task_hour_cost
from api.measurement_chain_costs import chain_costs
from api.measurement_agents import RANKING_MINIMUM_N
from api.measurement_models import two_windows
from api.measurement_model_cost import query_model_costs
from api.measurement_queries import prepare_outcome_periods
from core.measurement_scope import resolve_grain
from model_identity_fixtures import LOCAL_ID, seed_identity_catalog
from tests.plan_writer_authorizer import raw_plan_seed

# Weeks in UTC on Monday 2026-10-05: the latest window is Sep 7 to Oct 4, the previous Aug 10 to Sep 6.
GRAIN = resolve_grain("weeks", "UTC", now=datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
LATEST, PREVIOUS = "2026-09-28", "2026-08-17"
SCOPE = {"scope_project": "all", "scope_agent": None, "user_id": "steve"}
OPUS = "claude-code/claude-opus-5-5"


@pytest.fixture
def db(isolated_db):
    from infra.database import get_db

    connection = get_db()
    for thread in ("root", "other"):
        connection.execute("INSERT INTO threads (id,user_id,title,agent_enabled,agent_status,created_at,updated_at) VALUES (?,'steve',?,1,'active','t','t')", (thread, thread))
    connection.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) VALUES ('plan','thread','root','active',1,'t','t')")
    seed_identity_catalog(connection)
    try:
        yield connection
    finally:
        connection.close()


def _task(db, node: str, size: str | None, accepted: str = "2026-09-30T12:00:00Z") -> None:
    with raw_plan_seed(db):
        db.execute("INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,state,commitment_id,requirements_revision,revision,size,created_at,updated_at) VALUES (?, 'plan',NULL,1,'task','task','done','accepted',?,1,1,?,'t','t')",
                   (node, node, size))
        db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) VALUES (?,?,'test','accepted','active','accepted',?)", (f"accept-{node}", node, accepted))


def _usage(db, usage_id: str, node: str, model: str, *, source: str = "external_harness:claude-code", prompt: int = 1_000_000,
           at: str = "2026-09-29T12:00:00Z") -> None:
    db.execute("INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,cache_read_tokens,total_tokens,source,created_at,plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role) VALUES (?, 'root',?,?,0,0,0,?,?,?,'plan',?,'attempt',?,'execution')",
               (usage_id, model, prompt, prompt, source, at, node, node))


def _read(db) -> dict:
    prepare_outcome_periods(db, {**SCOPE, "grain": GRAIN}, roots=True)
    return query_task_hour_cost(db, {**SCOPE, "grain": two_windows(GRAIN)}, chain_costs(db, SCOPE, GRAIN)["cost_rows"])


def test_a_task_counts_when_it_has_priced_dollars_and_a_rating_whatever_else_its_chain_holds(db):
    _task(db, "m", "M")                       # counted: 2 h
    _task(db, "l", "L")                       # counted: 8 h
    _task(db, "before", "M", accepted="2026-08-19T12:00:00Z")   # the previous window
    _task(db, "unrated", None)                # no usable size: excluded, never zero hours
    _task(db, "local", "S")                   # only local calls: no priced dollars
    _task(db, "unproven", "S")                # only a call Cost cannot price: no priced dollars
    _task(db, "gap", "S")                     # a call failed to reach the ledger: still counted
    _task(db, "mixed", "S")                   # a priced call beside one Cost cannot price: still counted
    for node in ("m", "l", "unrated", "gap", "mixed"):
        _usage(db, f"u-{node}", node, OPUS)
    _usage(db, "u-mixed-unproven", "mixed", "codex/gpt-5.6-terra", source="external_harness:codex")
    _usage(db, "u-before", "before", OPUS, at="2026-08-18T12:00:00Z")
    _usage(db, "u-local", "local", LOCAL_ID, source="conversation")
    _usage(db, "u-unproven", "unproven", "codex/gpt-5.6-terra", source="external_harness:codex")
    db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,created_at) VALUES ('gap','gap','test','usage_recording_failed','2026-09-29T12:00:00Z')")
    prepare_outcome_periods(db, {**SCOPE, "grain": GRAIN}, roots=True)
    priced = {row["task_id"]: row["dollars"] for row in query_model_costs(db, {**SCOPE, "grain": two_windows(GRAIN)}, IdentityResolver(db))
              if row["outcome"] == "priced"}
    assert set(priced) == {"m", "l", "unrated", "gap", "mixed", "before"}

    costs = query_task_hour_cost(db, {**SCOPE, "grain": two_windows(GRAIN)}, chain_costs(db, SCOPE, GRAIN)["cost_rows"])
    counted = priced["m"] + priced["l"] + priced["gap"] + priced["mixed"]
    assert costs == {
        ("root", LATEST): {"dollars": pytest.approx(counted), "hours": 11.0, "n": 4},
        ("root", PREVIOUS): {"dollars": pytest.approx(priced["before"]), "hours": 2.0, "n": 1},
    }
    cell = dollars_metric(costs, "root", GRAIN, 1)
    assert cell["value"] == pytest.approx(counted / 11.0) and cell["n"] == 4
    assert cell["previous_value"] == pytest.approx(priced["before"] / 2.0) and cell["previous_n"] == 1
    assert dollars_metric(costs, "other", GRAIN, 1)["value"] is None


def test_narrowing_the_chain_read_to_the_rated_tasks_leaves_their_figures_unchanged(db):
    _task(db, "m", "M")
    _task(db, "other-task", None)
    _usage(db, "u-m", "m", OPUS)
    _usage(db, "u-o", "other-task", OPUS)
    prepare_outcome_periods(db, {**SCOPE, "grain": GRAIN}, roots=True)
    params = {**SCOPE, "grain": two_windows(GRAIN)}
    full = {row["task_id"]: row["dollars"] for row in query_model_costs(db, params, IdentityResolver(db)) if row["outcome"] == "priced"}
    only = {row["task_id"]: row["dollars"] for row in query_model_costs(db, params, IdentityResolver(db), {"m"}) if row["outcome"] == "priced"}
    assert set(full) == {"m", "other-task"} and only == {"m": full["m"]}


def test_a_window_with_no_rated_task_has_no_figure(db):
    _task(db, "unrated", None)
    _usage(db, "u", "unrated", OPUS)
    assert _read(db) == {}


def test_the_figure_is_withheld_under_the_table_floor_and_pools_across_periods():
    periods = list(GRAIN.latest.periods)
    few = {"dollars": 5.0, "hours": 10.0, "n": RANKING_MINIMUM_N - 1}
    enough = {"dollars": 5.0, "hours": 10.0, "n": RANKING_MINIMUM_N}
    assert dollars_metric({("a", periods[0]): few}, "a", GRAIN, RANKING_MINIMUM_N)["value"] is None
    assert dollars_metric({("a", periods[0]): enough}, "a", GRAIN, RANKING_MINIMUM_N)["value"] == 0.5
    # Two thin periods pool past the floor; one alone does not.
    pooled = dollars_metric({("a", periods[0]): few, ("a", periods[1]): {"dollars": 1.0, "hours": 2.0, "n": 1}},
                            "a", GRAIN, RANKING_MINIMUM_N)
    assert pooled["value"] == pytest.approx(0.5) and pooled["n"] == RANKING_MINIMUM_N
    assert dollars_metric({("a", periods[0]): {"dollars": 1.0, "hours": 0.0, "n": 25}}, "a", GRAIN, RANKING_MINIMUM_N)["value"] is None


def test_the_scope_figure_pools_every_agents_tasks_by_the_same_function():
    latest, before = list(GRAIN.latest.periods), list(GRAIN.previous.periods)
    costs = {
        ("a", latest[0]): {"dollars": 10.0, "hours": 10.0, "n": 25},
        ("b", latest[1]): {"dollars": 30.0, "hours": 10.0, "n": 25},
        ("a", before[0]): {"dollars": 5.0, "hours": 10.0, "n": 25},
    }
    overall = dollars_metric(costs, None, GRAIN, RANKING_MINIMUM_N)
    assert (overall["value"], overall["n"]) == (2.0, 50)
    assert (overall["previous_value"], overall["previous_n"]) == (0.5, 25)
    # One agent's own cell is unchanged by the other's tasks.
    assert dollars_metric(costs, "a", GRAIN, RANKING_MINIMUM_N)["value"] == 1.0


@pytest.mark.asyncio
async def test_the_agents_route_serves_dollars_per_hour_and_attention_minutes(isolated_db, monkeypatch):
    from test_measurement_agents import POST_BREAK, _freeze, _read, _seed_agent, _seed_project, _seed_period, _seeded

    _freeze(monkeypatch, POST_BREAK)

    def seed(connection):
        seed_identity_catalog(connection)
        _seed_project(connection, "p1")
        _seed_agent(connection, "worker", project_id="p1")
        _seed_period(connection, "worker", "2026-11-02", RANKING_MINIMUM_N)
        connection.execute("UPDATE plan_nodes SET size='S' WHERE id LIKE 'worker-2026-11-02-%'")
        for index in range(RANKING_MINIMUM_N):
            node = f"worker-2026-11-02-{index}"
            connection.execute("INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,cache_read_tokens,total_tokens,source,created_at,plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role) VALUES (?, 'worker',?,1000000,0,0,0,1000000,'external_harness:claude-code','2026-11-02T10:00:00Z','plan-worker',?,'attempt',?,'execution')",
                               (f"usage-{node}", OPUS, node, f"commitment-worker-2026-11-02-{index}"))
        connection.execute("INSERT INTO messages (id,thread_id,role,content,metadata,created_at) VALUES ('m1','worker','user','x','{}','2026-11-03T12:00:00Z')")

    _seeded(seed)
    (row,) = (await _read())["rows"]
    cell = row["metrics"]["dollars_per_work_hour"]
    assert cell["n"] == RANKING_MINIMUM_N and cell["previous_n"] == 0 and cell["previous_value"] is None
    # Each S task is 1 h of work carrying one 1M-token Opus call, so dollars per hour is that call's price.
    assert cell["value"] == pytest.approx(8.0)
    assert row["active_minutes"] == pytest.approx(1.0)
    # The one agent is all the work there is, so the scope's pooled figure is its own.
    overall = (await _read())["overall"]["dollars_per_work_hour"]
    assert (overall["value"], overall["n"]) == (cell["value"], cell["n"])
