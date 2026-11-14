"""The harvest decides readiness on a read and takes the write lock only to harvest.

The periodic sweep visits every live delegated attachment. Deciding that none
is ready is reads only, so a pass with nothing ready must not hold the
database's one write lock while it decides. A row found ready is decided again
under the lock, so a pass that lost a race, or a row that stopped being ready,
writes nothing. These run on a WAL database file, where every outermost write
takes the process's real write admission ticket.
"""

from __future__ import annotations

import contextlib
import sqlite3

import pytest

from automation.continuation.sources.harvest import HarvestSource
from infra import write_admission
from infra.database import PooledConnection
from infra.schema import SCHEMA_SQL
from plans import delegation_harvest, mutations, plan_write, service
from tests.plan_review_helpers import submit_review_evidence


def _connect(path) -> sqlite3.Connection:
    db = sqlite3.connect(path, factory=PooledConnection, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    return db


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "harvest.db"
    db = _connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project-1','Project','t','t')"
    )
    for thread_id, spawned_by in (("parent", None), ("worker", "parent")):
        db.execute(
            """INSERT INTO threads
               (id,user_id,project_id,spawned_by,model,created_at,updated_at)
               VALUES (?,'user-1','project-1',?,'test/model','t','t')""",
            (thread_id, spawned_by),
        )
    db.commit()
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    delegated = service.add_node(
        parent_plan.id, kind="task", title="Delegate work",
        acceptance_criteria="Worker result is reviewed",
        spawn_kind="claude-code", db=db,
    )
    service.transition_node(delegated.id, "active", db=db)
    plan = service.ensure_plan("thread", "worker", db=db)
    mutations.attach_spawned_thread(delegated.id, "worker", db=db)
    leaf = service.add_node(
        plan.id, kind="task", title="Write the tests", acceptance_criteria="Done", db=db,
    )
    service.transition_node(leaf.id, "active", actor_id="worker", db=db)
    db.commit()
    try:
        yield path, db, delegated.id, leaf.id
    finally:
        db.close()


@pytest.fixture
def admissions(monkeypatch):
    """The scopes the real write admission granted, in order."""
    scopes: list[str] = []
    admit = write_admission.admit

    def recording(connection, scope):
        admission = admit(connection, scope)
        if admission is not None:
            scopes.append(scope)
        return admission

    monkeypatch.setattr(write_admission, "admit", recording)
    return scopes


def _statements(db) -> list[str]:
    seen: list[str] = []
    db.set_trace_callback(seen.append)
    return seen


def _harvest_events(db, node_id: str) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM plan_node_events WHERE node_id=? AND event='child_plan_completed'",
        (node_id,),
    ).fetchone()[0]


def _return_wakes(db, node_id: str) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id='parent' AND id LIKE ?",
        (f"plan-wake:child_returned:{node_id}:%",),
    ).fetchone()[0]


def _between_read_and_lock(monkeypatch, action):
    """Run ``action`` once, after the read pass and before the harvest takes the lock."""
    real = plan_write.transaction
    pending = [action]

    @contextlib.contextmanager
    def transaction(db):
        if pending:
            pending.pop()()
        with real(db) as connection:
            yield connection

    monkeypatch.setattr(delegation_harvest.plan_write, "transaction", transaction)


def test_a_sweep_with_nothing_ready_takes_no_write(store, admissions):
    _path, db, delegated_id, _leaf_id = store
    statements = _statements(db)

    woken = HarvestSource().prepare("2026-08-01T00:00:00+00:00", db=db)

    db.set_trace_callback(None)
    assert woken == []
    assert admissions == []
    writes = [
        sql for sql in statements
        if sql.split(None, 1)[0].upper() in {"BEGIN", "SAVEPOINT", "INSERT", "UPDATE", "DELETE"}
    ]
    assert writes == []
    assert not db.in_transaction
    assert service.get_node(delegated_id, db=db).state == "active"


def test_a_ready_child_takes_the_write_and_is_harvested(store, admissions):
    _path, db, delegated_id, leaf_id = store
    submit_review_evidence(leaf_id, db=db, evidence="Tests pass", actor_id="worker")
    db.commit()
    admissions.clear()

    harvested = delegation_harvest.harvest_ready_children(
        db=db, drives=[], changed_plan_ids=[],
    )

    assert harvested == [delegated_id]
    # The harvest is one write; the status drain writes after its commit.
    assert admissions.count("plan_service") == 1
    assert service.get_node(delegated_id, db=db).state == "review_requested"
    assert _harvest_events(db, delegated_id) == 1
    assert _return_wakes(db, delegated_id) == 1


def test_two_racing_sweeps_harvest_a_ready_child_once(store, admissions, monkeypatch):
    path, db, delegated_id, leaf_id = store
    submit_review_evidence(leaf_id, db=db, evidence="Tests pass", actor_id="worker")
    db.commit()
    other = _connect(path)
    first: list[str] = []

    def other_sweep_wins():
        first.extend(delegation_harvest.harvest_ready_children(
            db=other, drives=[], changed_plan_ids=[],
        ))

    _between_read_and_lock(monkeypatch, other_sweep_wins)
    try:
        second = delegation_harvest.harvest_ready_children(
            db=db, drives=[], changed_plan_ids=[],
        )
    finally:
        other.close()

    assert first == [delegated_id]
    assert second == []
    assert service.get_node(delegated_id, db=db).state == "review_requested"
    assert _harvest_events(db, delegated_id) == 1
    assert _return_wakes(db, delegated_id) == 1


def test_a_child_that_stops_being_ready_before_the_lock_is_left(store, monkeypatch):
    path, db, delegated_id, leaf_id = store
    submit_review_evidence(leaf_id, db=db, evidence="Tests pass", actor_id="worker")
    db.commit()
    other = _connect(path)

    def worker_turn_starts():
        other.execute(
            """INSERT INTO active_turns
               (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
               VALUES ('turn-2','worker',0,'test/model','running','2026-08-01T00:00:00','boot-1')"""
        )
        other.commit()

    _between_read_and_lock(monkeypatch, worker_turn_starts)
    try:
        harvested = delegation_harvest.harvest_ready_children(
            db=db, drives=[], changed_plan_ids=[],
        )
    finally:
        other.close()

    assert harvested == []
    assert service.get_node(delegated_id, db=db).state == "active"
    assert _harvest_events(db, delegated_id) == 0
    assert _return_wakes(db, delegated_id) == 0
