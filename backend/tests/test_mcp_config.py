"""Configuration failures cannot expose credentials or broaden an allowlist."""

import json
from types import SimpleNamespace

import pytest

from tools.mcp_config import MCPConfigError, allowed_names, http_options, read_bearer_file


@pytest.mark.parametrize("value", [None, "read_profile", [None], [""]])
def test_malformed_allowlist_fails_closed(value):
    with pytest.raises(MCPConfigError):
        allowed_names({"allowed_tools": value})


def test_allowlist_alias_and_disagreement():
    assert allowed_names({"tool_allowlist": ["read_profile"]}) == {"read_profile"}
    assert allowed_names({"allowed_tools": []}) == set()
    with pytest.raises(MCPConfigError):
        allowed_names({"allowed_tools": [], "tool_allowlist": ["write_record"]})


@pytest.mark.parametrize("value", ["", "token\nsecond", "token\rsecond", "token token", "x" * 8193])
def test_malformed_bearer_file_is_redacted(tmp_path, value):
    bearer = tmp_path / "bearer"
    bearer.write_text(value)
    bearer.chmod(0o600)
    with pytest.raises(MCPConfigError, match="one nonempty bearer token"):
        read_bearer_file(str(bearer))


def test_symlink_and_missing_file_are_refused(tmp_path):
    bearer = tmp_path / "bearer"
    bearer.write_text("synthetic-secret")
    bearer.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(bearer)
    for path in [link, tmp_path / "missing", tmp_path]:
        with pytest.raises(MCPConfigError):
            read_bearer_file(str(path))


def test_cli_configure_refuses_the_retired_settings_writer(tmp_path, isolated_db, capsys):
    from cli_mcp import configure_mcp
    from infra.settings import get_tool_config, set_setting

    path = tmp_path / "servers.json"
    path.write_text(json.dumps({"service": {"command": "fixture"}}))
    set_setting("tools", {"web": {"unchanged": True}, "mcpServers": {"old": {}}})

    assert configure_mcp(SimpleNamespace(file=path)) == 2
    assert get_tool_config() == {"web": {"unchanged": True}, "mcpServers": {"old": {}}}
    assert "add the app in Connected apps" in capsys.readouterr().err


@pytest.mark.parametrize("header", ["Authorization", "authorization", "Proxy-Authorization", "Cookie", "X-API-Key"])
def test_literal_credentials_never_persist_or_reach_http(tmp_path, isolated_db, capsys, header):
    from cli_mcp import configure_mcp
    from infra.settings import get_tool_config, set_setting

    config = {"url": "http://127.0.0.1:1234/mcp", "headers": {header: "synthetic-private-value"}}
    with pytest.raises(MCPConfigError, match="literal credential headers"):
        http_options(config)
    set_setting("tools", {"mcpServers": {"unchanged": {"command": "fixture"}}})
    original = get_tool_config()
    path = tmp_path / "servers.json"
    path.write_text(json.dumps({"private": config}))
    assert configure_mcp(SimpleNamespace(file=path)) == 2
    assert get_tool_config() == original
    captured = capsys.readouterr()
    assert "synthetic-private-value" not in captured.out + captured.err


def test_non_secret_literal_headers_remain_supported():
    headers = {"X-Organization": "example", "Accept": "application/json"}
    options, auth = http_options({"url": "http://127.0.0.1:1234/mcp", "headers": headers})
    assert options["headers"] == headers and auth is None
