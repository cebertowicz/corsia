"""Cold review-fork admission against real persisted reviewer histories."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from core.artifact_review_capture import ReviewForkFailure
from infra.queries import insert_message
from llm.resolution import ExecutionTarget
from test_doc_review_requests import request, review_env, stored


TARGET = ExecutionTarget(
    catalog_id="claude-code/fixture-cc",
    harness="claude-code",
    provider="claude-code",
    backend="cc",
    transport_model="fixture-cc",
    expected_reported_model="fixture-cc",
    context_window=262_144,
    available=True,
    availability_reason=None,
    display_name="Fixture CC",
    supports_reasoning=True,
)


def _turn(db, turn_id: str, status: str, *, messages: bool) -> None:
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,ended_at,boot_epoch)
           VALUES (?, 'reviewer', 0, 'claude-code/fixture-cc', ?, ?, ?, 'boot')""",
        (turn_id, status, f"{turn_id}-start", f"{turn_id}-end" if status == "completed" else None),
    )
    if messages:
        insert_message(
            "reviewer", "user", f"{turn_id} user", {"turn_id": turn_id},
            msg_id=f"{turn_id}-user", db=db,
        )
        insert_message(
            "reviewer", "assistant", f"{turn_id} assistant", {"turn_id": turn_id},
            msg_id=f"{turn_id}-assistant", db=db,
        )
    db.commit()


def _prepare(monkeypatch, request_id: str, target: ExecutionTarget = TARGET):
    from core.artifact_review_runtime import _cold_request

    monkeypatch.setattr("llm.resolution.resolve_thread_target", lambda *_args, **_kwargs: target)
    monkeypatch.setattr("core.fork_render.fork_binary", lambda *_args, **_kwargs: "claude")
    return _cold_request(request_id)


@pytest.mark.asyncio
async def test_target_unavailable_records_failed_request_reason(review_env, monkeypatch, caplog):
    from core.artifact_review_runtime import start

    db, artifact_id = review_env
    row = request(db, artifact_id)
    with pytest.raises(ReviewForkFailure, match="target_unavailable"):
        _prepare(monkeypatch, row["id"], replace(
            TARGET, available=False, availability_reason="fixture unavailable",
        ))
    await start(row["id"])
    fallback = stored(db, row["id"])
    assert fallback["state"] == "failed" and fallback["reason"] == "target_unavailable"
    assert f"artifact review ended request_id={row['id']} reason=target_unavailable" in caplog.text


@pytest.mark.asyncio
async def test_run_exception_reaches_escalation_handler_log(review_env, monkeypatch, caplog):
    from core.artifact_review_runtime import _run

    db, artifact_id = review_env
    row = request(db, artifact_id)
    _turn(db, "completed", "completed", messages=True)
    prepared = _prepare(monkeypatch, row["id"])
    assert prepared is not None

    async def source(request):
        return request

    async def fail_bind(*_args, **_kwargs):
        raise RuntimeError("fixture bind failure")

    monkeypatch.setattr("core.artifact_review_runtime._source", source)
    from core.artifact_review_fork_kind import ARTIFACT_REVIEW_KIND

    monkeypatch.setattr("core.artifact_review_runtime.ARTIFACT_REVIEW_KIND",
                        replace(ARTIFACT_REVIEW_KIND, usage_binder=fail_bind))

    await _run(row["id"], prepared[1], asyncio.Event())

    assert f"artifact review ended request_id={row['id']} reason=provider_error:RuntimeError" in caplog.text
    fallback = stored(db, row["id"])
    assert fallback["node_id"] is not None
    assert fallback["fork_id"] is None
    assert fallback["state"] == "failed" and fallback["reason"] == "provider_error:RuntimeError"


@pytest.mark.asyncio
async def test_unsubmitted_completed_review_logs_its_escalation_reason(review_env, caplog):
    from core.artifact_review_capture import review_end_handler

    db, artifact_id = review_env
    row = request(db, artifact_id)

    await review_end_handler(row["id"])

    assert (
        f"artifact review ended request_id={row['id']} "
        "reason=review_completed_without_submission"
    ) in caplog.text


def test_completed_history_with_running_successor_prepares_request(review_env, monkeypatch):
    db, artifact_id = review_env
    row = request(db, artifact_id)
    _turn(db, "completed", "completed", messages=True)
    _turn(db, "running", "running", messages=True)

    prepared = _prepare(monkeypatch, row["id"])

    assert prepared is not None


@pytest.mark.asyncio
async def test_only_running_first_turn_falls_back_to_reviewer_node(review_env, monkeypatch):
    from core.artifact_review_runtime import start

    db, artifact_id = review_env
    row = request(db, artifact_id)
    _turn(db, "running", "running", messages=True)

    with pytest.raises(ReviewForkFailure, match="no_sealed_boundary"):
        _prepare(monkeypatch, row["id"])
    await start(row["id"])
    fallback = stored(db, row["id"])
    assert fallback["node_id"] is not None
    assert fallback["fork_id"] is None
    assert fallback["state"] == "failed" and fallback["reason"] == "no_sealed_boundary"


@pytest.mark.asyncio
async def test_no_turns_fall_back_to_reviewer_node(review_env, monkeypatch):
    from core.artifact_review_runtime import start

    db, artifact_id = review_env
    row = request(db, artifact_id)

    with pytest.raises(ReviewForkFailure, match="no_sealed_boundary"):
        _prepare(monkeypatch, row["id"])
    await start(row["id"])
    fallback = stored(db, row["id"])
    assert fallback["node_id"] is not None
    assert fallback["fork_id"] is None
    assert fallback["state"] == "failed" and fallback["reason"] == "no_sealed_boundary"
