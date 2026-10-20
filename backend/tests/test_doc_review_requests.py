"""N8a: requests share the trusted, non-preemptive Plans lifecycle."""
from __future__ import annotations

import json

import pytest

from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
from doceditor import feedback_batches, feedback_delivery
from infra.database import get_db, write_transaction
from plans import public


@pytest.fixture
def review_env(isolated_db):
    db = get_db()
    for tid in ("owner", "requester", "reviewer", "second"):
        seed_thread(db, tid, title=tid.title())
        db.execute("UPDATE threads SET agent_enabled=1,agent_responsibility='Review documents' WHERE id=?", (tid,))
    aid = seed_document(db, "owner", name="PRD.md")
    yield db, aid
    db.close()


def request(db, aid, *, kind="agent", reviewer="reviewer", note="Check claims", key="review-key"):
    from doceditor import review_requests as reviews
    actor = reviews.ReviewActor(kind, "owner-token" if kind == "user" else "requester", "requester", "owner-token", "Steve" if kind == "user" else "Requester")
    return reviews.request_review(db, aid, reviewer, actor, note, key)


def stored(db, rid):
    return dict(db.execute("SELECT * FROM doc_review_requests WHERE id=?", (rid,)).fetchone())


@pytest.mark.parametrize("kind", ["user", "agent"])
def test_request_creates_linked_provenanced_queued_node_without_preemption(review_env, kind):
    db, aid = review_env
    plan = public.ensure_plan("thread", "reviewer", db=db)
    running = public.add_node(plan.id, kind="task", title="Current task", acceptance_criteria="Done", db=db)
    public.transition_node(running.id, "active", db=db)
    public.reconcile_plan(plan.id, actor_id=None, provenance="test", db=db)
    db.commit()
    result = request(db, aid, kind=kind)
    node = public.get_node(result["node_id"], db=db)
    assert node.state == "active"
    link = db.execute("SELECT * FROM plan_node_artifact_links WHERE node_id=?", (node.id,)).fetchone()
    assert (link["artifact_id"], link["role"], link["provenance"], link["source_event_id"]) == (aid, "input", "document.review_request", result["id"])
    selected = db.execute("SELECT node_id FROM plan_node_attempts WHERE released_at IS NULL AND executor_thread_id='reviewer'").fetchall()
    assert [r["node_id"] for r in selected] == [running.id]
    assert node.acceptance_criteria == "Your comments on this document reach its owner."


def test_repeat_keys_append_note_once_with_provenance_and_constant_framing(review_env, monkeypatch):
    db, aid = review_env
    monkeypatch.setattr(feedback_batches, "AGENT_QUIET", 180)
    first = request(db, aid)
    again = request(db, aid)
    assert first["id"] == again["id"]
    request(db, aid, note="Check evidence", key="second-key")
    request(db, aid, note="Check evidence", key="second-key")
    node = public.get_node(first["node_id"], db=db)
    assert node.description.count("Requester's note (context from Requester,") == 2
    assert node.description.count("never widen it):") == 2
    assert node.description.count("Check evidence") == 1
    assert "doc_comment_create" in node.description and "submit: true" in node.description and "review: true" in node.description
    assert "about three minutes" in node.description
    assert all(word not in node.description for word in ("workspace approval", "Steve approves", "train/documents"))
    assert json.loads(stored(db, first["id"])["client_ids"]) == ["review-key", "second-key"]
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 1


@pytest.mark.parametrize("target", ["owner", "unlisted", "nonagent", "cross-scope"])
def test_owner_and_nonvisible_reviewers_are_refused_without_writes(review_env, target):
    from doceditor import review_requests as reviews
    db, aid = review_env
    if target in {"nonagent", "cross-scope"}:
        seed_thread(db, target)
        if target == "cross-scope":
            db.execute("UPDATE threads SET user_id='foreign' WHERE id=?", (target,))
        db.commit()
    with pytest.raises(reviews.ReviewRequestError) as caught:
        request(db, aid, reviewer=target)
    expected = reviews.OWNER_REFUSAL if target == "owner" else reviews.ROSTER_REFUSAL.format(id=target)
    assert str(caught.value) == expected
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0


