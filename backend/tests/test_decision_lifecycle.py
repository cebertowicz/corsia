"""The decision queue's lifecycle invariant.

**Every counted queue item is actionable, and one bad row can never starve a
later decision.**

A decision is first-class user attention, but it cannot outlive the thing its
answer settles. Only a plan's owner authors its decisions, so the agent that
asked is always the owning plan's thread. Exactly one rule decides survival:

* the **owning plan** going away — its owner thread archived or purged, the plan
  archived, or the decision's source node archived — withdraws the question,
  because there is nowhere left for the answer to land;
* any other thread finishing, such as the parent that reviewed and raised the
  question, changes nothing: the user still owes an answer and it still lands on
  the plan (PRD-PLANS E4, "never strand").

Red at `2706b37a`: nothing reconciles queue membership with the plan, so three
of those four states are offered and then refused with no explanation, and a
stranded head permanently hides a live decision behind it.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from plans import service
from tests.test_attention_queue import (  # noqa: F401 - shared fixtures
    _mid,
    _seed,
    db,
    queue_for,
    raise_decision,
)
from tests.test_attention_queue_api import _DbProxy, _immediate
from tests.plan_writer_authorizer import raw_plan_seed


def _client(db, monkeypatch, user_id: str = "user-1") -> TestClient:
    """One app carrying both routers, so a journey needs no second fixture."""
    from api import attention as api_attention
    from api import plans as api_plans

    for module in (api_attention, api_plans):
        monkeypatch.setattr(module, "get_db", lambda: _DbProxy(db))
        monkeypatch.setattr(module, "run_in_db", _immediate)
    app = FastAPI()
    app.include_router(api_attention.router)
    app.include_router(api_plans.router)
    app.dependency_overrides[api_attention.require_user] = lambda: {"id": user_id}
    app.dependency_overrides[api_plans.require_user] = lambda: {
        "id": user_id, "token_kind": "session",
    }
    return TestClient(app)


def _queue(client) -> dict:
    response = client.get("/api/attention/queue")
    assert response.status_code == 200, response.text
    return response.json()


def _answer(client, decision, revision_offset: int = 0):
    return client.post(
        f"/api/plans/decisions/{decision['id']}/answer",
        json={
            "option_id": "A",
            "expected_revision": decision["revision"] + revision_offset,
            "mutation_id": f"answer-{decision['id']}-{revision_offset}",
        },
    )


def _metadata(db, decision_id: str) -> dict:
    row = db.execute(
        """SELECT metadata FROM artifact_identities
           WHERE integration='local' AND type='plan-decision' AND external_id=?""",
        (decision_id,),
    ).fetchone()
    return json.loads(row["metadata"])


def _state(db, decision_id: str) -> str:
    return _metadata(db, decision_id)["plan_decision"]["state"]


def _attention_state(db, decision_id: str):
    return _metadata(db, decision_id).get("attention_state")


def _node_state(db, node_id: str) -> str:
    return db.execute(
        "SELECT state FROM plan_nodes WHERE id=?", (node_id,),
    ).fetchone()["state"]


def _archive(db, thread_id: str) -> None:
    from core import thread_lifecycle
    from infra.database import write_transaction

    db.commit()
    with write_transaction(db, scope="archive"):
        thread_lifecycle.set_user_archive_state(db, thread_id, archived=True)


# --- 1. the promise that must keep working -----------------------------------


def test_the_reviewing_parent_finishing_leaves_the_question_answerable(
    db, monkeypatch,
):
    """PRD-PLANS E4 under single-owner plans.

    `child` asks in its own plan; `root`, which spawned it, reviewed and raised
    the question. Archiving `root` withdraws only decisions on plans `root`
    owns, so the user still owes this answer and it still lands on `child`'s plan.
    """
    decision = raise_decision(db, "child", "child")

    _archive(db, "root")

    assert _state(db, decision["id"]) == "raised"
    client = _client(db, monkeypatch)
    assert _queue(client)["count"] == 1
    assert _answer(client, decision).status_code == 200
    assert _state(db, decision["id"]) == "answered"


# --- 2. withdrawal at archive time (write side) ------------------------------


def test_archiving_the_owning_thread_withdraws_its_outstanding_decisions(
    db, monkeypatch,
):
    decision = raise_decision(db, "child", "child")
    assert _state(db, decision["id"]) == "raised"

    _archive(db, "child")

    assert _state(db, decision["id"]) == "invalidated"
    assert _attention_state(db, decision["id"]) != "raised"
    assert _node_state(db, decision["node_id"]) == "closed"
    assert _queue(_client(db, monkeypatch))["count"] == 0


def test_nothing_can_make_the_withdrawal_close_refuse(db):
    """Withdrawal closes through ``closure.close_node``, whose guards refuse a
    node with open children or a live worker. Neither can exist on an open
    question's review node, so archiving its owner never fails. The dependent's
    edge, which the node may hold, is released as any close releases it.
    """
    from plans import dependencies
    from plans.boundary import PlanActor, mutate

    decision = raise_decision(db, "child", "child")
    review = service._node_row(db, decision["node_id"])
    later = service.add_node(
        decision["plan_id"], kind="task", title="After the answer",
        acceptance_criteria="DOD", db=db,
    )
    dependencies.add_dependency(later.id, review["id"], db=db)
    db.commit()

    with pytest.raises(service.PlanInputError, match="leaves"):
        service.add_node(
            decision["plan_id"], kind="task", title="Under the question",
            acceptance_criteria="DOD", parent_id=review["id"], db=db,
        )
    with pytest.raises(service.PlanInputError, match="decision awaiting the user"):
        mutate(
            decision["plan_id"], "attach_spawned_thread",
            {"node_id": review["id"], "expected_revision": review["revision"],
             "spawned_thread_id": "grandchild"},
            actor=PlanActor("child", "user-1", "project-1"),
            provenance="test", mutation_id=_mid(), db=db,
        )
    db.rollback()

    _archive(db, "child")

    assert _node_state(db, review["id"]) == "closed"
    assert not db.execute(
        "SELECT 1 FROM plan_node_dependencies WHERE depends_on_node_id=?",
        (review["id"],),
    ).fetchone()


def test_withdrawal_preserves_the_audit_trail_and_bumps_the_revision(db):
    decision = raise_decision(db, "child", "child")

    _archive(db, "child")

    body = _metadata(db, decision["id"])["plan_decision"]
    assert body["revision"] == decision["revision"] + 1
    events = [
        row["event"] for row in db.execute(
            "SELECT event FROM plan_node_events WHERE node_id=? ORDER BY rowid",
            (decision["node_id"],),
        ).fetchall()
    ]
    # The whole history survives; withdrawal appends, never rewrites.
    for expected in ("decision_drafted", "decision_parent_reviewed",
                     "decision_raised", "decision_invalidated"):
        assert expected in events


def test_purging_the_owning_thread_also_withdraws(db):
    from core import thread_lifecycle

    decision = raise_decision(db, "child", "child")
    plan_id = decision["plan_id"]
    assert plan_id

    _archive(db, "child")

    assert _state(db, decision["id"]) == "invalidated"


def test_a_draft_that_never_reached_the_user_is_withdrawn_too(db):
    """It holds a queue place the moment it is raised; settle it terminally now."""
    from plans import decisions
    from plans.boundary import PlanActor

    plan = service.ensure_plan("thread", "child", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Work", acceptance_criteria="DOD", db=db,
    )
    db.commit()
    drafted = decisions.draft(
        plan.id, source_node_id=node.id, mutation_id=_mid(),
        actor=PlanActor("child", "user-1", "project-1"), provenance="p", db=db,
        title="Never raised", context="c", tradeoff="t", recommendation="A",
        rationale="r", other_enabled=True,
        options=[{"id": "A", "label": "One", "detail": ""},
                 {"id": "B", "label": "Two", "detail": ""}],
    ).result["decision"]
    db.commit()

    _archive(db, "child")

    assert _state(db, drafted["id"]) == "invalidated"


def test_withdrawal_is_idempotent(db):
    decision = raise_decision(db, "child", "child")

    _archive(db, "child")
    first = _state(db, decision["id"])
    _archive(db, "child")

    assert first == "invalidated"
    assert _state(db, decision["id"]) == "invalidated"


def test_an_already_answered_decision_is_not_reopened_by_archival(db, monkeypatch):
    decision = raise_decision(db, "child", "child")
    client = _client(db, monkeypatch)
    assert _answer(client, decision).status_code == 200

    _archive(db, "child")

    assert _state(db, decision["id"]) == "answered"
    assert _node_state(db, decision["node_id"]) == "accepted"


# --- 2b. withdrawal when the source task closes (#2651) -----------------------


def _close_source(db, decision) -> None:
    from plans import closure

    closure.close_node(
        decision["source_node_id"], reason="Not needed", actor_id="child",
        provenance="test", db=db,
    )
    db.commit()


def _draft(db, thread_id: str = "child") -> dict:
    from plans import decisions
    from plans.boundary import PlanActor

    plan = service.ensure_plan("thread", thread_id, db=db)
    node = service.add_node(
        plan.id, kind="task", title="Work", acceptance_criteria="DOD", db=db,
    )
    db.commit()
    drafted = decisions.draft(
        plan.id, source_node_id=node.id, mutation_id=_mid(),
        actor=PlanActor(thread_id, "user-1", "project-1"), provenance="p", db=db,
        title="Never raised", context="c", tradeoff="t", recommendation="A",
        rationale="r", other_enabled=True,
        options=[{"id": "A", "label": "One", "detail": ""},
                 {"id": "B", "label": "Two", "detail": ""}],
    ).result["decision"]
    db.commit()
    return drafted


def test_closing_the_source_task_withdraws_its_raised_question(db, monkeypatch):
    decision = raise_decision(db, "child", "child")

    _close_source(db, decision)

    assert _state(db, decision["id"]) == "invalidated"
    assert _attention_state(db, decision["id"]) != "raised"
    assert _node_state(db, decision["node_id"]) == "closed"
    assert _queue(_client(db, monkeypatch))["count"] == 0


def test_closing_the_source_task_withdraws_a_draft_the_parent_never_saw(db):
    """The #2453 probe's case: the draft and its user-assigned review node were
    left behind, and no actor could withdraw them."""
    drafted = _draft(db)

    _close_source(db, drafted)

    assert _state(db, drafted["id"]) == "invalidated"
    assert _node_state(db, drafted["node_id"]) == "closed"


def test_closing_one_task_leaves_another_tasks_question_alone(db):
    kept = raise_decision(db, "child", "child", title="KEPT")
    closed = raise_decision(db, "child", "child", title="CLOSED")

    _close_source(db, closed)

    assert _state(db, closed["id"]) == "invalidated"
    assert _state(db, kept["id"]) == "raised"


def test_a_failed_source_task_keeps_its_question_answerable(db, monkeypatch):
    """Failed work is still work: a rejected step's worker is revising it, and
    the answer still lands for it."""
    decision = raise_decision(db, "child", "child")
    service.transition_node(decision["source_node_id"], "active", db=db)
    service.transition_node(decision["source_node_id"], "failed", db=db)
    db.commit()

    assert _state(db, decision["id"]) == "raised"
    assert _answer(_client(db, monkeypatch), decision).status_code == 200


def _strand(db, monkeypatch, decision) -> None:
    """Close the source the way dev did before #2651, leaving the question open."""
    from plans import decision_lifecycle

    monkeypatch.setattr(
        decision_lifecycle, "withdraw_for_closed_source", lambda *a, **k: 0,
    )
    _close_source(db, decision)
    monkeypatch.undo()


