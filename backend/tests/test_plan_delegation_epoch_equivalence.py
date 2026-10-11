"""The attachment-epoch delegation reads answer exactly as the full reads did (#149 S2d).

Random histories of a delegated child plan, written through the real operations: adds
under containers, starts, reviews, accepts, failures, closes, send-backs, archives, a
reused worker detached and attached again, and a harvest recorded at random points. After
every step each epoch read is compared with the original whole-plan code, kept below
verbatim as the oracle: the same ids, the same booleans, the same steps, and the same
harvest decision (``_returned_since``). Failures print the seed and the operations.

``CORSIA_PLAN_PROPERTY_SEQUENCES`` raises the sequence count for a longer run.
"""

from __future__ import annotations

import os
import random
import sqlite3
from contextlib import contextmanager
from itertools import count

import pytest

from plans import delegation_epoch, delegation_harvest, mutations, node_states, projections, service
from tests.test_plan_aggregates_property import MAX_OPS, OPEN, World, _op

SEQUENCES = int(os.environ.get("CORSIA_PLAN_PROPERTY_SEQUENCES", "300"))


# -- the original whole-plan reads, as the oracle ---------------------------------------------

def old_return_rows(db, plan_id: str) -> list:
    leaves = service._plan_leaf_rows(db, plan_id)
    if leaves:
        return leaves
    return db.execute(
        """SELECT n.* FROM plan_nodes n
           WHERE n.plan_id=? AND n.state=? AND n.archived_at IS NULL
             AND NOT EXISTS (
               SELECT 1 FROM plan_nodes child
               WHERE child.parent_id=n.id AND child.state=?
                 AND child.archived_at IS NULL
             )
           ORDER BY n.order_key,n.id""",
        (plan_id, service.CLOSED, service.CLOSED),
    ).fetchall()


def old_waiting_on_answer(db, plan_id: str) -> bool:
    from plans import delegation_needs

    if delegation_needs.open_for(service._plan_row(db, plan_id), db=db):
        return True
    return db.execute(
        f"""SELECT 1 FROM plan_nodes w
           WHERE w.plan_id=? AND w.archived_at IS NULL AND w.assignee='user'
             AND w.state IN ('pending','active','review_requested')
             AND NOT {node_states.has_child_in_work_sql('w')}
           LIMIT 1""",
        (plan_id,),
    ).fetchone() is not None


def old_delegated_work_returned(db, plan_id: str) -> bool:
    steps = old_return_rows(db, plan_id)
    return bool(steps) and not old_waiting_on_answer(db, plan_id) and all(
        row["state"] not in ("pending", "active")
        and not delegation_harvest.awaiting_owner_verdict(db, row)
        for row in steps
    )


def old_open_ids(db, plan_id: str, limit: int = -1) -> tuple[str, ...]:
    return tuple(
        str(row["id"])
        for row in db.execute(
            f"""SELECT n.id FROM plan_nodes n
               WHERE n.plan_id=? AND {node_states.in_work_sql('n')}
                 AND n.state IN ('pending','active','review_requested')
                 AND NOT EXISTS (SELECT 1 FROM plan_nodes c
                      WHERE c.parent_id=n.id AND {node_states.in_work_sql('c')}
                        AND c.state IN ('pending','active','review_requested'))
                 AND NOT {node_states.is_container_sql('n')}
               ORDER BY n.order_key,n.created_at
               LIMIT ?""",
            (plan_id, limit),
        )
    )


@contextmanager
def original_reads():
    """The harvest's own readers with their whole-plan bodies, for the oracle side."""
    saved = (delegation_harvest.return_rows, delegation_harvest.open_leaves, delegation_harvest.open_row_sql,
             projections.open_row_sql)
    delegation_harvest.return_rows = lambda db, plan_id, since=None: old_return_rows(db, plan_id)
    delegation_harvest.open_leaves = service._plan_leaf_rows
    delegation_harvest.open_row_sql = projections.open_row_sql = lambda connection, alias: "1"
    try:
        yield
    finally:
        (delegation_harvest.return_rows, delegation_harvest.open_leaves, delegation_harvest.open_row_sql,
         projections.open_row_sql) = saved


# -- a delegated child plan under a driven clock ----------------------------------------------

