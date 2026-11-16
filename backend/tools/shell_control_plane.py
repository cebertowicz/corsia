"""Whether a shell command runs a CORSIA control-plane operation.

A command is judged by its separate words, anywhere in a simple command:
``timeout 5 corsia restart`` and ``find . -exec corsia restart \;`` are
refused, while quoted text such as ``echo "corsia restart finished"`` is
one word and only prints. A string that a carrier runs as a command again
(a shell's ``-c``, ``su -c``, ``eval``, ``ssh``, ``watch``, ``trap``,
``alias``, ``env -S``, ``$(...)``, backquotes) is judged the same way. The
raw-text pattern, which fails closed, judges a command that does not
tokenize, one holding a runner whose command text is not modelled (an
interpreter, awk, tmux, xargs, a shell reading stdin), a carried string
whose text is computed when it runs (``$(...)``, a backquote, ``$NAME``),
a word holding ``=!`` (a git alias runs its shell command), and each
``NAME=value`` word's value.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Sequence

from host_access.shell_credential_positions import plain_word_indices
from host_access.shell_view import shell_words

CONTROL_PLANE_TEXT_RE = re.compile(
    r"\bcorsia\s+(?:dev\s+up\b|"
    r"(?:(?:dev|service)\s+)?(?:reload|restart|stop)\b)"
    r"|\b(?:launchctl|systemctl)\b[^\n;&|]*"
    r"\b(?:bootout|kickstart|kill|restart|stop|unload)\b"
    r"[^\n;&|]*\bcorsia\b",
    re.IGNORECASE,
)
_CORSIA_VERB_RE = re.compile(
    r"(?:dev\s+up\b|(?:(?:dev|service)\s+)?(?:reload|restart|stop)\b)",
    re.IGNORECASE,
)
_SERVICE_VERB_RE = re.compile(
    r"\b(?:bootout|kickstart|kill|restart|stop|unload)\b", re.IGNORECASE
)
_SERVICE_MANAGERS = frozenset({"launchctl", "systemctl"})
# Run their ``-c``/``--command`` string; without one they read commands
# from stdin or a script the command text does not show.
_SHELLS = frozenset(
    {"bash", "csh", "dash", "fish", "ksh", "sh", "su", "tcsh", "zsh"}
)
# Join their arguments into one command line that a shell parses again.
_JOINERS = frozenset({"eval", "ssh", "trap", "watch"})
# Run command text in a form not modelled here (code strings, stdin, a
# terminal); like a shell with no ``-c``, the raw-text pattern judges them.
_RUNNERS_RE = re.compile(
    r"python\d*(?:\.\d+)?|perl|ruby|node|deno|bun|php|lua|osascript|tmux"
    r"|screen|script|flock|runuser|parallel|at|batch|expect|xargs|[gmn]?awk"
)
# Text that a carried string expands only when it runs: its command is
# unknowable here.
_EXPANSION_RE = re.compile(r"`|\$[\w{(@*#?!$-]")
# A ``NAME=value`` word, bare or after export, local, declare and the like.
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
_REDIRECTIONS = frozenset(
    {"<", "<<", "<<<", ">", ">>", ">|", "<>", "<&", ">&", "&>", "&>>"}
)
_OPERATOR_CHARS = frozenset(";&|<>")
_SUBSTITUTION_RE = re.compile(r"\$\(|`")
# Inside a plain word these start or end a command: ``$(``, a backquote,
# a leading ``(`` (subshell, ``<(...)``) and ``)`` (also a case pattern).
_COMMAND_EDGE_RE = re.compile(r"\$\(|`|\)|^\(+")
_MAX_DEPTH = 8


def runs_control_plane_operation(command: str, depth: int = 0) -> bool:
    """Whether bash would run ``corsia`` or a service manager on CORSIA."""
    if depth > _MAX_DEPTH:
        return True
    try:
        words = shell_words(command)
    except ValueError:
        return CONTROL_PLANE_TEXT_RE.search(command) is not None
    # Unknown quoting counts every word as written plainly: fails closed.
    plain = plain_word_indices(command, words) or frozenset(range(len(words)))
    for word in words:
        # Command substitution runs even inside double quotes, where the
        # whole substitution is one word.
        for opener in _SUBSTITUTION_RE.finditer(word):
            if runs_control_plane_operation(word[opener.end():], depth + 1):
                return True
    return any(
        _runs(segment, depth, command)
        for segment in _segments(words, plain)
    )


def _segments(
    words: Sequence[str], plain: frozenset[int]
) -> list[list[str]]:
    """Split shell words into simple commands, dropping redirections.

    In a plain word, ``$(``, a backquote or a leading ``(`` opens a nested
    command (substitution, subshell, ``<(...)``) and its closer returns to
    the enclosing one; a ``)`` with nothing open (a case pattern, ``f()``)
    and a ``{`` word start a new command.
    """
    done: list[list[str]] = []
    current: list[str] = []
    open_: list[tuple[list[str], str]] = []  # enclosing command, its closer
    redirection_target = False
    for index, word in enumerate(words):
        if word in _REDIRECTIONS:
            if current and current[-1].isdigit():
                current.pop()  # the file descriptor in 2>file
            redirection_target = True
            continue
        if word and set(word) <= _OPERATOR_CHARS:
            done.append(current)
            current = []
            continue
        edges = (
            list(_COMMAND_EDGE_RE.finditer(word)) if index in plain else []
        )
        # A redirection's target is a file name, unless it is <(...).
        if redirection_target and not edges:
            redirection_target = False
            continue
        redirection_target = False
        if index in plain and word == "{":
            done.append(current)
            current = []
            continue
        start = 0
        for edge in [*edges, None]:
            piece = word[start:edge.start() if edge else len(word)]
            mark = edge.group() if edge else ""
            closer = "`" if mark == "`" else ")"
            closing = mark in {")", "`"} and bool(open_) and open_[-1][1] == closer
            if mark in {"$(", "`"} and not closing:
                piece += mark  # the enclosing word expands when it runs
            if piece:
                current.append(piece)
            if edge is None:
                break
            start = edge.end()
            done.append(current)
            if closing:
                current = open_.pop()[0]  # back to the enclosing command
                continue
            if mark != ")":
                opened = len(mark) if mark.startswith("(") else 1
                open_.extend([(current, closer)] * opened)
            current = []
    done.append(current)
    done.extend(outer for outer, _closer in open_)
    return [segment for segment in done if segment]


def _runs(words: Sequence[str], depth: int, command: str) -> bool:
    """Whether any word of a simple command starts a control-plane operation.

    Position does not matter: a wrapper (``timeout``, ``find -exec``,
    ``nohup``) cannot hide the words that follow it.
    """
    for index, word in enumerate(words):
        name = Path(word).name.casefold()
        args = list(words[index + 1:])
        assignment = _ASSIGNMENT_RE.match(word)
        if assignment and CONTROL_PLANE_TEXT_RE.search(word[assignment.end():]):
            return True
        if (
            _RUNNERS_RE.fullmatch(name) or "=!" in word
        ) and CONTROL_PLANE_TEXT_RE.search(command):
            return True
        if name == "corsia" and _CORSIA_VERB_RE.match(" ".join(args)):
            return True
        if name in _SERVICE_MANAGERS and any(
            _SERVICE_VERB_RE.search(arg) for arg in args
        ) and any("corsia" in arg.casefold() for arg in args):
            return True
        if name in _SHELLS and _shell_runs(args, depth, command):
            return True
        if name in _JOINERS and _carried(" ".join(args), depth, command):
            return True
        if name == "alias" and any(
            _carried(arg.partition("=")[2], depth, command)
            for arg in args
            if "=" in arg
        ):
            return True
        if name == "env" and _env_split_runs(args, depth, command):
            return True
    return False


def _carried(text: str, depth: int, command: str) -> bool:
    """Whether a string a carrier runs as a command again runs an operation;
    if the string expands when it runs, the raw-text pattern judges the
    whole command."""
    if _EXPANSION_RE.search(text):
        return CONTROL_PLANE_TEXT_RE.search(command) is not None
    return runs_control_plane_operation(text, depth + 1)


def _shell_runs(args: Sequence[str], depth: int, command: str) -> bool:
    """Check each operand of ``sh -c`` (or ``--command``); a shell reading
    stdin or a script gets its text from elsewhere in the command, so the
    raw-text pattern judges the whole command."""
    long_values = [
        arg.partition("=")[2] for arg in args if arg.startswith("--command=")
    ]
    options = [arg for arg in args if arg[:1] in "-+" and not arg.startswith("--")]
    if not long_values and "--command" not in args and not any(
        "c" in option[1:] for option in options
    ):
        return CONTROL_PLANE_TEXT_RE.search(command) is not None
    return any(
        _carried(arg, depth, command)
        for arg in [*long_values, *(arg for arg in args if arg[:1] not in "-+")]
    )


def _env_split_runs(args: Sequence[str], depth: int, command: str) -> bool:
    """Check the string ``env -S`` splits into a command."""
    for index, arg in enumerate(args):
        value = None
        if arg in {"-S", "--split-string"} and index + 1 < len(args):
            value = args[index + 1]
        elif arg.startswith("--split-string="):
            value = arg.partition("=")[2]
        elif arg.startswith("-S") and arg != "-S":
            value = arg[2:]
        if value and _carried(value, depth, command):
            return True
    return False
