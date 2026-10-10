"""The roll-up builds each node's dict without ``dataclasses.asdict``'s recursive walk.

``PlanNode.as_dict`` must return exactly what ``asdict`` returns, and the polling
reads built on the roll-up (the chat list's thread states, the thread rollup)
must return identical payloads whichever one produces the node dicts.
"""

from __future__ import annotations

import re
from dataclasses import asdict, fields

import pytest

from plans import mutations, operator_chat_state, operator_projection, rollup, service
from plans.boundary import PlanActor
from plans.models import PlanNode
from tests.plan_close_next_helpers import HUMAN, plan_update, revision, task
from tests.plan_execution_helpers import agent_pause
from tests.test_plan_chat_list_state import _activate, _awaiting_user_review, _plan, _thread
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401 - fixture


def _scene(db) -> list[str]:
    """Threads whose plans are active, waiting, blocked, failed, archived, paused and delegated."""
    moving = _thread(db, "moving")
    plan = _plan(db, "moving")
    first = task(db, plan.id, "Build it")
    _activate(db, plan.id, first.id, moving)
    gated = task(db, plan.id, "Gated")
    service.add_dependency(gated.id, first.id, db=db)

    waiting = _thread(db, "waiting")
    plan = _plan(db, "waiting")
    held = task(db, plan.id, "Held")
    agent_pause(db, held.id, "waiting", thread_id="waiting")
    task(db, plan.id, "Behind it")

    _thread(db, "blocked")
    _awaiting_user_review(db, _plan(db, "blocked").id)

    failing = _thread(db, "failed")
    plan = _plan(db, "failed")
    doomed = task(db, plan.id, "Doomed")
    _activate(db, plan.id, doomed.id, failing)
    plan_update(db, plan.id, "fail", {
        "node_id": doomed.id, "expected_revision": revision(db, doomed.id),
    }, actor=failing)

    archiving = _thread(db, "archived")
    plan = _plan(db, "archived")
    done = task(db, plan.id, "Done")
    _activate(db, plan.id, done.id, archiving)
    plan_update(db, plan.id, "request_review", {
        "node_id": done.id, "evidence": "e", "expected_revision": revision(db, done.id),
    }, actor=archiving)
    plan_update(db, plan.id, "accept", {
        "node_id": done.id, "verification_result": "passed",
        "expected_revision": revision(db, done.id),
    }, actor=archiving)
    plan_update(db, plan.id, "archive_node", {
        "node_id": done.id, "expected_revision": revision(db, done.id),
    }, actor=archiving)
    task(db, plan.id, "Still open")

    _thread(db, "paused")
    plan = _plan(db, "paused")
    task(db, plan.id, "Later")
    plan_update(db, plan.id, "pause", {
        "expected_revision": service._plan_row(db, plan.id)["revision"],
    }, actor=HUMAN)

    parent = _thread(db, "parent")
    plan = _plan(db, "parent")
    step = task(db, plan.id, "Delegated step")
    _activate(db, plan.id, step.id, parent)
    db.execute("UPDATE threads SET spawned_by='parent' WHERE id='worker-a'")
    db.commit()
    child = service.ensure_plan("thread", "worker-a", db=db)
    task(db, child.id, "Child step")
    mutations.attach_spawned_thread(step.id, "worker-a", db=db)
    return ["moving", "waiting", "blocked", "failed", "archived", "paused", "parent", "worker-a"]


@pytest.fixture
def scene(db):
    db.execute("INSERT OR IGNORE INTO threads (id,user_id,model,created_at,updated_at) "
               "VALUES ('worker-a','user','test/model','t','t')")
    db.commit()
    return _scene(db)


def test_as_dict_is_asdict_for_every_node(db, scene):
    nodes = db.execute("SELECT id FROM plan_nodes").fetchall()
    assert len(nodes) > 8
    assert any(service.get_node(row["id"], db=db).unmet_dependencies for row in nodes)
    for row in nodes:
        node = service.get_node(row["id"], db=db)
        assert node.as_dict() == asdict(node)
        assert list(node.as_dict()) == list(asdict(node))


def test_as_dict_copies_the_dependency_list(db, scene):
    node = service.get_node(db.execute("SELECT id FROM plan_nodes").fetchone()["id"], db=db)
    copied = node.as_dict()
    copied["unmet_dependencies"].append("x")
    assert "x" not in node.unmet_dependencies


