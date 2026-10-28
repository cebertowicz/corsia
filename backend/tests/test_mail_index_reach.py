"""Account capability discovery follows the canonical account reach rules."""
from __future__ import annotations

import json
import sqlite3
import logging

import pytest

from connections.connected_accounts import (
    GOOGLE_PROVIDER, account_reaches_thread, capabilities_reaching_thread, create_account,
)
from tools.connected_app_visibility import account_capabilities_for_thread
from infra.corsia_agent import set_corsia_agent
from infra.database import get_db
from infra.time import utc_now_iso
from tests.registry_helpers import make_session
from tools.registry import ToolRegistry
from tools.tool_surface import pack_index


def test_index_tracks_reach_and_fails_closed(isolated_db, monkeypatch):
    db = get_db()
    now = utc_now_iso()
    db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES (?,?,?,?)", ("p", "P", now, now))
    db.commit()
    project = make_session(db, project_id="p", prefix="project")
    outside = make_session(db, prefix="outside")
    thread = outside
    try:
        registry = ToolRegistry()

        def index():
            return pack_index(registry, account_capabilities_for_thread(thread))
        before = index()
        assert before == index()
        assert "- google_connect:" in before
        assert "- email_search:" not in before
        account = create_account(GOOGLE_PROVIDER, "a@example.test", reach={"mode": "selected", "project_ids": ["p"], "outside_projects": False}, created_by=("system", "test"), db=db)
        assert index() == before
        thread = project
        with_account = index()
        assert "- email_search:" in with_account
        assert "- microsoft_files_list:" in with_account
        assert with_account == index()
        db.execute("UPDATE connected_accounts SET state='signed_out' WHERE id=?", (account["id"],))
        db.commit()
        assert index() == with_account
        with monkeypatch.context() as patch:
            patch.setattr("connections.connected_accounts.capabilities_reaching_thread", lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("read failed")))
            assert index() == before
        db.execute("DELETE FROM connected_accounts WHERE id=?", (account["id"],))
        db.commit()
        assert index() == before
        monkeypatch.setattr("infra.database.get_db", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("unavailable")))
        assert index() == before
    finally:
        db.close()


def test_programming_error_is_not_treated_as_store_failure(isolated_db, monkeypatch):
    def fail(*args, **kwargs):
        raise TypeError("programming error")

    monkeypatch.setattr("connections.connected_accounts.capabilities_reaching_thread", fail)
    with pytest.raises(TypeError, match="programming error"):
        account_capabilities_for_thread("thread")


def test_malformed_account_does_not_hide_healthy_account(isolated_db, caplog):
    db = get_db()
    try:
        thread = make_session(db, prefix="mail")
        reach = {"mode": "all", "project_ids": [], "outside_projects": False}
        bad = create_account(GOOGLE_PROVIDER, "bad@example.test", reach=reach, created_by=("system", "test"), db=db)
        create_account(GOOGLE_PROVIDER, "good@example.test", reach=reach, created_by=("system", "test"), db=db)
        db.execute("UPDATE connected_accounts SET capabilities_json=? WHERE id=?", ('{"secret":"bad"}', bad["id"]))
        db.commit()
        with caplog.at_level(logging.WARNING):
            capabilities = capabilities_reaching_thread(thread, db=db)
        assert "mail" in capabilities
        assert "Skipping malformed connected account capabilities" in caplog.text
        assert "secret" not in caplog.text
    finally:
        db.close()


@pytest.mark.asyncio
async def test_loading_loader_by_name_returns_static_header():
    from tools.catalog import advertised_tool_definitions
    from tools.tool_surface import ToolsLoadTool

    registry = ToolRegistry()
    loader = ToolsLoadTool(registry)
    registry.register(loader)
    loaded = await loader.execute(names=["tools_load"])
    schema = json.loads(loaded.split("\n", 1)[1])[0]
    advertised = advertised_tool_definitions(registry, frozenset({"mail"}))[0]["function"]
    assert schema["description"] == loader.description
    assert "- email_search:" not in schema["description"]
    assert "- email_search:" in advertised["description"]
    assert schema["parameters"] == advertised["parameters"]


