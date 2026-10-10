"""plan_read(thread_id=...): another thread's plan, read-only, under the one access rule.

A reader in the same project gets the same three reads the owner gets, with
every follow-up read addressed back to that plan and the owner named. Another
user, another project, a thread that moved away and a thread that does not exist
are refused alike; a thread with no plan answers ``plan: null``, and no read
creates a plan (#1552, R1-R7 of
``shared/planning/plans-read-another-plan-2026-09-24.md``).
"""

from __future__ import annotations

import json

import pytest

from plans import service
from plans.boundary import PlanActor, read_current_plan
from plans.current import addressed_to, read_current_status
from tools import plan_read_events
from tests.test_current_plan_bounded_read import _large_plan, _tool_env
from tests.test_current_plan_delivery import _session, db  # noqa: F401

READER = PlanActor("reader", "user-1", "project-a")


def _threads(db, *, owner_project="project-a", owner_user="user-1"):
    for project in ("project-a", "project-b"):
        db.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)",
            (project, project, "t", "t"),
        )
    _session(db, "reader", project_id="project-a")
    _session(db, "owner", user_id=owner_user, project_id=owner_project)
    db.execute("UPDATE threads SET title='Infrastructure' WHERE id='owner'")
    db.commit()


def _plans(db) -> int:
    return db.execute("SELECT COUNT(*) FROM plans WHERE owner_kind='thread'").fetchone()[0]


def test_a_same_project_status_read_is_addressed_to_the_owner_plan(db):
    _threads(db)
    plan = service.ensure_plan("thread", "owner", db=db)
    task = service.add_node(plan.id, kind="task", title="Fix the dev test", acceptance_criteria="Green", db=db)
    db.commit()

    page = read_current_status(actor=READER, thread_id="owner", db=db)
    read = addressed_to(page, "owner", db=db)

    assert read["plan"]["id"] == plan.id
    assert read["owner"] == {"thread_id": "owner", "label": "Infrastructure"}
    row = read["current"]["rows"][0]
    assert row["id"] == task.id
    assert row["detail_read"] == f"plan_read(thread_id='owner', node_id='{task.id}')"
    assert read["full_read"] == "plan_read(thread_id='owner', mode='full')"
    assert _plans(db) == 1


def test_a_node_and_full_read_of_another_plan(db):
    _threads(db)
    plan = service.ensure_plan("thread", "owner", db=db)
    task = service.add_node(plan.id, kind="task", title="Fix", acceptance_criteria="Green", db=db)
    db.commit()

    detail = read_current_status(actor=READER, thread_id="owner", node_id=task.id, db=db)
    assert detail["acceptance_criteria"] == "Green"
    full = read_current_plan(actor=READER, thread_id="owner", db=db)
    assert [node["id"] for node in full["nodes"]] == [task.id]


def test_a_foreign_cursor_continues_only_that_plan(db):
    _large_plan(db)  # the "owner" thread's plan, large enough to page
    _session(db, "reader", project_id=None)
    reader = PlanActor("reader", "user-1", None)
    service.ensure_plan("thread", "reader", db=db)
    db.commit()

    first = addressed_to(read_current_plan(actor=reader, thread_id="owner", db=db), "owner", db=db)
    assert first["continue_read"].startswith("plan_read(thread_id='owner', mode='full', cursor=")
    second = read_current_plan(actor=reader, thread_id="owner", cursor=first["cursor"], db=db)
    assert second["nodes"]
    # Without thread_id the cursor names a plan other than the reader's own.
    with pytest.raises(service.PlanError, match="stale"):
        read_current_plan(actor=reader, cursor=first["cursor"], db=db)


