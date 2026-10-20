"""The `closed` outcome: work that will not be done, kept on the plan as that record.

Before this, the only ways off a plan were `accepted`/`accepted_with_followup`
(the objective was met), `failed` (it cannot be met), and a manual archive that
takes no position at all. Nothing said "no longer wanted", so agents marked
superseded work failed or blocked and it sat in the pane forever, or they left
it open and the plan never finished.

`closed` is that missing outcome. It is terminal, legal from every open state,
and carries a required reason. Since #1887 it stays on the plan like any
settled node (it used to archive itself in the same write): it folds, counts in
neither `done` nor `total`, is never actionable to liveness, is never named by
the upkeep notice, and shows its reason where settled work is listed. Only an
explicit archive takes it off the plan.

Rule 1b (owner, 2026-09-16, issue #1827): the owning agent can always clean its
own plan. A user assignment governs who does the work and who may take the
executor slot -- never whether the owner can end the node. The live failure was
a user-assigned "Merge sign-off" node neither side could settle, leaving its
plan permanently 3 of 4.
"""

from __future__ import annotations

from collections.abc import Iterator

import sqlite3

import pytest

from infra.schema import SCHEMA_SQL
from plans import closure, projections, service, status_context
from plans.boundary import PlanActor, mutate
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


def _task(db, plan_id: str, title: str = "Superseded", **kwargs):
    node = service.add_node(
        plan_id, kind="task", title=title,
        acceptance_criteria=f"{title} DOD", db=db, **kwargs,
    )
    db.commit()
    return node


def _row(db, node_id: str):
    return db.execute("SELECT * FROM plan_nodes WHERE id=?", (node_id,)).fetchone()


def _close(db, plan_id, node_id, *, reason="superseded by the new approach",
           actor=OWNER, revision=None, mutation_id=None, **params):
    return mutate(
        plan_id, "close_node",
        {
            "node_id": node_id,
            "reason": reason,
            "expected_revision": (
                revision if revision is not None else _row(db, node_id)["revision"]
            ),
            **params,
        },
        actor=actor, provenance="test", mutation_id=mutation_id or _mid(), db=db,
    )


def _settle(db, plan_id, node_id, *, evidence="shipped and verified"):
    """Take one active node to accepted the way an owner does: evidence, then verdict."""
    mutate(
        plan_id, "request_review",
        {
            "node_id": node_id, "evidence": evidence,
            "expected_revision": _row(db, node_id)["revision"],
        },
        actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
    )
    return mutate(
        plan_id, "accept",
        {
            "node_id": node_id, "verification_result": "passed",
            "expected_revision": _row(db, node_id)["revision"],
        },
        actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
    )


def _event(db, node_id: str, event: str):
    return db.execute(
        "SELECT * FROM plan_node_events WHERE node_id=? AND event=? "
        "ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (node_id, event),
    ).fetchone()


# --- 1. the outcome itself ---------------------------------------------------


def test_close_ends_the_node_and_keeps_it_on_the_plan(db):
    plan = _plan(db)
    node = _task(db, plan.id)

    _close(db, plan.id, node.id)

    closed = _row(db, node.id)
    assert closed["state"] == "closed"
    assert closed["archived_at"] is None


def test_an_explicit_archive_still_retires_a_closed_node(db):
    plan = _plan(db)
    node = _task(db, plan.id)
    _close(db, plan.id, node.id)

    mutate(
        plan.id, "archive_node",
        {"node_id": node.id, "expected_revision": _row(db, node.id)["revision"]},
        actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
    )

    assert _row(db, node.id)["archived_at"] is not None


def test_close_records_its_reason_on_the_transition(db):
    plan = _plan(db)
    node = _task(db, plan.id)

    _close(db, plan.id, node.id, reason="the customer withdrew the request")

    event = _event(db, node.id, "state_transition")
    assert event["new_state"] == "closed"
    assert "the customer withdrew the request" in event["payload_json"]


@pytest.mark.parametrize(
    "states", [(), ("active",), ("active", "review_requested")],
    ids=["pending", "active", "review_requested"],
)
def test_close_is_legal_from_every_open_state(db, states):
    plan = _plan(db)
    node = _task(db, plan.id)
    for state in states:
        service.transition_node(node.id, state, db=db)
    db.commit()

    _close(db, plan.id, node.id)

    assert _row(db, node.id)["state"] == "closed"


