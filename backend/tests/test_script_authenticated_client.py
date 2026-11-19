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


def _no_host_login():
    raise ValueError("the person's GitHub credential is unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize("view", [
    AgentAccountView("needs_auth"),
    AgentAccountView("needs_auth", "read_failed"),
    AgentAccountView("not_installed", "github_executable_missing"),  # installed but broken, not absent
    AgentAccountView("not_installed", absent=True),  # no agents' account and no host gh login
], ids=["needs_auth", "read_failed", "executable_missing", "absent_without_host_login"])
async def test_unavailable_account_has_no_github_config(tmp_path, monkeypatch, view):
    @asynccontextmanager
    async def fake_channel(*args, **kwargs):
        yield

    @asynccontextmanager
    async def no_proxy(*args, **kwargs):
        raise AssertionError("no forwarder may start without a login")
        yield  # pragma: no cover

    monkeypatch.setattr(script_sandbox, "tool_channel", fake_channel)
    monkeypatch.setattr(script_sandbox, "github_proxy", no_proxy)
    monkeypatch.setattr(script_sandbox, "person_forwarder_credential", _no_host_login)
    monkeypatch.setattr(script_sandbox, "corsia_temp_root", lambda: tmp_path)
    monkeypatch.setattr(script_sandbox, "corsia_temp_root_for_bind", lambda thread_id: tmp_path)
    async with script_sandbox._authenticated_client("owner", view) as (_, _, config, _):
        assert config is None
    assert script_sandbox.github_lock_reason(view, host_login=False).startswith("scripts get no GitHub login: ")


@pytest.mark.parametrize("ready, approved, host_login, locked", [
    (True, True, False, False),  # a ready account never needs the host login
    (False, True, True, False),  # no account: reads as the host login
    (False, True, False, True),  # no account and no host login: locked with a reason
    (True, False, True, True),   # unapproved: locked whatever the account
    (False, False, True, True),
])
def test_lock_reason_by_account_approval_and_host_login(ready, approved, host_login, locked):
    view = (AgentAccountView("ready", home="/h", login="example") if ready
            else AgentAccountView("not_installed", absent=True))
    reason = script_sandbox.github_lock_reason(view, approved=approved, host_login=host_login)
    assert (reason is not None) == locked
    if locked and not approved:
        assert "authenticated=true" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["ready", "absent"])
async def test_forwarder_reads_as_the_agents_account_else_the_host_login(tmp_path, monkeypatch, state):
    from connections import github_agent
    from events.connectors.github_read_budget import budget, person_budget

    seen = {}

    @asynccontextmanager
    async def fake_channel(*args, **kwargs):
        yield

    @asynccontextmanager
    async def fake_proxy(path, credential, *, read_budget):
        seen.update(credential=credential(), budget=read_budget)
        yield

    monkeypatch.setattr(script_sandbox, "tool_channel", fake_channel)
    monkeypatch.setattr(script_sandbox, "github_proxy", fake_proxy)
    monkeypatch.setattr(script_sandbox, "corsia_temp_root", lambda: tmp_path)
    monkeypatch.setattr(script_sandbox, "corsia_temp_root_for_bind", lambda thread_id: tmp_path)
    monkeypatch.setattr(github_agent, "script_forwarder_credential", lambda view: "agents-synthetic")
    monkeypatch.setattr(script_sandbox, "person_forwarder_credential", lambda: "person-synthetic")
    view = (AgentAccountView("ready", home=str(tmp_path), login="example") if state == "ready"
            else AgentAccountView("not_installed", absent=True))
    async with script_sandbox._authenticated_client("owner", view) as (_, _, config, _):
        assert config is not None
        assert "synthetic" not in (config / "hosts.yml").read_text()
    assert seen == ({"credential": "agents-synthetic", "budget": budget} if state == "ready"
                    else {"credential": "person-synthetic", "budget": person_budget})


@pytest.mark.parametrize("returncode, stdout, expected", [
    (0, "person-synthetic\n", "person-synthetic"), (0, "\n", None), (1, "", None)])
def test_person_forwarder_credential_reads_the_host_login(monkeypatch, returncode, stdout, expected):
    import subprocess

    from connections import github_person

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    monkeypatch.setattr(github_person.subprocess, "run", fake_run)
    if expected is None:
        with pytest.raises(ValueError, match="unavailable") as raised:
            github_person.person_forwarder_credential()
        assert "synthetic" not in str(raised.value)
    else:
        assert github_person.person_forwarder_credential() == expected
    assert calls == [["gh", "auth", "token", "--hostname", "github.com"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["ready", "absent"])
async def test_forwarder_reads_as_the_agents_account_else_the_host_login(tmp_path, monkeypatch, state):
    from connections import github_agent
    from events.connectors.github_read_budget import budget, person_budget

    seen = {}

    @asynccontextmanager
    async def fake_channel(*args, **kwargs):
        yield

    @asynccontextmanager
    async def fake_proxy(path, credential, *, read_budget):
        seen.update(credential=credential(), budget=read_budget)
        yield

    monkeypatch.setattr(script_sandbox, "tool_channel", fake_channel)
    monkeypatch.setattr(script_sandbox, "github_proxy", fake_proxy)
    monkeypatch.setattr(script_sandbox, "corsia_temp_root", lambda: tmp_path)
    monkeypatch.setattr(script_sandbox, "corsia_temp_root_for_bind", lambda thread_id: tmp_path)
    monkeypatch.setattr(github_agent, "script_forwarder_credential", lambda view: "agents-synthetic")
    monkeypatch.setattr(script_sandbox, "person_forwarder_credential", lambda: "person-synthetic")
    view = (AgentAccountView("ready", home=str(tmp_path), login="example") if state == "ready"
            else AgentAccountView("not_installed", absent=True))
    async with script_sandbox._authenticated_client("owner", view) as (_, _, config, _):
        assert config is not None
        assert "synthetic" not in (config / "hosts.yml").read_text()
    assert seen == ({"credential": "agents-synthetic", "budget": budget} if state == "ready"
                    else {"credential": "person-synthetic", "budget": person_budget})


@pytest.mark.parametrize("returncode, stdout, expected", [
    (0, "person-synthetic\n", "person-synthetic"), (0, "\n", None), (1, "", None)])
def test_person_forwarder_credential_reads_the_host_login(monkeypatch, returncode, stdout, expected):
    import subprocess

    from connections import github_person

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    monkeypatch.setattr(github_person.subprocess, "run", fake_run)
    if expected is None:
        with pytest.raises(ValueError, match="unavailable") as raised:
            github_person.person_forwarder_credential()
        assert "synthetic" not in str(raised.value)
    else:
        assert github_person.person_forwarder_credential() == expected
    assert calls == [["gh", "auth", "token", "--hostname", "github.com"]]


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
