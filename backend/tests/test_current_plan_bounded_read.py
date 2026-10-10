"""Full plan_read/plan_ensure: identity and hierarchy only, paged, revision-aware.

``mode='full'`` never carries node bodies (``plan_read(node_id=...)`` does), is
cut into byte-capped pages in depth-first order with a cursor bound to the plan
revision, and ``known_revision`` turns an unchanged read into a one-line reply.
Reuses ``test_current_plan_delivery``'s ``db`` fixture and session/proxy helpers
rather than duplicating them.
"""

from __future__ import annotations

import json

import pytest

from plans import current, node_order, service, status_cursor
from plans.boundary import PlanActor, ensure_current_plan, read_current_plan
from plans.close_next_bounds import json_bytes
from plans.current import read_current_status
from tests.test_current_plan_delivery import _DbProxy, _session, db  # noqa: F401

ACTOR = PlanActor("owner", "user-1", None)
DESCRIPTION = "Context for this step: scope, surfaces, and constraints. " * 11
CRITERIA = "Observable evidence: focused tests pass and the diff is read. " * 9
IDENTITY_FIELDS = (
    "id", "plan_id", "parent_id", "order_key", "kind", "title", "state",
    "unmet_dependencies", "held_by", "assignee", "spawn_kind", "owner_thread_id", "executor_thread_id",
    "spawned_thread_id", "follow_up_of", "revision",
)


def _large_plan(db, *, phases: int = 6, tasks: int = 9):
    """A plan shaped like the largest production plan: phases of body-heavy tasks."""
    _session(db, "owner")
    plan = service.ensure_plan("thread", "owner", db=db)
    for p in range(phases):
        phase = service.add_node(
            plan.id, kind="phase", title=f"Release phase {p} delivery track",
            acceptance_criteria=CRITERIA, description=DESCRIPTION, db=db,
        )
        for t in range(tasks):
            service.add_node(
                plan.id, kind="task", title=f"Phase {p} task {t}: implement the slice",
                acceptance_criteria=CRITERIA, description=DESCRIPTION,
                parent_id=phase.id, db=db,
            )
    db.commit()
    return plan


def _depth_first_ids(db, plan_id: str) -> list[str]:
    return [row["id"] for row in node_order.ordered_nodes(db, plan_id)]


def _pages(read) -> list[dict]:
    pages = [read(None)]
    while "cursor" in pages[-1]:
        pages.append(read(pages[-1]["cursor"]))
    return pages


def test_full_read_omits_bodies_and_keeps_identity_hierarchy_ownership(db):
    _session(db, "owner")
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Body-bearing node",
        acceptance_criteria="Long acceptance criteria body.",
        description="Long description body.", db=db,
    )

    read = read_current_plan(actor=ACTOR, db=db)

    shown = read["nodes"][0]
    assert "acceptance_criteria" not in shown
    assert "description" not in shown
    stored = service.get_node(node.id, db=db)
    for field in IDENTITY_FIELDS:
        assert shown[field] == getattr(stored, field), field
    assert read["omitted_count"] == 0
    assert read["total_count"] == 1
    assert "cursor" not in read and "continue_read" not in read


def test_full_read_pages_by_bytes_in_depth_first_order(db):
    plan = _large_plan(db)

    pages = _pages(lambda cursor: read_current_plan(actor=ACTOR, cursor=cursor, db=db))

    assert len(pages) >= 2
    assert [n["id"] for page in pages for n in page["nodes"]] == _depth_first_ids(db, plan.id)
    shown = 0
    for page in pages:
        assert json_bytes(page) <= current.PAGE_BYTES
        assert page["total_count"] == 60
        shown += len(page["nodes"])
        assert page["omitted_count"] == 60 - shown
        if page["omitted_count"]:
            assert page["continue_read"] == f"plan_read(mode='full', cursor='{page['cursor']}')"
    assert "cursor" not in pages[-1]


def test_full_cursor_is_refused_stale_once_the_plan_revision_moves(db):
    plan = _large_plan(db)
    cursor = read_current_plan(actor=ACTOR, db=db)["cursor"]

    service.add_node(plan.id, kind="task", title="Late addition", acceptance_criteria="Done", db=db)
    db.commit()

    with pytest.raises(service.PlanError, match="stale") as refused:
        read_current_plan(actor=ACTOR, cursor=cursor, db=db)
    assert "mode='full'" in str(refused.value)


def test_a_status_forged_or_foreign_cursor_is_refused_by_the_full_read(db):
    plan = _large_plan(db, phases=12)
    status_cursor_value = read_current_status(actor=ACTOR, db=db)["current"]["cursor"]
    full_cursor = read_current_plan(actor=ACTOR, db=db)["cursor"]
    beyond = status_cursor.decode(full_cursor, plan_id=plan.id, section=status_cursor.FULL)
    beyond["o"] = 10_000

    for cursor in (status_cursor_value, "not-a-cursor", status_cursor.encode(beyond)):
        with pytest.raises(service.PlanError, match="stale"):
            read_current_plan(actor=ACTOR, cursor=cursor, db=db)
    with pytest.raises(service.PlanError, match="stale"):
        read_current_status(actor=ACTOR, section="current", cursor=full_cursor, db=db)


