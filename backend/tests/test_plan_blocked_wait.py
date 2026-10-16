"""A blocker anyone but the user clears is a wait that ends at a deadline.

- ``block_node`` for a thread, the system or an outside party arms a deadline
  watch, 24 hours out by default, and places no hold.
- Its condition may only be a deadline, and no later than 7 days out.
- The reply tells the agent what its blocker became and the ways out.
- The row leads with the deadline, then the reason.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import uuid

import pytest

from infra.schema import SCHEMA_SQL
from plans import admission, blocked_wait, service
from plans.boundary import PlanActor, mutate

OWNER = PlanActor("owner", "user", "project")


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','Project','t','t')"
    )
    connection.execute(
        """INSERT INTO threads (id,user_id,project_id,model,created_at,updated_at)
           VALUES ('owner','user','project','test/model','t','t')"""
    )
    connection.commit()
    yield connection
    connection.close()


def _blocked(db, **extra) -> tuple[str, dict]:
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Ship", acceptance_criteria="Shipped", db=db,
    )
    db.commit()
    params = {
        "node_id": node.id, "reason_key": "awaiting_ci", "reason": "CI disk is full",
        "owner_kind": "external", "owner_id": None,
        "expected_revision": service._node_row(db, node.id)["revision"], **extra,
    }
    result = mutate(
        plan.id, "block_node", params, actor=OWNER, provenance="test.blocked_wait",
        mutation_id=uuid.uuid4().hex, db=db,
    ).result
    db.commit()
    return node.id, result


def _armed(db, node_id: str) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT id, condition_type, condition_value FROM expected_event_watches
           WHERE plan_node_id=? AND status='armed'""",
        (node_id,),
    ).fetchall()


def _open_holds(db, node_id: str) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL",
        (node_id,),
    ).fetchone()[0]


def _ask_release_wakes(db) -> list[sqlite3.Row]:
    """Return the durable wake records emitted when an artifact ask clears."""
    return db.execute(
        "SELECT id,content FROM messages WHERE thread_id='owner' "
        "AND id LIKE 'plan-wake:ask_released:%' ORDER BY input_order",
    ).fetchall()


def _ask_receipts(db) -> list[sqlite3.Row]:
    """Return the durable plan-owner receipts for cleared artifact asks."""
    return db.execute(
        "SELECT id,content,metadata FROM messages WHERE thread_id='owner' "
        "AND id LIKE 'plan-event:%' ORDER BY input_order",
    ).fetchall()


def _plan_wakes_after(db, input_order: int, thread: str = "owner") -> list[sqlite3.Row]:
    """Return the plan wakes a thread's FIFO took after ``input_order``."""
    return db.execute(
        "SELECT id,content FROM messages WHERE thread_id=? AND input_order > ? "
        "AND id LIKE 'plan-wake:%' ORDER BY input_order",
        (thread, input_order),
    ).fetchall()


def _last_input_order(db, thread: str = "owner") -> int:
    return db.execute(
        "SELECT COALESCE(MAX(input_order), 0) FROM messages WHERE thread_id=?", (thread,),
    ).fetchone()[0]


def _ask_receipt_content(node, *, event: str, verdict: str | None = None,
                         answer: str | None = None, other_holds: str = "no") -> str:
    return (
        f'{event} for task "{node.title}" (node {node.id}).'
        + (f" Verdict: {verdict}." if verdict else "")
        + (f" User answer: {answer}." if answer else "")
        + f" Other holds remain: {other_holds}."
    )


def _ask_wake_next(db, wake, node, *, executor: str = "owner", **receipt) -> dict:
    """Check an ask wake's line and return its next block, the executor's real current one."""
    from plans import close_next

    line, block = wake["content"].split("\n", 1)
    assert line == "[CORSIA plan] " + _ask_receipt_content(node, **receipt)
    shown = json.loads(block)["next"]
    plan_id = service._node_row(db, node.id)["plan_id"]
    assert shown is not None
    assert shown == close_next.executor_next(db, plan_id, executor)
    return shown


