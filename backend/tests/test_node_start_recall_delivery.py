"""Prior words reach agents through the real task-start and wake surfaces."""

from __future__ import annotations

import json

import pytest

from infra.database import get_db
from plans import admission, service
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import add_task, execute_as, open_turn, registry_of
from tools.plans import PlanUpdateTool

KEY = "prior_words"


@pytest.fixture
def delivery_on(monkeypatch):
    from memory import node_start_recall

    monkeypatch.setattr(node_start_recall, "DELIVERY_ON", True)


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


async def _activate(db, thread_id: str, title: str) -> dict:
    identity = open_turn(db, thread_id)
    plan = service.ensure_plan("thread", thread_id, db=db)
    task = add_task(db, plan.id, title)
    db.commit()
    return json.loads(await execute_as(
        registry_of(PlanUpdateTool()), identity, thread_id, "plan_update",
        {"plan_id": plan.id, "mutation_id": f"activate-{task.id}", "operation": "activate",
         "params": {"node_id": task.id, "expected_revision": task.revision}},
    ))


@pytest.mark.asyncio
async def test_task_start_delivers_header_and_matching_person_message(recall_db, delivery_on):
    prior, now = mk_session("prior"), mk_session("now")
    _message(recall_db, prior, "The export account ledger needs CSV format with stable columns.")

    result = await _activate(recall_db, now, "Export account ledger in CSV format")

    assert result[KEY][0].startswith("Earlier words from the person")
    assert len(result[KEY]) == 2
    assert "message in”" not in result[KEY][1]  # keeps the quoted title readable
    assert "message in “T”" in result[KEY][1]
    assert f'get_thread_history(thread_id="{prior}")' in result[KEY][1]


@pytest.mark.asyncio
async def test_agent_send_and_routed_event_are_not_delivered(recall_db, delivery_on):
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


def test_wake_carries_prior_words(recall_db, delivery_on):
    prior, worker = mk_session("prior-wake"), mk_session("worker-wake")
    _message(recall_db, prior, "The export account ledger needs CSV format with stable columns.")
    plan = service.ensure_plan("thread", worker, db=recall_db)
    task = add_task(recall_db, plan.id, "Export account ledger in CSV format")
    admission.activate(task.id, expected_revision=task.revision, actor_id=worker, provenance="test", db=recall_db)
    recall_db.commit()

    from plans.plan_wake import with_next
    facts = json.loads(with_next(recall_db, plan.id, worker, "Work was assigned.").split("\n", 1)[1])

    assert facts[KEY][0].startswith("Earlier words from the person")
    assert len(facts[KEY]) == 2


@pytest.mark.asyncio
async def test_delivery_is_off_by_default_on_start_and_wake(recall_db, monkeypatch):
    from memory import node_start_recall

    looked_up = []
    monkeypatch.setattr(node_start_recall, "related", lambda *args, **kwargs: looked_up.append(1) or ["x"])
    prior, now, worker = mk_session("prior-off"), mk_session("now-off"), mk_session("worker-off")
    _message(recall_db, prior, "The export account ledger needs CSV format with stable columns.")

    result = await _activate(recall_db, now, "Export account ledger in CSV format")
    plan = service.ensure_plan("thread", worker, db=recall_db)
    task = add_task(recall_db, plan.id, "Export account ledger in CSV format")
    admission.activate(task.id, expected_revision=task.revision, actor_id=worker, provenance="test", db=recall_db)
    recall_db.commit()
    from plans.plan_wake import with_next
    facts = json.loads(with_next(recall_db, plan.id, worker, "Work was assigned.").split("\n", 1)[1])

    assert KEY not in result and KEY not in facts
    assert looked_up == []  # no lookup cost while off
