"""N8b list projection and mutation parity through the real HTTP adapters."""
from __future__ import annotations

import sqlite3

import pytest

from doc_feedback_helpers import seed_thread
from doceditor.review_requests import escalate_fork
from test_doc_review_adapters import client, payload  # noqa: F401
from test_doc_review_requests import request, review_env  # noqa: F401


def test_list_without_thread_names_and_permissions_never_reads_roster(client, review_env, monkeypatch):
    from doceditor import review_feedback, review_requests as reviews
    db, aid = review_env
    row = request(db, aid)
    monkeypatch.setattr(reviews, "agent_rows", lambda *args: pytest.fail("No thread must not read a roster"))
    response = client.get(f"/api/artifacts/{aid}/reviews")
    assert response.status_code == 200
    body = response.json()
    assert "reviewers" not in body and "reviewers_error" not in body
    shown = body["reviews"][0]
    assert shown["id"] == row["id"]
    assert shown["reviewer_name"] == "Reviewer"
    assert shown["requester_name"] == "Requester"
    assert shown["may_cancel"] is True


def test_user_requester_name_comes_from_the_user(client, review_env):
    db, aid = review_env
    request(db, aid, kind="user")
    body = client.get(f"/api/artifacts/{aid}/reviews").json()
    assert body["reviews"][0]["requester_name"] == "Steve"


def test_viewing_thread_is_owned_before_roster_read_and_artifact_is_checked_first(client, review_env, monkeypatch):
    from doceditor import review_feedback, review_requests as reviews
    db, aid = review_env
    seed_thread(db, "foreign")
    db.execute("UPDATE threads SET user_id='someone-else' WHERE id='foreign'")
    db.commit()
    calls = []
    original = reviews.get_thread_for_user

    def authenticate(tid, uid, *, db):
        calls.append(tid)
        return original(tid, uid, db=db)

    monkeypatch.setattr(reviews, "get_thread_for_user", authenticate)
    monkeypatch.setattr(reviews, "agent_rows", lambda *args: pytest.fail("Unauthorized roster read"))
    response = client.get(f"/api/artifacts/{aid}/reviews", params={"thread_id": "foreign"})
    assert response.status_code == 404 and "reviewers" not in response.json()
    assert calls == ["foreign"]
    calls.clear()
    db.execute("UPDATE threads SET user_id='someone-else' WHERE id='owner'")
    db.commit()
    denied = client.get(f"/api/artifacts/{aid}/reviews", params={"thread_id": "requester"})
    assert denied.status_code == 404
    assert denied.json()["detail"] == reviews.ARTIFACT_REFUSAL
    assert calls == []


def test_picker_and_post_share_one_helper_and_one_roster_read_per_list(client, review_env, monkeypatch):
    from doceditor import review_feedback, review_requests as reviews
    db, aid = review_env
    requested = request(db, aid)
    count = 0
    helper_calls = []
    original_roster = reviews.agent_rows
    original_helper = reviews.reviewer_eligibility

    def roster(*args):
        nonlocal count
        count += 1
        return original_roster(*args)

    def helper(*args, **kwargs):
        helper_calls.append(kwargs.get("reviewer_id"))
        return original_helper(*args, **kwargs)

    monkeypatch.setattr(reviews, "agent_rows", roster)
    monkeypatch.setattr(reviews, "reviewer_eligibility", helper)
    response = client.get(f"/api/artifacts/{aid}/reviews", params={"thread_id": "requester"})
    assert response.status_code == 200
    assert count == 1 and helper_calls == [None]
    choices = {row["id"]: row for row in response.json()["reviewers"]}
    assert "owner" not in choices
    assert choices["reviewer"] == {
        "id": "reviewer", "name": "Reviewer", "responsibility": "Review documents",
        "requested": True, "unavailable_reason": None, "unavailable_text": None,
    }
    assert choices["second"]["requested"] is False
    db.execute("UPDATE threads SET agent_enabled=0 WHERE id='second'")
    db.commit()
    refreshed = client.get(f"/api/artifacts/{aid}/reviews", params={"thread_id": "requester"}).json()
    assert "second" not in {row["id"] for row in refreshed["reviewers"]}
    denied = client.post("/api/doc-reviews", json=payload(aid, thread_id="requester", reviewer="second"))
    assert denied.status_code == 400
    assert helper_calls[-1] == "second"
    assert [row["id"] for row in reviews.for_document(db, aid)] == [requested["id"]]


