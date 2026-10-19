"""A paused plan or a paused item defers its asks' Needs-you attention until resume.

Activity's count, the operator payload, the chat list, status and the dock
queue share one rule (``operator_display_state.ask_deferred`` and its SQL twin).
The ask stays raised and answerable meanwhile, resume restores it, and an
unpaused sibling ask keeps counting. Every kind pauses alike, a decision included.
"""

from __future__ import annotations

from api import attention
from plans import (
    activity_projection,
    admission,
    boundary,
    decision_projection,
    decisions,
    operator_chat_state,
    operator_display_state,
    operator_projection,
    service,
    status,
    user_ask,
    user_steering,
)
from plans.boundary import PlanActor
from plans.pause_authority import reaches_paused_agent
from tests.plan_ask_helpers import document_ask
from tests.test_plan_decision_answer import (
    _human,
    _mid,
    _raised_decision,
    _thread_actor,
    db,  # noqa: F401 - shared fixture
)


def _lifecycle(db, plan_id: str, operation: str) -> None:
    boundary.mutate(
        plan_id, operation,
        {"expected_revision": service._plan_row(db, plan_id)["revision"]},
        actor=_human(), provenance=f"api.plans.{operation}", mutation_id=_mid(), db=db,
    )
    db.commit()


def _node_lifecycle(db, plan_id: str, node_id: str, operation: str, **params) -> None:
    revision = db.execute("SELECT revision FROM plan_nodes WHERE id=?", (node_id,)).fetchone()[0]
    boundary.mutate(
        plan_id, operation,
        {"node_id": node_id, "expected_revision": revision, **params},
        actor=_human(), provenance=f"api.plans.{operation}", mutation_id=_mid(), db=db,
    )
    db.commit()


def _pane_row(db, node_id: str) -> dict:
    pane = operator_projection.read_thread("owner", actor=_human(), db=db)["plans"][0]
    return next(node for node in pane["nodes"] if node["id"] == node_id)


def _payload_awaiting(db) -> int:
    return operator_projection.read_thread("owner", actor=_human(), db=db)["attention"][
        "awaiting_count"
    ]


def _dock(db) -> list:
    return attention._queue_for("user-1", db=db)


def _activity_awaiting(db) -> int:
    actor = PlanActor(None, "user-1", None, kind="user")
    return activity_projection.read_workspace(actor=actor, db=db)["attention"]["awaiting_count"]


def test_ask_deferred_sql_matches_projected_pause_facts(db):
    from tests.test_plan_decisions import _task

    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(plan.id, "Check parity", db=db)
    db.commit()

    def sql(plan_state: str) -> bool:
        return bool(db.execute(
            f"SELECT {operator_display_state.ask_deferred_sql('?', plan_state_sql='?')}",
            (plan_state, node.id),
        ).fetchone()[0])

    execution = admission.project(node.id, db=db)
    assert operator_display_state.ask_deferred(
        plan_paused=False, node_paused=operator_display_state.node_paused(execution),
    ) == sql("active") == False
    _node_lifecycle(db, plan.id, node.id, "pause_node")
    execution = admission.project(node.id, db=db)
    assert operator_display_state.ask_deferred(
        plan_paused=False, node_paused=operator_display_state.node_paused(execution),
    ) == sql("active") == True
    _node_lifecycle(db, plan.id, node.id, "resume_node")
    execution = admission.project(node.id, db=db)
    assert operator_display_state.ask_deferred(
        plan_paused=False, node_paused=operator_display_state.node_paused(execution),
    ) == sql("active") == False
    assert operator_display_state.ask_deferred(plan_paused=True) == sql("paused") == True


def test_pause_defers_the_decision_count_and_resume_restores_it(db):
    plan, _source, decision = _raised_decision(db)
    assert decision_projection.awaiting_count([plan.id], db=db) == 1
    assert _payload_awaiting(db) == 1
    assert _activity_awaiting(db) == 1
    assert len(_dock(db)) == 1

    _lifecycle(db, plan.id, "pause")

    assert _activity_awaiting(db) == 0

    assert decision_projection.awaiting_count([plan.id], db=db) == 0
    assert _payload_awaiting(db) == 0
    assert _dock(db) == []
    # Deferred, not removed: still raised and answerable.
    assert [item["id"] for item in decision_projection.raised_decisions([plan.id], db=db)] == [
        decision["id"]
    ]

    _lifecycle(db, plan.id, "resume")

    assert decision_projection.awaiting_count([plan.id], db=db) == 1
    assert _payload_awaiting(db) == 1
    assert len(_dock(db)) == 1


