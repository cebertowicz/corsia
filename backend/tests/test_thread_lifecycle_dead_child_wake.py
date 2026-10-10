"""F3 wired at its real call sites: user archive, purge, and not dormancy.

The hard-delete row is covered at its own call site in
``test_session_delete.py`` (``TestDeleteWakesDelegatingParent``), which needs
the full ``delete_thread`` HTTP path rather than this module's helpers.
Directly exercising ``core.thread_lifecycle``'s own three functions here
proves each production call site, not just the shared
``delegation_child_lifecycle.on_child_thread_ended`` unit covered in
``test_child_parent_notice_dead_child.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

import pytest

import core.thread_lifecycle as lifecycle
from infra.database import write_transaction
from infra.schema import SCHEMA_SQL
from plans import service
from tests.plan_writer_authorizer import raw_plan_seed


def _archive(db, *args, **kwargs):
    """The archive transition under the write scope its callers own."""
    db.commit()
    with write_transaction(db, scope="archive_state"):
        return lifecycle.set_user_archive_state(db, *args, **kwargs)


NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project-1','Project','t','t')"
    )
    for thread_id in ("parent", "worker"):
        connection.execute(
            """INSERT INTO threads
               (id,user_id,project_id,model,created_at,updated_at)
               VALUES (?,'user-1','project-1','native/test','t','t')""",
            (thread_id,),
        )
    connection.commit()
    yield connection
    connection.close()


def _delegate_to_worker(db) -> str:
    """Seed a parent plan with one open leaf delegated to 'worker'; return its id."""
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    node = service.add_node(
        parent_plan.id, kind="task", title="Delegate work",
        acceptance_criteria="Worker result is reviewed",
        spawn_kind="claude-code", db=db,
    )
    service.transition_node(node.id, "active", db=db)
    with raw_plan_seed(db):
        db.execute(
            "UPDATE plan_nodes SET spawned_thread_id='worker' WHERE id=?",
            (node.id,),
        )
    db.commit()
    return node.id


def _wake_content(db) -> str:
    row = db.execute(
        "SELECT content FROM messages WHERE thread_id='parent' "
        "AND id LIKE 'plan-wake:%'"
    ).fetchone()
    assert row is not None, "expected a plan wake in the parent's own FIFO"
    return str(row["content"])


def _no_wake(db) -> bool:
    return db.execute(
        "SELECT 1 FROM messages WHERE thread_id='parent' AND id LIKE 'plan-wake:%'"
    ).fetchone() is None


def _worker_leaf(db) -> str:
    worker_plan = service.ensure_plan("thread", "worker", db=db)
    leaf = service.add_node(
        worker_plan.id, kind="task", title="Worker step",
        acceptance_criteria="done", db=db,
    )
    db.commit()
    return leaf.id


def _wake_count(db) -> int:
    return db.execute(
        "SELECT COUNT(*) c FROM messages WHERE id LIKE 'plan-wake:%'"
    ).fetchone()["c"]


def test_f3_user_archive_wakes_the_parent_and_changes_nothing_irreversible(db):
    """A user archive can be undone, so it only tells the parent.

    The child's open work stays open and the attachment stays, so unarchiving
    resumes the delegation exactly as it was.
    """
    node_id = _delegate_to_worker(db)
    leaf_id = _worker_leaf(db)

    _archive(db, "worker", archived=True, now=NOW)

    content = _wake_content(db).lower()
    assert "archived" in content and "unarchive" in content
    assert db.execute(
        "SELECT spawned_thread_id FROM plan_nodes WHERE id=?", (node_id,),
    ).fetchone()["spawned_thread_id"] == "worker"
    assert db.execute(
        "SELECT state FROM plan_nodes WHERE id=?", (leaf_id,),
    ).fetchone()["state"] == "pending"


def test_f3_each_user_archive_is_its_own_wake(db):
    _delegate_to_worker(db)

    _archive(db, "worker", archived=True, now=NOW)
    _archive(db, "worker", archived=False, now=NOW)
    _archive(
        db, "worker", archived=True, now=datetime(2026, 9, 19, tzinfo=timezone.utc),
    )

    assert _wake_count(db) == 2


def test_f3_leaves_a_dormancy_archive_alone(db):
    """A dormancy archive is reversible, so it must not end a delegation.

    ``_evaluate_thread`` reactivates a dormancy-archived thread on fresh
    activity, but nothing reopens closed leaves or un-tells a parent that the
    delegation ended. That transition is the engine inferring death from
    silence; an idle child is F2's job, which reports it without destroying
    the delegation.
    """
    node_id = _delegate_to_worker(db)

    lifecycle._apply_archive(db, "worker", NOW, reason="dormancy")

    assert _no_wake(db)
    refreshed = db.execute(
        "SELECT spawned_thread_id FROM plan_nodes WHERE id=?", (node_id,),
    ).fetchone()
    assert refreshed["spawned_thread_id"] == "worker"


def test_f3_nightly_purge_wakes_and_releases_the_attachment(db):
    node_id = _delegate_to_worker(db)

    lifecycle._apply_purge(db, "worker", NOW)

    assert "purged" in _wake_content(db).lower()
    refreshed = db.execute(
        "SELECT spawned_thread_id FROM plan_nodes WHERE id=?", (node_id,),
    ).fetchone()
    assert refreshed["spawned_thread_id"] is None


def test_f4_the_plans_own_settle_of_its_worker_never_wakes(db):
    """F4: the plan standing its own worker down is not a thread-lifecycle event.

    ``archive_settled_worker`` is the parent *plan's* mechanism for retiring a
    worker with nothing left to do -- section F is thread lifecycle outside
    the plan, so this path never calls ``on_child_thread_ended`` and must
    never wake anyone.
    """
    _delegate_to_worker(db)

    lifecycle.archive_settled_worker(db, "worker", now=NOW)

    assert db.execute(
        "SELECT COUNT(*) c FROM messages WHERE id LIKE 'plan-wake:%'"
    ).fetchone()["c"] == 0


def test_f3_purge_after_a_user_archive_ends_the_delegation(db):
    """Archive reported the pause; purge reports the end and releases the node."""
    node_id = _delegate_to_worker(db)

    _archive(db, "worker", archived=True, now=NOW)
    lifecycle._apply_purge(db, "worker", NOW)
    lifecycle._apply_purge(db, "worker", NOW)

    assert _wake_count(db) == 2
    assert db.execute(
        "SELECT spawned_thread_id FROM plan_nodes WHERE id=?", (node_id,),
    ).fetchone()["spawned_thread_id"] is None