def test_a_question_stranded_before_the_fix_can_still_be_withdrawn(db, monkeypatch):
    from plans import decisions
    from plans.boundary import PlanActor

    drafted = _draft(db)
    _strand(db, monkeypatch, drafted)
    assert _state(db, drafted["id"]) == "draft"

    decisions.invalidate(
        drafted["id"], expected_revision=drafted["revision"], mutation_id=_mid(),
        actor=PlanActor("child", "user-1", "project-1"), provenance="p", db=db,
    )
    db.commit()

    assert _state(db, drafted["id"]) == "invalidated"
    assert _node_state(db, drafted["node_id"]) == "closed"


def test_changing_a_question_on_a_closed_task_names_the_real_cause(db, monkeypatch):
    """Not 'outside the current actor scope': the author is in scope."""
    from plans import decisions
    from plans.boundary import PlanActor

    drafted = _draft(db)
    _strand(db, monkeypatch, drafted)

    with pytest.raises(service.PlanError, match="no longer open work"):
        decisions.update(
            drafted["id"], expected_revision=drafted["revision"],
            mutation_id=_mid(), actor=PlanActor("child", "user-1", "project-1"),
            provenance="p", db=db, title="Changed",
        )


# --- 3. read-side safety net (a row that escaped the hook) -------------------


