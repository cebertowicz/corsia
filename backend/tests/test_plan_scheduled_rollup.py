"""A node is quietly scheduled by the same bottom-up roll-up that reads every parent.

``scheduled_until`` on each presented node is the soonest end among the
agent-scheduled waits beneath it, set only when ALL of its open work is such a
wait and nothing on or under it needs a person. The browser only compares that
one field with the clock; it walks no subtree.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
import uuid

import pytest

from infra.schema import SCHEMA_SQL
from plans import operator_projection, service
from plans.boundary import PlanActor, mutate

OWNER = PlanActor("owner", "user", "project")
HUMAN = PlanActor(None, "user", None, kind="user")


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','Project','t','t')"
    )
    connection.execute(
        """INSERT INTO threads (id,user_id,project_id,model,created_at,updated_at)
           VALUES ('owner','user','project','test/model','t','t')"""
    )
    connection.commit()
    yield connection
    connection.close()


def _at(**delta) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(**delta)).replace(microsecond=0)


class Tree:
    def __init__(self, db):
        self.db = db
        self.plan = service.ensure_plan("thread", "owner", db=db)

    def add(self, title: str, *, kind: str = "task", parent=None, **kwargs):
        node = service.add_node(
            self.plan.id, kind=kind, title=title, acceptance_criteria=f"{title} done",
            parent_id=parent.id if parent else None, db=self.db, **kwargs,
        )
        self.db.commit()
        return node

    def schedule(self, node, when: datetime) -> None:
        mutate(
            self.plan.id, "arm_watch",
            {
                "node_id": node.id, "reason": "Revisit later",
                "condition": {"type": "deadline", "value": when.isoformat()},
                "expected_revision": service._node_row(self.db, node.id)["revision"],
            },
            actor=OWNER, provenance="test.scheduled_rollup", mutation_id=uuid.uuid4().hex, db=self.db,
        )
        self.db.commit()

    def settle(self, node) -> None:
        service.transition_node(node.id, "closed", db=self.db)
        self.db.commit()

    def accept(self, node) -> None:
        """Mark a task done, as acceptance leaves it: a settled child that stays a leaf of its parent."""
        self.db.execute("UPDATE plan_nodes SET state='accepted' WHERE id=?", (node.id,))
        self.db.commit()

    def until(self) -> dict[str, str | None]:
        envelope = operator_projection.read_thread("owner", actor=HUMAN, db=self.db)
        return {node["title"]: node["scheduled_until"] for node in envelope["plans"][0]["nodes"]}


def _same(value: str | None, expected: datetime) -> bool:
    return value is not None and datetime.fromisoformat(value) == expected


def test_a_leaf_scheduled_by_its_agent_carries_its_end(db):
    tree = Tree(db)
    leaf, plain = tree.add("Leaf"), tree.add("Plain")
    end = _at(days=3)
    tree.schedule(leaf, end)
    until = tree.until()
    assert _same(until["Leaf"], end)
    assert until["Plain"] is None


def test_a_wait_inside_the_horizon_still_names_its_end(db):
    # The 24-hour horizon is the browser's comparison against its clock, so the
    # field carries the end whatever it is; a wait with no reason carries none.
    tree = Tree(db)
    soon = tree.add("Soon")
    end = _at(hours=23)
    tree.schedule(soon, end)
    assert _same(tree.until()["Soon"], end)


def test_a_parent_whose_only_open_leaf_is_scheduled_is_scheduled(db):
    tree = Tree(db)
    phase = tree.add("Phase", kind="phase")
    done = tree.add("Done", parent=phase)
    far = tree.add("Far", parent=phase)
    tree.settle(done)
    end = _at(days=4)
    tree.schedule(far, end)
    until = tree.until()
    assert _same(until["Phase"], end)
    assert _same(until["Far"], end)
    assert until["Done"] is None


def test_one_ready_leaf_keeps_the_parent_ordinary(db):
    tree = Tree(db)
    phase = tree.add("Phase", kind="phase")
    far, ready = tree.add("Far", parent=phase), tree.add("Ready", parent=phase)
    tree.schedule(far, _at(days=4))
    until = tree.until()
    assert until["Phase"] is None
    assert until["Far"] is not None
    assert until["Ready"] is None


def test_a_leaf_needing_the_user_keeps_the_parent_ordinary(db):
    tree = Tree(db)
    phase = tree.add("Phase", kind="phase")
    far = tree.add("Far", parent=phase)
    yours = tree.add("Yours", parent=phase, assignee="user")
    tree.schedule(far, _at(days=4))
    tree.schedule(yours, _at(days=5))
    until = tree.until()
    assert until["Yours"] is None
    assert until["Phase"] is None
    assert until["Far"] is not None


def test_a_parent_with_settled_children_is_scheduled_by_its_own_wait(db):
    tree = Tree(db)
    phase = tree.add("Phase", kind="phase")
    tree.settle(tree.add("Kid", parent=phase))
    tree.settle(tree.add("Kid two", parent=phase))
    end = _at(days=2)
    tree.schedule(phase, end)
    assert _same(tree.until()["Phase"], end)


def test_three_nested_levels_carry_the_soonest_end(db):
    tree = Tree(db)
    top = tree.add("Top", kind="phase")
    middle = tree.add("Middle", kind="phase", parent=top)
    bottom = tree.add("Bottom", kind="phase", parent=middle)
    late, early = tree.add("Late", parent=bottom), tree.add("Early", parent=middle)
    soonest = _at(days=2)
    tree.schedule(late, _at(days=9))
    tree.schedule(early, soonest)
    until = tree.until()
    assert _same(until["Top"], soonest)
    assert _same(until["Middle"], soonest)
    assert _same(until["Bottom"], _at(days=9))
    assert until["Late"] is not None


def test_a_parent_with_a_far_wait_but_open_unscheduled_work_is_ordinary(db):
    tree = Tree(db)
    phase = tree.add("Phase", kind="phase")
    tree.add("Ready", parent=phase)
    tree.schedule(phase, _at(days=2))
    assert tree.until()["Phase"] is None


def test_a_parent_whose_children_are_accepted_is_scheduled_by_its_own_wait(db):
    tree = Tree(db)
    phase = tree.add("Phase", kind="phase")
    tree.accept(tree.add("Kid", parent=phase))
    tree.accept(tree.add("Kid two", parent=phase))
    end = _at(days=2)
    tree.schedule(phase, end)
    until = tree.until()
    assert _same(until["Phase"], end)
    assert until["Kid"] is None