class Delegation(World):
    """``World``'s plan, now the plan of a worker attached to a step of a parent's plan."""

    def __init__(self, rng: random.Random) -> None:
        super().__init__()
        self.rng = rng
        db = self.db
        db.execute(
            "INSERT INTO threads (id,user_id,project_id,title,created_at,updated_at)"
            " VALUES ('boss','user','p','Boss','t','t')")
        db.execute("UPDATE threads SET spawned_by='boss' WHERE id='owner'")
        parent = service.ensure_plan("thread", "boss", db=db)
        db.execute("UPDATE plans SET parent_plan_id=? WHERE id=?", (parent.id, self.plan.id))
        self.step_id = service.add_node(
            parent.id, kind="task", title="Delegate", acceptance_criteria="done", spawn_kind="claude-code", db=db).id
        service.transition_node(self.step_id, "active", db=db)
        self.attached = False
        self.epochs = 0

    def step_row(self):
        return service._node_row(self.db, self.step_id)

    def reattach(self) -> None:
        """A reused worker: detached from its step and attached again (a new epoch)."""
        if self.attached and self.rng.random() < 0.5:
            with service._write(self.db):
                service.touch_node(self.db, self.step_id, spawned_thread_id=None)
            self.attached = False
        if not self.attached:
            self.step("attach_owner", lambda: mutations.attach_spawned_thread(self.step_id, "owner", db=self.db))
            self.attached = self.step_row()["spawned_thread_id"] == "owner"
            self.epochs += self.attached

    def harvest(self) -> None:
        """Record the return the parent judges, as the harvest does."""
        if not self.attached:
            return
        with service._write(self.db):
            service._event(
                self.db, self.step_id, "child_plan_completed", actor_id=None,
                provenance="plan.delegation_harvest", payload={"child_plan_id": self.plan.id},
            )

    def compare(self) -> None:
        db, plan_id, log = self.db, self.plan.id, self.log
        assert delegation_epoch.open_delegated_node_ids(db, plan_id) == old_open_ids(db, plan_id), log
        assert delegation_epoch.open_delegated_node_ids(db, plan_id, limit=1) == old_open_ids(db, plan_id, 1), log
        assert delegation_epoch.delegated_work_returned(db, plan_id) == old_delegated_work_returned(db, plan_id), log
        assert delegation_harvest.waiting_on_answer(db, plan_id) == old_waiting_on_answer(db, plan_id), log
        assert projections.movable_work(db, plan_id) == bool(
            db.execute(
                f"SELECT 1 FROM plan_nodes w WHERE w.plan_id=? AND {projections.movable_leaf_sql('w')} LIMIT 1",
                (plan_id,)).fetchone()), log
        step = self.step_row()
        plan = service._plan_row(db, plan_id)
        with original_reads():
            old_reviews = delegation_harvest.parent_reviews(db, plan)
            old_returned = [str(row["id"]) for row in delegation_harvest.returned_steps(db, step)]
            old_since = delegation_harvest._returned_since(db, step, plan_id)
        assert delegation_harvest.parent_reviews(db, plan) == old_reviews, log
        assert [str(row["id"]) for row in delegation_harvest.returned_steps(db, step)] == old_returned, log
        assert delegation_harvest._returned_since(db, step, plan_id) == old_since, log
        since = delegation_epoch.attached_since(db, self.step_id)
        if since is not None:
            wanted = [
                str(row["id"]) for row in old_return_rows(db, plan_id)
                if row["state"] in OPEN or row["own_last_changed"] >= since
            ]
            assert [str(row["id"]) for row in delegation_epoch.return_rows(db, plan_id, since)] == wanted, log

    def check(self) -> None:
        super().check()
        self.compare()


@pytest.fixture
def clock(monkeypatch):
    """A clock that never runs backward and sometimes stands still, so same-instant writes occur."""
    ticks = count()
    state = {"now": 0, "rng": random.Random(0), "steps": (0, 1, 1, 2)}

    def now() -> str:
        next(ticks)
        state["now"] += state["rng"].choice(state["steps"])
        return f"2026-01-01T00:{state['now'] // 60:02d}:{state['now'] % 60:02d}+00:00"

    monkeypatch.setattr(service, "_now", now)
    return state


def run_sequence(seed: int, clock) -> Delegation:
    clock["now"], clock["rng"] = 0, random.Random(seed)
    rng = random.Random(seed)
    world = Delegation(rng)
    try:
        world.reattach()
        for _ in range(rng.randint(1, MAX_OPS)):
            roll = rng.random()
            if roll < 0.08:
                world.reattach()
            elif roll < 0.15:
                world.harvest()
                world.check()
            else:
                _op(world, rng)
    except AssertionError as error:
        raise AssertionError(f"seed {seed}: {error}") from None
    return world


def test_epoch_reads_equal_the_whole_plan_reads_on_random_histories(clock) -> None:
    epochs = 0
    for seed in range(SEQUENCES):
        epochs += run_sequence(seed, clock).epochs
    assert epochs >= SEQUENCES  # every history was attached at least once, many more than once


def test_a_step_settled_before_the_attachment_is_not_part_of_the_return(clock) -> None:
    clock["steps"] = (1,)
    world = Delegation(random.Random(1))
    old = world.add(None)
    world.start(old) and world.review(old) and world.accept(old)
    world.reattach()
    fresh = world.add(None)
    world.start(fresh) and world.review(fresh)
    since = delegation_epoch.attached_since(world.db, world.step_id)
    assert [row["id"] for row in delegation_epoch.return_rows(world.db, world.plan.id, since)] == [fresh]
    assert [row["id"] for row in old_return_rows(world.db, world.plan.id)] == [old, fresh]
    assert delegation_harvest._returned_since(world.db, world.step_row(), world.plan.id)