def test_roster_error_preserves_known_requests_and_is_not_empty_or_500(client, review_env, monkeypatch):
    from doceditor import review_feedback, review_requests as reviews
    db, aid = review_env
    row = request(db, aid)

    def broken(*args):
        raise sqlite3.OperationalError("private schema detail")

    monkeypatch.setattr(reviews, "agent_rows", broken)
    response = client.get(f"/api/artifacts/{aid}/reviews", params={"thread_id": "requester"})
    assert response.status_code == 200
    body = response.json()
    assert body["reviews"][0]["id"] == row["id"]
    assert body["reviews"][0]["reviewer_name"] == "Reviewer"
    assert body["reviewers_error"] == "roster_error"
    assert "reviewers" not in body
    assert "private schema detail" not in response.text
    denied = client.post("/api/doc-reviews", json=payload(aid, thread_id="requester", reviewer="second"))
    assert denied.status_code == 503
    assert denied.json()["detail"] == reviews.ROSTER_ERROR
    assert "private schema detail" not in denied.text
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 1


@pytest.mark.parametrize("path,fields", [
    ("/api/doc-reviews", {"thread_id": "requester"}),
    ("/api/delegation/docs/reviews", {"as_session_id": "requester"}),
])
def test_both_create_routes_return_safe_roster_error(client, review_env, monkeypatch, path, fields):
    from doceditor import review_feedback, review_requests as reviews
    _db, aid = review_env

    def broken(*args):
        raise sqlite3.OperationalError("private schema detail")

    monkeypatch.setattr(reviews, "agent_rows", broken)
    response = client.post(path, json=payload(aid, **fields))
    assert response.status_code == 503
    assert response.json()["detail"] == reviews.ROSTER_ERROR
    assert "private schema detail" not in response.text


def test_user_and_agent_roster_refusals_have_distinct_stable_copy(client, review_env):
    from doceditor import review_feedback, review_requests as reviews
    db, aid = review_env
    db.execute("UPDATE threads SET agent_enabled=0 WHERE id='reviewer'")
    db.commit()
    with pytest.raises(reviews.ReviewRequestError) as caught:
        request(db, aid)
    assert caught.value.code == reviews.REVIEWER_NOT_IN_ROSTER
    user = client.post("/api/doc-reviews", json=payload(aid, thread_id="requester"))
    agent = client.post("/api/delegation/docs/reviews", json=payload(aid, as_session_id="requester"))
    assert user.status_code == agent.status_code == 400
    assert user.json()["detail"] == reviews.USER_ROSTER_REFUSAL
    assert agent.json()["detail"] == reviews.ROSTER_REFUSAL.format(id="reviewer")


def test_agent_self_request_is_refused_without_request_or_node(client, review_env):
    db, aid = review_env
    response = client.post("/api/delegation/docs/reviews", json=payload(aid, as_session_id="requester", reviewer="requester"))
    assert response.status_code == 400
    assert "yourself" in response.json()["detail"]
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0


@pytest.mark.parametrize("reason,copy", [
    ("owner_unaddressable", "Can't be reached right now"),
    ("owner_archived", "Archived"),
    ("owner_unavailable", "Not available right now"),
    ("owner_admission_refused", "Not accepting work right now"),
])
def test_reason_codes_in_picker_and_approved_post_copy(client, review_env, monkeypatch, reason, copy):
    from doceditor import feedback_delivery
    db, aid = review_env
    monkeypatch.setattr(feedback_delivery, "recipient_problem", lambda db, tid: reason if tid == "reviewer" else None)
    body = client.get(f"/api/artifacts/{aid}/reviews", params={"thread_id": "requester"}).json()
    # A reviewer who cannot take the work is not offered; submit still names why.
    assert "reviewer" not in {row["id"] for row in body["reviewers"]}
    denied = client.post("/api/doc-reviews", json=payload(aid, thread_id="requester"))
    assert denied.status_code == 400 and denied.json()["detail"] == copy
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0