def _decision_surfaces(db, plan_id: str, decision: dict, sibling: dict) -> tuple:
    """Every Needs-you surface's reading of the two decisions."""
    pane = operator_projection.read_thread("owner", actor=_human(), db=db)["plans"][0]
    asking = {node["id"] for node in pane["nodes"] if node["requires_user_action"]}
    items = operator_chat_state.read_thread_states(["owner"], actor=_human(), db=db)["states"]["owner"][
        "attention_items"
    ]
    return (
        decision_projection.awaiting_count([plan_id], db=db),
        _payload_awaiting(db),
        _activity_awaiting(db),
        [row["detail"]["id"] for row in _dock(db)],
        asking & {decision["node_id"], sibling["node_id"]},
        [item["node_id"] for item in items if item["kind"] == "decision"],
    )


def test_pause_node_defers_a_decision_and_resume_restores_it(db):
    plan, _source, decision = _raised_decision(db)
    _same_plan, _sibling_source, sibling = _raised_decision(db, title="Keep this decision visible")
    both = (
        2, 2, 2, [decision["id"], sibling["id"]],
        {decision["node_id"], sibling["node_id"]}, [decision["node_id"], sibling["node_id"]],
    )
    assert _decision_surfaces(db, plan.id, decision, sibling) == both

    _node_lifecycle(db, plan.id, decision["node_id"], "pause_node")

    assert _decision_surfaces(db, plan.id, decision, sibling) == (
        1, 1, 1, [sibling["id"]], {sibling["node_id"]}, [sibling["node_id"]],
    )
    assert _pane_row(db, decision["node_id"])["status_reason"] == user_steering.USER_PAUSE_REASON
    assert _pane_row(db, sibling["node_id"])["status_reason"] is None
    # Deferred, not removed: still raised and answerable, and its node unmoved.
    assert decision["id"] in {
        item["id"] for item in decision_projection.raised_decisions([plan.id], db=db)
    }
    assert service._node_row(db, decision["node_id"])["state"] == "review_requested"

    _node_lifecycle(db, plan.id, decision["node_id"], "resume_node")

    assert _decision_surfaces(db, plan.id, decision, sibling) == both


def test_paused_only_decision_leaves_chat_list_and_status_unowed(db):
    plan, _source, decision = _raised_decision(db)
    _node_lifecycle(db, plan.id, decision["node_id"], "pause_node")

    state = operator_chat_state.read_thread_states(["owner"], actor=_human(), db=db)["states"]["owner"]
    assert state["attention_items"] == []
    assert state["state"] != "blocked_on_you"
    assert status.read(db, service._plan_row(db, plan.id), actor=_human())["severity"] != "needs_input"


def test_answering_a_paused_decision_delivers_it_and_settles_the_item(db):
    plan, _source, decision = _raised_decision(db)
    _node_lifecycle(db, plan.id, decision["node_id"], "pause_node")
    plan_state = service._plan_row(db, plan.id)["state"]

    answered = decisions.answer(
        decision["id"], option_id="A", expected_revision=decision["revision"],
        mutation_id=_mid(), actor=_human(), provenance="api.plans.decision", db=db,
    ).result["decision"]
    db.commit()

    assert answered["state"] == "answered"
    assert service._node_row(db, decision["node_id"])["state"] == "accepted"
    assert service._plan_row(db, plan.id)["state"] == plan_state != "paused"
    row = db.execute(
        "SELECT * FROM messages WHERE id=?", (f"plan-decision-answer:{decision['id']}",),
    ).fetchone()
    assert row["thread_id"] == "owner" and row["delivery_state"] == "queued"
    assert reaches_paused_agent(row)
    assert decision_projection.awaiting_count([plan.id], db=db) == 0
    # The answer ended the person's own pause on it; nothing is left holding.
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL",
        (decision["node_id"],),
    ).fetchone()[0] == 0


