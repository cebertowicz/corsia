"""Regressions for one request lifecycle and cross-thread fork withdrawal."""

from __future__ import annotations

import asyncio
import time

import pytest

from core.fork_runtime import ForkHandle, handles
from core.artifact_review_fork_kind import ARTIFACT_REVIEW_KIND
from doceditor import comments, feedback_batches, review_requests
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from plans import public
from test_doc_review_requests import request, review_env, fork_comment, stored  # noqa: F401


@pytest.mark.parametrize("reason", ["limit", "target_unavailable"])
def test_ended_continuation_keeps_visible_findings_for_ordinary_delivery(review_env, reason):
    db, aid = review_env
    row = request(db, aid)
    fork_comment(db, aid, row["id"])
    visible = comments.list_for_artifact(aid, db=db)
    assert len(visible) == 1 and not visible[0]["resolved"] and not visible[0]["acked"]
    continuation = review_requests.escalate_fork(db, row["id"], reason=reason)
    with write_transaction(db, scope="test_end_review_continuation"):
        public.close_review_node(db, continuation["node_id"], reason="The task ended without submission.")
    assert not review_requests.is_open(db, stored(db, row["id"]))
    assert review_requests.for_document(db, aid) == []
    assert db.execute("SELECT state FROM doc_feedback_batches").fetchone()[0] == "open"
    feedback_batches.close_due("2099-01-01T00:00:00+00:00", db=db)
    assert db.execute("SELECT COUNT(*) FROM doc_feedback_batches").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM doc_feedback_batch_items").fetchone()[0] == 1
    assert db.execute("SELECT state FROM doc_feedback_batches").fetchone()[0] == "delivered"
    assert comments.list_for_artifact(aid, db=db) == visible
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 2
    feedback_batches.close_due("2099-01-01T00:00:00+00:00", db=db)
    assert comments.list_for_artifact(aid, db=db) == visible
    assert stored(db, row["id"])["state"] == continuation["state"]


def test_missing_continuation_node_is_closed_and_unwatched(review_env):
    from infra.reviewer_watch import watched_reviews

    from plan_writer_authorizer import raw_plan_seed

    db, aid = review_env
    row = request(db, aid)
    continuation = review_requests.escalate_fork(db, row["id"])
    with raw_plan_seed(db):
        db.execute("DELETE FROM plan_nodes WHERE id=?", (continuation["node_id"],))
    db.commit()

    assert not review_requests.is_open(db, stored(db, row["id"]))
    assert review_requests.for_document(db, aid) == []
    assert aid not in watched_reviews(db, "reviewer")


def test_comment_admission_reads_request_state_only(review_env):
    db, aid = review_env
    row = request(db, aid)
    fork_comment(db, aid, row["id"])
    assert db.execute("SELECT item_count FROM doc_feedback_batches").fetchone()[0] == 1
    with write_transaction(db, scope="test_withdrawn_review"):
        db.execute("UPDATE doc_review_requests SET state='withdrawn' WHERE id=?", (row["id"],))
    with pytest.raises(ValueError, match="no longer owns"):
        fork_comment(db, aid, row["id"])


@pytest.mark.parametrize("text", ["binary output is malformed", "credential field is not a string", "authentication example is missing"])
def test_provider_failure_words_do_not_change_typed_reason(text):
    from core.artifact_review_capture import failure_reason

    assert failure_reason(ValueError(text)) == "provider_error:ValueError"
    assert failure_reason(FileNotFoundError(text)) == "provider_error:FileNotFoundError"


def test_typed_failure_reasons_ignore_exception_wording():
    from core.artifact_review_capture import failure_reason
    from core.fork_failures import ForkBinaryResolutionError, ForkCredentialsUnavailable

    assert failure_reason(ForkBinaryResolutionError("unavailable")) == "binary_resolution"
    assert failure_reason(ForkCredentialsUnavailable("unavailable")) == "missing_credentials"


def test_native_auth_result_raises_typed_failure_at_source():
    from core.fork_native import _provider_failure
    from core.fork_failures import ForkCredentialsUnavailable
    from llm.router import LLMResponse

    with pytest.raises(ForkCredentialsUnavailable, match="unavailable"):
        _provider_failure(LLMResponse(content="unavailable", endpoint_failure="auth"), ARTIFACT_REVIEW_KIND)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["claude", "/missing-directory"])