def test_unknown_reason_code_keeps_raw_post_refusal_and_is_not_offered(client, review_env, monkeypatch):
    from doceditor import feedback_delivery
    db, aid = review_env
    monkeypatch.setattr(feedback_delivery, "recipient_problem", lambda db, tid: "unknown_reviewer_state" if tid == "reviewer" else None)
    body = client.get(f"/api/artifacts/{aid}/reviews", params={"thread_id": "requester"}).json()
    assert "reviewer" not in {row["id"] for row in body["reviewers"]}
    denied = client.post("/api/doc-reviews", json=payload(aid, thread_id="requester"))
    assert denied.status_code == 400
    assert denied.json()["detail"] == "unknown_reviewer_state"
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["removed", "unavailable"])
def test_post_rechecks_reviewer_after_picker_read(client, review_env, monkeypatch, change):
    from doceditor import feedback_delivery
    db, aid = review_env
    body = client.get(f"/api/artifacts/{aid}/reviews", params={"thread_id": "requester"}).json()
    assert any(row["id"] == "reviewer" and row["unavailable_reason"] is None for row in body["reviewers"])
    if change == "removed":
        db.execute("UPDATE threads SET agent_enabled=0 WHERE id='reviewer'")
        db.commit()
    else:
        monkeypatch.setattr(feedback_delivery, "recipient_problem", lambda db, tid: "owner_unaddressable")
    denied = client.post("/api/doc-reviews", json=payload(aid, thread_id="requester", note="Keep my note"))
    assert denied.status_code == 400
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0


@pytest.mark.parametrize("role,expected", [("requester", True), ("owner", True), ("neither", False)])
def test_list_cancel_permission_matches_mutation(client, review_env, monkeypatch, role, expected):
    from doceditor import review_feedback, review_requests as reviews
    db, aid = review_env
    requester_id = "owner-token" if role == "requester" else "someone-else"
    actor = reviews.ReviewActor("user", requester_id, "requester", "owner-token", "Steve")
    row = reviews.request_review(db, aid, "reviewer", actor, None, "permission-key")
    if role in {"requester", "neither"}:
        monkeypatch.setattr(reviews, "get_thread_for_user", lambda *a, **kw: None)
    body = client.get(f"/api/artifacts/{aid}/reviews").json()
    assert body["reviews"][0]["may_cancel"] is expected
    response = client.delete(f"/api/doc-reviews/{row['id']}")
    assert response.status_code == (200 if expected else 403)
    stored = db.execute("SELECT * FROM doc_review_requests WHERE id=?", (row["id"],)).fetchone()
    assert stored["state"] == ("withdrawn" if expected else "forked")
    assert "cancelled_at" not in stored.keys()
    assert reviews.outcome(db, stored) == ("cancelled" if expected else "open")


def test_projection_uses_canonical_cancel_permission_helper(client, review_env, monkeypatch):
    from doceditor import review_feedback, review_requests as reviews
    db, aid = review_env
    row = request(db, aid)
    calls = []

    def forbidden(db, artifact, row, actor):
        calls.append(row["id"])
        return False

    monkeypatch.setattr(reviews, "may_cancel", forbidden)
    body = client.get(f"/api/artifacts/{aid}/reviews").json()
    assert body["reviews"][0]["may_cancel"] is False
    assert client.delete(f"/api/doc-reviews/{row['id']}").status_code == 403
    assert calls == [row["id"], row["id"]]


@pytest.mark.parametrize("end", ["closed", "failed"])
def test_ended_review_disappears_without_requested_picker_state(client, review_env, end):
    from plans import public
    db, aid = review_env
    row = request(db, aid)
    row = escalate_fork(db, row["id"])
    public.transition_node(row["node_id"], end, db=db)
    db.commit()
    body = client.get(f"/api/artifacts/{aid}/reviews", params={"thread_id": "requester"}).json()
    assert body["reviews"] == []
    choices = {choice["id"]: choice for choice in body["reviewers"]}
    assert choices["reviewer"]["requested"] is False
    repeated = client.post("/api/doc-reviews", json=payload(aid, thread_id="requester", client_id="after-end"))
    assert repeated.status_code == 200
    assert repeated.json()["request"]["outcome"] == "open"
    import asyncio
    from core.artifact_review_runtime import start
    asyncio.run(start(repeated.json()["request"]["id"]))
    refreshed = client.get(f"/api/artifacts/{aid}/reviews").json()["reviews"]
    # This fixture has no runnable review source: the new request's preparation
    # fails visibly and preserves the work in a live reviewer task.
    assert [(r["id"], r["outcome"]) for r in refreshed] == [(repeated.json()["request"]["id"], "failed")]
    assert refreshed[0]["reason"] and refreshed[0]["node_id"]
    assert refreshed[0]["id"] != row["id"]