def test_unavailable_catalog_target_uses_public_addressability_reason(review_env, monkeypatch):
    from doceditor import review_requests as reviews
    db, aid = review_env
    from types import SimpleNamespace
    monkeypatch.setattr(feedback_delivery, "resolve_execution_target", lambda *a, **kw: SimpleNamespace(available=False))
    with pytest.raises(reviews.ReviewRequestError, match="owner_unaddressable") as caught:
        request(db, aid)
    assert caught.value.code == "owner_unaddressable"
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 0


def test_delivered_batch_settles_once_with_evidence_attempt_release_and_next_selection(review_env):
    from doceditor import review_requests as reviews
    db, aid = review_env
    result = request(db, aid)
    node = public.get_node(result["node_id"], db=db)
    next_node = public.add_node(node.plan_id, kind="task", title="Next task", acceptance_criteria="Done", db=db)
    public.transition_node(next_node.id, "active", db=db)
    db.commit()
    add_comment(db, aid, author_id="reviewer", submit=True)
    batch = batches(db)[0]
    assert stored(db, result["id"])["answered_batch_id"] == batch["id"]
    assert reviews.outcome(db, stored(db, result["id"])) == "commented"
    assert public.get_node(node.id, db=db).state == "accepted"
    evidence = public.latest_review_evidence(node.id, requirements_revision=node.requirements_revision, db=db)
    assert batch["id"] in evidence["evidence"]
    assert db.execute("SELECT released_at FROM plan_node_attempts WHERE node_id=?", (node.id,)).fetchone()[0]
    assert db.execute("SELECT node_id FROM plan_node_attempts WHERE released_at IS NULL AND executor_thread_id='reviewer'").fetchone()[0] == next_node.id
    with write_transaction(db, scope="test_repeat_settle"):
        reviews.settle_for_batch(db, batch)
    assert db.execute("SELECT COUNT(*) FROM plan_node_events WHERE node_id=? AND new_state='accepted'", (node.id,)).fetchone()[0] == 1


def test_undelivered_batch_stays_open_then_retry_settles(review_env):
    from doceditor import review_requests as reviews
    db, aid = review_env
    result = request(db, aid)
    db.execute("UPDATE threads SET lifecycle_state='archived',archived_reason='user' WHERE id='owner'")
    db.commit()
    add_comment(db, aid, author_id="reviewer", submit=True)
    assert batches(db)[0]["state"] == "undelivered"
    assert reviews.outcome(db, stored(db, result["id"])) == "open"
    db.execute("UPDATE threads SET lifecycle_state='active' WHERE id='owner'")
    db.commit()
    feedback_batches.retry_batch(db, batches(db)[0]["id"])
    assert reviews.outcome(db, stored(db, result["id"])) == "commented"
    assert public.get_node(result["node_id"], db=db).state == "accepted"


def test_comments_after_the_task_ended_reach_the_owner_and_count(review_env):
    from doceditor import review_requests as reviews
    db, aid = review_env
    result = request(db, aid)
    public.close_review_node(db, result["node_id"], reason="Already finished")
    revision = public.get_node(result["node_id"], db=db).revision
    add_comment(db, aid, author_id="reviewer", submit=True)
    assert batches(db)[0]["state"] == "delivered"
    late = stored(db, result["id"])
    assert late["answered_batch_id"] == batches(db)[0]["id"]
    assert reviews.outcome(db, late) == "commented"
    assert reviews.for_document(db, aid) == []
    assert public.get_node(result["node_id"], db=db).revision == revision


def test_only_the_first_late_batch_answers_a_request(review_env):
    from doceditor import review_requests as reviews
    db, aid = review_env
    first = request(db, aid)
    public.transition_node(first["node_id"], "closed", db=db)
    db.commit()
    add_comment(db, aid, author_id="reviewer", submit=True)
    answered = stored(db, first["id"])
    add_comment(db, aid, author_id="reviewer", submit=True, client_id="another-note")
    assert len(batches(db)) == 2
    assert stored(db, first["id"]) == answered
    assert answered["answered_batch_id"] == batches(db)[0]["id"]
    assert reviews.outcome(db, answered) == "commented"


