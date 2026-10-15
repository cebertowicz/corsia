"""MCP credentials live in the key store and are resolved at each launch.

The claims: a credential-named env value is never written into MCP config by any
path; the entry name is derived, fits the store's name rule and cannot collide;
the launcher resolves the references for stdio and HTTP servers; a harness is
never handed user MCP config; removing a server drops its entries; a failed
write commits nothing and what it wrote goes in the next sweep. Every value
here is synthetic.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import textwrap
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest

from infra import key_store, key_store_values
from infra.database import get_db
from tools import mcp_import, mcp_secrets
from tools.mcp import connect_mcp_servers
from tools.mcp_config import MCPConfigError
from tools.registry import ToolRegistry

SOURCE_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    return root


def _stored() -> dict[str, str]:
    """Each entry's committed value."""
    names = [row[0] for row in get_db().execute("SELECT name FROM key_store_entries")]
    return key_store.resolve_names(names)


def _file_refs() -> set[str]:
    path = key_store.keys_dir() / key_store.VALUES_FILE
    return set(json.loads(path.read_text())) if path.exists() else set()


def _live_refs() -> set[str]:
    return {row[0] for row in get_db().execute("SELECT value_ref FROM key_store_entries WHERE value_ref IS NOT NULL")}


# --- naming ---------------------------------------------------------------


def test_entry_names_fit_the_store_rule_for_any_input():
    odd = ["a", "x" * 300, "GitHub MCP", "日本語", "..", "a-b", "-", "UPPER_case.9"]
    for server in odd:
        for var in odd:
            for field in ("env", "headers"):
                name = mcp_secrets.entry_name(server, field, var)
                assert key_store.NAME_PATTERN.match(name), name
                assert name.startswith("mcp-")
                assert key_store.is_managed_name(name)


def test_a_name_that_only_starts_with_mcp_is_not_claimed_as_code_owned():
    assert key_store.is_managed_name("mcp-docs-token-0123456789")
    assert key_store.is_managed_name("mcp-x-x-abcdef0123")
    for name in ("mcp-", "mcp-docs", "mcp-docs-token", "mcp-docs-token-012345678", "mcp-docs-token-01234567890",
                 "mcp-docs-token-0123456789a", "mcp-Docs-token-0123456789", "mcp--token-0123456789",
                 "mcp-docs-token-ABCDEF0123", "mcp-docs_token-0123456789", "mcp-" + "a" * 30 + "-0123456789",
                 "my-mcp-docs-token-0123456789"):
        assert not key_store.is_managed_name(name), name


def test_entry_names_are_deterministic_and_never_collide():
    seen: dict[str, tuple] = {}
    servers = ["a", "a-b", "a_b", "A", "b", "b-c", "x" * 40, "x" * 41, "x" * 50]
    variables = ["TOKEN", "token", "B_TOKEN", "b-token", "c", "y" * 30, "y" * 31]
    for server in servers:
        for var in variables:
            for field in ("env", "headers"):
                name = mcp_secrets.entry_name(server, field, var)
                assert name == mcp_secrets.entry_name(server, field, var)
                assert seen.setdefault(name, (server, field, var)) == (server, field, var)
    # Slug-alike pairs that a plain join would merge.
    assert mcp_secrets.entry_name("a-b", "env", "c") != mcp_secrets.entry_name("a", "env", "b-c")
    assert mcp_secrets.entry_name("s", "env", "K_TOKEN") != mcp_secrets.entry_name("s", "headers", "K_TOKEN")


def test_a_config_cannot_name_an_entry_it_does_not_own(store):
    key_store.set_managed_value("provider-anthropic", "provider-value-0001")
    config = {"command": "fixture", "secret_env": ["provider-anthropic", "mcp-other-token-0123456789"]}
    with pytest.raises(MCPConfigError, match="missing from the key store"):
        mcp_secrets.resolve("server", config)


# --- splitting and resolving ----------------------------------------------


CONFIG = {
    "command": "fixture",
    "env": {"SERVICE_TOKEN": "synthetic-token-0001", "API_KEY": "synthetic-key-0002", "LOG_LEVEL": "debug"},
}