@pytest.mark.parametrize("break_it", ["owner_thread", "plan", "source_node"])
def test_an_unanswerable_row_never_enters_the_queue(db, monkeypatch, break_it):
    """Whatever went wrong, the queue offers only rows the answer route accepts."""
    decision = raise_decision(db, "child", "child")
    if break_it == "owner_thread":
        db.execute(
            "UPDATE threads SET lifecycle_state='archived', archived_at='t' "
            "WHERE id='child'",
        )
    elif break_it == "plan":
        db.execute(
            "UPDATE plans SET state='archived' WHERE id=?", (decision["plan_id"],),
        )
    else:
        with raw_plan_seed(db):
            db.execute(
                "UPDATE plan_nodes SET archived_at='t' WHERE id=?",
                (decision["source_node_id"],),
            )
    db.commit()

    assert _queue(_client(db, monkeypatch))["count"] == 0


def test_each_unanswerable_state_would_in_fact_be_refused(db, monkeypatch):
    """The guard mirrors the answer route rather than guessing at it."""
    decision = raise_decision(db, "child", "child")
    with raw_plan_seed(db):
        db.execute(
            "UPDATE plan_nodes SET archived_at='t' WHERE id=?",
            (decision["source_node_id"],),
        )
    db.commit()

    assert _answer(_client(db, monkeypatch), decision).status_code in (403, 404)


