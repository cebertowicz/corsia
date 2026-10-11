"""The review bar's answer: verdict, note and round feedback reach the asking agent (DCOL-FR12.4)."""

from __future__ import annotations

import json
import uuid

import pytest

from doc_feedback_helpers import add_comment, seed_document, seed_thread
from infra.database import get_db
from doceditor import review_bar
from plans import holds, service, user_ask
from plans.boundary import PlanActor, mutate
from registry import attention, models

OWNER = PlanActor("owner", "owner-token", None)


@pytest.fixture
def db(isolated_db):
    connection = get_db()
    seed_thread(connection, "owner", title="Style")
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


def _ask(db, artifact_id: str, *, title: str = "Review the proposal") -> str:
    """An agent task blocked on the user for this artifact; returns the task id."""
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title=title, acceptance_criteria="Reviewed", db=db,
    )
    db.commit()
    mutate(
        plan.id, "block_node", {
            "node_id": node.id, "reason_key": "review", "reason": f"Please read: {title}",
            "owner_kind": "user", "owner_id": None, "ask_artifact_id": artifact_id,
            "expected_revision": service._node_row(db, node.id)["revision"],
        }, actor=OWNER, provenance="test.review_bar",
        mutation_id=uuid.uuid4().hex, db=db,
    )
    db.commit()
    return node.id


def _comment(db, artifact_id: str, **kwargs) -> None:
    add_comment(db, artifact_id, author_kind="user", author="Steve", author_id=None, **kwargs)


def _answer(db, artifact_id: str, verdict: str, note: str | None = None):
    result = attention.clear_attention(
        artifact_id, outcome="answer", verdict=verdict, note=note, db=db,
    )
    db.commit()
    return result


def _notices(db) -> list[str]:
    return [
        row["content"] for row in db.execute(
            "SELECT content FROM messages WHERE thread_id='owner' ORDER BY seq",
        ).fetchall()
    ]


def _last_ask_wake(db) -> str:
    return [notice for notice in _notices(db) if notice.startswith("[CORSIA plan] ")][-1]


def _answer_notice(node_id: str, *, ask: str, title: str, verdict: str,
                   answer: str = "none") -> str:
    return (
        f'[CORSIA plan] The user answered "{ask}" for task "{title}" (node {node_id}). '
        f'Verdict: {verdict}. User answer: {answer}. Other holds remain: no.\n'
        '{"next": null}'
    )


def _open_holds(db, node_id: str) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL",
        (node_id,),
    ).fetchone()[0]


def _clearance(db, node_id: str) -> dict:
    row = db.execute(
        """SELECT e.payload_json FROM plan_node_holds h
             JOIN plan_node_events e ON e.id=h.clearance_event_id WHERE h.node_id=?""",
        (node_id,),
    ).fetchone()
    return json.loads(row["payload_json"])["detail"]


def test_a_plain_approval_reaches_the_agent_with_its_note(db):
    artifact = seed_document(db, "owner", name="proposal.md")
    node = _ask(db, artifact)

    _answer(db, artifact, "approved", "Ship it.")

    assert _last_ask_wake(db) == _answer_notice(
        node, ask="proposal.md", title="Review the proposal", verdict="approved", answer="Ship it.",
    )
    assert _open_holds(db, node) == 0
    stored = _clearance(db, node)
    assert stored["verdict"] == "approved"
    assert stored["note"] == "Ship it."
    assert stored["feedback_counts"] == {"comments": 0, "suggestions": 0, "edits": 0}


def test_the_round_feedback_switches_approve_to_approve_with_changes(db):
    artifact = seed_document(db, "owner", name="proposal.md")
    _ask(db, artifact)
    assert review_bar.round_state(db, artifact)["round_feedback_count"] == 0

    _comment(db, artifact)
    _comment(db, artifact, replacement="better words")
    state = review_bar.round_state(db, artifact)

    # Both columns are UTC ISO-8601 written by their own domains, so a comment made after the
    # raise sorts after the hold's stamp as a string and counts.
    stamp = db.execute("SELECT MIN(created_at) FROM doc_comments WHERE artifact_id=?", (artifact,)).fetchone()[0]
    assert stamp > state["ask_raised_at"]
    assert stamp[10] == state["ask_raised_at"][10] == "T"

    assert state["round_feedback_count"] == 2
    assert state["round_feedback_counts"] == {"comments": 1, "suggestions": 1, "edits": 0}
    # The label the client last showed does not decide: the round does.
    _answer(db, artifact, "approved")
    assert "approved with changes" in _last_ask_wake(db)


