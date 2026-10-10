"""A registered pull request outlives the task that opened it (#1822).

A PR's life ends at its own merge or close, not whenever the plan task that
happened to open it settles. Registering one while a task is active must not
make it task-scoped: node settlement leaves it stamped-free and visible, and
the pane reports it exempt so the row offers no misleading persistence
affordance. Everything else registered the same way still retires.
"""

from __future__ import annotations

import pytest

from infra.database import get_db
from plans import service
from registry import models, pinning, pr_lifecycle, view
from registry.node_retirement import retire_registrations_for_node
from registry.registration_writer import RegistrationOrigin
from tests.registry_helpers import make_session


@pytest.fixture()
def db(isolated_db):
    return get_db()


def _thread_with_active_node(db, tag: str):
    """A thread whose current Turn is bound to an active plan task."""
    thread_id = make_session(db, prefix=f"pr-exempt-{tag}")
    plan = service.ensure_plan("thread", thread_id, db=db)
    node = service.add_node(
        plan.id,
        kind="task",
        title=f"{tag} work",
        acceptance_criteria="The pull request is opened",
        db=db,
    )
    node = service.transition_node(node.id, "active", db=db)
    turn_id = f"turn-{tag}-{thread_id}"
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch,
            plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role)
           VALUES (?,?,0,'native/test','running','t0','boot',?,?,?,?,?)""",
        (
            turn_id, thread_id, plan.id, node.id,
            f"attempt-{turn_id}", node.commitment_id, "execution",
        ),
    )
    db.commit()
    return thread_id, node, turn_id


def _register_github(db, thread_id, turn_id, *, type_: str, external_id: str):
    return models.register_artifact(
        integration="github",
        type=type_,
        external_id=external_id,
        parent_thread_id=thread_id,
        title=external_id,
        origin=RegistrationOrigin(thread_id, turn_id, "artifact_tool"),
        db=db,
    )


def _row(db, artifact_id: str, thread_id: str):
    return db.execute(
        """SELECT surfacing, retired_at, origin_plan_node_id
           FROM artifact_registrations
           WHERE artifact_id=? AND parent_thread_id=?""",
        (artifact_id, thread_id),
    ).fetchone()


def test_settlement_leaves_a_registered_pull_request_alone(db):
    thread_id, node, turn_id = _thread_with_active_node(db, "open")
    pr = _register_github(
        db, thread_id, turn_id,
        type_="pull-request", external_id="cebertowicz/corsia#1822",
    )
    assert _row(db, pr["id"], thread_id)["origin_plan_node_id"] == node.id

    retired = retire_registrations_for_node(
        db, node_id=node.id, trigger="accepted", causal_event_id=None,
    )
    db.commit()

    assert retired == 0
    row = _row(db, pr["id"], thread_id)
    assert (row["surfacing"], row["retired_at"]) == ("visible", None)


def test_settlement_still_retires_a_non_pr_github_artifact(db):
    """The exemption is the PR type specifically, not the github integration."""
    thread_id, node, turn_id = _thread_with_active_node(db, "issue")
    issue = _register_github(
        db, thread_id, turn_id,
        type_="issue", external_id="cebertowicz/corsia#1823",
    )

    retired = retire_registrations_for_node(
        db, node_id=node.id, trigger="accepted", causal_event_id=None,
    )
    db.commit()

    assert retired == 1
    row = _row(db, issue["id"], thread_id)
    assert row["surfacing"] == "settled"
    assert row["retired_at"] is not None


def test_pane_reports_a_pull_request_row_as_retirement_exempt(db):
    thread_id, _node, turn_id = _thread_with_active_node(db, "pane")
    pr = _register_github(
        db, thread_id, turn_id,
        type_="pull-request", external_id="cebertowicz/corsia#1824",
    )
    issue = _register_github(
        db, thread_id, turn_id,
        type_="issue", external_id="cebertowicz/corsia#1825",
    )

    items = {
        n["id"]: n
        for n in view.pane_for_thread(thread_id)["nodes"]
        if n["kind"] == "artifact"
    }
    assert items[pr["id"]]["retirement_exempt"] is True
    assert items[issue["id"]]["retirement_exempt"] is False


def _merge(db, external_id: str):
    return pr_lifecycle.record_pull_request_ended(
        "github", "pull-request", external_id, pr_state="merged", db=db,
    )


def test_a_merged_pull_request_is_no_longer_exempt(db):
    """Pinned so it stays on the pane, the merged row reports it can retire."""
    thread_id, node, turn_id = _thread_with_active_node(db, "merged")
    pr = _register_github(
        db, thread_id, turn_id,
        type_="pull-request", external_id="cebertowicz/corsia#1826",
    )
    pinning.update_pinned(pr["id"], pinned=True, parent_thread_id=thread_id, db=db)
    _merge(db, "cebertowicz/corsia#1826")

    item = next(
        n for n in view.pane_for_thread(thread_id)["nodes"] if n["id"] == pr["id"]
    )
    assert item["retirement_exempt"] is False

    retired = retire_registrations_for_node(
        db, node_id=node.id, trigger="accepted", causal_event_id=None,
    )
    assert retired == 0  # the merge already recorded this row's retirement


def test_re_registering_a_merged_pull_request_does_not_reopen_its_row(db):
    thread_id, first_node, turn_id = _thread_with_active_node(db, "rebind")
    pr = _register_github(
        db, thread_id, turn_id,
        type_="pull-request", external_id="cebertowicz/corsia#1827",
    )
    _merge(db, "cebertowicz/corsia#1827")
    stamped = _row(db, pr["id"], thread_id)["retired_at"]

    _register_github(
        db, thread_id, turn_id,
        type_="pull-request", external_id="cebertowicz/corsia#1827",
    )
    db.commit()

    row = _row(db, pr["id"], thread_id)
    assert (row["surfacing"], row["retired_at"]) == ("settled", stamped)
    assert pr["id"] not in {
        n["id"] for n in view.pane_for_thread(thread_id)["nodes"]
    }


def test_a_fresh_subscription_to_an_already_ended_pull_request_starts_retired(db):
    """A brand new (artifact, thread) pair -- not a rebind -- must not skip the guard."""
    thread_id, _node, turn_id = _thread_with_active_node(db, "fresh-sub")
    pr = _register_github(
        db, thread_id, turn_id,
        type_="pull-request", external_id="cebertowicz/corsia#1829",
    )
    _merge(db, "cebertowicz/corsia#1829")

    subscriber_thread = make_session(db, prefix="pr-exempt-fresh-sub-subscriber")
    models.register_artifact(
        integration="github",
        type="pull-request",
        external_id="cebertowicz/corsia#1829",
        parent_thread_id=subscriber_thread,
        title="cebertowicz/corsia#1829",
        db=db,
    )
    db.commit()

    row = _row(db, pr["id"], subscriber_thread)
    assert row["surfacing"] == "settled"
    assert row["retired_at"] is not None
    # Not a second, silent writer: the same audited event as every other
    # retirement, through the shared registry.node_retirement writer -- the
    # exact gap the round-2 review named.
    event = db.execute(
        """SELECT authority FROM artifact_registration_events
           WHERE artifact_id=? AND affected_thread_id=? AND event_type='artifact_ended'""",
        (pr["id"], subscriber_thread),
    ).fetchone()
    assert event is not None
    assert event["authority"] == "artifact-ended"


def test_re_registering_an_open_pull_request_still_rebinds(db):
    """The rebind guard is the ended state, not the PR type."""
    thread_id, node, turn_id = _thread_with_active_node(db, "open-rebind")
    pr = _register_github(
        db, thread_id, turn_id,
        type_="pull-request", external_id="cebertowicz/corsia#1828",
    )
    db.execute(
        "UPDATE artifact_registrations SET surfacing='settled' WHERE artifact_id=?",
        (pr["id"],),
    )
    db.commit()

    _register_github(
        db, thread_id, turn_id,
        type_="pull-request", external_id="cebertowicz/corsia#1828",
    )
    db.commit()

    assert _row(db, pr["id"], thread_id)["surfacing"] == "visible"
