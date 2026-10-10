"""The spawned-work refusal blocks only a reachable child.

Split out of ``test_plan_archival`` (which already covers the live-delegation
attachment and the ordinary archive contract) to keep both files under the
project's file-size budget. Reuses that module's ``db`` fixture and shared
delegation helpers directly rather than duplicating the seed/plan/task
scaffolding.

A retired (archived/purged) or missing child thread can never come back to
settle its own open work, so treating that open work as still-blocking would
freeze the parent node forever with no path out. Only a thread that could
still act (active or dormant) keeps the refusal.
"""

from __future__ import annotations

import pytest

from plans import service
from tests.test_plan_archival import (  # noqa: F401 - db is a reused fixture
    _archive,
    _counts,
    _delegated,
    _events,
    _mid,
    _row,
    _task,
    db,
)


def _open_child_work(db, *, child="child"):
    """Attach a child plan holding one pending (open) leaf node."""
    child_plan = service.ensure_plan("thread", child, db=db)
    _task(db, child_plan.id, "Child work")
    db.commit()
    return child_plan


def _retire_session(db, thread_id: str, *, lifecycle_state: str, purged: bool = False):
    """Match each lifecycle_state's real row shape, not a convenient stand-in.

    Production purge (``core/thread_lifecycle.py::_apply_purge``) sets
    ``lifecycle_state='purged'`` and ``purged_at`` but never touches
    ``archived_at``. A purged row must therefore leave ``archived_at`` NULL
    so the reachability predicate is actually decided by ``purged_at``, not
    by an ``archived_at`` value no real purge would ever write.
    """
    db.execute(
        "UPDATE threads SET lifecycle_state=?, archived_at=?, purged_at=? WHERE id=?",
        (
            lifecycle_state,
            "t" if lifecycle_state == "archived" else None,
            "t" if purged else None,
            thread_id,
        ),
    )
    db.commit()


@pytest.mark.parametrize(
    "lifecycle_state,purged", [("archived", False), ("purged", True)],
)
def test_archive_unblocks_a_terminal_node_whose_child_thread_is_retired(
    db, lifecycle_state, purged,
):
    plan, node = _delegated(db)
    _open_child_work(db)
    service.transition_node(node.id, "failed", db=db)
    db.commit()
    _retire_session(db, "child", lifecycle_state=lifecycle_state, purged=purged)

    _archive(db, plan.id, node.id)

    assert _row(db, node.id)["archived_at"] is not None


def test_archive_still_refuses_a_terminal_node_whose_live_child_has_open_work(db):
    """The escape is reachability, not merely reaching a terminal node state.

    A cancellation settles the work the child had and retires it; work the
    child takes on after being brought back is open work like any other.
    """
    from core.thread_lifecycle import reactivate_settled_worker

    plan, node = _delegated(db)
    service.transition_node(node.id, "failed", db=db)
    db.commit()
    reactivate_settled_worker(db, "child")
    _open_child_work(db)

    with pytest.raises(service.PlanError, match="settled first"):
        _archive(db, plan.id, node.id)
    db.rollback()

    assert _row(db, node.id)["archived_at"] is None


def test_the_spawned_work_refusal_names_the_childs_exact_open_nodes(db):
    """"Settle the child's work" is only actionable if it says which work."""
    from core.thread_lifecycle import reactivate_settled_worker

    plan, node = _delegated(db)
    service.transition_node(node.id, "failed", db=db)
    db.commit()
    reactivate_settled_worker(db, "child")
    child_plan = _open_child_work(db)
    (open_id,) = [
        row["id"] for row in db.execute(
            "SELECT id FROM plan_nodes WHERE plan_id=?", (child_plan.id,)
        )
    ]

    with pytest.raises(service.PlanError) as refusal:
        _archive(db, plan.id, node.id)
    db.rollback()

    assert open_id in str(refusal.value)


def test_archive_still_refuses_a_terminal_node_whose_dormant_child_has_open_work(db):
    """Dormant is idle, not retired: it can still resume to settle its work."""
    plan, node = _delegated(db)
    service.transition_node(node.id, "failed", db=db)
    db.commit()
    _retire_session(db, "child", lifecycle_state="dormant")
    _open_child_work(db)

    with pytest.raises(service.PlanError, match="settled first"):
        _archive(db, plan.id, node.id)
    db.rollback()

    assert _row(db, node.id)["archived_at"] is None


def test_archive_of_a_non_terminal_node_with_a_retired_child_still_refuses_via_delegation(
    db,
):
    """The reachability escape never bypasses the parent's own review obligation."""
    plan, node = _delegated(db, state="active")
    _open_child_work(db)
    _retire_session(db, "child", lifecycle_state="purged", purged=True)

    with pytest.raises(service.PlanError, match="retired"):
        _archive(db, plan.id, node.id)
    db.rollback()

    assert _row(db, node.id)["archived_at"] is None


def test_failing_the_node_lets_a_retired_childs_delegation_be_settled(db):
    """The message names the one actionable path: fail (or reject), then archive."""
    plan, node = _delegated(db, state="active")
    _open_child_work(db)
    _retire_session(db, "child", lifecycle_state="purged", purged=True)

    service.transition_node(node.id, "failed", db=db)
    db.commit()
    _archive(db, plan.id, node.id)

    assert _row(db, node.id)["archived_at"] is not None


def test_child_thread_reachable_is_true_for_active_and_dormant(db):
    from plans.archival import _child_thread_reachable

    assert _child_thread_reachable(db, "child") is True

    _retire_session(db, "child", lifecycle_state="dormant")

    assert _child_thread_reachable(db, "child") is True


def test_child_thread_reachable_is_false_for_archived_purged_and_missing(db):
    from plans.archival import _child_thread_reachable

    _retire_session(db, "child", lifecycle_state="archived")
    assert _child_thread_reachable(db, "child") is False

    _retire_session(db, "child", lifecycle_state="purged", purged=True)
    assert _child_thread_reachable(db, "child") is False

    assert _child_thread_reachable(db, "does-not-exist") is False


def test_repeated_archive_of_a_retired_child_node_is_idempotent(db):
    plan, node = _delegated(db)
    _open_child_work(db)
    service.transition_node(node.id, "failed", db=db)
    db.commit()
    _retire_session(db, "child", lifecycle_state="archived")
    mutation_id = _mid()

    first = _archive(db, plan.id, node.id, mutation_id=mutation_id).result
    before = _counts(db)
    second = _archive(
        db, plan.id, node.id, mutation_id=mutation_id,
        revision=first["node"]["revision"] - 1,
    ).result

    assert second == first
    assert _counts(db) == before


def test_archiving_a_retired_child_node_preserves_the_child_plan_and_events(db):
    plan, node = _delegated(db)
    child_plan = _open_child_work(db)
    service.transition_node(node.id, "failed", db=db)
    db.commit()
    _retire_session(db, "child", lifecycle_state="archived")

    _archive(db, plan.id, node.id)

    surviving_child_nodes = db.execute(
        "SELECT COUNT(*) AS n FROM plan_nodes WHERE plan_id=?", (child_plan.id,),
    ).fetchone()["n"]
    assert surviving_child_nodes == 1
    assert _events(db, node.id, "spawned_thread_attached") == 1
    assert _row(db, node.id)["spawned_thread_id"] == "child"