async def test_cc_launch_types_only_missing_executable(monkeypatch, missing):
    from core import fork_cc as cc
    from core.fork_failures import ForkBinaryResolutionError

    async def spawn(*args, **kwargs):
        raise FileNotFoundError(2, "unavailable", missing)

    monkeypatch.setattr(cc.asyncio, "create_subprocess_exec", spawn)
    expected = ForkBinaryResolutionError if missing == "claude" else FileNotFoundError
    with pytest.raises(expected):
        await cc._spawn_process(["claude"], cwd="/missing-directory", env={})


@pytest.mark.asyncio
async def test_pool_thread_withdrawal_wakes_running_fork_promptly(review_env):
    db, aid = review_env
    row = request(db, aid)
    event, entered = asyncio.Event(), asyncio.Event()

    async def running():
        entered.set()
        await event.wait()

    task = asyncio.create_task(running())
    handles[row["fork_id"]] = ForkHandle(task, event, "reviewer", kind=ARTIFACT_REVIEW_KIND)
    await entered.wait()
    loop = asyncio.get_running_loop()
    original_debug = loop.get_debug()
    loop.set_debug(True)

    def withdraw():
        from tools.document_reviews import cancel_request
        actor = review_requests.ReviewActor("agent", "requester", "requester", "owner-token", "Requester")
        cancel_request(actor, row["id"])

    started = time.monotonic()
    try:
        await run_in_db(withdraw)
        await asyncio.wait_for(task, timeout=0.5)
        assert time.monotonic() - started < 0.5
        assert stored(db, row["id"])["state"] == "withdrawn"
    finally:
        loop.set_debug(original_debug)
        handles.pop(row["fork_id"], None)
        event.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("withdrawn", [False, True])
async def test_a_fork_that_never_exits_hands_back_at_its_time_bound(review_env, monkeypatch, withdrawn):
    from core import artifact_review_runtime as runtime

    db, aid = review_env
    row = request(db, aid)

    async def hung(_request_id, _request, cancel_event):
        if withdrawn:
            await run_in_db(lambda: _withdraw(row["id"]))
            cancel_event.set()
        await cancel_event.wait()
        raise asyncio.CancelledError  # what every adapter raises once its stop event is set

    monkeypatch.setattr(runtime, "REVIEW_WALL_SECONDS", 60 if withdrawn else 0.05)
    monkeypatch.setattr(runtime, "_review", hung)
    if withdrawn:
        with pytest.raises(asyncio.CancelledError):
            await runtime._run(row["id"], None, asyncio.Event())
        assert stored(db, row["id"])["state"] == "withdrawn"
        return
    await asyncio.wait_for(runtime._run(row["id"], None, asyncio.Event()), timeout=2)
    ended = stored(db, row["id"])
    assert (ended["state"], ended["reason"]) == ("handed_back", "time_limit")
    assert public.review_is_open(db, ended["node_id"])
    assert "it ran out of time" in public.get_node(ended["node_id"], db=db).description


def _withdraw(request_id):
    db = get_db()
    try:
        with write_transaction(db, scope="test_withdraw_hung_fork"):
            db.execute("UPDATE doc_review_requests SET state='withdrawn' WHERE id=?", (request_id,))
    finally:
        db.close()


@pytest.mark.parametrize("extra", [0, 1])
def test_source_budget_refuses_before_rendering_payloads(review_env, monkeypatch, extra):
    from core import artifact_review_source as source
    from core.artifact_review_capture import ReviewForkFailure

    db, _aid = review_env
    db.executemany(
        "INSERT INTO messages(id,thread_id,role,content,created_at,seq) "
        "VALUES (?,'reviewer','user','body','t0',?)",
        [(f"budget-{seq}", seq) for seq in range(1, source.REVIEW_TAIL_MAX_ROWS + extra + 1)],
    )
    db.commit()
    calls = []
    sentinel = object()

    def render(*args, **kwargs):
        calls.append(True)
        return sentinel

    monkeypatch.setattr(source, "render_portable_history", render)
    if extra:
        with pytest.raises(ReviewForkFailure, match="history_tail_bound"):
            source.bounded_review_history("reviewer", None, db=db)
        assert calls == []
    else:
        assert source.bounded_review_history("reviewer", None, db=db) is sentinel
        assert calls == [True]
