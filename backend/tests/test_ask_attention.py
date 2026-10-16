"""One reconciler brings an artifact's attention in line with what still raises it.

An open review ask is held in two stores: the plan hold and the artifact's
attention. Every ask edge (placing, restating, ending or withdrawing one) calls
``plans.ask_attention.sync_ask_attention``. These prove it leaves alone what an
ask does not own: a parent review's raise or deferral, a raised plan decision,
and a deferral the user chose.
"""

from __future__ import annotations

import json
import types
import uuid

import pytest

from plans import service
from plans.ask_attention import sync_ask_attention
from plans.boundary import PlanActor, mutate
from registry import attention
from tests.plan_ask_helpers import document_ask
from tests.test_attention_queue import db, raise_decision  # noqa: F401 - shared fixtures

AGENT = PlanActor("root", "user-1", "project-1")
_DEADLINE = {"type": "deadline", "value": "2026-06-27T09:00:00Z"}


@pytest.fixture(autouse=True)
def _no_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


def _task(db) -> tuple[str, str]:
    plan = service.ensure_plan("thread", "root", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Review", acceptance_criteria="Reviewed", db=db,
    )
    db.commit()
    return plan.id, node.id


def _block(db, plan_id: str, node_id: str, ask: str, *, reason: str = "Review it") -> None:
    mutate(
        plan_id, "block_node", {
            "node_id": node_id, "reason_key": "review", "reason": reason,
            "owner_kind": "user", "ask_artifact_id": ask,
            "expected_revision": service._node_row(db, node_id)["revision"],
        }, actor=AGENT, provenance="test.ask_attention", mutation_id=uuid.uuid4().hex, db=db,
    )
    db.commit()


def _open_holds(db, node_id: str) -> list:
    return db.execute(
        "SELECT id,reason FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL",
        (node_id,),
    ).fetchall()


def _end_ask(db, plan_id: str, node_id: str, operation: str) -> None:
    """The agent ends its own ask: by resolving the blocker, or by closing the task."""
    params = {"node_id": node_id, "expected_revision": service._node_row(db, node_id)["revision"]}
    if operation == "resolve_blocker":
        [hold] = _open_holds(db, node_id)
        params["hold_id"] = hold["id"]
    else:
        params["reason"] = "No longer needed"
    mutate(
        plan_id, operation, params, actor=AGENT, provenance="test.ask_attention",
        mutation_id=uuid.uuid4().hex, db=db,
    )
    db.commit()


def _dispose(db, ask: str, outcome: str, **kwargs) -> None:
    from parent_review import disposition

    disposition.dispose(ask, outcome, db=db, **kwargs)
    db.commit()


def _approve_to_user(db, ask: str) -> None:
    from parent_review.disposition import APPROVE_TO_USER

    _dispose(
        db, ask, APPROVE_TO_USER, review_result=types.SimpleNamespace(blocking_failure=False),
    )


def _metadata(db, artifact_id: str) -> dict:
    raw = db.execute(
        "SELECT metadata FROM artifact_identities WHERE id=?", (artifact_id,),
    ).fetchone()["metadata"]
    return json.loads(raw) if raw else {}


@pytest.mark.parametrize("approve_first", (False, True))
@pytest.mark.parametrize("operation", ("resolve_blocker", "close_node"))
def test_ending_a_tasks_ask_keeps_a_parent_reviews_raise(db, operation, approve_first):
    plan_id, node_id = _task(db)
    ask = document_ask(db, "root")
    if approve_first:
        _approve_to_user(db, ask)
    _block(db, plan_id, node_id, ask)
    if not approve_first:
        _approve_to_user(db, ask)

    _end_ask(db, plan_id, node_id, operation)

    assert _open_holds(db, node_id) == []
    assert attention.get_attention_state(ask, db=db) == "raised"


def test_a_parent_approval_the_user_cleared_does_not_hold_a_later_withdrawn_ask(db):
    plan_id, node_id = _task(db)
    ask = document_ask(db, "root")
    _approve_to_user(db, ask)
    attention.clear_attention(ask, outcome="acknowledge", db=db)
    _block(db, plan_id, node_id, ask)
    # The ask started a new episode: the earlier episode's disposition is gone.
    assert attention.get_attention_state(ask, db=db) == "raised"
    assert "disposition_outcome" not in _metadata(db, ask)

    _end_ask(db, plan_id, node_id, "resolve_blocker")

    assert attention.get_attention_state(ask, db=db) == "cleared"
    assert _metadata(db, ask)["attention_outcome"] == "withdraw"