def _document_ask(db, external_id: str) -> str:
    from registry import models

    models.register_artifact(
        integration="workspace", type="document", external_id=external_id,
        parent_thread_id="owner", title="Read the proposal", db=db,
    )
    return str(db.execute(
        "SELECT id FROM artifact_identities WHERE integration='workspace' "
        "AND type='document' AND external_id=?", (external_id,),
    ).fetchone()["id"])


def _ask_held_node(db, *, state: str, external_id: str, before_block=None,
                   description: str | None = None, author: str | None = None):
    """Create an ask-held task in one state whose release needs an owner wake."""
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Review", acceptance_criteria="Reviewed",
        description=description, actor_id=author, db=db,
    )
    if state == "active":
        admission.activate(
            node.id, expected_revision=service._node_row(db, node.id)["revision"],
            actor_id="owner", provenance="test.blocked_wait", db=db,
        )
    elif state == "review_requested":
        admission.activate(
            node.id, expected_revision=service._node_row(db, node.id)["revision"],
            actor_id="owner", provenance="test.blocked_wait", db=db,
        )
        mutate(
            plan.id, "request_review", {
                "node_id": node.id, "evidence": "Focused proof passed",
                "expected_revision": service._node_row(db, node.id)["revision"],
            }, actor=OWNER, provenance="test.blocked_wait", mutation_id=uuid.uuid4().hex, db=db,
        )
    else:
        assert state == "pending"
    ask = _document_ask(db, external_id)
    db.commit()
    if before_block is not None:
        before_block(node, plan)
    mutate(
        plan.id, "block_node", {
            "node_id": node.id, "reason_key": "review", "reason": "Review it",
            "owner_kind": "user", "owner_id": None, "ask_artifact_id": ask,
            "expected_revision": service._node_row(db, node.id)["revision"],
        }, actor=OWNER, provenance="test.blocked_wait", mutation_id=uuid.uuid4().hex, db=db,
    )
    return node, ask


def test_an_external_blocker_is_a_24_hour_wait_and_no_hold(db):
    before = datetime.now(timezone.utc)

    node_id, result = _blocked(db)

    [watch] = _armed(db, node_id)
    due = datetime.fromisoformat(watch["condition_value"])
    assert watch["condition_type"] == "deadline"
    assert before + timedelta(hours=24) <= due <= datetime.now(timezone.utc) + timedelta(hours=24)
    assert _open_holds(db, node_id) == 0
    projected = admission.project(node_id, db=db)
    assert projected["condition"] == "waiting"
    until = due.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    assert [cause["reason"] for cause in projected["causes"]] == [
        f"Until {until} · CI disk is full",
    ]
    # The reply names what happened, when the wait ends, and every way out.
    assert result["wait"] == blocked_wait.explain(
        type("Watch", (), {"id": watch["id"], "condition": {"value": watch["condition_value"]}}),
    )
    for way_out in ("resolve the task", "block_node again", "raise a decision", "close it", "resolve_watch"):
        assert way_out in result["wait"]


def test_a_named_deadline_within_7_days_is_kept(db):
    due = (datetime.now(timezone.utc) + timedelta(days=6)).replace(microsecond=0).isoformat()

    node_id, _result = _blocked(db, condition={"type": "deadline", "value": due})

    [watch] = _armed(db, node_id)
    assert datetime.fromisoformat(watch["condition_value"]) == datetime.fromisoformat(due)


def test_a_deadline_past_7_days_is_refused(db):
    due = (datetime.now(timezone.utc) + timedelta(days=7, minutes=5)).isoformat()

    with pytest.raises(service.PlanInputError, match="within 7 days"):
        _blocked(db, condition={"type": "deadline", "value": due})


