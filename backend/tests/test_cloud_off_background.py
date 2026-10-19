"""Background work never sends a cloud-off project's history to the thread's own cloud model."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import checkpoint_runtime
from core.checkpoint_types import (
    CLOUD_OFF_CHECKPOINT_MESSAGE,
    CheckpointCloudOff,
    CheckpointRequest,
    IdleCheckpointDisposition,
    IdleCheckpointReason,
    ProviderCheckpointRequest,
)
from core.checkpoint_warm_request import CheckpointAdmission
from infra.database import get_db
from llm.resolution import ExecutionTarget

SONNET = "claude-code/sonnet"
OPUS = "claude-code/opus"
OFF, ON = "project-off", "project-on"


@pytest.fixture
def world(isolated_db, tmp_path):
    """Two projects, one with cloud models off, and a launch seam that must stay unused."""
    from llm.locality import set_project_cloud_models_off

    db = get_db()
    try:
        for project in (OFF, ON):
            db.execute(
                "INSERT INTO projects (id,name,path,created_at,updated_at) VALUES (?,?,?,'t','t')",
                (project, project, str(tmp_path)),
            )
        db.commit()
        set_project_cloud_models_off(OFF, cloud_models_off=True, db=db)
    finally:
        db.close()
    return isolated_db


def _thread(thread_id: str, project: str | None, model: str = SONNET, *, title: str = "New Chat"):
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,title,model,project_id,history_epoch,next_message_seq,"
            "created_at,updated_at) VALUES (?, 'user-1', ?, ?, ?, 0, 4, 't0', 't0')",
            (thread_id, title, model, project),
        )
        for seq in range(1, 4):
            db.execute(
                "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
                "VALUES (?,?,?,'body','t0',?)",
                (f"{thread_id}-m{seq}", thread_id, "user" if seq == 1 else "assistant", seq),
            )
        db.commit()
    finally:
        db.close()


def _target(model: str) -> ExecutionTarget:
    return ExecutionTarget(
        catalog_id=model, harness="claude-code", provider="claude-code", backend="cli",
        transport_model=model.split("/")[-1], expected_reported_model=None,
        context_window=200_000, available=True, availability_reason=None, display_name=model,
        supports_reasoning=True, preserve_thinking_default=True,
    )


def _pair(thread_id: str, model: str = SONNET, *, trigger: str = "pressure", turn_id="turn-1"):
    request = CheckpointRequest(
        id="c1", thread_id=thread_id, history_epoch=0, previous_generation_id=None,
        through_seq=3, through_message_id=f"{thread_id}-m3", trigger=trigger, mode="warm",
        source_harness="claude-code", source_model=model, writer_harness="claude-code",
        writer_model=model, originating_turn_id=turn_id, first_included_seq=1, started_at="t1",
    )
    provider = ProviderCheckpointRequest(
        checkpoint_id="c1", thread_id=thread_id, target=_target(model), instruction="fold",
        reasoning_effort=None, working_dir="/tmp", warm_source=object(),
    )
    return request, provider


def _scalar(sql: str, *args):
    db = get_db()
    try:
        return db.execute(sql, args).fetchone()[0]
    finally:
        db.close()


@pytest.fixture
def launch(monkeypatch) -> Mock:
    mock = Mock()
    monkeypatch.setattr(checkpoint_runtime, "_launch_checkpoint", mock)
    return mock


@pytest.mark.asyncio
async def test_dispatch_refuses_a_cloud_off_target_before_any_row_or_provider_call(world, launch):
    _thread("t-off", OFF)
    request, provider = _pair("t-off")

    with pytest.raises(CheckpointCloudOff) as refused:
        await checkpoint_runtime.request_checkpoint(request, provider)

    assert (refused.value.reason_key, str(refused.value)) == (
        "project_cloud_off", CLOUD_OFF_CHECKPOINT_MESSAGE,
    )
    assert _scalar("SELECT COUNT(*) FROM checkpoints") == 0
    launch.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_rechecks_policy_even_when_eligibility_was_read_before_cloud_went_off(
    world, launch,
):
    from llm.locality import set_project_cloud_models_off

    _thread("t-on", ON)
    request, provider = _pair("t-on")
    db = get_db()
    try:
        set_project_cloud_models_off(ON, cloud_models_off=True, db=db)
    finally:
        db.close()

    with pytest.raises(CheckpointCloudOff):
        await checkpoint_runtime.request_checkpoint(request, provider)
    launch.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_still_launches_in_a_project_with_cloud_on(world, launch):
    _thread("t-on", ON)

    await checkpoint_runtime.request_checkpoint(*_pair("t-on"))

    launch.assert_called_once()


def _completed_marker(thread_id: str, turn_id: str, model: str) -> None:
    db = get_db()
    try:
        db.execute(
            "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,"
            "started_at,ended_at,boot_epoch,idle_checkpoint_pending) "
            "VALUES (?,?,0,?,'completed','s',datetime('now'),'boot',1)",
            (turn_id, thread_id, model),
        )
        db.commit()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_idle_fire_after_cloud_went_off_consumes_the_marker_without_a_call(
    world, launch, monkeypatch,
):
    """The marker was armed while cloud was on; the fire re-reads policy and never raises."""
    from llm.locality import set_project_cloud_models_off

    _thread("t-on", ON)
    _completed_marker("t-on", "turn-idle", SONNET)
    request, provider = _pair("t-on", trigger="idle", turn_id="turn-idle")
    db = get_db()
    try:
        set_project_cloud_models_off(ON, cloud_models_off=True, db=db)
    finally:
        db.close()
    monkeypatch.setattr(
        checkpoint_runtime, "fail_idle_checkpoint",
        AsyncMock(side_effect=AssertionError("a refusal is never a failed attempt")),
    )

    result = await checkpoint_runtime.request_idle_checkpoint(
        "turn-idle", CheckpointAdmission(request, provider),
    )

    assert result.disposition is IdleCheckpointDisposition.REFUSED
    assert result.reason is IdleCheckpointReason.PROJECT_CLOUD_OFF
    assert _scalar("SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-idle'") == 0
    assert _scalar("SELECT COUNT(*) FROM checkpoints") == 0
    launch.assert_not_called()


def test_idle_marker_is_not_written_for_a_cloud_off_thread_and_is_for_a_cloud_on_one(world):
    from infra.idle_checkpoints import idle_checkpoint_exclusion_reason

    _thread("t-off", OFF)
    _thread("t-on", ON)
    db = get_db()
    try:
        assert idle_checkpoint_exclusion_reason(db, "t-off", SONNET) is (
            IdleCheckpointReason.PROJECT_CLOUD_OFF
        )
        assert idle_checkpoint_exclusion_reason(db, "t-on", SONNET) is None
    finally:
        db.close()


def test_idle_context_read_skips_a_cloud_off_thread(world):
    from infra.idle_checkpoints import IdleCheckpointDue
    from thread.idle_checkpoint import IdleCheckpointSkip, read_idle_context

    _thread("t-off", OFF)
    _completed_marker("t-off", "turn-idle", SONNET)
    due = IdleCheckpointDue("turn-idle", 1, "t-off", "2026-10-19T00:00:00")

    skipped = read_idle_context(due)

    assert isinstance(skipped, IdleCheckpointSkip)
    assert skipped.reason is IdleCheckpointReason.PROJECT_CLOUD_OFF


def _switch(thread_id: str, source: str, destination: str, monkeypatch):
    from infra.model_switch_types import SwitchRequest

    # The project row has no checkout; the working directory is incidental to these reads.
    monkeypatch.setattr(
        "subagents.codex_runtime_support.resolve_working_dir", lambda *_a, **_k: "/tmp",
    )
    db = get_db()
    try:
        db.execute(
            "UPDATE threads SET pending_model=?, switch_operation_id='op' WHERE id=?",
            (destination, thread_id),
        )
        db.commit()
    finally:
        db.close()
    return SwitchRequest(
        "op", thread_id, source, destination, 0, 1, 0, "user", "user-1", "model_switch",
        "current", "2026-10-19T00:00:00+00:00", 0,
    )


@pytest.mark.asyncio
async def test_model_switch_treats_a_cloud_off_source_as_unusable_and_makes_no_warm_call(
    world, monkeypatch,
):
    from thread import model_checkpoint

    _thread("t-off", OFF)
    request = _switch("t-off", SONNET, OPUS, monkeypatch)
    capture = AsyncMock(side_effect=AssertionError("no warm capture on a refused source"))
    monkeypatch.setattr("core.checkpoint_render.capture_warm_checkpoint_source", capture)

    context = model_checkpoint.read_switch_checkpoint_context(request)
    admission = await model_checkpoint.prepare_source_checkpoint(request, "c1")

    assert context.source_target is None
    assert context.source_refusal == "project_cloud_off"
    assert context.destination_target.catalog_id == OPUS
    assert admission is None
    capture.assert_not_called()


def test_model_switch_source_keeps_its_target_in_a_cloud_on_project(world, monkeypatch):
    from thread import model_checkpoint

    _thread("t-on", ON)

    context = model_checkpoint.read_switch_checkpoint_context(
        _switch("t-on", SONNET, OPUS, monkeypatch),
    )

    assert context.source_target.catalog_id == SONNET
    assert context.source_refusal is None


@pytest.mark.asyncio
async def test_title_is_kept_and_no_model_is_called_in_a_cloud_off_project(world, monkeypatch):
    from ws.post_turn import ensure_thread_title

    _thread("t-off", OFF)
    invoke = AsyncMock(side_effect=AssertionError("no title call for a cloud-off thread"))
    monkeypatch.setattr("llm.isolated_structured.invoke_isolated_structured", invoke)

    async def no_send(_message: dict) -> None:
        return None

    await ensure_thread_title("t-off", no_send)

    assert _scalar("SELECT title FROM threads WHERE id='t-off'") == "New Chat"
    invoke.assert_not_called()


def test_cache_warm_is_not_run_for_a_cloud_off_thread(world):
    from api.thread_cache_warm import _load_warm_target

    _thread("t-off", OFF)
    _thread("t-on", ON)

    refused = _load_warm_target("t-off", "user-1")

    assert refused == {
        "ok": False, "skipped": True, "reason": "project_cloud_off",
        "message": "Cloud models are off in this project, so this thread was not warmed",
    }
    allowed = _load_warm_target("t-on", "user-1")
    assert not isinstance(allowed, dict) or allowed.get("reason") != "project_cloud_off"


def test_external_checkpoint_context_is_not_read_for_a_cloud_off_thread(world):
    from subagents.harness_checkpoint_context import read_external_checkpoint_context

    _thread("t-off", OFF)

    assert read_external_checkpoint_context("t-off", SONNET, "claude-code") is None


def test_tool_media_is_declined_with_the_refusal_text_in_a_cloud_off_project(world):
    from core.media_delivery import tool_result_media_for_model
    from core.media_locality import media_locality_refusal_text

    _thread("t-off", OFF)
    _thread("t-on", ON)
    metadata = {"media": {
        "path": "/tmp/shot.png", "mime_type": "image/png", "kind": "image",
        "byte_size": 1, "sha256": "0" * 64,
    }}

    def delivery(thread_id: str):
        return SimpleNamespace(
            message_id=f"{thread_id}-m3", content="[Image: shot.png]", media_metadata=metadata,
        )

    declined = tool_result_media_for_model(delivery("t-off"), SONNET)

    assert declined is not None and declined.image is None
    assert declined.text.startswith("[Image: shot.png] project_cloud_off:")
    assert media_locality_refusal_text(delivery("t-on"), SONNET) is None
    assert media_locality_refusal_text(SimpleNamespace(message_id=None), SONNET) is None


def _client(monkeypatch) -> TestClient:
    from api import checkpoints

    monkeypatch.setattr(
        checkpoints, "authenticate", lambda token: {"id": "user-1"} if token == "valid" else None,
    )
    app = FastAPI()
    app.include_router(checkpoints.router, prefix="/api")
    return TestClient(app)


def test_manual_checkpoint_http_refusal_is_the_exact_pinned_body(world, launch, monkeypatch):
    capture = AsyncMock(side_effect=AssertionError("no provider capture for a cloud-off thread"))
    monkeypatch.setattr("core.checkpoint_render.capture_warm_checkpoint_source", capture)
    _thread("t-off", OFF)

    response = _client(monkeypatch).post("/api/threads/t-off/checkpoints?token=valid")

    assert response.status_code == 409
    assert response.json() == {"detail": {
        "reason": "project_cloud_off",
        "message": (
            "Cloud models are off in this project. "
            "Switch this chat to a local model to checkpoint it."
        ),
    }}
    capture.assert_not_called()
    launch.assert_not_called()
    assert _scalar("SELECT COUNT(*) FROM checkpoints") == 0


@pytest.mark.asyncio
async def test_agent_checkpoint_tool_refuses_with_its_own_text_and_makes_no_call(
    world, launch, monkeypatch,
):
    from tools import request_checkpoint

    _thread("t-off", OFF)
    emitted = []

    async def no_native():
        return None

    monkeypatch.setattr(request_checkpoint, "_emit", lambda *a, **k: emitted.append(a))
    monkeypatch.setattr("core.agent_checkpoint_runtime.prepare_native_agent_checkpoint", no_native)

    result = await request_checkpoint.RequestCheckpointTool().execute(
        "boundary", _thread_id="t-off",
    )

    assert result == (
        "Cloud models are off in this project, so a checkpoint cannot run on this "
        "thread's model. Continue working."
    )
    assert emitted[0][1:3] == ("refused", "project_cloud_off")
    launch.assert_not_called()


@pytest.mark.asyncio
async def test_foreground_send_still_refuses_through_the_shared_helper(world):
    from infra import send_target

    _thread("t-off", OFF)
    _thread("t-on", ON)

    with pytest.raises(send_target.TargetUnavailable) as refused:
        await send_target.resolve_target(SONNET, thread_id="t-off")

    assert "project_cloud_off" in refused.value.detail
    assert (await send_target.resolve_target(SONNET, thread_id="t-on")).catalog_id == SONNET
