"""Recognize a direct command: one argv the shell would run without interpreting it."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

_SHELL_COMMAND_SEPARATORS = {
    ";", "&&", "||", "|", "|&", "&", "&>", "\n", "(", ")",
}
_SHELL_EXECUTION_MARKERS = ("$(", "`", "<(", ">(")
_DIRECT_SHELL_MARKERS = ("\n", "\r", *_SHELL_EXECUTION_MARKERS)
_DIRECT_SHELL_TOKENS = {
    *_SHELL_COMMAND_SEPARATORS,
    "<",
    "<<",
    "<<<",
    ">",
    ">>",
    ">|",
    "<>",
    "<&",
    ">&",
}


def direct_argv(command: str) -> list[str] | None:
    """Return the argv of one direct command, or None when the shell would interpret it."""
    if any(marker in command for marker in _DIRECT_SHELL_MARKERS):
        return None
    tokens = command_tokens(command)
    if not tokens or any(token in _DIRECT_SHELL_TOKENS for token in tokens):
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv or _is_env_assignment(argv[0]):
        return None
    executable = Path(argv[0])
    if executable.name != argv[0]:
        return None
    return argv


def _is_env_assignment(token: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token))


def command_tokens(command: str) -> list[str]:
    """Split a command into shell words and operator tokens; [] when it does not lex."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return []
