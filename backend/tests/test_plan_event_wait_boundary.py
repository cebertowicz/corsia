"""The event a task waited for ends its wait — the right event, once it arrives.

A wait on something outside the plan names two things: the artifact whose events
can end it, and the event that does. Both halves are load-bearing here.

- An artifact is ``(integration, type, external_id)``. Matching on the external
  id alone would let one integration's pull request end another's wait.
- The wait ends only once the event has reached the waiting thread. Settling it
  earlier would re-admit a task whose owner never received the payload.

And a thread may bind a wait only to an artifact it owns: the owner is the one
thread the router delivers the artifact's events to, so any other thread's wait
could end only at its deadline.
"""

from __future__ import annotations

import uuid

import pytest

from events.identity import GITHUB_INTEGRATION, PR_TYPE, pr_external_id
from plans import admission, service, watch_resolution
from plans.boundary import PlanActor, mutate
from tests.event_routing_helpers import mk_session, register_pr, pr_payload, route

REPO = "owner/repo"
OPENED = "webhook.github.pull_request_opened"


def _owner(db) -> PlanActor:
    mk_session("owner")
    return PlanActor("owner", "user-1", None)


def _artifact_id(db, external_id: str) -> str:
    """The artifact's own id: what a wait binds to, never the external id."""
    from registry import artifact_record

    return str(artifact_record.identity_row_for(external_id, db=db)["id"])


def _watching(db, actor, *, external_id: str, event: str):
    """A task of the owner's, waiting on one artifact's named event."""
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Await CI",
        acceptance_criteria="CI is green", db=db,
    )
    service.transition_node(node.id, "active", db=db)
    db.commit()
    mutate(
        plan.id, "arm_watch",
        {
            "node_id": node.id,
            "condition": {"type": "event", "value": "ci.complete"},
            "artifact_id": _artifact_id(db, external_id),
            "expected_event": event,
            "expected_revision": service._node_row(db, node.id)["revision"],
        },
        actor=actor, provenance="test.event_wait", mutation_id=uuid.uuid4().hex, db=db,
    )
    db.commit()
    return plan, node


def _armed(db, node_id: str) -> int:
    return int(db.execute(
        "SELECT COUNT(*) FROM expected_event_watches "
        "WHERE plan_node_id=? AND status='armed'",
        (node_id,),
    ).fetchone()[0])


