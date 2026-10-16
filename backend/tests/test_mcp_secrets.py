"""MCP credential references are private, named deterministically, and resolved at launch."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from infra import key_store
from infra.database import get_db
from tools import mcp_secrets
from tools.mcp import _StoredHeadersAuth
from tools.mcp_config import MCPConfigError


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    return root


def test_entry_names_fit_the_store_rule_for_any_input():
    names = ["a", "x" * 300, "GitHub MCP", "日本語", "..", "a-b", "-"]
    for server in names:
        for variable in names:
            for field in ("env", "headers"):
                entry = mcp_secrets.entry_name(server, field, variable)
                assert key_store.NAME_PATTERN.match(entry)
                assert entry.startswith("mcp-")
                assert key_store.is_managed_name(entry)


def test_entry_names_are_deterministic_and_keep_kinds_separate():
    assert mcp_secrets.entry_name("a-b", "env", "c") != mcp_secrets.entry_name("a", "env", "b-c")
    assert mcp_secrets.entry_name("svc", "env", "TOKEN") != mcp_secrets.entry_name("svc", "headers", "TOKEN")
    assert mcp_secrets.entry_name("svc", "env", "TOKEN") == mcp_secrets.entry_name("svc", "env", "TOKEN")


def test_a_name_that_only_starts_with_mcp_is_not_claimed_as_code_owned():
    assert key_store.is_managed_name("mcp-docs-token-0123456789")
    for name in ("mcp-", "mcp-docs", "mcp-docs-token", "mcp-docs-token-012345678", "mcp-docs-token-0123456789a"):
        assert not key_store.is_managed_name(name), name


CONFIG = {
    "command": "fixture",
    "env": {"SERVICE_TOKEN": "value-one", "API_KEY": "value-two", "LOG_LEVEL": "debug"},
}


def test_credential_named_env_values_move_and_the_rest_stay():
    clean, lifted = mcp_secrets.split_secrets("svc", CONFIG)

    assert clean["env"] == {"LOG_LEVEL": "debug"}
    assert clean["secret_env"] == ["SERVICE_TOKEN", "API_KEY"]
    assert sorted(lifted.values()) == ["value-one", "value-two"]
    assert "value-one" not in json.dumps(clean)
    assert CONFIG["env"]["SERVICE_TOKEN"] == "value-one"


def test_plain_write_does_not_lift_headers_but_the_migration_shape_can():
    config = {"url": "http://127.0.0.1:1/mcp", "headers": {"X-API-Key": "value-one", "X-Org": "acme"}}

    clean, lifted = mcp_secrets.split_secrets("svc", config)
    migrated, migrated_values = mcp_secrets.split_secrets("svc", config, headers=True)

    assert clean == config and lifted == {}
    assert migrated["headers"] == {"X-Org": "acme"}
    assert migrated["secret_headers"] == ["X-API-Key"]
    assert migrated_values


@pytest.mark.parametrize("bad", [
    {"secret_env": "TOKEN"},
    {"secret_env": ["TOKEN", "TOKEN"]},
    {"secret_env": ["TOKEN"], "env": {"TOKEN": "value-one"}},
    {"secret_headers": ["Authorization"]},
    {"secret_headers": ["cookie"]},
])
def test_malformed_references_are_refused(bad):
    with pytest.raises(MCPConfigError):
        mcp_secrets.check_references({"command": "fixture", **bad})


def test_resolve_returns_stored_values_and_names_missing_variables_without_values(store):
    clean, lifted = mcp_secrets.split_secrets("svc", CONFIG)
    for entry, value in lifted.items():
        key_store.set_managed_value(entry, value)

    env, headers = mcp_secrets.resolve("svc", clean)
    assert env == {"SERVICE_TOKEN": "value-one", "API_KEY": "value-two"}
    assert headers == {}

    key_store.drop_managed_entry(mcp_secrets.entry_name("svc", "env", "API_KEY"))
    with pytest.raises(MCPConfigError) as error:
        mcp_secrets.resolve("svc", clean)
    assert "API_KEY" in str(error.value)
    assert "value-two" not in str(error.value)


def test_managed_header_key_reaches_auth_only_after_key_store_resolution(store):
    clean, lifted = mcp_secrets.split_secrets(
        "svc", {"url": "http://127.0.0.1:1/mcp", "headers": {"X-API-Key": "value-one"}}, headers=True,
    )
    for entry, value in lifted.items():
        key_store.set_managed_value(entry, value)

    _env, headers = mcp_secrets.resolve("svc", clean)
    request = next(_StoredHeadersAuth(headers).auth_flow(httpx.Request("POST", clean["url"])))

    assert request.headers["X-API-Key"] == "value-one"
    assert "value-one" not in json.dumps(clean)


def test_missing_managed_header_key_fails_before_transport(store):
    clean = {"url": "http://127.0.0.1:1/mcp", "secret_headers": ["X-API-Key"]}

    with pytest.raises(MCPConfigError, match="credential value missing"):
        mcp_secrets.resolve("svc", clean)


def test_legacy_settings_writers_are_refused():
    from cli_mcp import configure_mcp
    from tools.mcp_import import save_import
    from types import SimpleNamespace

    with pytest.raises(MCPConfigError, match="add the app in Connected apps"):
        save_import({"svc": dict(CONFIG)})
    assert configure_mcp(SimpleNamespace(file="unused")) == 2


def test_a_harness_is_never_handed_connected_app_config(isolated_db, store):
    from connections.app_lifecycle import create_app
    from infra.database import get_db
    from subagents.harness_tool_bridge import write_cc_mcp_config

    db = get_db()
    try:
        create_app(
            "Private app", {"command": "fixture", "key_refs": [{"kind": "env", "name": "SERVICE_TOKEN"}]},
            {"mode": "all", "project_ids": [], "outside_projects": False}, db=db,
        )
    finally:
        db.close()
    path = Path(write_cc_mcp_config(thread_id="thread-1", tool_token="test-harness-token"))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    finally:
        path.unlink()

    assert list(payload["mcpServers"]) == ["corsia"]
    assert "Private app" not in json.dumps(payload)


def test_reference_entries_are_not_arbitrary_key_store_names(store):
    key_store.set_managed_value("provider-anthropic", "value-one")
    config = {"command": "fixture", "secret_env": ["provider-anthropic"]}

    with pytest.raises(MCPConfigError, match="missing from the key store"):
        mcp_secrets.resolve("server", config)

    names = [row[0] for row in get_db().execute("SELECT name FROM key_store_entries")]
    assert "provider-anthropic" in names
