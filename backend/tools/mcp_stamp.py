"""Which connected app and which upstream tool produced a persisted result.

The wrapper builds the stamp once at connect and exposes it as ``mcp_stamp``.
``ToolRegistry`` attaches it, from the tool object that ran the call, to every
string that call returns (``ConnectedAppResult``); the result row copies it from
there. It names the server by what it is (a remote server's host, a local
server's package or image) and never by CORSIA's alias or a harness's
``mcp__<server>__<tool>`` name, so an import from another client or a rename
leaves both values unchanged.

The stamp never holds a header, token, environment value or call argument. The
launch command is the one place a secret can sit beside a name, so the rule is
structural: a flag's value is never read as the package, and a candidate that
does not look like a package name falls back to the bare command.
"""

from __future__ import annotations

import os
import re
from pathlib import PurePath
from typing import Any
from urllib.parse import urlsplit

APP_FIELD = "mcp_app"
TOOL_FIELD = "mcp_tool"

# Runners whose first positional argument is the package they start.
_PACKAGE_RUNNERS = {"npx", "bunx", "pnpx", "uvx"}
_SUBCOMMAND_RUNNERS = {
    "pnpm": ["dlx"], "yarn": ["dlx"], "npm": ["exec"], "pipx": ["run"], "uv": ["tool", "run"],
}
_CONTAINER_RUNNERS = {"docker", "podman"}
# Interpreters name no server; the script (or ``-m`` module) they run does.
_INTERPRETERS = {"node", "bun", "deno", "python", "python3", "ruby"}
_SCRIPT_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".mts", ".py", ".rb")
# Interpreter flags are overwhelmingly boolean, so only these take a value.
_INTERPRETER_VALUE_FLAGS = {"-r", "--require", "--import", "--loader", "--env-file", "-W", "-X"}
_PACKAGE_FLAGS = {"--from", "--package"}
# A flag without ``=`` consumes the next token (it may be a secret) unless it is
# known to take no value. Kept small on purpose: an unknown boolean flag makes the
# stamp fall back to the bare command, never to a wrong or secret name.
_NO_VALUE_FLAGS = {
    "-y", "--yes", "-q", "--quiet", "--offline", "--prefer-offline", "--no-install",
    "--refresh", "--isolated", "--no-cache",
}
_CONTAINER_NO_VALUE_FLAGS = {
    "-i", "-t", "-it", "-d", "--rm", "--init", "--interactive", "--tty", "--detach",
    "--read-only", "--privileged",
}
_PACKAGE_NAME = re.compile(r"^(?:@[A-Za-z0-9][\w.~-]*/)?[A-Za-z0-9][\w.~-]*(?:\[[\w,.-]+\])?$")
_VERSION_PIN = re.compile(r"(?<=.)(@|==|~=|>=|<=|!=|>|<)[^/@]*$")
_IMAGE_NAME = re.compile(r"^[A-Za-z0-9][\w.-]*(?::\d+)?(?:/[\w.-]+)+$")
_HOME_PREFIX = re.compile(r"^(?:/Users|/home)/[^/]+")


def _remote_identity(url: str) -> str | None:
    """``scheme://host[:port]`` only: hosted endpoints put API keys in the path or query."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname:
        return None
    host = parts.hostname.lower()
    host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme.lower()}://{host}{f':{port}' if port else ''}"


def _package_name(token: str) -> str | None:
    """A package token without its version pin; None unless it has a package name's shape."""
    if "://" in token:
        return _remote_identity(token)
    name = _VERSION_PIN.sub("", token)
    return name if _PACKAGE_NAME.match(name) else None


def _positional(args: list[str], no_value: set[str]) -> str | None:
    """The first argument that is neither a flag nor a flag's value."""
    iterator = iter(args)
    for token in iterator:
        if token == "--":
            return next(iterator, None)
        flag, has_value, value = token.partition("=")
        if flag in _PACKAGE_FLAGS:
            return value if has_value else next(iterator, None)
        if token.startswith("-"):
            if not has_value and token not in no_value:
                next(iterator, None)
            continue
        return token
    return None


