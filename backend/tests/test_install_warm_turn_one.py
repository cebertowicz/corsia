"""The install warm covers exactly what the conductor's first turn sends first."""

from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

from unittest.mock import AsyncMock, MagicMock

import pytest

SECRET_ENOUGH = "long-enough-passphrase"
STARTER = "omlx@srv-1/model-1"
OTHER = "omlx@srv-1/model-2"
WIRE_TOKEN = "t"


def _write(sql: str, *args) -> None:
    from infra.database import write_transaction

    with write_transaction(None, scope="test_install_warm") as db:
        db.execute(sql, args)


def _seed_omlx(*model_ids: str) -> None:
    for model_id in model_ids:
        provider_model = model_id.partition("/")[2]
        _write(
            "INSERT OR REPLACE INTO models (id,harness,provider,provider_model_id,"
            "litellm_model,display_name,backend,source,enabled,available,"
            "context_window,metadata_json,created_at,updated_at) VALUES "
            "(?,'native','omlx',?,?,?,'network','test',1,1,200000,'{}','t','t')",
            model_id, provider_model, model_id, provider_model,
        )
        _write(
            "INSERT OR REPLACE INTO model_profiles (model_id, context_window) "
            "VALUES (?, 200000)", model_id,
        )


@pytest.fixture
def fresh_install(isolated_db):
    from infra import accounts
    from infra.conductor import WORKSPACE_SCOPE, get_scope_coordinator
    from infra.database import write_transaction

    with write_transaction(None, scope="test_install_warm") as db:
        db.execute("DELETE FROM auth_tokens")
        db.execute("DELETE FROM users")
    accounts.create_user("newcomer", SECRET_ENOUGH)
    conductor_id = get_corsia_agent()
    assert conductor_id
    return conductor_id


async def _until(condition, seconds: float = 10.0) -> None:
    import asyncio

    for _ in range(int(seconds / 0.05)):
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out waiting for the first turn's request")


@pytest.fixture
def omlx_models(fresh_install):
    _seed_omlx(STARTER, OTHER)


@pytest.fixture
def wire(monkeypatch):
    """Fake the oMLX provider: record the warm call and the turn's request."""
    import infra.state as state
    import llm.base as base
    import llm.resolution as resolution
    from llm.router import LLMResponse

    recorded = {"warm": [], "probe": [], "turn": []}
    provider = MagicMock()

    async def _probe(cfg, **kwargs):
        recorded["probe"].append(kwargs)
        return {"ok": False}

    async def _warm(cfg, **kwargs):
        recorded["warm"].append(kwargs)
        return {"ok": True}

    provider.probe_cache, provider.warm_cache = _probe, _warm
    monkeypatch.setattr("llm.providers.for_provider", lambda _name: provider)

    async def _completion(kwargs, **_rest):
        recorded["turn"].append(kwargs)
        return LLMResponse(content="hello", finish_reason="stop")

    async def _healthy(_model):
        return True, ""

    monkeypatch.setattr(base, "_completion_with_retries", _completion)
    monkeypatch.setattr(base, "_omlx_loaded_only_preflight", AsyncMock(return_value=None))
    monkeypatch.setattr(resolution, "_check_local_model_health", _healthy)
    monkeypatch.setattr(state, "broadcast_to_thread", MagicMock())
    monkeypatch.setattr(
        "llm.network_endpoints.build_api_base", lambda _server: "http://127.0.0.1:9/v1",
    )
    return recorded


async def _first_turn(conductor: str, model: str, wire) -> dict:
    """Pick ``model`` and return the request its first turn put on the wire."""
    from thread.model import switch_model

    wire["turn"].clear()
    outcome = await switch_model(
        conductor, model, user_id="newcomer", actor_kind="user",
        actor_id="newcomer", reason="harness_model_switch",
    )
    assert outcome.changed
    await _until(lambda: wire["turn"])
    return wire["turn"][0]


async def _warm(conductor: str, model: str, wire, monkeypatch) -> dict:
    from api import thread_cache_warm as api_runtime
    from infra import state

    monkeypatch.setattr(api_runtime, "authenticate", lambda _t: {"id": "newcomer"})
    response = await api_runtime.warm_session_cache(conductor, token=WIRE_TOKEN, model=model)
    assert response["ok"] is True, response
    await state._active_warm_tasks[conductor]
    return wire["warm"][-1]


def _shaping(extra: dict) -> dict:
    return {key: value for key, value in extra.items() if key != "session_id"}


@pytest.mark.asyncio
async def test_warmed_prefix_is_what_turn_one_sends(
    fresh_install, omlx_models, wire, monkeypatch,
):
    import re

    from loguru import logger

    sizes: list[str] = []
    sink = logger.add(lambda m: sizes.append(m.record["message"]), level="INFO")
    try:
        warm = await _warm(fresh_install, STARTER, wire, monkeypatch)
        turn = await _first_turn(fresh_install, STARTER, wire)
    finally:
        logger.remove(sink)

    # The warm is a strict prefix of turn 1: same system message, same tools,
    # and every prompt-shaping field the provider sees is equal.
    assert turn["messages"][: len(warm["messages"])] == warm["messages"]
    assert len(turn["messages"]) == len(warm["messages"]) + 1
    assert turn["messages"][-1]["role"] == "user"
    assert turn["tools"] == warm["tools"]
    assert _shaping(turn["extra_body"]) == {
        key: value for key, value in warm.items() if key not in ("model_id", "messages", "tools")
    }

    # Measurement, not a gate: how much of turn 1 the warm covers.
    [line] = [m for m in sizes if m.startswith("prepare_turn_context: system=")]
    system, tools, user = (
        int(re.search(rf"{name}=(\d+)", line).group(1)) for name in ("system", "tools", "user")
    )
    logger.info(
        "install warm measurement: system_tokens + tool_def_tokens = {} of {} turn-1 tokens "
        "({:.0%}); wake and digest = {}", system + tools, system + tools + user,
        (system + tools) / (system + tools + user), user,
    )


@pytest.mark.asyncio
async def test_a_warm_of_another_model_leaves_turn_one_untouched(
    fresh_install, omlx_models, wire, monkeypatch,
):
    import asyncio
    import shutil

    from infra import database, state

    db_path = database.DB_PATH
    snapshot = db_path.with_name("before-warm.db")
    await asyncio.to_thread(database.shutdown_pool, lambda: shutil.copy(db_path, snapshot))

    await _warm(fresh_install, STARTER, wire, monkeypatch)
    after_warm = await _first_turn(fresh_install, OTHER, wire)
    await asyncio.sleep(0.2)

    def _restore() -> None:
        for suffix in ("-wal", "-shm"):
            db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
        shutil.copy(snapshot, db_path)

    await asyncio.to_thread(database.shutdown_pool, _restore)
    state._active_warm_tasks.clear()
    without_warm = await _first_turn(fresh_install, OTHER, wire)

    assert after_warm["messages"] == without_warm["messages"]
    assert after_warm["tools"] == without_warm["tools"]
    assert after_warm["extra_body"] == without_warm["extra_body"]
