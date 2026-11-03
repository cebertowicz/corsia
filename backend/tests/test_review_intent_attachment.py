"""A PR's review start carries its task's intent at claim and the intent's revision on its result
(Quality Gate design section 12, I1, I2, I5)."""

from __future__ import annotations

import pytest

from types import SimpleNamespace

from automation import review_intent, worker_inputs, worker_settle
from events.rule_outcomes import RETURNED
from infra.database import get_db
from plans import closure, service
from plans.boundary import PlanActor, mutate
from plans.close_next_bounds import REVISION_BRIEF_MAX_BYTES
from plans.public import link_node_artifact
from tests.rule_worker_helpers import world  # noqa: F401
from tests.test_plan_linked_node_for_artifact import PR, _active_task, _plan, _registered_from

OPTED_IN = SimpleNamespace(corsia={"workflow": {"task_intent": True}})
ROW = {"artifact_integration": PR[0], "artifact_type": PR[1], "artifact_external_id": PR[2]}


@pytest.fixture()
def db(isolated_db):
    return get_db()


def _task(db, plan_id: str, title: str, **fields):
    fields.setdefault("acceptance_criteria", "Done")
    node = service.add_node(plan_id, kind="task", title=title, db=db, **fields)
    return service.transition_node(node.id, "active", db=db)


def _revise(db, thread_id: str, plan_id: str, node_id: str, intent: str) -> None:
    mutate(plan_id, "edit", {"node_id": node_id, "intent": intent,
                             "expected_revision": service.get_node(node_id, db=db).revision},
           actor=PlanActor(thread_id, "steve", None), provenance="test", mutation_id="revise", db=db)


def test_task_intent_validates_as_a_boolean_and_is_a_shared_workflow_key():
    from core.skill_workflow_validation import WORKFLOW_KEYS, validate_workflow

    assert "task_intent" in WORKFLOW_KEYS
    for ok in (True, False):
        assert validate_workflow({"workflow": {"task_intent": ok}}) == []
    assert validate_workflow({"workflow": {}}) == []
    for bad in ("true", 1, None, [True]):
        assert validate_workflow({"workflow": {"task_intent": bad}}) == [
            "corsia.workflow.task_intent must be true or false"]


def test_ok_attaches_the_intent_in_its_shape(db):
    thread_id, plan = _plan(db)
    task = _task(db, plan.id, "Open the PR", intent="Ship the thing")
    _registered_from(db, thread_id, plan.id, task.id)

    intent, record = review_intent.attach(ROW, OPTED_IN, db=db)

    assert set(intent) == {"node_id", "linked_node", "title", "requirements_revision", "intent",
                           "acceptance_criteria", "intent_words"}
    assert (intent["intent"], intent["acceptance_criteria"], intent["title"]) == ("Ship the thing", "Done", "Open the PR")
    assert record == {"node_id": task.id, "linked_node": task.id, "requirements_revision": intent["requirements_revision"],
                      "intent": "Ship the thing", "acceptance_criteria": "Done",
                      "quotes": [word["quote"] for word in intent["intent_words"]]}
    assert "Ship the thing" in worker_inputs.brief("w@1", "body", {}, [], {}, intent)
    assert "Ship the thing" not in worker_inputs.brief("w@1", "body", {}, [], {})


def test_the_canonical_node_and_the_linked_node_are_both_recorded(db):
    thread_id, plan = _plan(db)
    canonical = _task(db, plan.id, "Canonical", intent="The shared goal")
    linked = _task(db, plan.id, "Linked", intent_node_id=canonical.id)
    _registered_from(db, thread_id, plan.id, linked.id)

    intent, record = review_intent.attach(ROW, OPTED_IN, db=db)

    assert (intent["node_id"], intent["linked_node"]) == (canonical.id, linked.id)
    assert (record["node_id"], record["linked_node"]) == (canonical.id, linked.id)
    assert intent["intent"] == "The shared goal"


def test_no_linked_task(db):
    thread_id, plan = _plan(db)
    task = _active_task(db, plan.id, "Open the PR")
    _registered_from(db, thread_id, plan.id, task.id)
    closure.close_node(task.id, reason="Not needed", db=db)
    db.commit()

    assert review_intent.attach(ROW, OPTED_IN, db=db) == (None, {"reason": "no_linked_task"})


def test_several_linked_tasks(db):
    thread_id, plan = _plan(db)
    first = _active_task(db, plan.id, "Open the PR")
    artifact_id = _registered_from(db, thread_id, plan.id, first.id)
    other = _active_task(db, plan.id, "Review the PR")
    link_node_artifact(other.id, artifact_id, "result", actor_id=None, provenance="test", db=db)
    db.commit()

    assert review_intent.attach(ROW, OPTED_IN, db=db) == (None, {"reason": "several_linked_tasks"})