@pytest.mark.parametrize("condition", [
    {"type": "event", "value": "ci.complete"},
    {"type": "milestone", "value": "release"},
])
def test_a_wait_that_is_not_a_deadline_is_refused(db, condition):
    with pytest.raises(service.PlanInputError, match="must be a deadline"):
        _blocked(db, condition=condition)


def test_an_agent_cannot_block_on_the_user_without_an_actionable_ask(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Sign", acceptance_criteria="Signed", db=db,
    )
    db.commit()

    with pytest.raises(service.PlanInputError, match="ask_artifact_id"):
        mutate(
            plan.id, "block_node", {
                "node_id": node.id, "reason_key": "sign_off", "reason": "The user signs",
                "owner_kind": "user", "owner_id": None,
                "expected_revision": service._node_row(db, node.id)["revision"],
            }, actor=OWNER, provenance="test.blocked_wait",
            mutation_id=uuid.uuid4().hex, db=db,
        )

    assert _open_holds(db, node.id) == 0


def test_an_agent_user_ask_must_be_an_active_document(db):
    from registry import models

    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Review", acceptance_criteria="Reviewed", db=db,
    )
    models.register_artifact(
        integration="workspace", type="code", external_id="proposal.py",
        parent_thread_id="owner", title="Not a review document", db=db,
    )
    ask = str(db.execute(
        "SELECT id FROM artifact_identities WHERE integration='workspace' "
        "AND type='code' AND external_id='proposal.py'",
    ).fetchone()["id"])
    db.commit()

    with pytest.raises(service.PlanInputError, match="active registered document"):
        mutate(
            plan.id, "block_node", {
                "node_id": node.id, "reason_key": "review", "reason": "Review it",
                "owner_kind": "user", "owner_id": None, "ask_artifact_id": ask,
                "expected_revision": service._node_row(db, node.id)["revision"],
            }, actor=OWNER, provenance="test.blocked_wait",
            mutation_id=uuid.uuid4().hex, db=db,
        )

    assert _open_holds(db, node.id) == 0


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"condition": {"type": "deadline", "value": "2099-01-01T00:00:00+00:00"}}, "condition"),
        ({"owner_id": "someone-else"}, "owner_id"),
    ],
)
def test_an_agent_user_ask_refuses_fields_the_ask_owns(db, extra, message):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Review", acceptance_criteria="Reviewed", db=db,
    )
    ask = _document_ask(db, "proposal.md")
    db.commit()

    with pytest.raises(service.PlanInputError, match=message):
        mutate(
            plan.id, "block_node", {
                "node_id": node.id, "reason_key": "review", "reason": "Review it",
                "owner_kind": "user", "ask_artifact_id": ask,
                "expected_revision": service._node_row(db, node.id)["revision"],
                **extra,
            }, actor=OWNER, provenance="test.blocked_wait",
            mutation_id=uuid.uuid4().hex, db=db,
        )

    assert _open_holds(db, node.id) == 0


def test_a_document_ask_holds_the_task_until_the_user_acts(db):
    from plans import operator_projection, user_ask
    from registry import attention

    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Review", acceptance_criteria="Reviewed", db=db,
    )
    ask = _document_ask(db, "proposal.md")
    db.commit()

    mutate(
        plan.id, "block_node", {
            "node_id": node.id, "reason_key": "review", "reason": "Review the proposal",
            "owner_kind": "user", "owner_id": None, "ask_artifact_id": ask,
            "expected_revision": service._node_row(db, node.id)["revision"],
        }, actor=OWNER, provenance="test.blocked_wait",
        mutation_id=uuid.uuid4().hex, db=db,
    )

    hold = db.execute(
        "SELECT * FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL", (node.id,),
    ).fetchone()
    assert hold["owner_id"] == ask
    assert db.execute(
        "SELECT role FROM plan_node_artifact_links WHERE node_id=? AND artifact_id=?",
        (node.id, ask),
    ).fetchone()["role"] == "attention"
    assert attention.get_attention_state(ask, db=db) is None  # The open hold is the ask's sole raised state.
    assert admission.project(node.id, db=db)["condition"] == "blocked"
    shown = operator_projection.read_thread("owner", actor=USER, db=db)["plans"][0]["nodes"][0]
    assert shown["artifacts"][0]["attention_state"] == "raised"
    assert shown["requires_user_action"] is True

    attention.clear_attention(ask, outcome="acknowledge", db=db)
    assert user_ask.cleared(ask, "acknowledge", db=db) == 0  # idempotent public hook
    assert _open_holds(db, node.id) == 0
    assert admission.project(node.id, db=db)["condition"] != "blocked"
    shown = operator_projection.read_thread("owner", actor=USER, db=db)["plans"][0]["nodes"][0]
    assert shown["requires_user_action"] is False
    assert shown["display"]["label_key"] == "waiting"
    assert shown["status_reason"] == "Not started"
    assert shown["artifacts"][0]["attention_state"] == "cleared"


