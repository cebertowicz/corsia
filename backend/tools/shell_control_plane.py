"""Whether a shell command runs a CORSIA control-plane operation.

The operation's words anywhere in a simple command are refused:
``timeout 5 corsia restart`` and ``find . -exec corsia restart \;``.
Otherwise the raw-text pattern, which fails closed, judges the whole
command. Its match is cleared only when the text can do nothing but be
printed or searched: every simple command is a text program (``echo``,
``printf``, ``grep``, ``rg`` without ``--pre``, ``git commit``) and no
output goes to a file, so ``echo "corsia restart finished"`` runs while
the same text handed to any other program (a shell, ``source``, a
variable in program position, a git alias) is refused.
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
# Programs that only print or search their text and run none of it. The
# word must be exactly this: a path, a variable or a substitution in
# program position is not known to be one of them.
_TEXT_PROGRAMS = frozenset({"echo", "printf", "grep", "rg"})
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
    known = plain_word_indices(command, words)
    # Unknown quoting counts every word as written plainly: fails closed.
    plain = known or frozenset(range(len(words)))
    for word in words:
        # Command substitution runs even inside double quotes, where the
        # whole substitution is one word.
        for opener in _SUBSTITUTION_RE.finditer(word):
            if runs_control_plane_operation(word[opener.end():], depth + 1):
                return True
    segments = _segments(words, plain)
    if any(_runs(segment) for segment in segments):
        return True
    if CONTROL_PLANE_TEXT_RE.search(command) is None:
        return False
    return not (
        known
        and all(_only_text(segment) for segment in segments)
        and not _writes_file(words)
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


def _runs(words: Sequence[str]) -> bool:
    """Whether any word of a simple command starts a control-plane operation.

    Position does not matter: a wrapper (``timeout``, ``find -exec``,
    ``nohup``) cannot hide the words that follow it.
    """
    for index, word in enumerate(words):
        name = Path(word).name.casefold()
        args = list(words[index + 1:])
        if name == "corsia" and _CORSIA_VERB_RE.match(" ".join(args)):
            return True
        if name in _SERVICE_MANAGERS and any(
            _SERVICE_VERB_RE.search(arg) for arg in args
        ) and any("corsia" in arg.casefold() for arg in args):
            return True
    return False


def _only_text(words: Sequence[str]) -> bool:
    """Whether a simple command only prints or searches its words.

    ``rg --pre`` runs a program on each file; ``git`` is a text program
    only as ``git commit`` with nothing between, so no ``-c`` setting or
    alias reaches it.
    """
    program = words[0]
    if program == "git":
        return len(words) > 1 and words[1] == "commit"
    if program == "rg":
        return not any(word.startswith("--pre") for word in words)
    return program in _TEXT_PROGRAMS


def _writes_file(words: Sequence[str]) -> bool:
    """Whether output is redirected anywhere but ``/dev/null`` or another
    descriptor: text written to a file (a script, a startup file) can be
    run later."""
    for index, word in enumerate(words):
        if word not in _REDIRECTIONS or ">" not in word:
            continue
        target = words[index + 1] if index + 1 < len(words) else ""
        duplicate = word == ">&" and (target.isdigit() or target == "-")
        if target != "/dev/null" and not duplicate:
            return True
    return False