def test_the_named_event_on_the_bound_artifact_ends_the_wait(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        actor = _owner(db)
        register_pr(REPO, 7, "owner")
        _plan, node = _watching(
            db, actor, external_id=pr_external_id(REPO, 7), event=OPENED,
        )
        assert admission.project(node.id, db=db)["condition"] == "waiting"

        resolved = watch_resolution.resolve_delivered_event(
            _identity(pr_external_id(REPO, 7)), OPENED, recipient="owner", db=db,
        )
        db.commit()

        assert len(resolved) == 1
        assert admission.project(node.id, db=db)["causes"] == []
    finally:
        db.close()


def test_rescheduling_a_task_keeps_its_artifact_bound_event_wait(isolated_db):
    """#903: a new schedule retires old schedules only, never a wait on a PR."""
    from datetime import datetime, timedelta, timezone
    from infra.database import get_db

    db = get_db()
    try:
        actor = _owner(db)
        register_pr(REPO, 7, "owner")
        plan, node = _watching(
            db, actor, external_id=pr_external_id(REPO, 7), event=OPENED,
        )
        pr_watch = db.execute(
            "SELECT id FROM expected_event_watches WHERE plan_node_id=?", (node.id,),
        ).fetchone()["id"]

        def schedule(days: int):
            result = mutate(
                plan.id, "arm_watch",
                {
                    "node_id": node.id, "reason": "Come back",
                    "condition": {"type": "deadline", "value": (
                        datetime.now(timezone.utc) + timedelta(days=days)
                    ).replace(microsecond=0).isoformat()},
                    "expected_revision": service._node_row(db, node.id)["revision"],
                },
                actor=actor, provenance="test.event_wait",
                mutation_id=uuid.uuid4().hex, db=db,
            ).result
            db.commit()
            return result

        first = schedule(10)["watch"]["id"]
        second = schedule(20)

        statuses = {
            row["id"]: row["status"] for row in db.execute(
                "SELECT id,status FROM expected_event_watches WHERE plan_node_id=?",
                (node.id,),
            )
        }
        assert statuses == {pr_watch: "armed", first: "resolved", second["watch"]["id"]: "armed"}
        assert second["superseded_watch_ids"] == [first]
    finally:
        db.close()


def test_an_unrelated_event_and_another_integrations_artifact_do_not_end_it(
    isolated_db,
):
    from infra.database import get_db

    db = get_db()
    try:
        actor = _owner(db)
        register_pr(REPO, 7, "owner")
        external_id = pr_external_id(REPO, 7)
        _plan, node = _watching(db, actor, external_id=external_id, event=OPENED)

        # The same external id, a different integration: a different artifact.
        elsewhere = watch_resolution.resolve_delivered_event(
            _identity(external_id, integration="gitlab"), OPENED,
            recipient="owner", db=db,
        )
        unrelated = watch_resolution.resolve_delivered_event(
            _identity(external_id), "webhook.github.issue_comment",
            recipient="owner", db=db,
        )
        db.commit()

        assert (elsewhere, unrelated) == ([], [])
        assert _armed(db, node.id) == 1
        assert admission.project(node.id, db=db)["condition"] == "waiting"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_delivery_that_fails_leaves_the_wait_armed(isolated_db, monkeypatch):
    from events import delivery
    from infra.database import get_db

    db = get_db()
    try:
        actor = _owner(db)
        register_pr(REPO, 7, "owner")
        _plan, node = _watching(
            db, actor, external_id=pr_external_id(REPO, 7), event=OPENED,
        )

        async def undelivered(*_args, **_kwargs):
            return False

        monkeypatch.setattr(delivery, "deliver_to_thread", undelivered)
        await route(OPENED, pr_payload(REPO, 7))

        assert _armed(db, node.id) == 1
        assert admission.project(node.id, db=db)["condition"] == "waiting"
    finally:
        db.close()


def test_a_wait_may_only_name_an_artifact_this_thread_owns(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        actor = _owner(db)
        mk_session("stranger")
        register_pr(REPO, 9, "stranger")
        plan = service.ensure_plan("thread", "owner", db=db)
        node = service.add_node(
            plan.id, kind="task", title="Await CI",
            acceptance_criteria="CI is green", db=db,
        )
        service.transition_node(node.id, "active", db=db)
        db.commit()

        with pytest.raises(service.PlanInputError):
            mutate(
                plan.id, "arm_watch",
                {
                    "node_id": node.id,
                    "condition": {"type": "event", "value": "ci.complete"},
                    "artifact_id": _artifact_id(db, pr_external_id(REPO, 9)),
                    "expected_event": OPENED,
                    "expected_revision": service._node_row(db, node.id)["revision"],
                },
                actor=actor, provenance="test.event_wait",
                mutation_id=uuid.uuid4().hex, db=db,
            )

        assert _armed(db, node.id) == 0
    finally:
        db.close()


def test_a_wait_binds_by_the_artifacts_id_never_its_external_id(isolated_db):
    """An external id is not an identity: two integrations can share one.

    Bound through an external id, a wait could land on an arbitrary artifact
    and have its authorization checked against the wrong record.
    """
    from infra.database import get_db

    db = get_db()
    try:
        actor = _owner(db)
        register_pr(REPO, 7, "owner")
        plan = service.ensure_plan("thread", "owner", db=db)
        node = service.add_node(
            plan.id, kind="task", title="Await CI",
            acceptance_criteria="CI is green", db=db,
        )
        service.transition_node(node.id, "active", db=db)
        db.commit()

        with pytest.raises(service.PlanInputError, match="must be the id"):
            mutate(
                plan.id, "arm_watch",
                {
                    "node_id": node.id,
                    "condition": {"type": "event", "value": "ci.complete"},
                    "artifact_id": pr_external_id(REPO, 7),
                    "expected_event": OPENED,
                    "expected_revision": service._node_row(db, node.id)["revision"],
                },
                actor=actor, provenance="test.event_wait",
                mutation_id=uuid.uuid4().hex, db=db,
            )

        assert _armed(db, node.id) == 0
    finally:
        db.close()


def test_a_subscriber_cannot_wait_on_an_event_it_is_never_delivered(isolated_db):
    """A subscriber sees an artifact's events as digest facts, not deliveries.

    The router delivers each event to the artifact's owner and ends only that
    thread's wait. A wait a subscriber armed could end only at its deadline,
    so the boundary refuses it before the plan ever waits on it.
    """
    from infra.database import get_db
    from registry import artifact_record

    db = get_db()
    try:
        _owner(db)
        mk_session("watcher")
        watcher = PlanActor("watcher", "user-1", None)
        register_pr(REPO, 7, "owner")
        external_id = pr_external_id(REPO, 7)
        artifact_record.subscribe_parent_chain(
            {"integration": GITHUB_INTEGRATION, "type": PR_TYPE,
             "external_id": external_id},
            "watcher", db=db,
        )
        db.commit()
        plan = service.ensure_plan("thread", "watcher", db=db)
        node = service.add_node(
            plan.id, kind="task", title="Await CI",
            acceptance_criteria="CI is green", db=db,
        )
        service.transition_node(node.id, "active", db=db)
        db.commit()

        with pytest.raises(service.PlanInputError):
            mutate(
                plan.id, "arm_watch",
                {
                    "node_id": node.id,
                    "condition": {"type": "event", "value": "ci.complete"},
                    "artifact_id": _artifact_id(db, external_id),
                    "expected_event": OPENED,
                    "expected_revision": service._node_row(db, node.id)["revision"],
                },
                actor=watcher, provenance="test.event_wait",
                mutation_id=uuid.uuid4().hex, db=db,
            )

        assert _armed(db, node.id) == 0
    finally:
        db.close()


def test_a_delivery_to_another_thread_does_not_end_the_owners_wait(isolated_db):
    """The wait ends when the event reaches the thread that armed it.

    Delivered elsewhere -- to a thread the artifact was since handed to --
    the payload never reached the waiting task, and ending its wait would
    re-admit work with nothing to continue on.
    """
    from infra.database import get_db

    db = get_db()
    try:
        owner = _owner(db)
        register_pr(REPO, 7, "owner")
        external_id = pr_external_id(REPO, 7)
        _plan, node = _watching(db, owner, external_id=external_id, event=OPENED)

        resolved = watch_resolution.resolve_delivered_event(
            _identity(external_id), OPENED, recipient="stranger", db=db,
        )
        db.commit()

        assert resolved == []
        assert _armed(db, node.id) == 1
    finally:
        db.close()


def _identity(external_id: str, *, integration: str = GITHUB_INTEGRATION):
    from events.identity import RoutingIdentity

    return RoutingIdentity(integration, PR_TYPE, external_id)