def test_comments_after_a_new_request_answer_the_new_one_only(review_env):
    from doceditor import review_requests as reviews
    db, aid = review_env
    first = request(db, aid)
    public.transition_node(first["node_id"], "closed", db=db)
    db.commit()
    second = request(db, aid, key="again")
    add_comment(db, aid, author_id="reviewer", submit=True)
    assert reviews.outcome(db, stored(db, first["id"])) == reviews.ENDED_WITHOUT_COMMENTS
    assert stored(db, second["id"])["answered_batch_id"] == batches(db)[0]["id"]
    assert reviews.outcome(db, stored(db, second["id"])) == "commented"


@pytest.mark.parametrize("end", ["closed", "failed"])
def test_review_task_ending_without_a_batch_reads_without_comments(review_env, end):
    from doceditor import review_requests as reviews
    db, aid = review_env
    result = request(db, aid)
    before = stored(db, result["id"])
    public.transition_node(result["node_id"], end, db=db)
    db.commit()
    assert stored(db, result["id"]) == before
    listed = reviews.for_document(db, aid)
    assert [(r["id"], r["outcome"]) for r in listed] == [(result["id"], reviews.ENDED_WITHOUT_COMMENTS)]


def test_reviewer_marking_the_task_done_without_comments_reads_without_comments(review_env):
    from doceditor import review_requests as reviews
    db, aid = review_env
    result = request(db, aid)
    from plans import review_evidence
    with write_transaction(db, scope="test_mark_done") as connection:
        review_evidence.submit(result["node_id"], evidence="Read it; nothing to add.", expected_revision=None, actor_id="reviewer", provenance="test", db=connection)
        public.transition_node(result["node_id"], "accepted", verification_result="passed", verified_by="reviewer", db=connection)
    assert reviews.outcome(db, stored(db, result["id"])) == reviews.ENDED_WITHOUT_COMMENTS


def test_a_rejected_review_stays_open_through_its_revision(review_env):
    from doceditor import review_requests as reviews
    from plans import rejection, review_evidence
    db, aid = review_env
    result = request(db, aid)
    with write_transaction(db, scope="test_submit_review") as connection:
        review_evidence.submit(result["node_id"], evidence="Read it.", expected_revision=None, actor_id="reviewer", provenance="test", db=connection)
    submitted = public.get_node(result["node_id"], db=db).revision
    rejection.dispatch_reject(result["node_id"], {"feedback": "Comment on section 2 too", "expected_revision": submitted},
                              {"db": db, "actor_id": None, "provenance": "test"}, executor=None)
    db.commit()
    assert reviews.outcome(db, stored(db, result["id"])) == "open"
    assert request(db, aid, note="Also check section 3", key="while-revising")["id"] == result["id"]
    revision = db.execute("SELECT id FROM plan_nodes WHERE follow_up_of=?", (result["node_id"],)).fetchone()["id"]
    assert "Also check section 3" in public.get_node(revision, db=db).description
    assert "Also check section 3" not in public.get_node(result["node_id"], db=db).description
    add_comment(db, aid, author_id="reviewer", submit=True)
    assert stored(db, result["id"])["answered_batch_id"] == batches(db)[0]["id"]
    assert public.get_node(revision, db=db).state == "accepted"


def test_a_rejected_review_ends_when_its_revision_ends(review_env):
    from doceditor import review_requests as reviews
    from plans import rejection, review_evidence
    db, aid = review_env
    result = request(db, aid)
    with write_transaction(db, scope="test_submit_review") as connection:
        review_evidence.submit(result["node_id"], evidence="Read it.", expected_revision=None, actor_id="reviewer", provenance="test", db=connection)
    submitted = public.get_node(result["node_id"], db=db).revision
    rejection.dispatch_reject(result["node_id"], {"feedback": "Comment on section 2 too", "expected_revision": submitted},
                              {"db": db, "actor_id": None, "provenance": "test"}, executor=None)
    revision = db.execute("SELECT id FROM plan_nodes WHERE follow_up_of=?", (result["node_id"],)).fetchone()["id"]
    public.transition_node(revision, "closed", db=db)
    db.commit()
    assert reviews.outcome(db, stored(db, result["id"])) == reviews.ENDED_WITHOUT_COMMENTS


