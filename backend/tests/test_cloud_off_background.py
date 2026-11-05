"""Background work never sends a cloud-off project's history to the thread's own cloud model.

Every path resolves through ``llm.resolution.resolve_thread_target``, so each refuses in its
existing unavailable branch; these pin that each one reaches it and makes no call.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import checkpoint_runtime
from core.checkpoint_types import IdleCheckpointReason
from infra.database import get_db

SONNET = "claude-code/sonnet"
OPUS = "claude-code/opus"
LOCAL, LAN_MODEL = "omlx@loop/m", "omlx@lan/n"
OFF, ON = "project-off", "project-on"


def _server(db, server_id: str, host: str) -> None:
    db.execute(
        "INSERT INTO network_servers(id,provider,kind,instance_id,instance_name,port,status,metadata_json,"
        "last_seen_at,created_at,updated_at) VALUES (?,'omlx','managed',?,?,8000,'online','{}','t','t','t')",
        (server_id, server_id, server_id),
    )
    db.execute(
        "INSERT INTO network_server_endpoints(id,server_id,host,kind,created_at,updated_at)"
        " VALUES (?,?,?,?,'t','t')",
        (f"ep-{server_id}", server_id, host, "local" if host == "127.0.0.1" else "lan"),
    )


@pytest.fixture
def world(isolated_db, tmp_path):
    """Two projects, one with cloud models off, a model on this Mac and one on the LAN."""
    from llm.locality import set_project_cloud_models_off
    from tests.unavailable_model_support import seed_model
    from tests.vendor_account_fixtures import signed_in_default

    db = get_db()
    try:
        for project in (OFF, ON):
            db.execute(
                "INSERT INTO projects (id,name,path,created_at,updated_at) VALUES (?,?,?,'t','t')",
                (project, project, str(tmp_path)),
            )
        _server(db, "loop", "127.0.0.1")
        _server(db, "lan", "192.168.1.2")
        db.commit()
        set_project_cloud_models_off(OFF, cloud_models_off=True, db=db)
    finally:
        db.close()
    seed_model(LOCAL)
    seed_model(LAN_MODEL)
    # The thread's own cloud model is otherwise runnable: Claude Code has a signed-in account.
    signed_in_default("claude-code")
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


def test_the_thread_resolver_refuses_cloud_and_lan_rows_only_in_a_cloud_off_project(world):
    from infra.send_target import classify_target_admission
    from llm.resolution import resolve_thread_target

    _thread("t-off", OFF)
    _thread("t-on", ON)
    _thread("t-none", None)

    for model in (SONNET, LAN_MODEL):
        refused = resolve_thread_target(model, thread_id="t-off")
        assert (refused.catalog_id, refused.available, refused.availability_reason) == (
            model, False, "project_cloud_off",
        )
        assert classify_target_admission(refused) == "wait"
    assert resolve_thread_target(LOCAL, thread_id="t-off").availability_reason is None
    for thread_id in ("t-on", "t-none", None):
        assert resolve_thread_target(SONNET, thread_id=thread_id).available is True


def test_checkpoint_eligibility_names_the_refusal_and_keeps_model_unavailable_apart(world):
    from core.checkpoint_eligibility import checkpoint_eligibility

    _thread("t-off", OFF)
    _thread("t-on", ON)
    db = get_db()
    try:
        off = checkpoint_eligibility("t-off", "user-1", db=db)
        on = checkpoint_eligibility("t-on", "user-1", db=db)
    finally:
        db.close()

    assert (off.eligible, off.reason_key) == (False, "project_cloud_off")
    assert off.target.availability_reason == "project_cloud_off"
    assert on.reason_key != "project_cloud_off"


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


def test_idle_context_read_skips_a_cloud_off_thread_with_its_cause(world):
    from infra.idle_checkpoints import IdleCheckpointDue
    from thread.idle_checkpoint import IdleCheckpointSkip, read_idle_context

    _thread("t-off", OFF)
    _completed_marker("t-off", "turn-idle", SONNET)

    skipped = read_idle_context(IdleCheckpointDue("turn-idle", 1, "t-off", "2026-10-19T00:00:00"))

    assert isinstance(skipped, IdleCheckpointSkip)
    assert (skipped.reason, skipped.cause) == (IdleCheckpointReason.LOCALITY, "project_cloud_off")


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
async def test_switch_to_local_treats_a_cloud_off_source_as_unusable_and_makes_no_warm_call(
    world, monkeypatch,
):
    from thread import model_checkpoint

    _thread("t-off", OFF)
    request = _switch("t-off", SONNET, LOCAL, monkeypatch)
    capture = AsyncMock(side_effect=AssertionError("no warm capture on a refused source"))
    monkeypatch.setattr("core.fork_render.capture_warm_fork_source", capture)

    context = model_checkpoint.read_switch_checkpoint_context(request)
    admission = await model_checkpoint.prepare_source_checkpoint(request, "c1")

    assert context.source_target is None
    assert context.destination_target.catalog_id == LOCAL
    assert admission is None
    capture.assert_not_called()


def test_switch_to_another_cloud_model_fails_visibly_with_the_refusal(world, monkeypatch):
    from thread import model_checkpoint

    _thread("t-off", OFF)

    with pytest.raises(RuntimeError, match="^project_cloud_off$"):
        model_checkpoint.read_switch_checkpoint_context(_switch("t-off", SONNET, OPUS, monkeypatch))


def test_model_switch_source_keeps_its_target_in_a_cloud_on_project(world, monkeypatch):
    from thread import model_checkpoint

    _thread("t-on", ON)

    context = model_checkpoint.read_switch_checkpoint_context(
        _switch("t-on", SONNET, OPUS, monkeypatch),
    )

    assert context.source_target.catalog_id == SONNET


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


@pytest.mark.asyncio
async def test_cache_warm_refuses_a_lan_model_in_a_cloud_off_project_before_any_call(world):
    from api.thread_cache_warm import _load_warm_target, _prove_warm_target

    _thread("t-off", OFF, LAN_MODEL)

    target, _project, _effort, _thread_row = _load_warm_target("t-off", "user-1")
    proved = await _prove_warm_target(target, "t-off")

    assert (target.available, target.availability_reason) == (False, "project_cloud_off")
    assert isinstance(proved, dict) and proved["ok"] is False


def test_external_checkpoint_context_is_not_read_for_a_cloud_off_thread(world):
    from subagents.harness_checkpoint_context import read_external_checkpoint_context

    _thread("t-off", OFF)

    assert read_external_checkpoint_context("t-off", SONNET, "claude-code") is None


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
    monkeypatch.setattr("core.fork_render.capture_warm_fork_source", capture)
    _thread("t-off", OFF)

    response = _client(monkeypatch).post("/api/threads/t-off/checkpoints", params={"token": "valid"})

    assert response.status_code == 409
    assert response.json() == {"detail": {
        "reason": "project_cloud_off",
        "message": (
            "This project uses local models only. "
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
        "This project uses local models only, so a checkpoint cannot run on this "
        "thread's model. Continue working."
    )
    assert emitted[0][1:3] == ("refused", "project_cloud_off")
    launch.assert_not_called()