def test_closed_is_terminal(db):
    plan = _plan(db)
    node = _task(db, plan.id)
    _close(db, plan.id, node.id)

    with pytest.raises(service.PlanError):
        service.transition_node(node.id, "active", db=db)


def test_close_requires_a_reason(db):
    plan = _plan(db)
    node = _task(db, plan.id)

    # Matched exactly: a bare PlanInputError also passes when close_node is not
    # a known operation at all, which is how this test passed before any of it
    # was wired. The message is what proves a reason guard is the refuser.
    with pytest.raises(service.PlanInputError, match="reason is required"):
        _close(db, plan.id, node.id, reason="   ")


def test_the_writer_keeps_the_reason_invariant_for_direct_callers(db):
    """The boundary's shape check fires first; this is the writer's own guard.

    `closure.close_node` is callable without going through the boundary, and a
    close whose entire content is *why* must not be able to record nothing.
    """
    plan = _plan(db)
    node = _task(db, plan.id)

    with pytest.raises(service.PlanInputError, match=closure.REASON_REQUIRED):
        closure.close_node(node.id, reason="   ", provenance="test", db=db)


def test_a_closed_node_counts_in_neither_done_nor_total(db):
    plan = _plan(db)
    kept = _task(db, plan.id, title="Kept")
    dropped = _task(db, plan.id, title="Dropped")
    before = projections.progress(plan.id, db=db)

    _close(db, plan.id, dropped.id)

    after = projections.progress(plan.id, db=db)
    assert (before.done, before.total) == (0, 2)
    assert (after.done, after.total) == (0, 1)
    assert _row(db, kept.id)["archived_at"] is None


def test_close_leaves_the_row_events_and_history(db):
    plan = _plan(db)
    node = _task(db, plan.id)
    created = db.execute(
        "SELECT COUNT(*) AS n FROM plan_node_events WHERE node_id=? AND event='node_created'",
        (node.id,),
    ).fetchone()["n"]

    _close(db, plan.id, node.id)

    assert _row(db, node.id) is not None
    assert db.execute(
        "SELECT COUNT(*) AS n FROM plan_node_events WHERE node_id=? AND event='node_created'",
        (node.id,),
    ).fetchone()["n"] == created


# --- 2. a closed node is never live work -------------------------------------


def test_a_closed_node_is_not_actionable_to_liveness_or_delegation(db):
    plan = _plan(db)
    node = _task(db, plan.id)

    _close(db, plan.id, node.id)

    assert projections.open_delegated_work(db, plan.id) is False
    # Closing the only step is the plan's return (labeled closed), not silence.
    assert projections.delegated_work_returned(db, plan.id) is True


def test_a_closed_node_is_never_named_by_the_upkeep_notice(db):
    from plans import upkeep_notice

    plan = _plan(db)
    node = _task(db, plan.id)
    service.transition_node(node.id, "active", db=db)
    db.commit()

    _close(db, plan.id, node.id)

    line = upkeep_notice.notice_for(db, "root", "read_file")

    assert node.id not in (line or "")
    assert "Current task" not in (line or "")


def test_a_closed_node_moves_from_current_to_recent(db):
    from plans import status

    plan = _plan(db)
    kept = _task(db, plan.id, title="Kept")
    dropped = _task(db, plan.id, title="Dropped")

    _close(db, plan.id, dropped.id)

    read = status.read(db, service._plan_row(db, plan.id), actor=OWNER)
    assert [row["id"] for row in read["current"]["rows"]] == [kept.id]
    assert [row["id"] for row in read["recent"]["rows"]] == [dropped.id]


# --- 3. accepted and failed work is never hidden -----------------------------


@pytest.mark.parametrize("outcome", ["accepted", "failed"])
def test_settled_work_stays_visible_and_leaves_the_counter(db, outcome):
    """Settled work is never archived automatically and stays in Recent (2026-09-16);
    the counter covers only work still in play (2026-09-20, Gate 3)."""
    from plans import status

    plan = _plan(db)
    node = _task(db, plan.id, title="Shipped")
    service.transition_node(node.id, "active", db=db)
    db.commit()
    if outcome == "accepted":
        _settle(db, plan.id, node.id)
    else:
        service.transition_node(node.id, "failed", db=db)
        db.commit()

    read = status.read(db, service._plan_row(db, plan.id), actor=OWNER)

    assert _row(db, node.id)["archived_at"] is None
    assert [row["id"] for row in read["recent"]["rows"]] == [node.id]
    assert projections.progress(plan.id, db=db).total == 0