def _interpreter_target(args: list[str]) -> str | None:
    """The script (home prefix replaced by ``~``) or module an interpreter starts."""
    iterator = iter(args)
    for token in iterator:
        if token == "-m":
            return next(iterator, None)
        if token in _INTERPRETER_VALUE_FLAGS:
            next(iterator, None)
        elif not token.startswith("-") and token.endswith(_SCRIPT_SUFFIXES):
            home = str(PurePath(os.path.expanduser("~")))
            if home != "/" and token.startswith(home + "/"):
                token = "~" + token[len(home):]
            return _HOME_PREFIX.sub("~", token)
    return None


def _image_identity(args: list[str]) -> str | None:
    """The image a ``docker run`` starts, without tag or digest; None for a bare name.

    A single-segment image (``alpine``) is refused: with no registry or namespace
    it is indistinguishable from a stray positional value.
    """
    if not args or args[0] != "run":
        return None
    image = _positional(args[1:], _CONTAINER_NO_VALUE_FLAGS)
    if not image:
        return None
    image = image.split("@", 1)[0]
    head, slash, last = image.rpartition("/")
    image = f"{head}{slash}{last.split(':', 1)[0]}"
    return image if _IMAGE_NAME.match(image) else None


def _local_identity(command: str, args: object) -> str | None:
    """The package, image or script a local server starts, or its command when it names none."""
    base = PurePath(command).name
    arguments = [value for value in args if isinstance(value, str)] if isinstance(args, list) else []
    target: str | None = None
    if base in _PACKAGE_RUNNERS:
        target = _package_name(_positional(arguments, _NO_VALUE_FLAGS) or "")
    elif base in _SUBCOMMAND_RUNNERS:
        words = _SUBCOMMAND_RUNNERS[base]
        if arguments[: len(words)] == words:
            target = _package_name(_positional(arguments[len(words):], _NO_VALUE_FLAGS) or "")
    elif base in _CONTAINER_RUNNERS:
        target = _image_identity(arguments)
    elif base in _INTERPRETERS:
        target = _interpreter_target(arguments)
    return target or base or None


def app_identity(config: dict) -> str | None:
    """Return the server's stable identity from its launch config."""
    url = config.get("url")
    if isinstance(url, str) and url:
        return _remote_identity(url)
    command = config.get("command")
    if isinstance(command, str) and command:
        return _local_identity(command, config.get("args"))
    return None


def build_stamp(config: dict, upstream_tool: str) -> dict[str, str]:
    """The result-row fields for one connected-app tool; empty when no identity can be named."""
    app = app_identity(config)
    return {APP_FIELD: app, TOOL_FIELD: upstream_tool} if app and upstream_tool else {}


class ConnectedAppResult(str):
    """Tool-result text that carries the stamp of the connected-app tool that ran the call."""

    mcp_stamp: dict[str, str]

    def __new__(cls, value: str, stamp: dict[str, str]) -> "ConnectedAppResult":
        instance = super().__new__(cls, value)
        instance.mcp_stamp = stamp
        return instance

    def __getnewargs__(self) -> tuple[str, dict[str, str]]:  # type: ignore[override]
        """Preserve the stamp across copy/pickle."""
        return str(self), self.mcp_stamp


def with_connected_app_stamp(tool: object, result: Any) -> Any:
    """Stamp a string result with the tool that ran the call; any other result passes through."""
    stamp = getattr(tool, "mcp_stamp", None)
    if isinstance(result, str) and isinstance(stamp, dict) and stamp:
        return ConnectedAppResult(result, stamp)
    return result


def restamp(source: object, text: Any) -> Any:
    """``text``, a transformed copy of ``source``, keeping the stamp ``source`` carried."""
    stamp = result_stamp(source)
    return ConnectedAppResult(text, stamp) if stamp and isinstance(text, str) else text


def result_stamp(content: object) -> dict[str, str]:
    """The stamp a result carries; nothing for any result that did not come from a connected app."""
    stamp = getattr(content, "mcp_stamp", None)
    return dict(stamp) if isinstance(stamp, dict) else {}
