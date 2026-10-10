"""The exact-node read still answers for a node that has left the plan.

A closed node used to be archived in the same write that closes it (until
#1887), so the only place its outcome could be read was the exact-node read --
and that read refused every
archived node, because it looked the node up in the live projection that
archived rows never enter. The row, events and links survived exactly as
archive promises; nothing could show them.

The read now serves any archived node from its durable row: its fields, its
events, and a display that says what happened to it. Only closed nodes gained a
new outcome, but serving closed alone would make "archived" a special case of
itself, so every retirement reads the same way.
"""

from __future__ import annotations

from collections.abc import Iterator

import sqlite3

import pytest

from infra.schema import SCHEMA_SQL
from plans import archival, operator_node_state, service
from plans.boundary import PlanActor, mutate
from plans.current import read_current_status
from tests.test_attention_queue import _mid, _seed, raise_decision  # noqa: F401

OWNER = PlanActor("root", "user-1", "project-1")


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    _seed(connection)
    connection.commit()
    yield connection
    connection.close()


def _plan(db, owner: str = "root"):
    plan = service.ensure_plan("thread", owner, db=db)
    db.commit()
    return plan


def _task(db, plan_id: str, title: str):
    node = service.add_node(
        plan_id, kind="task", title=title,
        acceptance_criteria=f"{title} DOD", db=db,
    )
    db.commit()
    return node


def _revision(db, node_id: str) -> int:
    return db.execute(
        "SELECT revision FROM plan_nodes WHERE id=?", (node_id,)
    ).fetchone()["revision"]


def _mutate(db, plan_id, operation, params):
    return mutate(
        plan_id, operation, params,
        actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
    )


def _read(db, node_id: str) -> dict:
    return read_current_status(actor=OWNER, node_id=node_id, db=db)


def test_an_archived_closed_node_reads_back_with_its_outcome_and_reason(db):
    plan = _plan(db)
    node = _task(db, plan.id, "Superseded")
    _mutate(db, plan.id, "close_node", {
        "node_id": node.id, "reason": "replaced by the new approach",
        "expected_revision": _revision(db, node.id),
    })
    _mutate(db, plan.id, "archive_node", {
        "node_id": node.id, "expected_revision": _revision(db, node.id),
    })

    detail = _read(db, node.id)

    assert detail["id"] == node.id
    assert detail["title"] == "Superseded"
    assert detail["state"] == "closed"
    assert detail["archived_at"] is not None
    assert detail["display"] == {"label_key": "finished", "tone_key": "closed"}
    closing = next(
        event for event in detail["events"]
        if event["event"] == "state_transition" and event["new_state"] == "closed"
    )
    assert closing["payload"]["reason"] == "replaced by the new approach"
    assert [event["event"] for event in detail["events"]][:1] == ["node_archived"]


def test_an_archived_unsettled_node_says_archived_not_to_do(db):
    """Archive takes no position on state, so the label must not claim one."""
    plan = _plan(db)
    node = _task(db, plan.id, "Parked")
    _mutate(db, plan.id, "archive_node", {
        "node_id": node.id, "expected_revision": _revision(db, node.id),
    })

    detail = _read(db, node.id)

    assert detail["state"] == "pending"
    assert detail["display"] == {"label_key": "finished", "tone_key": "closed"}


def test_an_archived_settled_node_keeps_its_settled_outcome(db):
    plan = _plan(db)
    node = _task(db, plan.id, "Shipped")
    service.transition_node(node.id, "active", db=db)
    service.transition_node(node.id, "failed", db=db)
    db.commit()
    _mutate(db, plan.id, "archive_node", {
        "node_id": node.id, "expected_revision": _revision(db, node.id),
    })

    detail = _read(db, node.id)

    assert detail["display"] == {"label_key": "finished", "tone_key": "failed"}


def test_a_live_node_reads_exactly_as_it_did(db):
    plan = _plan(db)
    node = _task(db, plan.id, "Live")

    detail = _read(db, node.id)

    assert detail["archived_at"] is None
    assert detail["display"] == {"label_key": "waiting", "tone_key": "waiting"}
    assert "execution_time" in detail


def test_a_retired_node_in_another_plan_is_still_refused_by_name(db):
    plan = _plan(db)
    other = _plan(db, "other-thread")
    foreign = _task(db, other.id, "Foreign")
    # The writer directly: the boundary would rightly refuse `root` here.
    archival.archive_node(foreign.id, provenance="test", db=db)
    db.commit()
    _task(db, plan.id, "Mine")

    with pytest.raises(service.PlanError, match="not in this plan"):
        _read(db, foreign.id)


def test_every_terminal_state_has_a_retired_display():
    """A new terminal state cannot reach the retired read as a KeyError."""
    for state in service._TERMINAL_STATES:
        display = operator_node_state.retired_display(state)
        assert display["label_key"] in operator_node_state.NODE_LABEL_KEYS
        assert display["tone_key"] in operator_node_state.TONE_KEYS