def test_a_full_read_of_an_empty_or_missing_plan(db):
    _session(db, "owner")

    assert read_current_plan(actor=ACTOR, db=db) is None
    ensured = ensure_current_plan(actor=ACTOR, db=db)
    assert ensured["nodes"] == []
    assert ensured["omitted_count"] == 0 and ensured["total_count"] == 0


def test_known_revision_equal_to_the_plan_revision_returns_only_unchanged(db):
    plan = _large_plan(db, phases=1, tasks=2)
    revision = service._plan_row(db, plan.id)["revision"]
    unchanged = {
        "unchanged": True,
        "plan": {"id": plan.id, "revision": revision, "state": "active"},
    }

    assert read_current_plan(actor=ACTOR, known_revision=revision, db=db) == unchanged
    assert ensure_current_plan(actor=ACTOR, known_revision=revision, db=db) == unchanged
    assert "nodes" in read_current_plan(actor=ACTOR, known_revision=revision - 1, db=db)


def test_known_revision_never_answers_the_status_read(db):
    """The plan revision cannot prove the status is unchanged.

    Selection and release write ``plan_node_attempts`` alone, which no trigger
    counts, so an unchanged revision is compatible with the current task having
    been released. Answering 'unchanged' there would report a status the caller
    no longer has; the node page it does answer is bound to node writes.
    """
    plan = _large_plan(db, phases=1, tasks=2)
    revision = service._plan_row(db, plan.id)["revision"]

    with pytest.raises(service.PlanInputError, match="known_revision"):
        read_current_status(actor=ACTOR, known_revision=revision, db=db)


def test_known_revision_cannot_narrow_a_node_read_or_continuation(db):
    plan = _large_plan(db)
    revision = service._plan_row(db, plan.id)["revision"]
    cursor = read_current_plan(actor=ACTOR, db=db)["cursor"]
    node_id = _depth_first_ids(db, plan.id)[0]

    with pytest.raises(service.PlanInputError, match="known_revision"):
        read_current_plan(actor=ACTOR, cursor=cursor, known_revision=revision, db=db)
    with pytest.raises(service.PlanInputError, match="known_revision"):
        read_current_status(actor=ACTOR, node_id=node_id, known_revision=revision, db=db)


def _tool_env(db, monkeypatch):
    from tools import plans as tool_plans

    proxy = _DbProxy(db)

    async def immediate(callable_):
        return callable_()

    monkeypatch.setattr(
        tool_plans, "_require_context",
        lambda _kwargs: ("owner", {"user_id": "user-1", "project_id": None}),
    )
    monkeypatch.setattr(tool_plans, "get_db", lambda: proxy)
    monkeypatch.setattr(tool_plans, "run_in_db", immediate)
    return tool_plans


@pytest.mark.asyncio
async def test_tool_full_read_is_body_free_and_bounded_is_gone(db, monkeypatch):
    plan = _large_plan(db, phases=1, tasks=2)
    tools = _tool_env(db, monkeypatch)
    read = tools.PlanReadTool()

    full = json.loads(await read.execute(mode="full"))
    assert [n["id"] for n in full["nodes"]] == _depth_first_ids(db, plan.id)
    assert all("acceptance_criteria" not in n and "description" not in n for n in full["nodes"])
    # The old `bounded` parameter is gone, not quietly accepted: mode='full' is
    # the one way to ask for the whole plan without bodies.
    assert "bounded" not in read.parameters["properties"]
    assert any(
        "bounded" in error for error in read.validate_params({"bounded": True})
    )


@pytest.mark.asyncio
async def test_tool_pages_and_honours_known_revision(db, monkeypatch):
    plan = _large_plan(db)
    tools = _tool_env(db, monkeypatch)
    revision = service._plan_row(db, plan.id)["revision"]

    async def full(cursor):
        return json.loads(await tools.PlanReadTool().execute(mode="full", cursor=cursor))

    first = await full(None)
    second = await full(first["cursor"])
    assert [n["id"] for n in first["nodes"] + second["nodes"]] == (
        _depth_first_ids(db, plan.id)[: len(first["nodes"]) + len(second["nodes"])]
    )
    unchanged = {"unchanged": True, "plan": {"id": plan.id, "revision": revision, "state": "active"}}
    for tool in (tools.PlanReadTool(), tools.PlanEnsureTool()):
        reply = await tool.execute(mode="full", known_revision=revision)
        assert json.loads(reply) == unchanged
        assert tool.parameters["properties"]["known_revision"]["type"] == "integer"
        # The status read cannot be answered from the plan revision, so it says
        # so instead of reporting a status the caller may no longer hold.
        status = await tool.execute(known_revision=revision)
        assert status.startswith("Error:") and "mode='full'" in status
    refused = await tools.PlanReadTool().execute(node_id=plan.id, known_revision=revision)
    assert refused.startswith("Error:") and "known_revision" in refused