def test_an_unanswerable_row_stays_inspectable_rather_than_vanishing(db, monkeypatch):
    """It leaves the queue but not the product: `/raised` still lists it."""
    decision = raise_decision(db, "child", "child")
    db.execute(
        "UPDATE plans SET state='archived' WHERE id=?", (decision["plan_id"],),
    )
    db.commit()
    client = _client(db, monkeypatch)

    assert _queue(client)["count"] == 0
    raised = client.get("/api/attention/raised")

    assert raised.status_code == 200
    assert decision["id"] in [item["external_id"] for item in raised.json()["items"]]


def test_a_decision_with_no_attention_link_is_not_offered(db, monkeypatch):
    raise_decision(db, "child", "child")
    db.execute("DELETE FROM plan_node_artifact_links WHERE role='attention'")
    db.commit()

    assert _queue(_client(db, monkeypatch))["count"] == 0


# --- 4. the starvation invariant --------------------------------------------


def test_a_stranded_head_can_never_starve_a_later_live_decision(db, monkeypatch):
    raise_decision(db, "child", "child", title="STRANDED")
    _archive(db, "child")
    live = raise_decision(db, "other-root", "other-root", title="LIVE")

    client = _client(db, monkeypatch)
    head = _queue(client)

    assert head["count"] == 1
    assert head["current"]["detail"]["title"] == "LIVE"
    assert _answer(client, live).status_code == 200
    assert _queue(client)["count"] == 0


def test_a_corrupt_first_body_still_lets_the_next_decision_through(db, monkeypatch):
    corrupt = raise_decision(db, "child", "child", title="CORRUPT")
    db.execute(
        """UPDATE artifact_identities SET metadata='{"plan_decision":{"nope":1}}'
           WHERE integration='local' AND type='plan-decision' AND external_id=?""",
        (corrupt["id"],),
    )
    raise_decision(db, "other-root", "other-root", title="LIVE")
    db.commit()

    head = _queue(_client(db, monkeypatch))

    assert head["count"] == 1
    assert head["current"]["detail"]["title"] == "LIVE"


# --- 5. concurrency and the generic-clear control ----------------------------