def test_a_user_defer_of_a_parent_approval_keeps_it_past_a_withdrawn_ask(db):
    plan_id, node_id = _task(db)
    ask = document_ask(db, "root")
    _approve_to_user(db, ask)
    attention.clear_attention(ask, outcome="defer", condition=_DEADLINE, db=db)
    _block(db, plan_id, node_id, ask)

    _end_ask(db, plan_id, node_id, "resolve_blocker")

    assert attention.get_attention_state(ask, db=db) == "deferred"
    assert _metadata(db, ask)["disposition_outcome"] == "approve_to_user"


@pytest.mark.parametrize("prior,kept", (
    (None, False), ("cleared", False), ("raised", True), ("deferred", True),
))
def test_only_a_raise_that_starts_an_episode_drops_the_disposition(db, prior, kept):
    from registry import models

    ask = document_ask(db, "root")
    external_id = db.execute(
        "SELECT external_id FROM artifact_identities WHERE id=?", (ask,),
    ).fetchone()["external_id"]
    models.update_metadata(
        "workspace", "document", external_id,
        {"attention_state": prior, "disposition_outcome": "approve_to_user"}, db=db,
    )

    attention.raise_attention(ask, db=db)

    metadata = _metadata(db, ask)
    assert metadata["attention_state"] == "raised"
    assert ("disposition_outcome" in metadata) is kept


def test_a_tasks_ask_ending_on_a_parent_deferral_stays_deferred_until_its_watch(db):
    from automation.watches import arm_watch, expire_watch
    from parent_review.disposition import DEFER_UNTIL

    plan_id, node_id = _task(db)
    ask = document_ask(db, "root")
    _block(db, plan_id, node_id, ask)
    _dispose(db, ask, DEFER_UNTIL, condition=_DEADLINE, reason="After the release")

    _end_ask(db, plan_id, node_id, "resolve_blocker")

    assert _open_holds(db, node_id) == []
    assert attention.get_attention_state(ask, db=db) == "deferred"
    watch = arm_watch(ask, _DEADLINE, db=db)
    expire_watch(watch.id, db=db)
    assert attention.get_attention_state(ask, db=db) == "raised"


@pytest.mark.parametrize("reason", ("Review it", "Review the revised draft"))
def test_reblocking_after_the_user_deferred_keeps_the_ask_deferred(db, reason):
    plan_id, node_id = _task(db)
    ask = document_ask(db, "root")
    _block(db, plan_id, node_id, ask)
    attention.clear_attention(ask, outcome="defer", condition=_DEADLINE, db=db)

    _block(db, plan_id, node_id, ask, reason=reason)

    [hold] = _open_holds(db, node_id)
    assert hold["reason"] == reason
    assert attention.get_attention_state(ask, db=db) == "deferred"


def test_a_restated_hold_names_the_hold_that_replaced_it(db):
    plan_id, node_id = _task(db)
    ask = document_ask(db, "root")
    _block(db, plan_id, node_id, ask)
    [first] = _open_holds(db, node_id)

    _block(db, plan_id, node_id, ask, reason="Review the revised draft")

    [second] = _open_holds(db, node_id)
    payload = json.loads(db.execute(
        """SELECT e.payload_json FROM plan_node_holds h
             JOIN plan_node_events e ON e.id=h.clearance_event_id
            WHERE h.id=?""",
        (first["id"],),
    ).fetchone()["payload_json"])
    assert payload["detail"] == {"ended": "restated", "successor": second["id"]}
    assert attention.get_attention_state(ask, db=db) == "raised"


def test_a_raised_decision_is_a_cause_no_ask_edge_clears(db):
    from plans import decision_projection

    decision = raise_decision(db, "root", "root")
    artifact_id = db.execute(
        "SELECT id FROM artifact_identities WHERE type=? AND external_id=?",
        (decision_projection.DECISION_TYPE, decision["id"]),
    ).fetchone()["id"]
    assert attention.get_attention_state(artifact_id, db=db) == "raised"

    assert sync_ask_attention(db, artifact_id, outcome="withdraw") is None
    assert attention.get_attention_state(artifact_id, db=db) == "raised"


def test_attention_nothing_raises_ends_and_an_ask_raises_it_again(db):
    plan_id, node_id = _task(db)
    ask = document_ask(db, "root")
    _block(db, plan_id, node_id, ask)
    _end_ask(db, plan_id, node_id, "resolve_blocker")
    assert attention.get_attention_state(ask, db=db) == "cleared"
    # Nothing raises it, so a second ending writes nothing.
    assert sync_ask_attention(db, ask, outcome="withdraw") is None

    _block(db, plan_id, node_id, ask)

    assert attention.get_attention_state(ask, db=db) == "raised"


def test_only_an_ask_ending_is_an_outcome(db):
    ask = document_ask(db, "root")

    with pytest.raises(ValueError, match="not an ask ending"):
        sync_ask_attention(db, ask, outcome="answer")
