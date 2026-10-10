"""A delivered reviewer verdict resolves its armed watch — issue #1995.

``github_reviewer_notifications.deliver_owner_event`` used to deliver via
``deliver_to_thread`` directly and never call
``plans.watch_resolution.resolve_delivered_event`` -- the only seam that
ends a plan task's wait on an artifact's event -- unlike the general router
(``events/router.py``), which always resolves right after a successful
delivery. A task waiting on ``reviewer.verdict`` never woke.
"""

from __future__ import annotations

import asyncio
import uuid

from events.identity import GITHUB_INTEGRATION, PR_TYPE, pr_external_id
from plans import admission, service
from plans.boundary import PlanActor, mutate
from tests.event_routing_helpers import mk_session, register_pr

REPO = "owner/repo"


def _artifact_id(db, external_id: str) -> str:
    from registry import artifact_record

    return str(artifact_record.identity_row_for(external_id, db=db)["id"])


def _watching_verdict(db, actor, *, external_id: str):
    """A task of the owner's, waiting on the PR's reviewer verdict."""
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Await reviewer verdict",
        acceptance_criteria="The reviewer verdict resolves this wait", db=db,
    )
    service.transition_node(node.id, "active", db=db)
    db.commit()
    mutate(
        plan.id, "arm_watch",
        {
            "node_id": node.id,
            "condition": {"type": "event", "value": "reviewer.verdict"},
            "artifact_id": _artifact_id(db, external_id),
            "expected_event": "reviewer.verdict",
            "expected_revision": service._node_row(db, node.id)["revision"],
        },
        actor=actor, provenance="test.reviewer_verdict_watch",
        mutation_id=uuid.uuid4().hex, db=db,
    )
    db.commit()
    return plan, node


def _armed(db, node_id: str) -> int:
    return int(db.execute(
        "SELECT COUNT(*) FROM expected_event_watches "
        "WHERE plan_node_id=? AND status='armed'",
        (node_id,),
    ).fetchone()[0])


def _call_on_backend_loop(callback, *args, **kwargs):
    """Run one sync ``loop=``-taking callback against a genuinely running loop."""

    async def invoke():
        loop = asyncio.get_running_loop()
        return await asyncio.to_thread(callback, *args, loop=loop, **kwargs)

    return asyncio.run(invoke())


def test_reviewer_verdict_resolves_its_armed_watch(isolated_db, monkeypatch):
    from events import delivery
    from events.connectors import github_reviewer_notifications
    from infra.database import get_db

    async def delivered(*_args, **_kwargs):
        return True

    monkeypatch.setattr(delivery, "deliver_to_thread", delivered)

    db = get_db()
    try:
        mk_session("owner")
        actor = PlanActor("owner", "user-1", None)
        register_pr(REPO, 7, "owner")
        _plan, node = _watching_verdict(
            db, actor, external_id=pr_external_id(REPO, 7),
        )
        assert admission.project(node.id, db=db)["condition"] == "waiting"
        assert _armed(db, node.id) == 1
    finally:
        db.close()

    result = _call_on_backend_loop(
        github_reviewer_notifications.notify_reviewer_verdict,
        REPO, 7, "abc123def", {"verdict": "clean"},
    )
    assert result == "delivered"

    db = get_db()
    try:
        assert _armed(db, node.id) == 0
        assert admission.project(node.id, db=db)["causes"] == []
    finally:
        db.close()

    # A second delivery of the same verdict resolves nothing further: the
    # watch is already gone, so this is a plain no-op re-delivery.
    result2 = _call_on_backend_loop(
        github_reviewer_notifications.notify_reviewer_verdict,
        REPO, 7, "abc123def", {"verdict": "clean"},
    )
    assert result2 == "delivered"
    db = get_db()
    try:
        assert _armed(db, node.id) == 0
    finally:
        db.close()