def test_answering_a_pending_document_ask_wakes_the_owner_once(db):
    from registry import attention

    node, ask = _ask_held_node(db, state="pending", external_id="pending-wake.md")

    attention.clear_attention(ask, outcome="acknowledge", db=db)

    [wake] = _ask_release_wakes(db)
    assert wake["id"].startswith(f"plan-wake:ask_released:{node.id}:")
    _ask_wake_next(db, wake, node, event='The user acknowledged "Read the proposal"')
    [receipt] = _ask_receipts(db)
    assert receipt["content"] == _ask_receipt_content(
        node, event='The user acknowledged "Read the proposal"',
    )
    assert json.loads(receipt["metadata"])["ask_wake_id"] == wake["id"]


def test_answering_a_review_requested_document_ask_wakes_the_owner_once(db):
    from registry import attention

    node, ask = _ask_held_node(
        db, state="review_requested", external_id="review-requested-wake.md",
    )

    attention.clear_attention(ask, outcome="acknowledge", db=db)

    [wake] = _ask_release_wakes(db)
    assert wake["id"].startswith(f"plan-wake:ask_released:{node.id}:")
    _ask_wake_next(db, wake, node, event='The user acknowledged "Read the proposal"')


@pytest.mark.parametrize(("outcome", "ended", "event"), [
    ("reject_with_reason", "rejected", 'The user rejected "Read the proposal"'),
    ("request_revision", "revision_requested", 'The user asked for a revision of "Read the proposal"'),
    ("answer_internally", "answered_internally", 'The user answered "Read the proposal"'),
    ("supersede", "replaced", 'The user replaced "Read the proposal"'),
])
def test_each_waking_ask_ending_keeps_its_stored_value_and_renders_a_verb(
    db, outcome, ended, event,
):
    from plans import user_ask

    node, ask = _ask_held_node(db, state="pending", external_id=f"{outcome}.md")

    assert user_ask.cleared(
        ask, outcome, db=db, release=user_ask.AskRelease({"note": "The reason"}),
    ) == 1

    [wake] = _ask_release_wakes(db)
    _ask_wake_next(db, wake, node, event=event, answer="The reason")
    detail = db.execute(
        "SELECT payload_json FROM plan_node_events WHERE id=(SELECT clearance_event_id "
        "FROM plan_node_holds WHERE node_id=?)", (node.id,),
    ).fetchone()["payload_json"]
    assert json.loads(detail)["detail"]["ended"] == ended


def test_answering_an_ask_that_still_has_another_hold_reports_that_hold(db):
    from plans import holds
    from registry import attention

    node, ask = _ask_held_node(db, state="pending", external_id="second-hold-wake.md")
    holds.place_hold(
        node.id, kind="blocked", reason_key="external", reason="Waiting on CI",
        owner_kind="external", owner_id=None,
        expected_revision=service._node_row(db, node.id)["revision"],
        actor_id="owner", provenance="test.blocked_wait", db=db,
    )

    attention.clear_attention(ask, outcome="acknowledge", db=db)

    [wake] = _ask_release_wakes(db)
    shown = _ask_wake_next(
        db, wake, node, event='The user acknowledged "Read the proposal"', other_holds="yes",
    )
    # Still held, so the answer cannot have given the task the slot.
    assert (shown["selected"] or {}).get("id") != node.id
    assert "pick it up" not in wake["content"]


