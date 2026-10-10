"""The contract runner must isolate credentials and all child-process egress."""

from __future__ import annotations

import errno
import asyncio
import socket
import sys
from pathlib import Path

import httpx
import pytest

from tests.contract import matrix_backend, matrix_runner
from tests.contract.matrix_environment import isolated_environment


def test_matrix_environment_does_not_inherit_credentials_or_host_config(tmp_path, monkeypatch):
    """A newly added provider credential must not need another denylist entry."""
    monkeypatch.setenv("FUTURE_PROVIDER_CREDENTIAL", "fixture-not-a-real-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-not-a-real-key")
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.invalid:8080")
    monkeypatch.setenv("CODEX_HOME", "/host/private-codex")
    backend = matrix_runner.IsolatedBackend(tmp_path)
    backend.port = 43123

    env = backend._env()

    assert "FUTURE_PROVIDER_CREDENTIAL" not in env
    assert "OPENAI_API_KEY" not in env
    assert "HTTPS_PROXY" not in env
    assert Path(env["HOME"]).is_relative_to(tmp_path)
    assert Path(env["CODEX_HOME"]).is_relative_to(tmp_path)
    assert env["LITELLM_LOCAL_MODEL_COST_MAP"] == "true"
    assert env["CORSIA_WORKSPACE"] == str(backend.workspace)
    assert env["CORSIA_STATE_DIR"] == str(backend.state)
    assert env.get("BACKEND_PORT") == "43123"


def test_matrix_environment_preserves_nonempty_runner_tracking_id(tmp_path, monkeypatch):
    marker = "runner-job-123"
    monkeypatch.setenv("RUNNER_TRACKING_ID", marker)

    env = isolated_environment(tmp_path, tmp_path / "workspace", tmp_path / "state")

    assert env["RUNNER_TRACKING_ID"] == marker


@pytest.mark.parametrize("marker", [None, ""], ids=["absent", "empty"])
def test_matrix_environment_omits_absent_or_empty_runner_tracking_id(tmp_path, monkeypatch, marker):
    if marker is None:
        monkeypatch.delenv("RUNNER_TRACKING_ID", raising=False)
    else:
        monkeypatch.setenv("RUNNER_TRACKING_ID", marker)

    env = isolated_environment(tmp_path, tmp_path / "workspace", tmp_path / "state")

    assert "RUNNER_TRACKING_ID" not in env


@pytest.mark.skipif(sys.platform != "darwin", reason="CI uses macOS Seatbelt")
@pytest.mark.parametrize("child", [False, True], ids=["backend", "descendant"])
def test_matrix_process_can_reach_fixture_but_not_unlisted_listener(tmp_path, monkeypatch, child):
    """Use two real local endpoints: no live inference endpoint is ever contacted."""
    fixture_root = Path(__file__).parent / "fixtures" / "contract_isolation"
    monkeypatch.setattr(matrix_backend, "BACKEND_ROOT", fixture_root)
    # The probe app has no database and serves no authenticated route.
    monkeypatch.setattr(matrix_backend.IsolatedBackend, "_seed_accounts", lambda self: None)
    backend = matrix_runner.IsolatedBackend(tmp_path)
    with socket.socket() as unlisted:
        unlisted.bind(("127.0.0.1", 0))
        unlisted.listen()
        try:
            backend.boot(timeout=15)
            with httpx.Client(base_url=backend.base_url, trust_env=False) as client:
                allowed = client.post("/probe", json={"port": backend.port, "child": child})
                denied = client.post("/probe", json={"port": unlisted.getsockname()[1], "child": child})
            assert allowed.json() == {"errno": 0}
            assert denied.json() == {"errno": errno.EPERM}
        finally:
            backend.stop()


@pytest.mark.integration
def test_native_answers_twice_with_real_app_inside_network_guard(tmp_path):
    """The guard must preserve the real HTTP/WS/DB journey, not only deny sockets."""
    backend = matrix_runner.IsolatedBackend(tmp_path)
    try:
        backend.boot(timeout=30)
        asyncio.run(_two_native_turns(backend))
    finally:
        backend.stop()


async def _two_native_turns(backend):
    client = matrix_runner.BackendClient(backend)
    thread_id = await client.create_session("guarded native two-turn qualification")
    for expected in ("FIRST_GUARDED_ANSWER", "SECOND_GUARDED_ANSWER"):
        await client.seed_replies([{"content": expected, "finish_reason": "stop"}])
        async with matrix_runner.TurnDriver(backend) as driver:
            await driver.subscribe(thread_id)
            await driver.send(thread_id, expected)
            frames = await driver.collect(thread_id, until=("turn_complete", "error"), timeout=15)
        assert expected in matrix_runner.assistant_texts(frames), frames
        assert "turn_complete" in matrix_runner.frame_types(frames), frames
        assert await client.call_count() == 1
    users = [row for row in await client.messages(thread_id) if row["role"] == "user"]
    assert len(users) == 2
    assert [row["delivery_state"] for row in users] == ["delivered", "delivered"]