def _submit_and_reject(db, node_id, *, pick_up):
    """Reject to the reviewer; ``pick_up`` has the reviewer start the revision."""
    from plans import admission, rejection, review_evidence
    with write_transaction(db, scope="test_submit_review") as connection:
        review_evidence.submit(node_id, evidence="Read it.", expected_revision=None, actor_id="reviewer", provenance="test", db=connection)
    submitted = public.get_node(node_id, db=db).revision
    rejection.dispatch_reject(node_id, {"feedback": "Comment on section 2 too", "expected_revision": submitted},
                              {"db": db, "actor_id": "requester", "provenance": "test"}, executor="requester")
    revision = db.execute("SELECT id FROM plan_nodes WHERE follow_up_of=?", (node_id,)).fetchone()["id"]
    if pick_up:
        admission.activate(revision, expected_revision=public.get_node(revision, db=db).revision,
                           actor_id="reviewer", provenance="test", db=db)
    db.commit()
    return revision


def test_comments_settle_a_revision_handed_to_the_reviewer_and_release_its_slot(review_env):
    from doceditor import review_requests as reviews
    db, aid = review_env
    result = request(db, aid)
    revision = _submit_and_reject(db, result["node_id"], pick_up=True)
    assert db.execute("SELECT COUNT(*) FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL", (revision,)).fetchone()[0] == 1
    add_comment(db, aid, author_id="reviewer", submit=True)
    assert public.get_node(revision, db=db).state == "accepted"
    assert reviews.outcome(db, stored(db, result["id"])) == "commented"
    assert db.execute("SELECT COUNT(*) FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL", (revision,)).fetchone()[0] == 0


def test_a_review_rejected_twice_follows_the_chain_to_its_latest_revision(review_env):
    from doceditor import review_requests as reviews
    db, aid = review_env
    result = request(db, aid)
    first = _submit_and_reject(db, result["node_id"], pick_up=True)
    second = _submit_and_reject(db, first, pick_up=False)
    assert reviews.outcome(db, stored(db, result["id"])) == "open"
    add_comment(db, aid, author_id="reviewer", submit=True)
    assert public.get_node(second, db=db).state == "accepted"
    assert reviews.outcome(db, stored(db, result["id"])) == "commented"


def test_delivered_and_cancelled_requests_keep_their_own_outcome(review_env):
    from doceditor import review_requests as reviews
    db, aid = review_env
    delivered = request(db, aid)
    add_comment(db, aid, author_id="reviewer", submit=True)
    assert stored(db, delivered["id"])["answered_batch_id"] == batches(db)[0]["id"]
    cancelled = request(db, aid, reviewer="second", key="second-reviewer")
    actor = reviews.ReviewActor("agent", "requester", "requester", "owner-token", "Requester")
    assert reviews.cancel(db, cancelled["id"], actor)["outcome"] == "cancelled"
    add_comment(db, aid, author_id="second", submit=True)
    assert stored(db, cancelled["id"])["answered_batch_id"] is None
    assert reviews.for_document(db, aid) == []


def test_a_new_request_replaces_the_ended_one_in_the_document_list(review_env):
    from doceditor import review_requests as reviews
    db, aid = review_env
    first = request(db, aid)
    public.transition_node(first["node_id"], "closed", db=db)
    db.commit()
    again = request(db, aid, key="again")
    assert again["id"] != first["id"]
    assert [(r["id"], r["outcome"]) for r in reviews.for_document(db, aid)] == [(again["id"], "open")]


