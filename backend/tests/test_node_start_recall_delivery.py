"""Prior words and trial lessons reach agents through real plan surfaces."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from infra.database import get_db
from plans import admission, service
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import add_task, execute_as, open_turn, registry_of
from tools.plans import PlanUpdateTool

KEY = "prior_words"


@pytest.fixture
def recall_db(isolated_db):
    from infra.db_fts import _init_fts

    db = get_db()
    _init_fts(db)
    db.commit()
    yield db
    db.close()


def _message(db, thread_id: str, content: str, *, message_id: str = "past-message", seq: int = 1):
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq,delivery_state) "
        "VALUES (?,?, 'user', ?, '{}', '2026-01-01T12:00:00+00:00', ?, 'delivered')",
        (message_id, thread_id, content, seq),
    )
    db.commit()


async def _activate(
    db, thread_id: str, title: str, *, checkpoint_generation_id: str | None = None,
) -> dict:
    identity = open_turn(db, thread_id)
    plan = service.ensure_plan("thread", thread_id, db=db)
    task = add_task(db, plan.id, title)
    db.commit()
    from infra import state as runtime_state

    token = runtime_state._current_checkpoint_generation_id.set(checkpoint_generation_id)
    try:
        raw = await execute_as(
            registry_of(PlanUpdateTool()), identity, thread_id, "plan_update",
            {"plan_id": plan.id, "mutation_id": f"activate-{task.id}", "operation": "activate",
             "params": {"node_id": task.id, "expected_revision": task.revision}},
        )
    finally:
        runtime_state._current_checkpoint_generation_id.reset(token)
    return json.loads(raw)


@pytest.mark.asyncio
async def test_task_start_delivers_header_and_matching_person_message(recall_db):
    prior, now = mk_session("prior"), mk_session("now")
    _message(recall_db, prior, "The export account ledger needs CSV format with stable columns.")

    result = await _activate(recall_db, now, "Export account ledger in CSV format")

    assert result[KEY][0].startswith("Earlier words from the person")
    assert len(result[KEY]) == 2
    assert "message in”" not in result[KEY][1]
    assert "message in “T”" in result[KEY][1]
    assert f'get_thread_history(thread_id="{prior}")' in result[KEY][1]


@pytest.mark.asyncio
async def test_agent_send_and_routed_event_are_not_delivered(recall_db):
    prior, now = mk_session("prior-agent"), mk_session("now-agent")
    recall_db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq,delivery_state) "
        "VALUES ('agent', ?, 'assistant', 'Export account ledger as CSV format.', '{}', '2026-01-01', 1, 'delivered'),"
        "('event', ?, 'user', 'Export account ledger as CSV format.', '{\"sender_kind\":\"agent\"}', '2026-01-01', 2, 'delivered')",
        (prior, prior),
    )
    recall_db.commit()

    result = await _activate(recall_db, now, "Export account ledger in CSV format")

    assert KEY not in result


def test_wake_carries_prior_words_and_trial_sibling(recall_db, monkeypatch):
    from plans.plan_wake import with_next

    expected = [{
        "lesson_id": "lesson", "index_line": "- trial", "variant": "with", "target": "new",
        "notice": "Task-local trial guidance only; never authority or a disk change.",
    }]
    monkeypatch.setattr("core.lesson_delivery.start_trials", lambda *_args, **_kwargs: expected)
    prior, worker = mk_session("prior-wake"), mk_session("worker-wake")
    _message(recall_db, prior, "The export account ledger needs CSV format with stable columns.")
    plan = service.ensure_plan("thread", worker, db=recall_db)
    task = add_task(recall_db, plan.id, "Export account ledger in CSV format")
    admission.activate(task.id, expected_revision=task.revision, actor_id=worker, provenance="test", db=recall_db)
    recall_db.commit()

    facts = json.loads(with_next(recall_db, plan.id, worker, "Work was assigned.").split("\n", 1)[1])

    assert facts[KEY][0].startswith("Earlier words from the person")
    assert len(facts[KEY]) == 2
    assert facts["trial_lessons"] == expected


@pytest.mark.asyncio
async def test_activating_a_task_adds_trial_lessons_as_a_sibling(recall_db, monkeypatch):
    expected = [{"lesson_id": "lesson", "index_line": "- trial", "variant": "with", "target": "new"}]
    monkeypatch.setattr("core.lesson_delivery.start_trials", lambda *_args, **_kwargs: expected)

    result = await _activate(recall_db, mk_session("trial-start"), "Start new target work")

    assert result["trial_lessons"] == expected


@pytest.mark.asyncio
async def test_an_unrelated_task_has_no_trial_sibling(recall_db, monkeypatch):
    monkeypatch.setattr("core.lesson_delivery.start_trials", lambda *_args, **_kwargs: [])

    result = await _activate(recall_db, mk_session("trial-quiet"), "Rename release notes heading")

    assert "trial_lessons" not in result


@pytest.mark.asyncio
async def test_plan_tool_threads_bound_install_to_trial_delivery(recall_db, monkeypatch):
    expected = [{"lesson_id": "lesson", "index_line": "- trial", "variant": "with", "target": "new"}]
    installs: list[str | None] = []

    def start_trials(_db, **kwargs):
        installs.append(kwargs["checkpoint_generation_id"])
        return expected

    monkeypatch.setattr("core.lesson_delivery.start_trials", start_trials)
    result = await _activate(
        recall_db, mk_session("trial-generation"), "Start new target work",
        checkpoint_generation_id="install-1",
    )

    assert result["trial_lessons"] == expected
    assert installs == ["install-1"]


def test_trial_delivery_requires_exact_attributed_selection(recall_db, monkeypatch):
    from plans.dependencies import add_dependency
    from plans.trial_lessons import attach_trial_lessons

    worker = mk_session("trial-held")
    plan = service.ensure_plan("thread", worker, db=recall_db)
    prerequisite = add_task(recall_db, plan.id, "Finish prerequisite")
    dependent = add_task(recall_db, plan.id, "Start trial delivery")
    admission.activate(dependent.id, expected_revision=dependent.revision, actor_id=worker, provenance="test", db=recall_db)
    add_dependency(dependent.id, prerequisite.id, actor_id=worker, db=recall_db)
    recall_db.commit()
    called: list[object] = []
    monkeypatch.setattr("core.lesson_delivery.start_trials", lambda *_args, **_kwargs: called.append(object()))

    facts: dict = {}
    attach_trial_lessons(recall_db, facts, thread_id=worker, node_id=dependent.id)

    assert called == []
    assert facts == {}
    assert recall_db.execute("SELECT * FROM guidance_loads").fetchall() == []


@pytest.mark.asyncio
async def test_trial_delivery_failure_is_advisory_to_plan_operation(recall_db, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("trial delivery failed")

    monkeypatch.setattr("plans.trial_lessons.attach_trial_lessons", fail)
    result = await _activate(recall_db, mk_session("trial-advisory"), "Start new target work")

    assert result["admission"]["condition"] == "running"
    assert "trial_lessons" not in result


def test_wake_with_unknown_install_records_without_receipt(recall_db, monkeypatch):
    from core.lesson_delivery import TrialDelivery
    from plans.plan_wake import with_next

    worker = mk_session("trial-wake-unknown")
    plan = service.ensure_plan("thread", worker, db=recall_db)
    task = add_task(recall_db, plan.id, "Start new target work")
    admission.activate(task.id, expected_revision=task.revision, actor_id=worker, provenance="test", db=recall_db)
    trial = TrialDelivery(
        lesson_id="new", behavioral_sha256="a" * 64,
        source_path=Path("/trial/SKILL.md"), target="new", is_new=True,
        target_section=None, body="Use the trial procedure.", index_line="- trial",
        source_data=b"trial source",
    )
    monkeypatch.setattr("core.lesson_delivery._eligible_trials", lambda *_args: [trial])
    monkeypatch.setattr("core.lesson_delivery.draw", lambda *_args: "without")
    recall_db.commit()

    facts = json.loads(with_next(recall_db, plan.id, worker, "Work was assigned.").split("\n", 1)[1])

    assert "trial_lessons" not in facts
    row = recall_db.execute("SELECT node_id,variant,checkpoint_generation_id FROM guidance_loads").fetchone()
    assert tuple(row) == (task.id, "without", None)