def test_approve_with_changes_and_the_feedback_task_pointer(db):
    artifact = seed_document(db, "owner", name="proposal.md")
    node = _ask(db, artifact)
    _comment(db, artifact)
    _comment(db, artifact, replacement="better words")

    _answer(db, artifact, "approved_with_changes", "Mostly fine.")

    open_batches = db.execute(
        "SELECT COUNT(*) FROM doc_feedback_batches WHERE state='open'",
    ).fetchone()[0]
    assert open_batches == 0
    assert _last_ask_wake(db) == _answer_notice(
        node,
        ask="proposal.md",
        title="Review the proposal",
        verdict="approved with changes",
        answer="Mostly fine.",
    )
    assert _clearance(db, node)["feedback_counts"] == {
        "comments": 1, "suggestions": 1, "edits": 0,
    }


def test_an_approval_with_changes_but_no_feedback_reads_as_plain(db):
    artifact = seed_document(db, "owner", name="proposal.md")
    node = _ask(db, artifact)

    _answer(db, artifact, "approved_with_changes")

    assert _clearance(db, node)["verdict"] == "approved"


def test_request_changes_with_no_feedback_needs_a_note(db):
    artifact = seed_document(db, "owner", name="proposal.md")
    node = _ask(db, artifact)

    with pytest.raises(review_bar.AnswerRefused) as refused:
        attention.clear_attention(
            artifact, outcome="answer", verdict="changes_requested", db=db,
        )

    assert refused.value.code == "note_required"
    assert _open_holds(db, node) == 1
    assert attention.get_attention_state(artifact, db=db) == "raised"

    _answer(db, artifact, "changes_requested", "Cut section 3.")
    assert _last_ask_wake(db) == _answer_notice(
        node,
        ask="proposal.md",
        title="Review the proposal",
        verdict="changes requested",
        answer="Cut section 3.",
    )


def test_request_changes_with_round_feedback_needs_no_note(db):
    artifact = seed_document(db, "owner", name="proposal.md")
    node = _ask(db, artifact)
    _comment(db, artifact)

    _answer(db, artifact, "changes_requested")

    assert _last_ask_wake(db) == _answer_notice(
        node,
        ask="proposal.md",
        title="Review the proposal",
        verdict="changes requested",
    )


def test_one_answer_releases_every_ask_on_the_artifact(db):
    artifact = seed_document(db, "owner", name="proposal.md")
    first = _ask(db, artifact, title="Review the proposal")
    second = _ask(db, artifact, title="Check the numbers")

    _answer(db, artifact, "approved")

    assert _open_holds(db, first) == 0 and _open_holds(db, second) == 0
    notices = [
        text for text in _notices(db)
        if text.startswith("[CORSIA plan] The user answered") and "Verdict: approved." in text
    ]
    assert len(notices) == 2
    assert any('task "Review the proposal"' in text for text in notices)
    assert any('task "Check the numbers"' in text for text in notices)
    assert _clearance(db, first)["verdict"] == _clearance(db, second)["verdict"] == "approved"


def test_a_superseded_ask_returns_stale_and_is_never_cleared_twice(db):
    artifact = seed_document(db, "owner", name="proposal.md")
    node = _ask(db, artifact)
    attention.clear_attention(artifact, outcome="supersede", db=db)
    db.commit()
    before = len(_notices(db))

    with pytest.raises(review_bar.AnswerRefused) as refused:
        attention.clear_attention(artifact, outcome="answer", verdict="approved", db=db)

    assert refused.value.code == "ask_stale"
    assert len(_notices(db)) == before
    assert _open_holds(db, node) == 0


def test_a_retired_ask_returns_stale(db):
    artifact = seed_document(db, "owner", name="proposal.md")
    _ask(db, artifact)
    models.retire(artifact, db=db)
    db.commit()

    with pytest.raises(review_bar.AnswerRefused) as refused:
        attention.clear_attention(artifact, outcome="answer", verdict="approved", db=db)

    assert refused.value.code == "ask_stale"


@pytest.mark.parametrize("outcome", ["acknowledge", "dismiss", "supersede", "defer"])
def test_a_verdict_on_another_outcome_is_refused(db, outcome):
    artifact = seed_document(db, "owner", name="proposal.md")
    node = _ask(db, artifact)

    with pytest.raises(attention.AttentionError, match="only with the answer outcome"):
        attention.clear_attention(
            artifact, outcome=outcome, verdict="approved", db=db,
            condition={"type": "deadline", "value": "2099-01-01T00:00:00+00:00"},
        )

    assert _open_holds(db, node) == 1


def test_an_unknown_verdict_is_refused(db):
    artifact = seed_document(db, "owner", name="proposal.md")
    _ask(db, artifact)

    with pytest.raises(review_bar.AnswerRefused) as refused:
        attention.clear_attention(artifact, outcome="answer", verdict="maybe", db=db)

    assert refused.value.code == "verdict_unknown"