def test_nonterminal_settlement_failure_rolls_back_delivery_and_request(review_env, monkeypatch):
    db, aid = review_env
    result = request(db, aid)
    add_comment(db, aid, author_id="reviewer")
    batch_id = batches(db)[0]["id"]
    def fail(*args, **kwargs):
        raise RuntimeError("injected settlement failure")
    monkeypatch.setattr(public, "settle_review_node", fail)
    with pytest.raises(RuntimeError, match="injected settlement failure"):
        feedback_batches.close_batch(db, batch_id, "submit")
    assert batches(db)[0]["state"] == "open"
    assert stored(db, result["id"])["answered_batch_id"] is None
    assert public.get_node(result["node_id"], db=db).state == "active"
    assert db.execute("SELECT COUNT(*) FROM plan_node_artifact_links WHERE provenance='document.feedback_batch'").fetchone()[0] == 0


def test_cancel_closes_selected_node_and_terminal_cancel_is_noop(review_env):
    from doceditor import review_requests as reviews
    db, aid = review_env
    result = request(db, aid)
    actor = reviews.ReviewActor("agent", "requester", "requester", "owner-token", "Requester")
    cancelled = reviews.cancel(db, result["id"], actor)
    assert cancelled["outcome"] == "cancelled" and cancelled["cancelled_at"]
    assert public.get_node(result["node_id"], db=db).state == "closed"
    assert db.execute("SELECT released_at FROM plan_node_attempts WHERE node_id=?", (result["node_id"],)).fetchone()[0]
    assert reviews.cancel(db, result["id"], actor) == cancelled
    opened = request(db, aid, key="terminal-open")
    public.close_review_node(db, opened["node_id"], reason="Already closed")
    terminal = public.get_node(opened["node_id"], db=db)
    assert reviews.outcome(db, stored(db, opened["id"])) == reviews.ENDED_WITHOUT_COMMENTS
    ended = reviews.cancel(db, opened["id"], actor)
    assert ended == {**stored(db, opened["id"]), "outcome": reviews.ENDED_WITHOUT_COMMENTS}
    assert public.get_node(terminal.id, db=db).revision == terminal.revision


def test_two_reviewers_settle_independently_and_no_findings_comment_counts(review_env):
    from doceditor import comments
    from doceditor import review_requests as reviews
    db, aid = review_env
    first = request(db, aid)
    second = request(db, aid, reviewer="second", key="second-reviewer")
    with write_transaction(db, scope="test_no_findings"):
        row, created = comments.create_with_status(aid, body="No findings.", author="Reviewer", author_kind="agent", author_thread_id="reviewer", review=True, client_id="no-findings", db=db)
        feedback_batches.record_comment(db, row, created=created, submit=True)
    assert reviews.outcome(db, stored(db, first["id"])) == "commented"
    assert reviews.outcome(db, stored(db, second["id"])) == "open"
    add_comment(db, aid, author_id="second", submit=True)
    assert reviews.outcome(db, stored(db, second["id"])) == "commented"


def _stop_review(db, node_id, stop):
    from plans import holds, lifecycle, service
    node = public.get_node(node_id, db=db)
    if stop == "paused plan":
        lifecycle.transition(
            node.plan_id, "paused", actor_id="user:test", provenance="test",
            expected_revision=service._plan_row(db, node.plan_id)["revision"], db=db,
        )
    else:
        holds.pause(
            node_id, "Not now", expected_revision=node.revision,
            actor_id="user:test", provenance="test", db=db,
        )
    db.commit()


def _resume_review(db, node_id, stop):
    from plans import holds, lifecycle, service
    node = public.get_node(node_id, db=db)
    if stop == "paused plan":
        lifecycle.transition(
            node.plan_id, "active", actor_id="user:test", provenance="test",
            expected_revision=service._plan_row(db, node.plan_id)["revision"], db=db,
        )
    else:
        hold = db.execute(
            "SELECT id,kind FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL", (node_id,),
        ).fetchone()
        holds.clear(
            node_id, hold["id"], expected_kind=hold["kind"], expected_revision=node.revision,
            actor_id="user:test", provenance="test", db=db,
        )
    db.commit()


