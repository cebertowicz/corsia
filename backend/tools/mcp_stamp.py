"""Which connected app and which upstream tool produced a persisted result.

The stamp is read from the live wrapper when a result row is written. It names
the server by what it is (a remote server's URL, a local server's package) and
never by CORSIA's alias or a harness's ``mcp__<server>__<tool>`` name, so an
import from another client or a rename leaves both values unchanged. It holds no
header, token, environment value or call argument.
"""

from __future__ import annotations

import re
from pathlib import PurePath
from urllib.parse import urlsplit

APP_FIELD = "mcp_app"
TOOL_FIELD = "mcp_tool"

# Runners whose first positional argument is the package they start.
_PACKAGE_RUNNERS = {"npx", "bunx", "pnpx", "uvx"}
# Runners that need a subcommand first: ``pnpm dlx <package>``.
_SUBCOMMAND_RUNNERS = {
    "pnpm": ("dlx",), "yarn": ("dlx",), "npm": ("exec",), "pipx": ("run",), "uv": ("tool", "run"),
}
# Interpreters name no server; the script they run does.
_INTERPRETERS = {"node", "bun", "deno", "python", "python3", "ruby"}
_PACKAGE_FLAGS = {"--from", "--package", "-p"}
_VERSION_SUFFIX = re.compile(r"(?<=.)(@|==|~=|>=|<=)[^/@]*$")


def _remote_identity(url: str) -> str | None:
    """The server's endpoint without credentials, query or fragment."""
    parts = urlsplit(url)
    if not parts.hostname:
        return None
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname.lower()}{port}{parts.path.rstrip('/')}"


def _package_name(token: str) -> str:
    """One package token without its version pin, and never a credential-bearing URL."""
    if "://" in token:
        return _remote_identity(token) or ""
    return _VERSION_SUFFIX.sub("", token)


def _first_positional(args: list[str]) -> str | None:
    """The package a runner names: its package flag's value, else the first non-flag argument."""
    iterator = iter(args)
    for token in iterator:
        if token in _PACKAGE_FLAGS:
            return next(iterator, None)
        flag, _, value = token.partition("=")
        if value and flag in _PACKAGE_FLAGS:
            return value
        if not token.startswith("-"):
            return token
    return None


def _local_identity(command: str, args: object) -> str | None:
    """The package a local server starts, or its command when it names no package."""
    base = PurePath(command).name
    arguments = [value for value in args if isinstance(value, str)] if isinstance(args, list) else []
    package: str | None = None
    if base in _PACKAGE_RUNNERS:
        package = _first_positional(arguments)
    elif base in _SUBCOMMAND_RUNNERS:
        subcommand = _SUBCOMMAND_RUNNERS[base]
        if arguments[: len(subcommand)] == list(subcommand):
            package = _first_positional(arguments[len(subcommand):])
    elif base in _INTERPRETERS:
        script = _first_positional(arguments)
        return PurePath(script).name if script else base
    if package:
        return _package_name(package) or base
    return base or None


def app_identity(config: dict) -> str | None:
    """Return the server's stable identity from its launch config."""
    url = config.get("url")
    if isinstance(url, str) and url:
        return _remote_identity(url)
    command = config.get("command")
    if isinstance(command, str) and command:
        return _local_identity(command, config.get("args"))
    return None


def result_stamp(tool_name: str) -> dict[str, str]:
    """Return the result-row fields for a live connected-app tool, or nothing for any other tool."""
    from tools.mcp_runtime import live_mcp_tool

    wrapper = live_mcp_tool(tool_name)
    if wrapper is None:
        return {}
    config = getattr(wrapper, "mcp_server_config", None)
    app = app_identity(config) if isinstance(config, dict) else None
    upstream = getattr(wrapper, "mcp_tool_name", None)
    if app is None or not isinstance(upstream, str) or not upstream:
        return {}
    return {APP_FIELD: app, TOOL_FIELD: upstream}
