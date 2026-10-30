"""The shared pane URL policy at the native command boundary."""
from __future__ import annotations

import shlex
from collections.abc import Sequence

from host_access.browser_session_state import BrowserSessionError
from host_access.browser_url import normalize_browser_url

URL_REFUSAL = ("Browser navigation requires HTTP, HTTPS or about:blank; open a web page, "
               "then read it with snapshot or get text")


def normalize_url(value: str) -> str:
    normalized = normalize_browser_url(value)
    if normalized is None:
        raise BrowserSessionError(URL_REFUSAL)
    return normalized


def _positions(args: Sequence[str]) -> list[int]:
    # agent-browser 0.31.2 --help and commands.rs: navigation, read, tabs, diff,
    # recording, vitals and SPA navigation. Non-navigation URL patterns stay data.
    command = args[0]
    if command in {"open", "goto", "navigate", "pushstate"}:
        return [1] if len(args) > 1 else []
    if command == "diff" and args[1:2] == ("url",):
        return list(range(2, min(len(args), 4)))
    if command == "record" and len(args) > 3 and args[1] in {"start", "restart"}:
        return [3]
    if command == "tab" and args[1:2] == ("new",):
        position = 2
        while position < len(args):
            if args[position] == "--label":
                position += 2
            else:
                return [position]
        return []
    if command in {"read", "vitals", "web-vitals"}:
        position = 1
        while position < len(args):
            item = args[position]
            if command == "read" and item in {"--llms", "--filter", "--timeout"}:
                position += 2
            elif item.startswith("--"):
                position += 1
            else:
                return [position]
    return []


def normalize_command(args: Sequence[str]) -> tuple[str, ...]:
    """Normalize every URL operand, including commands nested in native batch strings."""
    result = list(args)
    if args[0].startswith("-"):
        raise BrowserSessionError("Browser commands must start with their command name; " + URL_REFUSAL)
    if args[0] == "batch":
        for position in range(1, len(args)):
            if args[position] == "--bail":
                continue
            try:
                nested = shlex.split(args[position])
            except ValueError as exc:
                raise BrowserSessionError("Browser batch command has invalid quoting") from exc
            if nested:
                normalized = normalize_command(nested)
                if tuple(nested) != normalized:
                    result[position] = shlex.join(normalized)
        return tuple(result)
    for position in _positions(tuple(args)):
        result[position] = normalize_url(args[position])
    return tuple(result)
