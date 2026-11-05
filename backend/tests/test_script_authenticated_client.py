"""Run-private client configuration contains no account credential."""
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from automation import script_sandbox
from connections.github_agent import AgentAccountView


@pytest.mark.asyncio
async def test_authenticated_client_has_only_placeholder_configuration(tmp_path, monkeypatch):
    @asynccontextmanager
    async def fake_channel(*args, **kwargs):
        yield

    monkeypatch.setattr(script_sandbox, "tool_channel", fake_channel)
    monkeypatch.setattr(script_sandbox, "github_proxy", fake_channel)
    monkeypatch.setattr(script_sandbox, "corsia_temp_root", lambda: tmp_path)
    monkeypatch.setattr(script_sandbox, "corsia_temp_root_for_bind", lambda thread_id: tmp_path)
    view = AgentAccountView("ready", home=str(tmp_path / "agent-home"), login="example")
    async with script_sandbox._authenticated_client("owner", view) as (sockets, tool, config, client):
        assert tool.parent == sockets
        assert config is not None
        content = (config / "hosts.yml").read_text() + (config / "config.yml").read_text()
        assert "placeholder" in content
        assert "synthetic-real-value" not in content
        assert (client / "corsia").is_file()


@pytest.mark.asyncio
async def test_unavailable_account_has_no_github_config(tmp_path, monkeypatch):
    @asynccontextmanager
    async def fake_channel(*args, **kwargs):
        yield

    monkeypatch.setattr(script_sandbox, "tool_channel", fake_channel)
    monkeypatch.setattr(script_sandbox, "corsia_temp_root", lambda: tmp_path)
    monkeypatch.setattr(script_sandbox, "corsia_temp_root_for_bind", lambda thread_id: tmp_path)
    async with script_sandbox._authenticated_client("owner", AgentAccountView("needs_auth")) as (_, _, config, _):
        assert config is None


@pytest.mark.parametrize("nested", [False, True])
def test_forwarder_reads_synthetic_account_credential(tmp_path, nested):
    import yaml
    from connections.github_agent import script_forwarder_credential

    host = {"users": {"example": {"oauth_token": "synthetic-value"}}} if nested else {
        "oauth_token": "synthetic-value"
    }
    (tmp_path / "hosts.yml").write_text(yaml.safe_dump({"github.com": host}))
    view = AgentAccountView("ready", home=str(tmp_path), login="example")
    assert script_forwarder_credential(view) == "synthetic-value"


def test_run_profile_grants_only_its_socket_and_hides_agent_home(tmp_path, monkeypatch):
    monkeypatch.setattr(script_sandbox, "execution_envelope_profile", lambda *args, **kwargs: "base\n")
    agent_home = tmp_path / "agents"
    own_socket = tmp_path / "one"
    other_socket = tmp_path / "two"
    view = AgentAccountView("ready", home=str(agent_home))
    profile = script_sandbox._script_profile(view, own_socket)("command", tmp_path, tmp_path / "profile")
    assert str(own_socket) in profile
    assert str(other_socket) not in profile
    assert f'(deny file-read* file-write* (subpath "{agent_home}"))' in profile