def test_credential_named_env_values_move_and_the_rest_stay():
    clean, lifted = mcp_secrets.split_secrets("svc", CONFIG)

    assert clean["env"] == {"LOG_LEVEL": "debug"}
    assert clean["secret_env"] == ["SERVICE_TOKEN", "API_KEY"]
    assert sorted(lifted.values()) == ["synthetic-key-0002", "synthetic-token-0001"]
    assert "synthetic" not in json.dumps(clean)
    assert CONFIG["env"]["SERVICE_TOKEN"] == "synthetic-token-0001"  # the input is not mutated
    assert mcp_secrets.split_secrets("svc", clean) == (clean, {})  # splitting is idempotent


def test_an_all_credential_env_leaves_no_empty_env_block():
    clean, _ = mcp_secrets.split_secrets("svc", {"command": "fixture", "env": {"TOKEN": "synthetic-token-0001"}})
    assert "env" not in clean and clean["secret_env"] == ["TOKEN"]


def test_a_write_never_lifts_a_header_so_the_literal_refusal_stands():
    config = {"url": "http://127.0.0.1:1/mcp", "headers": {"X-API-Key": "synthetic-key-0003", "X-Org": "acme"}}
    clean, lifted = mcp_secrets.split_secrets("svc", config)
    assert clean == config and lifted == {}
    assert mcp_secrets.split_secrets("svc", config, headers=True)[1]


def test_the_migration_split_lifts_headers_but_never_the_refused_names():
    config = {"url": "http://127.0.0.1:1/mcp",
              "headers": {"X-API-Key": "synthetic-key-0003", "Authorization": "synthetic-auth-0004", "X-Org": "acme"}}
    clean, lifted = mcp_secrets.split_secrets("svc", config, headers=True)
    assert clean["headers"] == {"Authorization": "synthetic-auth-0004", "X-Org": "acme"}
    assert clean["secret_headers"] == ["X-API-Key"] and list(lifted.values()) == ["synthetic-key-0003"]


@pytest.mark.parametrize("bad", [
    {"secret_env": "TOKEN"},
    {"secret_env": ["TOKEN", "TOKEN"]},
    {"secret_env": ["TOKEN"], "env": {"TOKEN": "x"}},
    {"secret_headers": ["Authorization"]},
    {"secret_headers": ["cookie"]},
])
def test_malformed_references_are_refused(bad):
    with pytest.raises(MCPConfigError):
        mcp_secrets.check_references({"command": "fixture", **bad})


def test_resolve_returns_stored_values_and_names_a_missing_one_without_a_value(store):
    clean, lifted = mcp_secrets.split_secrets("svc", CONFIG)
    mcp_secrets.store_secrets(lifted)
    env, headers = mcp_secrets.resolve("svc", clean)
    assert env == {"SERVICE_TOKEN": "synthetic-token-0001", "API_KEY": "synthetic-key-0002"} and headers == {}

    key_store.drop_managed_entry(mcp_secrets.entry_name("svc", "env", "API_KEY"))
    with pytest.raises(MCPConfigError) as error:
        mcp_secrets.resolve("svc", clean)
    assert "API_KEY" in str(error.value) and "synthetic" not in str(error.value)


def test_an_unstorable_value_is_refused_without_echoing_it(store):
    with pytest.raises(MCPConfigError) as error:
        mcp_secrets.store_secrets(mcp_secrets.split_secrets("svc", {"command": "fixture", "env": {"TOKEN": "has space synthetic-0005"}})[1])
    assert "synthetic" not in str(error.value)
    assert _stored() == {}


# --- import and configure ------------------------------------------------


def _settings_text() -> str:
    from infra.settings import get_tool_config

    return json.dumps(get_tool_config())


def test_import_stores_the_value_in_the_key_store_and_never_in_settings(isolated_db, store):
    outcomes = mcp_import.save_import({"svc": dict(CONFIG)})

    assert outcomes == {"svc": "saved"}
    assert "synthetic" not in _settings_text()
    saved = json.loads(_settings_text())["mcpServers"]["svc"]
    assert saved["env"] == {"LOG_LEVEL": "debug"}
    env, _ = mcp_secrets.resolve("svc", saved)
    assert env["SERVICE_TOKEN"] == "synthetic-token-0001"