def test_pause_node_defers_a_user_assigned_step_and_keeps_its_sibling(db):
    from tests.test_plan_decisions import _task

    plan = service.ensure_plan("thread", "owner", db=db)
    paused = _task(plan.id, "Paused sign-off", assignee="user", db=db)
    sibling = _task(plan.id, "Visible sign-off", assignee="user", db=db)
    db.commit()

    def nodes():
        return operator_projection.read_thread("owner", actor=_human(), db=db)["plans"][0]["nodes"]

    assert all(node["requires_user_action"] for node in nodes())
    _node_lifecycle(db, plan.id, paused.id, "pause_node")
    assert not next(node for node in nodes() if node["id"] == paused.id)["requires_user_action"]
    assert next(node for node in nodes() if node["id"] == sibling.id)["requires_user_action"]
    assert operator_chat_state.read_thread_states(["owner"], actor=_human(), db=db)["states"]["owner"][
        "attention_items"
    ] == [{"kind": "hold", "title": "Visible sign-off", "node_id": sibling.id}]
    _node_lifecycle(db, plan.id, paused.id, "resume_node")
    assert all(node["requires_user_action"] for node in nodes())


def test_pause_node_defers_a_human_review_and_keeps_its_sibling(db):
    from tests.test_plan_decisions import _task

    plan = service.ensure_plan("thread", "owner", db=db)
    paused = _task(plan.id, "Paused review", assignee="user", db=db)
    sibling = _task(plan.id, "Visible review", assignee="user", db=db)
    db.commit()
    for node in (paused, sibling):
        _node_lifecycle(db, plan.id, node.id, "activate")
        _node_lifecycle(db, plan.id, node.id, "request_review", evidence="Ready")

    _node_lifecycle(db, plan.id, paused.id, "pause_node")
    state = operator_chat_state.read_thread_states(["owner"], actor=_human(), db=db)["states"]["owner"]
    assert state["state"] == "blocked_on_you"
    assert state["attention_items"] == [{"kind": "review", "title": "Visible review", "node_id": sibling.id}]
    _node_lifecycle(db, plan.id, paused.id, "resume_node")
    assert len(operator_chat_state.read_thread_states(["owner"], actor=_human(), db=db)["states"]["owner"][
        "attention_items"
    ]) == 2


def test_paused_only_human_review_leaves_chat_list_unowed(db):
    from tests.test_plan_decisions import _task

    plan = service.ensure_plan("thread", "owner", db=db)
    review = _task(plan.id, "Paused review", assignee="user", db=db)
    db.commit()
    _node_lifecycle(db, plan.id, review.id, "activate")
    _node_lifecycle(db, plan.id, review.id, "request_review", evidence="Ready")
    _node_lifecycle(db, plan.id, review.id, "pause_node")
    row = _pane_row(db, review.id)
    assert row["display"]["label_key"] == "waiting"
    # The row names the pause, not the review it defers.
    assert row["status_reason"] == user_steering.USER_PAUSE_REASON

    state = operator_chat_state.read_thread_states(["owner"], actor=_human(), db=db)["states"]["owner"]
    assert state["state"] == "paused"
    assert state["attention_items"] == []
    assert status.read(db, service._plan_row(db, plan.id), actor=_human())["severity"] != "needs_input"


def test_pause_node_defers_a_document_ask_and_resume_restores_it(db):
    from tests.test_plan_decisions import _task

    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(plan.id, "Review the document", db=db)
    artifact_id = document_ask(db, "owner")
    db.commit()
    boundary.mutate(
        plan.id, "block_node",
        {"node_id": node.id, "owner_kind": "user", "ask_artifact_id": artifact_id,
         "reason": "Review the document", "reason_key": "document_review",
         "expected_revision": node.revision},
        actor=_thread_actor("owner"), provenance="test", mutation_id=_mid(), db=db,
    )
    db.commit()

    assert user_ask.ask_attention_state(db, artifact_id) == "raised"
    assert len(attention._summarize_raised(None, "user-1", db=db)) == 1
    _node_lifecycle(db, plan.id, node.id, "pause_node")
    assert user_ask.ask_attention_state(db, artifact_id) == "deferred"
    assert attention._summarize_raised(None, "user-1", db=db) == []
    _node_lifecycle(db, plan.id, node.id, "resume_node")
    assert user_ask.ask_attention_state(db, artifact_id) == "raised"
    assert len(attention._summarize_raised(None, "user-1", db=db)) == 1


