"""The reviewer's view of a Bash command: shell words and here-documents."""

from __future__ import annotations

from dataclasses import dataclass
import re
import shlex
from typing import Any, Mapping, Sequence

_NAME = r"[A-Za-z_][A-Za-z0-9_]{0,63}"
_DELIMITER_RE = re.compile(_NAME)
_DELIMITER_WORD_RE = re.compile(
    rf"'(?P<single>{_NAME})'|\"(?P<double>{_NAME})\"|(?P<escape>\\)?(?P<bare>{_NAME})"
)
_WORD_BREAKS = frozenset(" \t\n;&|()<>")
# A ``(`` after any other character (``<(``, ``>(``, ``a=(``, ``@(``, ``f()``)
# is read by a matched-parenthesis scan that ignores here-documents.
_SUBSHELL_OPENERS = frozenset(" \t\n;&|()")
_COMPLEX_PARAMETER_RE = re.compile(r"[\"'`$({\\\n]")
_MAX_HEREDOCS = 16
_MAX_HEREDOC_BODY_CHARS = 16_000


class _Unsplittable(Exception):
    """The command uses a construct whose here-document extent is inexact."""


@dataclass(frozen=True, slots=True)
class Heredoc:
    """One here-document body exactly as written between its delimiter lines."""

    delimiter: str
    quoted_delimiter: bool
    body: str

    def descriptor(self) -> dict[str, Any]:
        """Return the JSON form carried in the command descriptor."""
        return {
            "delimiter": self.delimiter,
            "quoted_delimiter": self.quoted_delimiter,
            "body": self.body,
        }

    def is_canonical(self) -> bool:
        """Whether the body is bounded and holds no line that could end it."""
        return (
            len(self.body) <= _MAX_HEREDOC_BODY_CHARS
            and "\x00" not in self.body
            and all(
                line.lstrip("\t") != self.delimiter
                for line in self.body.split("\n")
            )
        )


@dataclass(frozen=True, slots=True)
class ShellCommandRedaction:
    """A normalized command view plus private sensitivity classification."""

    tokens: tuple[str, ...]
    redactions: int
    literal_credentials: int
    heredocs: tuple[Heredoc, ...] = ()

    def descriptor(self) -> dict[str, Any]:
        """Return the reviewer's command descriptor; bodies only if split."""
        value: dict[str, Any] = {
            "tokens": list(self.tokens),
            "redactions": self.redactions,
            "literal_credentials": self.literal_credentials,
        }
        if self.heredocs:
            value["heredocs"] = [item.descriptor() for item in self.heredocs]
        return value


def _make_line_boundaries_explicit(command: str) -> str:
    """Render unquoted shell newlines as review-only command separators."""
    rendered: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        character = command[index]
        if character == "\\" and quote != "'" and index + 1 < len(command):
            following = command[index + 1]
            if following in {"\r", "\n"}:
                rendered.append(" ")
                index += 2
                if (
                    following == "\r"
                    and index < len(command)
                    and command[index] == "\n"
                ):
                    index += 1
                continue
            rendered.extend((character, following))
            index += 2
            continue
        if character == "'" and quote is None:
            quote = "'"
        elif character == "'" and quote == "'":
            quote = None
        elif character == '"' and quote is None:
            quote = '"'
        elif character == '"' and quote == '"':
            quote = None
        if character in {"\r", "\n"} and quote is None:
            rendered.append(" ; ")
            if (
                character == "\r"
                and index + 1 < len(command)
                and command[index + 1] == "\n"
            ):
                index += 1
        else:
            rendered.append(character)
        index += 1
    return "".join(rendered)


def shell_words(text: str) -> tuple[str, ...]:
    """Split shell text into the reviewer's token view.

    Raises ``ValueError`` when quoting is unbalanced.
    """
    lexer = shlex.shlex(
        _make_line_boundaries_explicit(text),
        posix=True,
        punctuation_chars=";&|<>",
    )
    lexer.whitespace_split = True
    lexer.commenters = ""
    return tuple(lexer)