def test_answering_an_ask_for_a_model_less_owner_keeps_only_the_receipt(db):
    from registry import attention

    node, ask = _ask_held_node(db, state="pending", external_id="model-less-wake.md")
    db.execute("UPDATE threads SET model='' WHERE id='owner'")

    attention.clear_attention(ask, outcome="acknowledge", db=db)

    assert _ask_release_wakes(db) == []
    [receipt] = _ask_receipts(db)
    assert receipt["content"] == _ask_receipt_content(
        node, event='The user acknowledged "Read the proposal"',
    )
    assert json.loads(receipt["metadata"])["ask_wake_id"] is None


def test_answering_an_active_document_ask_is_one_wake_naming_the_selected_task(db):
    from registry import attention

    node, ask = _ask_held_node(db, state="active", external_id="active-wake.md")
    before = _last_input_order(db)

    attention.clear_attention(
        ask, outcome="answer", verdict="changes_requested", note="Cut section 3.", db=db,
    )

    [wake] = _plan_wakes_after(db, before)
    assert wake["id"].startswith(f"plan-wake:ask_released:{node.id}:")
    shown = _ask_wake_next(
        db, wake, node,
        event='The user answered "Read the proposal"',
        verdict="changes requested",
        answer="Cut section 3.",
    )
    assert shown["selected"]["id"] == node.id
    assert "selected_description" not in json.loads(wake["content"].split("\n", 1)[1])


@pytest.mark.parametrize("author, carried", [("conductor", True), ("owner", False)])
def test_the_ask_wake_carries_the_description_the_work_selected_wake_would(
    db, author, carried,
):
    """Standing in for the work-selected wake, the ask wake hands over the same facts."""
    from registry import attention

    items = "Comment c-1\nFeedback: Please check this point."
    node, ask = _ask_held_node(
        db, state="active", external_id=f"described-{author}.md",
        description=items, author=author,
    )
    before = _last_input_order(db)

    attention.clear_attention(ask, outcome="answer", verdict="approved", db=db)

    [wake] = _plan_wakes_after(db, before)
    shown = _ask_wake_next(
        db, wake, node, event='The user answered "Read the proposal"', verdict="approved",
    )
    assert shown["selected"]["id"] == node.id
    facts = json.loads(wake["content"].split("\n", 1)[1])
    expected = {"node_id": node.id, "text": items, "cut": False}
    assert facts.get("selected_description") == (expected if carried else None)


def test_answering_an_ask_on_a_scope_plan_wakes_the_task_executor(db):
    from plans import holds, user_ask
    from registry import attention

    db.execute(
        """INSERT INTO threads (id,user_id,project_id,model,created_at,updated_at)
           VALUES ('runner','user','project','test/model','t','t')"""
    )
    plan = service.ensure_plan("project_scope", "project", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Review", acceptance_criteria="Reviewed", db=db,
    )
    db.execute(
        "UPDATE plan_nodes SET executor_thread_id='runner',owner_thread_id='runner' WHERE id=?",
        (node.id,),
    )
    ask = _document_ask(db, "scope-wake.md")
    # The public boundary refuses scope-plan writes, so place the ask the way
    # block_node does underneath it.
    holds.block_hold(
        node.id, reason_key="review", reason="Review it", owner_kind="user", owner_id=ask,
        expected_revision=service._node_row(db, node.id)["revision"],
        actor_id=None, provenance="test.blocked_wait", db=db,
    )
    user_ask.attach(db, node.id, ask, actor_id=None, provenance="test.blocked_wait")
    db.commit()
    before = _last_input_order(db, "runner")

    attention.clear_attention(ask, outcome="answer", verdict="approved", db=db)

    [wake] = _plan_wakes_after(db, before, "runner")
    assert wake["id"].startswith(f"plan-wake:ask_released:{node.id}:")
    _ask_wake_next(
        db, wake, node, executor="runner",
        event='The user answered "Read the proposal"', verdict="approved",
    )


