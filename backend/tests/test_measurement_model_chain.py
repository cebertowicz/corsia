"""Models counts an accepted task's whole chain: workers at every level and sent-back attempts.

Each call is charged once, to the nearest accepted task above it, and each model is
charged only for the calls it served.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from api.cost_identity import IdentityResolver
from api.measurement_model_cost import query_model_costs, query_recording_gaps
from api.measurement_scope import resolve_grain
from model_identity_fixtures import seed_identity_catalog
from tests.plan_writer_authorizer import raw_plan_seed

# Weeks in UTC as of Monday 2026-10-05: the twelve completed weeks end on 2026-09-28.
GRAIN = resolve_grain("weeks", "UTC", now=datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
# The same grain reduced to its last period alone, so a task accepted earlier is out of range.
LATEST_ONLY = replace(GRAIN, periods=GRAIN.periods[-1:], starts=GRAIN.starts[-2:])
PARAMS = {"scope_project": "all", "scope_agent": None, "user_id": "steve", "grain": GRAIN}
OPUS, SONNET = "claude-code/claude-opus-5-5", "claude-code/claude-sonnet-5-5"  # $4 and $2 per 1M prompt tokens
SETTLED = "2026-09-30T12:00:00Z"
BEFORE = "2026-09-29T12:00:00Z"
AFTER = "2026-10-01T12:00:00Z"


@pytest.fixture
def db(isolated_db):
    from infra.database import get_db

    connection = get_db()
    connection.execute("INSERT INTO threads (id,user_id,title,agent_enabled,agent_status,created_at,updated_at) VALUES ('root','steve','root',1,'active','t','t')")
    connection.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) VALUES ('plan','thread','root','active',1,'t','t')")
    seed_identity_catalog(connection)
    try:
        yield connection
    finally:
        connection.close()


def _worker(db, thread: str, plan: str, parent_plan: str) -> None:
    """A spawned thread with its own plan nested under ``parent_plan``."""
    db.execute("INSERT INTO threads (id,user_id,title,agent_enabled,agent_status,spawned_by,created_at,updated_at) VALUES (?,'steve',?,1,'active','root','t','t')", (thread, thread))
    db.execute("INSERT INTO plans (id,owner_kind,owner_id,parent_plan_id,state,revision,created_at,updated_at) VALUES (?,'thread',?,?,'active',1,'t','t')", (plan, thread, parent_plan))


def _node(db, node: str, plan: str = "plan", *, parent: str | None = None, state: str = "accepted", at: str | None = SETTLED,
          worker: str | None = None, follow_up_of: str | None = None, revision: bool = False, rejected: bool = False) -> None:
    """One task node; an accepted one carries its acceptance event, a sent-back one its rejection."""
    with raw_plan_seed(db):
        db.execute("INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,state,spawned_thread_id,follow_up_of,commitment_id,requirements_revision,revision,created_at,updated_at) VALUES (?,?,?,1,'task','task','done',?,?,?,?,1,1,'t','t')",
                   (node, plan, parent, state, worker, follow_up_of, node))
        event = "INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,payload_json,created_at) VALUES (?,?,'test',?,?,?,?,?)"
        if state == "accepted" and at:
            db.execute(event, (f"accept-{node}", node, "accepted", "active", "accepted", None, at))
        if rejected:
            db.execute(event, (f"reject-{node}", node, "review_rejected", "active", "review_requested", None, BEFORE))
        if revision:
            db.execute(event, (f"revision-{node}", node, "revision_created", None, None, None, BEFORE))


def _attach(db, node: str, child_plan: str) -> None:
    db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,payload_json,created_at) VALUES (?,?,'test','spawned_thread_attached',?,'2026-09-29T00:00:00Z')",
               (f"attach-{node}-{child_plan}", node, json.dumps({"child_plan_id": child_plan})))


_calls = iter(range(10_000))


def _call(db, node: str | None, model: str = OPUS, at: str = BEFORE, thread: str = "root") -> None:
    plan = ("plan", node, "attempt", node, "execution") if node else (None,) * 5
    db.execute("INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,cache_read_tokens,total_tokens,source,created_at,plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role) VALUES (?,?,?,1000000,0,0,0,1000000,'external_harness:claude-code',?,?,?,?,?,?)",
               (f"call-{next(_calls)}", thread, model, at, *plan))


def _chains(db, params=PARAMS) -> dict[tuple[str, str], dict]:
    return {(row["task_id"], row["model"]): row for row in query_model_costs(db, params, IdentityResolver(db))}


def _records(db, **kwargs) -> Counter:
    return Counter({key: row["records"] for key, row in _chains(db, **kwargs).items()})


def test_a_task_counts_its_workers_two_levels_down_on_another_model(db):
    _node(db, "task", worker="w1")
    _worker(db, "w1", "w1plan", "plan")
    _attach(db, "task", "w1plan")
    _node(db, "step", "w1plan", worker="w2")
    _worker(db, "w2", "w2plan", "w1plan")
    _attach(db, "step", "w2plan")
    _node(db, "leaf", "w2plan", state="active", at=None)
    _call(db, "task")
    _call(db, "step", SONNET, thread="w1")
    _call(db, "leaf", OPUS, thread="w2")
    _call(db, "leaf", SONNET, thread="w2")
    _call(db, None, SONNET, thread="w1")  # a worker call with no task is never attributed
    assert _records(db) == Counter({("task", OPUS): 2, ("task", SONNET): 2})


def test_every_sent_back_attempt_is_counted_once_with_the_accepted_revision(db):
    _node(db, "attempt", state="review_requested", at=None, rejected=True)
    _node(db, "second", state="review_requested", at=None, rejected=True, follow_up_of="attempt", revision=True)
    _node(db, "third", follow_up_of="second", revision=True)
    _call(db, "attempt", SONNET)
    _call(db, "second", SONNET)
    _call(db, "third")
    assert _records(db) == Counter({("third", OPUS): 1, ("third", SONNET): 2})


def test_an_ordinary_follow_up_does_not_pull_its_source_into_its_chain(db):
    _node(db, "source", state="accepted")
    _node(db, "follow-up", follow_up_of="source")  # accept-with-follow-up writes no revision_created
    _call(db, "source")
    _call(db, "follow-up", SONNET)
    assert _records(db) == Counter({("source", OPUS): 1, ("follow-up", SONNET): 1})


def test_each_model_is_charged_only_its_own_calls_and_the_task_counts_under_each(db):
    _node(db, "task", worker="w1")
    _worker(db, "w1", "w1plan", "plan")
    _attach(db, "task", "w1plan")
    _node(db, "work", "w1plan", state="active", at=None)
    for _ in range(2):
        _call(db, "task", OPUS)
    _call(db, "work", SONNET, thread="w1")
    chains = _chains(db)
    assert {key for key in chains if key[0] == "task"} == {("task", OPUS), ("task", SONNET)}
    assert chains[("task", OPUS)]["dollars"] == pytest.approx(2 * 4.0)
    assert chains[("task", SONNET)]["dollars"] == pytest.approx(2.0)


def test_a_predecessor_also_reached_through_the_tree_is_counted_once(db):
    _node(db, "task")
    _node(db, "first", parent="task", state="review_requested", at=None, rejected=True)
    _node(db, "retry", parent="task", state="active", at=None, follow_up_of="first", revision=True)
    _call(db, "task")
    _call(db, "first")
    _call(db, "retry")
    chains = _chains(db)
    assert chains[("task", OPUS)]["records"] == 3
    assert chains[("task", OPUS)]["dollars"] == pytest.approx(3 * 4.0)


def test_a_nested_accepted_task_keeps_its_own_subtree_and_revision_chain(db):
    _node(db, "outer", worker="w1")
    _worker(db, "w1", "w1plan", "plan")
    _attach(db, "outer", "w1plan")
    _node(db, "worker-step", "w1plan", state="active", at=None)
    _node(db, "inner", parent="outer", worker="w2")
    _worker(db, "w2", "w2plan", "plan")
    _attach(db, "inner", "w2plan")
    _node(db, "inner-step", "w2plan", state="active", at=None)
    _node(db, "first", parent="outer", state="review_requested", at=None, rejected=True)
    _node(db, "revised", parent="outer", follow_up_of="first", revision=True)
    _call(db, "outer")
    _call(db, "worker-step", SONNET, thread="w1")
    _call(db, "inner", SONNET)
    _call(db, "inner-step", SONNET, thread="w2")
    _call(db, "first", SONNET)
    _call(db, "revised")
    assert _records(db) == Counter({
        ("outer", OPUS): 1, ("outer", SONNET): 1,
        ("inner", SONNET): 2,
        ("revised", OPUS): 1, ("revised", SONNET): 1,
    })


def test_a_nested_task_accepted_in_another_period_still_owns_its_calls(db):
    _node(db, "outer")
    _node(db, "inner", parent="outer", at="2026-09-15T12:00:00Z")
    _call(db, "outer")
    _call(db, "inner", SONNET, at="2026-09-14T12:00:00Z")
    assert _records(db, params={**PARAMS, "grain": LATEST_ONLY}) == Counter({("outer", OPUS): 1})


def test_the_acceptance_cutoff_applies_to_workers_and_attempts(db):
    _node(db, "task", worker="w1")
    _worker(db, "w1", "w1plan", "plan")
    _attach(db, "task", "w1plan")
    _node(db, "work", "w1plan", state="active", at=None)
    _call(db, "work", SONNET, at=AFTER, thread="w1")
    _call(db, "task")
    assert _records(db) == Counter({("task", OPUS): 1})


def test_a_recording_failure_anywhere_in_the_chain_marks_the_task(db):
    _node(db, "task", worker="w1")
    _worker(db, "w1", "w1plan", "plan")
    _attach(db, "task", "w1plan")
    _node(db, "work", "w1plan", state="active", at=None)
    _node(db, "late", "w1plan", state="active", at=None)
    failed = "INSERT INTO plan_node_events (id,node_id,provenance,event,created_at) VALUES (?,?,'test','usage_recording_failed',?)"
    db.execute(failed, ("after", "late", AFTER))
    assert query_recording_gaps(db, PARAMS) == set()
    db.execute(failed, ("before", "work", BEFORE))
    assert query_recording_gaps(db, PARAMS) == {"task"}
