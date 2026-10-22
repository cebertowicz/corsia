"""delegate_send hands a reused worker a plan task only when the caller names it.

A send is often just a question, so nothing is attached by default; a send to
the caller's own idle worker says its reply arrives as chat. A named task that
cannot be handed over is refused before the message goes.
"""

from __future__ import annotations

import json

import pytest

from delegation_fixtures import deleg_db  # noqa: F401
from plans import service
from plans.delegate_attach import SEND_UNATTACHED

pytestmark = pytest.mark.asyncio

_PARENT = "parent-1"
_WORKER = "worker-1"


def _seed(db) -> str:
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,model,title,created_at,updated_at) "
        "VALUES (?,'u-test','proj-1','claude-code/opus','Parent','t','t')",
        (_PARENT,),
    )
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,spawned_by,model,title,"
        "created_at,updated_at) VALUES (?,'u-test','proj-1',?,'omlx@local/qwen',"
        "'Worker','t','t')",
        (_WORKER, _PARENT),
    )
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,model,title,created_at,updated_at) "
        "VALUES ('peer-1','u-test','proj-1','omlx@local/qwen','Peer','t','t')"
    )
    db.commit()
    return service.ensure_plan("thread", _PARENT, db=db).id


def _task(db, plan_id: str, title: str, *, state: str = "active") -> str:
    node = service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria=f"{title} done", db=db,
    )
    service.transition_node(node.id, "active", db=db)
    if state != "active":
        service.transition_node(node.id, state, db=db)
    db.commit()
    return node.id


def _attach(db, node_id: str, worker: str = _WORKER) -> None:
    from plans.delegation import attach_spawned_thread

    attach_spawned_thread(node_id, worker, actor_id=_PARENT, db=db)
    db.commit()


@pytest.fixture
def sent(deleg_db, monkeypatch):
    """Record every delivered message; the real send queues it for the worker."""
    delivered: list[tuple[str, str]] = []

    async def _send(thread_id, _row, text, **_kwargs):
        deleg_db.execute(
            "INSERT INTO messages (thread_id,role,content,seq,input_order,"
            "delivery_state,created_at) VALUES (?,'user',?,1,1,'queued','t')",
            (thread_id, text),
        )
        deleg_db.commit()
        delivered.append((thread_id, text))
        return type("Sent", (), {"driver": "turn_engine", "duplicate": False})()

    monkeypatch.setattr("tools.delegation.send_thread_input", _send)
    return delivered


async def _send_to(target: str = _WORKER, **kwargs) -> str:
    from tools.delegate_send import DelegateSendTool

    return await DelegateSendTool().execute(
        target, "one more thing", _thread_id=_PARENT, **kwargs,
    )


def _worker_of(db, node_id: str):
    return db.execute(
        "SELECT spawned_thread_id FROM plan_nodes WHERE id=?", (node_id,),
    ).fetchone()["spawned_thread_id"]


async def test_naming_a_task_hands_it_to_a_worker_whose_step_settled(deleg_db, sent):
    plan_id = _seed(deleg_db)
    earlier = _task(deleg_db, plan_id, "First job")
    _attach(deleg_db, earlier)
    service.transition_node(earlier, "failed", db=deleg_db)
    deleg_db.commit()
    later = _task(deleg_db, plan_id, "Second job")

    receipt = json.loads(await _send_to(plan_node_id=later))

    assert sent == [(_WORKER, "one more thing\n\n" + _line("Second job", later))]
    assert _worker_of(deleg_db, later) == _WORKER
    assert receipt["plan_task"]["attached"] is True
    assert receipt["plan_task"]["node_id"] == later


async def test_a_send_failure_leaves_the_named_task_attached_for_retry(
    deleg_db, monkeypatch,
):
    plan_id = _seed(deleg_db)
    named = _task(deleg_db, plan_id, "Retry this job")

    async def _fail(*args, **kwargs):
        return "Error: transport failed"

    monkeypatch.setattr("tools.delegate_send._send", _fail)

    result = await _send_to(plan_node_id=named, idempotency_key="retry-key")

    assert result == "Error: transport failed"
    assert _worker_of(deleg_db, named) == _WORKER


async def test_retrying_the_same_named_task_reaches_idempotent_send(
    deleg_db, monkeypatch,
):
    plan_id = _seed(deleg_db)
    named = _task(deleg_db, plan_id, "Retry this job")
    calls: list[str | None] = []

    async def _send(thread_id, _row, text, **kwargs):
        calls.append(kwargs.get("idempotency_key"))
        return type(
            "Sent", (), {"driver": "turn_engine", "duplicate": len(calls) > 1},
        )()

    monkeypatch.setattr("tools.delegation.send_thread_input", _send)

    first = json.loads(await _send_to(plan_node_id=named, idempotency_key="retry-key"))
    second = json.loads(await _send_to(plan_node_id=named, idempotency_key="retry-key"))

    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert calls == ["retry-key", "retry-key"]
    assert _worker_of(deleg_db, named) == _WORKER


