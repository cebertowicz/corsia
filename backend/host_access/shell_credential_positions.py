"""Where a shell command puts credential values, by word position.

Shared shell-word vocabulary and the position finders the redactor uses to
tell a credential value from an ordinary argument.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence


COMMAND_BOUNDARIES = frozenset({";", ";;", "&", "&&", "|", "||", "|&"})
_REDIRECTION_OPERATORS = frozenset(
    {"<", "<<", "<<<", ">", ">>", ">|", "<>", "<&", ">&", "&>"}
)
SHELL_SEPARATORS = COMMAND_BOUNDARIES | _REDIRECTION_OPERATORS
NON_VALUE_CREDENTIAL_OPTIONS = frozenset({"password-stdin", "with-token"})
LITERAL_OUTPUT_COMMANDS = frozenset({"echo", "printf"})
SECURITY_PASSWORD_SUBCOMMANDS = frozenset(
    {
        "add-generic-password",
        "add-internet-password",
        "set-keychain-password",
    }
)
_SAFE_PRINTF_CREDENTIAL_FORMATS = frozenset(
    {"%b", "%b\\n", "%s", "%s\\n"}
)
# Commands whose short -u is a switch that takes no value: ``date -u``
# prints UTC, ``sort -u`` drops duplicate lines.
_SWITCH_U_COMMANDS = frozenset({"date", "sort"})
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_PLAIN_COMMAND_RE = re.compile(r"[A-Za-z0-9._/+-]+")


def credential_stdin_source_indices(
    tokens: Sequence[str],
) -> frozenset[int]:
    """Locate shell tokens that directly supply credential-only stdin."""
    sensitive: set[int] = set()
    hard_boundaries = frozenset({";", ";;", "&", "&&", "||"})
    for consumer_index, raw in enumerate(tokens):
        option, _separator, _value = str(raw).partition("=")
        if option.removeprefix("--").casefold() not in (
            NON_VALUE_CREDENTIAL_OPTIONS
        ):
            continue

        command_end = len(tokens)
        for index in range(consumer_index + 1, len(tokens)):
            if str(tokens[index]) in hard_boundaries:
                command_end = index
                break
        for index in range(consumer_index + 1, command_end - 1):
            if str(tokens[index]) == "<<<":
                sensitive.add(index + 1)

        pipe_index: int | None = None
        for index in range(consumer_index - 1, -1, -1):
            token = str(tokens[index])
            if token in {"|", "|&"}:
                pipe_index = index
                break
            if token in hard_boundaries:
                break
        if pipe_index is None:
            continue

        segment_start = 0
        for index in range(pipe_index - 1, -1, -1):
            if str(tokens[index]) in COMMAND_BOUNDARIES:
                segment_start = index + 1
                break
        if segment_start >= pipe_index:
            continue
        executable = Path(str(tokens[segment_start])).name.casefold()
        if executable not in LITERAL_OUTPUT_COMMANDS:
            continue
        argument_indices = [
            index
            for index in range(segment_start + 1, pipe_index)
            if str(tokens[index]) not in SHELL_SEPARATORS
        ]
        if (
            executable == "printf"
            and argument_indices
            and str(tokens[argument_indices[0]])
            in _SAFE_PRINTF_CREDENTIAL_FORMATS
        ):
            argument_indices = argument_indices[1:]
        sensitive.update(argument_indices)
    return frozenset(sensitive)


def security_password_value_indices(
    tokens: Sequence[str],
) -> frozenset[int]:
    """Locate literal values supplied to security(1) password-write flags."""
    sensitive: set[int] = set()
    segment_start = 0
    for index in range(len(tokens) + 1):
        if (
            index < len(tokens)
            and str(tokens[index]) not in COMMAND_BOUNDARIES
        ):
            continue
        segment = [str(token) for token in tokens[segment_start:index]]
        if (
            len(segment) >= 2
            and Path(segment[0]).name.casefold() == "security"
            and segment[1].casefold() in SECURITY_PASSWORD_SUBCOMMANDS
        ):
            for offset, token in enumerate(segment[:-1]):
                if token == "-w":
                    sensitive.add(segment_start + offset + 1)
        segment_start = index + 1
    return frozenset(sensitive)


def short_u_is_switch(words: Sequence[str], index: int) -> bool:
    """Whether short ``-u`` at ``words[index]`` is a switch with no value.

    True only when the command it belongs to is named plainly and is one
    of ``_SWITCH_U_COMMANDS``. Anywhere else -u may carry a user and
    password (curl and tools that copy its flags), so a command this
    cannot name (a variable, an alias, curl started inside ``$(...)``)
    keeps the value treated as one.
    """
    segment: list[str] = []
    for raw in reversed(words[:index]):
        word = str(raw)
        if word in COMMAND_BOUNDARIES:
            break
        # A subshell or substitution starts its own command: (date,
        # ts=$(date, or date after a backquote.
        opener = max(word.rfind("("), word.rfind("`"))
        if opener >= 0:
            segment.insert(0, word[opener + 1:])
            break
        segment.insert(0, word)
    command = next(
        (word for word in segment if word and not _ASSIGNMENT_RE.match(word)),
        "",
    )
    return (
        _PLAIN_COMMAND_RE.fullmatch(command) is not None
        and Path(command).name.casefold() in _SWITCH_U_COMMANDS
    )
