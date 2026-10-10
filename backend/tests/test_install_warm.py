"""Install pre-warm: suppression, route target, join-by-model and the CLI."""

from __future__ import annotations

import asyncio
import io
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from cache_warm_fixtures import (
    _fake_session_row,
    _clear_warm_state,  # noqa: F401  (autouse: clears the warm task map)
    install_cache_warm_isolation,
    install_replay_candidates,
    install_resolved_omlx_session,
    make_exact_replay_selection,
)

SECRET_ENOUGH = "long-enough-passphrase"
MODEL = "omlx@srv-1/model-1"


@pytest.fixture
def fresh_install(isolated_db):
    """A real first account: its conductor holds the wake and the digest."""
    from infra import accounts
    from infra.conductor import WORKSPACE_SCOPE, get_conductor
    from infra.database import write_transaction

    with write_transaction(None, scope="test_install_warm") as db:
        db.execute("DELETE FROM auth_tokens")
        db.execute("DELETE FROM users")
    accounts.create_user("newcomer", SECRET_ENOUGH)
    conductor_id = get_conductor(WORKSPACE_SCOPE)
    assert conductor_id
    return conductor_id


def _reason(thread_id: str):
    from core.thread_warm import warm_suppression_reason
    from infra.database import get_db

    db = get_db()
    try:
        return warm_suppression_reason(thread_id, db=db)
    finally:
        db.close()


def _write(sql: str, *args) -> None:
    from infra.database import write_transaction

    with write_transaction(None, scope="test_install_warm") as db:
        db.execute(sql, args)


# --- test 2: suppression ----------------------------------------------------


def test_a_fresh_first_run_conductor_is_not_suppressed(fresh_install):
    from infra.database import get_db

    db = get_db()
    held = db.execute(
        "SELECT delivery_error FROM messages WHERE thread_id=? "
        "AND input_order IS NOT NULL AND delivery_state='queued'",
        (fresh_install,),
    ).fetchall()
    db.close()
    # Both the digest and the wake wait, held for the person's model choice.
    assert len(held) == 2
    assert {row["delivery_error"] for row in held} == {"no_model_selected"}
    assert _reason(fresh_install) is None


def test_ordinary_queued_input_still_suppresses(fresh_install):
    _write(
        "UPDATE messages SET delivery_error=NULL WHERE thread_id=? "
        "AND delivery_state='queued'", fresh_install,
    )
    assert _reason(fresh_install) == "queued_input"


def test_a_row_held_for_an_account_still_suppresses(fresh_install):
    _write(
        "UPDATE messages SET delivery_error='account_limited' WHERE thread_id=? "
        "AND delivery_state='queued'", fresh_install,
    )
    assert _reason(fresh_install) == "queued_input"


def test_a_running_turn_still_suppresses(fresh_install):
    _write(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,"
        "started_at,boot_epoch) VALUES ('t1',?,0,'m','running','t','b')",
        fresh_install,
    )
    assert _reason(fresh_install) == "active_turn"


# --- test 3: route ----------------------------------------------------------


def _route_session(monkeypatch, bound_model: str):
    from api import thread_cache_warm as api_runtime
    from llm.resolution import ModelUnavailableError

    install_resolved_omlx_session(monkeypatch, api_runtime, MODEL)
    monkeypatch.setattr(
        api_runtime, "get_thread_for_user",
        lambda *_a, **_k: _fake_session_row(bound_model),
    )
    resolved: list[str] = []
    real = api_runtime.resolve_execution_target

    def _resolve(selected, *, db):
        resolved.append(selected)
        if not selected:
            raise ModelUnavailableError("(none)", "No model configured")
        return real(selected, db=db)

    monkeypatch.setattr(api_runtime, "resolve_execution_target", _resolve)
    monkeypatch.setattr(
        "core.thread_warm.warm_suppression_reason", lambda *_a, **_k: None,
    )
    return api_runtime, resolved


def test_route_accepts_a_model_for_a_model_less_thread(monkeypatch):
    api_runtime, resolved = _route_session(monkeypatch, "")
    loaded = api_runtime._load_warm_target("t", "steve", MODEL)
    assert not isinstance(loaded, dict)
    assert resolved == [MODEL]


def test_route_accepts_the_bound_model_like_an_omitted_one(monkeypatch):
    api_runtime, resolved = _route_session(monkeypatch, MODEL)
    assert not isinstance(api_runtime._load_warm_target("t", "steve", MODEL), dict)
    assert not isinstance(api_runtime._load_warm_target("t", "steve", None), dict)
    assert resolved == [MODEL, MODEL]


