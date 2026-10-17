"""Where OpenClaw and Hermes Agent keep their MCP servers, and how each entry reads.

Read only, and only the server definitions: OpenClaw's ``mcp.servers`` in its
JSON5 config and Hermes' ``mcp_servers`` in its ``config.yaml``. Neither tool's
credentials, ``.env`` files, sessions or databases are opened. Each entry then
goes through the same import path as every other source, so secrets, sign-in
and status handling are shared.

Locations follow each vendor's own documentation (read 2026-10-16):
https://docs.openclaw.ai/gateway/configuration (config file, ``OPENCLAW_CONFIG_PATH``,
``OPENCLAW_STATE_DIR``, ``OPENCLAW_HOME``), https://docs.openclaw.ai/gateway/config-extensions
(``mcp.servers``), https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference
(``mcp_servers``) and https://hermes-agent.nousresearch.com/docs/reference/environment-variables
(``HERMES_HOME``).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import tools.mcp_import_json5 as mcp_import_json5
import tools.mcp_secrets as mcp_secrets

SOURCES = ("openclaw", "hermes")
_MAX_CONFIG_BYTES = 1_000_000
_UNSET_VALUES = {"", "undefined", "null"}


def _environment(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return None if value in _UNSET_VALUES else value


def _expand(value: str, home: Path, *, variables: bool = False) -> Path:
    """Expand a leading ``~`` against ``home`` (and ``$VAR`` where the vendor does)."""
    if variables:
        value = os.path.expandvars(value)
    if value == "~" or value.startswith(("~/", "~\\")):
        return home / value[2:]
    return Path(value)


def config_path(source: str, home: Path) -> Path:
    if source == "openclaw":
        # An explicit config path wins; otherwise the config sits in the state directory.
        explicit = _environment("OPENCLAW_CONFIG_PATH")
        if explicit:
            return _expand(explicit, home)
        state = _environment("OPENCLAW_STATE_DIR")
        if state:
            return _expand(state, home) / "openclaw.json"
        relocated = _environment("OPENCLAW_HOME")
        return (_expand(relocated, home) if relocated else home) / ".openclaw" / "openclaw.json"
    override = _environment("HERMES_HOME")
    if override:
        return _expand(override, home, variables=True) / "config.yaml"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "config.yaml"
    return home / ".hermes" / "config.yaml"


def _parse(source: str, text: str) -> Any:
    if source == "openclaw":
        return mcp_import_json5.loads(text)
    import yaml

    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError("invalid YAML") from exc


def _document(source: str, path: Path) -> dict[str, Any]:
    """The parsed config, or ``{}`` when it is unreadable, oversized or malformed."""
    try:
        if path.stat().st_size > _MAX_CONFIG_BYTES:
            return {}
        value = _parse(source, path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def entries(source: str, path: Path) -> list[tuple[str, dict[str, Any], Path | None]]:
    """The enabled server definitions in one config file; a disabled one is not offered."""
    document = _document(source, path)
    if source == "openclaw":
        mcp = document.get("mcp")
        servers = mcp.get("servers") if isinstance(mcp, dict) else None
    else:
        servers = document.get("mcp_servers")
    if not isinstance(servers, dict):
        return []
    return [
        (name, config, None) for name, config in servers.items()
        if isinstance(name, str) and isinstance(config, dict) and config.get("enabled") is not False
    ]


def kind(config: dict[str, Any]) -> str | None:
    command, url = config.get("command"), config.get("url")
    if isinstance(command, str) and command:
        return "stdio"
    if isinstance(url, str) and url and transport(config) in {"http", "sse"}:
        return "remote"
    return None


def transport(config: dict[str, Any]) -> str | None:
    """``http`` or ``sse`` for a remote entry; ``None`` for a transport CORSIA does not read."""
    declared = config.get("transport", config.get("type"))
    if declared is None or declared in {"http", "streamable-http"}:
        return "http"
    return "sse" if declared == "sse" else None


def needs_sign_in(source: str, config: dict[str, Any]) -> bool:
    return source in SOURCES and config.get("auth") == "oauth"


def timeouts(source: str, config: dict[str, Any]) -> dict[str, Any]:
    """Timeouts in the seconds CORSIA keeps; a value that is not a number is left for validation to refuse."""
    if source == "openclaw":
        keys = (("connectionTimeoutMs", "connect_timeout", 1000), ("requestTimeoutMs", "tool_timeout", 1000))
    else:
        keys = (("connect_timeout", "connect_timeout", 1), ("timeout", "tool_timeout", 1))
    copied: dict[str, Any] = {}
    for key, target, divisor in keys:
        if key not in config:
            continue
        value = config[key]
        number = isinstance(value, (int, float)) and not isinstance(value, bool)
        copied[target] = value / divisor if number else value
    return copied


def _is_reference(value: Any) -> bool:
    """A ``${VAR}`` or ``${env:VAR}`` placeholder the tool fills from its own environment."""
    return isinstance(value, str) and "${" in value


def has_reference(headers: dict[Any, Any]) -> bool:
    return any(isinstance(name, str) and name and _is_reference(value) for name, value in headers.items())


def complete(source: str, config: dict[str, Any], copied: dict[str, Any]) -> None:
    """Add what only these sources carry: timeouts in seconds, and names for unread references."""
    if source in SOURCES:
        copied.update(timeouts(source, config))
        withhold_references(copied)


def withhold_references(copied: dict[str, Any]) -> None:
    """Keep a placeholder's variable name, never the placeholder text, as the key to set later.

    The tool resolves ``${VAR}`` from its own ``.env``, which is never read, so storing the
    placeholder as the credential would send a literal ``${VAR}``. The name goes on the same
    reference lists the Codex import uses, and the person sets the value in Settings > Keys.
    An ``Authorization: Bearer ${VAR}`` keeps its known scheme, as a literal one does on import.
    """
    for field, listed, token in (("env", "secret_env", False), ("headers", "secret_headers", True)):
        values = copied.get(field)
        if not isinstance(values, dict):
            continue
        names = [name for name, value in values.items() if isinstance(name, str) and name and _is_reference(value)]
        if not names:
            continue
        kept = {name: value for name, value in values.items() if name not in names}
        if kept:
            copied[field] = kept
        else:
            del copied[field]
        copied[listed] = [*copied.get(listed, []), *names]
        for name in names if token else []:
            scheme = mcp_secrets.split_scheme(values[name])[0] if name.lower() == "authorization" else None
            if scheme is not None:
                pair = {"header": name, "scheme": scheme}
                copied[mcp_secrets.HEADER_SCHEMES] = [*copied.get(mcp_secrets.HEADER_SCHEMES, []), pair]
        if token:
            copied["needs_token"] = True