def _replace_all(text: str, replacements: Mapping[str, str]) -> str:
    """Replace every occurrence of each key in one pass, longest key first."""
    if not replacements:
        return text
    pattern = re.compile(
        "|".join(
            re.escape(key)
            for key in sorted(replacements, key=len, reverse=True)
        )
    )
    return pattern.sub(lambda match: replacements[match.group()], text)


class _HeredocScanner:
    """Walk bash text the way bash finds here-documents.

    The scanner models plain words, quoting, escapes, comments, and
    here-document operators exactly. Any construct whose quoting or ``<<``
    meaning it does not model (substitution, arithmetic, ANSI-C quoting,
    nested parameter words, parentheses attached to a word) makes the command
    unsplittable, so a body is never reported where bash would execute shell
    text.

    Operators are accepted only on the command's first line. Bash parses that
    whole line, bodies included, before any of it runs, so no alias or shell
    option the command itself sets can change how the line is read; a later
    line has no such guarantee.
    """

    def __init__(self, command: str) -> None:
        self.command = command
        self.index = 0
        self.quote: str | None = None
        self.word_start = True
        self.first_line = True
        self.shell: list[str] = []
        self.pending: list[tuple[str, bool, bool]] = []
        self.heredocs: list[Heredoc] = []

    def run(self) -> tuple[str, tuple[Heredoc, ...]]:
        """Return the shell text with bodies removed, and the bodies."""
        while self.index < len(self.command):
            if self.quote is None and self.command[self.index] == "\n":
                self.first_line = False
                if self.pending:
                    self._read_bodies()
                    continue
            start = self.index
            self._advance()
            self.shell.append(self.command[start:self.index])
        if self.quote is not None or self.pending:
            raise _Unsplittable
        return "".join(self.shell), tuple(self.heredocs)

    def _advance(self) -> None:
        command, index = self.command, self.index
        character = command[index]
        if self.quote == "'":
            self.quote = None if character == "'" else "'"
            self.index += 1
            return
        self._reject_unmodeled()
        if self.quote == '"':
            if character == '"':
                self.quote = None
            self.index += 2 if character == "\\" else 1
            return
        word_start = self.word_start
        self.word_start = character in _WORD_BREAKS
        if command.startswith("\\\n", index):
            self.word_start = word_start
            self.index += 2
        elif character == "\\":
            self.index += 2
        elif character in {"'", '"'}:
            self.quote = character
            self.index += 1
        elif character == "#" and word_start:
            newline = command.find("\n", index)
            self.index = len(command) if newline < 0 else newline
        elif command.startswith("<<<", index):
            self.word_start = True
            self.index += 3
        elif command.startswith("<<", index):
            self._read_operator()
        else:
            self.index += 1

    def _reject_unmodeled(self) -> None:
        pair = self.command[self.index:self.index + 2]
        if pair.startswith("`") or pair in {"$(", "$["}:
            raise _Unsplittable
        if self.quote is None and pair in {"$'", "(("}:
            raise _Unsplittable
        if (
            self.quote is None
            and pair.startswith("(")
            and self.index > 0
            and self.command[self.index - 1] not in _SUBSHELL_OPENERS
        ):
            raise _Unsplittable
        if pair == "${":
            close = self.command.find("}", self.index)
            if close < 0 or _COMPLEX_PARAMETER_RE.search(
                self.command, self.index + 2, close
            ):
                raise _Unsplittable

    def _read_operator(self) -> None:
        if not self.first_line:
            raise _Unsplittable
        command = self.command
        cursor = self.index + 2
        strip_tabs = command.startswith("-", cursor)
        cursor += int(strip_tabs)
        while cursor < len(command) and command[cursor] in " \t":
            cursor += 1
        end = cursor
        while end < len(command) and command[end] not in _WORD_BREAKS:
            end += 1
        match = _DELIMITER_WORD_RE.fullmatch(command, cursor, end)
        if match is None:
            raise _Unsplittable
        bare = match.group("bare")
        delimiter = match.group("single") or match.group("double") or bare
        quoted = bare is None or match.group("escape") is not None
        self.pending.append((delimiter, quoted, strip_tabs))
        self.index = end

    def _read_bodies(self) -> None:
        command = self.command
        cursor = self.index + 1
        line_end = self.index
        for delimiter, quoted, strip_tabs in self.pending:
            lines: list[str] = []
            while True:
                if cursor >= len(command):
                    raise _Unsplittable
                newline = command.find("\n", cursor)
                line_end = len(command) if newline < 0 else newline
                line = command[cursor:line_end]
                cursor = line_end + 1
                if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                    break
                trailing = len(line) - len(line.rstrip("\\"))
                if not quoted and trailing % 2:
                    # Bash joins an escaped newline before matching the
                    # delimiter in an expanding body.
                    raise _Unsplittable
                lines.append(line)
            self.heredocs.append(Heredoc(delimiter, quoted, "\n".join(lines)))
        self.pending = []
        self.index = line_end