@pytest.mark.parametrize(
    "owner_user,owner_project",
    [("user-2", "project-a"), ("user-1", "project-b"), ("user-1", None)],
)
def test_another_user_or_project_is_refused(db, owner_user, owner_project):
    _threads(db, owner_user=owner_user, owner_project=owner_project)
    service.ensure_plan("thread", "owner", db=db)
    db.commit()

    with pytest.raises(PermissionError):
        read_current_status(actor=READER, thread_id="owner", db=db)
    with pytest.raises(PermissionError):
        read_current_plan(actor=READER, thread_id="owner", db=db)


def test_a_moved_thread_takes_its_plan_out_of_reach(db):
    _threads(db)
    service.ensure_plan("thread", "owner", db=db)
    db.commit()
    assert read_current_status(actor=READER, thread_id="owner", db=db) is not None

    db.execute("UPDATE threads SET project_id='project-b' WHERE id='owner'")
    db.commit()

    with pytest.raises(PermissionError):
        read_current_status(actor=READER, thread_id="owner", db=db)


def test_a_missing_thread_is_refused_like_one_out_of_reach(db):
    _threads(db)

    with pytest.raises(PermissionError, match="not accessible"):
        read_current_status(actor=READER, thread_id="no-such-thread", db=db)


def test_a_thread_with_no_plan_answers_null_and_creates_nothing(db):
    _threads(db)

    assert read_current_status(actor=READER, thread_id="owner", db=db) is None
    assert read_current_plan(actor=READER, thread_id="owner", db=db) is None
    assert addressed_to(None, "owner", db=db) == {
        "plan": None, "owner": {"thread_id": "owner", "label": "Infrastructure"},
    }
    assert _plans(db) == 0


def _reader_tool(db, monkeypatch):
    tools = _tool_env(db, monkeypatch)
    monkeypatch.setattr(
        tools, "_require_context",
        lambda _kwargs: ("reader", {"user_id": "user-1", "project_id": "project-a"}),
    )
    emitted = []
    monkeypatch.setattr(
        plan_read_events, "emit_product_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )
    return tools, emitted


@pytest.mark.asyncio
async def test_the_tool_reads_another_plan_and_says_whose(db, monkeypatch):
    _threads(db)
    plan = service.ensure_plan("thread", "owner", db=db)
    service.add_node(plan.id, kind="task", title="Fix", acceptance_criteria="Green", db=db)
    db.commit()
    tools, emitted = _reader_tool(db, monkeypatch)

    read = json.loads(await tools.PlanReadTool().execute(thread_id="owner"))

    assert read["owner"]["label"] == "Infrastructure"
    assert read["plan"]["id"] == plan.id
    (args, kwargs), = emitted
    assert args == ("plans", "plan_read")
    assert kwargs["properties"]["foreign"] is True
    assert kwargs["properties"]["mode"] == "status"
    assert "thread_id" in tools.PlanReadTool().parameters["properties"]
    # Ensuring creates a plan, which only its owner may do.
    assert "thread_id" not in tools.PlanEnsureTool().parameters["properties"]


@pytest.mark.asyncio
async def test_the_tool_refuses_out_of_reach_plainly(db, monkeypatch):
    _threads(db, owner_project="project-b")
    service.ensure_plan("thread", "owner", db=db)
    db.commit()
    tools, _ = _reader_tool(db, monkeypatch)

    reply = await tools.PlanReadTool().execute(thread_id="owner")

    assert reply == "Error: that thread's plan is outside your project"


@pytest.mark.asyncio
async def test_naming_yourself_is_the_ordinary_read(db, monkeypatch):
    _threads(db)
    plan = service.ensure_plan("thread", "reader", db=db)
    service.add_node(plan.id, kind="task", title="Mine", acceptance_criteria="Done", db=db)
    db.commit()
    tools, _ = _reader_tool(db, monkeypatch)

    own = await tools.PlanReadTool().execute()
    named = await tools.PlanReadTool().execute(thread_id="reader")

    def stable(reply):
        body = json.loads(reply)
        body.pop("as_of"), body["current"].pop("as_of"), body["recent"].pop("as_of")
        return body

    assert stable(named) == stable(own)
    assert "owner" not in json.loads(own)
