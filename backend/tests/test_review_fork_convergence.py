"""Regressions for one request lifecycle and cross-thread fork withdrawal."""

from __future__ import annotations

import asyncio
import time

import pytest

from core.fork_runtime import ForkHandle, handles
from core.artifact_review_fork_kind import ARTIFACT_REVIEW_KIND
from doceditor import comments, feedback_batches, review_requests
from doceditor.feedback_batch_identity import open_batch
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from plans import public
from test_doc_review_requests import request, review_env, staged_comment, stored  # noqa: F401


@pytest.mark.parametrize("reason", ["limit", "target_unavailable"])
@pytest.mark.parametrize("batch_state", ["open", "undelivered"])
def test_ended_continuation_preserves_visible_findings_without_delivery(review_env, reason, batch_state):
    db, aid = review_env
    row = request(db, aid)
    staged_comment(db, aid, row["id"])
    visible = comments.list_for_artifact(aid, db=db)
    assert len(visible) == 1 and not visible[0]["resolved"] and not visible[0]["acked"]
    with write_transaction(db, scope="test_ended_review_staging"):
        batch = db.execute("SELECT id FROM doc_feedback_batches").fetchone()[0]
        db.execute("UPDATE doc_feedback_batches SET state=? WHERE id=?", (batch_state, batch))
        db.execute(
            "INSERT INTO doc_feedback_edit_items(batch_id,actor_display,baseline_sha256,baseline_text,"
            "latest_sha256,latest_text,save_count,first_at,last_at) VALUES (?,?,?,?,?,?,1,?,?)",
            (batch, "Reviewer", "before", "before", "after", "after", "t0", "t0"),
        )
    continuation = review_requests.escalate_fork(db, row["id"], reason=reason)
    with write_transaction(db, scope="test_end_review_continuation"):
        public.close_review_node(db, continuation["node_id"], reason="The task ended without submission.")
    assert review_requests.outcome(db, stored(db, row["id"])) == "ended_without_comments"
    assert review_requests.for_document(db, aid) == []
    assert feedback_batches.close_due("2099-01-01T00:00:00+00:00", db=db) == []
    assert db.execute("SELECT COUNT(*) FROM doc_feedback_batches").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM doc_feedback_batch_items").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM doc_feedback_edit_items").fetchone()[0] == 0
    assert comments.list_for_artifact(aid, db=db) == visible
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 1
    # Repeated cleanup preserves the visible findings and never submits feedback.
    feedback_batches.close_due("2099-01-01T00:00:00+00:00", db=db)
    assert comments.list_for_artifact(aid, db=db) == visible
    assert stored(db, row["id"])["state"] == continuation["state"]


def test_batch_admission_reads_request_state_only(review_env):
    db, aid = review_env
    row = request(db, aid)
    with write_transaction(db, scope="test_legacy_review_metadata"):
        assert "cancelled_at" not in {column[1] for column in db.execute("PRAGMA table_info(doc_review_requests)")}
        batch = open_batch(
            db, author_kind="agent", author_id="reviewer", artifact_id=aid,
            recipient_id="owner", now=row["created_at"], closes_at=row["created_at"],
            review_request_id=row["id"],
        )
    assert batch
    with write_transaction(db, scope="test_withdrawn_review"):
        db.execute("UPDATE doc_review_requests SET state='withdrawn' WHERE id=?", (row["id"],))
        with pytest.raises(ValueError, match="no longer owns"):
            open_batch(
                db, author_kind="agent", author_id="reviewer", artifact_id=aid,
                recipient_id="owner", now=row["created_at"], closes_at=row["created_at"],
                review_request_id=row["id"],
            )


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
        connection = get_db()
        try:
            actor = review_requests.ReviewActor("agent", "requester", "requester", "owner-token", "Requester")
            review_requests.cancel(connection, row["id"], actor)
        finally:
            connection.close()

    started = time.monotonic()
    try:
        await run_in_db(withdraw)
        await asyncio.wait_for(task, timeout=0.5)
        assert time.monotonic() - started < 0.5
        assert stored(db, row["id"])["state"] == "withdrawn"
        assert "cancelled_at" not in stored(db, row["id"])
    finally:
        loop.set_debug(original_debug)
        handles.pop(row["fork_id"], None)
        event.set()
        await task


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