def split_heredocs(command: str) -> tuple[str, tuple[Heredoc, ...]] | None:
    """Separate here-document bodies from the shell text bash parses.

    Returns ``None`` when the command has no here-document, or when its
    here-document extents cannot be determined exactly.
    """
    try:
        shell, heredocs = _HeredocScanner(command).run()
    except _Unsplittable:
        return None
    if not heredocs or len(heredocs) > _MAX_HEREDOCS:
        return None
    if not all(heredoc.is_canonical() for heredoc in heredocs):
        return None
    return shell, heredocs


def _hidden_part(raw: str, safe: str, marker: str) -> str:
    """Return the part of ``raw`` that ``safe`` shows as its single marker."""
    prefix, found, suffix = safe.partition(marker)
    if (
        found
        and marker not in suffix
        and len(raw) >= len(prefix) + len(suffix)
        and raw.startswith(prefix)
        and raw.endswith(suffix)
    ):
        return raw[len(prefix):len(raw) - len(suffix)]
    return raw


def heredoc_view(
    command: str,
    raw_tokens: Sequence[str],
    safe_tokens: Sequence[str],
    marker: str,
) -> tuple[str, tuple[Heredoc, ...], tuple[str, ...]] | None:
    """Separate here-documents and apply the classifier's token rendering.

    ``safe_tokens`` is the classifier's rendering of ``raw_tokens``, with
    ``marker`` in place of each hidden value. Every hidden value is replaced
    throughout each body. Returns the remaining shell text, the rewritten
    here-documents, and the hidden values that must not survive in the shown
    view, or ``None`` to keep the token view.
    """
    split = split_heredocs(command)
    if split is None or len(raw_tokens) != len(safe_tokens):
        return None
    replaced: dict[str, str] = {}
    for raw, safe in zip(raw_tokens, safe_tokens):
        if raw == safe:
            continue
        hidden = _hidden_part(raw, safe, marker)
        rendered = marker if hidden != raw else safe
        if hidden and replaced.setdefault(hidden, rendered) != rendered:
            return None
    shell_text, heredocs = split
    shown = tuple(
        Heredoc(
            item.delimiter,
            item.quoted_delimiter,
            _replace_all(item.body, replaced),
        )
        for item in heredocs
    )
    return shell_text, shown, tuple(replaced)


def descriptor_heredocs(command: Mapping[str, Any]) -> tuple[Heredoc, ...]:
    """Parse a command descriptor's optional here-document list strictly."""
    if "heredocs" not in command:
        return ()
    value = command["heredocs"]
    if not isinstance(value, list) or not 0 < len(value) <= _MAX_HEREDOCS:
        raise ValueError("here-document list is invalid")
    heredocs: list[Heredoc] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "delimiter",
            "quoted_delimiter",
            "body",
        }:
            raise ValueError("here-document shape is invalid")
        delimiter = item["delimiter"]
        quoted = item["quoted_delimiter"]
        body = item["body"]
        if (
            not isinstance(delimiter, str)
            or _DELIMITER_RE.fullmatch(delimiter) is None
            or not isinstance(quoted, bool)
            or not isinstance(body, str)
        ):
            raise ValueError("here-document values are invalid")
        heredoc = Heredoc(delimiter, quoted, body)
        if not heredoc.is_canonical():
            raise ValueError("here-document body is invalid")
        heredocs.append(heredoc)
    return tuple(heredocs)
