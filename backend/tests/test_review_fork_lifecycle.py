"""Approach-reset regressions: explicit states, start admission and boot races."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import replace

import pytest

from core import artifact_review_capture as capture, artifact_review_runtime as runtime
from core.artifact_review_fork_kind import ARTIFACT_REVIEW_KIND
from core.fork_failures import ForkBinaryResolutionError, ForkCredentialsUnavailable
from core.fork_types import ProviderForkRejected
from core.fork_runtime import ForkHandle, ForkStepLimitReached, handles
from doceditor import review_feedback, review_requests
from infra.database import write_transaction
from plans import public
from prompts.artifact_review import continuation
from test_doc_review_requests import request, review_env, fork_comment, stored
from test_checkpoint_provider_adapters import _request


@pytest.mark.asyncio
@pytest.mark.parametrize("error,reason", [
    (ValueError("cold input does not fit"), "cold_request_build:ValueError"),
    (ForkBinaryResolutionError("harness binary missing"), "binary_resolution"),
    (ForkBinaryResolutionError("executable"), "binary_resolution"),
    (capture.ReviewForkFailure("no_sealed_boundary"), "no_sealed_boundary"),
    (ForkCredentialsUnavailable("missing credentials"), "missing_credentials"),
])
async def test_start_build_failure_records_reason_and_hands_back(review_env, monkeypatch, caplog, error, reason):
    db, aid = review_env
    row = request(db, aid)

    def broken(_request_id):
        raise error

    monkeypatch.setattr(runtime, "_cold_request", broken)
    with caplog.at_level(logging.ERROR):
        await runtime.start(row["id"])
    failed = stored(db, row["id"])
    assert failed["state"] == "failed" and failed["reason"] == reason
    assert review_requests.is_open(db, failed)
    assert public.get_node(failed["node_id"], db=db).description.endswith(
        "Check claims\n\n" + continuation(0, reason),
    )
    assert failed["fork_id"] is None and row["fork_id"] not in handles
    assert any(reason in record.message for record in caplog.records)
    await runtime.start(row["id"])
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error,expected_state,reason", [
    (ForkStepLimitReached("job exhausted"), "handed_back", "limit"),
    (ProviderForkRejected("provider unavailable"), "failed", "provider_error:ProviderForkRejected"),
    (ForkCredentialsUnavailable("credentials are missing"), "failed", "missing_credentials"),
])
async def test_partial_review_limit_and_failure_are_distinct_live_tasks(review_env, error, expected_state, reason):
    db, aid = review_env
    row = request(db, aid)
    fork_comment(db, aid, row["id"])
    assert db.execute("SELECT state,item_count FROM doc_feedback_batches").fetchone()[:] == ("open", 1)
    await capture.review_limit_or_failure_handler(row["id"], error)
    result = stored(db, row["id"])
    assert (result["state"], result["reason"]) == (expected_state, reason)
    assert public.review_is_open(db, result["node_id"])
    note = public.get_node(result["node_id"], db=db).description
    assert note.endswith("Check claims\n\n" + continuation(1, reason))
    assert db.execute("SELECT state,item_count FROM doc_feedback_batches").fetchone()[:] == ("open", 1)
    await capture.review_limit_or_failure_handler(row["id"], error)
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_start_checks_live_and_submitted_requests_before_build(review_env, monkeypatch):
    db, aid = review_env
    row = request(db, aid)

    def unexpected(_request_id):
        pytest.fail("idempotent start rebuilt cold history")

    monkeypatch.setattr(runtime, "_cold_request", unexpected)
    event = asyncio.Event()
    task = asyncio.create_task(event.wait())
    handles[row["fork_id"]] = ForkHandle(task, event, "reviewer", ARTIFACT_REVIEW_KIND)
    try:
        await runtime.start(row["id"])
        with write_transaction(db, scope="test_clean_review"):
            review_feedback.submit_fork_feedback(db, row["id"], "reviewer", aid)
        handles.pop(row["fork_id"])
        await runtime.start(row["id"])
        assert stored(db, row["id"])["state"] == "submitted"
    finally:
        handles.pop(row["fork_id"], None)
        event.set()
        await task


@pytest.mark.asyncio
async def test_concurrent_start_builds_once_and_withdrawal_wins(review_env, monkeypatch):
    db, aid = review_env
    row = request(db, aid)
    entered, release = threading.Event(), threading.Event()
    builds = []

    def build(request_id):
        builds.append(request_id)
        entered.set()
        assert release.wait(5)
        return row, replace(_request(None), fork_id=row["fork_id"], thread_id="reviewer")

    monkeypatch.setattr(runtime, "_cold_request", build)
    first = asyncio.create_task(runtime.start(row["id"]))
    assert await asyncio.to_thread(entered.wait, 5)
    try:
        await runtime.start(row["id"])
        actor = review_requests.ReviewActor("agent", "requester", "requester", "owner-token", "Requester")
        review_requests.cancel(db, row["id"], actor)
    finally:
        release.set()
        await first
    assert builds == [row["id"]]
    assert stored(db, row["id"])["state"] == "withdrawn"
    assert row["fork_id"] not in handles
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_startup_does_not_orphan_a_new_request_without_a_handle(review_env):
    db, aid = review_env
    previous = request(db, aid)
    process_started = "2026-10-20T22:00:00.000002+00:00"
    db.execute("UPDATE doc_review_requests SET created_at=? WHERE id=?",
               ("2026-10-20T22:00:00.000001+00:00", previous["id"]))
    db.commit()
    # This request commits after process start but before reconciliation reads.
    # Its handle need not exist yet: production start yields during the build.
    current = request(db, aid, reviewer="second", key="current")
    db.execute("UPDATE doc_review_requests SET created_at=? WHERE id=?",
               ("2026-10-20T22:00:00.000003+00:00", current["id"]))
    db.commit()
    assert current["fork_id"] not in handles
    assert await capture.reconcile_startup_reviews() == [previous["id"], current["id"]]
    for request_id in (previous["id"], current["id"]):
        recovered = stored(db, request_id)
        assert (recovered["state"], recovered["reason"]) == ("handed_back", "interrupted")
        assert recovered["node_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["http", "tool"])
async def test_creation_reports_failed_state_after_cold_build_failure(review_env, monkeypatch, entrypoint):
    import json
    from api.doc_reviews import ReviewCreate, _create
    from tools import document_reviews

    db, aid = review_env

    def broken(_request_id):
        raise capture.ReviewForkFailure("no_sealed_boundary")

    from infra import app_loop
    previous_loop = app_loop._LOOP
    app_loop.capture(asyncio.get_running_loop())
    launches, notifications = [], []
    entered = asyncio.Event()
    start, deliver = runtime.start, public.deliver_notifications

    async def tracked_start(request_id):
        launches.append(asyncio.current_task())
        entered.set()
        await start(request_id)

    def notify(messages):
        notifications.extend(messages)
        return deliver(messages)

    monkeypatch.setattr(runtime, "start", tracked_start)
    monkeypatch.setattr(public, "deliver_notifications", notify)
    monkeypatch.setattr(runtime, "_cold_request", broken)
    fields = dict(artifact_id=aid, reviewer="reviewer", note="Check claims", client_id="failure")
    try:
        if entrypoint == "http":
            actor = review_requests.ReviewActor("agent", "requester", "requester", "owner-token", "Requester")
            result = await _create(ReviewCreate(**fields), actor)
        else:
            monkeypatch.setattr(document_reviews, "_require_context", lambda _kwargs: (
                "requester", {"user_id": "owner-token", "title": "Requester"},
            ))
            result = json.loads(await document_reviews.DocReviewRequestTool().execute(**fields))
        assert result["request"]["state"] == "forked"
        assert result["request"]["outcome"] == "open"
        assert result["request"]["node_id"] is None
        await asyncio.wait_for(entered.wait(), timeout=5)
        await asyncio.gather(*launches)
        refreshed = review_requests.get_request(db, result["request"]["id"])
        assert refreshed["outcome"] == "open"
        assert refreshed["state"] == "failed"
        assert refreshed["reason"] == "no_sealed_boundary"
        assert refreshed["node_id"]
        assert sum(
            frame.get("type") == "artifacts_changed" and "requester" in frame["affected_thread_ids"]
            for _user_id, frame in notifications
        ) >= 2  # The failure invalidates the requester after the create notification.
    finally:
        monkeypatch.setattr(app_loop, "_LOOP", previous_loop)


def test_pending_reads_lifecycle_and_checks_only_continuation_tasks(review_env):
    db, aid = review_env
    row = request(db, aid)
    snapshot = {**stored(db, row["id"]), "node_id": "unrelated-node"}
    assert review_requests.is_open(db, snapshot)
    snapshot.update(state="submitted", answered_batch_id=None)
    assert not review_requests.is_open(db, snapshot)
    snapshot.update(state="failed", reason="no_sealed_boundary")
    assert not review_requests.is_open(db, snapshot)