def test_as_dict_covers_every_mutable_field():
    """A mutable field added to PlanNode must be copied in ``as_dict``."""
    mutable = [
        field.name for field in fields(PlanNode)
        if str(field.type).startswith(("list", "dict", "set"))
    ]
    assert mutable == ["unmet_dependencies"]


def test_polling_reads_return_identical_payloads(db, scene, monkeypatch):
    def reads():
        states = operator_chat_state.read_thread_states(scene, actor=HUMAN, db=db)
        rolled = [rollup.rollup_thread(plan["id"], db=db) for plan in
                  map(dict, db.execute("SELECT * FROM plans WHERE owner_kind='thread'"))]
        return states, rolled

    fast = reads()
    monkeypatch.setattr(PlanNode, "as_dict", lambda self: asdict(self))
    assert reads() == fast
    states = fast[0]["states"]
    assert {"moving", "paused", "blocked_on_you"} <= {value["state"] for value in states.values()}


def test_chat_list_batches_child_plan_reads(db, scene):
    """One chat-list request reads summaries for several workers in set queries."""
    parent = PlanActor("parent", "user", None)
    parent_plan = _plan(db, "parent")
    child_steps = []
    for index in range(3):
        child_id = f"batched-worker-{index}"
        _thread(db, child_id)
        db.execute("UPDATE threads SET spawned_by='parent' WHERE id=?", (child_id,))
        db.commit()
        child_plan = _plan(db, child_id)
        child_steps.append((child_plan, task(db, child_plan.id, "Child step"), child_id))
        delegated = task(db, parent_plan.id, "Delegated step")
        _activate(db, parent_plan.id, delegated.id, parent)
        mutations.attach_spawned_thread(delegated.id, child_id, db=db)

    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        states = operator_chat_state.read_thread_states(["parent"], actor=HUMAN, db=db)
    finally:
        db.set_trace_callback(None)

    assert "parent" in states["states"]
    child_queries = [
        statement for statement in statements
        if "SELECT n.id AS attached_node_id,p.* FROM plan_nodes n" in statement
    ]
    child_plan_ids = [str(child_plan.id) for child_plan, _step, _id in child_steps]
    # The rollup reads each shown child's open rows once (#731 S2-2: a plan's open work,
    # not the plan); admission's own single-plan walks are not this read.
    tree_queries = [
        statement for statement in statements if _open_read_of(statement) in child_plan_ids
    ]
    # ``fetch_all`` prepares then executes the one packed result query.
    assert len(child_queries) == 2
    assert sorted(_open_read_of(statement) for statement in tree_queries) == sorted(child_plan_ids)

    child_plan, child_step, child_id = child_steps[0]
    _activate(db, child_plan.id, child_step.id, PlanActor(child_id, "user", None))
    fresh = operator_chat_state.read_thread_states(["parent"], actor=HUMAN, db=db)
    assert fresh["states"]["parent"]["state"] == "moving"


_OPEN_READ = re.compile(
    r"^WITH q\(.*AS \(SELECT \* FROM plan_nodes WHERE plan_id='(\w+)' "
    r"AND plan_nodes\.archived_at IS NULL AND folded=0\)",
    re.DOTALL,
)


def _open_read_of(statement: str) -> str | None:
    """The plan whose open rows this statement reads (``open_tree.load``), if it is that read.

    ``fetch_all`` first prepares each read with a ``LIMIT 0`` probe; only the packed read counts.
    """
    found = _OPEN_READ.search(statement)
    return found.group(1) if found else None


def _tree_reads_of(db, plan_id: str, read) -> list[str]:
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        read()
    finally:
        db.set_trace_callback(None)
    # ``fetch_all`` first prepares each read with a ``LIMIT 0`` probe for its
    # column names; the probe reads no rows, so it is not a read of the tree.
    return [statement for statement in statements if _open_read_of(statement) == plan_id]


@pytest.mark.parametrize("read", [
    lambda db: operator_chat_state.read_thread_states(["parent"], actor=HUMAN, db=db),
    lambda db: operator_projection.read_thread("parent", actor=HUMAN, db=db),
], ids=["chat_list", "pane"])
def test_a_request_reads_a_delegated_child_plan_once(db, scene, read):
    """The operator envelope reuses the plans the rollup already walked.

    The rollup reads a delegated step's child tree to project its state; the
    envelope's admission read of the same step asks for that state again and
    must find it in the request's memo, not walk the child a second time.
    """
    child_plan_id = str(service.ensure_plan("thread", "worker-a", db=db).id)

    reads = _tree_reads_of(db, child_plan_id, lambda: read(db))

    assert len(reads) == 1, reads