def test_an_image_can_be_asked_about_and_answered(db):
    image = models.register_artifact(
        integration="local", type="image", external_id="/workspace/mock.png",
        parent_thread_id="owner", title="mock.png", db=db,
    )
    db.commit()
    artifact = str(image["id"])
    node = _ask(db, artifact, title="Look at the mock")

    state = review_bar.round_state(db, artifact)
    assert state["ask_raised"] is True
    _answer(db, artifact, "changes_requested", "Darker header.")

    assert _open_holds(db, node) == 0
    assert _last_ask_wake(db) == _answer_notice(
        node,
        ask="mock.png",
        title="Look at the mock",
        verdict="changes requested",
        answer="Darker header.",
    )


def test_other_artifact_types_still_cannot_be_asked_about(db):
    thing = models.register_artifact(
        integration="local", type="spreadsheet", external_id="/workspace/a.xlsx",
        parent_thread_id="owner", title="a.xlsx", db=db,
    )
    db.commit()
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Look", acceptance_criteria="Looked", db=db,
    )
    db.commit()

    with pytest.raises(service.PlanInputError, match="document or image"):
        mutate(
            plan.id, "block_node", {
                "node_id": node.id, "reason_key": "review", "reason": "Look",
                "owner_kind": "user", "owner_id": None, "ask_artifact_id": str(thing["id"]),
                "expected_revision": service._node_row(db, node.id)["revision"],
            }, actor=OWNER, provenance="test.review_bar",
            mutation_id=uuid.uuid4().hex, db=db,
        )


def test_the_read_names_the_newest_ask_and_the_other_askers(db):
    artifact = seed_document(db, "owner", name="proposal.md")
    seed_thread(db, "research", title="Research")
    _ask(db, artifact, title="Review the proposal")
    # A second agent's ask: the artifact changed hands, so its block passes the owner check.
    plan = service.ensure_plan("thread", "research", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Verify claims", acceptance_criteria="Verified", db=db,
    )
    db.commit()
    user_ask.attach(db, node.id, artifact, actor_id="research", provenance="test.review_bar")
    holds.place_hold(
        node.id, kind="blocked", reason_key="review", reason="Check my sources too",
        owner_kind="user", owner_id=artifact,
        expected_revision=service._node_row(db, node.id)["revision"],
        actor_id="research", provenance="test.review_bar", db=db,
    )
    db.commit()

    state = review_bar.round_state(db, artifact)

    assert state["ask_reason"] == "Check my sources too"
    assert state["asker"]["name"] == "Research"
    assert state["other_askers"] == ["Style"]
    assert state["other_asks"] == [{"name": "Style", "reason": "Please read: Review the proposal"}]
    assert state["other_request_count"] == 1


def test_an_edit_sent_before_the_answer_still_counts_in_the_round(db):
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    from doceditor import feedback_batches
    from doceditor.feedback_state import USER_AUTHOR_ID

    artifact = seed_document(db, "owner", name="edited.md")
    _ask(db, artifact)
    later = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    batch = feedback_batches.record_edit(
        db, artifact_id=artifact, recipient_id="owner", author_kind="user",
        author_id=USER_AUTHOR_ID, actor_display="Steve", path=Path("/workspace/shared/edited.md"),
        before=b"before\n", after=b"after\n", at=later,
    )
    db.commit()
    assert review_bar.round_state(db, artifact)["round_feedback_counts"]["edits"] == 1
    delivered, _ = feedback_batches.close_batch(db, batch, "send")
    db.commit()
    assert delivered
    assert db.execute("SELECT COUNT(*) FROM doc_feedback_edit_items").fetchone()[0] == 0
    state = review_bar.round_state(db, artifact)
    assert state["round_feedback_counts"] == {"comments": 0, "suggestions": 0, "edits": 1}
    result = _answer(db, artifact, "approved")
    assert result.review["verdict"] == "approved_with_changes"
    assert result.review["feedback_counts"]["edits"] == 1


@pytest.mark.parametrize("verdict,with_comment,note,feedback", [
    ("approved", False, None, None),
    ("approved", True, None, {"comments": 1}),
    ("changes_requested", False, "Cut section 3.", None),
])
def test_the_chat_card_reads_the_verdict_the_review_bar_recorded(db, verdict, with_comment, note, feedback):
    from registry.chat_cards import ask_cards

    artifact = seed_document(db, "owner", name="proposal.md")
    node = _ask(db, artifact)
    hold_id = db.execute("SELECT id FROM plan_node_holds WHERE node_id=?", (node,)).fetchone()["id"]
    if with_comment:
        _comment(db, artifact)

    recorded = _answer(db, artifact, verdict, note).review["verdict"]

    card = ask_cards(db, {hold_id: artifact})[hold_id]
    assert (card["state"], card.get("feedback")) == (recorded, feedback)