def test_pause_node_defers_a_raised_artifact_ask_and_resume_restores_it(db):
    from plans import artifact_links, operator_chat_state
    from registry import attention as registry_attention, models
    from tests.test_plan_decisions import _task

    plan = service.ensure_plan("thread", "cousin", db=db)
    node = _task(plan.id, "Merge the release", db=db)
    models.register_artifact(
        integration="github", type="pr", external_id="cousin#1",
        parent_thread_id="cousin", title="Merge the release", db=db,
    )
    raised = db.execute(
        "SELECT id FROM artifact_identities WHERE external_id='cousin#1'",
    ).fetchone()["id"]
    registry_attention.raise_attention(raised, db=db)
    artifact_links.link(node.id, raised, "attention", actor_id=None, provenance="test", db=db)
    db.commit()

    def items() -> list:
        states = operator_chat_state.read_thread_states(["cousin"], actor=_human(), db=db)
        return states["states"]["cousin"]["attention_items"]

    def shown() -> tuple:
        # An untyped ask is listed by /raised, the dock's generic rows.
        return (
            [item["node_id"] for item in items()],
            len(attention._summarize_raised(None, "user-1", db=db)),
        )

    assert shown() == ([node.id], 1)
    _node_lifecycle(db, plan.id, node.id, "pause_node")
    assert shown() == ([], 0)
    _node_lifecycle(db, plan.id, node.id, "resume_node")
    assert shown() == ([node.id], 1)
    # #983: a whole-plan pause defers an untyped ask too, not only decisions.
    _lifecycle(db, plan.id, "pause")
    assert shown()[1] == 0
    _lifecycle(db, plan.id, "resume")
    assert shown() == ([node.id], 1)


def _raised_pr(db, number: int, *nodes) -> str:
    from plans import artifact_links
    from registry import attention as registry_attention, models

    models.register_artifact(
        integration="github", type="pr", external_id=f"cousin#{number}",
        parent_thread_id="cousin", title="Merge the release", db=db,
    )
    raised = db.execute(
        "SELECT id FROM artifact_identities WHERE external_id=?", (f"cousin#{number}",),
    ).fetchone()["id"]
    registry_attention.raise_attention(raised, db=db)
    for node in nodes:
        artifact_links.link(node.id, raised, "attention", actor_id=None, provenance="test", db=db)
    db.commit()
    return raised


def test_an_artifact_asked_from_two_tasks_stays_raised_until_both_are_paused(db):
    from tests.test_plan_decisions import _task

    first_plan = service.ensure_plan("thread", "cousin", db=db)
    first = _task(first_plan.id, "Merge the release", db=db)
    second_plan = service.ensure_plan("thread", "owner", db=db)
    second = _task(second_plan.id, "Check the release", db=db)
    _raised_pr(db, 2, first, second)

    _node_lifecycle(db, first_plan.id, first.id, "pause_node")
    assert len(attention._summarize_raised(None, "user-1", db=db)) == 1
    _node_lifecycle(db, second_plan.id, second.id, "pause_node")
    assert attention._summarize_raised(None, "user-1", db=db) == []

def test_a_settled_link_in_a_paused_plan_never_hides_a_live_ask(db):
    from tests.test_plan_decisions import _task

    settled_plan = service.ensure_plan("thread", "cousin", db=db)
    settled = _task(settled_plan.id, "Earlier merge", db=db)
    live_plan = service.ensure_plan("thread", "owner", db=db)
    live = _task(live_plan.id, "Merge the release", db=db)
    _raised_pr(db, 3, settled, live)
    _node_lifecycle(db, settled_plan.id, settled.id, "close_node")

    _lifecycle(db, settled_plan.id, "pause")
    assert len(attention._summarize_raised(None, "user-1", db=db)) == 1
    _node_lifecycle(db, live_plan.id, live.id, "pause_node")
    assert attention._summarize_raised(None, "user-1", db=db) == []