def test_importing_the_same_server_again_changes_nothing_and_a_different_one_conflicts(isolated_db, store):
    mcp_import.save_import({"svc": dict(CONFIG)})
    before = _stored()

    assert mcp_import.save_import({"svc": dict(CONFIG)}) == {"svc": "existing_equal"}
    changed = {**CONFIG, "env": {**CONFIG["env"], "SERVICE_TOKEN": "synthetic-token-9999"}}
    assert mcp_import.save_import({"svc": changed}) == {"svc": "conflict"}
    assert _stored() == before
    assert "synthetic" not in _settings_text()


def test_a_failed_import_commits_no_value_and_a_sweep_removes_what_it_wrote(isolated_db, store, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated settings failure")

    monkeypatch.setattr("infra.settings.set_setting", boom)
    with pytest.raises(RuntimeError):
        mcp_import.save_import({"svc": dict(CONFIG)})
    assert _stored() == {}
    assert _file_refs() and not _live_refs()
    key_store_values.sweep()
    assert _file_refs() == set()


def test_prepare_import_validates_the_config_as_saved(tmp_path):
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor/mcp.json").write_text(json.dumps({"mcpServers": {
        "remote": {"url": "https://example.test/mcp", "headers": {"X-API-Key": "synthetic-key-0003"}},
        "local": {"command": "npx", "env": {"TOKEN": "synthetic-token-0001"}},
    }}))
    additions, statuses = mcp_import.prepare_import(
        [{"source": "cursor", "name": "remote"}, {"source": "cursor", "name": "local"}], tmp_path,
    )
    assert set(additions) == {"local"}
    assert statuses[0]["status"].startswith("failed: literal credential headers")
    assert "synthetic" not in json.dumps(statuses)


def _configure(tmp_path, servers: dict) -> int:
    from cli_mcp import configure_mcp

    path = tmp_path / "servers.json"
    path.write_text(json.dumps(servers))
    return configure_mcp(SimpleNamespace(file=path))


def test_configure_stores_values_and_removing_a_server_drops_its_entries(isolated_db, store, tmp_path):
    other = {"command": "fixture", "env": {"TOKEN": "synthetic-token-0006"}}
    assert _configure(tmp_path, {"svc": CONFIG, "other": other}) == 0
    assert "synthetic" not in _settings_text()
    assert len(_stored()) == 3

    # Replacing the set without "svc" drops its two entries and keeps the other server's.
    assert _configure(tmp_path, {"other": {"command": "fixture", "env": {"TOKEN": "synthetic-token-0006"}}}) == 0
    assert list(_stored().values()) == ["synthetic-token-0006"]
    assert _file_refs() == _live_refs()  # the dropped entries' values went in the commit's sweep
    saved = json.loads(_settings_text())["mcpServers"]
    assert list(saved) == ["other"] and "synthetic" not in _settings_text()


def test_configure_drops_an_entry_when_a_server_stops_using_it(isolated_db, store, tmp_path):
    assert _configure(tmp_path, {"svc": CONFIG}) == 0
    assert _configure(tmp_path, {"svc": {"command": "fixture", "env": {"LOG_LEVEL": "debug"}}}) == 0
    assert _stored() == {}


@pytest.mark.parametrize("failure", ["host", "commit"])
def test_a_failed_replacement_keeps_the_old_config_on_its_old_value(isolated_db, store, tmp_path, monkeypatch, failure):
    from infra.settings import get_mcp_servers
    from key_store_fixtures import fail_the_commit_after_a_key_write, values_after_a_sweep

    assert _configure(tmp_path, {"svc": CONFIG}) == 0
    before = dict(_stored())
    changed = {**CONFIG, "env": {name: value + "-new" for name, value in CONFIG["env"].items()}}

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated settings write failure")

    with monkeypatch.context() as patched:
        if failure == "host":
            patched.setattr("infra.settings.set_setting", boom)
        else:
            fail_the_commit_after_a_key_write(patched)
        with pytest.raises(RuntimeError):
            _configure(tmp_path, {"svc": changed})

    saved = get_mcp_servers()
    env, _headers = mcp_secrets.resolve("svc", saved["svc"])
    assert env == {name: value for name, value in CONFIG["env"].items() if name != "LOG_LEVEL"}
    assert _stored() == before
    assert sorted(values_after_a_sweep().values()) == sorted(before.values())


