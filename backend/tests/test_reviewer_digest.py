"""DCOL-FR10.2, FR10.6: a reviewer with an open request hears what others did to the document."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from doc_feedback_helpers import add_comment, seed_document, seed_thread
from doceditor import review_requests as reviews
from infra.database import get_db
from infra.passive_changes import append_thread_context_note, materialize_pending_digest
from plans import public
from registry import models

REVIEWER = "reviewer"


@pytest.fixture
def env(isolated_db, monkeypatch):
    monkeypatch.setattr("infra.passive_changes.clock_event", lambda *_args: None)
    db = get_db()
    for tid in ("owner", "requester", REVIEWER, "bee"):
        seed_thread(db, tid, title=tid.title())
        db.execute("UPDATE threads SET agent_enabled=1,agent_responsibility='Review documents' WHERE id=?", (tid,))
    artifact_id = seed_document(db, "owner", name="PRD.md")
    yield db, artifact_id
    db.close()


def _actor():
    return reviews.ReviewActor("agent", "requester", "requester", "owner-token", "Requester")


def request_review(db, artifact_id, reviewer=REVIEWER, key="k1"):
    row = reviews.request_review(db, artifact_id, reviewer, _actor(), None, key)
    # These fixtures exercise a review node's existing watch/closure semantics.
    row = reviews.escalate_fork(db, row["id"])
    db.commit()
    return row


def edited(db, artifact_id, *, actor_thread_id=None, who="The user"):
    models.record_artifact_change(
        artifact_id=artifact_id, internal_event="content_edited", external_id=None,
        summary=f"{who} edited PRD.md.", provenance={"source": "test"},
        actor_thread_id=actor_thread_id, db=db,
    )
    db.commit()


def digest(db, thread_id) -> list[str]:
    """Summaries of the artifact and note events one digest carries, or [] when none goes out."""
    accepted = materialize_pending_digest(db, thread_id)
    db.commit()
    if accepted is None:
        return []
    row = db.execute("SELECT metadata FROM messages WHERE id=?", (accepted.message_id,)).fetchone()
    return [
        event["summary"] for event in json.loads(row["metadata"])["context_events"]
        if event.get("object_kind") in {"artifact", "thread_context"}
    ]


def settle(db, request, how):
    if how == "cancelled":
        reviews.cancel(db, request["id"], _actor())
    elif how == "closed":
        public.close_review_node(db, request["node_id"], reason="done elsewhere")
    else:
        public.settle_review_node(db, request["node_id"], evidence="delivered", provenance="document.review_request")
    db.commit()


def test_open_reviewer_hears_comments_and_edits_by_others_but_not_its_own(env):
    db, artifact_id = env
    request_review(db, artifact_id)
    digest(db, REVIEWER)

    add_comment(db, artifact_id, author_id="bee", author="Bee")
    edited(db, artifact_id)
    add_comment(db, artifact_id, author_id=REVIEWER, author="Reviewer")
    edited(db, artifact_id, actor_thread_id=REVIEWER, who="Reviewer")

    lines = digest(db, REVIEWER)
    assert len(lines) == 2
    assert any("Bee commented on PRD." in line for line in lines)
    assert any("user edited PRD." in line for line in lines)


def test_node_less_fork_watches_document_until_submission_or_withdrawal(env):
    from infra.reviewer_watch import watched_reviews

    db, artifact_id = env
    row = reviews.request_review(db, artifact_id, REVIEWER, _actor(), "Check claims", "fork-watch")
    assert row["node_id"] is None
    assert artifact_id in watched_reviews(db, REVIEWER)
    reviews.cancel(db, row["id"], _actor())
    assert artifact_id not in watched_reviews(db, REVIEWER)


def test_two_edits_by_one_actor_collapse_but_comment_and_edit_stay_apart(env):
    db, artifact_id = env
    request_review(db, artifact_id)
    digest(db, REVIEWER)

    edited(db, artifact_id)
    add_comment(db, artifact_id, author_id="bee", author="Bee")
    edited(db, artifact_id)

    assert len(digest(db, REVIEWER)) == 2


@pytest.mark.parametrize("how", ["cancelled", "closed", "delivered"])
def test_settled_request_stops_the_digest(env, how):
    db, artifact_id = env
    request = request_review(db, artifact_id)
    digest(db, REVIEWER)
    settle(db, request, how)
    digest(db, REVIEWER)

    add_comment(db, artifact_id, author_id="bee", author="Bee")

    assert digest(db, REVIEWER) == []


def test_digest_expires_seven_days_after_the_reviewers_last_activity_and_resumes_on_a_comment(env):
    db, artifact_id = env
    request = request_review(db, artifact_id)
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    db.execute("UPDATE doc_review_requests SET created_at=? WHERE id=?", (old, request["id"]))
    db.commit()
    digest(db, REVIEWER)

    add_comment(db, artifact_id, author_id="bee", author="Bee")
    assert digest(db, REVIEWER) == []

    add_comment(db, artifact_id, author_id=REVIEWER, author="Reviewer")
    add_comment(db, artifact_id, author_id="bee", author="Bee")
    lines = digest(db, REVIEWER)
    assert len(lines) == 1 and "Bee commented on PRD." in lines[0]


def test_a_reviewers_own_edit_also_restarts_the_seven_day_window(env):
    db, artifact_id = env
    request = request_review(db, artifact_id)
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    db.execute("UPDATE doc_review_requests SET created_at=? WHERE id=?", (old, request["id"]))
    db.commit()
    digest(db, REVIEWER)

    edited(db, artifact_id, actor_thread_id=REVIEWER, who="Reviewer")
    add_comment(db, artifact_id, author_id="bee", author="Bee")
    lines = digest(db, REVIEWER)
    assert len(lines) == 1 and "Bee commented on PRD." in lines[0]


def test_an_owner_who_is_not_reviewing_hears_no_content_edited_rows(env):
    db, artifact_id = env
    request_review(db, artifact_id)
    digest(db, "owner")

    edited(db, artifact_id, actor_thread_id="bee", who="Bee")

    assert digest(db, "owner") == []
    assert digest(db, REVIEWER) == ["Bee edited PRD.md."]


def test_a_change_from_before_the_request_never_reaches_the_reviewer(env):
    db, artifact_id = env
    edited(db, artifact_id)
    request_review(db, artifact_id)

    assert digest(db, REVIEWER) == []


def test_owner_with_a_review_row_gets_no_duplicates_and_the_unchanged_owner_digest(env):
    db, artifact_id = env
    request = request_review(db, artifact_id)
    db.execute("UPDATE doc_review_requests SET reviewer_thread_id='owner' WHERE id=?", (request["id"],))
    db.commit()
    digest(db, "owner")

    add_comment(db, artifact_id, author_id="bee", author="Bee")
    edited(db, artifact_id)
    lines = digest(db, "owner")

    # The owner clause collapses an artifact's rows into the newest, as it always has.
    assert len(lines) == 1


@pytest.mark.parametrize("kind", ["ownership_bypass_write", "relocated", "artifact.attention_cleared",
                                  "automation.watch_expired", "artifact.disposition"])
def test_owner_only_artifact_changes_never_reach_a_reviewer(env, kind):
    db, artifact_id = env
    request_review(db, artifact_id)
    digest(db, REVIEWER)

    models.record_artifact_change(
        artifact_id=artifact_id, internal_event=kind, external_id=None,
        summary="owner-only change", provenance={}, db=db,
    )
    db.commit()

    assert digest(db, REVIEWER) == []
    assert digest(db, "owner") == ["owner-only change"]


def test_a_retired_reviewer_receives_nothing_and_nothing_retries(env):
    db, artifact_id = env
    request_review(db, artifact_id)
    digest(db, REVIEWER)
    db.execute("UPDATE threads SET lifecycle_state='archived',archived_reason='user' WHERE id=?", (REVIEWER,))
    db.commit()

    add_comment(db, artifact_id, author_id="bee", author="Bee")

    assert digest(db, REVIEWER) == []
    assert digest(db, REVIEWER) == []


def test_a_request_whose_review_node_is_gone_is_skipped_and_the_digest_still_goes_out(env):
    db, artifact_id = env
    request_review(db, artifact_id)
    digest(db, REVIEWER)
    db.execute("UPDATE doc_review_requests SET node_id='missing-node' WHERE reviewer_thread_id=?", (REVIEWER,))
    db.commit()
    add_comment(db, artifact_id, author_id="bee", author="Bee")
    append_thread_context_note(db, thread_id=REVIEWER, summary="Robin accepted your suggestion on PRD.")
    db.commit()

    assert digest(db, REVIEWER) == ["Robin accepted your suggestion on PRD."]


def test_two_notes_for_one_thread_in_one_window_both_appear(env):
    db, _artifact_id = env
    append_thread_context_note(db, thread_id=REVIEWER, summary="Robin rejected your suggestion on PRD. Reason: A.")
    append_thread_context_note(db, thread_id=REVIEWER, summary="Robin rejected your suggestion on PRD. Reason: B.")
    db.commit()

    lines = digest(db, REVIEWER)

    assert len(lines) == 2
    assert "Reason: A." in lines[0] and "Reason: B." in lines[1]


def test_a_note_survives_more_than_a_hundred_artifact_rows(env):
    db, artifact_id = env
    request_review(db, artifact_id)
    digest(db, REVIEWER)
    append_thread_context_note(db, thread_id=REVIEWER, summary="Robin rejected your suggestion. Reason: keep.")
    for index in range(130):
        models.record_artifact_change(
            artifact_id=artifact_id, internal_event="comment.created", external_id=None,
            summary=f"Bee {index} commented on PRD.md.", provenance={},
            actor_thread_id=f"actor-{index}", db=db,
        )
    db.commit()

    lines = digest(db, REVIEWER)

    assert any("Reason: keep." in line for line in lines)
    assert len(lines) <= 101


def _history(db, count_ended, count_open, *, ended_age_days):
    """A reviewer's long review history on distinct documents; returns the open artifact ids."""
    created = (datetime.now(timezone.utc) - timedelta(days=ended_age_days)).isoformat()
    open_ids = []
    for index in range(count_ended + count_open):
        artifact_id = seed_document(db, "owner", name=f"Doc{index}.md")
        request = request_review(db, artifact_id, key=f"k{index}")
        if index < count_ended:
            public.close_review_node(db, request["node_id"], reason="ended")
            db.execute("UPDATE doc_review_requests SET created_at=? WHERE id=?", (created, request["id"]))
        else:
            open_ids.append(artifact_id)
    db.commit()
    return open_ids


def _count_openness_checks(monkeypatch):
    calls = []
    original = public.review_is_open

    def counted(conn, node_id):
        calls.append(node_id)
        return original(conn, node_id)

    monkeypatch.setattr(public, "review_is_open", counted)
    return calls


def test_old_history_costs_one_openness_check_per_survivor_of_the_window(env, monkeypatch):
    db, _artifact_id = env
    open_ids = _history(db, 50, 2, ended_age_days=30)
    calls = _count_openness_checks(monkeypatch)

    digest(db, REVIEWER)

    assert len(calls) == len(open_ids) == 2


def test_recent_ended_history_is_bounded_by_the_window_not_by_the_history(env, monkeypatch):
    db, _artifact_id = env
    _history(db, 50, 2, ended_age_days=1)
    calls = _count_openness_checks(monkeypatch)

    digest(db, REVIEWER)

    # Ended-without-comments requests still inside the window are checked once each.
    assert len(calls) == 52