async def test_no_task_named_attaches_nothing_and_says_the_reply_is_chat(deleg_db, sent):
    plan_id = _seed(deleg_db)
    current = _task(deleg_db, plan_id, "My own work")

    receipt = json.loads(await _send_to())

    assert sent == [(_WORKER, "one more thing")]
    assert _worker_of(deleg_db, current) is None
    assert receipt["plan_task"] == {
        "attached": False, "note": SEND_UNATTACHED.format(child=_WORKER),
    }


async def test_a_worker_on_an_open_task_gets_no_note(deleg_db, sent):
    plan_id = _seed(deleg_db)
    _attach(deleg_db, _task(deleg_db, plan_id, "Its job"))

    receipt = json.loads(await _send_to())

    assert "plan_task" not in receipt


async def test_an_accepted_send_to_its_attached_step_answers_worker_needs(deleg_db, sent, monkeypatch):
    """RL4: only the post-receipt reuse path calls Plans, with production db shape."""
    plan_id = _seed(deleg_db)
    step = _task(deleg_db, plan_id, "Needs the parent")
    _attach(deleg_db, step)
    called = {}

    def answer(parent, child, node, *, mutation_id, db=None):
        called.update(parent=parent, child=child, node=node, mutation_id=mutation_id, db=db)
        return 1

    monkeypatch.setattr("plans.public.answer_worker_needs", answer)

    receipt = json.loads(await _send_to(plan_node_id=step, idempotency_key="answer-needs"))

    assert receipt["needs_cleared"] == 1
    assert called == {
        "parent": _PARENT, "child": _WORKER, "node": step,
        "mutation_id": "answer-needs", "db": None,
    }


async def test_a_failed_answer_still_reports_the_delivered_send(deleg_db, sent, monkeypatch):
    """A delivered message is never reported as an error, so the parent doesn't resend."""
    plan_id = _seed(deleg_db)
    step = _task(deleg_db, plan_id, "Needs the parent")
    _attach(deleg_db, step)

    def answer(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr("plans.public.answer_worker_needs", answer)

    receipt = json.loads(await _send_to(plan_node_id=step, idempotency_key="answer-needs"))

    assert receipt["needs_cleared"] == 0
    assert "still open" in receipt["needs_note"]


@pytest.mark.parametrize("case", ["refused", "no_step", "new_step"])
async def test_rl4_only_an_accepted_send_to_the_attached_step_answers_needs(
    deleg_db, sent, monkeypatch, case,
):
    plan_id = _seed(deleg_db)
    existing = _task(deleg_db, plan_id, "Needs the parent")
    calls = []

    def answer(*args, **kwargs):
        calls.append((args, kwargs))
        return 0

    monkeypatch.setattr("plans.public.answer_worker_needs", answer)
    if case == "refused":
        _attach(deleg_db, existing)

        async def _fail(*args, **kwargs):
            return "Error: transport failed"

        monkeypatch.setattr("tools.delegate_send._send", _fail)
        result = await _send_to(plan_node_id=existing)
        assert result == "Error: transport failed"
    elif case == "no_step":
        _attach(deleg_db, existing)
        await _send_to()
    else:
        new_step = _task(deleg_db, plan_id, "New work")
        await _send_to(plan_node_id=new_step)

    assert calls == []


@pytest.mark.parametrize("case", ["finished", "other_worker", "holds_open_task", "not_mine"])
async def test_a_task_that_cannot_be_handed_over_is_refused_before_sending(
    deleg_db, sent, case,
):
    plan_id = _seed(deleg_db)
    target = _WORKER
    named = _task(deleg_db, plan_id, "Named job")
    if case == "finished":
        named = _task(deleg_db, plan_id, "Done job", state="failed")
    elif case == "other_worker":
        deleg_db.execute(
            "INSERT INTO threads (id,user_id,project_id,spawned_by,model,title,"
            "created_at,updated_at) VALUES ('worker-2','u-test','proj-1',?,"
            "'omlx@local/qwen','Worker 2','t','t')",
            (_PARENT,),
        )
        deleg_db.commit()
        _attach(deleg_db, named, "worker-2")
    elif case == "holds_open_task":
        _attach(deleg_db, _task(deleg_db, plan_id, "Its job"))
    else:
        target = "peer-1"

    result = await _send_to(target, plan_node_id=named)

    assert result.startswith("Error: invalid request"), result
    assert sent == []


def _line(title: str, node_id: str) -> str:
    return (
        f'Your plan task: "{title}" ({node_id}). Read its intent and acceptance criteria with '
        f"plan_read(thread_id='{_PARENT}', node_id='{node_id}') before you start, and again "
        "if you're told it changed."
    )


async def test_a_handed_task_is_named_by_reference_and_the_child_can_read_it(deleg_db, sent):
    from tools.plans import PlanReadTool

    plan_id = _seed(deleg_db)
    node_id = _task(deleg_db, plan_id, "Second job")

    await _send_to(plan_node_id=node_id)

    text = sent[0][1]
    assert _line("Second job", node_id) in text
    assert "Second job done" not in text  # criteria are read, never copied
    read = json.loads(await PlanReadTool().execute(
        node_id=node_id, thread_id=_PARENT, _thread_id=_WORKER,
    ))
    assert read["acceptance_criteria"] == "Second job done"


async def test_a_send_with_no_task_is_unchanged(deleg_db, sent):
    _seed(deleg_db)

    await _send_to()

    assert sent == [(_WORKER, "one more thing")]