@pytest.mark.asyncio
async def test_native_warm_cc_and_codex_prepare_identical_account_index(isolated_db, monkeypatch):
    """Compare all four paths with no reach-limited MCP app in the registry."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from api import thread_cache_warm
    from core import turn_iteration
    from infra import state
    from subagents import cc_spawn_prepare, codex_runtime, codex_runtime_spawn, harness_tool_bridge
    from tools.tool_surface import ToolsLoadTool

    db = get_db()
    now = utc_now_iso()
    db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES (?,?,?,?)", ("p", "P", now, now))
    db.commit()
    thread = make_session(db, project_id="p", prefix="account")
    other = make_session(db, prefix="other")
    create_account(GOOGLE_PROVIDER, "a@example.test", reach={"mode": "selected", "project_ids": ["p"], "outside_projects": False}, created_by=("system", "test"), db=db)
    db.close()

    registry = ToolRegistry()
    registry.register(ToolsLoadTool(registry))
    agent = SimpleNamespace(tools=registry, _connect_mcp=AsyncMock())
    target = SimpleNamespace(catalog_id="test-model", preserve_thinking_default=False,
                             context_window=1000, discovered_context_window=None,
                             expected_reported_model=None, harness="claude-code")
    monkeypatch.setattr(turn_iteration, "prepare_turn_context", AsyncMock(return_value=([{"role": "user", "content": "hi"}], {})))
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(thread_cache_warm, "_warm_replay_candidates", lambda *_: (None, None, None))
    monkeypatch.setattr(thread_cache_warm, "_render_warm_replay", AsyncMock(return_value=([], {"history_messages": 0, "history_tokens": 0, "was_trimmed": False})))
    monkeypatch.setattr("llm.providers.omlx.make_prompt_extra_body", lambda *args, **kwargs: {})
    monkeypatch.setattr("llm.reasoning_config.reasoning_config_for_model", lambda *_: None)

    snapshot = SimpleNamespace(to_dict=lambda: {})
    reconstruction = SimpleNamespace(seed_text="seed", persisted_seed_text="seed", installed=None,
                                     snapshot_id="snapshot", snapshot=snapshot)
    monkeypatch.setattr(cc_spawn_prepare, "build_cc_context_reconstruction", lambda **kwargs: reconstruction)
    monkeypatch.setattr(cc_spawn_prepare, "_build_seed", lambda *_: "seed")
    monkeypatch.setattr(cc_spawn_prepare, "_write_seed_file", lambda *_: "seed.txt")
    monkeypatch.setattr(cc_spawn_prepare, "_persist_seed", lambda *_: None)
    monkeypatch.setattr(cc_spawn_prepare, "_source_artifact", lambda **kwargs: {})
    monkeypatch.setattr("subagents.cc_sessions.resolve_claude_dir", lambda path: path)
    request = SimpleNamespace(target=target, project_id="p", claude_dir="test-dir", parent_thread_id=other)

    class Captured(Exception):
        pass

    captured = []
    actual_resolve = codex_runtime_spawn.resolve_app_server_tools
    def capture(*args, **kwargs):
        captured.append(actual_resolve(*args, **kwargs)[1])
        raise Captured

    monkeypatch.setattr(codex_runtime, "_require_owned_accept_db", lambda *_: None)
    monkeypatch.setattr(codex_runtime, "_reusable_app_session", lambda *_: None)
    monkeypatch.setattr(codex_runtime_spawn, "resolve_app_server_tools", capture)
    monkeypatch.setattr(harness_tool_bridge, "build_tool_registry", lambda *_: registry)
    spec = SimpleNamespace(db=None, broadcast=None, thread_id=thread, working_dir=None,
                           tool_registry=None, dynamic_tools=None)
    token = state._current_thread_id.set(other)
    try:
        native = await turn_iteration._prepare_iteration_state(agent, thread, "hi", "test-model", False, "p", "web", thread, None, 1000, None)
        _, warm, _ = await thread_cache_warm._build_warm_prefix(thread, "p", target, None)
        live, _ = await cc_spawn_prepare._build_live(request, None, child_id=thread, name="test", working_dir=None,
                                                     cc_alias="test", tool_registry=registry, runtime_context_epoch=0)
        with pytest.raises(Captured):
            await codex_runtime._run_app_server_turn(spec)
    finally:
        state._current_thread_id.reset(token)

    def index(definitions):
        return next(d["function"]["description"] for d in definitions if d["function"]["name"] == "tools_load")

    indexes = [index(native.tool_definitions), index(warm), index(live.tool_definitions), index(captured[0])]
    assert len(set(indexes)) == 1
    assert "- email_search:" in indexes[0]


def test_bulk_reach_matches_individual_checks_and_query_count(isolated_db):
    db = get_db()
    now = utc_now_iso()
    for project in ("a", "b", "gone"):
        db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES (?,?,?,?)", (project, project, now, now))
    db.commit()
    a = make_session(db, project_id="a", prefix="a")
    b = make_session(db, project_id="b", prefix="b")
    outside = make_session(db, prefix="outside")
    gone = make_session(db, project_id="gone", prefix="gone")
    db.execute("UPDATE threads SET parent_thread_id=? WHERE id=?", (a, b))
    specs = [
        {"mode": "all", "project_ids": [], "outside_projects": False},
        {"mode": "selected", "project_ids": ["a"], "outside_projects": False},
        {"mode": "selected", "project_ids": [], "outside_projects": True},
        {"mode": "selected", "project_ids": [], "outside_projects": False},
    ]
    accounts = [create_account(GOOGLE_PROVIDER, f"{i}@example.test", reach=reach, created_by=("system", "test"), db=db) for i, reach in enumerate(specs)]
    for account, capability in zip(accounts, ("mail", "calendar", "outside", "files")):
        account["capabilities"] = [capability]
        db.execute("UPDATE connected_accounts SET capabilities_json=? WHERE id=?", (json.dumps([capability]), account["id"]))
    db.execute("UPDATE connected_accounts SET reach_mode='corsia' WHERE id=?", (accounts[3]["id"],))
    assert set_corsia_agent(outside, db=db)
    db.execute("DELETE FROM projects WHERE id='gone'")
    db.commit()
    try:
        for thread in (a, b, outside, gone, "missing"):
            expected = set()
            for account in accounts:
                if account_reaches_thread(str(account["id"]), thread, db=db):
                    expected.update(account["capabilities"])
            assert capabilities_reaching_thread(thread, db=db) == expected
        statements = []
        db.set_trace_callback(statements.append)
        capabilities_reaching_thread(a, db=db)
        first_count = len(statements)
        db.set_trace_callback(None)
        for i in range(4, 50):
            create_account(GOOGLE_PROVIDER, f"{i}@example.test", reach=specs[i % len(specs)], created_by=("system", "test"), db=db)
        statements.clear()
        db.set_trace_callback(statements.append)
        capabilities_reaching_thread(a, db=db)
        db.set_trace_callback(None)
        assert len(statements) == first_count
        assert pack_index(ToolRegistry(), capabilities_reaching_thread(a, db=db))
    finally:
        db.set_trace_callback(None)
        db.close()