def test_selected_delegated_ask_release_wakes_when_worker_news_is_queued(db):
    from plans import delegation, plan_wake, wake_currency
    from registry import attention

    db.execute(
        """INSERT INTO threads
           (id,user_id,project_id,spawned_by,model,created_at,updated_at)
           VALUES ('worker','user','project','owner','test/model','t','t')"""
    )

    def queue_worker_news(node, plan) -> None:
        delegation.attach_spawned_thread(node.id, "worker", db=db)
        assert plan_wake.append(
            db, recipient="owner", kind=plan_wake.CHILD_RETURNED,
            subject_id=node.id, revision=int(service._node_row(db, node.id)["revision"]),
            plan_id=plan.id, content="Worker returned.", delegated_step_id=node.id,
            delegated_worker="worker",
        ) is not None
        assert wake_currency.step_news_queued(db, "owner", node.id)

    node, ask = _ask_held_node(
        db, state="active", external_id="delegated-selected-wake.md",
        before_block=queue_worker_news,
    )

    attention.clear_attention(
        ask, outcome="answer", verdict="changes_requested", note="Cut section 3.", db=db,
    )

    [wake] = _ask_release_wakes(db)
    assert wake["id"].startswith(f"plan-wake:ask_released:{node.id}:")
    _ask_wake_next(
        db, wake, node,
        event='The user answered "Read the proposal"',
        verdict="changes requested",
        answer="Cut section 3.",
    )


def test_retiring_a_document_ask_releases_its_hold_through_attention(db):
    from registry import attention, models

    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Review", acceptance_criteria="Reviewed", db=db,
    )
    ask = _document_ask(db, "retired.md")
    db.commit()
    mutate(
        plan.id, "block_node", {
            "node_id": node.id, "reason_key": "review", "reason": "Review it",
            "owner_kind": "user", "owner_id": None, "ask_artifact_id": ask,
            "expected_revision": service._node_row(db, node.id)["revision"],
        }, actor=OWNER, provenance="test.blocked_wait",
        mutation_id=uuid.uuid4().hex, db=db,
    )

    models.retire(ask, db=db)

    assert attention.get_attention_state(ask, db=db) == "cleared"
    assert _open_holds(db, node.id) == 0
    assert admission.project(node.id, db=db)["condition"] != "blocked"
    [wake] = _ask_release_wakes(db)
    assert wake["id"].startswith(f"plan-wake:ask_released:{node.id}:")
    assert "became unavailable" in wake["content"]
    event = db.execute(
        "SELECT content FROM messages WHERE thread_id='owner' ORDER BY seq DESC LIMIT 1",
    ).fetchone()
    assert "became unavailable" in event["content"]
    assert "The user" not in event["content"]
    closeout = db.execute(
        "SELECT details_json FROM context_change_journal "
        "WHERE change_kind='artifact.attention_cleared' ORDER BY seq DESC LIMIT 1",
    ).fetchone()
    assert json.loads(closeout["details_json"])["provenance"]["outcome"] == "retire"


