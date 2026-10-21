"""Regression proofs for request staging at ordinary feedback boundaries."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from doceditor import feedback_batches, review_requests
from infra.database import write_transaction
from test_doc_review_requests import request, review_env, staged_comment  # noqa: F401


def test_prepare_edit_with_active_fork_uses_one_ordinary_open_batch(review_env):
    from doceditor.feedback_edit_preparation import prepare_edit

    db, aid = review_env
    path = Path(db.execute("SELECT path FROM artifact_identities WHERE id=?", (aid,)).fetchone()[0])
    params = dict(artifact_id=aid, recipient_id="owner", author_kind="agent", author_id="reviewer",
                  actor_display="Reviewer", path=path)
    with write_transaction(db, scope="test_first_ordinary_edit"):
        batch = feedback_batches.record_edit(db, **params, before=b"old", after=b"first")
    row = request(db, aid)
    staged_comment(db, aid, row["id"])
    prepared = prepare_edit(db, **params, before=b"first", after=b"second")
    assert prepared["row"]["batch_id"] == batch
    with write_transaction(db, scope="test_second_ordinary_edit"):
        assert feedback_batches.record_edit(db, **params, before=b"first", after=b"second", prepared=prepared) == batch
    assert db.execute("SELECT COUNT(*) FROM doc_feedback_batches WHERE state='open'").fetchone()[0] == 1
    assert db.execute("SELECT save_count FROM doc_feedback_edit_items WHERE batch_id=?", (batch,)).fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM doc_review_request_comments").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_withdrawal_between_state_read_and_handle_registration_launches_no_model_step(review_env, monkeypatch):
    from core import artifact_review_runtime as runtime, fork_runtime
    from infra.db_async import run_in_db
    from tools.document_reviews import cancel_request

    db, aid = review_env
    row = await run_in_db(request, db, aid)
    cold = SimpleNamespace(fork_id=row["fork_id"], thread_id="reviewer")
    monkeypatch.setattr(runtime, "_cold_request", lambda _request_id: (row, cold))
    original = runtime.submitted
    reads = []
    model_steps = []
    source_reads = []

    async def source(prepared):
        source_reads.append(prepared)
        raise AssertionError("a withdrawn review must stop before source preparation")

    def withdraw_after_read(request_id):
        stale = original(request_id)
        if not reads:
            actor = review_requests.ReviewActor("agent", "requester", "requester", "owner-token", "Requester")
            cancel_request(actor, request_id)
            reads.append(request_id)
        return stale

    async def invoke(*args, **kwargs):
        model_steps.append(args)
        raise AssertionError("a withdrawn review must not launch a model")

    monkeypatch.setattr(runtime, "_source", source)
    monkeypatch.setattr(runtime, "submitted", withdraw_after_read)
    monkeypatch.setattr(fork_runtime, "invoke_fork", invoke)
    await runtime.start(row["id"])
    handle = fork_runtime.handles.get(row["fork_id"])
    assert handle is not None
    await asyncio.gather(handle.task, return_exceptions=True)
    assert reads == [row["id"]]
    assert model_steps == []
    assert source_reads == []
    assert db.execute("SELECT state FROM doc_review_requests WHERE id=?", (row["id"],)).fetchone()[0] == "withdrawn"
    assert handle.task.done()


@pytest.mark.parametrize("reason", ["limit", "target_unavailable"])
def test_inflight_fork_comment_after_release_is_refused(review_env, reason):
    db, aid = review_env
    row = request(db, aid)
    staged_comment(db, aid, row["id"])
    review_requests.escalate_fork(db, row["id"], reason=reason)
    with pytest.raises(ValueError, match="no longer owns"):
        staged_comment(db, aid, row["id"])
    assert db.execute("SELECT COUNT(*) FROM doc_review_request_comments").fetchone()[0] == 0
    assert db.execute("SELECT item_count FROM doc_feedback_batches").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_startup_recovery_releases_crashed_fork_staging_for_quiet_delivery(review_env):
    from core.artifact_review_capture import reconcile_startup_reviews
    from core.fork_runtime import handles
    from infra.db_async import run_in_db

    db, aid = review_env
    row = await run_in_db(request, db, aid)
    await run_in_db(staged_comment, db, aid, row["id"])
    assert row["fork_id"] not in handles  # The durable row survived, its process did not.
    assert await reconcile_startup_reviews() == [row["id"]]
    recovered = db.execute("SELECT state,reason FROM doc_review_requests WHERE id=?", (row["id"],)).fetchone()
    assert recovered[:] == ("handed_back", "interrupted")
    assert db.execute("SELECT COUNT(*) FROM doc_review_request_comments").fetchone()[0] == 0
    assert db.execute("SELECT state FROM doc_feedback_batches").fetchone()[0] == "open"
    await run_in_db(feedback_batches.close_due, "9999-01-01T00:00:00+00:00", db=db)
    batch = db.execute("SELECT state,delivered_node_id FROM doc_feedback_batches").fetchone()
    assert batch["state"] == "delivered"
    assert "The claim needs evidence." in db.execute("SELECT description FROM plan_nodes WHERE id=?", (batch["delivered_node_id"],)).fetchone()[0]


def test_withdrawal_after_handback_preserves_released_ordinary_feedback(review_env):
    db, aid = review_env
    row = request(db, aid)
    staged_comment(db, aid, row["id"])
    review_requests.escalate_fork(db, row["id"])
    assert db.execute("SELECT COUNT(*) FROM doc_review_request_comments").fetchone()[0] == 0
    batch = db.execute("SELECT id FROM doc_feedback_batches WHERE state='open'").fetchone()[0]
    actor = review_requests.ReviewActor("agent", "requester", "requester", "owner-token", "Requester")
    assert review_requests.cancel(db, row["id"], actor)["state"] == "withdrawn"
    assert db.execute("SELECT comment_id FROM doc_feedback_batch_items WHERE batch_id=?", (batch,)).fetchone()
    feedback_batches.close_due("9999-01-01T00:00:00+00:00", db=db)
    delivered = db.execute("SELECT state,delivered_node_id FROM doc_feedback_batches WHERE id=?", (batch,)).fetchone()
    assert delivered["state"] == "delivered"
    assert "The claim needs evidence." in db.execute("SELECT description FROM plan_nodes WHERE id=?", (delivered["delivered_node_id"],)).fetchone()[0]
