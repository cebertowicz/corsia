"""HTTP contracts for the clean checkpoint projection and exact cancel."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.checkpoint_types import CheckpointRequest, ProviderCheckpointRequest
from llm.resolution import ExecutionTarget


def _seed(thread_id: str, *, user_id: str = "user-1") -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads "
            "(id,user_id,model,history_epoch,next_message_seq,created_at,updated_at) "
            "VALUES (?,?, 'openai/test-checkpoint',0,2,'t0','t0')",
            (thread_id, user_id),
        )
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
            "VALUES (?,?,'user','body','t0',1)",
            (f"{thread_id}-m1", thread_id),
        )
        db.commit()
    finally:
        db.close()


def _request(thread_id: str, checkpoint_id: str) -> CheckpointRequest:
    return CheckpointRequest(
        id=checkpoint_id,
        thread_id=thread_id,
        history_epoch=0,
        previous_generation_id=None,
        through_seq=1,
        through_message_id=f"{thread_id}-m1",
        trigger="manual",
        mode="warm",
        source_harness="native",
        source_model="openai/test-checkpoint",
        writer_harness="native",
        writer_model="openai/test-checkpoint",
        originating_turn_id=None,
        first_included_seq=None,
        started_at="t1",
    )


def _provider(thread_id: str, checkpoint_id: str) -> ProviderCheckpointRequest:
    target = ExecutionTarget(
        catalog_id="openai/test-checkpoint",
        harness="native",
        provider="openai",
        backend="api",
        transport_model="test-checkpoint",
        expected_reported_model=None,
        context_window=200_000,
        available=True,
        availability_reason=None,
        display_name="test",
        supports_reasoning=True,
        preserve_thinking_default=True,
    )
    return ProviderCheckpointRequest(
        fork_id=checkpoint_id,
        thread_id=thread_id,
        target=target,
        instruction="checkpoint",
        reasoning_effort=None,
        working_dir="/tmp",
    )


def _client(monkeypatch) -> TestClient:
    from api import checkpoints

    monkeypatch.setattr(
        checkpoints,
        "authenticate",
        lambda token: {"id": "user-1"} if token == "valid" else None,
    )
    app = FastAPI()
    app.include_router(checkpoints.router, prefix="/api")
    return TestClient(app)


def test_manual_non_admission_is_visible_and_creates_no_row(isolated_db, monkeypatch):
    from api import checkpoints
    from infra.database import get_db

    _seed("thread-1")

    async def decline(_thread_id, _user_id):
        raise checkpoints.CheckpointNotReady("writer has no spare capacity")

    monkeypatch.setattr(checkpoints, "_prepare_manual", decline)
    response = _client(monkeypatch).post(
        "/api/threads/thread-1/checkpoints?token=valid"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "message": "writer has no spare capacity",
        "reason": "not_ready",
    }
    db = get_db()
    try:
        assert db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
    finally:
        db.close()


def test_manual_checkpoint_names_nothing_new_to_fold(isolated_db, monkeypatch):
    """The frontend used to show a generic "try again" for every refusal
    (#1952); the real reason (nothing sealed since the last checkpoint) is
    now a stable machine-readable code the UI can act on instead of prose."""
    from api import checkpoints

    _seed("thread-1")

    async def decline(_thread_id, _user_id):
        raise checkpoints.CheckpointNotReady(
            "checkpoint already covers the sealed history",
            reason_key="no_new_history",
        )

    monkeypatch.setattr(checkpoints, "_prepare_manual", decline)
    response = _client(monkeypatch).post(
        "/api/threads/thread-1/checkpoints?token=valid"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "message": "checkpoint already covers the sealed history",
        "reason": "no_new_history",
    }


def test_manual_request_returns_the_durable_operation(isolated_db, monkeypatch):
    from api import checkpoints
    from core import checkpoint_runtime
    from infra.checkpoints import create_running

    _seed("thread-1")
    request = _request("thread-1", "cp-1")
    provider = _provider("thread-1", "cp-1")

    async def prepare(_thread_id, _user_id):
        return request, provider

    async def admit(given, _provider_request):
        return create_running(given)

    monkeypatch.setattr(checkpoints, "_prepare_manual", prepare)
    monkeypatch.setattr(checkpoint_runtime, "request_checkpoint", admit)
    response = _client(monkeypatch).post(
        "/api/threads/thread-1/checkpoints?token=valid"
    )

    assert response.status_code == 202
    assert response.json()["checkpoint"] == {
        "id": "cp-1",
        "trigger": "manual",
        "mode": "warm",
        "status": "running",
        "writerModel": "openai/test-checkpoint",
        "error": None,
        "startedAt": "t1",
    }


def test_manual_request_returns_exact_installed_race_winner(
    isolated_db,
    monkeypatch,
):
    from dataclasses import replace

    from api import checkpoints
    from core import checkpoint_runtime
    from infra import checkpoints as store

    _seed("thread-1")
    request = _request("thread-1", "cp-candidate")
    provider = _provider("thread-1", "cp-candidate")
    real_create = store.create_running
    raced = False
    provider_calls = 0

    async def prepare(_thread_id, _user_id):
        return request, provider

    async def invoke(_request, _cancel):
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("installed winner must not dispatch the stale candidate")

    def create_with_winner(candidate):
        nonlocal raced
        if not raced:
            raced = True
            winner = real_create(
                replace(candidate, id="cp-winner")
            )
            store.capture(
                winner.id, raw_result="winner", usage_json=None, captured_at="t2",
            )
            store.install(winner.id, installed_at="t3")
        return real_create(candidate)

    monkeypatch.setattr(checkpoints, "_prepare_manual", prepare)
    monkeypatch.setattr(checkpoint_runtime, "_invoke", invoke)
    monkeypatch.setattr(store, "create_running", create_with_winner)
    response = _client(monkeypatch).post(
        "/api/threads/thread-1/checkpoints?token=" + "valid"
    )

    assert response.status_code == 202
    assert response.json()["checkpoint"]["id"] == "cp-winner"
    assert response.json()["checkpoint"]["status"] == "installed"
    assert provider_calls == 0
    assert checkpoint_runtime.checkpoint_task_count() == 0


def test_manual_request_reuses_active_row_without_preparing_another_call(
    isolated_db, monkeypatch
):
    from api import checkpoints
    from infra.checkpoints import create_running

    _seed("thread-1")
    create_running(_request("thread-1", "cp-1"))

    async def forbidden(_thread_id, _user_id):
        raise AssertionError("active checkpoint must coalesce before preparation")

    monkeypatch.setattr(checkpoints, "_prepare_manual", forbidden)
    response = _client(monkeypatch).post(
        "/api/threads/thread-1/checkpoints?token=valid"
    )

    assert response.status_code == 202
    assert response.json()["checkpoint"]["id"] == "cp-1"


def test_history_and_cancel_are_scoped_to_exact_thread(isolated_db, monkeypatch):
    from infra.checkpoints import create_running

    _seed("thread-1")
    _seed("thread-2")
    create_running(_request("thread-1", "cp-1"))
    client = _client(monkeypatch)

    history = client.get("/api/threads/thread-1/checkpoints?token=valid")
    wrong = client.post("/api/threads/thread-2/checkpoints/cp-1/cancel?token=valid")
    cancelled = client.post(
        "/api/threads/thread-1/checkpoints/cp-1/cancel?token=valid"
    )

    assert history.status_code == 200
    assert history.json()["checkpoints"][0]["id"] == "cp-1"
    assert wrong.status_code == 404
    assert cancelled.status_code == 200
    assert cancelled.json()["checkpoint"]["status"] == "cancelled"