def test_route_refuses_a_model_that_differs_from_the_bound_one(monkeypatch):
    api_runtime, resolved = _route_session(monkeypatch, MODEL)
    with pytest.raises(HTTPException) as refused:
        api_runtime._load_warm_target("t", "steve", "omlx@srv-1/other")
    assert refused.value.status_code == 409
    assert resolved == []


def test_route_without_a_model_on_a_model_less_thread_keeps_todays_answer(monkeypatch):
    api_runtime, _ = _route_session(monkeypatch, "")
    loaded = api_runtime._load_warm_target("t", "steve", None)
    assert loaded["ok"] is False


@pytest.mark.asyncio
async def test_route_refuses_a_model_that_is_not_omlx(monkeypatch):
    from llm.resolution import ExecutionTarget

    api_runtime, _ = _route_session(monkeypatch, "")
    cloud = ExecutionTarget(
        catalog_id="claude-code/sonnet", harness="claude-code", provider="anthropic",
        backend="api", transport_model="claude-code/sonnet",
        expected_reported_model=None, context_window=200000, available=True,
        availability_reason=None, display_name="Sonnet", supports_reasoning=False,
    )
    monkeypatch.setattr(api_runtime, "resolve_execution_target", lambda *_a, **_k: cloud)
    response = await api_runtime.warm_session_cache("t", token="x", model="claude-code/sonnet")
    assert response == {
        "ok": False, "message": "Not a warmable available Native oMLX network model",
    }


def test_the_install_service_token_reaches_the_accounts_own_conductor(fresh_install):
    """The CLI's token is a service token for the oldest account; it owns the conductor."""
    from api import thread_cache_warm as api_runtime
    from infra import accounts

    token, _ = accounts.issue_token("newcomer", kind="service", label="lifecycle")
    user = api_runtime.authenticate(token)
    assert user["id"] == "newcomer"
    loaded = api_runtime._load_warm_target(fresh_install, user["id"], None)
    # Reaches the model check (nothing bound yet), not a 404 for the thread.
    assert isinstance(loaded, dict) and loaded["ok"] is False


# --- test 4: join by model ---------------------------------------------------


@pytest.mark.asyncio
async def test_delivery_joins_only_a_warm_of_its_own_model():
    from core.thread_warm import join_requested_warm_for_delivery, start_thread_warm
    from infra import state

    release = asyncio.Event()

    async def _slow():
        await release.wait()
        return {"ok": True}

    task, started, _ = await start_thread_warm(
        "thread-x", "key-a", _slow, model="omlx@srv-1/model-A",
        admission_check=lambda: None,
    )
    assert started

    # Delivery on native model B does not wait, and does not cancel the warm.
    assert await asyncio.wait_for(
        join_requested_warm_for_delivery("thread-x", "omlx@srv-1/model-B"), 0.5,
    ) is False
    assert not task.done() and not task.cancelling()
    assert state._active_warm_tasks["thread-x"] is task

    # Delivery on the warm's own model waits for it.
    joiner = asyncio.create_task(join_requested_warm_for_delivery("thread-x", "omlx@srv-1/model-A"))
    await asyncio.sleep(0.05)
    assert not joiner.done()
    release.set()
    assert await asyncio.wait_for(joiner, 1) is True


# --- test 5: CLI -------------------------------------------------------------


