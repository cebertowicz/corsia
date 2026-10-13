"""Harness-wide provider enable state uses the connection as its sole owner."""

from __future__ import annotations

import asyncio


def _discover(provider: str) -> None:
    from connections.harness_models import store_success

    store_success(
        provider,
        [{
            "selector_id": "sonnet",
            "display_name": "Sonnet",
            "description": "",
            "available": True,
            "resolved_identity": "claude-sonnet-5",
            "capabilities": {},
            "context_window": 262_144,
            "is_default": False,
        }],
        harness_version="test",
        source="test",
        timestamp="2026-10-13T00:00:00+00:00",
    )


def _available_connection(provider: str) -> None:
    from connections.models import ConnectionStatus, ConnectionType, upsert_connection

    upsert_connection(
        type=ConnectionType.CLI,
        provider=provider,
        status=ConnectionStatus.AVAILABLE,
        config={"binary": f"/bin/{provider}"},
    )


def test_disabling_harness_hides_catalog_refuses_spawn_and_holds_admission(isolated_db, monkeypatch):
    from connections.models import set_harness_enabled
    from infra.send_target import classify_target_admission
    from llm import delegation_model
    from llm.model_status import get_model_status_rows
    from llm.resolution import resolve_execution_target

    _available_connection("claude-code")
    _discover("claude-code")
    set_harness_enabled("claude-code", False)

    assert "claude-code/sonnet" not in {row["id"] for row in get_model_status_rows()}
    target = resolve_execution_target("claude-code/sonnet")
    assert (target.available, target.availability_reason) == (False, "provider_disabled")
    assert classify_target_admission(target) == "wait"

    monkeypatch.setattr(delegation_model, "_scoped_rows", lambda *_args: ([], None))
    selector, error = delegation_model.eligible_delegation_model("claude-code/sonnet")
    assert selector is None
    assert error == (
        "Claude Code is turned off in Settings > Providers. Turn it on before "
        "spawning a subagent on one of its models."
    )


def test_reenabling_harness_restores_its_existing_dispatch_path(isolated_db):
    from connections.models import set_harness_enabled
    from infra.send_target import classify_target_admission
    from llm.model_status import get_model_status_rows
    from llm.resolution import resolve_execution_target

    _available_connection("claude-code")
    _discover("claude-code")
    set_harness_enabled("claude-code", False)
    set_harness_enabled("claude-code", True)

    assert "claude-code/sonnet" in {row["id"] for row in get_model_status_rows()}
    target = resolve_execution_target("claude-code/sonnet")
    assert target.available is True
    assert classify_target_admission(target) == "run"


async def test_disabled_harness_source_check_launches_no_cli_process(isolated_db, monkeypatch):
    from connections.models import set_harness_enabled
    from connections.source_state import check_harness_source

    _available_connection("codex")
    set_harness_enabled("codex", False)
    launched = False

    def unexpected_launch(*_args, **_kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("disabled harness launched a process")

    monkeypatch.setattr("connections._codex_sign_in_state", unexpected_launch)
    await check_harness_source("codex")

    assert launched is False


def test_disabled_claude_stays_disabled_when_catalog_refresh_records_ready(isolated_db, monkeypatch):
    from connections import claude_discovery
    from connections.models import ConnectionStatus, get_connection, set_harness_enabled
    from connections.vendor_accounts import default_account

    _available_connection("claude-code")
    account = default_account("claude-code")
    set_harness_enabled("claude-code", False)
    monkeypatch.setattr("connections.harness_usage.record_usage", lambda **_kwargs: None)
    monkeypatch.setattr("connections.claude_usage.apply_usage_response", lambda *_args, **_kwargs: None)

    claude_discovery._store_catalog_success({}, account_id=account["id"], receipt=None)

    row = get_connection("claude-code")
    assert row["status"] == ConnectionStatus.DISABLED.value
    assert row["config"]["source_check"]["state"] == "ready"


def test_disabled_codex_stays_disabled_when_recheck_records_ready(isolated_db, monkeypatch):
    from connections.models import ConnectionStatus, get_connection, set_harness_enabled
    from connections.source_state import check_harness_source

    _available_connection("codex")
    set_harness_enabled("codex", False)
    monkeypatch.setattr("connections.resolve_builtin_binary", lambda _provider: ({}, "/bin/codex"))
    monkeypatch.setattr("connections._codex_sign_in_state", lambda _binary: "ready")

    asyncio.run(check_harness_source("codex", rediscover=True))

    row = get_connection("codex")
    assert row["status"] == ConnectionStatus.DISABLED.value
    assert row["config"]["source_check"]["state"] == "ready"


def test_reenabling_needs_auth_harness_keeps_held_input_held(isolated_db, monkeypatch):
    from connections.models import ConnectionStatus, get_connection, set_harness_enabled
    from connections.source_state import NEEDS_AUTH, apply_check

    _available_connection("codex")
    monkeypatch.setattr("connections.harness_legacy_fallback.sync_legacy_fallback", lambda *_args, **_kwargs: None)
    apply_check("codex", NEEDS_AUTH)
    set_harness_enabled("codex", False)
    released = []
    monkeypatch.setattr(
        "infra.connection_limit_release.release_after_sign_in",
        lambda provider, **_kwargs: released.append(provider),
    )

    set_harness_enabled("codex", True)

    assert get_connection("codex")["status"] == ConnectionStatus.NEEDS_AUTH.value
    assert released == []


def test_reenabling_ready_harness_releases_held_input(isolated_db, monkeypatch):
    from connections.models import ConnectionStatus, get_connection, set_harness_enabled
    from connections.source_state import READY, apply_check

    _available_connection("codex")
    apply_check("codex", READY)
    set_harness_enabled("codex", False)
    released = []
    monkeypatch.setattr(
        "infra.connection_limit_release.release_after_sign_in",
        lambda provider, **_kwargs: released.append(provider),
    )

    set_harness_enabled("codex", True)

    assert get_connection("codex")["status"] == ConnectionStatus.AVAILABLE.value
    assert released == ["codex"]