def test_a_failed_settings_write_keeps_the_old_entries_resolvable(isolated_db, store, tmp_path, monkeypatch):
    assert _configure(tmp_path, {"svc": CONFIG}) == 0
    before = dict(_stored())
    assert len(before) == 2

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated settings write failure")

    monkeypatch.setattr("infra.settings.set_setting", boom)
    with pytest.raises(RuntimeError):
        _configure(tmp_path, {"other": {"command": "fixture", "env": {"LOG_LEVEL": "debug"}}})

    assert _stored() == before
    from infra.settings import get_mcp_servers

    saved = get_mcp_servers()
    assert list(saved) == ["svc"]
    env, _headers = mcp_secrets.resolve("svc", saved["svc"])
    assert env == {name: value for name, value in CONFIG["env"].items() if name != "LOG_LEVEL"}


def test_configure_refuses_a_reference_whose_value_is_gone(isolated_db, store, tmp_path, capsys):
    assert _configure(tmp_path, {"svc": {"command": "fixture", "secret_env": ["TOKEN"]}}) == 2
    assert "no value in the key store" in capsys.readouterr().err


# --- launch paths ----------------------------------------------------------


STDIO_SERVER = textwrap.dedent('''
    import os
    from mcp.server.fastmcp import FastMCP

    app = FastMCP("fixture")

    @app.tool()
    def seen() -> str:
        """Report which expected variables arrived."""
        return ",".join(sorted(f"{k}={os.environ[k]}" for k in ("SERVICE_TOKEN", "LOG_LEVEL") if k in os.environ))

    app.run("stdio")
''')