# --- 4. rule 1b: the owner can always clean its own plan ---------------------


def test_the_owner_can_close_a_user_assigned_node(db):
    """Issue #1827: a user-assigned sign-off node neither side could settle."""
    plan = _plan(db)
    node = _task(db, plan.id, title="Merge sign-off", assignee="user")

    _close(db, plan.id, node.id, reason="merged by hand; the gate is moot")

    assert _row(db, node.id)["state"] == "closed"
    assert projections.progress(plan.id, db=db).total == 0


def test_the_owner_can_archive_a_user_assigned_node(db):
    plan = _plan(db)
    node = _task(db, plan.id, title="Merge sign-off", assignee="user")

    mutate(
        plan.id, "archive_node",
        {"node_id": node.id, "expected_revision": _row(db, node.id)["revision"]},
        actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
    )

    assert _row(db, node.id)["archived_at"] is not None


def test_the_owner_can_settle_a_user_assigned_node_with_evidence(db):
    """A user assignment is not lifecycle immunity: evidence still settles it."""
    plan = _plan(db)
    node = _task(db, plan.id, title="Merge sign-off", assignee="user")
    service.transition_node(node.id, "active", db=db)
    db.commit()

    _settle(db, plan.id, node.id, evidence="PR #1802 merged at cd24d88c")

    assert _row(db, node.id)["state"] == "accepted"


def test_admission_still_refuses_a_user_assigned_node_the_executor_slot(db):
    """Who does the work is unchanged: only the user starts the user's task."""
    plan = _plan(db)
    node = _task(db, plan.id, title="Merge sign-off", assignee="user")

    result = mutate(
        plan.id, "activate",
        {"node_id": node.id, "expected_revision": _row(db, node.id)["revision"]},
        actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
    ).result

    assert result["admission"]["conflict"] == {"condition": "user_assigned"}
    assert _row(db, node.id)["state"] == "pending"


# --- 5. the oversize signal -------------------------------------------------


def _digest(db, plan_id: str) -> dict:
    return status_context.projection(
        db, service._plan_row(db, plan_id), actor=OWNER,
    )


def test_the_digest_is_silent_at_or_below_the_open_node_budget(db):
    plan = _plan(db)
    for index in range(status_context.OPEN_NODE_BUDGET):
        _task(db, plan.id, title=f"Task {index}")

    assert "oversize" not in _digest(db, plan.id)


def test_the_digest_names_the_open_node_budget_once_it_is_passed(db):
    plan = _plan(db)
    for index in range(status_context.OPEN_NODE_BUDGET + 1):
        _task(db, plan.id, title=f"Task {index}")

    assert _digest(db, plan.id)["oversize"] == {
        "open": status_context.OPEN_NODE_BUDGET + 1,
        "budget": status_context.OPEN_NODE_BUDGET,
    }


def test_the_open_count_excludes_settled_and_closed_work(db):
    plan = _plan(db)
    for index in range(status_context.OPEN_NODE_BUDGET + 2):
        _task(db, plan.id, title=f"Task {index}")
    stale = _task(db, plan.id, title="Stale")
    shipped = _task(db, plan.id, title="Shipped")
    service.transition_node(shipped.id, "active", db=db)
    db.commit()

    _close(db, plan.id, stale.id)
    _settle(db, plan.id, shipped.id)
    over = _digest(db, plan.id)["oversize"]

    # Accepted work stays on the plan and in progress, but it is not open.
    assert _row(db, shipped.id)["archived_at"] is None
    assert over["open"] == status_context.OPEN_NODE_BUDGET + 2


def test_passing_the_budget_never_refuses_a_write(db):
    """A soft signal: the owner decides what to prune, nothing sweeps or blocks."""
    plan = _plan(db)
    for index in range(status_context.OPEN_NODE_BUDGET + 1):
        _task(db, plan.id, title=f"Task {index}")

    added = mutate(
        plan.id, "add",
        {"kind": "task", "title": "One more", "acceptance_criteria": "DOD", "size": "unknown"},
        actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
    ).result

    assert added["node"]["title"] == "One more"
    assert _digest(db, plan.id)["oversize"]["open"] == (
        status_context.OPEN_NODE_BUDGET + 2
    )