@pytest.mark.parametrize("target", ["linked_node_for_artifact", "intent_of"])
def test_a_raising_read_is_lookup_failed_never_no_linked_task(db, monkeypatch, target):
    thread_id, plan = _plan(db)
    task = _task(db, plan.id, "Open the PR", intent="Ship the thing")
    _registered_from(db, thread_id, plan.id, task.id)

    def broken(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(review_intent, target, broken)

    assert review_intent.attach(ROW, OPTED_IN, db=db) == (None, {"reason": "lookup_failed"})


def test_a_subject_that_is_not_a_pull_request_has_nothing_to_attach(db):
    assert review_intent.attach({"artifact_integration": "local", "artifact_type": "document",
                                 "artifact_external_id": "x"}, OPTED_IN, db=db) == (None, None)
    assert review_intent.attach({"thread_id": "t"}, OPTED_IN, db=db) == (None, None)


def test_a_pr_start_whose_workflow_does_not_declare_task_intent_carries_none(db):
    # The shipped github-pr-screen case: a registered PR with an intent, a workflow that did not opt in.
    thread_id, plan = _plan(db)
    task = _task(db, plan.id, "Open the PR", intent="Ship the thing")
    _registered_from(db, thread_id, plan.id, task.id)
    for corsia in (None, {}, {"workflow": {"result": {}}}, {"workflow": {"task_intent": "yes"}}):
        assert review_intent.attach(ROW, SimpleNamespace(corsia=corsia), db=db) == (None, None)
    assert not {"intent_node", "intent_reason", "intent_current_revision"} & set(_settled_facts(db, None))


def test_the_intent_and_criteria_are_bounded(db, monkeypatch):
    # Plans caps a node's own text far below the bound, so the read is widened here.
    thread_id, plan = _plan(db)
    task = _task(db, plan.id, "Open the PR", intent="Ship the thing")
    _registered_from(db, thread_id, plan.id, task.id)
    real = review_intent.intent_of
    big = "x" * (REVISION_BRIEF_MAX_BYTES * 2)
    monkeypatch.setattr(review_intent, "intent_of",
                        lambda node_id, **kw: {**real(node_id, **kw), "intent": big, "acceptance_criteria": big})

    intent, _ = review_intent.attach(ROW, OPTED_IN, db=db)

    assert 0 < len(intent["intent"].encode()) <= REVISION_BRIEF_MAX_BYTES
    assert 0 < len(intent["acceptance_criteria"].encode()) <= REVISION_BRIEF_MAX_BYTES


RECORD = {"node_id": "n1", "linked_node": "n0", "requirements_revision": 3, "intent": "Goal",
          "acceptance_criteria": "AC", "quotes": ["Quote one"]}


def _settled_facts(db, record, said=None):
    row = {"thread_id": "owner", "rule_id": "r", "event_id": "e", "request_identity": "pr@a", "id": 1,
           "connector": "qa", "event_type": "request", "artifact_external_id": PR[2],
           "artifact_integration": PR[0], "artifact_type": PR[1]}
    check = {"workflow": "review@1", "intent": record}
    return worker_settle.settlement(row, check, RETURNED, said or {"result": "{}"}, db).facts


def test_a_result_is_stamped_with_the_revision_read_at_emit_not_at_claim(db):
    thread_id, plan = _plan(db)
    task = _task(db, plan.id, "Open the PR", intent="First goal")
    _registered_from(db, thread_id, plan.id, task.id)
    _, record = review_intent.attach(ROW, OPTED_IN, db=db)
    claimed = record["requirements_revision"]
    assert _settled_facts(db, record)["intent_current_revision"] == claimed

    _revise(db, thread_id, plan.id, task.id, "A corrected goal")

    facts = _settled_facts(db, record)
    assert facts["intent_node"] == task.id
    assert facts["intent_current_revision"] > claimed


def test_the_stamp_comes_from_the_record_and_wins_over_the_workers_answer(db):
    thread_id, plan = _plan(db)
    task = _task(db, plan.id, "Open the PR", intent="First goal")
    _registered_from(db, thread_id, plan.id, task.id)
    _, record = review_intent.attach(ROW, OPTED_IN, db=db)
    forged = {"intent_text": {"intent": "fake", "acceptance_criteria": "fake", "quotes": []},
              "intent_node": "fake", "intent_current_revision": 99, "intent_revision": 99,
              "intent_linked_node": "fake"}

    for said in ({"result": "{}"}, {"result": "{}", **forged}):
        facts = _settled_facts(db, record, said)
        assert facts["intent_node"] == facts["intent_linked_node"] == task.id
        assert facts["intent_revision"] == facts["intent_current_revision"] == record["requirements_revision"]
        assert facts["intent_text"] == {"intent": "First goal", "acceptance_criteria": "Done",
                                        "quotes": record["quotes"]}


def test_a_worst_case_intent_with_a_typical_result_fits_intakes_fact_limit(db, monkeypatch):
    import json

    from events.intake import _MAX_FACT_BYTES

    big = "x" * (REVISION_BRIEF_MAX_BYTES * 2)
    quote = "q" * 240
    record = {"node_id": "n1", "linked_node": "n0", "requirements_revision": 3,
              "intent": review_intent.bounded_label(big, REVISION_BRIEF_MAX_BYTES),
              "acceptance_criteria": review_intent.bounded_label(big, REVISION_BRIEF_MAX_BYTES),
              "quotes": [quote] * 3}
    monkeypatch.setattr(review_intent, "intent_of", lambda *a, **k: {"requirements_revision": 3})
    typical = json.dumps({"findings": [{"title": "t" * 200, "body": "b" * 600}] * 20})
    facts = _settled_facts(db, record, {"result": typical})
    size = len(json.dumps(facts, separators=(",", ":"), ensure_ascii=False).encode())
    print("worst-case stamped facts bytes:", size, "of", _MAX_FACT_BYTES)
    assert size < _MAX_FACT_BYTES


def test_a_result_with_no_intent_says_none_and_why(db):
    facts = _settled_facts(db, {"reason": "several_linked_tasks"})
    assert (facts["intent_node"], facts["intent_reason"]) == ("none", "several_linked_tasks")
    assert "intent_current_revision" not in facts


def test_a_failed_fresh_read_says_so_and_never_copies_the_old_revision(db, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(review_intent, "intent_of", broken)
    facts = _settled_facts(db, RECORD)
    assert (facts["intent_node"], facts["intent_reason"]) == ("n1", "lookup_failed")
    assert (facts["intent_revision"], facts["intent_linked_node"], facts["intent_text"]) == (
        3, "n0", {"intent": "Goal", "acceptance_criteria": "AC", "quotes": ["Quote one"]})
    assert "intent_current_revision" not in facts


def test_a_result_settled_without_a_connection_still_carries_its_stamp(db):
    row = {"thread_id": "owner", "rule_id": "r", "event_id": "e", "request_identity": "pr@a", "id": 1,
           "connector": "qa", "event_type": "request", "artifact_external_id": PR[2],
           "artifact_integration": PR[0], "artifact_type": PR[1]}
    facts = worker_settle.settlement(row, {"workflow": "review@1", "intent": RECORD}, RETURNED,
                                     {"result": "{}"}).facts
    assert facts["intent_node"] == "n1" and facts["intent_text"]["intent"] == "Goal"


def test_a_start_that_was_not_a_pull_request_carries_no_stamp(db):
    facts = _settled_facts(db, None)
    assert not {"intent_node", "intent_current_revision", "intent_reason"} & set(facts)


@pytest.mark.asyncio
async def test_the_runner_prepares_the_attachment_beside_its_other_reads(world):
    from automation import worker_runner
    from infra.db_async import run_in_db
    from tests.rule_worker_helpers import OWNER

    def prepare():
        return worker_runner._prepare(
            {"thread_id": OWNER, "workflow_id": "worker-flow", **ROW},
            {"event": {"facts": {"staging_path": "req"}}},
        )["intent"]

    assert await run_in_db(prepare) == (None, None)  # the fixture's workflow does not opt in
    sidecar = world["root"] / "skills" / "worker-flow" / "corsia.yaml"
    sidecar.write_text(sidecar.read_text().replace("workflow:\n", "workflow:\n  task_intent: true\n", 1))
    assert await run_in_db(prepare) == (None, {"reason": "lookup_failed"})


@pytest.mark.asyncio
async def test_a_big_result_with_a_maximal_intent_is_admitted_through_the_real_settle(world, monkeypatch):
    import json

    from events.intake import _MAX_FACT_BYTES
    from tests.rule_worker_helpers import fenced, make_rule, request, settle_all, starts, worker_events

    big = "x" * (REVISION_BRIEF_MAX_BYTES * 2)
    record = {"node_id": "n1", "linked_node": "n0", "requirements_revision": 3,
              "intent": review_intent.bounded_label(big, REVISION_BRIEF_MAX_BYTES),
              "acceptance_criteria": review_intent.bounded_label(big, REVISION_BRIEF_MAX_BYTES),
              "quotes": ["q" * 8192] * 4}  # 32 KB of quotes, as in Quality Gate pack test
    monkeypatch.setattr(review_intent, "attach", lambda row, skill, *, db: (None, record))
    monkeypatch.setattr(review_intent, "intent_of", lambda *a, **k: {"requirements_revision": 3})
    seen = []
    real = worker_settle.settle

    async def measuring(item):
        seen.append(len(json.dumps(item.facts, separators=(",", ":"), ensure_ascii=False).encode()))
        await real(item)

    monkeypatch.setattr(worker_settle, "settle", measuring)
    payload = {"findings": [{"title": "t" * 570} for _ in range(78)]}
    world["reply"].write_text(fenced(payload))
    rule = await make_rule()
    await request()
    await settle_all()

    assert starts(rule)[0][0] == "returned"
    assert [kind for kind, _ in worker_events()] == ["worker.result"]
    print("settled worker.result facts bytes:", seen[0], "of", _MAX_FACT_BYTES)
    assert seen[0] > _MAX_FACT_BYTES


def test_an_emitter_event_over_the_limit_is_still_refused_too_large(isolated_db):
    import asyncio

    from events.intake import admit_event

    result = asyncio.run(admit_event(event_id="big-1", origin="emitter", producer="someone",
                                     event_type="qa.route", facts={"code": "x" * (65 * 1024)}))
    assert result.refusal is not None and result.refusal.code == "too_large"
