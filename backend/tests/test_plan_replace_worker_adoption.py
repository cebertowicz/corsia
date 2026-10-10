"""Grandchild adoption and replaced-worker notification scenarios.

The shared replacement setup remains in ``test_plan_replace_worker`` so these
related scenarios use the same mutation and fixture helpers without duplicating
collection fixtures.
"""

from __future__ import annotations

import pytest

from plans import delegation, delegation_child_lifecycle, mutations, service
from tests.child_parent_notice_helpers import (
    db,
    delegated,
    notice_details,
    transcript,
    wakes,
)
from tests.plan_replace_worker_helpers import (
    replace as _replace,
    replaced_notices as _replaced_notices,
    row as _row,
    started_replacement as _started_replacement,
    thread as _thread,
)


# --- A gone mid-tier worker's children ---------------------------------------


def _grandchild(db, worker_node_id: str):
    _thread(db, "grandchild", "worker")
    plan = service.ensure_plan("thread", "grandchild", db=db)
    service.add_node(plan.id, kind="task", title="Deep", acceptance_criteria="-", db=db)
    service.transition_node(worker_node_id, "active", db=db)
    mutations.attach_spawned_thread(worker_node_id, "grandchild", db=db)
    return plan


def test_replace_names_the_old_workers_own_workers_it_left_running(db):
    step, _plan, worker_node = delegated(db)
    _grandchild(db, worker_node.id)
    _started_replacement(db)

    reply = _replace(db, step.id, "replacement")

    assert reply["node"]["spawned_thread_id"] == "replacement"
    assert reply["replaced_worker"] == "worker"
    assert reply["stranded_workers"] == ["grandchild"]
    assert "worker was replaced" in reply["notice"]
    assert "grandchild" in reply["notice"]


def test_replace_tells_the_caller_to_stop_the_old_worker(db):
    step, _plan, _node = delegated(db)
    _started_replacement(db)

    reply = _replace(db, step.id, "replacement")

    assert reply["replaced_worker"] == "worker"
    assert "stranded_workers" not in reply
    assert "delegate tools" in reply["notice"]


def test_replace_onto_an_empty_step_names_nobody(db):
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    step = service.add_node(
        parent_plan.id,
        kind="task",
        title="Unowned",
        acceptance_criteria="-",
        spawn_kind="claude-code",
        db=db,
    )
    service.transition_node(step.id, "active", db=db)
    _started_replacement(db)

    reply = _replace(db, step.id, "replacement")

    assert reply["node"]["spawned_thread_id"] == "replacement"
    assert "replaced_worker" not in reply
    assert "notice" not in reply


# --- Adopting a replaced delegator's worker ----------------------------------
# Delete means delete (#2647): a deleted or purged delegator never leaves an
# adoptable worker, so replacing it off its step is the one adoption source.


def _purge(db, thread_id: str) -> None:
    delegation_child_lifecycle.on_child_thread_ended(db, thread_id, reason="purged")
    db.execute("UPDATE threads SET lifecycle_state='purged' WHERE id=?", (thread_id,))


def _spawned_by(db, thread_id: str):
    return db.execute(
        "SELECT spawned_by FROM threads WHERE id=?", (thread_id,)
    ).fetchone()["spawned_by"]


def _stranded_grandchild(db):
    """Create a purged worker whose own worker still exists."""
    step, _plan, worker_node = delegated(db)
    grandchild_plan = _grandchild(db, worker_node.id)
    _purge(db, "worker")
    return step, grandchild_plan


def _open_step(db, title: str):
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    step = service.add_node(
        parent_plan.id,
        kind="task",
        title=title,
        acceptance_criteria="-",
        spawn_kind="claude-code",
        db=db,
    )
    service.transition_node(step.id, "active", db=db)
    return step


def test_a_purged_delegators_worker_is_never_adopted_with_replace(db):
    step, _grandchild_plan = _stranded_grandchild(db)

    with pytest.raises(service.PlanError, match="not replaced"):
        _replace(db, step.id, "grandchild")

    assert _spawned_by(db, "grandchild") == "worker"
    assert _row(db, step.id)["spawned_thread_id"] is None


def test_a_purged_delegators_worker_is_never_adopted_onto_an_empty_step(db):
    _stranded_grandchild(db)
    empty = _open_step(db, "Take over")

    with pytest.raises(service.PlanError, match="not replaced"):
        mutations.attach_spawned_thread(empty.id, "grandchild", db=db)

    assert _spawned_by(db, "grandchild") == "worker"
    assert _row(db, empty.id)["spawned_thread_id"] is None