def _install_uv(target: Path) -> Path:
    """Install uv for this test only and return the bin directory that holds it."""
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--target", str(target), "uv"],
        check=True, capture_output=True, timeout=180,
    )
    return target / "bin"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_stdio_server_starts_with_its_resolved_secret(isolated_db, store, monkeypatch, tmp_path):
    """A server script in the app's project directory, run by uv as a person runs it.

    Safehouse confines the app, so its interpreter must sit where the profile
    reaches: uv runs a uv-managed Python and resolves mcp through the app's cache.
    """
    from infra.config import WORKSPACE
    from mcp.client import stdio

    script = Path(WORKSPACE) / "mcp-secret-fixture.py"
    script.write_text(STDIO_SERVER)
    uv_bin = await asyncio.to_thread(_install_uv, tmp_path / "uv")
    monkeypatch.setenv("PATH", f"{uv_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    launched: list[list[str]] = []
    create_process = stdio._create_platform_compatible_process

    async def capture_process(command, args, *rest, **kwargs):
        launched.append([command, *args])
        return await create_process(command, args, *rest, **kwargs)

    monkeypatch.setattr(stdio, "_create_platform_compatible_process", capture_process)
    # STDIO_SERVER uses FastMCP, which mcp 2 renamed.
    config = {"command": "uv", "args": ["run", "--managed-python", "--no-project", "--with", "mcp<2", script.name],
              "env": {"SERVICE_TOKEN": "synthetic-token-0001", "LOG_LEVEL": "debug"}}
    # A cold start fetches a uv-managed Python and mcp into the app's cache.
    config["connect_timeout"] = 180
    mcp_import.save_import({"svc": config})
    saved = json.loads(_settings_text())["mcpServers"]["svc"]
    assert "synthetic" not in json.dumps(saved)

    registry = ToolRegistry()
    async with AsyncExitStack() as stack:
        errors = await connect_mcp_servers({"svc": saved}, registry, stack)
        assert errors == {}
        result = await registry.get("mcp_svc_seen").execute()
    secret = config["env"]["SERVICE_TOKEN"]
    assert launched and not any(secret in json.dumps(argv) for argv in launched)
    assert result == "LOG_LEVEL=debug,SERVICE_TOKEN=synthetic-token-0001"


@pytest.mark.asyncio
async def test_a_server_with_a_missing_secret_reports_which_variable(store):
    registry = ToolRegistry()
    async with AsyncExitStack() as stack:
        errors = await connect_mcp_servers(
            {"svc": {"command": sys.executable, "secret_env": ["SERVICE_TOKEN"]}}, registry, stack,
        )
    assert "SERVICE_TOKEN" in errors["svc"] and registry.tool_names == []


@pytest.mark.asyncio
async def test_a_legacy_header_reference_is_refused_at_launch_without_resolving_it(store):
    url = "http://127.0.0.1:1/mcp"
    clean, lifted = mcp_secrets.split_secrets(
        "svc", {"url": url, "headers": {"X-Api-Key": "synthetic-key-0003"}}, headers=True,
    )
    mcp_secrets.store_secrets(lifted)
    assert "synthetic" not in json.dumps(clean)

    message = "literal credential headers are refused; use bearer_file for bearer authentication"
    registry = ToolRegistry()
    async with AsyncExitStack() as stack:
        errors = await connect_mcp_servers({"svc": clean}, registry, stack)
    assert errors == {"svc": message} and registry.tool_names == []
    # The refusal comes before any value is read, so a missing value changes nothing.
    key_store.drop_managed_entry(next(iter(lifted)))
    async with AsyncExitStack() as stack:
        assert await connect_mcp_servers({"svc": clean}, ToolRegistry(), stack) == errors


def test_a_harness_is_never_handed_user_mcp_config(isolated_db, store):
    from subagents.harness_tool_bridge import write_cc_mcp_config

    mcp_import.save_import({"svc": dict(CONFIG)})
    path = Path(write_cc_mcp_config(thread_id="thread-1", tool_token="bridge-token-0007"))
    try:
        payload = json.loads(path.read_text())
    finally:
        path.unlink()
    assert list(payload["mcpServers"]) == ["corsia"]
    assert "synthetic" not in json.dumps(payload) and "svc" not in json.dumps(payload)


# --- consumer search -------------------------------------------------------


def test_every_writer_of_mcp_config_goes_through_the_key_store():
    """No source file other than the known writers touches the stored MCP server set,
    and each writer lifts credentials through ``mcp_secrets`` first."""
    writers = set()
    for path in SOURCE_ROOT.rglob("*.py"):
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        if relative.startswith(("tests/", ".venv/")):
            continue
        text = path.read_text()
        if re.search(r"""\[["']mcpServers["']\]\s*=""", text) or re.search(r"""["']mcpServers["']\s*:""", text):
            writers.add(relative)
    assert writers == {
        "cli_mcp.py",
        "infra/migration_mcp_secrets.py",
        "tools/mcp_import.py",
        "subagents/harness_tool_bridge.py",  # CORSIA's own bridge: it carries no user values
        "subagents/cc_invocation.py",  # the empty strict config that keeps Claude Code off user servers
        "scripts/spikes/t1b2_warm_fork_hot.py",  # a spike passing the same empty config
    }
    for relative in ("cli_mcp.py", "infra/migration_mcp_secrets.py", "tools/mcp_import.py"):
        assert "mcp_secrets" in (SOURCE_ROOT / relative).read_text(), relative


def test_no_stored_config_holds_a_literal_credential_after_any_write(isolated_db, store, tmp_path):
    mcp_import.save_import({"imported": dict(CONFIG)})
    assert _configure(tmp_path, {"configured": dict(CONFIG)}) == 0

    from infra.settings import get_mcp_servers

    servers = get_mcp_servers()
    assert servers
    for name, config in servers.items():
        assert not mcp_secrets.split_secrets(name, config, headers=True)[1], name
    assert "synthetic" not in _settings_text()