def test_a_user_cannot_spoof_artifact_retirement_with_free_text(db):
    from registry import attention

    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Review", acceptance_criteria="Reviewed", db=db,
    )
    ask = _document_ask(db, "user-dismissed.md")
    db.commit()
    mutate(
        plan.id, "block_node", {
            "node_id": node.id, "reason_key": "review", "reason": "Review it",
            "owner_kind": "user", "owner_id": None, "ask_artifact_id": ask,
            "expected_revision": service._node_row(db, node.id)["revision"],
        }, actor=OWNER, provenance="test.blocked_wait",
        mutation_id=uuid.uuid4().hex, db=db,
    )

    attention.clear_attention(
        ask, outcome="dismiss", reason="artifact retired", db=db,
    )

    event = db.execute(
        "SELECT content FROM messages WHERE thread_id='owner' ORDER BY seq DESC LIMIT 1",
    ).fetchone()
    assert 'The user dismissed "Read the proposal"' in event["content"]
    assert "became unavailable" not in event["content"]


def test_deferring_a_document_ask_keeps_its_hold(db):
    from registry import attention

    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Review", acceptance_criteria="Reviewed", db=db,
    )
    ask = _document_ask(db, "deferred.md")
    db.commit()
    mutate(
        plan.id, "block_node", {
            "node_id": node.id, "reason_key": "review", "reason": "Review it",
            "owner_kind": "user", "owner_id": None, "ask_artifact_id": ask,
            "expected_revision": service._node_row(db, node.id)["revision"],
        }, actor=OWNER, provenance="test.blocked_wait",
        mutation_id=uuid.uuid4().hex, db=db,
    )

    attention.clear_attention(
        ask, outcome="defer",
        condition={"type": "deadline", "value": "2026-10-01T16:00:00+00:00"}, db=db,
    )

    assert attention.get_attention_state(ask, db=db) == "deferred"
    assert _open_holds(db, node.id) == 1
    assert admission.project(node.id, db=db)["condition"] == "blocked"


def test_clearing_an_old_ask_does_not_clear_a_later_hold(db):
    from plans import user_ask

    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Review", acceptance_criteria="Reviewed", db=db,
    )
    first = _document_ask(db, "first.md")
    second = _document_ask(db, "second.md")
    db.commit()

    def block(ask: str) -> None:
        mutate(
            plan.id, "block_node", {
                "node_id": node.id, "reason_key": "review", "reason": "Review it",
                "owner_kind": "user", "owner_id": None, "ask_artifact_id": ask,
                "expected_revision": service._node_row(db, node.id)["revision"],
            }, actor=OWNER, provenance="test.blocked_wait",
            mutation_id=uuid.uuid4().hex, db=db,
        )

    block(first)
    assert user_ask.cleared(first, "answer", db=db) == 1
    block(second)

    assert user_ask.cleared(first, "answer", db=db) == 0
    active = db.execute(
        "SELECT owner_id FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL",
        (node.id,),
    ).fetchone()
    assert active["owner_id"] == second


USER = PlanActor(None, "user", "project", kind="user")


def test_the_user_can_place_their_own_hold_without_an_ask(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Sign", acceptance_criteria="Signed", db=db,
    )
    db.commit()

    mutate(
        plan.id, "block_node", {
            "node_id": node.id, "reason_key": "sign_off", "reason": "I will sign",
            "owner_kind": "user", "owner_id": None,
            "expected_revision": service._node_row(db, node.id)["revision"],
        }, actor=USER, provenance="test.blocked_wait",
        mutation_id=uuid.uuid4().hex, db=db,
    )

    assert _open_holds(db, node.id) == 1
    assert _armed(db, node.id) == []


def _block_as(db, plan_id: str, node_id: str, actor: PlanActor):
    return mutate(
        plan_id, "block_node", {
            "node_id": node_id, "reason_key": "awaiting_ci", "reason": "CI disk is full",
            "owner_kind": "external", "owner_id": None,
            "expected_revision": service._node_row(db, node_id)["revision"],
        }, actor=actor, provenance="test.blocked_wait",
        mutation_id=uuid.uuid4().hex, db=db,
    )