def _mark_done_as_reviewer(db, node_id):
    from plans.boundary import PlanActor, mutate
    node = public.get_node(node_id, db=db)
    mutate(node.plan_id, "mark_done", {
        "node_id": node_id, "expected_revision": node.revision,
        "evidence": "Comments delivered to the owner while stopped (see the note).",
    }, actor=PlanActor("reviewer", "owner-token", None), provenance="test",
        mutation_id=f"done-{node_id}", db=db)
    db.commit()


@pytest.mark.parametrize("stop", ["paused plan", "paused step"])
def test_a_stopped_reviewers_comments_reach_the_owner_and_the_review_waits(review_env, stop):
    # 2026-10-14: comments coming due for a reviewer whose plan was paused
    # raised through the delivery pass and stalled every wake source on it.
    from doceditor import review_requests as reviews
    db, aid = review_env
    result = request(db, aid)
    _stop_review(db, result["node_id"], stop)
    before = public.get_node(result["node_id"], db=db).state
    add_comment(db, aid, author_id="reviewer")

    feedback_batches.close_due("9999-01-01T00:00:00+00:00", db=db)
    db.commit()

    assert batches(db)[0]["state"] == "delivered"
    assert reviews.outcome(db, stored(db, result["id"])) == "commented"
    assert public.get_node(result["node_id"], db=db).state == before
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_events WHERE node_id=? AND new_state='accepted'",
        (result["node_id"],),
    ).fetchone()[0] == 0
    # Nothing settles it after resume, so the task must stop promising that it
    # settles by itself and tell its owner to finish it with the delivery.
    node = public.get_node(result["node_id"], db=db)
    assert f"batch {batches(db)[0]['id']}" in node.description
    assert "mark it done" in node.description

    _resume_review(db, result["node_id"], stop)
    _mark_done_as_reviewer(db, result["node_id"])
    assert public.get_node(result["node_id"], db=db).state == "accepted"


def test_one_batch_that_fails_to_close_does_not_hold_back_the_others(review_env, monkeypatch):
    db, aid = review_env
    other = seed_document(db, "owner", name="Spec.md")
    add_comment(db, aid, author_id="reviewer")
    add_comment(db, other, author_id="reviewer")
    failing = next(b["id"] for b in batches(db) if b["artifact_id"] == aid)
    real = feedback_batches.close_batch

    def close_batch(connection, batch_id, reason):
        if batch_id == failing:
            raise RuntimeError("injected close failure")
        return real(connection, batch_id, reason)

    monkeypatch.setattr(feedback_batches, "close_batch", close_batch)
    feedback_batches.close_due("9999-01-01T00:00:00+00:00", db=db)
    db.commit()

    states = {b["artifact_id"]: b["state"] for b in batches(db)}
    assert states == {aid: "open", other: "delivered"}
    monkeypatch.setattr(feedback_batches, "close_batch", real)
    feedback_batches.close_due("9999-01-01T00:00:00+00:00", db=db)
    assert {b["state"] for b in batches(db)} == {"delivered"}


def test_review_node_carries_the_requesting_nodes_canonical_intent_when_present(review_env):
    from plans import feedback_nodes, service

    db, aid = review_env
    requester = public.ensure_plan("thread", "requester", db=db)
    canonical = service.add_node(
        requester.id, kind="task", title="Canonical", intent="Preserve the outcome",
        acceptance_criteria="Outcome is preserved", db=db,
    )
    requesting = service.add_node(
        requester.id, kind="task", title="Inherited", acceptance_criteria="Review starts",
        intent_node_id=canonical.id, db=db,
    )
    without = feedback_nodes.create_review_node(
        db, "reviewer", {
            "id": "review-without-intent", "artifact_id": aid, "requester_display": "Requester", "note": "",
        },
    )
    with_request = feedback_nodes.create_review_node(
        db, "second", {
            "id": "review-with-intent", "artifact_id": aid, "requester_display": "Requester", "note": "",
            "requesting_node_id": requesting.id,
        },
    )

    assert public.get_node(without.node_id, db=db).intent_node_id is None
    assert public.get_node(with_request.node_id, db=db).intent_node_id == canonical.id
