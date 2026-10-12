"""Bounded, server-side MCP configuration discovery and import.

Only documented user configuration files are read. Detection returns server names
and kinds; raw configuration stays inside this module until a selected server is
copied into CORSIA's canonical tools.mcpServers setting.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tomllib
from typing import Any

from tools import mcp_secrets
from tools.mcp_config import MCPConfigError, allowed_names, http_options, timeout_seconds

_SOURCE_LABELS = {
    "claude_desktop": "Claude Desktop",
    "claude_code": "Claude Code",
    "cursor": "Cursor",
    "codex": "Codex",
}


def _home() -> Path:
    return Path.home()


def _paths(home: Path | None = None) -> dict[str, Path]:
    home = home or _home()
    desktop = Path(os.environ.get("APPDATA", "")) / "Claude" / "claude_desktop_config.json" if os.name == "nt" else (
        home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    )
    codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex"))
    return {
        "claude_desktop": desktop,
        "claude_code": home / ".claude.json",
        "cursor": home / ".cursor" / "mcp.json",
        "codex": codex_home / "config.toml",
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_servers(path: Path, key: str = "mcpServers") -> dict[str, dict[str, Any]]:
    value = _read_json(path).get(key)
    return {name: config for name, config in value.items()
            if isinstance(name, str) and isinstance(config, dict)} if isinstance(value, dict) else {}


def _kind(source: str, config: dict[str, Any]) -> str | None:
    command, url = config.get("command"), config.get("url")
    declared = config.get("type")
    if source == "claude_code":
        if declared == "sdk" or (isinstance(url, str) and not declared):
            return None
        if declared in {None, "stdio"} and isinstance(command, str) and command:
            return "stdio"
        if declared in {"http", "streamable-http", "sse", "ws"} and isinstance(url, str) and url:
            return "remote"
        return None
    if source == "claude_desktop":
        return "stdio" if isinstance(command, str) and command else None
    if source == "cursor":
        if isinstance(command, str) and command:
            return "stdio"
        return "remote" if isinstance(url, str) and url else None
    if source == "codex":
        if isinstance(command, str) and command:
            return "stdio"
        return "remote" if isinstance(url, str) and url else None
    return None


def _source_servers(source: str, path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    if source == "codex":
        servers = _read_toml(path).get("mcp_servers")
        return {name: config for name, config in servers.items()
                if isinstance(name, str) and isinstance(config, dict)} if isinstance(servers, dict) else {}
    return _json_servers(path)


def detected_sources(home: Path | None = None) -> list[dict[str, Any]]:
    """Return the safe, bounded source view used by the conductor."""
    results = []
    for source, path in _paths(home).items():
        servers = [
            {"name": name, "kind": kind}
            for name, config in _source_servers(source, path).items()
            if (kind := _kind(source, config)) is not None
        ]
        if servers:
            results.append({"source": source, "app": _SOURCE_LABELS[source], "servers": servers})
    return results


def _copyable_config(source: str, config: dict[str, Any]) -> dict[str, Any]:
    kind = _kind(source, config)
    if kind is None:
        raise MCPConfigError("server configuration is unsupported")
    copied: dict[str, Any] = {}
    if kind == "stdio":
        copied["command"] = config["command"]
        if "args" in config:
            copied["args"] = config["args"]
        if "env" in config:
            copied["env"] = config["env"]
    else:
        copied["url"] = config["url"]
        headers = config.get("headers", config.get("http_headers"))
        if headers is not None:
            copied["headers"] = headers
    for source_key, target_key in (("connect_timeout", "connect_timeout"),
                                   ("startup_timeout_sec", "connect_timeout"),
                                   ("tool_timeout", "tool_timeout"),
                                   ("tool_timeout_sec", "tool_timeout")):
        if source_key in config:
            copied[target_key] = config[source_key]
    return copied


def _validate(server: str, config: dict[str, Any]) -> None:
    """Validate the config as it will be saved: credential values become key store references."""
    config, _ = mcp_secrets.split_secrets(server, config)
    allowed_names(config)
    timeout_seconds(config, "connect_timeout", 10)
    timeout_seconds(config, "tool_timeout", 30)
    if config.get("url"):
        http_options(config)
    elif not isinstance(config.get("command"), str) or config.get("bearer_file"):
        raise MCPConfigError("configure an HTTP URL or a stdio command without bearer_file")


def prepare_import(selected: list[dict[str, str]], home: Path | None = None) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """Select validated configs without returning any configuration values."""
    paths = _paths(home)
    available = {source: _source_servers(source, path) for source, path in paths.items()}
    name_counts: dict[str, int] = {}
    for item in selected:
        name = item.get("name")
        if isinstance(name, str):
            name_counts[name] = name_counts.get(name, 0) + 1
    additions: dict[str, dict[str, Any]] = {}
    statuses: list[dict[str, str]] = []
    for item in selected:
        source, name = item.get("source"), item.get("name")
        if source not in _SOURCE_LABELS or not isinstance(name, str):
            statuses.append({"name": str(name or "unknown"), "status": "failed: invalid selected server"})
            continue
        if name_counts[name] > 1:
            statuses.append({"name": name, "status": "failed: selected server name conflicts with another selection"})
            continue
        config = available[source].get(name)
        if config is None:
            statuses.append({"name": name, "status": "failed: server was not found in that client"})
            continue
        kind = _kind(source, config)
        try:
            copied = _copyable_config(source, config)
            _validate(name, copied)
        except MCPConfigError as error:
            statuses.append({"name": name, "kind": kind or "unknown", "status": f"failed: {error}"})
            continue
        additions[name] = copied
        statuses.append({"name": name, "kind": kind or "unknown", "status": "pending"})
    return additions, statuses


def save_import(additions: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Merge selected configs and classify existing names in one transaction.

    A new server's credential values go to the key store and its config keeps
    references. A name already set up is never overwritten, and its stored
    credentials are left as they are.
    """
    from infra.database import write_transaction
    from infra.settings import get_setting, set_setting

    outcomes: dict[str, str] = {}
    with write_transaction(None, scope="import_mcp_servers") as db:
        tools = get_setting("tools", db=db) or {}
        servers = tools.get("mcpServers", {})
        servers = dict(servers) if isinstance(servers, dict) else {}
        for name, config in additions.items():
            clean, lifted = mcp_secrets.split_secrets(name, config)
            if name not in servers:
                mcp_secrets.store_secrets(lifted, db=db)
                servers[name] = clean
                outcomes[name] = "saved"
            elif servers[name] == clean and mcp_secrets.stored_matches(lifted, db=db):
                outcomes[name] = "existing_equal"
            else:
                outcomes[name] = "conflict"
        tools["mcpServers"] = servers
        set_setting("tools", tools, db=db)
    return outcomes