def test_only_the_plans_own_agent_arms_a_blocker_wait(db):
    """The user's own call is refused in blocker words, not watch words."""
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Ship", acceptance_criteria="Shipped", db=db,
    )
    db.commit()

    with pytest.raises(PermissionError) as refusal:
        _block_as(db, plan.id, node.id, USER)

    assert str(refusal.value) == blocked_wait.OWNER_ONLY_REFUSAL
    assert _armed(db, node.id) == [] and _open_holds(db, node.id) == 0


def _as_owner(db, plan_id: str, operation: str, params: dict):
    result = mutate(
        plan_id, operation, params, actor=OWNER, provenance="test.blocked_wait",
        mutation_id=uuid.uuid4().hex, db=db,
    ).result
    db.commit()
    return result


def test_the_owner_clears_a_wait_the_schema_step_armed_then_accepts(db):
    """#2969: a wait armed with no arming actor is still its owner's to clear.

    The schema step that moved old blockers armed their waits as nobody, and
    resolve_watch refused the owner for not being that nobody, so finished
    work could not be accepted until the wait ran out.
    """
    from plans import holds

    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Ship", acceptance_criteria="Shipped", db=db,
    )
    db.commit()
    _as_owner(db, plan.id, "start", {
        "node_id": node.id, "expected_revision": service._node_row(db, node.id)["revision"],
    })
    holds.place_hold(
        node.id, kind="blocked", reason_key="legacy", reason="CI disk is full",
        owner_kind="external", owner_id=None,
        expected_revision=service._node_row(db, node.id)["revision"],
        actor_id="owner", provenance="tool.plans", db=db,
    )
    hold = db.execute(
        "SELECT * FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL", (node.id,),
    ).fetchone()
    deadline = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    blocked_wait.convert_hold(db, hold, deadline=deadline, provenance="migration.57_to_58")
    db.commit()
    [watch_id] = [row[0] for row in db.execute(
        "SELECT id FROM expected_event_watches WHERE plan_node_id=? AND status='armed'"
        " AND armed_by_actor_id IS NULL", (node.id,),
    )]

    _as_owner(db, plan.id, "resolve_watch", {
        "node_id": node.id, "watch_id": watch_id,
        "expected_revision": service._node_row(db, node.id)["revision"],
    })
    _as_owner(db, plan.id, "accept", {
        "node_id": node.id, "evidence": "Shipped", "verification_result": "passed",
        "expected_revision": service._node_row(db, node.id)["revision"],
    })

    assert service._node_row(db, node.id)["state"] == "accepted"


def test_the_owner_clears_a_scheduled_wait_the_58_to_59_step_armed_then_accepts(db):
    """#3032: the pause-to-schedule schema step arms waits as nobody too.

    Same shape as #2969, through ``scheduled_wait.schedule_task``: the owner
    releases the wait and then accepts the finished work.
    """
    from plans import scheduled_wait

    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Ship", acceptance_criteria="Shipped", db=db,
    )
    db.commit()
    scheduled_wait.schedule_task(
        db, node.id, reason="Moved from a pause",
        deadline=(datetime.now(timezone.utc) + timedelta(days=6)).isoformat(),
        provenance="migration.58_to_59",
    )
    db.commit()
    [watch_id] = [row[0] for row in db.execute(
        "SELECT id FROM expected_event_watches WHERE plan_node_id=? AND status='armed'"
        " AND armed_by_actor_id IS NULL", (node.id,),
    )]
    _as_owner(db, plan.id, "start", {
        "node_id": node.id, "expected_revision": service._node_row(db, node.id)["revision"],
    })
    _as_owner(db, plan.id, "resolve_watch", {
        "node_id": node.id, "watch_id": watch_id,
        "expected_revision": service._node_row(db, node.id)["revision"],
    })
    _as_owner(db, plan.id, "accept", {
        "node_id": node.id, "evidence": "Shipped", "verification_result": "passed",
        "expected_revision": service._node_row(db, node.id)["revision"],
    })
    assert service._node_row(db, node.id)["state"] == "accepted"