class _Out(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _starter_row(status: str) -> dict:
    from llm.local_profiles import DEFAULT_PROFILE

    return {
        "id": "omlx-starter", "status": status, "backend": "network",
        "provider": "omlx", "litellm_model": f"omlx@srv-1/{DEFAULT_PROFILE.model}",
    }


@pytest.fixture
def cli(monkeypatch):
    import cli_conductor

    monkeypatch.setenv("CORSIA_SOURCES_SERVICE_TOKEN", "svc")
    monkeypatch.delenv("CORSIA_INSTALL_FLOW", raising=False)
    calls: list[str] = []

    def _request(method, path, token, timeout, body=None):
        calls.append(f"{method} {path.split('?')[0]}")
        if path == "/api/models":
            return {"models": [_starter_row(cli.starter_status)]}
        if path.startswith("/api/conductor"):
            return {"thread_id": "cond-1"}
        raise AssertionError(path)

    monkeypatch.setattr(cli_conductor, "request_json", _request)
    cli.starter_status = "ready"
    cli.calls = calls
    cli.module = cli_conductor
    return cli


def _run(cli, reply, *, tty=False, finish_at: float | None = 3):
    """Run the CLI with one held-open warm request that returns at ``finish_at``.

    The fake clock advances one second per tick; the request answers when the
    clock reaches ``finish_at`` (never, when None, to model a warm that outlasts
    the deadline).
    """
    out, clock = _Out(tty), _Clock()
    gate = threading.Event()

    def _post(thread_id, catalog_id, token):
        cli.calls.append(f"POST warm {thread_id} {catalog_id}")
        assert gate.wait(5)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def _tick(seconds):
        clock.sleep(seconds)
        if finish_at is not None and clock.now >= finish_at:
            gate.set()
            # Let the request thread finish so the fake clock cannot outrun it.
            for worker in threading.enumerate():
                if worker.name == cli.module.REQUEST_THREAD:
                    worker.join(5)

    try:
        code = cli.module.warm_conductor(out=out, clock=clock, sleep=_tick, call=_post)
    finally:
        gate.set()
    return code, out.getvalue()


def test_cli_is_silent_when_the_starter_is_not_ready(cli, capsys):
    cli.starter_status = "available"
    code, text = _run(cli, None)
    assert (code, text) == (0, "")
    assert capsys.readouterr().err == ""
    assert cli.calls == ["GET /api/models"]


def test_cli_is_silent_without_a_token(cli, monkeypatch):
    monkeypatch.delenv("CORSIA_SOURCES_SERVICE_TOKEN")
    assert _run(cli, None) == (0, "")
    assert cli.calls == []


def test_cli_holds_one_request_and_ticks_to_done(cli):
    code, text = _run(cli, {"ok": True, "message": "Warm finished"}, tty=True)
    assert code == 0
    assert "Getting your conductor ready (its first reply will be faster) · 0:00" in text
    assert "about" not in text
    assert "\r\x1b[K" in text
    assert "· 0:02" in text
    assert text.rstrip().endswith("Conductor ready.")
    assert [c for c in cli.calls if c.startswith("POST")] == ["POST warm cond-1 omlx-starter"]


def test_cli_already_warm_is_done(cli):
    code, text = _run(cli, {"ok": True, "skipped": True, "message": "Already warm"}, finish_at=0)
    assert code == 0
    assert text.splitlines()[-1] == "Conductor ready."


def test_cli_failure_prints_one_line_and_exits_zero(cli):
    code, text = _run(cli, {"ok": False, "message": "model not loaded"})
    assert code == 0
    assert text.splitlines()[-1] == (
        "Couldn't get the conductor ready ahead of time; "
        "its first reply will take a little longer."
    )
    assert text.count("Couldn't") == 1
    assert "Conductor ready." not in text


def test_cli_request_error_is_a_failure_not_a_crash(cli):
    code, text = _run(cli, OSError("refused"))
    assert code == 0
    assert text.splitlines()[-1].startswith("Couldn't get the conductor ready")


def test_cli_timeout_prints_one_line_and_exits_zero(cli):
    code, text = _run(cli, {"ok": True, "message": "Warm finished"}, finish_at=None)
    assert code == 0
    assert text.splitlines()[-1] == "Still getting ready in the background; you can start now."
    assert len([c for c in cli.calls if c.startswith("POST")]) == 1


@pytest.mark.parametrize("route", ["/api/models", "/api/conductor"])
def test_cli_discovery_error_names_the_route_on_stderr_and_exits_zero(
    cli, monkeypatch, capsys, route,
):
    real = cli.module.request_json

    def _broken(method, path, token, timeout, body=None):
        if path.split("?")[0] == route:
            raise ValueError("unreadable body")
        return real(method, path, token, timeout, body)

    monkeypatch.setattr(cli.module, "request_json", _broken)
    code, text = _run(cli, None)
    assert (code, text) == (0, "")
    assert capsys.readouterr().err == (
        f"conductor warm: GET {route} failed (ValueError)\n"
    )
    assert not [c for c in cli.calls if c.startswith("POST")]


def test_cli_numbers_its_line_only_inside_the_installer(cli, monkeypatch):
    monkeypatch.setenv("CORSIA_INSTALL_FLOW", "1")
    _, text = _run(cli, {"ok": True, "message": "Warm finished"})
    assert text.splitlines()[-1] == "8/8  Conductor ready."


def test_cli_registers_under_the_existing_parser():
    import argparse

    import cli_conductor

    parser = argparse.ArgumentParser()
    cli_conductor.build_parser(parser.add_subparsers(dest="cmd"))
    assert parser.parse_args(["conductor", "warm"]).func is cli_conductor.cmd_warm


# --- route: wait=true ---------------------------------------------------------


def _wait_route(monkeypatch, warm):
    """The real warm route over a fake provider whose warm is ``warm``."""
    from api import thread_cache_warm as api_runtime
    from cache_warm_fixtures import _make_mock_agent

    install_cache_warm_isolation(monkeypatch)
    install_replay_candidates(
        monkeypatch,
        exact=make_exact_replay_selection(
            "install-warm-replay", [[("user", "earlier user msg")]],
        ),
    )
    install_resolved_omlx_session(monkeypatch, api_runtime, MODEL)
    monkeypatch.setattr(
        "core.agent.get_agent", AsyncMock(return_value=_make_mock_agent(system_prompt="S")),
    )
    monkeypatch.setattr("core.native_context_prefix.build_real_send_prefix", AsyncMock(
        return_value=(
            [{"role": "system", "content": "same-prefix"}],
            {"history_messages": 0, "history_tokens": 0, "was_trimmed": False},
        ),
    ))
    provider = MagicMock()
    provider.probe_cache = AsyncMock(return_value={"ok": False})
    provider.warm_cache = warm
    monkeypatch.setattr("llm.providers.for_provider", lambda _name: provider)
    return api_runtime


@pytest.mark.asyncio
async def test_wait_true_answers_when_the_warm_finishes(monkeypatch):
    release = asyncio.Event()

    async def _warm(cfg, *, model_id, messages, tools=None, **_shape):
        await release.wait()
        return {"ok": True}

    route = _wait_route(monkeypatch, _warm)
    request = asyncio.create_task(route.warm_session_cache("sess-1", token="", wait=True))
    await asyncio.sleep(0.05)
    assert not request.done()
    release.set()
    assert await asyncio.wait_for(request, 1) == {"ok": True, "message": "Warm finished"}


@pytest.mark.asyncio
async def test_wait_true_reports_a_failed_warm(monkeypatch):
    async def _warm(cfg, *, model_id, messages, tools=None, **_shape):
        return {"ok": False, "message": "model not loaded: model-1"}

    route = _wait_route(monkeypatch, _warm)
    assert await route.warm_session_cache("sess-1", token="", wait=True) == {
        "ok": False, "message": "model not loaded: model-1",
    }


@pytest.mark.asyncio
async def test_wait_true_reports_a_warm_that_raises(monkeypatch):
    async def _warm(cfg, *, model_id, messages, tools=None, **_shape):
        raise RuntimeError("provider exploded")

    route = _wait_route(monkeypatch, _warm)
    assert await route.warm_session_cache("sess-1", token="", wait=True) == {
        "ok": False, "message": "provider exploded",
    }


@pytest.mark.asyncio
async def test_a_cancelled_wait_leaves_the_warm_running_and_registered(monkeypatch):
    from infra import state

    release = asyncio.Event()

    async def _warm(cfg, *, model_id, messages, tools=None, **_shape):
        await release.wait()
        return {"ok": True}

    route = _wait_route(monkeypatch, _warm)
    request = asyncio.create_task(route.warm_session_cache("sess-1", token="", wait=True))
    async with asyncio.timeout(5):  # registration time depends on host load; wait for the fact
        while "sess-1" not in state._active_warm_tasks:
            await asyncio.sleep(0.01)
    task = state._active_warm_tasks["sess-1"]
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert not task.done() and not task.cancelling()
    assert state._active_warm_tasks["sess-1"] is task
    release.set()
    assert (await task) == {"ok": True}


@pytest.mark.asyncio
async def test_wait_true_joins_an_identical_warm_already_in_flight(monkeypatch):
    from infra import state

    release = asyncio.Event()
    calls = 0

    async def _warm(cfg, *, model_id, messages, tools=None, **_shape):
        nonlocal calls
        calls += 1
        await release.wait()
        return {"ok": True}

    route = _wait_route(monkeypatch, _warm)
    first = await route.warm_session_cache("sess-1", token="")
    assert first["message"] == "Warm request submitted"
    task = state._active_warm_tasks["sess-1"]
    joined = asyncio.create_task(route.warm_session_cache("sess-1", token="", wait=True))
    await asyncio.sleep(0.05)
    assert not joined.done()
    assert state._active_warm_tasks["sess-1"] is task
    release.set()
    assert await asyncio.wait_for(joined, 1) == {"ok": True, "message": "Warm finished"}
    assert calls == 1