def test_clean_fork_without_comments_remains_named_in_http_list(client, review_env):
    from doceditor import review_feedback, review_requests as reviews
    from infra.database import write_transaction

    db, aid = review_env
    row = request(db, aid)
    with write_transaction(db, scope="test_empty_review"):
        review_feedback.submit_fork_feedback(db, row["id"], "reviewer", aid)
    body = client.get(f"/api/artifacts/{aid}/reviews", params={"thread_id": "requester"}).json()
    shown = body["reviews"][0]
    assert (shown["id"], shown["outcome"], shown["may_cancel"]) == (row["id"], "ended_without_comments", False)
    assert shown["state"] == "submitted"
    assert next(choice for choice in body["reviewers"] if choice["id"] == "reviewer")["requested"] is False


def test_ended_review_stays_absent_after_later_comments(client, review_env):
    from doc_feedback_helpers import add_comment
    from plans import public
    db, aid = review_env
    row = request(db, aid)
    row = escalate_fork(db, row["id"])
    public.transition_node(row["node_id"], "closed", db=db)
    db.commit()
    assert client.get(f"/api/artifacts/{aid}/reviews").json()["reviews"] == []
    add_comment(db, aid, author_id="reviewer", submit=True)
    assert client.get(f"/api/artifacts/{aid}/reviews").json()["reviews"] == []


def test_silent_end_disappears_through_real_plan_http_contract(client, review_env):
    from api import plans
    client.app.include_router(plans.router)
    db, aid = review_env
    row = request(db, aid)
    row = escalate_fork(db, row["id"])
    operator = client.get("/api/plans/operator/threads/reviewer")
    assert operator.status_code == 200
    plan = next(p for p in operator.json()["plans"] if any(n["id"] == row["node_id"] for n in p["nodes"]))
    node = next(n for n in plan["nodes"] if n["id"] == row["node_id"])
    closed = client.post(f"/api/plans/{plan['plan']['id']}/mutations", json={
        "operation": "close_node", "mutation_id": "silent-end-http", "params": {
            "node_id": node["id"], "expected_revision": node["revision"], "reason": "Review ended without comments",
        },
    })
    assert closed.status_code == 200, closed.text
    shown = client.get(f"/api/artifacts/{aid}/reviews").json()["reviews"]
    assert shown == []


def test_workspace_conductor_is_never_offered_but_the_project_conductor_is(client, review_env):
    """The roster lists the workspace conductor for messaging; the picker and submit both refuse it.

    A project's own conductor is an ordinary project agent and stays eligible."""
    db, aid = review_env
    db.execute("UPDATE threads SET project_id='project-a' WHERE id IN ('owner','requester','reviewer','second')")
    seed_thread(db, "workspace-conductor", title="Workspace conductor")
    seed_thread(db, "project-conductor", title="Project conductor")
    db.execute("UPDATE threads SET project_id='project-a' WHERE id='project-conductor'")
    db.execute("UPDATE threads SET agent_enabled=1, agent_responsibility='Coordinates' WHERE id IN ('workspace-conductor','project-conductor')")
    db.execute("INSERT INTO settings (key,value,updated_at) VALUES ('corsia_agent_thread_id', ?, 't')",
               ('{"session_id": "workspace-conductor"}',))
    db.commit()
    body = client.get(f"/api/artifacts/{aid}/reviews", params={"thread_id": "requester"}).json()
    ids = {row["id"] for row in body["reviewers"]}
    assert {"reviewer", "second", "project-conductor"} <= ids and "workspace-conductor" not in ids
    denied = client.post("/api/doc-reviews", json=payload(aid, thread_id="requester", reviewer="workspace-conductor"))
    assert denied.status_code == 400
    assert client.post("/api/doc-reviews", json=payload(aid, thread_id="requester", reviewer="project-conductor")).status_code == 200


def test_workspace_conductor_is_not_offered_to_a_caller_outside_any_project(client, review_env):
    db, aid = review_env
    seed_thread(db, "workspace-conductor", title="Workspace conductor")
    db.execute("UPDATE threads SET agent_enabled=1, agent_responsibility='Coordinates' WHERE id='workspace-conductor'")
    db.execute("INSERT INTO settings (key,value,updated_at) VALUES ('corsia_agent_thread_id', ?, 't')",
               ('{"session_id": "workspace-conductor"}',))
    db.commit()
    ids = {row["id"] for row in client.get(f"/api/artifacts/{aid}/reviews", params={"thread_id": "requester"}).json()["reviewers"]}
    assert "reviewer" in ids and "workspace-conductor" not in ids
