"""Match one literal shell command against an observe rule's leading arguments."""

from __future__ import annotations

import shlex
from typing import Protocol

_SHELL_PUNCTUATION = ";&|<>()"
_PARAMETER_START = frozenset("{([*@$#?!-$")


class _HasArgvPrefix(Protocol):
    argv_prefix: tuple[str, ...]


def _has_active_shell_syntax(command: str) -> bool:
    """Detect top-level operators and active expansion without running a shell."""
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "\\":
            if index + 1 >= len(command):
                index += 1
            elif quote == '"' and command[index + 1] not in '$`"\\':
                index += 1
            else:
                index += 2
            continue
        if char == "'" and quote is None:
            quote = "'"
            index += 1
            continue
        if char == '"':
            quote = None if quote == '"' else '"'
            index += 1
            continue
        if char == "`":
            return True
        if char == "$" and index + 1 < len(command):
            next_char = command[index + 1]
            if (
                next_char.isalpha()
                or next_char.isdigit()
                or next_char == "_"
                or next_char in _PARAMETER_START
            ):
                return True
        if quote is None and char in _SHELL_PUNCTUATION:
            return True
        index += 1
    return False


def command_matches_rule(command: object, rule: _HasArgvPrefix) -> bool:
    """Match one literal top-level command against an observe rule's leading arguments."""
    if not isinstance(command, str) or "\n" in command or "\r" in command:
        return False
    if _has_active_shell_syntax(command):
        return False
    try:
        argv = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return False
    prefix = rule.argv_prefix
    return bool(prefix) and tuple(argv[: len(prefix)]) == prefix