def test_the_parent_can_adopt_a_replaced_workers_child_onto_an_empty_step(db):
    step, _plan, worker_node = delegated(db)
    grandchild_plan = _grandchild(db, worker_node.id)
    _started_replacement(db)
    _replace(db, step.id, "replacement")
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    empty = _open_step(db, "Take over")

    mutations.attach_spawned_thread(empty.id, "grandchild", db=db)

    assert _row(db, empty.id)["spawned_thread_id"] == "grandchild"
    assert _spawned_by(db, "grandchild") == "parent"
    assert service._plan_row(db, grandchild_plan.id)["parent_plan_id"] == parent_plan.id
    assert delegation._valid_structured_epoch(_row(db, empty.id), db) is not None
    assert delegation.has_open_structured_attachment("grandchild", db=db)


def test_the_parent_can_adopt_the_child_of_a_worker_it_replaces(db):
    """Replacing a live worker with its own worker adopts it in the same write."""
    step, _plan, worker_node = delegated(db)
    _grandchild(db, worker_node.id)

    reply = _replace(db, step.id, "grandchild")

    assert reply["node"]["spawned_thread_id"] == "grandchild"
    assert _spawned_by(db, "grandchild") == "parent"


def test_a_worker_whose_delegator_is_live_is_not_adopted(db):
    _step, _plan, worker_node = delegated(db)
    _grandchild(db, worker_node.id)
    other = _open_step(db, "Other")

    with pytest.raises(service.PlanError, match="not replaced"):
        mutations.attach_spawned_thread(other.id, "grandchild", db=db)

    assert _spawned_by(db, "grandchild") == "worker"
    assert _row(db, other.id)["spawned_thread_id"] is None


def test_a_worker_whose_delegator_was_stood_down_is_not_adopted(db):
    """Detached without being replaced or ended: the delegator is still its owner."""
    step, _plan, worker_node = delegated(db)
    _grandchild(db, worker_node.id)
    delegation.detach_spawned_thread(step.id, reason="stood down", db=db)

    with pytest.raises(service.PlanError, match="not replaced"):
        _replace(db, step.id, "grandchild")

    assert _spawned_by(db, "grandchild") == "worker"


def test_a_worker_from_an_unrelated_tree_is_not_adopted(db):
    """A gone delegator this plan never delegated to leaves its worker alone."""
    grandparent_plan = service.ensure_plan("thread", "grandparent", db=db)
    elsewhere_step = service.add_node(
        grandparent_plan.id,
        kind="task",
        title="Elsewhere",
        acceptance_criteria="-",
        spawn_kind="claude-code",
        db=db,
    )
    service.transition_node(elsewhere_step.id, "active", db=db)
    _thread(db, "elsewhere", "grandparent")
    elsewhere_plan = service.ensure_plan("thread", "elsewhere", db=db)
    mutations.attach_spawned_thread(elsewhere_step.id, "elsewhere", db=db)
    inner = service.add_node(
        elsewhere_plan.id,
        kind="task",
        title="Inner",
        acceptance_criteria="-",
        spawn_kind="claude-code",
        db=db,
    )
    service.transition_node(inner.id, "active", db=db)
    _thread(db, "stray", "elsewhere")
    service.ensure_plan("thread", "stray", db=db)
    mutations.attach_spawned_thread(inner.id, "stray", db=db)
    _purge(db, "elsewhere")
    step, _plan, _node = delegated(db)

    with pytest.raises(service.PlanError, match="no step of this plan"):
        _replace(db, step.id, "stray")

    assert _spawned_by(db, "stray") == "elsewhere"
    assert _row(db, step.id)["spawned_thread_id"] == "worker"


def test_a_gone_delegators_wake_never_offers_its_worker_for_adoption(db):
    _stranded_grandchild(db)

    text = " ".join(str(row["content"]) for row in wakes(db, "parent"))

    assert "grandchild" in text
    assert "adopt" not in text
    assert "replace_spawned_thread" not in text
    assert "attach_spawned_thread" not in text


# --- A replaced worker is told -----------------------------------------------


def test_a_replaced_live_worker_is_told_without_being_woken(db):
    step, _plan, _node = delegated(db)
    _started_replacement(db)
    woken = len(wakes(db, "worker"))

    _replace(db, step.id, "replacement")

    told = _replaced_notices(db, "worker")
    assert len(told) == 1
    assert "You were replaced on Delegate work" in told[0]["summary"]
    assert "Stop working" in told[0]["summary"]
    assert notice_details(told[0])["replacement"] == "replacement"
    assert len(wakes(db, "worker")) == woken
    assert transcript(db, "worker") == []


def test_a_replaced_worker_that_is_gone_is_not_told(db):
    step, _plan, _node = delegated(db)
    _started_replacement(db)
    db.execute("UPDATE threads SET lifecycle_state='archived' WHERE id='worker'")

    _replace(db, step.id, "replacement")

    assert _replaced_notices(db, "worker") == []


def test_attaching_to_an_empty_step_tells_nobody(db):
    step = _open_step(db, "Unowned")
    _started_replacement(db)

    _replace(db, step.id, "replacement")

    assert _replaced_notices(db, "replacement") == []
