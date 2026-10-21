"""Self-caught failures enter the existing correction lifecycle without inference."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from events.connectors.github_admission import _write_raw_facts
from events.connectors.github_outcomes import map_github_outcomes
from infra.database import get_db
from plans import service
from plans.boundary import PlanActor, mutate
from tests.event_routing_helpers import register_pr

FIXTURES = Path(__file__).parent / "fixtures/github_outcomes"
REPO = "cebertowicz/maestro"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def db(isolated_db, monkeypatch):
    monkeypatch.setattr("infra.passive_changes.clock_event", lambda *_args: None)
    connection = get_db()
    for thread_id in ("owner", "reporter"):
        connection.execute(
            "INSERT INTO threads (id,user_id,created_at,updated_at) VALUES (?, 'user', 't', 't')",
            (thread_id,),
        )
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


def _actor(thread_id="owner") -> PlanActor:
    return PlanActor(thread_id, "user", None)


def _mutate(db, plan_id, operation, params, key, *, actor="owner"):
    return mutate(
        plan_id, operation, params, actor=_actor(actor), provenance="test.self_caught",
        mutation_id=key, db=db,
    ).result


def _task(db, plan_id, title):
    return service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria="verified", db=db,
    )


def _facts(db):
    return [
        (row["subject_key"], row["agent_thread_id"], json.loads(row["attributes"]))
        for row in db.execute(
            "SELECT subject_key,agent_thread_id,attributes FROM outcome_events "
            "WHERE event_name='measurement.correction' ORDER BY rowid"
        )
    ]


def test_fail_records_one_direct_correction_and_replay_is_idempotent(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    task = _task(db, plan.id, "Fail me")
    active = _mutate(db, plan.id, "activate", {"node_id": task.id, "expected_revision": task.revision}, "start")
    params = {"node_id": task.id, "expected_revision": active["node"]["revision"]}

    _mutate(db, plan.id, "fail", params, "fail")
    _mutate(db, plan.id, "fail", params, "fail")

    [(subject, thread_id, attributes)] = _facts(db)
    assert subject == f"self-caught:plan-failed:{task.id}"
    assert thread_id == "owner"
    assert attributes == {
        "source_kind": "self_caught", "source_id": f"plan-failed:{task.id}",
        "thread_id": "owner", "caught_by": "us", "label": "correction",
        "recorded_at": attributes["recorded_at"],
    }
    assert db.execute(
        "SELECT COUNT(*) FROM outcome_events "
        "WHERE json_extract(attributes, '$.caught_by')='us'"
    ).fetchone()[0] == 1


def test_terminal_add_correction_targets_the_original_settler(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    source = _task(db, plan.id, "Original work")
    active = _mutate(db, plan.id, "activate", {"node_id": source.id, "expected_revision": source.revision}, "start-source")
    reviewed = _mutate(
        db, plan.id, "request_review",
        {"node_id": source.id, "evidence": "proof", "expected_revision": active["node"]["revision"]},
        "review-source",
    )
    accepted = _mutate(
        db, plan.id, "accept",
        {"node_id": source.id, "verification_result": "passed", "expected_revision": reviewed["node"]["revision"]},
        "accept-source",
    )
    affected = _task(db, plan.id, "Affected work")
    _mutate(
        db, plan.id, "add_correction",
        {
            "node_id": source.id, "title": "Correct source", "acceptance_criteria": "corrected",
            "affected_nodes": [{"node_id": affected.id, "expected_revision": affected.revision}],
            "expected_revision": accepted["node"]["revision"],
        },
        "correct-source",
    )

    [(subject, thread_id, attributes)] = _facts(db)
    assert subject.startswith(f"self-caught:plan-correction:{source.id}:")
    assert thread_id == "owner"
    assert attributes["caught_by"] == "us"


def test_reject_is_not_a_self_caught_source(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    task = _task(db, plan.id, "Needs review")
    active = _mutate(db, plan.id, "activate", {"node_id": task.id, "expected_revision": task.revision}, "start")
    reviewed = _mutate(
        db, plan.id, "request_review",
        {"node_id": task.id, "evidence": "proof", "expected_revision": active["node"]["revision"]},
        "review",
    )
    _mutate(
        db, plan.id, "reject",
        {"node_id": task.id, "feedback": "revise", "expected_revision": reviewed["node"]["revision"]},
        "reject",
    )
    assert _facts(db) == []


def test_github_reopen_and_explicit_merged_revert_target_owned_prs(db):
    register_pr(REPO, 3191, "owner")
    closes = _fixture("pr_closes_merged")
    _write_raw_facts(db, closes["event"], map_github_outcomes(closes["event"], closes["payload"]), "close")
    reopened = _fixture("issue_reopened")
    _write_raw_facts(db, reopened["event"], map_github_outcomes(reopened["event"], reopened["payload"]), "reopen")

    reverts = _fixture("pr_reverts_merged")
    _write_raw_facts(db, reverts["event"], map_github_outcomes(reverts["event"], reverts["payload"]), "revert")

    facts = _facts(db)
    assert len(facts) == 2
    assert {thread_id for _subject, thread_id, _attributes in facts} == {"owner"}
    assert {attributes["caught_by"] for _subject, _thread_id, attributes in facts} == {"us"}
    assert any("github:issue:cebertowicz/maestro#1627:reopened" in subject for subject, *_ in facts)
    assert any("github:pr:cebertowicz/maestro#3200:reverts:cebertowicz/maestro#3191" in subject for subject, *_ in facts)


def test_red_checks_do_not_create_self_caught_corrections(db):
    failure = _fixture("workflow_run_failure")
    _write_raw_facts(db, failure["event"], map_github_outcomes(failure["event"], failure["payload"]), "red-check")
    assert _facts(db) == []
