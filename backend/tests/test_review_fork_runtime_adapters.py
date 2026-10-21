"""Runtime-boundary regressions for review fork launch, recovery, and cancellation."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from core import artifact_review_capture as capture
from core.artifact_review_fork_kind import ARTIFACT_REVIEW_KIND
from core.fork_runtime import ForkHandle, handles
from doceditor import review_requests
from infra.db_async import run_in_db
from test_doc_review_requests import request, review_env, fork_comment, stored
from tools import document_reviews


def _actor() -> review_requests.ReviewActor:
    return review_requests.ReviewActor(
        "agent", "requester", "requester", "owner-token", "Requester",
    )


@pytest.mark.asyncio
async def test_cancelled_create_caller_starts_committed_fork(review_env, monkeypatch):
    """The DB worker submits the fork after commit before rethrowing caller cancellation."""
    from core import artifact_review_runtime as runtime
    from infra import app_loop

    db, artifact_id = review_env
    entered, release = threading.Event(), threading.Event()
    running = asyncio.Event()
    previous_loop = app_loop._LOOP
    app_loop.capture(asyncio.get_running_loop())
    launch = document_reviews._start_after_commit

    def launch_and_hold(request_id):
        launch(request_id)
        entered.set()
        assert release.wait(5)

    def prepare(request_id):
        from infra.database import get_db
        connection = get_db()
        try:
            row = stored(connection, request_id)
            return row, SimpleNamespace(fork_id=row["fork_id"], thread_id="reviewer")
        finally:
            connection.close()

    async def run(request_id, prepared, cancel_event):
        running.set()
        await cancel_event.wait()

    monkeypatch.setattr(document_reviews, "_start_after_commit", launch_and_hold)
    monkeypatch.setattr(runtime, "_cold_request", prepare)
    monkeypatch.setattr(runtime, "_run", run)
    task = asyncio.create_task(run_in_db(
        document_reviews.create_request,
        _actor(),
        reviewer="reviewer",
        note="Check claims",
        client_id="cancelled-create",
        artifact_id=artifact_id,
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(running.wait(), timeout=5)
        row = db.execute("SELECT * FROM doc_review_requests").fetchone()
        handle = handles[row["fork_id"]]
        assert row["state"] == "forked" and not handle.task.done()
    finally:
        release.set()
        for fork_id, handle in tuple(handles.items()):
            handle.cancel_event.set()
            await handle.task
            handles.pop(fork_id, None)
        monkeypatch.setattr(app_loop, "_LOOP", previous_loop)


@pytest.mark.asyncio
async def test_startup_hands_back_every_forked_request_as_interrupted(review_env):
    """No process-local handle survives startup, regardless of request age."""
    db, artifact_id = review_env
    first = request(db, artifact_id)
    second = request(db, artifact_id, reviewer="second", key="second-fork")

    assert await capture.reconcile_startup_reviews() == [first["id"], second["id"]]
    for request_id in (first["id"], second["id"]):
        result = stored(db, request_id)
        assert (result["state"], result["reason"]) == ("handed_back", "interrupted")


@pytest.mark.asyncio
async def test_db_thread_cancel_signals_running_fork_after_commit(review_env):
    """Cancellation commits first, then the pool-thread callback wakes the owner loop."""
    db, artifact_id = review_env
    row = request(db, artifact_id)
    fork_comment(db, artifact_id, row["id"])
    cancel_event = asyncio.Event()
    task = asyncio.create_task(cancel_event.wait())
    handles[row["fork_id"]] = ForkHandle(task, cancel_event, "reviewer", ARTIFACT_REVIEW_KIND)
    try:
        result, woken = await run_in_db(document_reviews.cancel_request, _actor(), row["id"])
        await asyncio.wait_for(cancel_event.wait(), timeout=1)
        assert result["state"] == "withdrawn"
        assert stored(db, row["id"])["state"] == "withdrawn"
        assert not woken
        assert db.execute("SELECT state,item_count FROM doc_feedback_batches").fetchone()[:] == ("open", 1)
        assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM doc_comments").fetchone()[0] == 1
    finally:
        handles.pop(row["fork_id"], None)
        cancel_event.set()
        await task