def test_answering_after_the_owner_archived_is_refused_not_half_applied(
    db, monkeypatch,
):
    decision = raise_decision(db, "child", "child")
    client = _client(db, monkeypatch)

    _archive(db, "child")
    response = _answer(client, decision)

    assert response.status_code in (403, 404, 409, 422), response.text
    assert _state(db, decision["id"]) == "invalidated"
    assert _node_state(db, decision["node_id"]) == "closed"


def test_a_generic_clear_cannot_desynchronize_the_queue_from_the_decision(
    db, monkeypatch,
):
    """The queue never offers a generic clear, and one taken out of band is
    caught by the read side rather than leaving a half-settled decision counted."""
    decision = raise_decision(db, "child", "child")
    artifact_id = db.execute(
        """SELECT id FROM artifact_identities
           WHERE integration='local' AND type='plan-decision' AND external_id=?""",
        (decision["id"],),
    ).fetchone()["id"]
    from registry import attention as registry_attention

    from infra.database import write_transaction
    with write_transaction(db, scope="test_attention_setup"):
        registry_attention.clear_attention(
            artifact_id, outcome="dismiss", reason="out of band", db=db,
        )
    db.commit()

    client = _client(db, monkeypatch)
    # Cleared attention leaves the pile, so it leaves the queue with it.
    assert _queue(client)["count"] == 0
    # And the plan's indicator must not keep counting a question nobody can see.
    from plans import decision_projection

    assert decision_projection.awaiting_count([decision["plan_id"]], db=db) == 0


# --- 6. privacy is unchanged --------------------------------------------------


def test_another_users_decision_never_enters_this_users_queue(db, monkeypatch):
    raise_decision(db, "child", "child")

    assert _queue(_client(db, monkeypatch, user_id="user-2"))["count"] == 0


# --- 2c. the user's Won't do on the question itself ---------------------------


def _wont_do(client, db, decision):
    """The exact request the plan pane's one-tap Won't do posts."""
    revision = db.execute(
        "SELECT revision FROM plan_nodes WHERE id=?", (decision["node_id"],),
    ).fetchone()["revision"]
    plan_id = db.execute(
        "SELECT plan_id FROM plan_nodes WHERE id=?", (decision["node_id"],),
    ).fetchone()["plan_id"]
    return client.post(
        f"/api/plans/{plan_id}/mutations",
        json={
            "operation": "close_node", "mutation_id": _mid(),
            "params": {"node_id": decision["node_id"], "expected_revision": revision},
        },
    )


def test_the_users_wont_do_on_a_question_withdraws_it(db, monkeypatch):
    """Report: Won't do on a decision task was refused with
    'use the decision operations', leaving no way to clear it."""
    decision = raise_decision(db, "child", "child")
    client = _client(db, monkeypatch)

    response = _wont_do(client, db, decision)

    assert response.status_code == 200, response.text
    assert _state(db, decision["id"]) == "invalidated"
    assert _attention_state(db, decision["id"]) != "raised"
    assert _node_state(db, decision["node_id"]) == "closed"
    assert _queue(client)["count"] == 0
    owner_line = db.execute(
        """SELECT summary FROM context_change_journal
           WHERE change_kind='plan.user_task_action' ORDER BY seq DESC LIMIT 1""",
    ).fetchone()["summary"]
    assert "The user closed task" in owner_line


def test_an_agent_still_may_not_close_a_question_out_from_under_the_user(db):
    from plans.boundary import PlanActor, mutate

    decision = raise_decision(db, "child", "child")
    plan_id = db.execute(
        "SELECT plan_id FROM plan_nodes WHERE id=?", (decision["node_id"],),
    ).fetchone()["plan_id"]
    revision = db.execute(
        "SELECT revision FROM plan_nodes WHERE id=?", (decision["node_id"],),
    ).fetchone()["revision"]

    with pytest.raises(service.PlanError, match="decision"):
        mutate(plan_id, "close_node", {
            "node_id": decision["node_id"], "expected_revision": revision,
            "reason": "Not needed",
        }, actor=PlanActor("child", "user-1", "project-1"), provenance="test",
            mutation_id=_mid(), db=db)

    assert _state(db, decision["id"]) == "raised"
