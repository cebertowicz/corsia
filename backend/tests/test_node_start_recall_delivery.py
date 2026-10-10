"""Related past work reaches the agent through the real plan surfaces.

The real ``plan_update`` tool, on the real plan schema, carries the block when
a task starts and leaves it out when nothing related exists; a wake for work
someone else selected carries it beside its ``next`` block.
"""

from __future__ import annotations

import json

import pytest

from infra.database import get_db
from plans import service
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import (
    add_task,
    execute_as,
    finish_turn,
    open_turn,
    registry_of,
)
from tools.plans import PlanReadTool, PlanUpdateTool

KEY = "related_past_work"


@pytest.fixture
def recall_db(isolated_db):
    """The isolated schema plus the search indexes a fresh install creates."""
    from infra.db_fts import _init_fts

    db = get_db()
    _init_fts(db)
    db.commit()
    yield db
    db.close()


@pytest.fixture
def logged():
    from loguru import logger

    lines: list[str] = []
    sink = logger.add(lines.append, format="{message}", level="INFO")
    yield lines
    logger.remove(sink)


def _settled(db, thread_id: str, title: str) -> str:
    """Settle one task in ``thread_id``'s plan and return its id."""
    plan = service.ensure_plan("thread", thread_id, db=db)
    node = add_task(db, plan.id, title)
    db.execute("UPDATE plan_nodes SET state='accepted' WHERE id=?", (node.id,))
    db.commit()
    return node.id


def _settled_elsewhere(db, title: str) -> tuple[str, str]:
    """Settle one task in another owned thread's plan: (thread, node)."""
    past = mk_session("recall-past")
    return past, _settled(db, past, title)


async def _plan_read(db, thread_id: str, node_id: str) -> str:
    identity = open_turn(db, thread_id, turn_id="turn-read")
    return await execute_as(
        registry_of(PlanReadTool()), identity, thread_id, "plan_read",
        {"node_id": node_id},
    )


async def _activate(db, thread_id: str, title: str) -> dict:
    identity = open_turn(db, thread_id)
    plan = service.ensure_plan("thread", thread_id, db=db)
    task = add_task(db, plan.id, title)
    db.commit()
    raw = await execute_as(
        registry_of(PlanUpdateTool()), identity, thread_id, "plan_update",
        {
            "plan_id": plan.id, "mutation_id": "activate-1", "operation": "activate",
            "params": {"node_id": task.id, "expected_revision": task.revision},
        },
    )
    return json.loads(raw)


@pytest.mark.asyncio
async def test_activating_a_task_carries_the_settled_task_it_repeats(recall_db):
    db = recall_db
    thread, past = _settled_elsewhere(db, "Identify and fix missed idle checkpoints")

    result = await _activate(
        db, mk_session("recall-now"), "Missed idle checkpoints on quiet threads",
    )

    assert result["admission"]["condition"] == "running"
    assert past not in result[KEY][0]
    # plan_read cannot open another thread's node, and no read returns the node
    # itself, so the pointer names what get_thread_history does return: that
    # thread's messages (#2520).
    assert len(result[KEY]) == 1 and result[KEY][0].endswith(
        f'→ get_thread_history(thread_id="{thread}") — '
        "that thread's messages, not this node"
    )


@pytest.mark.asyncio
async def test_a_pointer_into_the_callers_own_plan_opens_with_plan_read(recall_db, logged):
    db = recall_db
    now = mk_session("recall-own")
    past = _settled(db, now, "Identify and fix missed idle checkpoints")

    result = await _activate(db, now, "Missed idle checkpoints on quiet threads")
    assert result[KEY][0].endswith(f'→ plan_read(node_id="{past}")')
    finish_turn(db, "turn-1")

    page = json.loads(await _plan_read(db, now, past))

    assert page["id"] == past and page["state"] == "accepted"
    assert sum("reason=opened" in line for line in logged) == 1


@pytest.mark.asyncio
async def test_a_pointer_into_another_thread_opens_with_its_history(recall_db, logged):
    from tools.query_projects import GetThreadHistoryTool

    db = recall_db
    thread, _past = _settled_elsewhere(db, "Identify and fix missed idle checkpoints")
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,created_at,seq,delivery_state) "
        "VALUES ('m-past', ?, 'assistant', 'Timer now survives passive input.', 't', 1, "
        "'delivered')",
        (thread,),
    )
    db.commit()
    result = await _activate(
        db, mk_session("recall-history"), "Missed idle checkpoints on quiet threads",
    )
    finish_turn(db, "turn-1")

    history = await execute_as(
        registry_of(GetThreadHistoryTool()), open_turn(db, "recall-history", "turn-h"),
        "recall-history", "get_thread_history", {"thread_id": thread},
    )

    assert f'get_thread_history(thread_id="{thread}")' in result[KEY][0]
    assert "Timer now survives passive input." in history
    assert sum("reason=opened" in line for line in logged) == 1


@pytest.mark.asyncio
async def test_a_failed_read_is_not_counted_as_opened(recall_db, logged):
    from memory.node_start_opened import remember

    db = recall_db
    _thread, past = _settled_elsewhere(db, "Identify and fix missed idle checkpoints")
    now = mk_session("recall-failed")
    service.ensure_plan("thread", now, db=db)
    db.commit()
    remember(now, past)

    text = await _plan_read(db, now, past)

    assert "not in this plan" in text
    assert not any("reason=opened" in line for line in logged)


@pytest.mark.asyncio
async def test_an_unrelated_task_starts_with_no_block(recall_db):
    db = recall_db
    _settled_elsewhere(db, "Identify and fix missed idle checkpoints")

    result = await _activate(db, mk_session("recall-quiet"), "Rename release notes heading")

    assert result["admission"]["condition"] == "running"
    assert KEY not in result


def test_a_wake_for_selected_work_carries_the_block(recall_db):
    from plans import admission
    from plans.plan_wake import with_next

    db = recall_db
    thread, past = _settled_elsewhere(db, "Identify and fix missed idle checkpoints")
    worker = mk_session("recall-worker")
    plan = service.ensure_plan("thread", worker, db=db)
    task = add_task(db, plan.id, "Missed idle checkpoints on quiet threads")
    admission.activate(
        task.id, expected_revision=task.revision, actor_id=worker,
        provenance="test", db=db,
    )
    db.commit()

    text = with_next(db, plan.id, worker, "Work was assigned.")
    facts = json.loads(text.split("\n", 1)[1])

    assert facts["next"]["selected"]["id"] == task.id
    assert past not in facts[KEY][0]
    assert facts[KEY][0].endswith(
        f'get_thread_history(thread_id="{thread}") — '
        "that thread's messages, not this node"
    )
