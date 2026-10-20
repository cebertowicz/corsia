"""Cold review-fork admission against real persisted reviewer histories."""

from __future__ import annotations

import pytest

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


def _prepare(monkeypatch, request_id: str):
    from core.artifact_review_runtime import _cold_request

    monkeypatch.setattr("llm.resolution.resolve_thread_target", lambda *_args, **_kwargs: TARGET)
    monkeypatch.setattr("core.checkpoint_render.checkpoint_binary", lambda *_args, **_kwargs: "claude")
    return _cold_request(request_id)


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

    prepared = _prepare(monkeypatch, row["id"])

    # Fallback until the reviewer has a sealed turn; debt ledger 2026-10-20.
    assert prepared is None
    await start(row["id"])
    fallback = stored(db, row["id"])
    assert fallback["node_id"] is not None
    assert fallback["fork_id"] is None


@pytest.mark.asyncio
async def test_no_turns_fall_back_to_reviewer_node(review_env, monkeypatch):
    from core.artifact_review_runtime import start

    db, artifact_id = review_env
    row = request(db, artifact_id)

    prepared = _prepare(monkeypatch, row["id"])

    # Fallback until the reviewer has a sealed turn; debt ledger 2026-10-20.
    assert prepared is None
    await start(row["id"])
    fallback = stored(db, row["id"])
    assert fallback["node_id"] is not None
    assert fallback["fork_id"] is None
